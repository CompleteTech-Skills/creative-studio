"""Durable immutable backing records for the canonical AssetLibrary owner.

SQLite serializes writers and is the query projection. A completed record is
published only after its blob bytes are fsynced and verified. Explicit recovery
replays completed records; abandoned staging files are never inferred committed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from asset_library_contracts import canonical_bytes, load_manifest, validate
from atomic_publish import publish_verified_file, VerifiedFileCleanupError
from rig_errors import RigError, decode_json, object_hash

MAX_RECORDS = 65536
MAX_RECORD_BYTES = 2 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _fail(message):
    raise RigError('LIBRARY_DURABILITY', message)


def validate_stream(stream):
    if not isinstance(stream, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,95}', stream):
        _fail('Event stream must be a bounded portable lowercase identifier.')


def _sync_directory(path):
    # Windows file flush + exclusive rename supplies its supported durability
    # boundary; opening a directory for fsync is not supported there.
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Storage:
    def __init__(self, library, *, create=False):
        self.library = library
        self.root = library.root / 'records'
        if create:
            self.root.mkdir(mode=0o700)
            for name in ('blobs', 'commits', 'staging'):
                (self.root / name).mkdir(mode=0o700)
            _sync_directory(library.root)
        self._check()

    def _check(self):
        # The parent library remains a private owned trust boundary, matching
        # AssetLibrary._connect. Reject aliases even inside that boundary.
        admitted = {}
        for path in (self.library.root, self.root, *(self.root / n for n in ('blobs', 'commits', 'staging'))):
            info = path.lstat()
            if path == self.library.root and hasattr(self.library, '_root_identity') and (info.st_dev, info.st_ino) != self.library._root_identity:
                _fail('Canonical library root was replaced.')
            if (not stat.S_ISDIR(info.st_mode) or path.is_symlink() or
                    getattr(info, 'st_file_attributes', 0) & 0x400 or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                _fail('Canonical storage directories must remain private, owned and free of aliases.')
            admitted[path] = info
        return admitted

    def _read(self, path, limit):
        from asset_library import read_source_bytes
        info = path.lstat()
        if info.st_nlink != 1:
            _fail('Canonical records cannot be hard links.')
        return read_source_bytes(path, limit)

    def _publish(self, directory, name, payload):
        admitted = self._check()
        if directory not in admitted:
            _fail('Canonical publication parent is outside its admitted storage directories.')
        parent_info = admitted[directory]
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            if self._read(destination, max(len(payload), 1)) != payload:
                _fail('Existing immutable content differs from its digest.')
            return
        # Keep the admitted bytes on one open temporary file. The canonical
        # publisher copies that descriptor into its pinned private namespace;
        # neither publication nor cleanup reopens a mutable staging basename.
        with tempfile.TemporaryFile(mode='w+b') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                publish_verified_file(stream.fileno(), destination,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_size=len(payload), max_bytes=max(len(payload), 1),
                    staging_parent=self.root / 'staging',
                    expected_parent_identity=(parent_info.st_dev, parent_info.st_ino))
            except VerifiedFileCleanupError as exc:
                # A file commit is not a claim that its enclosing SQLite
                # transaction committed. Preserve the exact file receipt and
                # let the caller roll back its index; recovery can replay a
                # completed canonical commit record if one was already written.
                raise RigError('LIBRARY_PUBLICATION_CLEANUP',
                    'Canonical file committed; owned publication cleanup is pending.',
                    asset=str(destination), observed=dict(file_committed=True, receipt=exc.receipt),
                    preserved=str(destination), retryable=True,
                    correction='Retain the committed file. Resolve owned cleanup, then recover any canonical/index split before retrying.') from exc
            except (OSError, ValueError) as exc:
                raise RigError('LIBRARY_DURABILITY', 'Canonical file publication failed: ' + str(exc),
                               asset=str(destination)) from exc
        self._check()
        if self._read(destination, max(len(payload), 1)) != payload:
            _fail('Published bytes differ from the verified staged bytes.')

    def ready(self):
        self._publish(self.root, 'READY', b'asset-library-canonical-storage-v1\n')

    def validate_ready(self):
        if self._read(self.root / 'READY', 128) != b'asset-library-canonical-storage-v1\n':
            _fail('Canonical storage readiness marker is invalid.')

    def check_projection(self, db):
        """Reject a split commit instead of silently discarding durable history."""
        manifests, _, _ = self.records()
        # Registration inserts its own row before calling this check.
        indexed = {(a, v): digest for a, v, digest in db.execute('SELECT id,version,sha256 FROM versions')}
        if any(indexed.get(key) != m['manifest_sha256'] for key, m in manifests.items()):
            _fail('A committed version is absent from the index; run recover before writing.')

    def check_annotation_previous(self, reference, revision):
        manifests, annotations, _ = self.records()
        key = (reference['id'], reference['version'])
        if key not in manifests:
            _fail('No durable version backs this annotation edit; run recover.')
        revisions = [r for a, v, r in annotations if (a, v) == key]
        if max(revisions, default=0) != revision:
            _fail('Committed annotation revision differs from index; run recover before editing.')

    def append(self, record, payloads=()):
        """Caller must hold the library's BEGIN IMMEDIATE writer transaction."""
        encoded = canonical_bytes(record)
        if len(encoded) > MAX_RECORD_BYTES:
            _fail('Canonical commit exceeds record budget.')
        payloads = dict(payloads)
        paths = list((self.root / 'blobs').iterdir())
        if len(paths) > 1048576:
            _fail('Canonical blob inventory exceeds bound.')
        total = sum(p.lstat().st_size for p in paths)
        commits = list((self.root / 'commits').iterdir())
        total += sum(p.lstat().st_size for p in commits) + len(encoded)
        for digest, payload in payloads.items():
            if not (self.root / 'blobs' / digest).exists():
                total += len(payload)
        if total > 1024 ** 3:
            _fail('Canonical payload quota exceeded; analyze unreferenced content before retrying.')
        for digest, payload in payloads.items():
            if not _HASH.fullmatch(digest) or hashlib.sha256(payload).hexdigest() != digest:
                _fail('Blob digest differs from supplied immutable bytes.')
            self._publish(self.root / 'blobs', digest, payload)
        name = hashlib.sha256(encoded).hexdigest() + '.json'
        if len(commits) >= MAX_RECORDS and not (self.root / 'commits' / name).exists():
            _fail('Canonical commit history limit exceeded.')
        self._publish(self.root / 'commits', name, encoded)

    def _records(self):
        self._check()
        paths = sorted((self.root / 'commits').iterdir())
        if len(paths) > MAX_RECORDS:
            _fail('Canonical commit history limit exceeded.')
        manifests, annotations, digest_rows, events = {}, {}, [], {}
        for path in paths:
            if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
                _fail('Unexpected canonical commit path.')
            payload = self._read(path, MAX_RECORD_BYTES)
            if hashlib.sha256(payload).hexdigest() + '.json' != path.name:
                _fail('Canonical commit hash differs.')
            record = decode_json(payload)
            if not isinstance(record, dict) or set(record) != {'schema_version', 'kind', 'value'} or type(record['schema_version']) is not int or record['schema_version'] != 1:
                _fail('Malformed canonical commit.')
            value = record['value']
            if record['kind'] == 'version':
                value = load_manifest(canonical_bytes(value))
                key = (value['identity']['id'], value['version'])
                if key in manifests and manifests[key] != value:
                    _fail('Conflicting immutable version commits.')
                manifests[key] = value
                annotations.setdefault((*key, 0), dict(schema_version=1,
                    asset=dict(id=key[0], version=key[1], sha256=value['manifest_sha256']),
                    revision=0, tags=[], favorite=False, note=''))
            elif record['kind'] == 'annotations':
                value = validate(value, 'AssetAnnotations')
                key = (value['asset']['id'], value['asset']['version'], value['revision'])
                if key in annotations and annotations[key] != value:
                    _fail('Conflicting annotation revision commits.')
                annotations[key] = value
            elif record['kind'] == 'annotation-checkpoint':
                value = validate(value, 'AssetAnnotations')
                key = (value['asset']['id'], value['asset']['version'], value['revision'])
                if key in annotations and {k: v for k, v in annotations[key].items() if k != '_checkpoint'} != value:
                    _fail('Conflicting annotation checkpoint commits.')
                annotations[key] = {**value, '_checkpoint': True}
            elif record['kind'] == 'event':
                if not isinstance(value, dict) or set(value) != {'stream', 'revision', 'document'}:
                    _fail('Malformed durable event.')
                validate_stream(value['stream'])
                revision = value['revision']
                if type(revision) is not int or not 1 <= revision <= 65536 or not isinstance(value['document'], dict) or len(canonical_bytes(value['document'])) > 65536:
                    _fail('Event payload or revision exceeds bounds.')
                key = (value['stream'], revision)
                if key in events and events[key] != value:
                    _fail('Conflicting durable event revision.')
                events[key] = value
            else:
                _fail('Unknown canonical commit kind.')
            digest_rows.append(path.name)
        streams = {stream for stream, _ in events}
        for stream in streams:
            revisions = sorted(revision for name, revision in events if name == stream)
            if revisions != list(range(1, len(revisions) + 1)):
                _fail('Durable event stream has missing revisions.')
        return manifests, annotations, object_hash(digest_rows), [events[key] for key in sorted(events)]

    def records(self):
        return self._records()[:3]

    def event_records(self):
        return self._records()[3]

    def materialize(self):
        manifests, annotations, snapshot = self.records()
        blobs = {}
        current_annotations = {}
        for key, manifest in manifests.items():
            for file in manifest['files']:
                digest = file['sha256']
                if digest not in blobs:
                    payload = self._read(self.root / 'blobs' / digest, file['bytes'])
                    if len(payload) != file['bytes'] or hashlib.sha256(payload).hexdigest() != digest:
                        _fail('Required canonical blob is corrupt.')
                    blobs[digest] = payload
            revisions = sorted((revision, value) for (a, v, revision), value in annotations.items() if (a, v) == key)
            checkpoints = [r for r, value in revisions if value.get('_checkpoint')]
            start = max(checkpoints, default=0)
            revisions = [(r, value) for r, value in revisions if r >= start]
            if not revisions or [r for r, _ in revisions] != list(range(start, start + len(revisions))):
                _fail('Missing/noncontiguous immutable annotation history.')
            for _, value in revisions:
                if value['asset']['sha256'] != manifest['manifest_sha256']:
                    _fail('Annotation history targets a different manifest.')
            current_annotations[key] = {k: v for k, v in revisions[-1][1].items() if k != '_checkpoint'}
        if any((a, v) not in manifests for a, v, _ in annotations):
            _fail('Annotation commit has no canonical version.')
        return manifests, blobs, current_annotations, snapshot

    def rebuild(self, db):
        """Verify the whole canonical closure before replacing query rows."""
        manifests, blobs, annotations, snapshot = self.materialize()
        db.execute('DELETE FROM annotations')
        db.execute('DELETE FROM versions')
        db.execute('DELETE FROM blobs')
        db.execute('DELETE FROM asset_events')
        db.executemany('INSERT INTO blobs VALUES(?,?)', blobs.items())
        for (identity, version), manifest in manifests.items():
            db.execute('INSERT INTO versions VALUES(?,?,?,?)', (identity, version, manifest['manifest_sha256'], canonical_bytes(manifest)))
            value = annotations[(identity, version)]
            db.execute('INSERT INTO annotations VALUES(?,?,?,?)', (identity, version, value['revision'], canonical_bytes(value)))
        for value in self.event_records():
            db.execute('INSERT INTO asset_events VALUES(?,?,?)', (value['stream'], value['revision'], canonical_bytes(value['document'])))
        # Resolve dependency identity/hash/bytes through the established owner.
        for manifest in manifests.values():
            pending = ([d['target'] for d in manifest['dependencies']] + manifest['provenance']['parents'] + manifest['metadata']['style_refs'])
            for reference in pending:
                dependency = self.library._lookup(db, reference)
                self.library._verify_payloads(db, dependency)
        return dict(snapshot=snapshot, versions=len(manifests), blobs=len(blobs), bytes=sum(map(len, blobs.values())))

    def gc_plan(self):
        manifests, blobs, _, snapshot = self.materialize()
        paths = sorted((self.root / 'blobs').iterdir())
        if len(paths) > 1048576:
            _fail('Blob inventory exceeds bounded scan.')
        candidates = []
        for path in paths:
            if not _HASH.fullmatch(path.name):
                _fail('Unexpected canonical blob path.')
            if path.name in blobs:
                continue
            payload = self._read(path, 64 * 1024 * 1024)
            if hashlib.sha256(payload).hexdigest() != path.name:
                _fail('Unreachable blob has an invalid digest; retain for diagnosis.')
            candidates.append(dict(sha256=path.name, bytes=len(payload)))
        body = dict(schema_version=1, kind='asset-library-gc', snapshot=snapshot,
                    candidates=candidates, protected_versions=len(manifests),
                    policy='Retain all committed versions, annotation history, dependencies, source and evidence; staging is never deleted.')
        return {**body, 'plan_sha256': object_hash(body)}

    def apply_gc(self, plan):
        if not isinstance(plan, dict) or plan != self.gc_plan():
            _fail('GC analysis is stale or altered; analyze again before applying.')
        for candidate in plan['candidates']:
            (self.root / 'blobs' / candidate['sha256']).unlink()
        _sync_directory(self.root / 'blobs')
        return dict(deleted=len(plan['candidates']), bytes=sum(c['bytes'] for c in plan['candidates']), snapshot=plan['snapshot'])
