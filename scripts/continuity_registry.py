"""Transactional continuity identity revisions, separate from native asset storage.

SQLite stores only continuity documents, draft/control history and version pins.
Native asset bytes and native approval receipts remain in their original owners.
No operation here performs geometry inspection, rendering or visual review.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from continuity_contracts import (MAX_BYTES, canonical_bytes, decode_json, digest, ID,
    references, read_reference_bytes, validate_document, verify_references)

MAX_RECORDS = 10000
MAX_DB_BYTES = 256 * 1024 * 1024


def _id(value):
    import re
    if not isinstance(value, str) or re.fullmatch(ID['pattern'], value) is None:
        raise ValueError('CONTINUITY_ID: invalid identifier')
    return value


def _sha(value):
    import re
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('CONTINUITY_HASH: expected exact SHA-256')
    return value


def _unpack(payload):
    return decode_json(payload)


class IdentityRegistry:
    """One local data-only database; explicit expected hashes guard every edit.

    Root and its ancestors must be trusted against arbitrary concurrent renaming.
    Leaf symlinks/nonregular databases are rejected, including broken symlinks.
    SQLite serializes writers and commits metadata/pin migrations atomically.
    """

    def __init__(self, path, *, asset_root):
        requested = Path(path)
        self.path = requested.parent.resolve(strict=True) / requested.name
        self.asset_root = Path(asset_root).resolve(strict=True)
        parent = self.path.parent
        if not parent.is_dir():
            raise ValueError('CONTINUITY_PATH: registry parent must already exist')
        for candidate in (self.path, *self.path.parents):
            if candidate.is_symlink():
                raise ValueError('CONTINUITY_PATH: registry paths cannot traverse symlinks')
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
                raise ValueError('CONTINUITY_PATH: unsafe SQLite sidecar')
        created = not self.path.exists()
        if not created:
            status = self.path.stat(follow_symlinks=False)
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_DB_BYTES:
                raise ValueError('CONTINUITY_LIMIT: invalid or oversized registry')
        else:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        # The constructor owns this handle until all initialization succeeds.
        # Transaction rollback alone does not close a sqlite3.Connection.
        try:
            application_id = self.db.execute('PRAGMA application_id').fetchone()[0]
            if not created and application_id != 0x48435431:
                raise ValueError('CONTINUITY_VERSION: existing file is not a continuity registry')
            if created:
                self.db.execute('PRAGMA application_id=1212372017')
            self.db.execute('PRAGMA trusted_schema=OFF')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            # A bounded local metadata store; oversized growth fails atomically.
            page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
            self.db.execute('PRAGMA max_page_count=' + str(MAX_DB_BYTES // page_size))
            with self._transaction():
                self.db.execute('CREATE TABLE IF NOT EXISTS meta (version TEXT NOT NULL)')
                rows = self.db.execute('SELECT version FROM meta').fetchall()
                if not rows:
                    self.db.execute('INSERT INTO meta VALUES (?)', ('1.0.0',))
                elif rows != [('1.0.0',)]:
                    raise ValueError('CONTINUITY_VERSION: incompatible registry format')
                self.db.execute('CREATE TABLE IF NOT EXISTS identities (id TEXT PRIMARY KEY, document BLOB NOT NULL, hash TEXT NOT NULL, selected TEXT)')
                self.db.execute('CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, identity_id TEXT NOT NULL REFERENCES identities(id), document BLOB NOT NULL, hash TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS contents (hash TEXT PRIMARY KEY, document BLOB NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY, identity_id TEXT NOT NULL REFERENCES identities(id), version TEXT NOT NULL, hash TEXT NOT NULL REFERENCES contents(hash), review BLOB NOT NULL, deprecated INTEGER NOT NULL DEFAULT 0, UNIQUE(identity_id, version))')
                self.db.execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, document BLOB NOT NULL, hash TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS history (sequence INTEGER PRIMARY KEY, action TEXT NOT NULL, subject TEXT NOT NULL, details BLOB NOT NULL)')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _history(self, action, subject, details):
        count = self.db.execute('SELECT count(*) FROM history').fetchone()[0]
        if count >= MAX_RECORDS * 10:
            raise ValueError('CONTINUITY_LIMIT: history limit exceeded; archive before further changes')
        self.db.execute('INSERT INTO history(action,subject,details) VALUES (?,?,?)',
                        (action, subject, canonical_bytes(details)))

    def _limit(self, table):
        # All table names are fixed callers, never input SQL.
        if self.db.execute('SELECT count(*) FROM ' + table).fetchone()[0] >= MAX_RECORDS:
            raise ValueError('CONTINUITY_LIMIT: too many registry records')

    def create_identity(self, document):
        document = validate_document(document, 'identity')
        if document['canonical_revision'] is not None or document['approval_status'] != 'unreviewed':
            raise ValueError('CONTINUITY_APPROVAL: new identity must be unreviewed without selected revision')
        verify_references(self.asset_root, document)
        identity_id = document['identity_id']
        encoded, checksum = canonical_bytes(document), digest(document)
        with self._transaction():
            old = self.db.execute('SELECT hash FROM identities WHERE id=?', (identity_id,)).fetchone()
            if old:
                if old[0] != checksum:
                    raise ValueError('CONTINUITY_IMMUTABLE: identity ID already holds different bytes')
                return checksum
            self._limit('identities')
            self.db.execute('INSERT INTO identities(id,document,hash) VALUES (?,?,?)', (identity_id, encoded, checksum))
            self._history('create-identity', identity_id, dict(sha256=checksum))
        return checksum

    def identity(self, identity_id):
        row = self.db.execute('SELECT document,hash,selected FROM identities WHERE id=?', (_id(identity_id),)).fetchone()
        if not row:
            raise ValueError('CONTINUITY_REFERENCE: unknown identity')
        document = validate_document(_unpack(row[0]), 'identity')
        if digest(document) != row[1]:
            raise ValueError('CONTINUITY_HASH: corrupted identity document')
        pin = None
        if row[2] is not None:
            selected = self.revision(row[2])
            if selected['document']['identity_id'] != identity_id:
                raise ValueError('CONTINUITY_REFERENCE: selected revision belongs to another identity')
            pin = dict(identity_id=identity_id, revision_id=row[2],
                       semantic_version=selected['document']['semantic_version'], sha256=selected['sha256'])
        return dict(document=document, sha256=row[1], selected_revision=row[2], selected_pin=pin)

    def create_draft(self, draft_id, document):
        _id(draft_id)
        document = validate_document(document, 'revision')
        self.identity(document['identity_id'])
        self._lineage(document)
        verify_references(self.asset_root, document)
        encoded, checksum = canonical_bytes(document), digest(document)
        with self._transaction():
            if self.db.execute('SELECT 1 FROM drafts WHERE id=?', (draft_id,)).fetchone():
                raise ValueError('CONTINUITY_CONFLICT: draft already exists')
            self._limit('drafts')
            self.db.execute('INSERT INTO drafts VALUES (?,?,?,?)', (draft_id, document['identity_id'], encoded, checksum))
            self._history('create-draft', draft_id, dict(sha256=checksum))
        return checksum

    def draft(self, draft_id):
        row = self.db.execute('SELECT document,hash FROM drafts WHERE id=?', (_id(draft_id),)).fetchone()
        if not row:
            raise ValueError('CONTINUITY_REFERENCE: unknown draft')
        document = _unpack(row[0])
        if digest(document) != row[1]:
            raise ValueError('CONTINUITY_HASH: corrupted draft')
        return dict(document=document, sha256=row[1])

    def update_draft(self, draft_id, document, *, expected_sha256):
        _sha(expected_sha256)
        document = validate_document(document, 'revision')
        self._lineage(document)
        verify_references(self.asset_root, document)
        encoded, checksum = canonical_bytes(document), digest(document)
        with self._transaction():
            old = self.draft(draft_id)
            if old['sha256'] != expected_sha256:
                raise ValueError('CONTINUITY_CONFLICT: stale draft edit')
            if old['document']['identity_id'] != document['identity_id']:
                raise ValueError('CONTINUITY_IDENTITY: draft cannot change logical identity')
            self.db.execute('UPDATE drafts SET document=?,hash=? WHERE id=?', (encoded, checksum, draft_id))
            self._history('update-draft', draft_id, dict(before_sha256=expected_sha256, after_sha256=checksum))
        return checksum

    def _lineage(self, document):
        identity = self.identity(document['identity_id'])['document']
        if identity['provenance']['fixture'] != document['source_provenance']['fixture']:
            raise ValueError('CONTINUITY_PROVENANCE: drafts must preserve logical identity fixture classification')
        parent = document['parent_revision_id']
        if parent is not None:
            previous = self.revision(parent)
            if previous['document']['identity_id'] != document['identity_id']:
                raise ValueError('CONTINUITY_IDENTITY: parent belongs to another identity')
            def version(value):
                return tuple(map(int, value.split('.')))
            if version(document['semantic_version']) <= version(previous['document']['semantic_version']):
                raise ValueError('CONTINUITY_VERSION: derived revision must advance semantic version')

    def promote(self, draft_id, *, expected_sha256, review, select=False, expected_selected_revision_id=None):
        """Freeze an explicitly reviewed draft. Submitted review is not an attestation.

        Synthetic receipts never approve. Exact receipt bytes and inspected native
        dependencies are reverified; fixture status is retained. This operation
        records an existing supplied decision, and does not mint visual approval.
        """
        _sha(expected_sha256)
        review = validate_document(review, 'review')
        if expected_selected_revision_id is not None:
            _id(expected_selected_revision_id)
        current = self.draft(draft_id)
        document, checksum = current['document'], current['sha256']
        if checksum != expected_sha256:
            raise ValueError('CONTINUITY_CONFLICT: stale promotion')
        if review['verdict'] != 'approved' or review['subject_sha256'] != checksum:
            raise ValueError('CONTINUITY_REVIEW: current explicit approval is required')
        if review['fixture'] != document['source_provenance']['fixture']:
            raise ValueError('CONTINUITY_REVIEW: source fixture classification must be preserved')
        if not document['reference_renders']:
            raise ValueError('CONTINUITY_REVIEW: explicit inspected reference renders are required')
        inspected = {(x['path'], x['sha256']) for x in review['inspected']}
        required = {(x['path'], x['sha256']) for x in [document['geometry'], *document['reference_renders']]}
        if not required <= inspected:
            raise ValueError('CONTINUITY_REVIEW: approval does not cover geometry and reference renders')
        verify_references(self.asset_root, document)
        verify_references(self.asset_root, review)
        # The native receipt is itself real, hash-pinned, and retained by its owner.
        # Only explicitly bound continuity receipts are accepted here. Native rig
        # approvals can be adapted by a future capability-tested review adapter.
        native_bytes = read_reference_bytes(self.asset_root, review['native_receipt'])
        native_record = decode_json(native_bytes)
        expected_record = {key: value for key, value in review.items() if key != 'native_receipt'}
        if native_record != expected_record:
            raise ValueError('CONTINUITY_REVIEW: supplied decision disagrees with retained native receipt')
        self._lineage(document)
        with self._transaction():
            # Re-read after obtaining the write lock so a concurrent draft edit
            # cannot promote bytes other than those actually verified above.
            latest = self.draft(draft_id)
            if select and self.identity(document['identity_id'])['selected_revision'] != expected_selected_revision_id:
                raise ValueError('CONTINUITY_CONFLICT: stale promotion selection')
            if latest['sha256'] != checksum:
                raise ValueError('CONTINUITY_CONFLICT: draft changed during promotion')
            old = self.db.execute('SELECT hash,deprecated FROM revisions WHERE id=?', (document['revision_id'],)).fetchone()
            if old:
                if old[0] != checksum:
                    raise ValueError('CONTINUITY_IMMUTABLE: revision ID reused with different bytes')
                if select and old[1]:
                    raise ValueError('CONTINUITY_DEPRECATED: cannot select deprecated revision')
                if select:
                    self.db.execute('UPDATE identities SET selected=? WHERE id=?', (document['revision_id'], document['identity_id']))
                self.db.execute('DELETE FROM drafts WHERE id=?', (draft_id,))
                self._history('reuse-revision', document['revision_id'], dict(sha256=checksum, selected=select))
                return checksum
            self._limit('revisions')
            existing = self.db.execute('SELECT id FROM revisions WHERE identity_id=? AND version=?',
                (document['identity_id'], document['semantic_version'])).fetchone()
            if existing:
                raise ValueError('CONTINUITY_IMMUTABLE: identity semantic version already exists')
            self.db.execute('INSERT OR IGNORE INTO contents VALUES (?,?)', (checksum, canonical_bytes(document)))
            self.db.execute('INSERT INTO revisions(id,identity_id,version,hash,review) VALUES (?,?,?,?,?)',
                (document['revision_id'], document['identity_id'], document['semantic_version'], checksum, canonical_bytes(review)))
            if select:
                self.db.execute('UPDATE identities SET selected=? WHERE id=?', (document['revision_id'], document['identity_id']))
            self.db.execute('DELETE FROM drafts WHERE id=?', (draft_id,))
            self._history('promote', document['revision_id'], dict(identity_id=document['identity_id'], sha256=checksum,
                selected=select, review_sha256=digest(review), fixture=review['fixture'], production_artwork_approved=False))
        return checksum

    def revision(self, revision_id, *, verify=True):
        row = self.db.execute('SELECT r.hash,c.document,r.review,r.deprecated,r.identity_id,r.version FROM revisions r JOIN contents c ON r.hash=c.hash WHERE r.id=?',
                              (_id(revision_id),)).fetchone()
        if not row:
            raise ValueError('CONTINUITY_REFERENCE: unknown immutable revision')
        document = validate_document(_unpack(row[1]), 'revision')
        if digest(document) != row[0]:
            raise ValueError('CONTINUITY_HASH: corrupted immutable revision')
        if (document['revision_id'] != revision_id or document['identity_id'] != row[4]
                or document['semantic_version'] != row[5]):
            raise ValueError('CONTINUITY_REFERENCE: immutable document disagrees with indexed identity/version')
        review = validate_document(_unpack(row[2]), 'review')
        if review['subject_sha256'] != row[0] or review['verdict'] != 'approved':
            raise ValueError('CONTINUITY_REVIEW: corrupted approval binding')
        if verify:
            verify_references(self.asset_root, document)
            verify_references(self.asset_root, review)
        return dict(document=document, sha256=row[0], review=review, deprecated=bool(row[3]),
                    production_artwork_approved=False)

    def select(self, identity_id, revision_id, *, expected_revision_id):
        """Pin/rollback the identity pointer without rewriting any revision."""
        revision = self.revision(revision_id)
        if revision['deprecated'] or revision['document']['identity_id'] != identity_id:
            raise ValueError('CONTINUITY_REFERENCE: cannot select deprecated/other-identity revision')
        with self._transaction():
            revision = self.revision(revision_id)
            if revision['deprecated']:
                raise ValueError('CONTINUITY_DEPRECATED: revision deprecated during selection')
            current = self.identity(identity_id)
            if current['selected_revision'] != expected_revision_id:
                raise ValueError('CONTINUITY_CONFLICT: stale identity selection')
            self.db.execute('UPDATE identities SET selected=? WHERE id=?', (revision_id, identity_id))
            self._history('select', identity_id, dict(before=expected_revision_id, after=revision_id))
        return revision['sha256']

    def deprecate(self, revision_id, *, expected_sha256, reason):
        _sha(expected_sha256)
        if not isinstance(reason, str) or not 1 <= len(reason) <= 4096:
            raise ValueError('CONTINUITY_REASON: deprecation requires a bounded reason')
        with self._transaction():
            revision = self.revision(revision_id)
            if revision['sha256'] != expected_sha256:
                raise ValueError('CONTINUITY_CONFLICT: stale deprecation')
            if revision['deprecated']:
                return
            self.db.execute('UPDATE revisions SET deprecated=1 WHERE id=?', (revision_id,))
            self._history('deprecate', revision_id, dict(sha256=expected_sha256, reason=reason))

    def compare(self, before_id, after_id):
        before, after = self.revision(before_id), self.revision(after_id)
        if before['document']['identity_id'] != after['document']['identity_id']:
            raise ValueError('CONTINUITY_IDENTITY: cannot compare revisions from different identities')
        changes = []
        def walk(a, b, path):
            if isinstance(a, dict) and isinstance(b, dict):
                for key in sorted(set(a) | set(b)):
                    walk(a.get(key), b.get(key), path + '/' + key)
            elif a != b:
                changes.append(dict(path=path, before=a, after=b))
        walk(before['document'], after['document'], '')
        return dict(before_sha256=before['sha256'], after_sha256=after['sha256'], changes=changes,
                    geometry_compared=False, visual_approval=False)

    def _validate_pins(self, bindings, *, allow_deprecated=False):
        for instance in bindings['instances']:
            pin = instance['revision']
            revision = self.revision(pin['revision_id'])
            doc = revision['document']
            if (pin['identity_id'] != doc['identity_id'] or pin['semantic_version'] != doc['semantic_version']
                    or pin['sha256'] != revision['sha256']):
                raise ValueError('CONTINUITY_REFERENCE: instance pin disagrees with immutable revision')
            if revision['deprecated'] and not allow_deprecated:
                raise ValueError('CONTINUITY_DEPRECATED: cannot newly bind deprecated revision')

    def register_bindings(self, bindings):
        bindings = validate_document(bindings, 'bindings')
        checksum = digest(bindings)
        with self._transaction():
            self._validate_pins(bindings)
            verify_references(self.asset_root, bindings)
            if self.db.execute('SELECT 1 FROM projects WHERE id=?', (bindings['project_id'],)).fetchone():
                raise ValueError('CONTINUITY_CONFLICT: project bindings already registered')
            self._limit('projects')
            self.db.execute('INSERT INTO projects VALUES (?,?,?)', (bindings['project_id'], canonical_bytes(bindings), checksum))
            self._history('register-bindings', bindings['project_id'], dict(sha256=checksum))
        return checksum

    def bindings(self, project_id):
        row = self.db.execute('SELECT document,hash FROM projects WHERE id=?', (_id(project_id),)).fetchone()
        if not row:
            raise ValueError('CONTINUITY_REFERENCE: project bindings missing')
        document = validate_document(_unpack(row[0]), 'bindings')
        if digest(document) != row[1]:
            raise ValueError('CONTINUITY_HASH: corrupted project binding')
        self._validate_pins(document, allow_deprecated=True)
        verify_references(self.asset_root, document)
        return dict(document=document, sha256=row[1])

    def migrate_selected(self, project_id, instance_ids, target_revision_id, *, expected_sha256):
        """Atomically migrate only selected sidecar pins, never native v1 scenes.

        Native scene/rig compatibility admission belongs to tested adapters in
        later integration tasks. Here the only permitted change is an exact
        approved version of the same logical identity, no entity/shot rebinding.
        """
        _sha(expected_sha256)
        if (not isinstance(instance_ids, list) or not instance_ids or len(instance_ids) > 4096
                or any(not isinstance(x, str) for x in instance_ids) or len(set(instance_ids)) != len(instance_ids)):
            raise ValueError('CONTINUITY_SELECTION: unique nonempty bounded instance selection required')
        target = self.revision(target_revision_id)
        if target['deprecated']:
            raise ValueError('CONTINUITY_DEPRECATED: cannot migrate to deprecated revision')
        with self._transaction():
            target = self.revision(target_revision_id)
            if target['deprecated']:
                raise ValueError('CONTINUITY_DEPRECATED: revision deprecated during migration')
            before = self.bindings(project_id)
            if before['sha256'] != expected_sha256:
                raise ValueError('CONTINUITY_CONFLICT: stale selected-version migration')
            bindings = deepcopy(before['document'])
            by_id = {x['instance_id']: x for x in bindings['instances']}
            if not set(instance_ids) <= set(by_id):
                raise ValueError('CONTINUITY_SELECTION: unknown selected instance')
            doc = target['document']
            for name in instance_ids:
                if by_id[name]['revision']['identity_id'] != doc['identity_id']:
                    raise ValueError('CONTINUITY_IDENTITY: migration cannot change logical identity')
                old = self.revision(by_id[name]['revision']['revision_id'])['document']
                # Until geometry/rig adapter acceptance is implemented, migration
                # may only rebind a metadata revision of exactly the same native
                # structural payload. Changed assets require real compatibility
                # inspection, never a name/ID-only substitution.
                structural = ('asset', 'dependencies', 'geometry', 'components', 'materials', 'textures', 'rig', 'skin_weights',
                              'morphs', 'attachments', 'coordinates', 'rest_pose', 'bounds')
                if any(old[key] != doc[key] for key in structural):
                    raise ValueError('CONTINUITY_ADAPTER_REQUIRED: changed native structure needs verified compatibility adapter')
                by_id[name]['revision'] = dict(identity_id=doc['identity_id'], revision_id=doc['revision_id'],
                    semantic_version=doc['semantic_version'], sha256=target['sha256'])
            bindings = validate_document(bindings, 'bindings')
            self._validate_pins(bindings, allow_deprecated=True)
            checksum = digest(bindings)
            self.db.execute('UPDATE projects SET document=?,hash=? WHERE id=?', (canonical_bytes(bindings), checksum, project_id))
            self._history('migrate-selected', project_id, dict(before_sha256=expected_sha256, after_sha256=checksum,
                instance_ids=sorted(instance_ids), target_revision_id=target_revision_id))
        return checksum

    def history(self):
        return [dict(sequence=row[0], action=row[1], subject=row[2], details=_unpack(row[3]))
                for row in self.db.execute('SELECT sequence,action,subject,details FROM history ORDER BY sequence')]
