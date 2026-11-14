"""Portable production planning, exact review evidence, and acquisition handoffs.

The production layer never invokes an image provider. Bound acquisition runs
retain hatch's original claim/token, reference, failure, and reconciliation
contract. Review receipts are explicit attestations, not inferred art approval.
"""
from __future__ import annotations
import copy
from contextlib import ExitStack
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from PIL import Image
from asset_contracts import check_image, hash_object, strict_keys, text
import hatch
from portable_runs import MAX_ARCHIVE_BYTES, verify_archive

WORKFLOW_VERSION = 2
WORKFLOW_FILE = 'production-workflow.json'
REVIEW_DIMENSIONS = ('identity_construction', 'pose_motion', 'facial_dialogue',
    'contacts_interactions', 'camera_composition', 'continuity', 'timing_av_sync', 'final_artifacts')
MAX_ARCHIVE_FILES = 10000
MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_REVIEW_BYTES = 8 * 1024 * 1024


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('production reference must be a portable relative path: ' + str(relative))
    value = PurePosixPath(relative)
    if value.is_absolute() or '..' in value.parts or '.' == relative or value.as_posix() != relative:
        raise ValueError('production reference escapes project or is not canonical: ' + relative)
    root = Path(root).resolve()
    target = root.joinpath(*value.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError('production reference resolves outside project: ' + relative)
    current = root
    for part in value.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('production reference contains a symlink: ' + relative)
    return target


def _bundle(root, project=None, verify_hashes=True):
    from production_contracts import load_project
    if project is not None:
        if not isinstance(project, dict) or not {'project', 'cast', 'characters', 'clips', 'shots'} <= set(project):
            raise ValueError('project must be the validated bundle from load_project')
        return project
    return load_project(Path(root).resolve() / 'project.json', verify_hashes=verify_hashes,
                        allow_missing_assets=not verify_hashes)


def _new_state(bundle):
    return {'schema_version': WORKFLOW_VERSION, 'project_id': bundle['project']['id'],
            'fixture': bundle['project']['fixture'], 'reviews': [], 'generation_bindings': {},
            'generation_limits': {'max_calls': bundle['project']['limits']['max_generation_calls'],
                                  'attempts_per_job': 3, 'concurrency': 1}, 'history': []}


def _state(root, bundle=None):
    bundle = _bundle(root, bundle)
    path = Path(root) / WORKFLOW_FILE
    value = hatch.read(path) if path.is_file() else _new_state(bundle)
    if value.get('schema_version') != WORKFLOW_VERSION:
        raise ValueError('unsupported production workflow schema; run production migrate explicitly')
    if value.get('project_id') != bundle['project']['id'] or value.get('fixture') is not bundle['project']['fixture']:
        raise ValueError('production workflow project identity or fixture label changed')
    if not isinstance(value.get('reviews'), list) or not isinstance(value.get('generation_bindings'), dict):
        raise ValueError('production workflow needs review history and generation bindings')
    limits = value.get('generation_limits')
    if not isinstance(limits, dict) or set(limits) != {'max_calls', 'attempts_per_job', 'concurrency'}:
        raise ValueError('production workflow must retain its generation limits')
    for key, minimum, maximum in (('max_calls', 0, 256), ('attempts_per_job', 1, 100), ('concurrency', 1, 16)):
        if type(limits[key]) is not int or not minimum <= limits[key] <= maximum:
            raise ValueError('invalid production generation limit: ' + key)
    _validate_review_chain(value['reviews'])
    return value


def _validate_review_chain(entries):
    """Shared append-only review authority; existing v2 digests stay unchanged."""
    prior = None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('previous_sha256') != prior:
            raise ValueError('production review history chain differs; preserve and inspect the original evidence')
        if not isinstance(entry.get('receipt'), dict) or not isinstance(entry['receipt'].get('dimensions'), dict):
            raise ValueError('production review history is missing its scoped receipt')
        expected = hash_object({k: v for k, v in entry.items() if k != 'sha256'})
        if entry.get('sha256') != expected:
            raise ValueError('production review history was modified; receipt hash differs')
        prior = entry['sha256']


def _event(state, name, **values):
    state.setdefault('history', []).append({'event': name, 'time': time.time(), **values})


def _save(root, state):
    hatch.atomic(Path(root) / WORKFLOW_FILE, state)


class NativeReviewJournal:
    """Director scopes in the existing production review journal and owner lock.

    The artifact owner supplies exact current dependencies for each dimension.
    This adapter checks those bytes; it does not infer creative approval or turn
    a native project into a 2D project. Reviewer identities are attestations,
    explicitly not authentication by a third-party identity service.
    """
    KIND = 'director-native-production-workflow'
    MAX_RECORDS = 4096
    MAX_ARTIFACT_BYTES = 1024**3
    MAX_INSPECTED_PIXELS = 128 * 1024**2

    def __init__(self, root, project_reference, *, artifact_root=None):
        from rig_errors import RigError
        self.root = Path(root).absolute()
        self.artifact_root = Path(artifact_root if artifact_root is not None else root).absolute()
        self.project_reference = copy.deepcopy(project_reference)
        self._bounded(self.project_reference)
        self._reference(self.project_reference)
        for path in (self.root, self.artifact_root):
            if path.is_symlink() or not path.is_dir():
                raise RigError('NATIVE_REVIEW_PATH', 'Open an existing owned journal and artifact directory.')
        info = self.root.stat()
        self._identity = (info.st_dev, info.st_ino)
        self.project_id, self.fixture = self._project_context()

    @staticmethod
    def _need(condition, code, message):
        from rig_errors import require
        require(condition, 'NATIVE_REVIEW_' + code, message, stage='review')

    @classmethod
    def _bounded(cls, value):
        from rig_errors import finite_json
        finite_json(value)
        try:
            size = len(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).encode('utf-8')) + 1
        except (ValueError, TypeError, RecursionError) as exc:
            from rig_errors import RigError
            raise RigError('NATIVE_REVIEW_INPUT', 'Use bounded finite JSON review data.') from exc
        cls._need(size <= MAX_REVIEW_BYTES, 'LIMIT', 'Review journal exceeds the 8 MiB bound.')

    @classmethod
    def _name(cls, value):
        cls._need(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value),
                  'INPUT', 'Use a bounded review identity or dimension name.')

    @staticmethod
    @lru_cache(maxsize=1)
    def _reference_validator():
        # Imports stay lazy: legacy 2D review loading acquires no new dependency.
        from director_schema import definitions
        from jsonschema import Draft202012Validator
        schema = {'$defs': definitions(), '$ref': '#/$defs/artifact-ref'}
        return Draft202012Validator(schema)

    @classmethod
    def _reference(cls, value):
        cls._need(next(cls._reference_validator().iter_errors(value), None) is None, 'REFERENCE',
                  'Review artifacts must use the exact Director ArtifactRef contract.')
        from rig_errors import portable_name
        portable_name(value['path'])

    def _read_reference(self, reference):
        from director_contracts import _read_artifact
        self._reference(reference)
        self._not_mutable_journal(reference)
        return _read_artifact(self.artifact_root, reference)

    def _not_mutable_journal(self, reference):
        target = os.path.abspath(self.artifact_root / reference['path'])
        self._need(target != os.path.abspath(self.root / WORKFLOW_FILE), 'SELF_REFERENCE',
                   'Current input signatures must exclude the mutable production review journal.')

    def _project_context(self):
        from director_contracts import validate_document, artifact_references
        from rig_errors import decode_json
        self._need(isinstance(self.project_reference, dict) and
                   self.project_reference.get('byte_size', MAX_REVIEW_BYTES + 1) <= MAX_REVIEW_BYTES,
                   'LIMIT', 'Director project context exceeds the journal bound.')
        project = validate_document(decode_json(self._read_reference(self.project_reference)))
        self._need(project['kind'] == 'director-project' and
                   self.project_reference['kind'] == project['kind'] and
                   self.project_reference['artifact_id'] == project['id'] and
                   self.project_reference['schema_version'] == project['schema_version'] and
                   self.project_reference['semantic_version'] == project['semantic_version'],
                   'IDENTITY', 'Review context must bind the exact native Director project.')
        fixture = self.project_reference['fixture'] or any(r['fixture'] for r in artifact_references(project))
        for reference in artifact_references(project):
            self._not_mutable_journal(reference)
        return project['id'], fixture

    def _check_context(self):
        info = self.root.stat()
        self._need(not self.root.is_symlink() and (info.st_dev, info.st_ino) == self._identity,
                   'PATH', 'Review journal directory changed.')
        self._need(self._project_context() == (self.project_id, self.fixture),
                   'IDENTITY', 'Director project identity or fixture classification changed.')

    def _load(self):
        import stat
        from rig_errors import decode_json
        self._check_context()
        path = self.root / WORKFLOW_FILE
        if not path.exists() and not path.is_symlink():
            return dict(schema_version=WORKFLOW_VERSION, kind=self.KIND, project_id=self.project_id,
                        fixture=self.fixture, reviews=[], validations=[], history=[])
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
            self._need(not path.is_symlink(), 'PATH', 'Review journal must not be a symbolic link.')
            with os.fdopen(os.open(path, flags), 'rb') as stream:
                self._need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'PATH', 'Review journal must be a regular file.')
                raw = stream.read(MAX_REVIEW_BYTES + 1)
            self._need(len(raw) <= MAX_REVIEW_BYTES, 'LIMIT', 'Review journal exceeds the 8 MiB bound.')
            value = decode_json(raw)
        except OSError as exc:
            from rig_errors import RigError
            raise RigError('NATIVE_REVIEW_PATH', 'Review journal is missing or unsafe.') from exc
        self._need(isinstance(value, dict) and set(value) ==
                   {'schema_version', 'kind', 'project_id', 'fixture', 'reviews', 'validations', 'history'} and
                   value['schema_version'] == WORKFLOW_VERSION and value['kind'] == self.KIND and
                   value['project_id'] == self.project_id and value['fixture'] is self.fixture,
                   'IDENTITY', 'Existing workflow belongs to another project or review authority; preserve it.')
        self._need(all(isinstance(value[k], list) for k in ('reviews', 'validations', 'history')) and
                   len(value['reviews']) + len(value['validations']) <= self.MAX_RECORDS,
                   'LIMIT', 'Review history exceeds its record bound.')
        _validate_review_chain(value['reviews'])
        prior = None
        for entry in value['validations']:
            self._need(isinstance(entry, dict) and entry.get('previous_sha256') == prior and
                       isinstance(entry.get('report'), dict) and entry.get('sha256') ==
                       hash_object({k: v for k, v in entry.items() if k != 'sha256'}),
                       'HISTORY', 'Structural validation history changed; preserve the original evidence.')
            prior = entry['sha256']
        identities = []
        for entry in [*value['reviews'], *value['validations']]:
            self._name(entry.get('transaction_id'))
            self._need(isinstance(entry.get('request_sha256'), str) and
                       re.fullmatch('[0-9a-f]{64}', entry['request_sha256']), 'HISTORY', 'Missing idempotency binding.')
            identities.append(entry['transaction_id'])
        self._need(len(identities) == len(set(identities)), 'HISTORY', 'Duplicate review transaction identities.')
        self._need(len(value['history']) == len(identities), 'HISTORY', 'Review event history is incomplete.')
        records = {e['transaction_id']: (e, 'native-review-recorded') for e in value['reviews']}
        records.update({e['transaction_id']: (e, 'native-validation-recorded') for e in value['validations']})
        observed = set()
        for event in value['history']:
            self._need(isinstance(event, dict) and set(event) == {'event', 'time', 'receipt_sha256', 'transaction_id'} and
                       event['transaction_id'] in records and event['transaction_id'] not in observed,
                       'HISTORY', 'Review event identity changed.')
            entry, name = records[event['transaction_id']]
            self._need(event['event'] == name and event['receipt_sha256'] == entry['sha256'] and
                       type(event['time']) in (int, float) and event['time'] >= 0,
                       'HISTORY', 'Review event differs from its immutable record.')
            observed.add(event['transaction_id'])
        return value

    def _references(self, value):
        from director_contracts import ARTIFACT_KEYS
        pending = [value]
        count = total = 0
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if set(item) == ARTIFACT_KEYS or {'path', 'sha256'} <= set(item):
                    self._reference(item)
                    self._not_mutable_journal(item)
                    count += 1
                    total += item['byte_size']
                    self._need(count <= 4096 and total <= self.MAX_ARTIFACT_BYTES,
                               'LIMIT', 'Review evidence exceeds the bounded byte or reference profile.')
                    yield item
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)

    def _verify_references(self, value):
        references = list(self._references(value))
        for reference in references:
            self._read_reference(reference)

    def _dependencies(self, value, *, verify, allow_empty=False):
        self._need(isinstance(value, dict) and (0 if allow_empty else 1) <= len(value) <= 128,
                   'INPUT', 'Supply exact current scoped dependencies.')
        self._bounded(value)
        for name, refs in value.items():
            self._name(name)
            self._need(isinstance(refs, list) and 0 < len(refs) <= 256, 'INPUT', 'Each review dimension needs actual dependencies.')
            for reference in refs:
                self._reference(reference)
        list(self._references(value))
        if verify:
            self._verify_references(value)
        return copy.deepcopy(value)

    def _visual_image(self, evidence, inspected):
        from io import BytesIO
        from rig_errors import RigError
        artifact = evidence['artifact']
        if not artifact['media_type'].startswith('image/') or evidence['method'] != 'visual_inspection':
            return False
        key = hash_object(artifact)
        if key in inspected['keys']:
            return True
        try:
            with Image.open(BytesIO(self._read_reference(artifact))) as image:
                inspected['pixels'] += image.width * image.height
                self._need(inspected['pixels'] <= self.MAX_INSPECTED_PIXELS, 'LIMIT', 'Inspection exceeds the per-review pixel bound; split the review into scopes.')
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise RigError('NATIVE_REVIEW_IMAGE', 'Visual review needs an actual decodable bounded image.') from exc
        inspected['keys'].add(key)
        return True

    def _receipt(self, value, current):
        value = copy.deepcopy(value)
        self._bounded(value)
        # Enforce reference/byte bounds before image decoding or any artifact I/O.
        list(self._references(value))
        self._need(isinstance(value, dict) and set(value) ==
                   {'schema_version', 'kind', 'project_id', 'fixture', 'reviewer', 'dimensions'} and
                   type(value['schema_version']) in (int, float) and value['schema_version'] == 1 and
                   value['kind'] == 'director-native-review' and value['project_id'] == self.project_id and
                   value['fixture'] is self.fixture, 'IDENTITY', 'Review must preserve native context and fixture identity.')
        reviewer = value['reviewer']
        self._need(isinstance(reviewer, dict) and set(reviewer) == {'kind', 'id', 'name'} and
                   reviewer['kind'] in ('human', 'assistant', 'synthetic'), 'REVIEWER', 'Identify the actual review authority.')
        self._name(reviewer['id'])
        self._need(isinstance(reviewer['name'], str) and 0 < len(reviewer['name']) <= 4096,
                   'REVIEWER', 'Supply the actual reviewer name.')
        dimensions = value['dimensions']
        self._need(isinstance(dimensions, dict) and 0 < len(dimensions) <= 128 and
                   set(dimensions) <= set(current), 'SCOPE', 'Review only declared current dimensions.')
        inspected = dict(keys=set(), pixels=0)
        for name, dimension in dimensions.items():
            self._need(isinstance(dimension, dict) and set(dimension) == {'verdict', 'notes', 'dependencies', 'evidence'} and
                       dimension['verdict'] in ('pass', 'fail') and isinstance(dimension['notes'], str) and
                       0 < len(dimension['notes']) <= 4096 and dimension['dependencies'] == current[name],
                       'STALE', 'Review requires explicit decisions and exact current scoped dependencies.')
            evidence = dimension['evidence']
            self._need(isinstance(evidence, list) and 0 < len(evidence) <= 256, 'EVIDENCE', 'Every decision needs inspected evidence.')
            actual_visual = False
            for item in evidence:
                self._need(isinstance(item, dict) and set(item) == {'artifact', 'method', 'frames', 'note'} and
                           item['method'] in ('visual_inspection', 'timed_playback', 'automated_check') and
                           isinstance(item['note'], str) and 0 < len(item['note']) <= 4096,
                           'EVIDENCE', 'Identify actual inspected artifacts and inspection methods.')
                self._reference(item['artifact'])
                self._need(item['artifact'] in dimension['dependencies'], 'EVIDENCE',
                           'Inspected evidence must be among the dimension\'s exact current dependencies.')
                frames = item['frames']
                self._need(frames is None or isinstance(frames, dict) and set(frames) == {'start', 'end'} and
                           all(type(frames[k]) in (int, float) and 0 <= frames[k] <= 2_000_000_000 and
                               frames[k] == int(frames[k]) for k in frames) and frames['start'] < frames['end'],
                           'EVIDENCE', 'Inspection frame intervals must be bounded and half-open.')
                actual_visual = self._visual_image(item, inspected) or actual_visual
            if dimension['verdict'] == 'pass':
                self._need(reviewer['kind'] != 'synthetic', 'SYNTHETIC', 'Synthetic checks cannot grant visual review approval.')
                self._need(actual_visual, 'EVIDENCE', 'A passing review must include an actually decoded inspected image; movies and metrics are supplemental.')
        self._verify_references(value)
        return value

    def _previous(self, state, transaction_id, request_hash):
        for entry in [*state['reviews'], *state['validations']]:
            if entry['transaction_id'] == transaction_id:
                self._need(entry['request_sha256'] == request_hash, 'IDEMPOTENCY', 'Transaction identity was used for different review content.')
                return copy.deepcopy(entry)
        return None

    def _write(self, state):
        self._need(len(state['reviews']) + len(state['validations']) <= self.MAX_RECORDS,
                   'LIMIT', 'Review history has reached its bounded record capacity.')
        self._bounded(state)
        self._check_context()
        self._need(not (self.root / WORKFLOW_FILE).is_symlink(), 'PATH', 'Review journal must not be a link.')
        _save(self.root, state)

    def record(self, receipt, transaction_id, *, current_dependencies):
        self._name(transaction_id)
        with hatch.ownership(self.root):
            state = self._load()
            current = self._dependencies(current_dependencies, verify=False)
            value = self._receipt(receipt, current)
            request_hash = hash_object(dict(kind='review', receipt=value))
            previous = self._previous(state, transaction_id, request_hash)
            if previous is not None:
                return previous
            record = dict(receipt=value, recorded_at=time.time(), transaction_id=transaction_id,
                          request_sha256=request_hash, project_reference=copy.deepcopy(self.project_reference),
                          passes=all(d['verdict'] == 'pass' for d in value['dimensions'].values()),
                          human_review=value['reviewer']['kind'] == 'human',
                          reviewer_identity_verification='self-attested', production_art_acceptance=False,
                          synthetic_gate_only=value['reviewer']['kind'] == 'synthetic',
                          previous_sha256=state['reviews'][-1]['sha256'] if state['reviews'] else None)
            record['sha256'] = hash_object(record)
            state['reviews'].append(record)
            _event(state, 'native-review-recorded', receipt_sha256=record['sha256'], transaction_id=transaction_id)
            self._write(state)
            return copy.deepcopy(record)

    def status(self, current_dependencies):
        current = self._dependencies(current_dependencies, verify=False, allow_empty=True)
        state = self._load()
        selected = {}
        for name in current:
            candidates = [e for e in state['reviews'] if name in e['receipt']['dimensions']]
            if candidates:
                selected[name] = candidates[-1]
        list(self._references(dict(current=current,
            decisions=[e['receipt']['dimensions'][name] for name, e in selected.items()])))
        result = {}
        for name, expected in current.items():
            if name not in selected:
                result[name] = dict(status='unreviewed', production_art_acceptance=False)
                continue
            entry = selected[name]
            dimension = entry['receipt']['dimensions'][name]
            stale = dimension['dependencies'] != expected
            reason = 'dependency references changed' if stale else None
            try:
                self._verify_references(dict(current=expected, decision=dimension))
            except (ValueError, OSError, TypeError) as exc:
                stale, reason = True, 'evidence missing, modified or unsafe'
            result[name] = dict(status='stale' if stale else dimension['verdict'],
                                receipt_sha256=entry['sha256'], stale_reason=reason,
                                reviewer=copy.deepcopy(entry['receipt']['reviewer']),
                                reviewer_identity_verification='self-attested', production_art_acceptance=False,
                                synthetic_gate_only=entry['synthetic_gate_only'])
        return dict(dimensions=result, history_count=len(state['reviews']),
                    current_pass=bool(result) and all(d['status'] == 'pass' for d in result.values()),
                    production_art_acceptance=False)

    def append_validation(self, report, transaction_id):
        self._name(transaction_id)
        self._bounded(report)
        self._need(isinstance(report, dict) and report.get('kind') == 'director-qa-report' and
                   type(report.get('schema_version')) in (int, float) and report['schema_version'] == 1 and report.get('project_id') == self.project_id and
                   report.get('fixture') is self.fixture and report.get('status') in
                   ('passed', 'failed', 'partial', 'unverified', 'unavailable', 'canceled', 'awaiting_review') and
                   report.get('project') == self.project_reference and
                   isinstance(report.get('inputs'), list) and bool(report['inputs']) and
                   isinstance(report.get('checks'), list), 'VALIDATION', 'Record an actual typed structural QA report.')
        self._need(report.get('production_art_acceptance', False) is False,
                   'VALIDATION', 'Structural validation cannot grant artistic acceptance.')
        self._need(all(key not in report or type(report[key]) is bool for key in ('canceled', 'deadline_exceeded')),
                   'VALIDATION', 'QA cancellation and deadline flags must be explicit booleans.')
        self._need(report['status'] != 'passed' or report['checks'] and
                   all(isinstance(c, dict) and c.get('status') == 'passed' for c in report['checks']) and
                   not report.get('canceled', False) and not report.get('deadline_exceeded', False),
                   'VALIDATION', 'A passed report requires nonempty passed checks without cancellation or deadline failure.')
        references = self._validation_references(report)
        with hatch.ownership(self.root):
            state = self._load()
            # Failed expected references are evidence of the failure, not a
            # requirement that the broken/missing input suddenly verify. The
            # exact active project/profile and every claimed passed check must
            # still verify before a historical report can be appended.
            positive = [report['project'], report['profile']]
            if report['status'] == 'passed':
                positive = references
            else:
                for check in report['checks']:
                    if check['status'] == 'passed':
                        positive.extend([check['artifact'], *check['evidence']])
            # A project's/profile's dependency declarations can deliberately
            # include the very missing input being diagnosed. Reverify the
            # positive roles' primary files, rather than promoting their whole
            # dependency declarations to successful verification on failure.
            for reference in {hash_object(r): r for r in positive}.values():
                self._read_reference(reference)
            request_hash = hash_object(dict(kind='validation', report=report))
            previous = self._previous(state, transaction_id, request_hash)
            if previous is not None:
                return previous
            record = dict(report=copy.deepcopy(report), recorded_at=time.time(), transaction_id=transaction_id,
                          request_sha256=request_hash, project_reference=copy.deepcopy(self.project_reference),
                          all_required_inputs_verified=report['status'] == 'passed',
                          production_art_acceptance=False,
                          previous_sha256=state['validations'][-1]['sha256'] if state['validations'] else None)
            record['sha256'] = hash_object(record)
            state['validations'].append(record)
            _event(state, 'native-validation-recorded', receipt_sha256=record['sha256'], transaction_id=transaction_id)
            self._write(state)
            return copy.deepcopy(record)

    def _validation_references(self, report):
        """Validate typed positions without interpreting native report details.

        Native owners may retain their own path/hash descriptor formats inside
        details. Only Director ArtifactRefs and declared typed positions carry
        Director read authority. Failed references are never read here.
        """
        from director_contracts import artifact_references
        refs = [report['project'], report.get('profile'), *report['inputs']]
        self._reference(report.get('profile'))
        self._need(report['profile']['kind'] == 'director-qa-profile', 'VALIDATION',
                   'Structural report must bind its actual QA profile.')
        check_ids = []
        for check in report['checks']:
            self._need(isinstance(check, dict) and check.get('status') in
                       ('passed', 'failed', 'blocked', 'unavailable', 'unverified', 'canceled', 'pending') and
                       isinstance(check.get('evidence'), list) and len(check['evidence']) <= 256,
                       'VALIDATION', 'Each structural check needs a typed status, target and bounded evidence.')
            self._name(check.get('id'))
            check_ids.append(check['id'])
            self._need(check['status'] != 'passed' or bool(check['evidence']), 'VALIDATION',
                       'A claimed passing check needs actual evidence references.')
            refs.extend([check.get('artifact'), *check['evidence']])
        self._need(len(check_ids) == len(set(check_ids)), 'VALIDATION', 'Structural check identities must be unique.')
        findings = report.get('findings', [])
        self._need(isinstance(findings, list) and len(findings) <= 256, 'VALIDATION',
                   'Structural findings must be a bounded list.')
        for finding in findings:
            self._need(isinstance(finding, dict) and 'artifact' in finding, 'VALIDATION',
                       'Every structural finding must bind its expected target artifact.')
            refs.append(finding['artifact'])
        refs.extend(artifact_references(report))
        unique = {}
        for reference in refs:
            self._reference(reference)
            self._not_mutable_journal(reference)
            unique[hash_object(reference)] = reference
        references = list(unique.values())
        # Include transitive reference byte budgets before any positive reads.
        list(self._references(references))
        return references

    def validation_history(self):
        """Historical reports only; callers must revalidate before current use."""
        state = self._load()
        return dict(records=copy.deepcopy(state['validations']), history_count=len(state['validations']),
                    current_execution_verified=False, production_art_acceptance=False)

    def history(self):
        return copy.deepcopy(self._load())


