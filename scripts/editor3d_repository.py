"""Durable editor revisions around the authoritative native Scene3D owner.

Every edit is evaluated in an isolated native snapshot. Only a verified immutable
revision can replace HEAD. Session previews never write persistent scene state.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import hashlib
import io
from itertools import chain
import zlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import tempfile
import threading
import time
import unicodedata
import zipfile

from atomic_publish import (_rename_no_replace, pinned_directory, pinned_directory_fd,
                            private_stage_fd_supported, private_stage_supported,
                            publish_verified_file, VerifiedFileCleanupError, windows_private_stage_supported)
from rig_errors import (RigError, atomic_bytes, decode_json, directory_capability, finite_json,
                        job_lock, portable_name, safe_path, timestamp, open_payload)
from scene3d_contracts import load_project

FORMAT = 1
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_COMMAND_BYTES = 1024 * 1024
ARCHIVE_MAX_RATIO = 1000
DEFAULT_LIMITS = dict(max_revisions=256, max_history=128, max_bytes=1024**3, max_file_bytes=64*1024**2,
                      max_files=10000, autosave_delay=1.0, autosave_interval=2.0)
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')



def _archive_snapshot(source):
    maximum = DEFAULT_LIMITS['max_bytes'] + MAX_METADATA_BYTES + (DEFAULT_LIMITS['max_files'] + 1) * 4096
    def capture(stream):
        stream.seek(0)
        data = stream.read(maximum + 1)
        _require(len(data) <= maximum, 'LIMIT', 'Encoded archive exceeds its byte budget.')
        return data
    if hasattr(source, 'read'):
        return capture(source)
    path = Path(source).absolute()
    with open_payload(path.parent, path.name) as (stream, info):
        _require(info.st_size <= maximum, 'LIMIT', 'Encoded archive exceeds its byte budget.')
        data = capture(stream)
        _require(len(data) == info.st_size, 'ARCHIVE', 'Archive changed while reading.')
        return data


def _archive_compression(payload):
    if len(payload) > ARCHIVE_MAX_RATIO:
        # Match ZipFile's default raw DEFLATE stream and keep the verifier's limit.
        compressed_size = len(zlib.compress(payload, wbits=-zlib.MAX_WBITS))
        if len(payload) > ARCHIVE_MAX_RATIO * compressed_size:
            return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _archive_member(archive, info, snapshot):
    # ZipExtFile clips output at file_size, hiding extra deflate output. Validate
    # the actual compressed stream with a bounded decoder before accepting bytes.
    _require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
             and not info.flag_bits & 1, 'ARCHIVE', 'Unsupported archive compression.')
    offset = info.header_offset
    _require(offset >= 0 and offset + 30 <= len(snapshot), 'ARCHIVE', 'Truncated local header.')
    signature, _, flags, method, _, _, crc, compressed_size, size, _, _ = struct.unpack_from(
        '<4s5H3I2H', snapshot, offset)
    _require(signature == b'PK\x03\x04' and flags == info.flag_bits and method == info.compress_type
             and (flags & 8 or (crc == info.CRC and compressed_size == info.compress_size and size == info.file_size)),
             'ARCHIVE', 'Local and central ZIP headers differ.')
    with archive.open(info) as member:
        start = member._fileobj.tell()  # zipfile has validated the local header and overlap.
        compressed = memoryview(snapshot)[start:start + info.compress_size]
        _require(len(compressed) == info.compress_size, 'ARCHIVE', 'Truncated archive member.')
        result = bytearray()
        if info.compress_type == zipfile.ZIP_STORED:
            _require(len(compressed) == info.file_size, 'ARCHIVE', 'Stored member size differs.')
            result.extend(compressed)
        else:
            decoder = zlib.decompressobj(-15)
            cursor = 0
            pending = b''
            while cursor < len(compressed) or pending:
                if not pending:
                    end = min(cursor + 65536, len(compressed))
                    pending = compressed[cursor:end]
                    cursor = end
                chunk = decoder.decompress(pending, min(65536, info.file_size - len(result) + 1))
                result.extend(chunk)
                _require(len(result) <= info.file_size, 'ARCHIVE', 'Member exceeds declared size.')
                pending = decoder.unconsumed_tail
                if decoder.eof:
                    _require(not pending and not decoder.unused_data and cursor == len(compressed),
                             'ARCHIVE', 'Trailing compressed member data.')
                    break
            _require(decoder.eof, 'ARCHIVE', 'Truncated compressed member.')
        _require(len(result) == info.file_size and zlib.crc32(result) == info.CRC,
                 'ARCHIVE', 'Archive member length or checksum differs.')
        return bytes(result)

def _fail(code, message):
    raise RigError('EDITOR3D_' + code, message, stage='editor3d-repository')


def _require(condition, code, message):
    if not condition:
        _fail(code, message)


def _bytes(value):
    finite_json(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _next_history(head, action, maximum):
    """Derive the history target and bounded stacks from one committed parent."""
    parent = head['revision']
    undo, redo = list(head['undo']), list(head['redo'])
    target = None
    if action == 'undo':
        _require(undo, 'HISTORY', 'No retained undo entry.')
        target = undo.pop()
        redo.append(parent)
    elif action == 'redo':
        _require(redo, 'HISTORY', 'No retained redo entry.')
        target = redo.pop()
        undo.append(parent)
    else:
        _require(action in ('apply', 'restore', 'native-adapter'), 'HISTORY', 'Unknown revision action.')
        undo.append(parent)
        redo.clear()
    return target, undo[-maximum:], redo[-maximum:]


def _replay_history(records, maximum):
    """Replay format-1 actions, retaining only the identity of each restore target."""
    snapshots = []
    restore_targets = {}
    head = None
    for revision, record in enumerate(records):
        _require(isinstance(record, dict) and type(record.get('revision')) is int and
                 record['revision'] == revision, 'HISTORY', 'Revision sequence differs from its records.')
        _require(isinstance(record.get('scene_path'), str) and
                 all(isinstance(record.get(key), str) and re.fullmatch('[0-9a-f]{64}', record[key])
                     for key in ('request_sha256', 'snapshot_sha256')),
                 'HISTORY', 'History requires a scene path and exact request and snapshot digests.')
        action = record['action']
        snapshot = (record['snapshot_sha256'], record['scene_path'])
        if revision == 0:
            _require(record['parent'] is None and action == 'create', 'HISTORY', 'Invalid initial revision.')
            undo, redo = [], []
        else:
            _require(type(record['parent']) is int and record['parent'] == revision - 1,
                     'HISTORY', 'History action differs from its parent.')
            target, undo, redo = _next_history(head, action, maximum)
            if action == 'restore':
                # Format 1 records the digest of the restore request, including its target.
                for i in range(len(restore_targets), revision):
                    restore_targets[_digest(dict(action=action, target=i))] = i
                target = restore_targets.get(record['request_sha256'])
                _require(target is not None, 'HISTORY', 'Restore request has no committed target.')
            elif target is not None:
                _require(record['request_sha256'] == _digest(dict(action=action, target=None)),
                         'HISTORY', 'History action differs from its recorded request.')
            if target is not None:
                _require(snapshot == snapshots[target], 'HISTORY', 'History action differs from its selected snapshot.')
        snapshots.append(snapshot)
        head = dict(schema_version=FORMAT, revision=revision, sha256=snapshot[0], undo=undo, redo=redo)
    return head


def _read_json(path):
    try:
        _require(not path.is_symlink() and path.is_file(), 'FILE', 'Expected regular metadata: ' + str(path))
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(fd, 'rb') as stream:
            import stat
            _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'FILE', 'Metadata must be a regular file.')
            raw = stream.read(MAX_METADATA_BYTES + 1)
        _require(len(raw) <= MAX_METADATA_BYTES, 'LIMIT', 'Repository metadata exceeds 4 MiB.')
        value = decode_json(raw)
        _require(isinstance(value, dict), 'FILE', 'Metadata must be a JSON object.')
        return value
    except OSError as exc:
        raise RigError('EDITOR3D_FILE', str(exc), stage='editor3d-repository') from exc


def _sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _sync_directory_tree(root):
    if os.name != 'posix':
        return

    def walk_error(error):
        raise error

    for directory, _, _ in os.walk(root, topdown=False, onerror=walk_error):
        _sync_directory(directory)


def _metadata_bytes(value):
    finite_json(value)
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def _write_metadata(path, value):
    _require(not path.is_symlink(), 'PATH', 'Metadata cannot be a symbolic link.')
    atomic_bytes(path, _metadata_bytes(value))
    _sync_directory(path.parent)


def _directory_identity(path):
    info = path.stat(follow_symlinks=False)
    _require(stat.S_ISDIR(info.st_mode), 'PATH', 'Repository root must be a directory.')
    return info.st_dev, info.st_ino


def _write_checkpoint_at(directory, name, payload):
    temporary = '.autosave-' + secrets.token_hex(16) + '.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    committed = False
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        committed = True
        os.fsync(directory)
    finally:
        if not committed:
            os.unlink(temporary, dir_fd=directory)


def _check_id(value):
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None, 'TRANSACTION', 'Use a bounded transaction identifier.')


def _regular_bytes(root, name, expected, max_bytes):
    path = safe_path(root, name, exists=True)
    _require(not getattr(path, 'is_junction', lambda: False)(), 'PATH', 'Junctions are not repository resources.')
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            import stat
            size = os.fstat(stream.fileno()).st_size
            _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode) and size <= max_bytes,
                     'LIMIT', 'Resource is not a bounded regular file.')
            payload = stream.read(max_bytes + 1)
        _require(len(payload) <= max_bytes and len(payload) == expected['bytes'] and
                 hashlib.sha256(payload).hexdigest() == expected['sha256'], 'HASH', 'Resource bytes changed: ' + name)
        return payload
    except OSError as exc:
        raise RigError('EDITOR3D_FILE', str(exc), stage='editor3d-repository', asset=name) from exc


def _portable_alias(name):
    return unicodedata.normalize('NFC', name).casefold()


def _scan(root, limits, *, replacement=None, bound_content=True):
    """Bound traversal itself, rejecting links before descending."""
    import stat
    pending=[(Path(root),'')]; files={}; aliases=set(); entries=0; total=0
    while pending:
        directory,prefix=pending.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries+=1
                _require(entries <= limits['max_files']*4, 'LIMIT', 'Repository directory-entry budget exceeded.')
                name=portable_name(prefix+entry.name)
                alias=_portable_alias(name)
                _require(alias not in aliases, 'PATH', 'Portable path alias collision.')
                aliases.add(alias)
                _require(not entry.is_symlink() and not getattr(Path(entry.path),'is_junction',lambda:False)(),
                         'PATH','Repository contains a symbolic link or junction.')
                info=entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    _require(len(Path(name).parts) <= 128, 'LIMIT', 'Repository directory depth exceeded.')
                    pending.append((Path(entry.path),name+'/'))
                else:
                    _require(stat.S_ISREG(info.st_mode), 'FILE', 'Repository contains a nonregular file.')
                    total+=info.st_size
                    if bound_content:
                        _require(len(files)<limits['max_files'] and info.st_size<=limits['max_file_bytes'] and
                                 total<=limits['max_bytes'], 'LIMIT', 'Repository exceeds file or byte limits.')
                    files[name]=info.st_size
    if replacement is not None:
        name, size = replacement
        exists = name in files
        _require(exists or _portable_alias(name) not in aliases, 'PATH',
                 'Checkpoint destination conflicts with an existing entry.')
        _require(entries + int(not exists) <= limits['max_files'] * 4 and
                 len(files) + int(not exists) <= limits['max_files'] and
                 size <= limits['max_file_bytes'] and
                 total - files.get(name, 0) + size <= limits['max_bytes'],
                 'LIMIT', 'Checkpoint would exceed retained repository limits.')
        files[name] = size
    return files


def _copy_closure(source, destination, limits):
    bundle = load_project(source)
    _require(len(bundle['inventory']) <= limits['max_files'], 'LIMIT', 'Native closure exceeds file budget.')
    _require(bundle['bytes'] <= limits['max_bytes'], 'LIMIT', 'Native closure exceeds byte budget.')
    destination.mkdir(mode=0o700)
    for name, record in bundle['inventory'].items():
        payload = _regular_bytes(source, name, record, limits['max_file_bytes'])
        target = destination / portable_name(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    return load_project(destination)


def _validated_limits(values):
    limits = dict(DEFAULT_LIMITS)
    _require(values is None or isinstance(values, dict) and set(values) <= set(limits), 'LIMIT', 'Unknown repository limits.')
    limits.update(values or {})
    for name in ('max_revisions', 'max_history', 'max_bytes', 'max_file_bytes', 'max_files'):
        _require(type(limits[name]) is int and 1 <= limits[name] <= DEFAULT_LIMITS[name], 'LIMIT', 'Invalid ' + name)
    _require(limits['max_history'] <= limits['max_revisions'], 'LIMIT', 'History cannot exceed retained revisions.')
    for name in ('autosave_delay', 'autosave_interval'):
        value = limits[name]
        _require(type(value) in (int, float) and 0.1 <= value <= 3600, 'LIMIT', 'Invalid ' + name)
    return limits


def _validate_revision_record(value, revision, limits):
    required = {'schema_version','revision','parent','snapshot_sha256','project_sha256','scene_path',
                'transaction_id','request_sha256','action','label','created_at','inventory'}
    _require(isinstance(value, dict) and set(value) == required and type(value['schema_version']) is int and value['schema_version'] == FORMAT and
             type(value['revision']) is int and value['revision'] == revision,
             'REVISION', 'Invalid immutable revision record.')
    _require(isinstance(value['inventory'], dict) and 0 < len(value['inventory']) <= limits['max_files'],
             'REVISION', 'Invalid snapshot inventory.')
    for name, record in value['inventory'].items():
        portable_name(name)
        _require(isinstance(record, dict) and set(record) == {'bytes','sha256'} and type(record['bytes']) is int and
                 0 <= record['bytes'] <= limits['max_file_bytes'] and isinstance(record['sha256'], str) and
                 re.fullmatch('[0-9a-f]{64}', record['sha256']), 'REVISION', 'Invalid immutable file record.')
    _require(_digest(value['inventory']) == value['snapshot_sha256'], 'HASH', 'Revision inventory digest differs.')
    _require(isinstance(value['scene_path'], str) and value['scene_path'] in value['inventory'],
             'SCENE', 'Revision scene is absent from its inventory.')
    _check_id(value['transaction_id'])
    return value

class EditorRepository:
    """Repository of exact native project revisions, with optimistic concurrency.

    ``apply`` accepts the native Scene3D command batch (not executable callbacks).
    The complete native owner must be installed; absence fails explicitly.
    """
    def __init__(self, root):
        given = Path(root).absolute()
        _require(not given.is_symlink() and given.is_dir(), 'FILE', 'Open an existing editor repository.')
        self.root = given.resolve(strict=True)
        self._root_identity = _directory_identity(self.root)
        self._metadata = _read_json(safe_path(self.root, 'repository.json', exists=True))
        _require(set(self._metadata) == {'schema_version', 'kind', 'id', 'created_at', 'limits'} and
                 type(self._metadata['schema_version']) is int and self._metadata['schema_version'] == FORMAT and
                 self._metadata['kind'] == 'editor3d-repository', 'VERSION', 'Unsupported editor repository.')
        _check_id(self._metadata['id'])
        self.limits = _validated_limits(self._metadata['limits'])
        self._drag = None
        self._autosave_lock = threading.Lock()
        self._pending_autosave = None
        self._last_autosave = 0.0
        self._head()

    @classmethod
    def create(cls, source, destination, *, identifier='editor-project', scene_path=None, limits=None):
        """Capture a verified native project at a new exclusive destination."""
        _check_id(identifier)
        limits = _validated_limits(limits)
        destination = Path(destination).absolute()
        _require(not destination.exists() and not destination.is_symlink(), 'DESTINATION', 'Destination must be new.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = destination.parent.resolve(strict=True) / destination.name
        with tempfile.TemporaryDirectory(prefix='.editor-create-', dir=destination.parent) as temporary:
            stage = Path(temporary) / 'repository'; stage.mkdir(mode=0o700)
            revision = stage / 'revisions' / '00000000'; revision.mkdir(parents=True)
            bundle = _copy_closure(Path(source), revision / 'native', limits)
            scenes = [r['path'] for r in bundle['project']['scenes']]
            scene_path = scene_path or (scenes[0] if scenes else None)
            _require(scene_path in scenes, 'SCENE', 'Select a scene in the native project.')
            closure = _digest(bundle['inventory'])
            record = dict(schema_version=FORMAT, revision=0, parent=None, snapshot_sha256=closure,
                          project_sha256=bundle['inventory']['project.json']['sha256'], scene_path=scene_path,
                          transaction_id='initial', request_sha256=_digest({'action':'create'}), action='create',
                          label='Import native project', created_at=timestamp(), inventory=bundle['inventory'])
            _write_metadata(revision / 'record.json', record)
            _write_metadata(stage / 'repository.json', dict(schema_version=FORMAT, kind='editor3d-repository',
                                                       id=identifier, created_at=timestamp(), limits=limits))
            _write_metadata(stage / 'HEAD.json', dict(schema_version=FORMAT, revision=0, sha256=closure, undo=[], redo=[]))
            (stage / 'journal').mkdir()
            _scan(stage, limits)
            cls(stage)
            _sync_directory_tree(stage)
            _rename_no_replace(stage, destination)
            _sync_directory(destination.parent)
        return cls(destination)

    def _head(self, *, verify_history=False):
        value = _read_json(safe_path(self.root, 'HEAD.json', exists=True))
        _require(set(value) == {'schema_version', 'revision', 'sha256', 'undo', 'redo'} and
                 type(value['schema_version']) is int and value['schema_version'] == FORMAT and
                 type(value['revision']) is int and 0 <= value['revision'] < self.limits['max_revisions'] and
                 isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}', value['sha256']) and
                 all(isinstance(value[k], list) and len(value[k]) <= self.limits['max_history'] and
                     all(type(v) is int and 0 <= v < value['revision'] for v in value[k]) for k in ('undo', 'redo')),
                 'HEAD', 'Invalid repository HEAD or undo/redo history.')
        record = self._record(value['revision'])
        _require(record['snapshot_sha256'] == value['sha256'], 'HASH', 'HEAD differs from its immutable revision.')
        if verify_history:
            _require(self._history_at(value['revision']) == value, 'HISTORY',
                     'HEAD history differs from its revision actions.')
        return value

    def _record(self, revision, *, directory=None):
        _require(type(revision) is int and 0 <= revision < self.limits['max_revisions'], 'REVISION', 'Invalid revision number.')
        if directory is None:
            path = safe_path(self.root, f'revisions/{revision:08d}/record.json', exists=True)
        else:
            path = safe_path(directory, 'record.json', exists=True)
        value = _read_json(path)
        return _validate_revision_record(value, revision, self.limits)

    def _native_root(self, revision):
        path = safe_path(self.root, f'revisions/{revision:08d}/native')
        _require(path.is_dir(), 'REVISION', 'Native snapshot is missing.')
        return path

    def _bundle(self, record, *, root=None):
        if root is None:
            root = self._native_root(record['revision'])
        # Hash all retained closure bytes, including source/provenance inputs.
        for name, expected in record['inventory'].items():
            _regular_bytes(root, name, expected, self.limits['max_file_bytes'])
        bundle = load_project(root)
        _require(bundle['inventory'] == record['inventory'], 'HASH', 'Native closure differs from saved inventory.')
        return bundle

    def _committed_record(self, revision, head):
        _require(type(revision) is int and 0 <= revision <= head['revision'],
                 'REVISION', 'Choose a committed revision.')
        return self._record(revision)

    def _history_at(self, revision):
        return _replay_history((self._record(i) for i in range(revision + 1)), self.limits['max_history'])

    def snapshot(self, revision=None):
        head = self._head(verify_history=True)
        record = self._committed_record(head['revision'] if revision is None else revision, head)
        bundle = self._bundle(record)
        if record['revision'] != head['revision']:
            head = self._history_at(record['revision'])
        return dict(revision=record['revision'], sha256=record['snapshot_sha256'],
                    project=bundle['project'], documents=bundle['documents'], inventory=bundle['inventory'],
                    scene=deepcopy(bundle['documents'][record['scene_path']]), scene_path=record['scene_path'],
                    undo_available=bool(head['undo']), redo_available=bool(head['redo']))

    def native_snapshot(self, revision, expected_sha256):
        """Verified immutable source reference for native render/import owners.

        Consumers must copy/recheck input bytes at their admission boundary.
        This returns a native filesystem location to trusted Python callers only.
        """
        record = self._committed_record(revision, self._head())
        _require(record['snapshot_sha256'] == expected_sha256, 'HASH', 'Requested native revision hash differs.')
        self._bundle(record)
        return dict(root=self._native_root(revision), revision=revision, sha256=expected_sha256,
                    scene_path=record['scene_path'], inventory=deepcopy(record['inventory']))

    def read_file(self, path, snapshot_sha256):
        _require(isinstance(snapshot_sha256, str) and re.fullmatch('[0-9a-f]{64}', snapshot_sha256), 'HASH', 'Use an exact snapshot digest.')
        head = self._head()
        for revision in range(head['revision'], -1, -1):
            record = self._record(revision)
            if record['snapshot_sha256'] == snapshot_sha256:
                _require(path in record['inventory'], 'PATH', 'File is outside the verified native closure.')
                return _regular_bytes(self._native_root(revision), path, record['inventory'][path], self.limits['max_file_bytes'])
        _fail('REVISION', 'Unknown snapshot digest.')

    def _check_expected(self, head, expected_revision, expected_sha256):
        _require(type(expected_revision) is int and expected_revision == head['revision'] and
                 (expected_sha256 is None or expected_sha256 == head['sha256']), 'STALE', 'Reload and reapply against the current revision.')

    def _existing_transaction(self, head, transaction_id, request_hash):
        for revision in range(head['revision'] + 1):
            record = self._record(revision)
            if record['transaction_id'] == transaction_id:
                _require(record['request_sha256'] == request_hash, 'IDEMPOTENCY', 'Transaction identifier was used for different content.')
                return self.snapshot(revision)
        return None

    def _space(self, next_revision, estimate):
        _require(next_revision < self.limits['max_revisions'], 'LIMIT', 'Revision budget reached; export and start a new repository.')
        used = sum(_scan(self.root, self.limits).values())
        _require(used + estimate <= self.limits['max_bytes'], 'LIMIT', 'Retained repository byte budget exceeded.')

    def _commit(self, head, transaction_id, request, label, action, build,
                *, schedule_autosave=False, expected_inventory=None):
        _require(not self.recovery(), 'RECOVERY', 'Resolve interrupted transactions before another edit.')
        _, undo, redo = _next_history(head, action, self.limits['max_history'])
        revision = head['revision'] + 1
        self._space(revision, sum(self._record(head['revision'])['inventory'][n]['bytes'] for n in self._record(head['revision'])['inventory']))
        transaction_hash = _digest(request)
        journal_name = hashlib.sha256(transaction_id.encode()).hexdigest() + '.json'
        journal = safe_path(self.root, 'journal/' + journal_name)
        _require(not journal.exists(), 'RECOVERY', 'An interrupted transaction requires inspection before retry.')
        pending = dict(schema_version=FORMAT, transaction_id=transaction_id, request_sha256=transaction_hash,
                       expected_revision=head['revision'], expected_sha256=head['sha256'], revision=revision,
                       status='building', new_head=None, stage=None)
        _write_metadata(journal, pending)
        _sync_directory(self.root)
        stage = Path(tempfile.mkdtemp(prefix='.editor-revision-', dir=safe_path(self.root, 'revisions')))
        pending['stage'] = stage.name; _write_metadata(journal, pending)
        try:
            scene_path = build(stage / 'native')
            bundle = load_project(stage / 'native')
            _require(expected_inventory is None or bundle['inventory'] == expected_inventory,
                     'HASH', 'Staged history differs from the selected immutable revision.')
            _require(scene_path in bundle['documents'] and bundle['documents'][scene_path]['kind'] == 'scene3d-scene',
                     'SCENE', 'Edited snapshot lacks its active native scene.')
            _require(bundle['bytes'] <= self.limits['max_bytes'] and len(bundle['inventory']) <= self.limits['max_files'],
                     'LIMIT', 'New snapshot exceeds resource budget.')
            record = dict(schema_version=FORMAT, revision=revision, parent=head['revision'],
                          snapshot_sha256=_digest(bundle['inventory']), project_sha256=bundle['inventory']['project.json']['sha256'],
                          scene_path=scene_path, transaction_id=transaction_id, request_sha256=transaction_hash,
                          action=action, label=label, created_at=timestamp(), inventory=bundle['inventory'])
            _write_metadata(stage / 'record.json', record)
            _require(self._record(revision, directory=stage) == record,
                     'HASH', 'Staged revision differs from its prepared record.')
            staged_files = _scan(stage / 'native', self.limits)
            _require(set(staged_files) == set(bundle['inventory']), 'HASH', 'Native adapter left uninventoried files in its snapshot.')
            current_bytes = sum(_scan(self.root, self.limits).values())
            _require(current_bytes <= self.limits['max_bytes'], 'LIMIT', 'New revision exceeds total repository byte budget.')
            next_head = dict(schema_version=FORMAT, revision=revision, sha256=record['snapshot_sha256'],
                             undo=undo, redo=redo)
            _sync_directory_tree(stage)
            _sync_directory(stage.parent)
            pending.update(status='verified', new_head=next_head)
            _write_metadata(journal, pending)
            _rename_no_replace(stage, safe_path(self.root, f'revisions/{revision:08d}'))
            _sync_directory(safe_path(self.root, 'revisions'))
            pending['status'] = 'published'; _write_metadata(journal, pending)
            installed_record = self._record(revision)
            _require(installed_record == record, 'HASH', 'Installed revision differs from its verified transaction.')
            self._bundle(installed_record)
            _write_metadata(safe_path(self.root, 'HEAD.json'), next_head)
            journal.unlink()
            _sync_directory(journal.parent)
        except Exception:
            # A verified/published revision is retained for explicit recovery.
            # An uncommitted private build can be removed without losing native inputs.
            if pending['status'] == 'building':
                journal.unlink(missing_ok=True)
            raise
        finally:
            if pending['status'] == 'building' and stage.exists(): shutil.rmtree(stage)
        snapshot = self.snapshot()
        if schedule_autosave:
            with self._autosave_lock:
                self._pending_autosave = (time.monotonic(), (snapshot['revision'], snapshot['sha256']))
        return snapshot

    def apply(self, expected_revision, commands, transaction_id, *, expected_sha256=None, label='Edit scene',
              schedule_autosave=False):
        _check_id(transaction_id)
        request = dict(action='apply', commands=commands, label=label)
        _require(isinstance(label, str) and 1 <= len(label) <= 256 and len(_bytes(request)) <= MAX_COMMAND_BYTES,
                 'COMMAND', 'Command batch exceeds limits.')
        with job_lock(self.root):
            head = self._head(verify_history=True)
            previous = self._existing_transaction(head, transaction_id, _digest(request))
            if previous is not None: return previous
            self._check_expected(head, expected_revision, expected_sha256)
            record = self._record(head['revision'])
            source = self._native_root(head['revision'])
            self._bundle(record)
            def build(destination):
                try:
                    from scene3d_graph import SceneGraph
                    from scene3d_prefabs import PrefabLibrary
                    from scene3d_edit import apply_commands
                    from scene3d_snapshot import snapshot_project
                except ImportError as exc:
                    raise RigError('EDITOR3D_DEPENDENCY', 'Install the native Scene3D edit owner.', stage='editor3d-repository') from exc
                bundle = load_project(source)
                graph = SceneGraph.from_bundle(bundle, record['scene_path'], source_root=source)
                library = PrefabLibrary.from_files(source, bundle)
                batch = apply_commands(graph, library, commands)
                result = snapshot_project(source, destination, graph, scene_path=record['scene_path'],
                                          derived_documents=batch['derived_documents'], max_bytes=self.limits['max_bytes'],
                                          expected_project_sha256=record['inventory']['project.json']['sha256'])
                return result['active_scene']['path']
            return self._commit(head, transaction_id, request, label, 'apply', build,
                                schedule_autosave=schedule_autosave)

    def _restore(self, expected_revision, transaction_id, action, target=None, expected_sha256=None,
                 *, schedule_autosave=False):
        _check_id(transaction_id)
        request = dict(action=action, target=target)
        with job_lock(self.root):
            head = self._head(verify_history=True)
            previous = self._existing_transaction(head, transaction_id, _digest(request))
            if previous is not None: return previous
            self._check_expected(head, expected_revision, expected_sha256)
            if action in ('undo', 'redo'):
                target, _, _ = _next_history(head, action, self.limits['max_history'])
            else:
                _require(type(target) is int and 0 <= target <= head['revision'], 'REVISION', 'Choose a retained revision.')
            original = self._record(target)
            source = self._native_root(target)
            self._bundle(original)
            def build(destination):
                _copy_closure(source, destination, self.limits)
                return original['scene_path']
            return self._commit(head, transaction_id, request, action.capitalize(), action, build,
                                schedule_autosave=schedule_autosave, expected_inventory=original['inventory'])

    def undo(self, expected_revision, transaction_id, *, expected_sha256=None, schedule_autosave=False):
        return self._restore(expected_revision, transaction_id, 'undo', expected_sha256=expected_sha256,
                             schedule_autosave=schedule_autosave)

    def redo(self, expected_revision, transaction_id, *, expected_sha256=None, schedule_autosave=False):
        return self._restore(expected_revision, transaction_id, 'redo', expected_sha256=expected_sha256,
                             schedule_autosave=schedule_autosave)

    def restore_revision(self, revision, expected_revision, transaction_id, *, expected_sha256=None):
        return self._restore(expected_revision, transaction_id, 'restore', revision, expected_sha256)

    def history(self):
        head = self._head()
        return [{key: value for key, value in self._record(n).items() if key != 'inventory'}
                for n in range(head['revision'] + 1)]

    def _journal(self, path):
        item = _read_json(path)
        fields = {'schema_version','transaction_id','request_sha256','expected_revision','expected_sha256',
                  'revision','status','new_head','stage'}
        _require(set(item) == fields and type(item['schema_version']) is int and item['schema_version'] == FORMAT and
                 item['status'] in ('building','verified','published') and type(item['expected_revision']) is int and
                 type(item['revision']) is int and item['revision'] == item['expected_revision'] + 1 and
                 0 < item['revision'] < self.limits['max_revisions'], 'RECOVERY', 'Malformed transaction journal.')
        _check_id(item['transaction_id'])
        _require(path.name == hashlib.sha256(item['transaction_id'].encode()).hexdigest()+'.json',
                 'RECOVERY', 'Journal identity differs from its path.')
        for key in ('request_sha256','expected_sha256'):
            _require(isinstance(item[key],str) and re.fullmatch('[0-9a-f]{64}',item[key]), 'RECOVERY', 'Invalid journal digest.')
        if item['stage'] is not None:
            _require(isinstance(item['stage'],str) and re.fullmatch(r'\.editor-revision-[a-zA-Z0-9_-]+',item['stage']),
                     'RECOVERY', 'Invalid private stage path.')
        if item['status'] == 'building':
            _require(item['new_head'] is None, 'RECOVERY', 'A building transaction cannot have a new HEAD.')
        else:
            value=item['new_head']
            _require(isinstance(value,dict) and set(value)=={'schema_version','revision','sha256','undo','redo'} and
                     type(value['schema_version']) is int and value['schema_version']==FORMAT and
                     type(value['revision']) is int and value['revision']==item['revision'] and
                     isinstance(value['sha256'],str) and re.fullmatch('[0-9a-f]{64}',value['sha256']) and
                     all(isinstance(value[k],list) and len(value[k])<=self.limits['max_history'] and
                         all(type(n) is int and 0<=n<item['revision'] for n in value[k]) for k in ('undo','redo')),
                     'RECOVERY', 'Invalid journal head.')
        return item

    def recovery(self):
        """Report interrupted attempts without applying or discarding evidence."""
        head = self._head(); result=[]
        for path in sorted(safe_path(self.root,'journal').glob('*.json')):
            item=self._journal(path)
            revision_path=safe_path(self.root,f"revisions/{item['revision']:08d}/record.json")
            stage=(safe_path(self.root,'revisions/'+item['stage']) if item['stage'] else None)
            item['committed']=(head==item['new_head'])
            item['can_recover']=(item['status'] in ('verified','published') and
                (item['committed'] or item['expected_revision']==head['revision'] and item['expected_sha256']==head['sha256']) and
                (revision_path.is_file() or stage is not None and (stage/'record.json').is_file()))
            if item['can_recover']:
                try:
                    self._verify_recovery_revision(item, revision_path.parent if revision_path.is_file() else stage)
                except RigError:
                    item['can_recover'] = False
            result.append(item)
        return result

    def _verify_recovery_revision(self, item, directory):
        _require(directory.is_dir() and not getattr(directory, 'is_junction', lambda: False)(),
                 'RECOVERY', 'Recovery revision must be an ordinary directory.')
        record = self._record(item['revision'], directory=directory)
        _require(type(record['parent']) is int and record['parent'] == item['expected_revision'] and
                 record['transaction_id'] == item['transaction_id'] and record['request_sha256'] == item['request_sha256'],
                 'RECOVERY', 'Journal transaction or parent differs from the immutable revision.')
        parent = self._record(record['parent'])
        _require(parent['snapshot_sha256'] == item['expected_sha256'],
                 'HASH', 'Journal expected digest differs from the immutable parent revision.')
        _require(record['snapshot_sha256'] == item['new_head']['sha256'],
                 'HASH', 'Journal differs from verified revision.')
        _require(record['action'] in ('apply', 'undo', 'redo', 'restore', 'native-adapter') and
                 isinstance(record['label'], str) and 0 < len(record['label']) <= 256 and
                 isinstance(record['created_at'], str) and
                 re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', record['created_at']),
                 'REVISION', 'Invalid immutable transaction metadata.')
        prior = (self._record(i) for i in range(record['revision']))
        expected = _replay_history(chain(prior, (record,)), self.limits['max_history'])
        _require(expected == item['new_head'], 'HISTORY', 'Journal history differs from its revision actions.')
        files = _scan(directory, self.limits)
        expected_files = {'record.json', *('native/' + name for name in record['inventory'])}
        _require(set(files) == expected_files, 'HASH', 'Recovery revision contains files outside its immutable closure.')
        bundle = self._bundle(record, root=safe_path(directory, 'native'))
        _require(record['project_sha256'] == bundle['inventory']['project.json']['sha256'],
                 'HASH', 'Revision project digest differs from the verified native project.')
        scene = bundle['documents'].get(record['scene_path'])
        _require(scene is not None and scene['kind'] == 'scene3d-scene',
                 'SCENE', 'Revision scene is absent from the verified native project.')
        return record

    def recover(self, transaction_id, expected_revision, *, expected_sha256=None):
        _check_id(transaction_id)
        with job_lock(self.root):
            head = self._head(verify_history=True)
            self._check_expected(head, expected_revision, expected_sha256)
            journal_name = hashlib.sha256(transaction_id.encode()).hexdigest() + '.json'
            path = safe_path(self.root, 'journal/' + journal_name, exists=True)
            item = self._journal(path)
            destination = safe_path(self.root, f"revisions/{item['revision']:08d}")
            verified = item['status'] in ('verified', 'published')
            if verified and head == item['new_head']:
                self._verify_recovery_revision(item, destination)
                _sync_directory_tree(destination)
                _sync_directory(destination.parent)
                _sync_directory(self.root)
                path.unlink()
                _sync_directory(path.parent)
                return self.snapshot(item['revision'])
            _require(verified and item['expected_revision'] == head['revision'] and item['expected_sha256'] == head['sha256'],
                     'STALE', 'Interrupted transaction does not match current HEAD.')
            staged_record = None
            if not destination.exists():
                _require(item['stage'] is not None, 'RECOVERY', 'Verified private stage is missing.')
                stage = safe_path(self.root, 'revisions/' + item['stage'])
                _require(stage.is_dir(), 'RECOVERY', 'Verified private stage is missing.')
                staged_record = self._verify_recovery_revision(item, stage)
                _sync_directory_tree(stage)
                _rename_no_replace(stage, destination)
                _sync_directory(destination.parent)
            record = self._verify_recovery_revision(item, destination)
            _require(staged_record is None or record == staged_record,
                     'HASH', 'Installed revision differs from its verified stage.')
            if staged_record is None:
                _sync_directory_tree(destination)
                _sync_directory(destination.parent)
            _write_metadata(safe_path(self.root, 'HEAD.json'), item['new_head'])
            self._head()
            path.unlink()
            _sync_directory(path.parent)
        return self.snapshot(item['revision'])

    def discard_incomplete(self, transaction_id, expected_revision, *, expected_sha256=None):
        """Roll back only an unverified private attempt, retaining its journal."""
        _check_id(transaction_id)
        with job_lock(self.root):
            head=self._head(verify_history=True);self._check_expected(head,expected_revision,expected_sha256)
            path=safe_path(self.root,'journal/'+hashlib.sha256(transaction_id.encode()).hexdigest()+'.json',exists=True)
            item=self._journal(path)
            _require(item['status']=='building' and item['expected_revision']==head['revision'] and
                     item['expected_sha256']==head['sha256'], 'RECOVERY','Only an unverified current attempt can be discarded.')
            if item['stage'] is not None:
                stage=safe_path(self.root,'revisions/'+item['stage'])
                if stage.exists():
                    # A crashed builder may exceed snapshot quotas before admission.
                    # Validate the private tree shape and entry bound before discarding it.
                    _scan(stage,self.limits,bound_content=False)
                    shutil.rmtree(stage)
                    _sync_directory(stage.parent)
            failure_root=safe_path(self.root,'failed');failure_root.mkdir(exist_ok=True)
            _sync_directory(self.root)
            destination=safe_path(failure_root, f'{path.stem}-{secrets.token_hex(16)}.json')
            _rename_no_replace(path,destination);_sync_directory(failure_root);_sync_directory(path.parent)
        return dict(discarded=True,transaction_id=transaction_id,revision=head['revision'])

    def transact_native(self, expected_revision, transaction_id, request, builder, *, expected_sha256=None, label='Native edit'):
        """Trusted in-process extension for an existing native subsystem adapter.

        ``builder(source_root, destination, scene_path)`` must publish a complete
        new native snapshot, flush its payload files, and return its active scene
        path. Callables are never deserialized or accepted by HTTP; route handlers
        select fixed adapters.
        """
        _check_id(transaction_id)
        request = dict(action='native-adapter', label=label, parameters=request)
        _require(callable(builder) and isinstance(label, str) and 0 < len(label) <= 256 and
                 len(_bytes(request)) <= MAX_COMMAND_BYTES, 'COMMAND', 'Invalid native adapter transaction.')
        with job_lock(self.root):
            head = self._head(verify_history=True)
            previous = self._existing_transaction(head, transaction_id, _digest(request))
            if previous is not None: return previous
            self._check_expected(head, expected_revision, expected_sha256)
            record = self._record(head['revision']); self._bundle(record)
            return self._commit(head, transaction_id, request, label, 'native-adapter',
                lambda destination: builder(self._native_root(head['revision']), destination, record['scene_path']))

    def begin_drag(self, expected_revision, *, expected_sha256=None):
        """Remember a saved base. Preview state remains local and nonpersistent."""
        _require(self._drag is None, 'DRAG', 'Finish or cancel the current drag first.')
        head = self._head(); self._check_expected(head, expected_revision, expected_sha256)
        self._drag = dict(revision=head['revision'], sha256=head['sha256'], commands=None)
        return self.snapshot()

    def preview_drag(self, commands):
        _require(self._drag is not None, 'DRAG', 'Begin a drag before preview.')
        _require(len(_bytes(commands)) <= MAX_COMMAND_BYTES, 'LIMIT', 'Drag commands exceed the bound.')
        record = self._record(self._drag['revision']); bundle = self._bundle(record)
        try:
            from scene3d_graph import SceneGraph
            from scene3d_prefabs import PrefabLibrary
            from scene3d_edit import apply_commands
        except ImportError as exc:
            raise RigError('EDITOR3D_DEPENDENCY', 'Install native Scene3D edit owner.', stage='editor3d-repository') from exc
        graph = SceneGraph.from_bundle(bundle, record['scene_path'], source_root=self._native_root(record['revision']))
        library = PrefabLibrary.from_files(self._native_root(record['revision']), bundle)
        apply_commands(graph, library, commands)
        self._drag['commands'] = deepcopy(commands)
        return dict(revision=record['revision'], sha256=record['snapshot_sha256'], scene=graph.document(), persistent=False)

    def cancel_drag(self):
        _require(self._drag is not None, 'DRAG', 'No active drag.')
        self._drag = None
        return self.snapshot()

    def commit_drag(self, transaction_id):
        _require(self._drag is not None and self._drag['commands'] is not None, 'DRAG', 'No valid drag preview to commit.')
        drag = self._drag
        result = self.apply(drag['revision'], drag['commands'], transaction_id,
                            expected_sha256=drag['sha256'], label='Transform drag')
        self._drag = None
        return result

    def request_autosave(self, *, now=None):
        """Debounce a durable checkpoint; this never commits preview state."""
        now = time.monotonic() if now is None else now
        _require(type(now) in (int,float) and now >= 0, 'TIME', 'Invalid monotonic time.')
        with self._autosave_lock:
            self._pending_autosave = (now, None)

    def _autosave_delay_elapsed(self, pending, now):
        return (now - pending[0] >= self.limits['autosave_delay'] and
                now - self._last_autosave >= self.limits['autosave_interval'])

    def autosave_due(self):
        """Cheap service scheduling hint; autosave rechecks under the writer lock."""
        with self._autosave_lock:
            pending = self._pending_autosave
            return (pending is not None and self._drag is None and
                    self._autosave_delay_elapsed(pending, time.monotonic()))

    def _check_checkpoint_root(self):
        _require(_directory_identity(self.root) == self._root_identity,
                 'PATH', 'Repository root changed during autosave.')

    @contextmanager
    def _checkpoint_directory(self):
        self._check_checkpoint_root()
        if private_stage_supported():
            with pinned_directory(self.root) as descriptor, directory_capability(descriptor) as capability:
                _require(capability.identity == self._root_identity, 'PATH', 'Repository root changed during autosave.')
                yield capability.path
            return
        if private_stage_fd_supported() and os.scandir in os.supports_fd and os.rename in os.supports_dir_fd:
            with pinned_directory_fd(self.root) as descriptor:
                info = os.fstat(descriptor)
                _require((info.st_dev, info.st_ino) == self._root_identity, 'PATH', 'Repository root changed during autosave.')
                yield descriptor
            return
        if windows_private_stage_supported():
            from atomic_windows import pinned_directory as pinned_windows_directory
            try:
                with pinned_windows_directory(self.root) as directory:
                    self._check_checkpoint_root()
                    yield directory.path
            except RigError:
                raise
            except ValueError as exc:
                raise RigError('EDITOR3D_PATH', 'Cannot pin the autosave directory.', stage='editor3d-repository') from exc
            return
        _require(False, 'PATH', 'Anchored autosave publication is unavailable on this platform.')

    def autosave(self, *, now=None, force=False, _writer_guard=None):
        now = time.monotonic() if now is None else now
        _require(type(now) in (int,float) and now >= 0 and type(force) is bool, 'TIME', 'Invalid autosave arguments.')
        with self._autosave_lock:
            pending = self._pending_autosave
            if self._drag is not None or pending is None:
                return dict(saved=False, reason='active-drag' if self._drag is not None else 'no-pending-change')
            if not force and not self._autosave_delay_elapsed(pending, now):
                return dict(saved=False, reason='debounced')
        guard = nullcontext() if _writer_guard is None else _writer_guard
        with guard, job_lock(self.root):
            self._check_checkpoint_root()
            with self._autosave_lock:
                pending = self._pending_autosave
                if self._drag is not None or pending is None:
                    return dict(saved=False, reason='active-drag' if self._drag is not None else 'no-pending-change')
                if not force and not self._autosave_delay_elapsed(pending, now):
                    return dict(saved=False, reason='debounced')
            _, target = pending
            head = self._head()
            if target is not None and target != (head['revision'], head['sha256']):
                with self._autosave_lock:
                    if self._pending_autosave is pending:
                        self._pending_autosave = None
                return dict(saved=False, reason='superseded')
            record = self._record(head['revision'])
            _require(record['snapshot_sha256'] == head['sha256'], 'HASH', 'Captured revision differs from HEAD.')
        # Published revisions are immutable. Verify the captured closure
        # without holding the writer lock across its file reads.
        self._bundle(record)
        with guard, job_lock(self.root), self._checkpoint_directory() as checkpoint_root:
            current = self._head()
            with self._autosave_lock:
                if self._drag is not None:
                    return dict(saved=False, reason='active-drag')
                if self._pending_autosave is not pending:
                    return dict(saved=False, reason='superseded')
                if current['revision'] != head['revision'] or current['sha256'] != head['sha256']:
                    # Manual requests remain pending for the newer head.
                    if target is not None:
                        self._pending_autosave = None
                    return dict(saved=False, reason='superseded')
            checkpoint = dict(schema_version=FORMAT, revision=head['revision'], sha256=head['sha256'], created_at=timestamp())
            name = 'autosave.json'
            if isinstance(checkpoint_root, int):
                payload = _bytes(checkpoint) + b'\n'
                _scan(self.root, self.limits, replacement=(name, len(payload)))
                self._check_checkpoint_root()
                _write_checkpoint_at(checkpoint_root, name, payload)
            else:
                _scan(checkpoint_root, self.limits, replacement=(name, len(_metadata_bytes(checkpoint))))
                self._check_checkpoint_root()
                _write_metadata(safe_path(checkpoint_root, name), checkpoint)
            self._check_checkpoint_root()
            with self._autosave_lock:
                if self._pending_autosave is pending:
                    self._pending_autosave = None
                self._last_autosave = now
        return dict(saved=True, **checkpoint)

    def checkpoint(self, expected_revision, *, expected_sha256=None):
        with job_lock(self.root):
            head = self._head(); self._check_expected(head, expected_revision, expected_sha256)
            self._bundle(self._record(head['revision']))
            record = dict(schema_version=FORMAT, revision=head['revision'], sha256=head['sha256'], created_at=timestamp())
            _scan(self.root, self.limits, replacement=('checkpoint.json', len(_metadata_bytes(record))))
            _write_metadata(safe_path(self.root, 'checkpoint.json'), record)
        return record

    def save_as(self, destination):
        """Copy the complete editor repository, preserving exact native bytes/history."""
        destination = Path(destination).absolute()
        _require(not destination.is_symlink(), 'DESTINATION', 'Save As needs a new destination.')
        destination = destination.resolve()
        _require(not destination.exists() and not destination.is_relative_to(self.root),
                 'DESTINATION', 'Save As needs a new destination outside the repository.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with job_lock(self.root):
            self._verify_all()
            _require(not self.recovery(), 'RECOVERY', 'Resolve interrupted transactions before Save As.')
            with tempfile.TemporaryDirectory(prefix='.editor-copy-', dir=destination.parent) as temporary:
                stage = Path(temporary) / 'repository'; stage.mkdir()
                for name, record in self._all_files().items():
                    target = stage / name; target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('xb') as output:
                        output.write(_regular_bytes(self.root, name, record, self.limits['max_file_bytes']))
                        output.flush(); os.fsync(output.fileno())
                EditorRepository(stage)._verify_all()
                _sync_directory_tree(stage)
                _rename_no_replace(stage, destination)
                _sync_directory(destination.parent)
        return EditorRepository(destination)

    def _all_files(self):
        files={}
        for name,size in _scan(self.root,self.limits).items():
            path=safe_path(self.root,name,exists=True)
            with path.open('rb') as stream:
                raw=stream.read(self.limits['max_file_bytes']+1)
            _require(len(raw)==size,'HASH','Repository changed while inventorying.')
            files[name]=dict(bytes=size,sha256=hashlib.sha256(raw).hexdigest())
        return files

    def _verify_all(self):
        head = self._head()
        def records():
            for revision in range(head['revision'] + 1):
                record = self._record(revision)
                self._bundle(record)
                yield record
        expected = _replay_history(records(), self.limits['max_history'])
        _require(expected == head, 'HISTORY', 'HEAD history differs from its revision actions.')
        return head

    def archive(self, destination):
        """Create an exact hashed ZIP with bounded verified extraction semantics."""
        destination = Path(destination).absolute()
        _require(not destination.exists() and not destination.is_symlink() and
                 '..' not in destination.parts and not destination.resolve().is_relative_to(self.root),
                 'DESTINATION', 'Archive needs a new file outside the repository.')
        if os.name == 'nt':
            from atomic_windows import validate_destination
            validate_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent_identity = _directory_identity(destination.parent)
        with job_lock(self.root):
            head = self._verify_all()
            _require(not self.recovery(), 'RECOVERY', 'Resolve interrupted transactions before archive export.')
            files = self._all_files()
            receipt = dict(schema_version=FORMAT, kind='editor3d-archive', files=files, head=head)
            # Keep the writer and verifier on the same open temporary file.
            # No published pathname can redirect either one to replacement bytes.
            with tempfile.TemporaryFile(mode='w+b') as stream:
                with zipfile.ZipFile(stream, 'w') as archive:
                    for name, record in files.items():
                        payload = _regular_bytes(self.root, name, record, self.limits['max_file_bytes'])
                        archive.writestr(name, payload, compress_type=_archive_compression(payload))
                    metadata = _bytes(receipt)
                    archive.writestr('archive-receipt.json', metadata, compress_type=_archive_compression(metadata))
                stream.flush(); os.fsync(stream.fileno())
                size = os.fstat(stream.fileno()).st_size
                maximum = self.limits['max_bytes'] + MAX_METADATA_BYTES + self.limits['max_files'] * 4096
                _require(size <= maximum, 'LIMIT', 'Encoded archive exceeds the export byte budget.')
                stream.seek(0)
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                stream.seek(0)
                _require(self.verify_archive(stream) == receipt, 'HASH', 'Verified archive differs from this export.')
                try:
                    publication = publish_verified_file(stream.fileno(), destination,
                        expected_sha256=digest, expected_size=size, max_bytes=maximum,
                        expected_parent_identity=parent_identity)
                    cleanup_pending = False
                except VerifiedFileCleanupError as exc:
                    publication = exc.receipt
                    cleanup_pending = True
        result = dict(archive=str(destination), sha256=publication['sha256'], revision=head['revision'])
        if cleanup_pending:
            result['cleanup_pending'] = True
        return result

    @staticmethod
    def verify_archive(path):
        try:
            snapshot = _archive_snapshot(path)
            # Reuse the native ZIP authority before ZipFile constructs ZipInfo objects.
            from rig_load_limits import _zip_directory
            class DirectoryBudget:
                limits = {'archive_files': DEFAULT_LIMITS['max_files'] + 1}
                def add(self, amount, asset):
                    _require(amount <= len(snapshot) * 2 + self.limits['archive_files'] * 1024,
                             'LIMIT', 'Archive directory exceeds its budget.')
            _zip_directory(snapshot, DirectoryBudget())
            with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
                entries = archive.infolist()
                _require(0 < len(entries) <= DEFAULT_LIMITS['max_files'] + 1 and
                         sum(i.file_size for i in entries) <= DEFAULT_LIMITS['max_bytes'], 'LIMIT', 'Archive exceeds resource limits.')
                names = set(); aliases = set()
                for entry in entries:
                    name = portable_name(entry.filename)
                    alias = _portable_alias(name)
                    mode = entry.external_attr >> 16
                    _require(name == entry.orig_filename and name not in names and alias not in aliases and
                             not entry.is_dir() and (mode & 0o170000) in (0,0o100000) and
                             entry.file_size <= DEFAULT_LIMITS['max_file_bytes'] and
                             (entry.file_size == 0 or entry.compress_size > 0) and
                             entry.file_size <= max(entry.compress_size, 1)*ARCHIVE_MAX_RATIO,
                             'ARCHIVE', 'Unsafe, duplicate or excessively compressed archive member.')
                    names.add(name); aliases.add(alias)
                _require('archive-receipt.json' in names and archive.getinfo('archive-receipt.json').file_size <= MAX_METADATA_BYTES,
                         'ARCHIVE', 'Missing or excessive archive receipt.')
                receipt = decode_json(_archive_member(archive, archive.getinfo('archive-receipt.json'), snapshot))
                _require(isinstance(receipt,dict) and set(receipt) == {'schema_version','kind','files','head'} and
                         receipt['schema_version'] == FORMAT and receipt['kind'] == 'editor3d-archive' and
                         isinstance(receipt['files'],dict) and names == set(receipt['files']) | {'archive-receipt.json'},
                         'ARCHIVE', 'Archive does not match its receipt.')
                for name, record in receipt['files'].items():
                    _require(isinstance(record,dict) and set(record) == {'bytes','sha256'} and
                             type(record['bytes']) is int and record['bytes'] == archive.getinfo(name).file_size,
                             'ARCHIVE', 'Malformed archived file record.')
                    raw = _archive_member(archive, archive.getinfo(name), snapshot)
                    _require(hashlib.sha256(raw).hexdigest() == record['sha256'], 'HASH', 'Archive file hash differs.')
                def metadata(name):
                    info = archive.getinfo(name)
                    _require(info.file_size <= MAX_METADATA_BYTES, 'LIMIT', 'Archived repository metadata exceeds its bound.')
                    value = decode_json(_archive_member(archive, info, snapshot))
                    _require(isinstance(value, dict), 'ARCHIVE', 'Archived repository metadata must be an object.')
                    return value
                limits = _validated_limits(metadata('repository.json')['limits'])
                head = metadata('HEAD.json')
                last = head.get('revision')
                _require(type(last) is int and 0 <= last < limits['max_revisions'],
                         'HISTORY', 'Invalid archived HEAD revision.')
                revision_files = set()
                def records():
                    for revision in range(last + 1):
                        prefix = f'revisions/{revision:08d}/'
                        record_path = prefix + 'record.json'
                        record = _validate_revision_record(metadata(record_path), revision, limits)
                        inventory = record['inventory']
                        _require('project.json' in inventory
                                 and inventory['project.json']['sha256'] == record['project_sha256'],
                                 'HASH', 'Archived revision project differs from its inventory.')
                        revision_files.add(record_path)
                        for name, expected in inventory.items():
                            full_name = prefix + 'native/' + name
                            _require(receipt['files'].get(full_name) == expected,
                                     'HASH', 'Archived revision file differs from its immutable inventory.')
                            revision_files.add(full_name)
                        yield record
                expected = _replay_history(records(), limits['max_history'])
                _require(revision_files == {name for name in receipt['files'] if name.startswith('revisions/')},
                         'HASH', 'Archive includes files outside its committed revision inventory.')
                _require(_bytes(head) == _bytes(expected) == _bytes(receipt['head']),
                         'HISTORY', 'Archive HEAD history differs from its revision actions.')
                return receipt
        except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, NotImplementedError, EOFError, zlib.error, ValueError) as exc:
            raise RigError('EDITOR3D_ARCHIVE', str(exc), stage='editor3d-repository') from exc

    @classmethod
    def restore_archive(cls, archive_path, destination):
        snapshot = _archive_snapshot(archive_path)
        receipt = cls.verify_archive(io.BytesIO(snapshot))
        destination = Path(destination).absolute()
        _require(not destination.exists() and not destination.is_symlink(), 'DESTINATION', 'Archive restoration needs a new destination.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = destination.parent.resolve(strict=True) / destination.name
        with tempfile.TemporaryDirectory(prefix='.editor-restore-', dir=destination.parent) as temporary:
            stage = Path(temporary) / 'repository'; stage.mkdir()
            with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
                for name, record in receipt['files'].items():
                    target = stage / portable_name(name); target.parent.mkdir(parents=True, exist_ok=True)
                    raw = _archive_member(archive, archive.getinfo(name), snapshot)
                    _require(len(raw) == record['bytes'] and hashlib.sha256(raw).hexdigest() == record['sha256'],
                             'HASH', 'Archive changed after verification.')
                    with target.open('xb') as output:
                        output.write(raw)
                        output.flush(); os.fsync(output.fileno())
            restored = cls(stage)
            _require(restored._verify_all() == receipt['head'], 'HASH', 'Archive HEAD differs from receipt.')
            _require(not restored.recovery(), 'RECOVERY', 'Archive contains interrupted transactions.')
            _sync_directory_tree(stage)
            _rename_no_replace(stage, destination)
            _sync_directory(destination.parent)
        return cls(destination)
