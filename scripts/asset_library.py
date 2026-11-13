"""Offline library foundation: transactional exact-byte registration and resolution.

A private library directory is the write trust boundary. Untrusted source trees
are read through no-follow file descriptors. Native formats retain their owners;
registration verifies bytes and evidence, never geometry or production approval.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from typing import Protocol

from asset_library_contracts import (MAX_FILE_BYTES, MAX_MANIFEST_BYTES,
    MAX_VERSION_BYTES, canonical_bytes, load_manifest, portable_path, validate)
from rig_errors import RigError, decode_json, object_hash

MAX_LIBRARY_BYTES = 1024 * 1024 * 1024
MAX_VERSIONS = 4096
INDEX_SCHEMA = '''
    CREATE TABLE settings(version INTEGER NOT NULL);
    INSERT INTO settings VALUES(1);
    CREATE TABLE blobs(sha256 TEXT PRIMARY KEY, payload BLOB NOT NULL);
    CREATE TABLE versions(id TEXT NOT NULL, version TEXT NOT NULL,
        sha256 TEXT UNIQUE NOT NULL, manifest BLOB NOT NULL,
        PRIMARY KEY(id,version));
    CREATE TABLE annotations(id TEXT NOT NULL, version TEXT NOT NULL,
        revision INTEGER NOT NULL, document BLOB NOT NULL,
        PRIMARY KEY(id,version));
    CREATE TABLE asset_events(stream TEXT NOT NULL, revision INTEGER NOT NULL,
        document BLOB NOT NULL, PRIMARY KEY(stream,revision));
'''


class AssetResolver(Protocol):
    def resolve(self, reference: dict) -> dict: ...
    def read_file(self, reference: dict, path: str) -> bytes: ...


def _fail(code, message, path=None):
    raise RigError('LIBRARY_' + code, message, asset=str(path) if path else None)


def _bounded_integer(value, minimum, maximum, *, code, label):
    """Normalize a validated numeric copy, preserving original manifest values.

    JSON Schema integer includes finite mathematically whole floating values.
    Bound comparison happens before conversion, including arbitrarily large ints.
    """
    if (type(value) not in (int, float) or value < minimum or value > maximum or
        (type(value) is float and (not math.isfinite(value) or not value.is_integer()))):
        _fail(code, label + ' must be a bounded finite integer; booleans are not integers.')
    return int(value)


def _read_source_bytes(path, limit=MAX_FILE_BYTES):
    """Reject symlink/reparse components, bound allocation, detect changed files."""
    limit = _bounded_integer(limit, 1, MAX_FILE_BYTES, code='LIMIT', label='Source read bound')
    path = Path(path).absolute()
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail('SYMLINK', 'Source paths cannot contain symlinks or reparse points.', component)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    if os.name == 'posix':
        # Walk using retained directory descriptors so a concurrently replaced
        # ancestor cannot redirect an untrusted source read outside that tree.
        parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    else:
        fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        if os.name == 'nt':
            # Check the kernel handle's normalized final location before any
            # bytes are read. This catches a raced junction/ancestor redirect.
            # Canonical long source paths are required on Windows; aliases fail
            # closed rather than guessing whether a redirected file is trusted.
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            operation = kernel.GetFinalPathNameByHandleW
            operation.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            operation.restype = wintypes.DWORD
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            buffer = ctypes.create_unicode_buffer(32768)
            length = operation(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                _fail('PATH', 'Cannot verify the canonical source handle location.', path)
            actual = buffer.value
            if actual.startswith('\\\\?\\UNC\\'):
                actual = '\\\\' + actual[8:]
            elif actual.startswith('\\\\?\\'):
                actual = actual[4:]
            if os.path.normcase(actual) != os.path.normcase(os.path.abspath(path)):
                _fail('PATH', 'Source was redirected or does not use its canonical long path.', path)
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            _fail('PAYLOAD', 'Expected a nonempty bounded regular file.', path)
        payload = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(payload) != before.st_size or len(payload) > limit or
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        _fail('CHANGED', 'Source changed during bounded reading.', path)
    return payload


def read_source_bytes(path, limit=MAX_FILE_BYTES):
    """Read exact bounded source bytes or return a structured library diagnostic."""
    try:
        return _read_source_bytes(path, limit)
    except (OSError, TypeError) as exc:
        raise RigError('LIBRARY_SOURCE', 'Cannot safely read the source file: ' + str(exc),
                       asset=str(path)) from exc


def detect_format(payload):
    """Header recognition, not a geometric/semantic format validator."""
    if payload.startswith(b'glTF'): return 'glb', 'model/gltf-binary'
    if payload.startswith(b'\x89PNG\r\n\x1a\n'): return 'png', 'image/png'
    if payload.startswith(b'\xff\xd8\xff'): return 'jpeg', 'image/jpeg'
    if payload.startswith(b'RIFF') and payload[8:12] == b'WEBP': return 'webp', 'image/webp'
    if payload.lstrip().startswith((b'{', b'[')):
        try:
            value = decode_json(payload)
            if isinstance(value, dict) and isinstance(value.get('asset'), dict) and value['asset'].get('version') == '2.0':
                return 'gltf', 'model/gltf+json'
            return 'json', 'application/json'
        except (RigError, ValueError): pass
    try:
        lines = payload.decode('utf-8').splitlines()
        if any(line.startswith('v ') for line in lines) and any(line.startswith('f ') for line in lines):
            return 'obj', 'model/obj'
    except UnicodeError: pass
    return 'binary', 'application/octet-stream'


def file_record(source, path, role='source', required=True):
    portable_path(path)
    payload = read_source_bytes(source, MAX_FILE_BYTES)
    fmt, mime = detect_format(payload)
    return dict(path=path, role=role, sha256=hashlib.sha256(payload).hexdigest(),
                bytes=len(payload), format=fmt, mime_type=mime, required=required)


class AssetLibrary:
    """Canonical v1 registry; exact references never silently select latest.

    New libraries retain immutable canonical records and a SQLite query index.
    Writers serialize through SQLite; canonical commits precede index commits,
    and explicit recovery reconciles an interrupted split commit. Legacy stores
    retain exact bytes until explicit upgrade. The caller owns the private root.
    """
    def __init__(self, root, *, create=False):
        self.root = Path(root).absolute()
        if create:
            self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or self.root.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail('ROOT', 'Library root must be an owned private directory.')
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            _fail('ROOT', 'Library root must be owned by this user with mode 0700.')
        self._root_identity = (info.st_dev, info.st_ino)
        self.database = self.root / 'library.sqlite3'
        if create:
            fd = os.open(self.database, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
            with self._connect(initial=True) as db:
                db.executescript(INDEX_SCHEMA)
        with self._connect() as db:
            if db.execute('SELECT version FROM settings').fetchall() != [(1,)]:
                _fail('VERSION', 'Unsupported library storage schema.')
            db.execute('CREATE TABLE IF NOT EXISTS asset_events(stream TEXT NOT NULL, revision INTEGER NOT NULL, document BLOB NOT NULL, PRIMARY KEY(stream,revision))')
            durable_flag = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='durability'").fetchone()
            durable_required = bool(durable_flag and db.execute('SELECT canonical FROM durability').fetchone() == (1,))
        from asset_library_storage import Storage
        self.storage = Storage(self, create=True) if create else (
            Storage(self) if (self.root / 'records' / 'READY').exists() else None)
        if create:
            self.storage.ready()
        elif self.storage:
            self.storage.validate_ready()
        if durable_required and not self.storage:
            _fail('DURABILITY', 'Required canonical records are missing; refusing to downgrade to legacy storage.')
        if create:
            with self._connect() as db:
                db.execute('CREATE TABLE durability(canonical INTEGER NOT NULL)')
                db.execute('INSERT INTO durability VALUES(1)')

    @contextmanager
    def _connect(self, initial=False):
        info = self.root.lstat()
        if ((info.st_dev, info.st_ino) != self._root_identity or not stat.S_ISDIR(info.st_mode) or
            self.root.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400):
            _fail('ROOT', 'Library directory was replaced.')
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            _fail('ROOT', 'Library directory no longer has private ownership.')
        info = self.database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail('DATABASE', 'Library database must be a regular unlinked-owned file.')
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            _fail('DATABASE', 'Library database must remain private to its owner.')
        # mode=rw prevents accidental recreation of a removed library database.
        uri = self.database.as_uri() + '?mode=rw'
        db = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=5000')
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            with db: yield db
        except sqlite3.DatabaseError as exc:
            raise RigError('LIBRARY_STORAGE', 'Library database operation failed: ' + str(exc)) from exc
        finally:
            db.close()

    def register(self, manifest, sources):
        """Register all inventoried logical files from explicit source paths.

        Files are always re-read and hashed; a supplied manifest never certifies
        its own payload. Missing optional inventoried files also fail this initial
        exact-byte store (optional dependency resolution is a later policy).
        """
        manifest = load_manifest(canonical_bytes(manifest))
        if not isinstance(sources, dict):
            _fail('INVENTORY', 'Sources must be an explicit logical-path mapping.')
        if set(sources) != {f['path'] for f in manifest['files']}:
            _fail('INVENTORY', 'Source mapping must exactly match the file inventory.')
        payloads = {}
        for f in manifest['files']:
            payload = read_source_bytes(sources[f['path']],
                                        _bounded_integer(f['bytes'], 1, MAX_FILE_BYTES,
                                                         code='LIMIT', label='File byte count'))
            if len(payload) != f['bytes'] or hashlib.sha256(payload).hexdigest() != f['sha256']:
                _fail('HASH', 'Payload hash/size differs from the declared exact bytes.', f['path'])
            if detect_format(payload) != (f['format'], f['mime_type']):
                _fail('FORMAT', 'Payload header does not match the recorded format.', f['path'])
            payloads[f['sha256']] = payload
        identity, version = manifest['identity']['id'], manifest['version']
        reference = dict(id=identity, version=version, sha256=manifest['manifest_sha256'])
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM versions WHERE id=? AND version=?', (identity, version)).fetchone():
                _fail('IMMUTABLE', 'This asset version already exists; publish a new version.')
            if db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] >= MAX_VERSIONS:
                _fail('LIMIT', 'Library version limit exceeded.')
            prior = db.execute('SELECT manifest FROM versions WHERE id=? LIMIT 1', (identity,)).fetchone()
            if prior:
                old_identity = load_manifest(prior[0])['identity']
                if any(old_identity[k] != manifest['identity'][k] for k in ('namespace', 'library', 'kind', 'created_at')):
                    _fail('IDENTITY', 'Stable identity cannot move namespace/library, change kind or rewrite original creation time.')
            pending = ([x['target'] for x in manifest['dependencies']] +
                       manifest['provenance']['parents'] + manifest['metadata']['style_refs'])
            visited = set()
            while pending:
                target = pending.pop()
                key = (target['id'], target['version'], target['sha256'])
                if key in visited:
                    continue
                visited.add(key)
                if len(visited) > MAX_VERSIONS:
                    _fail('LIMIT', 'Dependency closure exceeds the library version limit.')
                row = self._lookup(db, target)
                self._verify_payloads(db, row)
                pending.extend(x['target'] for x in row['dependencies'])
                pending.extend(row['provenance']['parents'])
                pending.extend(row['metadata']['style_refs'])
            total = db.execute('SELECT COALESCE(SUM(length(payload)),0) FROM blobs').fetchone()[0]
            for digest, payload in payloads.items():
                row = db.execute('SELECT payload FROM blobs WHERE sha256=? AND length(payload)=?', (digest, len(payload))).fetchone()
                exists = db.execute('SELECT 1 FROM blobs WHERE sha256=?', (digest,)).fetchone()
                if exists and not row:
                    _fail('CORRUPT', 'Existing content-addressed blob has incorrect size.')
                if row and row[0] != payload:
                    _fail('CORRUPT', 'Existing content-addressed blob is corrupt.')
                if not row:
                    total += len(payload)
                    if total > MAX_LIBRARY_BYTES: _fail('LIMIT', 'Library byte quota exceeded.')
                    db.execute('INSERT INTO blobs VALUES(?,?)', (digest, payload))
            db.execute('INSERT INTO versions VALUES(?,?,?,?)',
                       (identity, version, reference['sha256'], canonical_bytes(manifest)))
            annotations = dict(schema_version=1, asset=reference, revision=0, tags=[], favorite=False, note='')
            db.execute('INSERT INTO annotations VALUES(?,?,?,?)',
                       (identity, version, 0, canonical_bytes(annotations)))
            if self.storage:
                self.storage.check_projection(db)
                self.storage.append(dict(schema_version=1, kind='version', value=manifest), payloads)
        return reference

    def _lookup(self, db, reference):
        validate(reference, 'AssetRef')
        row = db.execute('SELECT sha256,manifest FROM versions WHERE id=? AND version=?',
                         (reference['id'], reference['version'])).fetchone()
        if not row: _fail('MISSING', 'The exact asset version is absent.')
        manifest = load_manifest(row[1])
        if row[0] != reference['sha256'] or manifest['manifest_sha256'] != reference['sha256']:
            _fail('HASH', 'Exact version hash does not match the stored manifest.')
        if (manifest['identity']['id'], manifest['version']) != (reference['id'], reference['version']):
            _fail('IDENTITY', 'Stored identity/version is inconsistent.')
        return manifest

    def _verify_payloads(self, db, manifest):
        for f in manifest['files']:
            row = db.execute('SELECT payload FROM blobs WHERE sha256=? AND length(payload)=?', (f['sha256'], f['bytes'])).fetchone()
            if not row or len(row[0]) != f['bytes'] or hashlib.sha256(row[0]).hexdigest() != f['sha256']:
                _fail('CORRUPT', 'Required inventoried content is absent or corrupt.', f['path'])
            if self.storage:
                payload = self.storage._read(self.storage.root / 'blobs' / f['sha256'], f['bytes'])
                if payload != row[0]:
                    _fail('CORRUPT', 'Canonical immutable payload differs from its verified index.', f['path'])

    def resolve(self, reference):
        validate(reference, 'AssetRef')
        with self._connect() as db:
            visited, pending = set(), [reference]
            first = None
            while pending:
                current = pending.pop()
                key = (current['id'], current['version'], current['sha256'])
                if key in visited: continue
                visited.add(key)
                if len(visited) > MAX_VERSIONS: _fail('LIMIT', 'Dependency closure exceeds limit.')
                manifest = self._lookup(db, current)
                if first is None: first = manifest
                self._verify_payloads(db, manifest)
                pending.extend(x['target'] for x in manifest['dependencies'])
                pending.extend(manifest['provenance']['parents'])
                pending.extend(manifest['metadata']['style_refs'])
            return first

    def inspect(self, reference, *, verify_payloads=True):
        """Read this exact version without traversing its dependency graph."""
        if type(verify_payloads) is not bool:
            _fail('REQUEST', 'verify_payloads must be boolean.')
        with self._connect() as db:
            manifest = self._lookup(db, reference)
            if verify_payloads:
                self._verify_payloads(db, manifest)
            return manifest

    def list_references(self, *, limit=MAX_VERSIONS):
        limit = _bounded_integer(limit, 1, MAX_VERSIONS, code='LIMIT', label='Reference limit')
        with self._connect() as db:
            rows = db.execute('SELECT id,version,sha256 FROM versions ORDER BY id,version LIMIT ?', (limit,)).fetchall()
            return [dict(id=row[0], version=row[1], sha256=row[2]) for row in rows]

    def read_file(self, reference, path):
        portable_path(path)
        manifest = self.resolve(reference)
        f = next((x for x in manifest['files'] if x['path'] == path), None)
        if not f: _fail('MISSING', 'Logical file is not in this exact version.', path)
        with self._connect() as db:
            row = db.execute('SELECT payload FROM blobs WHERE sha256=? AND length(payload)=?', (f['sha256'], f['bytes'])).fetchone()
            if not row or len(row[0]) != f['bytes'] or hashlib.sha256(row[0]).hexdigest() != f['sha256']:
                _fail('CORRUPT', 'Payload changed during retrieval.', path)
            return row[0]

    def read_own_file(self, reference, path):
        """Read verified own bytes; dependency admission stays with the caller."""
        portable_path(path)
        with self._connect() as db:
            manifest = self._lookup(db, reference)
            self._verify_payloads(db, manifest)
            file = next((x for x in manifest['files'] if x['path'] == path), None)
            if file is None:
                _fail('MISSING', 'Logical file is not in this exact version.', path)
            return db.execute('SELECT payload FROM blobs WHERE sha256=?', (file['sha256'],)).fetchone()[0]

    def annotations(self, reference):
        with self._connect() as db:
            # Canonical append precedes SQLite commit. Join the writer lock so
            # active publication cannot look like a crashed split transaction.
            db.execute('BEGIN IMMEDIATE')
            self._lookup(db, reference)
            row = db.execute('SELECT document FROM annotations WHERE id=? AND version=?',
                             (reference['id'], reference['version'])).fetchone()
            if not row: _fail('CORRUPT', 'Mutable annotation record is absent.')
            result = validate(decode_json(row[0]), 'AssetAnnotations')
            if result['asset'] != reference: _fail('CORRUPT', 'Annotation identity differs from its asset.')
            if self.storage:
                _, history, _ = self.storage.records()
                versions = [(revision, value) for (identity, version, revision), value in history.items()
                            if (identity, version) == (reference['id'], reference['version'])]
                if not versions:
                    _fail('DURABILITY', 'Canonical annotation history is missing.')
                expected = {k: v for k, v in max(versions, key=lambda x: x[0])[1].items() if k != '_checkpoint'}
                if result != expected:
                    _fail('DURABILITY', 'Annotation index differs from durable history; run recover.')
            return result

    def update_annotations(self, reference, *, expected_revision, tags, favorite, note):
        expected_revision = _bounded_integer(expected_revision, 0, 2**53-1,
                                             code='REVISION', label='Expected revision')
        replacement = dict(schema_version=1, asset=reference, revision=expected_revision + 1,
                           tags=tags, favorite=favorite, note=note)
        validate(replacement, 'AssetAnnotations')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._lookup(db, reference)
            count = db.execute('UPDATE annotations SET revision=?,document=? WHERE id=? AND version=? AND revision=?',
                               (replacement['revision'], canonical_bytes(replacement), reference['id'],
                                reference['version'], expected_revision)).rowcount
            if count != 1: _fail('CONFLICT', 'Annotation revision is stale; reload before editing.')
            if self.storage:
                # SQL has already advanced this row within the uncommitted
                # transaction; check the durable history before publication.
                self.storage.check_annotation_previous(reference, expected_revision)
                self.storage.append(dict(schema_version=1, kind='annotations', value=replacement))
        return replacement

    def upgrade_storage(self):
        """Explicitly migrate a legacy SQLite-only library without rewriting bytes."""
        from asset_library_storage import Storage
        if self.storage:
            return self.storage_status()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            storage = Storage(self, create=not (self.root / 'records').exists())
            # Validate all current records before making the durable copy.
            rows = db.execute('SELECT id,version,sha256 FROM versions ORDER BY id,version').fetchall()
            for identity, version, digest in rows:
                manifest = self._lookup(db, dict(id=identity, version=version, sha256=digest))
                self._verify_payloads(db, manifest)
            for identity, version, digest in rows:
                manifest = self._lookup(db, dict(id=identity, version=version, sha256=digest))
                blobs = {f['sha256']: db.execute('SELECT payload FROM blobs WHERE sha256=?', (f['sha256'],)).fetchone()[0] for f in manifest['files']}
                storage.append(dict(schema_version=1, kind='version', value=manifest), blobs)
                row = db.execute('SELECT document FROM annotations WHERE id=? AND version=?', (identity, version)).fetchone()
                value = validate(decode_json(row[0]), 'AssetAnnotations')
                if value['revision']:
                    # Legacy history was not retained; preserve its exact latest
                    # record with an explicit checkpoint, never invent revisions.
                    storage.append(dict(schema_version=1, kind='annotation-checkpoint', value=value))
            storage.ready()
            db.execute('CREATE TABLE IF NOT EXISTS durability(canonical INTEGER NOT NULL)')
            db.execute('DELETE FROM durability')
            db.execute('INSERT INTO durability VALUES(1)')
            self.storage = storage
        return self.storage_status()

    @classmethod
    def rebuild_index(cls, root):
        """Restore a missing SQLite index from verified immutable records.

        Existing database files are never replaced by this operation. Use
        recover() to rebuild query rows in an existing usable SQLite database.
        """
        import tempfile
        from atomic_publish import _rename_no_replace
        from asset_library_storage import Storage, _sync_directory
        root = Path(root).absolute()
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or root.is_symlink() or
                getattr(info, 'st_file_attributes', 0) & 0x400 or
                (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            _fail('ROOT', 'Index recovery requires an existing private owned library root.')
        destination = root / 'library.sqlite3'
        if destination.exists() or destination.is_symlink():
            _fail('EXISTS', 'Index recovery never overwrites an existing database.')
        instance = cls.__new__(cls)
        instance.root = root
        instance._root_identity = (info.st_dev, info.st_ino)
        storage = Storage(instance)
        instance.storage = storage
        if not (storage.root / 'READY').is_file():
            _fail('DURABILITY', 'Canonical storage migration did not finish.')
        storage.validate_ready()
        fd, name = tempfile.mkstemp(prefix='.index-recovery-', dir=root)
        os.close(fd)
        temporary = Path(name)
        try:
            from contextlib import closing
            with closing(sqlite3.connect(temporary)) as db:
                with db:
                    db.executescript(INDEX_SCHEMA)
                    result = storage.rebuild(db)
                    db.execute('CREATE TABLE durability(canonical INTEGER NOT NULL)')
                    db.execute('INSERT INTO durability VALUES(1)')
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            _rename_no_replace(temporary, destination)
            _sync_directory(root)
        finally:
            if temporary.exists():
                temporary.unlink()
        return result

    def storage_status(self):
        if not self.storage:
            return dict(durable=False, state='legacy-sqlite', migration='upgrade_storage')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            manifests, blobs, annotations, snapshot = self.storage.materialize()
            return dict(durable=True, snapshot=snapshot, versions=len(manifests),
                        blobs=len(blobs), bytes=sum(map(len, blobs.values())),
                        abandoned_staging=len(list((self.storage.root / 'staging').iterdir())))

    def recover(self):
        if not self.storage:
            _fail('DURABILITY', 'Legacy library requires explicit upgrade before canonical recovery.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.storage.rebuild(db)

    def events(self, stream):
        from asset_library_storage import validate_stream
        validate_stream(stream)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT revision,document FROM asset_events WHERE stream=? ORDER BY revision', (stream,)).fetchall()
            if len(rows) > 65536 or [r for r, _ in rows] != list(range(1, len(rows) + 1)):
                _fail('CORRUPT', 'Event stream is unbounded or has missing revisions.')
            result = [dict(stream=stream, revision=revision, document=decode_json(payload)) for revision, payload in rows]
            if self.storage and result != [value for value in self.storage.event_records() if value['stream'] == stream]:
                _fail('DURABILITY', 'Event index differs from durable history; run recover.')
            return result

    def append_event(self, stream, *, expected_revision, document):
        from asset_library_storage import validate_stream
        validate_stream(stream)
        expected_revision = _bounded_integer(expected_revision, 0, 65535, code='REVISION', label='Event revision')
        if not self.storage:
            _fail('DURABILITY', 'Upgrade legacy storage before appending durable events.')
        if not isinstance(document, dict):
            _fail('REQUEST', 'Event document must be a mapping.')
        payload = canonical_bytes(document)
        if len(payload) > 65536:
            _fail('LIMIT', 'Event document exceeds 64 KiB.')
        value = dict(stream=stream, revision=expected_revision + 1, document=decode_json(payload))
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT COALESCE(MAX(revision),0) FROM asset_events WHERE stream=?', (stream,)).fetchone()[0]
            if current != expected_revision:
                _fail('CONFLICT', 'Event revision is stale.')
            durable = [r['revision'] for r in self.storage.event_records() if r['stream'] == stream]
            if max(durable, default=0) != current:
                _fail('DURABILITY', 'Recover committed event history before appending.')
            self.storage.append(dict(schema_version=1, kind='event', value=value))
            db.execute('INSERT INTO asset_events VALUES(?,?,?)', (stream, value['revision'], payload))
        return value

    def analyze_gc(self):
        if not self.storage:
            _fail('DURABILITY', 'Legacy library requires explicit upgrade before garbage collection.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.storage.gc_plan()

    def apply_gc(self, plan):
        if not self.storage:
            _fail('DURABILITY', 'Legacy library requires explicit upgrade before garbage collection.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.storage.apply_gc(plan)

    def backup(self, destination):
        """Publish a complete portable private library at a new destination."""
        import shutil
        import tempfile
        from atomic_publish import _rename_no_replace
        from asset_library_storage import Storage, _sync_directory
        if not self.storage:
            _fail('DURABILITY', 'Upgrade legacy storage before making a canonical backup.')
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            _fail('EXISTS', 'Backup requires a new destination.')
        parent = destination.parent.resolve(strict=True)
        destination = parent / destination.name
        stage = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-backup-', dir=parent))
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                manifests, blobs, annotations, snapshot = self.storage.materialize()
                versions = {(a, v): digest for a, v, digest in db.execute('SELECT id,version,sha256 FROM versions')}
                if versions != {key: value['manifest_sha256'] for key, value in manifests.items()}:
                    _fail('DURABILITY', 'Recover the split index before backing it up.')
                for key, value in annotations.items():
                    row = db.execute('SELECT document FROM annotations WHERE id=? AND version=?', key).fetchone()
                    if row is None or decode_json(row[0]) != value:
                        _fail('DURABILITY', 'Recover split annotation history before backing it up.')
                holder = type('BackupRoot', (), {'root': stage})()
                storage = Storage(holder, create=True)
                for digest, payload in blobs.items():
                    storage._publish(storage.root / 'blobs', digest, payload)
                for path in sorted((self.storage.root / 'commits').iterdir()):
                    storage._publish(storage.root / 'commits', path.name, self.storage._read(path, 2 * 1024 * 1024))
                storage.ready()
            # Build only from copied canonical records, independently of source
            # SQLite pages and platform-local absolute names.
            cls = type(self)
            cls.rebuild_index(stage)
            receipt = dict(schema_version=1, kind='asset-library-backup', snapshot=snapshot,
                           versions=len(manifests), blobs=len(blobs), bytes=sum(map(len, blobs.values())))
            with (stage / 'backup.json').open('xb') as stream:
                stream.write(canonical_bytes(receipt))
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(stage)
            _rename_no_replace(stage, destination)
            _sync_directory(parent)
            return receipt
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def recover_blob(self, digest, payload, *, expected_bytes):
        """Repair only a referenced damaged index blob using its exact identity."""
        expected_bytes = _bounded_integer(expected_bytes, 1, MAX_FILE_BYTES, code='LIMIT', label='Blob byte count')
        if type(payload) is not bytes or len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != digest:
            _fail('HASH', 'Recovery requires exact expected blob bytes.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            known = False
            for row in db.execute('SELECT manifest FROM versions'):
                manifest = load_manifest(row[0])
                if any(f['sha256'] == digest and f['bytes'] == expected_bytes for f in manifest['files']):
                    known = True
            if not known:
                _fail('MISSING', 'Cannot repair a blob unreferenced by an immutable version.')
            row = db.execute('SELECT payload FROM blobs WHERE sha256=?', (digest,)).fetchone()
            if row and row[0] == payload:
                _fail('CONFLICT', 'Blob is already intact; recovery would not repair damage.')
            if self.storage:
                actual = self.storage._read(self.storage.root / 'blobs' / digest, expected_bytes)
                if actual != payload:
                    _fail('CORRUPT', 'Canonical payload is damaged; index repair cannot replace canonical evidence.')
            db.execute('INSERT OR REPLACE INTO blobs VALUES(?,?)', (digest, payload))
        return dict(sha256=digest, bytes=expected_bytes, repaired=True)

    def scene_reference(self, reference, path):
        manifest = self.resolve(reference)
        self.read_file(reference, path)
        return dict(id=reference['id'], version=reference['version'], path=path,
                    sha256=next(f['sha256'] for f in manifest['files'] if f['path'] == path),
                    kind=manifest['identity']['kind'])


def legacy_roundtrip(kind, payload):
    """Validate through native authority and return the *original* source bytes.

    This adapter registers preserved native documents; it never upgrades a
    profile into geometry or interprets a native review as library approval.
    Native IDs inside the document are deliberately not rewritten.
    """
    value = decode_json(payload)
    if kind == 'legacy-pet':
        from profile_spec import load_profile
        load_profile(value)
    elif kind == 'legacy-cast':
        from production_contracts import validate_cast
        validate_cast(value)
    elif kind == 'legacy-clip':
        # Full clip semantics depend on the owning cast and remain native.
        from production_contracts import validate_structure
        validate_structure(value, 'clip')
    elif kind == 'rig':
        from rig_contracts import validate_contract
        validate_contract(value, 'Rig')
    else:
        _fail('ADAPTER', 'Unsupported native adapter; no conversion was performed.')
    return payload