def _object_without(value, *keys):
    return {key: copy.deepcopy(item) for key, item in value.items() if key not in keys}


def _references(value):
    if isinstance(value, dict):
        if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
            yield value
        if isinstance(value.get('receipt_path'), str) and isinstance(value.get('receipt_sha256'), str):
            yield {'path': value['receipt_path'], 'sha256': value['receipt_sha256']}
        for key, item in value.items():
            if key not in ('view_reviews', 'review', 'evidence'):
                yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def _add_files(result, root, value, prefix='file'):
    for ref in _references(value):
        path = safe_path(root, ref['path'])
        result[f'{prefix}:{ref["path"]}'] = file_hash(path) if path.is_file() else 'missing'


def _character(bundle, character_id, version):
    key = f'{character_id}@{version}'
    if key not in bundle['characters']:
        raise ValueError('missing character version: ' + key)
    return bundle['characters'][key]


def _shot(bundle, scene_id, shot_id):
    shot = bundle['shots'].get(shot_id)
    if shot is None:
        raise ValueError('missing shot: ' + str(shot_id))
    scene = next((scene for scene in bundle['scenes'] if scene['id'] == scene_id), None)
    if scene is None or shot_id not in [entry['id'] if isinstance(entry, dict) else entry for entry in scene['shots']]:
        raise ValueError(f'shot {shot_id} does not belong to scene {scene_id}')
    return scene, shot


