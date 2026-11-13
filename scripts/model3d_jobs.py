"""Bounded local Model3D jobs with immutable, verified output checkpoints.

Handlers are trusted Python functions, never commands decoded from input files.
They receive private output directories and must cooperate with cancellation and
runtime checks through Context.check(). This is not an arbitrary-code sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
import hashlib
import os
import shutil
import struct
import tempfile
import time
import unicodedata
import uuid
import zipfile

from atomic_publish import _rename_no_replace
from rig_errors import (RigError, atomic_json, finite_json, job_lock, object_hash,
                        open_payload, portable_name, read_json, require, safe_path,
                        sha256, timestamp)

DEFAULT_BUDGETS = dict(files=4096, input_bytes=256*1024**2, output_bytes=1024**3,
                       attempts=3, runtime_seconds=120.)
_STATUS = {'queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'}


def _alias(name):
    """Canonical portable collision key; preserve literal source names/bytes."""
    return unicodedata.normalize('NFC', portable_name(name)).casefold()


def _unique_names(names):
    """Check file and implicit directory aliases without touching the filesystem."""
    seen = {}
    for name in names:
        portable_name(name)
        parts = name.split('/')
        # Native payload trees have at most32directories; archive envelopes add
        # a small fixed prefix. Bound prefix work even for hostile ZIP names.
        if len(parts) > 40:return False
        for index in range(1,len(parts)+1):
            prefix = '/'.join(parts[:index]);kind = 'file' if index == len(parts) else 'directory'
            key = _alias(prefix);prior = seen.get(key)
            if prior is not None and (prior != (prefix,kind) or kind == 'file'):return False
            seen[key] = (prefix,kind)
    return True


def _budgets(value):
    require(isinstance(value, dict) and set(value) <= set(DEFAULT_BUDGETS),
            'MODEL3D_JOB_BUDGET', 'Unknown job budget.')
    result = {**DEFAULT_BUDGETS, **value}
    for key, maximum in dict(files=10000, input_bytes=1024**3, output_bytes=4*1024**3,
                             attempts=100, runtime_seconds=86400.).items():
        item = result[key]
        require(type(item) in ((int, float) if key == 'runtime_seconds' else (int,))
                and 0 < item <= maximum, 'MODEL3D_JOB_BUDGET', 'Invalid job budget: '+key)
    finite_json(result)
    return result


def _request(value):
    require(isinstance(value, dict) and set(value) == {'engine', 'parameters'},
            'MODEL3D_JOB_REQUEST', 'Requests contain engine and parameters only.')
    require(isinstance(value['engine'], str) and 0 < len(value['engine']) <= 128
            and all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in value['engine']),
            'MODEL3D_JOB_REQUEST', 'Engine must be a stable local identifier.')
    require(isinstance(value['parameters'], dict), 'MODEL3D_JOB_REQUEST', 'Parameters must be an object.')
    finite_json(value)
    require(len(str(value)) <= 1024**2, 'MODEL3D_JOB_REQUEST', 'Request exceeds its bounded size.')
    return deepcopy(value)


def _root(root):
    root = Path(root).absolute()
    require(not root.is_symlink(), 'MODEL3D_JOB_PATH', 'A job root cannot be a symbolic link.')
    return root


def _inventory(root, *, files, bytes_):
    """Bound traversal before payload reads; reject aliases and every symlink."""
    records, total, folded = [], 0, set()
    pending = [(Path(root), 0)]
    entries = 0
    while pending:
        directory, depth = pending.pop()
        require(depth <= 32, 'MODEL3D_JOB_LIMIT', 'Output directory nesting exceeds 32.')
        with os.scandir(directory) as children:
            for item in children:
                entries += 1
                require(entries <= files*2, 'MODEL3D_JOB_LIMIT', 'Directory inventory exceeds its entry budget.')
                require(not item.is_symlink(), 'MODEL3D_JOB_PATH', 'Symbolic links are not job payloads.')
                relative = Path(item.path).relative_to(root).as_posix()
                alias = _alias(relative)
                require(alias not in folded, 'MODEL3D_JOB_PATH', 'Case/Unicode-aliased payload paths.')
                folded.add(alias)
                if item.is_dir(follow_symlinks=False):
                    pending.append((Path(item.path), depth+1))
                else:
                    require(item.is_file(follow_symlinks=False), 'MODEL3D_JOB_PATH', 'Payloads must be regular files.')
                    size = item.stat(follow_symlinks=False).st_size
                    total += size
                    require(len(records) < files and total <= bytes_, 'MODEL3D_JOB_LIMIT', 'Payload inventory exceeds its budget.')
                    with open_payload(root, relative) as (stream, before):
                        digest, count = hashlib.sha256(), 0
                        for chunk in iter(lambda: stream.read(1024**2), b''):
                            count += len(chunk)
                            require(count <= size, 'MODEL3D_JOB_LIMIT', 'Payload grew beyond admitted size.')
                            digest.update(chunk)
                        require(count == before.st_size == size, 'MODEL3D_JOB_CHANGED', 'Payload changed during inventory.')
                    records.append(dict(path=relative, bytes=size, sha256=digest.hexdigest()))
    return sorted(records, key=lambda item: item['path'])


def _verify(root, records, budgets, *, input_=False):
    require(isinstance(records, list) and len(records) <= budgets['files'],
            'MODEL3D_JOB_CHECKPOINT', 'Malformed checkpoint file inventory.')
    actual = _inventory(root, files=budgets['files'],
                        bytes_=budgets['input_bytes' if input_ else 'output_bytes'])
    require(actual == records, 'MODEL3D_JOB_CHECKPOINT', 'Checkpoint payload inventory or hashes changed.')


def _load(root):
    root = _root(root)
    value = read_json(safe_path(root, 'job.json', exists=True))
    required = {'schema_version', 'kind', 'id', 'request', 'request_sha256', 'inputs',
                'budgets', 'status', 'attempts', 'active_attempt', 'current_revision',
                'revisions', 'error', 'created_at', 'updated_at'}
    require(isinstance(value, dict) and set(value) == required
            and type(value['schema_version']) is int and value['schema_version'] == 1
            and value['kind'] == 'model3d-local-job',
            'MODEL3D_JOB_SCHEMA', 'Unsupported local job record.')
    _request(value['request'])
    require(_budgets(value['budgets']) == value['budgets'], 'MODEL3D_JOB_SCHEMA', 'Incomplete stored budgets.')
    require(value['request_sha256'] == object_hash(value['request'])
            and isinstance(value['status'], str) and value['status'] in _STATUS and type(value['attempts']) is int
            and 0 <= value['attempts'] <= value['budgets']['attempts']
            and isinstance(value['id'], str) and len(value['id']) == 32
            and all(c in '0123456789abcdef' for c in value['id'])
            and isinstance(value['revisions'], list) and len(value['revisions']) <= 100
            and all(isinstance(item, str) for item in value['revisions'])
            and len(set(value['revisions'])) == len(value['revisions']),
            'MODEL3D_JOB_SCHEMA', 'Invalid local job fields.')
    require(value['current_revision'] is None or (isinstance(value['current_revision'], str)
            and value['current_revision'] in value['revisions']),
            'MODEL3D_JOB_SCHEMA', 'Current revision is absent from history.')
    require((value['status'] == 'running' and isinstance(value['active_attempt'], dict))
            or (value['status'] != 'running' and value['active_attempt'] is None),
            'MODEL3D_JOB_SCHEMA', 'Active attempt must match the job state.')
    require(value['status'] != 'completed' or (value['current_revision'] is not None and value['attempts'] > 0),
            'MODEL3D_JOB_SCHEMA', 'Completed jobs require an output revision and an execution attempt.')
    return value


def _save(root, value):
    value['updated_at'] = timestamp()
    atomic_json(Path(root)/'job.json', value)


def create_job(root, request, *, inputs=None, budgets=None):
    """Freeze an explicit local request and sources; publish a new job only."""
    root = _root(root); request = _request(request); budgets = _budgets(budgets or {})
    inputs = inputs or {}
    require(isinstance(inputs, dict) and len(inputs) <= budgets['files'],
            'MODEL3D_JOB_LIMIT', 'Too many job inputs.')
    names = [portable_name(name) for name in inputs]
    require(_unique_names(names),
            'MODEL3D_JOB_PATH', 'Input aliases are not portable.')
    with job_lock(root):
        require(not root.exists() and not root.is_symlink(), 'MODEL3D_JOB_EXISTS', 'Job destination must be new.')
        with tempfile.TemporaryDirectory(prefix='.model-job-', dir=root.parent) as temp:
            stage = Path(temp)/'job'; (stage/'inputs').mkdir(parents=True)
            total = 0
            for name, path in inputs.items():
                source = Path(path).absolute()
                target = safe_path(stage/'inputs', name); target.parent.mkdir(parents=True, exist_ok=True)
                with open_payload(source.parent, source.name) as (stream, info):
                    require(total+info.st_size <= budgets['input_bytes'], 'MODEL3D_JOB_LIMIT', 'Input bytes exceed the budget.')
                    count = 0
                    with target.open('xb') as out:
                        for chunk in iter(lambda: stream.read(1024**2), b''):
                            count += len(chunk)
                            require(total+count <= budgets['input_bytes'], 'MODEL3D_JOB_LIMIT', 'Input grew beyond budget.')
                            out.write(chunk)
                        out.flush(); os.fsync(out.fileno())
                    require(count == info.st_size, 'MODEL3D_JOB_CHANGED', 'Input changed during capture.')
                    total += count
            inventory = _inventory(stage/'inputs', files=budgets['files'], bytes_=budgets['input_bytes'])
            value = dict(schema_version=1, kind='model3d-local-job', id=uuid.uuid4().hex,
                         request=request, request_sha256=object_hash(request), inputs=inventory,
                         budgets=budgets, status='queued', attempts=0, active_attempt=None,
                         current_revision=None, revisions=[], error=None,
                         created_at=timestamp(), updated_at=timestamp())
            _save(stage, value); _load(stage)
            _rename_no_replace(stage, root)
    return status(root)


@dataclass
class Context:
    root: Path
    inputs: Path
    output: Path
    request: dict
    budgets: dict
    started: float

    def check(self):
        require(not (self.root/'cancel.json').exists(), 'MODEL3D_JOB_CANCELLED', 'Cancellation was requested.')
        require(time.monotonic()-self.started <= self.budgets['runtime_seconds'],
                'MODEL3D_JOB_TIMEOUT', 'Cooperative job runtime budget exceeded.')


def _revision(root, value, name):
    portable_name(name)
    require(name.startswith('revisions/') and len(Path(name).parts) == 2,
            'MODEL3D_JOB_CHECKPOINT', 'Malformed revision path.')
    folder = safe_path(root, name)
    require(folder.is_dir(), 'MODEL3D_JOB_CHECKPOINT', 'Revision directory is missing.')
    receipt = read_json(safe_path(folder, 'receipt.json', exists=True))
    require(isinstance(receipt, dict) and set(receipt) == {'schema_version', 'job_id', 'request_sha256',
            'inputs_sha256', 'handler_version', 'files', 'attempt', 'revision'}
            and type(receipt['schema_version']) is int and receipt['schema_version'] == 1
            and receipt['job_id'] == value['id'] and receipt['request_sha256'] == value['request_sha256']
            and receipt['inputs_sha256'] == object_hash(value['inputs']) and receipt['revision'] == name,
            'MODEL3D_JOB_CHECKPOINT', 'Checkpoint belongs to different inputs or job.')
    _verify(folder/'output', receipt['files'], value['budgets'])
    return receipt


def _recover(root, value, handler_version):
    active = value['active_attempt']
    if value['status'] != 'running':
        return False
    require(isinstance(active, dict) and set(active) == {'token', 'revision', 'handler_version'}
            and isinstance(active['token'], str) and len(active['token']) == 32
            and all(c in '0123456789abcdef' for c in active['token'])
            and active['revision'] == f"revisions/{value['attempts']:06d}-{active['token']}"
            and active['revision'] not in value['revisions'] and active['handler_version'] == handler_version,
            'MODEL3D_JOB_CHECKPOINT', 'Interrupted job handler differs; explicit repair is required.')
    folder = safe_path(root, active['revision'])
    if folder.exists():
        receipt = _revision(root, value, active['revision'])
        require(receipt['attempt'] == value['attempts'] and receipt['handler_version'] == handler_version,
                'MODEL3D_JOB_CHECKPOINT', 'Interrupted checkpoint attempt differs.')
        value['current_revision'] = active['revision']; value['revisions'].append(active['revision'])
        value['status'] = 'completed'; value['active_attempt'] = None; value['error'] = None
        _save(root, value)
        return True
    value['status'] = 'interrupted'; value['active_attempt'] = None
    _save(root, value)
    return False


def run_job(root, handler, *, handler_version, retry=False):
    """Execute a trusted local handler into a private, publish-on-success revision.

    Handler version must identify its code/dependency configuration. A recovered
    published checkpoint is verified and committed without replaying the handler.
    Uncheckpointed interrupted attempts require an explicit retry.
    """
    root = _root(root)
    require(callable(handler) and isinstance(handler_version, str) and 0 < len(handler_version) <= 128,
            'MODEL3D_JOB_HANDLER', 'A trusted handler and explicit version are required.')
    with job_lock(root):
        value = _load(root); _verify(root/'inputs', value['inputs'], value['budgets'], input_=True)
        if _recover(root, value, handler_version):
            return status(root)
        if value['status'] == 'completed' and not retry:
            receipt = _revision(root, value, value['current_revision'])
            require(receipt['handler_version'] == handler_version, 'MODEL3D_JOB_HANDLER',
                    'Completed output uses different handler code; request an explicit retry.')
            return status(root)
        require(value['status'] == 'queued' or retry, 'MODEL3D_JOB_RETRY', 'Failed/interrupted/cancelled jobs require explicit retry.')
        if (root/'cancel.json').exists():
            value.update(status='cancelled', active_attempt=None,
                         error=dict(code='MODEL3D_JOB_CANCELLED', message='Cancellation was requested.'))
            _save(root, value)
            require(False, 'MODEL3D_JOB_CANCELLED', 'Clear cancellation explicitly before retry.')
        require(value['attempts'] < value['budgets']['attempts'], 'MODEL3D_JOB_ATTEMPTS', 'Attempt budget is exhausted.')
        token = uuid.uuid4().hex; attempt = value['attempts']+1
        revision = f'revisions/{attempt:06d}-{token}'
        value.update(status='running', attempts=attempt, error=None,
                     active_attempt=dict(token=token, revision=revision, handler_version=handler_version))
        _save(root, value)
        running_record = deepcopy(value)
        try:
            with tempfile.TemporaryDirectory(prefix='.attempt-', dir=root) as temp:
                stage = Path(temp)/'checkpoint'; (stage/'output').mkdir(parents=True)
                context = Context(root, root/'inputs', stage/'output', deepcopy(value['request']),
                                  deepcopy(value['budgets']), time.monotonic())
                context.check(); handler(context); context.check()
                _verify(root/'inputs', value['inputs'], value['budgets'], input_=True)
                records = _inventory(context.output, files=value['budgets']['files'], bytes_=value['budgets']['output_bytes'])
                require(bool(records), 'MODEL3D_JOB_EMPTY', 'A successful job must contain real output payloads.')
                context.check()
                receipt = dict(schema_version=1, job_id=value['id'], request_sha256=value['request_sha256'],
                               inputs_sha256=object_hash(value['inputs']), handler_version=handler_version,
                               files=records, attempt=attempt, revision=revision)
                atomic_json(stage/'receipt.json', receipt)
                target = safe_path(root, revision); target.parent.mkdir(exist_ok=True)
                _rename_no_replace(stage, target)
                # A process exit after publication is recoverable from this exact
                # checkpoint. The active manifest is the final write.
                _revision(root, value, revision)
                value['current_revision'] = revision; value['revisions'].append(revision)
                value.update(status='completed', active_attempt=None, error=None)
                _save(root, value)
        except Exception as exc:
            # Completion mutates the candidate before its final manifest write.
            # Recover the original running token/history if that write fails;
            # retaining the mutated list/token would make recovery impossible.
            value = deepcopy(running_record)
            value['status'] = 'cancelled' if isinstance(exc, RigError) and exc.report['code'] == 'MODEL3D_JOB_CANCELLED' else 'failed'
            value['error'] = exc.report if isinstance(exc, RigError) else dict(code='MODEL3D_JOB_HANDLER', message=str(exc)[:4096])
            # Keep a published checkpoint recoverable if final manifest I/O failed.
            if not safe_path(root, revision).exists(): value['active_attempt'] = None
            else: value['status'] = 'running'
            _save(root, value)
            raise
    return status(root)


def status(root):
    root = _root(root); value = _load(root)
    _verify(root/'inputs', value['inputs'], value['budgets'], input_=True)
    if value['current_revision'] is not None: _revision(root, value, value['current_revision'])
    return deepcopy(value)


def cancel_job(root):
    """Signal running cooperative work without waiting for its writer lock."""
    root = _root(root); value = _load(root)
    atomic_json(root/'cancel.json', dict(schema_version=1, job_id=value['id'], requested_at=timestamp()))
    return dict(job_id=value['id'], cancellation_requested=True)


def clear_cancellation(root):
    root = _root(root)
    with job_lock(root):
        value = _load(root)
        require(value['status'] != 'running', 'MODEL3D_JOB_BUSY', 'Recover active work before clearing cancellation.')
        (root/'cancel.json').unlink(missing_ok=True)
    return dict(job_id=value['id'], cancellation_requested=False)


def run_batch(jobs, handler, *, handler_version, retry=False):
    """Bounded sequential CPU fallback; one failure preserves sibling outputs."""
    require(isinstance(jobs, list) and 0 < len(jobs) <= 128, 'MODEL3D_BATCH_LIMIT', 'Choose one to 128 jobs.')
    results = []
    for root in jobs:
        try:
            result = run_job(root, handler, handler_version=handler_version, retry=retry)
            results.append(dict(root=str(root), ok=True, status=result['status']))
        except Exception as exc:
            results.append(dict(root=str(root), ok=False,
                                error=exc.report if isinstance(exc, RigError) else dict(
                                    code='MODEL3D_IO' if isinstance(exc,OSError) else 'MODEL3D_JOB_HANDLER',
                                    message=str(exc)[:4096])))
    return dict(schema_version=1, results=results, succeeded=sum(item['ok'] for item in results))


def archive_job(root, destination):
    """Archive only declared immutable payloads and verified job state."""
    import json
    root = _root(root); destination = Path(destination).absolute()
    require(not destination.is_relative_to(root) and not destination.exists() and not destination.is_symlink(),
            'MODEL3D_ARCHIVE_PATH', 'Archive needs a new destination outside the job.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with job_lock(root):
        value = status(root)
        require(value['status'] != 'running', 'MODEL3D_JOB_BUSY', 'Recover running work before packaging.')
        names = ['job.json'] + ['inputs/'+item['path'] for item in value['inputs']]
        for revision in value['revisions']:
            receipt = _revision(root, value, revision)
            names += [revision+'/receipt.json'] + [revision+'/output/'+item['path'] for item in receipt['files']]
        if (root/'cancel.json').exists(): names.append('cancel.json')
        require(len(names) <= 10000 and len(set(names)) == len(names), 'MODEL3D_ARCHIVE_LIMIT', 'Archive inventory exceeds its file budget.')
        records, total = [], 0
        for name in names:
            path = safe_path(root, name, exists=True)
            total += path.stat().st_size
            require(total <= 4*1024**3, 'MODEL3D_ARCHIVE_LIMIT', 'Archive exceeds its 4GiB expanded byte budget.')
            records.append(dict(path=name, bytes=path.stat().st_size, sha256=sha256(path)))
        receipt = dict(schema_version=1, kind='model3d-job-archive', job_id=value['id'], files=records)
        with tempfile.TemporaryDirectory(prefix='.model-archive-', dir=destination.parent) as temp:
            stage = Path(temp)/'archive.zip'
            with zipfile.ZipFile(stage, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
                for record in records:
                    with open_payload(root, record['path']) as (stream, info):
                        require(info.st_size == record['bytes'], 'MODEL3D_ARCHIVE_CHANGED', 'Payload changed before archive.')
                        digest, count = hashlib.sha256(), 0
                        with archive.open(record['path'], 'w', force_zip64=True) as out:
                            for chunk in iter(lambda: stream.read(1024**2), b''):
                                count += len(chunk)
                                require(count <= record['bytes'], 'MODEL3D_ARCHIVE_CHANGED', 'Payload grew during archive.')
                                digest.update(chunk); out.write(chunk)
                        require(count == record['bytes'] and digest.hexdigest() == record['sha256'],
                                'MODEL3D_ARCHIVE_CHANGED', 'Payload changed during archive.')
                archive.writestr('archive-receipt.json', json.dumps(receipt, allow_nan=False))
            _verify_archive(stage)
            _rename_no_replace(stage, destination)
    return dict(path=str(destination), sha256=sha256(destination), job_id=value['id'], files=len(records))


def _zip_preflight(stream, size):
    """Bound the central directory before zipfile allocates its entry objects."""
    require(22 <= size <= 4*1024**3, 'MODEL3D_ARCHIVE_LIMIT', 'Invalid encoded archive size.')
    tail_start = max(0, size-65557)
    stream.seek(tail_start); tail = stream.read(min(size, 65557))
    end = tail.rfind(b'PK\x05\x06')
    require(end >= 0 and len(tail)-end >= 22, 'MODEL3D_ARCHIVE_INVALID', 'Missing ZIP end directory.')
    # zipfile reads an adjacent ZIP64 locator in preference to classic EOCD
    # fields. Reject that unsupported authority before constructing ZipFile;
    # otherwise its larger real central directory bypasses our allocation cap.
    # Maximal ZIP comments can place the locator just before the tail window.
    absolute_end = tail_start+end
    locator = b''
    if absolute_end >= 20:
        stream.seek(absolute_end-20); locator = stream.read(20)
    require(locator[:4] != b'PK\x06\x07',
            'MODEL3D_ARCHIVE_LIMIT', 'ZIP64 end directories are not supported.')
    _, disk, directory_disk, local_count, count, directory_bytes, offset, comment = struct.unpack_from('<4s4H2LH', tail, end)
    require(end+22+comment == len(tail) and disk == directory_disk == 0
            and local_count == count and 1 < count <= 10001
            and directory_bytes <= 8*1024**2 and offset+directory_bytes <= size,
            'MODEL3D_ARCHIVE_LIMIT', 'Unsupported split/ZIP64 directory or excessive central inventory.')
    stream.seek(0)


def _verify_archive(path, destination=None):
    from rig_errors import decode_json, MAX_JSON_BYTES
    source = Path(path).absolute()
    with open_payload(source.parent, source.name) as (stream, info):
        _zip_preflight(stream, info.st_size)
        return _verify_open_archive(stream, destination)


def _verify_open_archive(stream, destination):
    from rig_errors import decode_json, MAX_JSON_BYTES
    with zipfile.ZipFile(stream) as archive:
        entries = archive.infolist(); names = [item.filename for item in entries]
        require(1 < len(entries) <= 10001 and _unique_names(names),
                'MODEL3D_ARCHIVE_LIMIT', 'Duplicate, aliased or excessive ZIP members.')
        total = 0
        for item in entries:
            require(item.filename == item.orig_filename and not item.is_dir()
                    and (item.external_attr >> 16) & 0o170000 in (0, 0o100000),
                    'MODEL3D_ARCHIVE_PATH', 'Archive contains nonregular payloads.')
            portable_name(item.filename)
            total += item.file_size
            require(total <= 4*1024**3, 'MODEL3D_ARCHIVE_LIMIT', 'Expanded archive exceeds 4GiB.')
        require('archive-receipt.json' in names and archive.getinfo('archive-receipt.json').file_size <= MAX_JSON_BYTES,
                'MODEL3D_ARCHIVE_SCHEMA', 'Missing or oversized archive inventory.')
        receipt = decode_json(archive.read('archive-receipt.json'))
        require(isinstance(receipt, dict) and set(receipt) == {'schema_version', 'kind', 'job_id', 'files'}
                and type(receipt['schema_version']) is int and receipt['schema_version'] == 1
                and receipt['kind'] == 'model3d-job-archive' and isinstance(receipt['files'], list)
                and 0 < len(receipt['files']) <= 10000,
                'MODEL3D_ARCHIVE_SCHEMA', 'Malformed archive inventory.')
        records = receipt['files']
        require(all(isinstance(item, dict) and set(item) == {'path', 'bytes', 'sha256'}
                    and isinstance(item['path'], str) and type(item['bytes']) is int and item['bytes'] >= 0
                    and isinstance(item['sha256'], str) and len(item['sha256']) == 64
                    and all(c in '0123456789abcdef' for c in item['sha256']) for item in records),
                'MODEL3D_ARCHIVE_SCHEMA', 'Invalid archive payload record.')
        require(all(item['path'] != 'archive-receipt.json' for item in records)
                and len({item['path'] for item in records}) == len(records)
                and set(names) == {item['path'] for item in records} | {'archive-receipt.json'},
                'MODEL3D_ARCHIVE_SCHEMA', 'Archive members differ from their inventory.')
        for record in records:
            require(archive.getinfo(record['path']).file_size == record['bytes'],
                    'MODEL3D_ARCHIVE_CHANGED', 'Archive size differs from its record.')
            digest, count = hashlib.sha256(), 0
            target = safe_path(destination, record['path']) if destination is not None else None
            if target is not None: target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(record['path']) as stream:
                with target.open('xb') if target is not None else open(os.devnull, 'wb') as out:
                    for chunk in iter(lambda: stream.read(1024**2), b''):
                        count += len(chunk)
                        require(count <= record['bytes'], 'MODEL3D_ARCHIVE_LIMIT', 'Expanded archive payload exceeded its declared bound.')
                        digest.update(chunk); out.write(chunk)
            require(count == record['bytes'] and digest.hexdigest() == record['sha256'],
                    'MODEL3D_ARCHIVE_CHANGED', 'Archive payload hash differs from its record.')
    return receipt


def restore_job(archive, destination):
    """Validate bounded ZIP records then publish into a new directory only."""
    destination = _root(destination)
    with job_lock(destination):
        require(not destination.exists() and not destination.is_symlink(), 'MODEL3D_JOB_EXISTS', 'Restore destination must be new.')
        with tempfile.TemporaryDirectory(prefix='.model-restore-', dir=destination.parent) as temp:
            stage = Path(temp)/'job'; stage.mkdir()
            try:
                receipt = _verify_archive(archive, stage)
            except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
                raise RigError('MODEL3D_ARCHIVE_INVALID', 'Invalid ZIP payload: '+str(exc)[:1024]) from exc
            # Empty original inputs need an explicit directory after ZIP extraction.
            (stage/'inputs').mkdir(exist_ok=True)
            value = status(stage)
            require(value['id'] == receipt['job_id'], 'MODEL3D_ARCHIVE_SCHEMA', 'Archive identity differs from restored job.')
            for revision in value['revisions']: _revision(stage, value, revision)
            _rename_no_replace(stage, destination)
    return status(destination)
