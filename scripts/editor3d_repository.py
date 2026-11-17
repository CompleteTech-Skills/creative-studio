"""Durable editor revisions around the authoritative native Scene3D owner.

Every edit is evaluated in an isolated native snapshot. Only a verified immutable
revision can replace HEAD. Session previews never write persistent scene state.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import zlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import time
import zipfile

from atomic_publish import _rename_no_replace, publish_verified_file, VerifiedFileCleanupError
from rig_errors import RigError, atomic_json, decode_json, finite_json, job_lock, portable_name, safe_path, timestamp, open_payload
from scene3d_contracts import load_project

FORMAT = 1
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_COMMAND_BYTES = 1024 * 1024
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


def _write_metadata(path, value):
    _require(not path.is_symlink(), 'PATH', 'Metadata cannot be a symbolic link.')
    atomic_json(path, value)
    _sync_directory(path.parent)


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


def _scan(root, limits):
    """Bound traversal itself, rejecting links before descending."""
    import stat
    import unicodedata
    pending=[(Path(root),'')]; files={}; aliases=set(); entries=0; total=0
    while pending:
        directory,prefix=pending.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries+=1
                _require(entries <= limits['max_files']*4, 'LIMIT', 'Repository directory-entry budget exceeded.')
                name=portable_name(prefix+entry.name)
                alias=unicodedata.normalize('NFC',name).casefold()
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
                    _require(len(files)<limits['max_files'] and info.st_size<=limits['max_file_bytes'] and
                             total<=limits['max_bytes'], 'LIMIT', 'Repository exceeds file or byte limits.')
                    files[name]=info.st_size
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


class EditorRepository:
    """Repository of exact native project revisions, with optimistic concurrency.

    ``apply`` accepts the native Scene3D command batch (not executable callbacks).
    The complete native owner must be installed; absence fails explicitly.
    """
    def __init__(self, root):
        given = Path(root).absolute()
        _require(not given.is_symlink() and given.is_dir(), 'FILE', 'Open an existing editor repository.')
        self.root = given.resolve(strict=True)
        self._metadata = _read_json(safe_path(self.root, 'repository.json', exists=True))
        _require(set(self._metadata) == {'schema_version', 'kind', 'id', 'created_at', 'limits'} and
                 type(self._metadata['schema_version']) is int and self._metadata['schema_version'] == FORMAT and
                 self._metadata['kind'] == 'editor3d-repository', 'VERSION', 'Unsupported editor repository.')
        _check_id(self._metadata['id'])
        self.limits = _validated_limits(self._metadata['limits'])
        self._drag = None
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
            _rename_no_replace(stage, destination)
            _sync_directory(destination.parent)
        return cls(destination)

    def _head(self):
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
        return value

    def _record(self, revision):
        _require(type(revision) is int and 0 <= revision < self.limits['max_revisions'], 'REVISION', 'Invalid revision number.')
        value = _read_json(safe_path(self.root, f'revisions/{revision:08d}/record.json', exists=True))
        required = {'schema_version','revision','parent','snapshot_sha256','project_sha256','scene_path',
                    'transaction_id','request_sha256','action','label','created_at','inventory'}
        _require(set(value) == required and value['schema_version'] == FORMAT and value['revision'] == revision,
                 'REVISION', 'Invalid immutable revision record.')
        _require(isinstance(value['inventory'], dict) and 0 < len(value['inventory']) <= self.limits['max_files'],
                 'REVISION', 'Invalid snapshot inventory.')
        for name, record in value['inventory'].items():
            portable_name(name)
            _require(isinstance(record, dict) and set(record) == {'bytes','sha256'} and type(record['bytes']) is int and
                     0 <= record['bytes'] <= self.limits['max_file_bytes'] and isinstance(record['sha256'], str) and
                     re.fullmatch('[0-9a-f]{64}', record['sha256']), 'REVISION', 'Invalid immutable file record.')
        _require(_digest(value['inventory']) == value['snapshot_sha256'], 'HASH', 'Revision inventory digest differs.')
        _require(value['scene_path'] in value['inventory'], 'SCENE', 'Revision scene is absent from its inventory.')
        _check_id(value['transaction_id'])
        return value

    def _native_root(self, revision):
        path = safe_path(self.root, f'revisions/{revision:08d}/native')
        _require(path.is_dir(), 'REVISION', 'Native snapshot is missing.')
        return path

    def _bundle(self, record):
        root = self._native_root(record['revision'])
        # Hash all retained closure bytes, including source/provenance inputs.
        for name, expected in record['inventory'].items():
            _regular_bytes(root, name, expected, self.limits['max_file_bytes'])
        bundle = load_project(root)
        _require(bundle['inventory'] == record['inventory'], 'HASH', 'Native closure differs from saved inventory.')
        return bundle

    def snapshot(self, revision=None):
        head = self._head()
        record = self._record(head['revision'] if revision is None else revision)
        bundle = self._bundle(record)
        return dict(revision=record['revision'], sha256=record['snapshot_sha256'],
                    project=bundle['project'], documents=bundle['documents'], inventory=bundle['inventory'],
                    scene=deepcopy(bundle['documents'][record['scene_path']]), scene_path=record['scene_path'],
                    undo_available=bool(head['undo']), redo_available=bool(head['redo']))

    def native_snapshot(self, revision, expected_sha256):
        """Verified immutable source reference for native render/import owners.

        Consumers must copy/recheck input bytes at their admission boundary.
        This returns a native filesystem location to trusted Python callers only.
        """
        record = self._record(revision)
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

    def _commit(self, head, transaction_id, request, label, action, build, undo, redo):
        _require(not self.recovery(), 'RECOVERY', 'Resolve interrupted transactions before another edit.')
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
        stage = Path(tempfile.mkdtemp(prefix='.editor-revision-', dir=safe_path(self.root, 'revisions')))
        pending['stage'] = stage.name; _write_metadata(journal, pending)
        try:
            scene_path = build(stage / 'native')
            bundle = load_project(stage / 'native')
            _require(scene_path in bundle['documents'] and bundle['documents'][scene_path]['kind'] == 'scene3d-scene',
                     'SCENE', 'Edited snapshot lacks its active native scene.')
            _require(bundle['bytes'] <= self.limits['max_bytes'] and len(bundle['inventory']) <= self.limits['max_files'],
                     'LIMIT', 'New snapshot exceeds resource budget.')
            record = dict(schema_version=FORMAT, revision=revision, parent=head['revision'],
                          snapshot_sha256=_digest(bundle['inventory']), project_sha256=bundle['inventory']['project.json']['sha256'],
                          scene_path=scene_path, transaction_id=transaction_id, request_sha256=transaction_hash,
                          action=action, label=label, created_at=timestamp(), inventory=bundle['inventory'])
            _write_metadata(stage / 'record.json', record)
            staged_files = _scan(stage / 'native', self.limits)
            _require(set(staged_files) == set(bundle['inventory']), 'HASH', 'Native adapter left uninventoried files in its snapshot.')
            current_bytes = sum(_scan(self.root, self.limits).values())
            _require(current_bytes <= self.limits['max_bytes'], 'LIMIT', 'New revision exceeds total repository byte budget.')
            next_head = dict(schema_version=FORMAT, revision=revision, sha256=record['snapshot_sha256'],
                             undo=undo[-self.limits['max_history']:], redo=redo[-self.limits['max_history']:])
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
        except Exception:
            # A verified/published revision is retained for explicit recovery.
            # An uncommitted private build can be removed without losing native inputs.
            if pending['status'] == 'building':
                journal.unlink(missing_ok=True)
            raise
        finally:
            if pending['status'] == 'building' and stage.exists(): shutil.rmtree(stage)
        return self.snapshot()

    def apply(self, expected_revision, commands, transaction_id, *, expected_sha256=None, label='Edit scene'):
        _check_id(transaction_id)
        request = dict(action='apply', commands=commands, label=label)
        _require(isinstance(label, str) and 1 <= len(label) <= 256 and len(_bytes(request)) <= MAX_COMMAND_BYTES,
                 'COMMAND', 'Command batch exceeds limits.')
        with job_lock(self.root):
            head = self._head()
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
            return self._commit(head, transaction_id, request, label, 'apply', build, head['undo'] + [head['revision']], [])

    def _restore(self, expected_revision, transaction_id, action, target=None, expected_sha256=None):
        _check_id(transaction_id)
        request = dict(action=action, target=target)
        with job_lock(self.root):
            head = self._head()
            previous = self._existing_transaction(head, transaction_id, _digest(request))
            if previous is not None: return previous
            self._check_expected(head, expected_revision, expected_sha256)
            undo, redo = list(head['undo']), list(head['redo'])
            if action == 'undo':
                _require(bool(undo), 'HISTORY', 'No retained undo entry.'); target = undo.pop(); redo.append(head['revision'])
            elif action == 'redo':
                _require(bool(redo), 'HISTORY', 'No retained redo entry.'); target = redo.pop(); undo.append(head['revision'])
            else:
                _require(type(target) is int and 0 <= target <= head['revision'], 'REVISION', 'Choose a retained revision.')
                undo.append(head['revision']); redo = []
            original = self._record(target)
            source = self._native_root(target)
            self._bundle(original)
            def build(destination):
                _copy_closure(source, destination, self.limits)
                return original['scene_path']
            return self._commit(head, transaction_id, request, action.capitalize(), action, build, undo, redo)

    def undo(self, expected_revision, transaction_id, *, expected_sha256=None):
        return self._restore(expected_revision, transaction_id, 'undo', expected_sha256=expected_sha256)

    def redo(self, expected_revision, transaction_id, *, expected_sha256=None):
        return self._restore(expected_revision, transaction_id, 'redo', expected_sha256=expected_sha256)

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
        _require(isinstance(item,dict) and set(item) == fields and item['schema_version'] == FORMAT and
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
        if item['status'] != 'building':
            value=item['new_head']
            _require(isinstance(value,dict) and set(value)=={'schema_version','revision','sha256','undo','redo'} and
                     value['schema_version']==FORMAT and value['revision']==item['revision'] and
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
            result.append(item)
        return result

    def recover(self, transaction_id, expected_revision, *, expected_sha256=None):
        _check_id(transaction_id)
        with job_lock(self.root):
            head=self._head(); self._check_expected(head,expected_revision,expected_sha256)
            path=safe_path(self.root,'journal/'+hashlib.sha256(transaction_id.encode()).hexdigest()+'.json',exists=True)
            item=self._journal(path)
            if head==item['new_head']:
                self._bundle(self._record(head['revision']));path.unlink();_sync_directory(path.parent)
                return self.snapshot()
            _require(item['status'] in ('verified','published') and
                     item['expected_revision']==head['revision'] and item['expected_sha256']==head['sha256'],
                     'STALE','Interrupted transaction does not match current HEAD.')
            destination=safe_path(self.root,f"revisions/{item['revision']:08d}")
            if not destination.exists():
                _require(item['stage'] is not None,'RECOVERY','Verified private stage is missing.')
                stage=safe_path(self.root,'revisions/'+item['stage'])
                _require(stage.is_dir(),'RECOVERY','Verified private stage is missing.')
                staged_record=_read_json(safe_path(stage,'record.json',exists=True))
                bundle=load_project(safe_path(stage,'native'))
                _require(staged_record['inventory']==bundle['inventory'] and
                         _digest(bundle['inventory'])==item['new_head']['sha256'], 'HASH','Staged closure changed.')
                _rename_no_replace(stage,destination);_sync_directory(destination.parent)
            record=self._record(item['revision']);self._bundle(record)
            _require(item['new_head']['sha256']==record['snapshot_sha256'], 'HASH','Journal differs from verified revision.')
            _write_metadata(safe_path(self.root,'HEAD.json'),item['new_head'])
            self._head();path.unlink();_sync_directory(path.parent)
        return self.snapshot()

    def discard_incomplete(self, transaction_id, expected_revision, *, expected_sha256=None):
        """Roll back only an unverified private attempt, retaining its journal."""
        _check_id(transaction_id)
        with job_lock(self.root):
            head=self._head();self._check_expected(head,expected_revision,expected_sha256)
            path=safe_path(self.root,'journal/'+hashlib.sha256(transaction_id.encode()).hexdigest()+'.json',exists=True)
            item=self._journal(path)
            _require(item['status']=='building' and item['expected_revision']==head['revision'] and
                     item['expected_sha256']==head['sha256'], 'RECOVERY','Only an unverified current attempt can be discarded.')
            if item['stage'] is not None:
                stage=safe_path(self.root,'revisions/'+item['stage'])
                if stage.exists():
                    _scan(stage,self.limits)
                    shutil.rmtree(stage)
            failure_root=safe_path(self.root,'failed');failure_root.mkdir(exist_ok=True)
            destination=safe_path(failure_root,path.name)
            _rename_no_replace(path,destination);_sync_directory(failure_root);_sync_directory(path.parent)
        return dict(discarded=True,transaction_id=transaction_id,revision=head['revision'])

    def transact_native(self, expected_revision, transaction_id, request, builder, *, expected_sha256=None, label='Native edit'):
        """Trusted in-process extension for an existing native subsystem adapter.

        ``builder(source_root, destination, scene_path)`` must publish a complete
        new native snapshot and return its active scene path. Callables are never
        deserialized or accepted by HTTP; route handlers select fixed adapters.
        """
        _check_id(transaction_id)
        request = dict(action='native-adapter', label=label, parameters=request)
        _require(callable(builder) and isinstance(label, str) and 0 < len(label) <= 256 and
                 len(_bytes(request)) <= MAX_COMMAND_BYTES, 'COMMAND', 'Invalid native adapter transaction.')
        with job_lock(self.root):
            head = self._head()
            previous = self._existing_transaction(head, transaction_id, _digest(request))
            if previous is not None: return previous
            self._check_expected(head, expected_revision, expected_sha256)
            record = self._record(head['revision']); self._bundle(record)
            return self._commit(head, transaction_id, request, label, 'native-adapter',
                lambda destination: builder(self._native_root(head['revision']), destination, record['scene_path']),
                head['undo'] + [head['revision']], [])

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
        self._pending_autosave = now

    def autosave(self, *, now=None, force=False):
        now = time.monotonic() if now is None else now
        _require(type(now) in (int,float) and now >= 0 and type(force) is bool, 'TIME', 'Invalid autosave arguments.')
        if self._drag is not None or self._pending_autosave is None:
            return dict(saved=False, reason='active-drag' if self._drag is not None else 'no-pending-change')
        if not force and (now-self._pending_autosave < self.limits['autosave_delay'] or
                          now-self._last_autosave < self.limits['autosave_interval']):
            return dict(saved=False, reason='debounced')
        with job_lock(self.root):
            head = self._head(); self._bundle(self._record(head['revision']))
            checkpoint = dict(schema_version=FORMAT, revision=head['revision'], sha256=head['sha256'], created_at=timestamp())
            _write_metadata(safe_path(self.root, 'autosave.json'), checkpoint)
        self._pending_autosave = None; self._last_autosave = now
        return dict(saved=True, **checkpoint)

    def checkpoint(self, expected_revision, *, expected_sha256=None):
        with job_lock(self.root):
            head = self._head(); self._check_expected(head, expected_revision, expected_sha256)
            self._bundle(self._record(head['revision']))
            record = dict(schema_version=FORMAT, revision=head['revision'], sha256=head['sha256'], created_at=timestamp())
            _write_metadata(safe_path(self.root, 'checkpoint.json'), record)
        return record

    def save_as(self, destination):
        """Copy the complete editor repository, preserving exact native bytes/history."""
        destination = Path(destination).absolute()
        _require(not destination.exists() and not destination.is_symlink(), 'DESTINATION', 'Save As needs a new destination.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = destination.parent.resolve(strict=True) / destination.name
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
                _sync_directory(stage)
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
        for revision in range(head['revision'] + 1): self._bundle(self._record(revision))
        return head

    def archive(self, destination):
        """Create an exact hashed ZIP with bounded verified extraction semantics."""
        destination = Path(destination).absolute()
        _require(not destination.exists() and not destination.is_symlink() and
                 not destination.is_relative_to(self.root), 'DESTINATION', 'Archive needs a new file outside the repository.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with job_lock(self.root):
            head = self._verify_all()
            _require(not self.recovery(), 'RECOVERY', 'Resolve interrupted transactions before archive export.')
            files = self._all_files()
            receipt = dict(schema_version=FORMAT, kind='editor3d-archive', files=files, head=head)
            # Keep the writer and verifier on the same open temporary file.
            # No published pathname can redirect either one to replacement bytes.
            with tempfile.TemporaryFile(mode='w+b') as stream:
                with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for name, record in files.items():
                        archive.writestr(name, _regular_bytes(self.root, name, record, self.limits['max_file_bytes']))
                    archive.writestr('archive-receipt.json', _bytes(receipt))
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
                        expected_sha256=digest, expected_size=size, max_bytes=maximum)
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
                    import unicodedata
                    alias = unicodedata.normalize('NFC', name).casefold()
                    mode = entry.external_attr >> 16
                    _require(name == entry.orig_filename and name not in names and alias not in aliases and
                             not entry.is_dir() and (mode & 0o170000) in (0,0o100000) and
                             entry.file_size <= DEFAULT_LIMITS['max_file_bytes'] and
                             (entry.file_size == 0 or entry.compress_size > 0) and
                             entry.file_size <= max(entry.compress_size, 1)*1000,
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
            _sync_directory(stage)
            _rename_no_replace(stage, destination)
            _sync_directory(destination.parent)
        return cls(destination)