def _renderer_identity():
    from production_render import renderer_identity
    return renderer_identity()


def _character_dependencies(root, character, component_id=None, selected_drawings=None, costume=None):
    key = f'{character["id"]}@{character["version"]}'
    identity = {name: character[name] for name in ('id', 'version', 'dimensions', 'relative_scale', 'identity', 'provenance')}
    result = {'character:' + key: hash_object(identity)}
    _add_files(result, root, character['provenance'], 'provenance_file')
    _add_files(result, root, character.get('references', []), 'reference')
    components = character['components']
    if component_id is not None:
        components = [item for item in components if item['id'] == component_id]
        if not components:
            raise ValueError(f'missing component {component_id} in {key}')
        by_id = {item['id']: item for item in character['components']}
        parent = components[0]['parent']
        while parent is not None:
            ancestor = by_id[parent]
            result[f'attachment:{key}:{parent}'] = hash_object(_object_without(ancestor, 'drawings', 'mask'))
            parent = ancestor['parent']
    for component in components:
        result[f'component:{key}:{component["id"]}'] = hash_object(_object_without(component, 'drawings'))
        chosen = (selected_drawings or {}).get(component['id']) if selected_drawings is not None else None
        drawings = component['drawings'] if chosen is None else {drawing_id: component['drawings'][drawing_id] for drawing_id in sorted(chosen)}
        for drawing_id, drawing in drawings.items():
            result[f'drawing:{key}:{component["id"]}:{drawing_id}'] = hash_object(drawing)
            _add_files(result, root, drawing)
        _add_files(result, root, component.get('mask'))
    for item in [value for value in character['costumes'] if costume is None or value['id'] == costume]:
        result[f'costume:{key}:{item["id"]}'] = hash_object(item)
    return result


def _selected_drawings(character, instance, clips):
    from production_performance import selected_drawing
    chosen = {component['id']: {selected_drawing(component, instance['viewpoint'], instance['costume'], character)}
              for component in character['components']}
    for source in [instance, *clips]:
        for channel in ('body', 'face', 'gaze', 'mouth'):
            for component_id, properties in source.get('tracks', {}).get(channel, {}).items():
                chosen[component_id].update(key['value'] for key in properties.get('drawing', {}).get('keys', []))
    return chosen


def _continuity_projection(bundle, shot, link, at_end=False):
    from production_performance import evaluate_instance_performance, prop_owner
    requirements = link['requirements']
    projection = {'instances': []}
    frame = shot['duration_frames'] if at_end else 0
    for instance in shot['instances']:
        if instance['character'] not in link['characters']:
            continue
        item = {'character': instance['character']}
        if 'costume' in requirements:
            item['costume'] = instance['costume']
        if 'scale' in requirements:
            item['cast_scale'] = shot['cast_scale']
            item['relative_scale'] = bundle['characters'][instance['character']]['relative_scale']
        if 'position' in requirements:
            item['position'] = evaluate_instance_performance(bundle, shot, instance, frame)['position']
        projection['instances'].append(item)
    if 'prop_ownership' in requirements:
        projection['props'] = [{'id': prop['id'], 'prop': prop['prop'], **prop_owner(prop, frame)} for prop in shot['props']]
    return projection


def shot_dependencies(root, scene_id, shot_id, project=None):
    """Bind only used shot inputs; additions elsewhere do not invalidate a shot."""
    root = Path(root).resolve()
    bundle = _bundle(root, project)
    scene, shot = _shot(bundle, scene_id, shot_id)
    result = {
        'project_identity': hash_object({'id': bundle['project']['id'], 'fixture': bundle['project']['fixture']}),
        'render_settings': hash_object(bundle['project']['render']),
        'renderer': hash_object(_renderer_identity()),
        'scene_identity': hash_object({'id': scene['id']}),
        'shot_settings': hash_object(_object_without(shot, 'characters', 'instances', 'camera', 'audio', 'dialogue', 'interactions', 'continuity')),
        'camera': hash_object(shot.get('camera', {})),
        'audio_track': hash_object(shot.get('audio', [])),
        'dialogue': hash_object(shot.get('dialogue', [])),
        'interactions': hash_object(shot.get('interactions', [])),
    }
    _add_files(result, root, _object_without(shot, 'characters', 'instances', 'audio'), 'shot_file')
    for instance in shot.get('characters', shot.get('instances', [])):
        character_id, version = instance['character'].rsplit('@', 1)
        character = _character(bundle, character_id, version)
        clips = [bundle['clips'][segment['clip']] for segment in instance['clips']]
        result.update(_character_dependencies(root, character, selected_drawings=_selected_drawings(character, instance, clips), costume=instance['costume']))
        result['instance:' + instance['id']] = hash_object(instance)
        for segment in instance['clips']:
            result['clip:' + segment['clip']] = hash_object(bundle['clips'][segment['clip']])
            _add_files(result, root, bundle['clips'][segment['clip']], 'clip_file')
    for prop_instance in shot['props']:
        prop = bundle['props'][prop_instance['prop']]
        result['prop:' + prop_instance['prop']] = hash_object(prop)
        _add_files(result, root, prop, 'prop_file')
    for audio in shot['audio']:
        _add_files(result, root, audio, 'audio_file')
    for linked_scene in bundle['scenes']:
        for link in linked_scene['continuity']:
            if shot_id in (link['from_shot'], link['to_shot']):
                result[f'continuity:{link["from_shot"]}:{link["to_shot"]}'] = hash_object({'link': link,
                    'from': _continuity_projection(bundle, bundle['shots'][link['from_shot']], link, at_end=True),
                    'to': _continuity_projection(bundle, bundle['shots'][link['to_shot']], link)})
    return dict(sorted(result.items()))


