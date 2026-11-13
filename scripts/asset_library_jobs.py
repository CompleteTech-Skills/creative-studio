"""Bounded local jobs for trusted asset adapters; canonical stores own publication.

The journal contains data only. Adapters are installed Python code and must use
an idempotent token-aware publisher plus an authoritative recovery probe.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Protocol, TypedDict
import uuid

from rig_errors import RigError, atomic_json, decode_json, job_lock, object_hash, timestamp

RESOURCES = ('cpu', 'blender', 'preview', 'archive', 'io', 'hash', 'index')
DEFAULT_LIMITS = dict(cpu=2, blender=1, preview=1, archive=1, io=2, hash=2, index=1,
                     runtime_seconds=300, attempts=3, jobs=4096, journal_bytes=1048576,
                     stage_bytes=268435456, stage_files=4096)
ACTIVE = {'running', 'staged', 'verified', 'publishing'}
STATUSES = {'queued', 'interrupted', 'running', 'staged', 'verified', 'publishing',
            'published', 'failed', 'cancelled'}
_IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9_-]{0,95}$')


class JobRecord(TypedDict):
    schema_version: int
    id: str
    kind: str
    sequence: int
    status: str
    parameters: dict
    parameters_sha256: str
    resources: list[str]
    estimates: dict
    limits: dict
    progress: float
    operation: str
    attempts: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    history: list[dict]
    staged: dict | None
    verified: dict | None
    receipt: dict | None
    error: dict | None


class JobAdapter(Protocol):
    def stage(self, context: 'JobContext') -> dict: ...
    def verify(self, context: 'JobContext', staged: dict) -> dict: ...
    def publish(self, context: 'JobContext', staged: dict, verified: dict) -> dict: ...
    def recover(self, context: 'JobContext', staged: dict, verified: dict) -> dict: ...


def fail(code, message):
    raise RigError('ASSET_JOB_' + code, message)


def _number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        fail('ID', 'Expected a bounded lowercase job identifier.')
    return value


def _limits(changes=None):
    if changes is not None and (not isinstance(changes, dict) or set(changes) - set(DEFAULT_LIMITS)):
        fail('LIMIT', 'Unknown job resource limit.')
    result = {**DEFAULT_LIMITS, **(changes or {})}
    for name, value in result.items():
        maximum = DEFAULT_LIMITS[name]
        if not _number(value, 1, maximum) or (name != 'runtime_seconds' and int(value) != value):
            fail('LIMIT', 'Limits must be positive finite values within the documented hard ceiling: ' + name)
    return result


def _json(value, maximum=1048576):
    pending, nodes = [(value, 0)], 0
    while pending:
        node, depth = pending.pop(); nodes += 1
        if depth > 32 or nodes > 65536:
            fail('LIMIT', 'Job JSON exceeds its nesting or node budget.')
        if type(node) is dict:
            if len(node) > 4096 or any(type(k) is not str or len(k) > 1024 for k in node):
                fail('JSON', 'Job JSON object keys must be bounded strings.')
            pending.extend((child, depth + 1) for child in node.values())
        elif type(node) is list:
            if len(node) > 65536: fail('LIMIT', 'Job JSON array is too large.')
            pending.extend((child, depth + 1) for child in node)
        elif type(node) is str:
            if len(node) > maximum: fail('LIMIT', 'Job JSON string is too large.')
        elif node is not None and type(node) not in (int, float, bool):
            fail('JSON', 'Job data must contain JSON values only.')
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise RigError('ASSET_JOB_JSON', 'Job data must be bounded finite JSON.') from exc
    if len(payload) > maximum:
        fail('LIMIT', 'Job JSON exceeds its byte budget.')
    return payload


def _safe_tree(path):
    """The private journal uses canonical paths, never links or special files."""
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                fail('PATH', 'Job paths cannot contain symlinks or reparse points.')
    return path


@contextmanager
def _lock(root):
    try:
        with job_lock(_safe_tree(root)):
            yield
    except RigError as exc:
        if exc.report['code'] == 'RIG_BUSY':
            fail('BUSY', 'Another cooperating job writer or worker owns this operation; retry later.')
        raise


@dataclass
class JobContext:
    manager: 'JobManager'
    id: str
    deadline: float

    @property
    def token(self) -> str:
        return self.id

    @property
    def stage_dir(self) -> Path:
        path = _safe_tree(self.manager.root / 'staging' / self.id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def parameters(self) -> dict:
        return deepcopy(self.manager._read(self.id)['parameters'])

    @property
    def limits(self) -> dict:
        return deepcopy(self.manager._read(self.id)['limits'])

    def check_cancelled(self) -> None:
        if time.monotonic() >= self.deadline:
            fail('TIMEOUT', 'Job runtime budget exhausted; staged evidence is retained.')
        if self.manager._cancel_path(self.id).exists():
            fail('CANCELLED', 'Job was cancelled; unpromoted staged evidence is retained.')
        self.manager._inventory(self.id, hash_contents=False)

    def progress(self, fraction: float, operation: str) -> None:
        self.check_cancelled()
        if not _number(fraction, 0, 1) or not isinstance(operation, str) or len(operation) > 256:
            fail('PROGRESS', 'Progress must be finite in [0,1] with a bounded operation label.')
        with _lock(self.manager.root / 'journal'):
            record = self.manager._read(self.id)
            record['progress'] = float(fraction)
            record['operation'] = operation
            self.manager._write(record)


class JobManager:
    def __init__(self, root: str | Path, *, limits: dict | None = None, create: bool = False):
        self.root = _safe_tree(root)
        self.limits = _limits(limits)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            for name in ('records', 'staging', 'leases', 'slots', 'cancel'):
                _safe_tree(self.root / name).mkdir(exist_ok=True)
        if not self.root.is_dir():
            fail('MISSING', 'Job store does not exist; explicitly create it first.')
        for name in ('records', 'staging', 'leases', 'slots', 'cancel'):
            if not _safe_tree(self.root / name).is_dir():
                fail('CORRUPT', 'Job store is missing an owned directory: ' + name)
        with _lock(self.root / 'journal'):
            config = _safe_tree(self.root / 'config.json')
            if not config.exists():
                if not create:
                    fail('CORRUPT', 'Job store has no durable resource configuration.')
                atomic_json(config, {'schema_version': 1, 'limits': self.limits})
            try:
                if not config.is_file() or config.stat().st_nlink != 1 or config.stat().st_size > 4096:
                    fail('CORRUPT', 'Invalid job resource configuration file.')
                saved = decode_json(config.read_bytes(), max_bytes=4096)
                if set(saved) != {'schema_version', 'limits'} or saved['schema_version'] != 1:
                    fail('CORRUPT', 'Invalid job resource configuration contract.')
                actual = _limits(saved['limits'])
            except (OSError, ValueError, TypeError) as exc:
                raise RigError('ASSET_JOB_CORRUPT', 'Cannot read job resource configuration.') from exc
            if limits is not None and self.limits != actual:
                fail('LIMIT', 'Resource configuration is immutable; reopen with the stored limits.')
            self.limits = actual

    def _path(self, identifier):
        return _safe_tree(self.root / 'records' / (_identifier(identifier) + '.json'))

    def _cancel_path(self, identifier):
        return _safe_tree(self.root / 'cancel' / (_identifier(identifier) + '.json'))

    def _read(self, identifier) -> JobRecord:
        path = self._path(identifier)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > self.limits['journal_bytes']:
                fail('CORRUPT', 'Job record is not a bounded owned regular file.')
            with path.open('rb') as source:
                data = source.read(self.limits['journal_bytes'] + 1)
            record = decode_json(data, max_bytes=self.limits['journal_bytes'])
        except (OSError, ValueError, RecursionError) as exc:
            raise RigError('ASSET_JOB_RECORD', 'Cannot load the requested job record.') from exc
        required = set(JobRecord.__annotations__)
        try:
            valid = not (not isinstance(record, dict) or set(record) != required or record['schema_version'] != 1
            or record['id'] != identifier or record['status'] not in STATUSES
            or not isinstance(record['parameters'], dict) or object_hash(record['parameters']) != record['parameters_sha256']
            or not isinstance(record['resources'], list) or not record['resources']
            or len(set(record['resources'])) != len(record['resources']) or set(record['resources']) - set(RESOURCES)
            or not _number(record['attempts'], 0, DEFAULT_LIMITS['attempts'])
            or not isinstance(record['history'], list) or len(record['history']) > 100
            or not _number(record['sequence'], 0, DEFAULT_LIMITS['jobs'])
            or not _number(record['progress'], 0, 1)
            or not isinstance(record['kind'], str) or not _IDENTIFIER.fullmatch(record['kind'])
            or not isinstance(record['estimates'], dict)
            or not isinstance(record['operation'], str) or len(record['operation']) > 256
            or any(record[k] is not None and not isinstance(record[k], dict) for k in ('staged', 'verified', 'receipt', 'error'))
            or any(record[k] is not None and not isinstance(record[k], str) for k in ('started_at', 'finished_at'))
            or not isinstance(record['created_at'], str)
            or (record['staged'] is not None and (set(record['staged']) != {'data', 'inventory'}
                or not isinstance(record['staged']['data'], dict) or not isinstance(record['staged']['inventory'], list)))
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            valid = False
        if not valid:
            fail('CORRUPT', 'Job journal identity, parameters, state or resource contract is invalid.')
        if _limits(record['limits']) != self.limits:
            fail('CORRUPT', 'Job limits differ from the durable store configuration.')
        _json(record, self.limits['journal_bytes'])
        return record

    def _write(self, record):
        _json(record, self.limits['journal_bytes'])
        atomic_json(self._path(record['id']), record)

    def _records(self):
        paths = sorted((self.root / 'records').iterdir())
        if len(paths) > self.limits['jobs']:
            fail('LIMIT', 'Job store exceeds its record ceiling.')
        result = []
        for path in paths:
            if path.suffix != '.json':
                fail('CORRUPT', 'Unexpected file in the job record inventory.')
            result.append(self._read(path.stem))
        return sorted(result, key=lambda value: (value['sequence'], value['id']))

    def submit(self, kind: str, parameters: dict, *, resources=('io',), estimates=None,
               key: str | None = None) -> JobRecord:
        _identifier(kind)
        if not isinstance(parameters, dict):
            fail('PARAMETERS', 'Job parameters must be an explicit JSON object.')
        if (not isinstance(resources, (tuple, list)) or not resources or any(type(r) is not str for r in resources)
            or set(resources) - set(RESOURCES) or len(set(resources)) != len(resources)):
            fail('RESOURCE', 'Choose unique supported resource pools.')
        estimates = {} if estimates is None else estimates
        if not isinstance(estimates, dict) or any(not isinstance(k, str) or not _number(v, 0, 2**53) for k, v in estimates.items()):
            fail('ESTIMATE', 'Resource estimates must contain finite nonnegative measurements.')
        _json(parameters, self.limits['journal_bytes'] // 4)
        identifier = _identifier(key) if key is not None else uuid.uuid4().hex
        with _lock(self.root / 'journal'):
            records = self._records()
            if self._path(identifier).exists():
                record = self._read(identifier)
                if (record['kind'], record['parameters'], record['resources'], record['estimates']) != (kind, parameters, list(resources), estimates):
                    fail('CONFLICT', 'Idempotency key already names a different request.')
                return deepcopy(record)
            if len(records) >= self.limits['jobs']:
                fail('LIMIT', 'Job record budget is exhausted.')
            record = JobRecord(schema_version=1, id=identifier, kind=kind,
                sequence=max((r['sequence'] for r in records), default=-1) + 1,
                status='queued', parameters=deepcopy(parameters), parameters_sha256=object_hash(parameters),
                resources=list(resources), estimates=deepcopy(estimates), limits=deepcopy(self.limits),
                progress=0., operation='queued', attempts=0, created_at=timestamp(), started_at=None,
                finished_at=None, history=[], staged=None, verified=None, receipt=None, error=None)
            self._write(record)
            return deepcopy(record)

    def list(self) -> list[JobRecord]:
        with _lock(self.root / 'journal'):
            records = self._records()
            queued = [r['id'] for r in records if r['status'] == 'queued']
            # Queue position is a view, deliberately not another persisted authority.
            return [dict(deepcopy(r), queue_position=queued.index(r['id']) + 1 if r['id'] in queued else None) for r in records]

    def status(self, identifier: str) -> JobRecord:
        record = self._read(identifier)
        if record['status'] in ACTIVE:
            try:
                with _lock(self.root / 'leases' / identifier):
                    with _lock(self.root / 'journal'):
                        record = self._read(identifier)
                        if record['status'] in ACTIVE:
                            record['status'] = 'interrupted'
                            record['operation'] = 'worker-exited'
                            self._write(record)
            except RigError as exc:
                if exc.report['code'] != 'ASSET_JOB_BUSY':
                    raise
        return deepcopy(record)

    def cancel(self, identifier: str) -> JobRecord:
        with _lock(self.root / 'journal'):
            record = self._read(identifier)
            if record['status'] == 'published':
                fail('PUBLISHED', 'Published output cannot be cancelled; use its canonical lifecycle API.')
            atomic_json(self._cancel_path(identifier), {'id': identifier, 'at': timestamp()})
            if record['status'] in ('queued', 'failed', 'interrupted', 'cancelled'):
                record['status'] = 'cancelled'
                record['finished_at'] = timestamp()
                self._write(record)
            return deepcopy(record)

    def resume(self, identifier: str) -> JobRecord:
        with _lock(self.root / 'leases' / identifier):
            with _lock(self.root / 'journal'):
                record = self._read(identifier)
                if record['status'] == 'published':
                    return deepcopy(record)
                if record['attempts'] >= record['limits']['attempts']:
                    fail('ATTEMPTS', 'Job retry ceiling exhausted; submit a corrected request with a new key.')
                self._cancel_path(identifier).unlink(missing_ok=True)
                record['status'] = 'queued'
                record['operation'] = 'resume-requested'
                record['error'] = None
                self._write(record)
                return deepcopy(record)

    def _inventory(self, identifier, *, hash_contents=True):
        stage = _safe_tree(self.root / 'staging' / identifier)
        if not stage.exists():
            return []
        limits = self._read(identifier)['limits']
        inventory, count, size = [], 0, 0
        for base, dirs, files in os.walk(stage, followlinks=False):
            for name in dirs:
                path = _safe_tree(Path(base) / name)
                if not path.is_dir(): fail('PATH', 'Invalid staging directory.')
            for name in files:
                path = _safe_tree(Path(base) / name)
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    fail('PATH', 'Staged output must be a single owned regular file.')
                count += 1; size += info.st_size
                if count > limits['stage_files'] or size > limits['stage_bytes']:
                    fail('LIMIT', 'Staged output exceeds its file or byte budget.')
                digest = hashlib.sha256()
                if hash_contents:
                    with path.open('rb') as source:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            digest.update(block)
                inventory.append(dict(path=path.relative_to(stage).as_posix(), bytes=info.st_size, sha256=digest.hexdigest() if hash_contents else None))
        return sorted(inventory, key=lambda r: r['path'])

    def _transition(self, identifier, state, **changes):
        with _lock(self.root / 'journal'):
            record = self._read(identifier)
            record.update(status=state, operation=state, **deepcopy(changes))
            record['history'] = [*record['history'], dict(status=state, at=timestamp(), attempt=record['attempts'])][-100:]
            self._write(record)
            return record

    def run(self, identifier: str, adapter: JobAdapter) -> JobRecord:
        """Run synchronously in a worker process; different processes share OS slots.

        Adapters must check context cancellation during bounded expensive loops.
        Subprocesses must independently enforce the same deadline/resource limits.
        """
        _identifier(identifier)
        with _lock(self.root / 'leases' / identifier), ExitStack() as slots:
            with _lock(self.root / 'journal'):
                record = self._read(identifier)
                if record['status'] == 'published': return deepcopy(record)
                if record['status'] == 'cancelled': fail('CANCELLED', 'Explicitly resume this cancelled job before running.')
                if record['status'] != 'queued':
                    fail('RESUME', 'Explicitly resume a failed or interrupted job before retrying.')
                earlier = [r for r in self._records() if r['sequence'] < record['sequence'] and r['status'] == 'queued' and set(r['resources']) & set(record['resources'])]
                if earlier: fail('QUEUED', 'An earlier queued job owns priority in a requested resource pool.')
                for resource in sorted(record['resources']):
                    acquired = False
                    for index in range(record['limits'][resource]):
                        try:
                            slots.enter_context(_lock(self.root / 'slots' / (resource + '-' + str(index))))
                            acquired = True; break
                        except RigError as exc:
                            if exc.report['code'] != 'ASSET_JOB_BUSY': raise
                    if not acquired: fail('BUSY', 'Requested worker pool is at capacity: ' + resource)
                if record['attempts'] >= record['limits']['attempts']: fail('ATTEMPTS', 'Job retry ceiling exhausted.')
                record.update(status='running', attempts=record['attempts'] + 1, started_at=timestamp(), finished_at=None, error=None)
                record['history'] = [*record['history'], dict(status='running', at=record['started_at'], attempt=record['attempts'])][-100:]
                self._write(record)
            context = JobContext(self, identifier, time.monotonic() + record['limits']['runtime_seconds'])
            try:
                context.check_cancelled()
                staged = record['staged']
                if staged is None:
                    data = adapter.stage(context)
                    if not isinstance(data, dict): fail('ADAPTER', 'Stage must return a JSON object.')
                    staged = dict(data=data, inventory=self._inventory(identifier))
                    self._transition(identifier, 'staged', staged=staged)
                elif staged['inventory'] != self._inventory(identifier):
                    fail('CHANGED', 'Retained staging inventory changed; do not replay or publish this job.')
                context.check_cancelled()
                verified = record['verified']
                if verified is None:
                    verified = adapter.verify(context, deepcopy(staged['data']))
                    if not isinstance(verified, dict): fail('ADAPTER', 'Verification must return a JSON object.')
                    self._transition(identifier, 'verified', verified=verified)
                if staged['inventory'] != self._inventory(identifier):
                    fail('CHANGED', 'Verification changed staged output; publication is refused.')
                context.check_cancelled()
                # Recovery always probes the canonical publisher before publication,
                # including after an exit between publication and receipt journaling.
                recovered = adapter.recover(context, deepcopy(staged['data']), deepcopy(verified))
                if not isinstance(recovered, dict) or recovered.get('status') not in ('published', 'unpublished'):
                    fail('RECOVERY', 'Publisher could not establish authoritative publication state.')
                with _lock(self.root / 'journal'):
                    context.check_cancelled()
                    current = self._read(identifier)
                    current['status'] = 'publishing'; current['operation'] = 'publishing'
                    self._write(current)
                    receipt = recovered.get('receipt') if recovered['status'] == 'published' else adapter.publish(context, deepcopy(staged['data']), deepcopy(verified))
                    if not isinstance(receipt, dict): fail('ADAPTER', 'Publisher must return its exact JSON receipt.')
                    current.update(status='published', operation='published', receipt=receipt,
                                   progress=1., finished_at=timestamp())
                    current['history'] = [*current['history'], dict(status='published', at=current['finished_at'], attempt=current['attempts'])][-100:]
                    self._write(current)
                    return deepcopy(current)
            except Exception as exc:
                error = exc.report if isinstance(exc, RigError) else dict(code='ASSET_JOB_ADAPTER', message=str(exc)[:2048])
                state = 'cancelled' if error.get('code') == 'ASSET_JOB_CANCELLED' else 'failed'
                self._transition(identifier, state, error=error, finished_at=timestamp())
                if isinstance(exc, RigError): raise
                raise RigError('ASSET_JOB_ADAPTER', 'Trusted adapter failed; staged evidence retained.') from exc