def _target_key(target):
    if not isinstance(target, dict) or target.get('kind') not in ('character', 'component', 'clip', 'shot'):
        raise ValueError('review target kind must be character, component, clip or shot')
    fields = {'character': ('character_id', 'version'), 'component': ('character_id', 'version', 'component_id'),
              'clip': ('clip_id', 'version'), 'shot': ('scene_id', 'shot_id')}[target['kind']]
    strict_keys(target, {'kind', *fields}, 'review target')
    if any(key not in target for key in fields):
        raise ValueError('review target is missing ' + ', '.join(fields))
    return hash_object(target)


def _target_dimensions(target):
    return {'character': ('identity_construction',), 'component': ('identity_construction',),
            'clip': ('pose_motion', 'contacts_interactions', 'timing_av_sync'), 'shot': REVIEW_DIMENSIONS}[target['kind']]


def _target_dependencies(root, bundle, target):
    _target_key(target)
    if target['kind'] == 'shot':
        return shot_dependencies(root, target['scene_id'], target['shot_id'], project=bundle)
    if target['kind'] in ('character', 'component'):
        character = _character(bundle, target['character_id'], target['version'])
        result = _character_dependencies(root, character, target.get('component_id'))
        if target['kind'] == 'character':
            result[f'coverage:{target["character_id"]}@{target["version"]}'] = hash_object({
                'views': character['views'], 'capabilities': character['capabilities'],
                'restrictions': character.get('restrictions', [])})
        return result
    key = f'{target["clip_id"]}@{target["version"]}'
    if key not in bundle['clips']:
        raise ValueError('missing clip version: ' + key)
    clip = bundle['clips'][key]
    result = {'clip:' + key: hash_object(clip)}
    _add_files(result, root, clip)
    character_id, version = clip['character'].rsplit('@', 1)
    result.update(_character_dependencies(root, _character(bundle, character_id, version)))
    return result


def _dimension_dependencies(dependencies, dimension):
    excluded = {
        'identity_construction': ('camera', 'audio_track', 'audio_file:', 'shot_file:', 'dialogue', 'interactions', 'continuity:', 'renderer', 'render_settings', 'scene_identity', 'shot_settings', 'instance:', 'clip:', 'prop:', 'prop_file:'),
        'pose_motion': ('camera', 'audio_track', 'audio_file:', 'dialogue', 'continuity:'),
        'facial_dialogue': ('camera', 'interactions', 'continuity:'),
        'contacts_interactions': ('camera', 'audio_track', 'dialogue'),
        'camera_composition': ('audio_track', 'dialogue'),
        'continuity': (), 'timing_av_sync': ('camera',), 'final_artifacts': (),
    }[dimension]
    return {key: value for key, value in dependencies.items() if not any(key == prefix or (prefix.endswith(':') and key.startswith(prefix)) for prefix in excluded)}


def _render_artifacts(root, relative, target, dependencies=None, bundle=None):
    path = safe_path(root, relative)
    manifest = hatch.read(path)
    if path.name != 'render-manifest.json' or manifest.get('schema_version') != 1 or manifest.get('kind') != 'shot-render':
        raise ValueError('final shot review requires the persisted shot render-manifest.json')
    if manifest.get('status') != 'complete' or manifest.get('shot_id') != target.get('shot_id'):
        raise ValueError('final review requires a complete render of the exact target shot')
    if manifest.get('scene_id') != target.get('scene_id'):
        raise ValueError('render scene differs from the review target')
    if manifest.get('renderer') != _renderer_identity():
        raise ValueError('renderer version changed; rerender before reviewing final artifacts')
    if dependencies is not None and manifest.get('dependencies') != dependencies:
        raise ValueError('render dependencies are stale; rerender affected frames/audio before final review')
    bundle = _bundle(root, bundle, verify_hashes=False)
    _, shot = _shot(bundle, target['scene_id'], target['shot_id'])
    if manifest.get('project_id') != bundle['project']['id'] or manifest.get('fixture') is not bundle['project']['fixture']:
        raise ValueError('render project identity or fixture label differs from the review target')
    settings = {**bundle['project']['render'], **shot.get('render', {})}
    if (manifest.get('settings') != settings or manifest.get('fps') != settings['fps'] or
            manifest.get('resolution') != [settings['width'], settings['height']] or
            manifest.get('frame_count') != shot['duration_frames']):
        raise ValueError('render timing, resolution or settings differ from the exact current shot')
    result = {'render_manifest:' + relative: file_hash(path)}
    frames = manifest.get('frames')
    if not isinstance(frames, list) or not frames or len(frames) != manifest.get('frame_count'):
        raise ValueError('complete render must contain its exact declared frame inventory')
    if [item.get('index') for item in frames] != list(range(len(frames))):
        raise ValueError('render frame inventory is incomplete or out of order')
    outputs = []
    for frame in frames:
        outputs.append(frame)
        outputs.extend(frame.get('layers', []))
    if isinstance(manifest.get('audio'), dict) and manifest['audio'].get('path'):
        outputs.append(manifest['audio'])
    for item in outputs:
        source = safe_path(path.parent, item['path'])
        if not source.is_file() or file_hash(source) != item['sha256']:
            raise ValueError('render artifact is missing or differs from its manifest: ' + item['path'])
        rel = source.relative_to(Path(root).resolve()).as_posix()
        result['render_artifact:' + rel] = item['sha256']
    from production_render import validate_render
    validate_render(path.parent)
    return result


def _has_final_visual_evidence(root, relative, evidence, reviewer_kind):
    """A WAV or a manifest cannot establish final visual inspection by itself."""
    manifest_path = safe_path(root, relative)
    manifest = hatch.read(manifest_path)
    visual_paths = set()
    for frame in manifest['frames']:
        for item in [frame, *frame.get('layers', [])]:
            source = safe_path(manifest_path.parent, item['path'])
            visual_paths.add(source.relative_to(Path(root).resolve()).as_posix())
    methods = {'visual_inspection', 'timed_playback'}
    if reviewer_kind == 'synthetic':
        methods.add('automated_check')
    return any(item['path'] in visual_paths and item['method'] in methods for item in evidence)


def review_template(root, target, dimensions=None, render_manifest=None, project=None):
    bundle = _bundle(root, project)
    _target_key(target)
    chosen = list(dimensions or _target_dimensions(target))
    if len(chosen) != len(set(chosen)) or not set(chosen) <= set(_target_dimensions(target)):
        raise ValueError('review dimensions are invalid for this target')
    dependencies = _target_dependencies(root, bundle, target)
    artifacts = _render_artifacts(root, render_manifest, target, dependencies, bundle) if render_manifest else {}
    return {'schema_version': 2, 'project_id': bundle['project']['id'], 'fixture': bundle['project']['fixture'],
            'target': copy.deepcopy(target), 'reviewer': {'kind': '', 'id': '', 'name': ''},
            'render_manifest': render_manifest,
            'dimensions': {dimension: {'verdict': 'unreviewed', 'notes': '',
                'dependencies': {**_dimension_dependencies(dependencies, dimension),
                                 **(artifacts if dimension in ('final_artifacts', 'timing_av_sync') else {})},
                'evidence': []} for dimension in chosen}}


def record_review(root, receipt):
    root = Path(root).resolve()
    value = hatch.read(receipt) if isinstance(receipt, (str, Path)) else copy.deepcopy(receipt)
    if len(json.dumps(value).encode()) > MAX_REVIEW_BYTES:
        raise ValueError('review exceeds the 8 MiB evidence bound; link inspected artifacts')
    strict_keys(value, {'schema_version', 'project_id', 'fixture', 'target', 'reviewer', 'render_manifest', 'dimensions'}, 'production review')
    if value.get('schema_version') != 2 or not isinstance(value.get('dimensions'), dict) or not value['dimensions']:
        raise ValueError('review schema 2 and at least one dimension are required')
    reviewer = value.get('reviewer', {})
    strict_keys(reviewer, {'kind', 'id', 'name'}, 'reviewer')
    if reviewer.get('kind') not in ('human', 'assistant', 'synthetic'):
        raise ValueError('reviewer.kind must explicitly be human, assistant or synthetic')
    text(reviewer.get('id'), 'actual reviewer identity')
    text(reviewer.get('name'), 'actual reviewer name')
    with hatch.ownership(root):
        bundle = _bundle(root)
        expected = review_template(root, value.get('target'), list(value['dimensions']), value.get('render_manifest'), bundle)
        if value.get('project_id') != expected['project_id'] or value.get('fixture') is not expected['fixture']:
            raise ValueError('review must preserve project identity and fixture label')
        for name, dimension in value['dimensions'].items():
            strict_keys(dimension, {'verdict', 'notes', 'dependencies', 'evidence'}, 'review dimension')
            if dimension.get('verdict') not in ('pass', 'fail'):
                raise ValueError('every reviewed dimension needs an explicit pass or fail verdict')
            text(dimension.get('notes'), name + ' inspection notes')
            if dimension.get('dependencies') != expected['dimensions'][name]['dependencies']:
                raise ValueError('stale review dependencies for ' + name + '; rebuild the review template and inspect changed artifacts')
            evidence = dimension.get('evidence')
            if not isinstance(evidence, list) or not evidence:
                raise ValueError('every review dimension requires inspectable evidence')
            for item in evidence:
                strict_keys(item, {'path', 'sha256', 'note', 'frames', 'method'}, 'review evidence')
                text(item.get('note'), 'evidence note')
                if item.get('method') not in ('visual_inspection', 'audio_inspection', 'timed_playback', 'automated_check'):
                    raise ValueError('evidence must identify visual, audio, timed playback or automated inspection')
                source = safe_path(root, item.get('path'))
                if not source.is_file() or file_hash(source) != item.get('sha256'):
                    raise ValueError('review evidence does not bind the current inspected file')
                if 'frames' in item and (not isinstance(item['frames'], list) or any(type(i) is not int or i < 0 for i in item['frames'])):
                    raise ValueError('evidence frame indices must be nonnegative integers')
                if value['target']['kind'] == 'shot' and any(i >= bundle['shots'][value['target']['shot_id']]['duration_frames'] for i in item.get('frames', [])):
                    raise ValueError('review evidence frame index exceeds the target shot duration')
            if reviewer['kind'] != 'synthetic' and all(item['method'] == 'automated_check' for item in evidence):
                raise ValueError('automated metrics alone cannot establish visual or production-art acceptance')
            if name == 'final_artifacts' and not value.get('render_manifest'):
                raise ValueError('final artifact review needs the completed exact render manifest')
            if name == 'final_artifacts':
                if not _has_final_visual_evidence(root, value['render_manifest'], evidence, reviewer['kind']):
                    raise ValueError('final artwork review must inspect a bound rendered visual frame or layer; audio and movies are supplemental evidence')
        state = _state(root, bundle)
        passed = all(item['verdict'] == 'pass' for item in value['dimensions'].values())
        record = {'receipt': value, 'recorded_at': time.time(), 'passes': passed,
                  'human_review': reviewer['kind'] == 'human',
                  'production_art_acceptance': passed and not value['fixture'] and reviewer['kind'] in ('human', 'assistant'),
                  'synthetic_gate_only': reviewer['kind'] == 'synthetic',
                  'previous_sha256': state['reviews'][-1]['sha256'] if state['reviews'] else None}
        record['sha256'] = hash_object(record)
        state['reviews'].append(record)
        _event(state, 'review-recorded', target=value['target'], receipt_sha256=record['sha256'], reviewer=reviewer)
        _save(root, state)
        return {'recorded': True, **{key: record[key] for key in ('sha256', 'passes', 'human_review', 'production_art_acceptance', 'synthetic_gate_only')}}


def review_status(root, target, project=None):
    bundle = _bundle(root, project, verify_hashes=False)
    state = _state(root, bundle)
    key = _target_key(target)
    current = _target_dependencies(root, bundle, target)
    result = {}
    for dimension in _target_dimensions(target):
        candidates = [record for record in state['reviews'] if _target_key(record['receipt']['target']) == key and dimension in record['receipt']['dimensions']]
        if not candidates:
            result[dimension] = {'status': 'unreviewed', 'production_art_acceptance': False}
            continue
        record = candidates[-1]
        receipt = record['receipt']
        expected = _dimension_dependencies(current, dimension)
        error = None
        if dimension in ('final_artifacts', 'timing_av_sync') and receipt.get('render_manifest'):
            try:
                expected.update(_render_artifacts(root, receipt['render_manifest'], target, current, bundle))
                if dimension == 'final_artifacts' and not _has_final_visual_evidence(
                        root, receipt['render_manifest'], receipt['dimensions'][dimension]['evidence'], receipt['reviewer']['kind']):
                    error = 'Final artwork review has no inspected bound visual frame or layer; repeat visual review.'
            except (ValueError, OSError, KeyError) as exc:
                error = str(exc)
        prior = receipt['dimensions'][dimension]['dependencies']
        changed = sorted(k for k in set(expected) | set(prior) if expected.get(k) != prior.get(k))
        for evidence in receipt['dimensions'][dimension]['evidence']:
            source = safe_path(root, evidence['path'])
            if not source.is_file() or file_hash(source) != evidence['sha256']:
                changed.append('evidence:' + evidence['path'])
        stale = bool(changed or error)
        verdict = receipt['dimensions'][dimension]['verdict']
        production_review = (not bundle['project']['fixture'] and receipt.get('fixture') is False
                             and receipt['reviewer']['kind'] in ('human', 'assistant'))
        result[dimension] = {'status': 'stale' if stale else verdict,
            'reviewer': receipt['reviewer'], 'receipt_sha256': record['sha256'],
            'changed_dependencies': changed, 'stale_reason': error,
            'human_review': record['human_review'],
            'production_art_acceptance': not stale and verdict == 'pass' and production_review,
            'synthetic_gate_only': record['synthetic_gate_only']}
    return {'target': copy.deepcopy(target), 'dimensions': result,
            'current_pass': all(item['status'] == 'pass' for item in result.values()),
            'production_art_acceptance': all(item['production_art_acceptance'] for item in result.values()),
            'history_count': sum(_target_key(record['receipt']['target']) == key for record in state['reviews'])}


def _binding_context(binding_id, binding):
    return {'binding_id': binding_id, 'run_id': binding['run_id'],
            'requirements_hash': binding['requirements_hash'],
            'requirements': copy.deepcopy(binding['requirements'])}


def bind_generation_run(root, binding_id, run_dir, requirements):
    """Bind a portable existing hatch run; do not create a second provider path."""
    root = Path(root).resolve()
    if not isinstance(binding_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', binding_id):
        raise ValueError('binding id must contain only letters, digits, hyphens or underscores')
    if (not isinstance(requirements, list) or not requirements or
            any(not isinstance(item, (str, dict)) for item in requirements) or
            len(json.dumps(requirements).encode()) > 256 * 1024):
        raise ValueError('binding needs a bounded nonempty list of exact coverage requirements')
    candidate = Path(run_dir)
    if candidate.is_absolute():
        if not candidate.resolve().is_relative_to(root):
            raise ValueError('bound acquisition must be inside the project for portable restoration')
        relative = candidate.resolve().relative_to(root).as_posix()
    else:
        relative = candidate.as_posix()
    run = safe_path(root, relative)
    if run == root:
        raise ValueError('production and acquisition run roots must be separate directories')
    with hatch.ownership(root):
        bundle = _bundle(root)
        state = _state(root, bundle)
        if binding_id in state['generation_bindings'] or any(
                item['run_path'] == relative for item in state['generation_bindings'].values()):
            raise ValueError('binding id or acquisition run is already bound; preserve its history')
        with hatch.ownership(run):
            data = hatch.manifest(run)
            if data['fixture_mode'] is not bundle['project']['fixture']:
                raise ValueError('acquisition fixture label must match the production project')
            if any(job['status'] == 'running' for job in data['jobs']):
                raise ValueError('reconcile existing active tokens before initially binding an acquisition')
            binding = {'run_path': relative, 'run_id': data['run_id'],
                       'request_sha256': file_hash(run / 'pet_request.json'),
                       'requirements': copy.deepcopy(requirements),
                       'requirements_hash': hash_object(requirements)}
            state['generation_bindings'][binding_id] = binding
            _event(state, 'generation-bound', binding_id=binding_id, **binding)
            _save(root, state)
    return {'bound': binding_id, **binding, 'provider_called': False}


def _bound_runs(root, state):
    result = []
    for binding_id, binding in sorted(state['generation_bindings'].items()):
        strict_keys(binding, {'run_path', 'run_id', 'request_sha256', 'requirements',
                              'requirements_hash'}, 'production generation binding')
        if not isinstance(binding.get('requirements'), list) or not binding['requirements']:
            raise ValueError('bound coverage requirements are missing')
        if hash_object(binding['requirements']) != binding.get('requirements_hash'):
            raise ValueError('bound coverage requirements changed; preserve the original binding')
        run = safe_path(root, binding['run_path'])
        if run == Path(root).resolve():
            raise ValueError('bound acquisition cannot share the production root')
        data = hatch.manifest(run)
        if data['run_id'] != binding['run_id'] or data['fixture_mode'] is not state['fixture']:
            raise ValueError('bound acquisition identity or fixture label changed')
        if file_hash(run / 'pet_request.json') != binding['request_sha256']:
            raise ValueError('bound original request changed; create a separately identified acquisition')
        result.append((binding_id, binding, run, data))
    if len({str(item[2]) for item in result}) != len(result):
        raise ValueError('one acquisition cannot consume multiple production bindings')
    return result


def _generation_report(bundle, state, runs):
    used, active, bindings = 0, [], {}
    for binding_id, binding, run, data in runs:
        jobs = []
        for job in data['jobs']:
            count = sum(entry.get('event') == 'claim' for entry in job['history'])
            used += count
            report = {key: copy.deepcopy(job[key]) for key in
                      ('id', 'status', 'attempts', 'token', 'stale', 'output_hash', 'model_call',
                       'source_origin', 'history') if key in job}
            report['recorded_claims'] = count
            jobs.append(report)
            if job['status'] == 'running':
                active.append({'binding_id': binding_id, 'job': job['id'],
                               'token': job['token'], 'stale': copy.deepcopy(job.get('stale'))})
        bindings[binding_id] = {**copy.deepcopy(binding), 'jobs': jobs}
    limits = copy.deepcopy(state['generation_limits'])
    limits['max_calls'] = min(limits['max_calls'], bundle['project']['limits']['max_generation_calls'])
    return {'schema_version': 1, 'project_id': bundle['project']['id'],
            'fixture': bundle['project']['fixture'], 'limits': limits,
            'recorded_calls': used, 'calls_used': used,
            'remaining_calls': max(0, limits['max_calls'] - used),
            'active_claims': active, 'running': len(active), 'bindings': bindings,
            'provider_called': False,
            'accounting': 'Every recorded hatch claim counts, including failed or uncertain attempts; repair does not erase call history.'}


def generation_status(root, project=None, reconcile=True):
    root = Path(root).resolve()
    bundle = _bundle(root, project, verify_hashes=False)
    state = _state(root, bundle)
    runs = _bound_runs(root, state)
    if reconcile:
        current = []
        for binding_id, binding, run, _ in runs:
            with hatch.ownership(run):
                data = hatch.manifest(run)
                hatch.reconcile(run, data)
                current.append((binding_id, binding, run, data))
        runs = current
    return _generation_report(bundle, state, runs)


def configure_generation_limits(root, *, max_calls=None, attempts_per_job=None, concurrency=None):
    root = Path(root).resolve()
    with hatch.ownership(root):
        bundle = _bundle(root)
        state = _state(root, bundle)
        proposed = {**state['generation_limits'], **{key: value for key, value in
            (('max_calls', max_calls), ('attempts_per_job', attempts_per_job), ('concurrency', concurrency))
            if value is not None}}
        for key, low, high in (('max_calls', 0, bundle['project']['limits']['max_generation_calls']),
                               ('attempts_per_job', 1, 100), ('concurrency', 1, 16)):
            if type(proposed[key]) is not int or not low <= proposed[key] <= high:
                raise ValueError('invalid or project-exceeding generation limit: ' + key)
        state['generation_limits'] = proposed
        _event(state, 'generation-limits-configured', limits=proposed)
        _save(root, state)
    return generation_status(root)


def claim_generation(root, binding_id, job):
    root = Path(root).resolve()
    with hatch.ownership(root):
        bundle = _bundle(root)
        state = _state(root, bundle)
        runs = _bound_runs(root, state)
        if binding_id not in state['generation_bindings']:
            raise ValueError('unknown production acquisition binding: ' + str(binding_id))
        with ExitStack() as locks:
            for _, _, run, _ in runs:
                locks.enter_context(hatch.ownership(run))
            current = []
            for name, binding, run, _ in runs:
                data = hatch.manifest(run)
                hatch.reconcile(run, data)
                current.append((name, binding, run, data))
            report = _generation_report(bundle, state, current)
            if report['remaining_calls'] <= 0:
                raise ValueError('production generation call limit exhausted; failed and uncertain claims remain counted')
            if report['running'] >= report['limits']['concurrency']:
                raise ValueError('production generation concurrency bound reached; reconcile an existing token')
            _, binding, run, data = next(item for item in current if item[0] == binding_id)
            selected = hatch.job_for(data, job)
            if selected['status'] in ('running', 'generated'):
                raise ValueError('generation is already running or generated; reconcile or explicitly repair first')
            if selected.get('attempts', 0) >= report['limits']['attempts_per_job']:
                raise ValueError('production retry bound reached; inspect failure history and explicitly repair')
            context = _binding_context(binding_id, binding)
            marker = '\nProduction coverage binding SHA-256: ' + binding['requirements_hash'] + '\n'
            instruction = marker + json.dumps(context, sort_keys=True, indent=2) + (
                '\nPreserve the original identity and every attached reference. '
                'Generate only the requested reusable asset; keep unrelated performance dimensions independent.\n')
            for field in ('prompt_file', 'retry_prompt_file'):
                if field in selected:
                    prompt = hatch.inside(run, selected[field])
                    current_text = prompt.read_text(encoding='utf-8')
                    if marker not in current_text:
                        prompt.write_text(current_text + instruction, encoding='utf-8')
            data.setdefault('limits', {})['attempts_per_job'] = min(
                data.get('limits', {}).get('attempts_per_job', report['limits']['attempts_per_job']),
                report['limits']['attempts_per_job'])
            data['limits']['concurrency'] = min(
                data['limits'].get('concurrency', report['limits']['concurrency']),
                report['limits']['concurrency'])
            handoff = hatch.claim(run, data, job)
            handoff['production_context'] = context
            handoff['receipt_template']['production_context'] = context
            _event(state, 'generation-claimed', binding_id=binding_id, job=job, token=handoff['token'],
                   requirements_hash=binding['requirements_hash'])
            _save(root, state)
            return handoff


def _generation_action(root, binding_id, job, action, **arguments):
    root = Path(root).resolve()
    with hatch.ownership(root):
        bundle = _bundle(root)
        state = _state(root, bundle)
        runs = _bound_runs(root, state)
        selected = next((item for item in runs if item[0] == binding_id), None)
        if selected is None:
            raise ValueError('unknown production acquisition binding: ' + str(binding_id))
        _, binding, run, _ = selected
        with hatch.ownership(run):
            data = hatch.manifest(run)
            if action == 'ingest':
                receipt = arguments.pop('receipt')
                value = hatch.read(receipt) if isinstance(receipt, (str, Path)) else copy.deepcopy(receipt)
                value = value.get('receipt_template', value)
                if value.get('production_context') != _binding_context(binding_id, binding):
                    raise ValueError('ingest receipt must attest the exact bound production coverage context')
                with tempfile.TemporaryDirectory(prefix='hatch-production-receipt-') as temporary:
                    receipt_path = Path(temporary) / 'receipt.json'
                    hatch.atomic(receipt_path, value)
                    result = hatch.ingest(run, data, job, used_inputs=receipt_path,
                                          fixture=bundle['project']['fixture'], **arguments)
            elif action == 'fail':
                result = hatch.fail(run, data, job, **arguments)
            elif action == 'repair':
                result = hatch.repair(run, data, job, **arguments)
            else:
                raise ValueError('unsupported production generation action')
            _event(state, 'generation-' + action, binding_id=binding_id, job=job,
                   requirements_hash=binding['requirements_hash'])
            _save(root, state)
    return {**result, 'binding_id': binding_id, 'provider_called': False}


def ingest_generation(root, binding_id, job, source, token, note, receipt):
    return _generation_action(root, binding_id, job, 'ingest', source=source, token=token,
                              note=note, receipt=receipt)


def fail_generation(root, binding_id, job, token, reason):
    return _generation_action(root, binding_id, job, 'fail', token=token, reason=reason)


def repair_generation(root, binding_id, job, reason):
    return _generation_action(root, binding_id, job, 'repair', reason=reason)


def _project_files(root, runtime_locks=()):
    result, total = {}, 0
    for path in sorted(Path(root).rglob('*')):
        relative = path.relative_to(root).as_posix()
        safe_path(root, relative)
        if path.is_symlink():
            raise ValueError('portable projects cannot archive symlinks: ' + relative)
        # These exact sibling files belong to acquisition locks held by this
        # archive operation. Windows denies reads of their mandatory lock byte.
        # They are runtime coordination, not acquisition history or project data.
        if relative in runtime_locks:
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        if size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError('project archive member exceeds the 256 MiB bound: ' + relative)
        if total > MAX_ARCHIVE_BYTES or len(result) >= MAX_ARCHIVE_FILES - 1:
            raise ValueError('project archive exceeds its 1 GiB or 9,999-file bound')
        result[relative] = file_hash(path)
    return result


def verify_project_archive(path):
    """Keep the shared portable archive verifier as the extraction authority."""
    try:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                limit = MAX_REVIEW_BYTES if entry.filename == 'archive-receipt.json' else MAX_ARCHIVE_MEMBER_BYTES
                if entry.file_size > limit:
                    raise ValueError('production archive member exceeds its bounded size: ' + entry.filename)
        receipt = verify_archive(path)
    except (zipfile.BadZipFile, KeyError, TypeError, UnicodeError, AttributeError) as exc:
        raise ValueError('invalid production archive: ' + str(exc)) from exc
    if (receipt.get('kind') != 'hatch-production-project' or
            type(receipt.get('project_schema')) is not int or receipt['project_schema'] != 1 or
            type(receipt.get('workflow_schema')) is not int or receipt['workflow_schema'] != WORKFLOW_VERSION or
            not isinstance(receipt.get('project_id'), str) or not receipt['project_id'] or
            type(receipt.get('fixture')) is not bool or 'project.json' not in receipt['files']):
        raise ValueError('archive is not a supported identified production project; migrate explicitly if required')
    return receipt


def _active_acquisitions(root):
    active = []
    for manifest_path in sorted(Path(root).rglob('imagegen-jobs.json')):
        safe_path(root, manifest_path.relative_to(root).as_posix())
        data = hatch.manifest(manifest_path.parent)
        for job in data['jobs']:
            if job['status'] == 'running':
                active.append({'run_path': manifest_path.parent.relative_to(root).as_posix(),
                               'run_id': data['run_id'], 'job': job['id'], 'token': job['token']})
    return active


def archive_project(root, destination):
    from production_sources import _new_destination, _publish_stage
    root = Path(root).resolve()
    destination = _new_destination(destination)
    if destination.is_relative_to(root):
        raise ValueError('archive destination must be a new file outside the project')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with hatch.ownership(root), ExitStack() as locks:
        bundle = _bundle(root)
        state = _state(root, bundle)
        _bound_runs(root, state)
        run_paths = sorted({path.parent for path in root.rglob('imagegen-jobs.json')})
        for run in run_paths:
            if run != root:
                locks.enter_context(hatch.ownership(run))
        if _active_acquisitions(root):
            raise ValueError('reconcile every active acquisition token before archiving; bound and unbound runs are checked')
        runtime_locks = {(run.parent / ('.' + run.name + '.hatch.lock')).relative_to(root).as_posix()
                         for run in run_paths if run != root}
        files = _project_files(root, runtime_locks)
        if 'archive-receipt.json' in files:
            raise ValueError('archive-receipt.json is reserved for archive inventory metadata')
        receipt = {'schema_version': 2, 'kind': 'hatch-production-project', 'mode': 'full',
                   'project_schema': 1, 'workflow_schema': WORKFLOW_VERSION,
                   'project_id': bundle['project']['id'], 'fixture': bundle['project']['fixture'],
                   'renderer': _renderer_identity(), 'files': files,
                   'external_integration_verified': False}
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.incoming')
        try:
            with zipfile.ZipFile(temporary, 'x', zipfile.ZIP_DEFLATED) as archive:
                for name in files:
                    archive.write(safe_path(root, name), name)
                archive.writestr('archive-receipt.json', json.dumps(receipt, sort_keys=True, indent=2) + '\n')
            verify_project_archive(temporary)
            _publish_stage(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {'archive': str(destination), 'sha256': file_hash(destination),
            'project_id': receipt['project_id'], 'fixture': receipt['fixture'],
            'file_count': len(files), 'external_integration_verified': False}


def _cached_render_freshness(root, bundle):
    from production_render import validate_render
    reusable, affected, dependency_cache = [], [], {}
    paths = sorted(Path(root).rglob('render-manifest.json'))

    def dependencies(scene_id, shot_id):
        key = (scene_id, shot_id)
        if key not in dependency_cache:
            dependency_cache[key] = shot_dependencies(root, scene_id, shot_id, project=bundle)
        return dependency_cache[key]

    def changes(before, after):
        if not isinstance(before, dict):
            return ['dependencies']
        return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))

    for path in paths:
        relative = path.relative_to(root).as_posix()
        detail = {'path': relative, 'changed_dependencies': []}
        try:
            safe_path(root, relative)
            manifest = hatch.read(path)
            if manifest.get('kind') == 'shot-render':
                current = dependencies(manifest['scene_id'], manifest['shot_id'])
                detail['changed_dependencies'] = changes(manifest.get('dependencies'), current)
                _render_artifacts(root, relative, {'kind': 'shot', 'scene_id': manifest['scene_id'],
                                  'shot_id': manifest['shot_id']}, current, bundle)
            elif manifest.get('kind') == 'scene-render':
                scene = next((item for item in bundle['scenes'] if item['id'] == manifest.get('scene_id')), None)
                if scene is None:
                    raise ValueError('cached render refers to a removed scene')
                if (manifest.get('schema_version') != 1 or manifest.get('status') != 'complete' or
                        manifest.get('renderer') != _renderer_identity() or
                        manifest.get('project_id') != bundle['project']['id'] or
                        manifest.get('fixture') is not bundle['project']['fixture']):
                    raise ValueError('cached scene renderer, identity, fixture or completeness changed')
                settings = bundle['project']['render']
                shot_ids = [shot['id'] if isinstance(shot, dict) else shot for shot in scene['shots']]
                duration = sum(bundle['shots'][shot_id]['duration_frames'] for shot_id in shot_ids)
                if (manifest.get('settings') != settings or manifest.get('fps') != settings['fps'] or
                        manifest.get('resolution') != [settings['width'], settings['height']] or
                        manifest.get('frame_count') != duration):
                    raise ValueError('cached scene timing, resolution or settings changed')
                entries = manifest.get('shots')
                if not isinstance(entries, list) or [item.get('id') for item in entries] != shot_ids:
                    raise ValueError('cached scene shot membership or order changed')
                combined, offset = {}, 0
                for entry in entries:
                    shot_id = entry['id']
                    count = bundle['shots'][shot_id]['duration_frames']
                    if entry.get('start_frame') != offset or entry.get('end_frame') != offset + count:
                        raise ValueError('cached scene shot boundaries changed')
                    child_path = safe_path(path.parent, entry['path'])
                    if not child_path.is_file() or file_hash(child_path) != entry['sha256']:
                        raise ValueError('cached scene child manifest differs: ' + entry['path'])
                    child = hatch.read(child_path)
                    current = dependencies(scene['id'], shot_id)
                    detail['changed_dependencies'].extend(
                        shot_id + ':' + key for key in changes(child.get('dependencies'), current))
                    _render_artifacts(root, child_path.relative_to(root).as_posix(),
                                      {'kind': 'shot', 'scene_id': scene['id'], 'shot_id': shot_id},
                                      current, bundle)
                    frames = manifest['frames'][offset:offset + count]
                    if len(frames) != count or any(
                            frame.get('shot_id') != shot_id or frame.get('shot_frame') != index or
                            frame.get('sha256') != child['frames'][index]['sha256']
                            for index, frame in enumerate(frames)):
                        raise ValueError('cached scene frames differ from the exact child render')
                    combined.update(current)
                    offset += count
                if manifest.get('dependencies') != combined:
                    raise ValueError('cached scene dependencies differ from current child renders')
                validate_render(path.parent)
            else:
                raise ValueError('unsupported cached render manifest kind')
        except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError) as exc:
            detail['reason'] = str(exc)
            detail['changed_dependencies'] = sorted(set(detail['changed_dependencies']))
            affected.append(detail)
        else:
            reusable.append(relative)
    return {'checked_renders': len(paths), 'reusable_renders': reusable, 'affected_renders': affected,
            'scope': 'Existing shot and scene render manifests; projects without cached renders still need their initial render.'}


def restore_project(archive_path, destination):
    from production_sources import _new_destination, _publish_stage
    receipt = verify_project_archive(archive_path)
    destination = _new_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.hatch-production-restore-', dir=destination.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for name in receipt['files']:
                target = safe_path(stage, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        bundle = _bundle(stage)
        if bundle['project']['id'] != receipt['project_id'] or bundle['project']['fixture'] is not receipt['fixture']:
            raise ValueError('restored project identity or fixture differs from the verified receipt')
        state = _state(stage, bundle)
        _bound_runs(stage, state)
        if _active_acquisitions(stage):
            raise ValueError('archive contains active acquisition tokens; preserve and reconcile the original run')
        renderer_changed = receipt.get('renderer') != _renderer_identity()
        render_freshness = _cached_render_freshness(stage, bundle)
        rerender_required = renderer_changed or bool(render_freshness['affected_renders'])
        _event(state, 'project-restored', archive_sha256=file_hash(archive_path),
               renderer_changed=renderer_changed, rerender_required=rerender_required,
               affected_renders=[item['path'] for item in render_freshness['affected_renders']],
               external_integration_verified=False)
        _save(stage, state)
        _publish_stage(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'restored': str(destination), 'project_id': receipt['project_id'], 'fixture': receipt['fixture'],
            'renderer_changed': renderer_changed, 'rerender_required': rerender_required,
            'render_freshness': render_freshness,
            'review_freshness': 'Recomputed against exact current dependencies; historical decisions are preserved.',
            'external_integration_verified': False}


def migrate_project(root):
    root = Path(root).resolve()
    with hatch.ownership(root):
        bundle = _bundle(root)
        path = root / WORKFLOW_FILE
        if not path.is_file():
            _save(root, _new_state(bundle))
            return {'migrated': False, 'initialized': True, 'schema_version': WORKFLOW_VERSION}
        old = hatch.read(path)
        if old.get('schema_version') == WORKFLOW_VERSION:
            _state(root, bundle)
            return {'migrated': False, 'schema_version': WORKFLOW_VERSION}
        if old.get('schema_version') != 1:
            raise ValueError('only production workflow schema 1 can migrate explicitly to 2')
        strict_keys(old, {'schema_version', 'project_id', 'fixture', 'reviews', 'generation_bindings',
                          'generation_limits', 'history'}, 'production workflow migration source')
        if (old.get('project_id') != bundle['project']['id'] or
                old.get('fixture') is not bundle['project']['fixture'] or
                not isinstance(old.get('reviews'), list) or
                not isinstance(old.get('generation_bindings'), dict)):
            raise ValueError('migration requires intact source identity, review history and acquisition bindings')
        backup = root / ('migration-workflow-v1-' + uuid.uuid4().hex + '.json')
        shutil.copyfile(path, backup)
        if file_hash(path) != file_hash(backup):
            raise ValueError('migration source backup verification failed')
        state = _new_state(bundle)
        state['generation_bindings'] = copy.deepcopy(old['generation_bindings'])
        if 'generation_limits' in old:
            state['generation_limits'] = copy.deepcopy(old['generation_limits'])
        state['history'] = copy.deepcopy(old.get('history', []))
        _event(state, 'workflow-migrated', source_schema=1, destination_schema=WORKFLOW_VERSION,
               previous_reviews=copy.deepcopy(old['reviews']), backup=backup.name,
               backup_sha256=file_hash(backup), review_required=True)
        _bound_runs(root, state)
        for key, low, high in (('max_calls', 0, 256), ('attempts_per_job', 1, 100), ('concurrency', 1, 16)):
            value = state['generation_limits'].get(key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('migration source has invalid generation limits')
        _save(root, state)
    return {'migrated': True, 'schema_version': WORKFLOW_VERSION, 'backup': str(backup),
            'review_required': True, 'generation_claims_preserved': True}


def _write_immutable(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError('immutable production artifact already exists with different bytes: ' + str(path))
        return
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.incoming')
    try:
        with temporary.open('xb') as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _next_patch(version):
    if not isinstance(version, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('production versions must be explicit numeric semantic versions')
    major, minor, patch = map(int, version.split('.'))
    return f'{major}.{minor}.{patch + 1}'


def _import_descriptor(descriptor):
    location = Path(descriptor).resolve() if isinstance(descriptor, (str, Path)) else None
    value = hatch.read(location) if location else copy.deepcopy(descriptor)
    strict_keys(value, {'schema_version', 'kind', 'source', 'source_sha256', 'fixture', 'origin',
                       'note', 'viewpoint', 'costume', 'drawing_id', 'crop', 'attribution', 'permissions'},
                'component import descriptor')
    if value.get('schema_version') != 1 or value.get('kind') != 'component-import':
        raise ValueError('component import descriptor needs schema_version 1 and kind component-import')
    text(value.get('note'), 'component import note')
    return value, location


def _component_import(root, bundle, character_id, version, component_id, spec, source, new_version, provenance):
    from io import BytesIO
    from production_contracts import load_project
    character = _character(bundle, character_id, version)
    _next_patch(new_version)
    if new_version == version or f'{character_id}@{new_version}' in bundle['characters']:
        raise ValueError('component import requires a new unoccupied character version')
    if tuple(map(int, new_version.split('.'))) <= tuple(map(int, version.split('.'))):
        raise ValueError('new character version must advance the existing version')
    component = next((item for item in character['components'] if item['id'] == component_id), None)
    if component is None:
        raise ValueError('component import target does not exist')
    viewpoint, costume = spec.get('viewpoint'), spec.get('costume')
    if viewpoint not in character['views'] or costume not in {item['id'] for item in character['costumes']}:
        raise ValueError('import viewpoint and costume must already be explicitly declared by the character')
    drawing_id = spec.get('drawing_id', component['default_drawing'])
    if not isinstance(drawing_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', drawing_id):
        raise ValueError('component drawing id must be a portable production id')
    source = Path(source).resolve()
    check_image(source)
    source_bytes = source.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if spec.get('source_sha256') is not None and spec['source_sha256'] != source_sha256:
        raise ValueError('import source bytes differ from the declared source SHA-256')
    with Image.open(source) as opened:
        image = opened.convert('RGBA')
    crop = spec.get('crop')
    if crop is not None:
        if (not isinstance(crop, list) or len(crop) != 4 or any(type(value) is not int for value in crop) or
                not 0 <= crop[0] < crop[2] <= image.width or not 0 <= crop[1] < crop[3] <= image.height):
            raise ValueError('crop must be an integer rectangle inside the original source')
        image = image.crop(crop)
    sizes = {tuple(drawing['image']['size']) for drawing in component['drawings'].values()}
    if len(sizes) != 1 or image.size not in sizes:
        raise ValueError('component import must preserve the exact component viewport; no implicit resizing')
    if image.getchannel('A').getbbox() is None:
        raise ValueError('component import cannot replace a drawing with an empty alpha plane')
    suffix = source.suffix.lower()
    suffix = suffix if suffix in ('.png', '.webp', '.jpg', '.jpeg') else '.source'
    original_relative = 'sources/imports/' + source_sha256 + suffix
    _write_immutable(safe_path(root, original_relative), source_bytes)
    output = BytesIO()
    image.save(output, format='PNG')
    image_bytes = output.getvalue()
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    image_relative = f'art/imports/{character_id}/{component_id}-{image_sha256}.png'
    _write_immutable(safe_path(root, image_relative), image_bytes)
    provenance = {**copy.deepcopy(provenance),
                  'source': {'path': original_relative, 'sha256': source_sha256},
                  'source_sha256': source_sha256,
                  'operation': {'kind': 'crop' if crop else 'rgba-copy',
                                **({'rect': crop} if crop else {}),
                                'note': 'Original bytes retained; component viewport preserved without resizing.'},
                  'note': spec['note']}
    updated = copy.deepcopy(character)
    updated['version'] = new_version
    replacement = next(item for item in updated['components'] if item['id'] == component_id)
    replacement['drawings'][drawing_id] = {
        'viewpoint': viewpoint, 'costume': costume, 'alpha': 'straight',
        'image': {'path': image_relative, 'sha256': image_sha256,
                  'size': list(image.size), 'provenance': provenance}}
    updated['view_reviews'] = {view: {'status': 'pending', 'kind': 'unreviewed', 'reviewer': None,
        'evidence': [], 'note': 'New component version requires construction and visual review.'}
        for view in updated['views']}
    cast = copy.deepcopy(bundle['cast'])
    cast['version'] = _next_patch(cast['version'])
    cast['characters'].append(updated)
    cast_bytes = _json_bytes(cast)
    cast_sha256 = hashlib.sha256(cast_bytes).hexdigest()
    cast_relative = 'cast/cast-' + cast_sha256 + '.json'
    _write_immutable(safe_path(root, cast_relative), cast_bytes)
    project = copy.deepcopy(bundle['project'])
    project['cast'] = {'path': cast_relative, 'sha256': cast_sha256}
    candidate = Path(root) / ('.candidate-project-' + uuid.uuid4().hex + '.json')
    try:
        hatch.atomic(candidate, project)
        load_project(candidate)
        os.replace(candidate, Path(root) / 'project.json')
    finally:
        candidate.unlink(missing_ok=True)
    state = _state(root, bundle)
    _event(state, 'component-imported', character_id=character_id, previous_version=version,
           new_version=new_version, component_id=component_id, drawing_id=drawing_id,
           source_sha256=source_sha256, image_sha256=image_sha256, provenance_kind=provenance['kind'])
    _save(root, state)
    return {'imported': True, 'character': f'{character_id}@{new_version}',
            'previous_character': f'{character_id}@{version}', 'component': component_id,
            'drawing_id': drawing_id, 'image': replacement['drawings'][drawing_id]['image'],
            'cast': project['cast'], 'fixture': bundle['project']['fixture'],
            'review_required': True, 'clip_rebinding_required': True, 'provider_called': False}


def import_component(root, character_id, version, component_id, descriptor, new_version):
    root = Path(root).resolve()
    spec, location = _import_descriptor(descriptor)
    text(spec.get('origin'), 'import source origin')
    text(spec.get('source'), 'original component source path')
    source = Path(spec['source'])
    source = source.resolve() if source.is_absolute() else ((location.parent if location else Path.cwd()) / source).resolve()
    if source.is_relative_to(root):
        raise ValueError('import source must be the original file outside the managed project')
    with hatch.ownership(root):
        bundle = _bundle(root)
        if spec.get('fixture') is not bundle['project']['fixture']:
            raise ValueError('component import must preserve the explicit project fixture label')
        provenance = {'kind': 'import', 'origin': spec['origin'], 'fixture': spec['fixture'],
                      **{key: spec[key] for key in ('attribution', 'permissions') if key in spec}}
        return _component_import(root, bundle, character_id, version, component_id, spec, source,
                                 new_version, provenance)


def adopt_generated_component(root, binding_id, job, character_id, version, component_id, descriptor, new_version):
    root = Path(root).resolve()
    spec, _ = _import_descriptor(descriptor)
    if 'source' in spec or 'origin' in spec:
        raise ValueError('generated component adoption takes its source and origin from the bound acquisition')
    with hatch.ownership(root):
        bundle = _bundle(root)
        if 'fixture' in spec and spec['fixture'] is not bundle['project']['fixture']:
            raise ValueError('generated component adoption cannot relabel fixture provenance')
        state = _state(root, bundle)
        selected = next((item for item in _bound_runs(root, state) if item[0] == binding_id), None)
        if selected is None:
            raise ValueError('unknown production acquisition binding')
        _, binding, run, _ = selected
        with hatch.ownership(run):
            data = hatch.manifest(run)
            hatch.reconcile(run, data)
            item = hatch.job_for(data, job)
            if item['status'] != 'generated' or not isinstance(item.get('generation_receipt'), dict):
                raise ValueError('adoption requires a current generated job with its original generation receipt')
            source = hatch.inside(run, item['output_path'])
            if not source.is_file() or file_hash(source) != item['output_hash']:
                raise ValueError('generated source is missing or differs from its acquisition receipt')
            acquisition = {'schema_version': 1, 'kind': 'production-acquisition', 'binding_id': binding_id,
                           **_binding_context(binding_id, binding), 'job': copy.deepcopy(item)}
            receipt_bytes = _json_bytes(acquisition)
            receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
            receipt_relative = 'provenance/acquisition-' + receipt_sha256 + '.json'
            _write_immutable(safe_path(root, receipt_relative), receipt_bytes)
            model = item.get('model_call', {'id': None, 'model': None, 'parameters': None,
                              'unavailable_reason': 'The handoff did not expose provider call metadata.'})
            provenance = {'kind': 'fixture' if bundle['project']['fixture'] else 'imagegen',
                          'origin': 'Bound hatch acquisition ' + data['run_id'] + '/' + job,
                          'fixture': bundle['project']['fixture'],
                          'acquisition': {'run_id': data['run_id'], 'job': job,
                              'receipt_path': receipt_relative, 'receipt_sha256': receipt_sha256,
                              'model_call': copy.deepcopy(model)}}
            return _component_import(root, bundle, character_id, version, component_id, spec, source,
                                     new_version, provenance)


def dependency_snapshot(root, scene_id=None, project=None):
    bundle = _bundle(root, project, verify_hashes=False)
    scenes = [scene for scene in bundle['scenes'] if scene_id is None or scene['id'] == scene_id]
    if not scenes:
        raise ValueError('unknown scene: ' + str(scene_id))
    shots = {}
    for scene in scenes:
        for item in scene['shots']:
            shot_id = item['id'] if isinstance(item, dict) else item
            dependencies = shot_dependencies(root, scene['id'], shot_id, project=bundle)
            shots[scene['id'] + '/' + shot_id] = {'dependencies': dependencies, 'sha256': hash_object(dependencies)}
    return {'schema_version': 1, 'project_id': bundle['project']['id'],
            'fixture': bundle['project']['fixture'], 'shots': shots}


def impact_report(root, snapshot, scene_id=None):
    previous = hatch.read(snapshot) if isinstance(snapshot, (str, Path)) else copy.deepcopy(snapshot)
    current = dependency_snapshot(root, scene_id)
    if (previous.get('schema_version') != 1 or previous.get('project_id') != current['project_id'] or
            previous.get('fixture') is not current['fixture'] or not isinstance(previous.get('shots'), dict)):
        raise ValueError('dependency snapshot must retain the exact project identity and fixture label')
    affected, reusable = [], []
    for shot in sorted(set(current['shots']) | set(previous['shots'])):
        before = previous['shots'].get(shot, {}).get('dependencies', {})
        after = current['shots'].get(shot, {}).get('dependencies', {})
        changes = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if not changes:
            reusable.append(shot)
            continue
        actions = {'rerender', 'review_changed_dimensions'}
        if any(key.startswith('audio') or key == 'dialogue' for key in changes):
            actions.add('remix_audio_and_check_sync')
        if any(after.get(key) == 'missing' for key in changes):
            actions.add('recover_exact_missing_source')
        affected.append({'shot': shot, 'changed_dependencies': changes, 'required_actions': sorted(actions),
                         'generation_required': False,
                         'reason': 'Only declared source, track, camera and continuity dependencies used by this shot are compared.'})
    return {'schema_version': 1, 'project_id': current['project_id'], 'fixture': current['fixture'],
            'affected_shots': affected, 'reusable_shots': reusable, 'current_snapshot': current,
            'provider_called': False}


def _available_values(character, dimension):
    values = set(character['capabilities'].get(dimension, []))
    if dimension == 'viewpoint':
        values.update(character['views'])
    elif dimension == 'facing':
        values.update(character['identity']['allowed_facings'])
    elif dimension == 'costume':
        values.update(item['id'] for item in character['costumes'])
    return values


def coverage_report(root, scene_id=None):
    """Plan independent requirements and bounded missing work, never a Cartesian asset explosion."""
    from production_contracts import planned_requirements
    from production_performance import incompatible_requirements
    root = Path(root).resolve()
    try:
        bundle = _bundle(root, verify_hashes=False)
    except (ValueError, OSError) as exc:
        return {'schema_version': 1, 'validated': False, 'ready': False, 'requirements': [],
                'reusable': [], 'missing': [],
                'incompatible': [{'reason': str(exc), 'required_action': 'Repair the project contract or source references before planning generation.'}],
                'unreviewed': [], 'expected_generation': None, 'provider_called': False}
    scenes = [scene for scene in bundle['scenes'] if scene_id is None or scene['id'] == scene_id]
    if not scenes:
        raise ValueError('unknown scene: ' + str(scene_id))
    requirements, selected_shots = [], []
    for scene in scenes:
        for item in scene['shots']:
            shot_id = item['id'] if isinstance(item, dict) else item
            selected_shots.append((scene['id'], shot_id))
            requirements.extend({**requirement, 'scene': scene['id']}
                                for requirement in planned_requirements(bundle, shot_id))
    reusable, missing, incompatible, unreviewed, statuses = [], [], [], [], {}
    for requirement in requirements:
        character = bundle['characters'][requirement['character']]
        if requirement['value'] not in _available_values(character, requirement['dimension']):
            missing.append({**requirement, 'reason': 'This independently requested capability is not declared.',
                            'work_key': hash_object([requirement['character'], requirement['dimension'], requirement['value']])})
            continue
        key = requirement['character']
        if key not in statuses:
            character_id, version = key.rsplit('@', 1)
            target = {'kind': 'character', 'character_id': character_id, 'version': version}
            statuses[key] = review_status(root, target, bundle)
        status = statuses[key]
        if status['current_pass'] and (bundle['project']['fixture'] or status['production_art_acceptance']):
            reusable.append(requirement)
        else:
            unreviewed.append({**requirement, 'target': status['target'], 'review': status,
                               'reason': 'Current construction review is required for the declared asset coverage.'})
    seen_clips = set()
    for scene, shot_id in selected_shots:
        shot = bundle['shots'][shot_id]
        for instance in shot['instances']:
            for placement in instance['clips']:
                key = placement['clip']
                if key in seen_clips:
                    continue
                seen_clips.add(key)
                clip_id, version = key.rsplit('@', 1)
                target = {'kind': 'clip', 'clip_id': clip_id, 'version': version}
                status = review_status(root, target, bundle)
                if not status['current_pass'] or (not bundle['project']['fixture'] and not status['production_art_acceptance']):
                    unreviewed.append({'scene': scene, 'shot': shot_id, 'instance': instance['id'],
                                       'dimension': 'clip_review', 'value': key, 'target': target, 'review': status,
                                       'reason': 'Reusable motion, contact and timing dimensions require current clip review.'})
            scoped = [item for item in requirements if item['shot'] == shot_id and item['instance'] == instance['id']]
            character = bundle['characters'][instance['character']]
            for restriction in incompatible_requirements(character, scoped):
                incompatible.append({'scene': scene, 'shot': shot_id, 'instance': instance['id'],
                    'requirements': [item['id'] for item in scoped], **restriction,
                    'required_action': 'Revise the overlapping performance or author and review compatible assets.'})
        dependencies = shot_dependencies(root, scene, shot_id, project=bundle)
        for key, digest in dependencies.items():
            if digest == 'missing':
                missing.append({'scene': scene, 'shot': shot_id, 'dimension': 'asset', 'value': key,
                                'work_key': 'recover:' + key.split(':', 1)[-1], 'reason': 'Recover the exact referenced source; generation is not a substitute for missing provenance or audio.'})
    work = {item['work_key']: item for item in missing}
    recovery = [item for item in work.values() if item['dimension'] == 'asset']
    authoring_dimensions = {'action', 'pose', 'gaze', 'intensity', 'contact'}
    authoring = [item for item in work.values() if item['dimension'] in authoring_dimensions]
    raster = [item for item in work.values() if item not in recovery and item not in authoring]
    generation = generation_status(root, project=bundle)
    remaining = generation['remaining_calls']
    expected = {'unique_missing_work_items': len(work), 'raster_asset_requests': len(raster),
                'motion_or_track_authoring_items': len(authoring), 'source_recovery_items': len(recovery),
                'minimum_calls_if_each_raster_request_needs_generation': len(raster),
                'bounded_attempts': min(len(raster) * generation['limits']['attempts_per_job'], remaining),
                'remaining_call_limit': remaining, 'within_limit': len(raster) <= remaining,
                'estimate_note': 'Generation is optional for missing raster coverage. Recover exact missing source, provenance and audio bytes; author clips or tracks for motion work.'}
    stale = bundle.get('stale', [])
    return {'schema_version': 1, 'project_id': bundle['project']['id'], 'fixture': bundle['project']['fixture'],
            'scene_id': scene_id, 'requirements': requirements, 'requirements_sha256': hash_object(requirements),
            'reusable': reusable, 'missing': missing, 'incompatible': incompatible, 'unreviewed': unreviewed,
            'expected_generation': expected, 'generation': generation, 'stale_sources': stale,
            'validated': not stale, 'ready': not (missing or incompatible or unreviewed or stale),
            'coverage_method': 'Independent dimensions plus declared combination restrictions; no exhaustive state generation.',
            'provider_called': False}
