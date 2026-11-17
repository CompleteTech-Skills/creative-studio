"""Actual durable storage, crash recovery, index loss and protected GC checks."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

from asset_library import AssetLibrary
from asset_library_contracts import canonical_bytes, seal
from rig_errors import RigError
import test_asset_library as foundation_fixture


class StorageTests(unittest.TestCase):
    def setUp(self):
        fixture = foundation_fixture.AssetLibraryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.library = fixture.library
        self.root = fixture.root

    def register(self):
        return self.fixture.register()

    def test_canonical_records_survive_total_index_loss_and_relocation(self):
        reference = self.register()
        self.library.update_annotations(reference, expected_revision=0, tags=['retained'], favorite=True, note='Original note')
        before = self.library.storage_status()
        renamed = self.root / 'moved'
        self.library.root.rename(renamed)
        (renamed / 'library.sqlite3').unlink()
        result = AssetLibrary.rebuild_index(renamed)
        recovered = AssetLibrary(renamed)
        self.assertEqual(result['snapshot'], before['snapshot'])
        self.assertEqual(recovered.resolve(reference), self.fixture.manifest)
        self.assertEqual(recovered.read_file(reference, 'mesh/triangle.obj'), self.fixture.mesh.read_bytes())
        self.assertEqual(recovered.annotations(reference)['note'], 'Original note')
        with self.assertRaises(RigError):
            AssetLibrary.rebuild_index(renamed)

    def test_real_process_exit_between_commit_record_and_sql_commit(self):
        manifest = self.root / 'manifest.json'
        manifest.write_bytes(canonical_bytes(self.fixture.manifest))
        sources = self.root / 'sources.json'
        sources.write_text(json.dumps({k: str(v) for k, v in self.fixture.sources.items()}))
        code = '''
import json, os, sys
from pathlib import Path
from asset_library import AssetLibrary
library=AssetLibrary(sys.argv[1])
original=library.storage.append
def crash(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(23)
library.storage.append=crash
library.register(json.loads(Path(sys.argv[2]).read_text()),json.loads(Path(sys.argv[3]).read_text()))
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts'))
        child = subprocess.run([sys.executable, '-c', code, str(self.library.root), str(manifest), str(sources)], env=env, timeout=20)
        self.assertEqual(child.returncode, 23)
        reference = dict(id='triangle', version='1.0.0', sha256=self.fixture.manifest['manifest_sha256'])
        reopened = AssetLibrary(self.library.root)
        with self.assertRaises(RigError):
            reopened.resolve(reference)
        receipt = reopened.recover()
        self.assertEqual(receipt['versions'], 1)
        self.assertEqual(reopened.resolve(reference), self.fixture.manifest)

    def test_annotation_split_requires_recovery_before_conflicting_retry(self):
        reference = self.register()
        original = self.library.storage.append
        def failed_commit(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('simulated loss after durable commit')
        with patch.object(self.library.storage, 'append', failed_commit):
            with self.assertRaises(OSError):
                self.library.update_annotations(reference, expected_revision=0, tags=['first'], favorite=False, note='survives')
        with self.assertRaises(RigError):
            self.library.update_annotations(reference, expected_revision=0, tags=['different'], favorite=False, note='conflict')
        self.library.recover()
        self.assertEqual(self.library.annotations(reference)['note'], 'survives')
        self.assertEqual(self.library.annotations(reference)['revision'], 1)

    def test_gc_retains_every_version_dependency_source_and_staging_file(self):
        reference = self.register()
        second = deepcopy(self.fixture.manifest)
        second['identity']['id'] = 'dependent'
        second['dependencies'] = [dict(target=reference, purpose='mesh', required=True,
                                       origin='authored', relationship='direct', capabilities=[])]
        dependent = self.library.register(seal(second), self.fixture.sources)
        staged = self.library.storage.root / 'staging' / 'interrupted-worker-data'
        staged.write_bytes(b'owned active or interrupted staging; never infer deletable')
        orphan = b'Uncommitted byte payload'
        digest = hashlib.sha256(orphan).hexdigest()
        self.library.storage._publish(self.library.storage.root / 'blobs', digest, orphan)
        plan = self.library.analyze_gc()
        self.assertEqual(plan['candidates'], [dict(sha256=digest, bytes=len(orphan))])
        self.assertEqual(plan['protected_versions'], 2)
        result = self.library.apply_gc(plan)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(staged.exists())
        self.assertEqual(self.library.resolve(dependent)['identity']['id'], 'dependent')
        self.assertEqual(self.library.read_file(reference, 'source/license.txt'), self.fixture.license.read_bytes())

    def test_gc_rejects_modified_and_concurrently_stale_plans(self):
        self.register()
        plan = self.library.analyze_gc()
        tampered = deepcopy(plan)
        tampered['protected_versions'] = 0
        with self.assertRaises(RigError):
            self.library.apply_gc(tampered)
        revised = deepcopy(self.fixture.manifest)
        revised['version'] = '2.0.0'
        self.library.register(seal(revised), self.fixture.sources)
        with self.assertRaises(RigError):
            self.library.apply_gc(plan)

    def test_corrupt_canonical_payload_prevents_rebuild_without_mutating_index(self):
        reference = self.register()
        path = self.library.storage.root / 'blobs' / self.fixture.files[0]['sha256']
        path.write_bytes(b'corrupt')
        with self.assertRaises(RigError):
            self.library.recover()
        # A failed rebuild leaves the existing index transaction intact, but
        # public resolution still reports the corrupted canonical authority.
        with closing(sqlite3.connect(self.library.database)) as db:
            payload = db.execute('SELECT payload FROM blobs WHERE sha256=?', (self.fixture.files[0]['sha256'],)).fetchone()[0]
        self.assertEqual(payload, self.fixture.mesh.read_bytes())
        with self.assertRaises(RigError):
            self.library.resolve(reference)

    def test_gc_rejects_symlink_and_hardlink_aliases(self):
        self.register()
        external = self.root / 'unrelated'
        external.write_bytes(b'unrelated')
        name = hashlib.sha256(external.read_bytes()).hexdigest()
        path = self.library.storage.root / 'blobs' / name
        try:
            path.symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Actual symlink creation unavailable: ' + str(exc))
        with self.assertRaises((RigError, OSError)):
            self.library.analyze_gc()
        path.unlink()
        os.link(external, path)
        with self.assertRaises(RigError):
            self.library.analyze_gc()
        self.assertEqual(external.read_bytes(), b'unrelated')

    def test_legacy_upgrade_preserves_exact_latest_annotations(self):
        reference = self.register()
        self.library.update_annotations(reference, expected_revision=0, tags=['old'], favorite=False, note='before migration')
        shutil.rmtree(self.library.storage.root)
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('DROP TABLE durability')
        legacy = AssetLibrary(self.library.root)
        self.assertFalse(legacy.storage_status()['durable'])
        legacy.upgrade_storage()
        legacy.database.unlink()
        AssetLibrary.rebuild_index(legacy.root)
        reopened = AssetLibrary(legacy.root)
        self.assertEqual(reopened.annotations(reference)['revision'], 1)
        self.assertEqual(reopened.annotations(reference)['note'], 'before migration')
        self.assertEqual(reopened.read_own_file(reference, 'mesh/triangle.obj'), self.fixture.mesh.read_bytes())

    def legacy_handles(self):
        reference = self.register()
        shutil.rmtree(self.library.storage.root)
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('DROP TABLE durability')
        return reference, [AssetLibrary(self.library.root) for _ in range(3)]

    def test_preupgrade_handles_publish_versions_and_annotations_durably(self):
        reference, (migrator, old_writer, old_annotations) = self.legacy_handles()
        migrator.upgrade_storage()
        manifest = deepcopy(self.fixture.manifest)
        manifest['version'] = '2.0.0'
        newer = old_writer.register(seal(manifest), self.fixture.sources)
        old_annotations.update_annotations(reference, expected_revision=0, tags=['retained'],
                                           favorite=True, note='after another handle migrated')
        migrator.recover()
        self.assertEqual(migrator.resolve(newer), seal(manifest))
        self.assertEqual(migrator.annotations(reference)['note'], 'after another handle migrated')
        self.assertTrue(old_writer.storage_status()['durable'])
        # A second explicit upgrade on a pre-upgrade handle is idempotent.
        untouched = AssetLibrary(self.library.root)
        self.assertEqual(untouched.upgrade_storage()['versions'], 2)

    def test_preupgrade_readers_cannot_hide_damaged_canonical_payloads(self):
        reference, handles = self.legacy_handles()
        stale_readers = [AssetLibrary(self.library.root) for _ in range(4)]
        handles[0].upgrade_storage()
        digest = self.fixture.files[0]['sha256']
        (handles[0].storage.root / 'blobs' / digest).write_bytes(b'damaged')
        for stale, name in zip(stale_readers, ('resolve', 'inspect', 'read_file', 'read_own_file')):
            # Each handle was opened before the ordinary migration.
            with self.subTest(api=name), self.assertRaises(RigError):
                method = getattr(stale, name)
                if name.startswith('read_'):
                    method(reference, 'mesh/triangle.obj')
                else:
                    method(reference)

    def test_writer_waiting_for_migration_refreshes_mode_after_lock(self):
        from asset_library_storage import Storage
        reference, (migrator, old_writer, old_annotations) = self.legacy_handles()
        migration_ready, release, writer_started = (threading.Event() for _ in range(3))
        failures, result = [], []
        original_ready = Storage.ready
        original_begin = old_writer._begin_writer
        def paused_ready(storage):
            original_ready(storage)
            migration_ready.set()
            if not release.wait(5):
                raise RuntimeError('migration release timed out')
        def waiting_begin(db):
            writer_started.set()
            original_begin(db)
        def migrate():
            try:
                migrator.upgrade_storage()
            except BaseException as exc:
                failures.append(exc)
        def publish():
            try:
                manifest = deepcopy(self.fixture.manifest)
                manifest['version'] = '2.0.0'
                result.append(old_writer.register(seal(manifest), self.fixture.sources))
                old_annotations.update_annotations(reference, expected_revision=0, tags=[],
                                                   favorite=False, note='waited migration')
            except BaseException as exc:
                failures.append(exc)
        with patch.object(Storage, 'ready', paused_ready), patch.object(old_writer, '_begin_writer', waiting_begin):
            a = threading.Thread(target=migrate)
            b = threading.Thread(target=publish)
            a.start()
            try:
                self.assertTrue(migration_ready.wait(5))
                b.start()
                self.assertTrue(writer_started.wait(5))
            finally:
                release.set()
                a.join(10)
                if b.ident is not None:
                    b.join(10)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(failures, [])
        migrator.recover()
        self.assertEqual(migrator.resolve(result[0])['version'], '2.0.0')
        self.assertEqual(migrator.annotations(reference)['note'], 'waited migration')

    def test_verified_own_reads_and_exact_blob_repair(self):
        reference = self.register()
        self.assertEqual(self.library.list_references(), [reference])
        self.assertEqual(self.library.inspect(reference), self.fixture.manifest)
        digest = self.fixture.files[0]['sha256']
        payload = self.fixture.mesh.read_bytes()
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('DELETE FROM blobs WHERE sha256=?', (digest,))
        with self.assertRaises(RigError):
            self.library.read_own_file(reference, 'mesh/triangle.obj')
        self.library.recover_blob(digest, payload, expected_bytes=len(payload))
        self.assertEqual(self.library.read_own_file(reference, 'mesh/triangle.obj'), payload)
        with self.assertRaises(RigError):
            self.library.recover_blob(digest, payload, expected_bytes=len(payload))

    def test_portable_backup_reopens_without_original_and_never_overwrites(self):
        reference = self.register()
        self.library.update_annotations(reference, expected_revision=0, tags=['portable'], favorite=True, note='retained')
        destination = self.root / 'backup'
        receipt = self.library.backup(destination)
        original = self.library.root
        original.rename(self.root / 'original-offline')
        restored = AssetLibrary(destination)
        self.assertEqual(restored.resolve(reference), self.fixture.manifest)
        self.assertEqual(restored.annotations(reference)['note'], 'retained')
        self.assertEqual(restored.storage_status()['snapshot'], receipt['snapshot'])
        empty = self.root / 'existing-empty'
        empty.mkdir()
        with self.assertRaises(RigError):
            restored.backup(empty)
        self.assertEqual(list(empty.iterdir()), [])

    def test_append_only_events_cas_backup_and_index_recovery(self):
        self.register()
        first = self.library.append_event('lifecycle.triangle', expected_revision=0,
                                           document={'kind': 'pin', 'owner': 'project-1'})
        self.assertEqual(first['revision'], 1)
        with self.assertRaises(RigError):
            self.library.append_event('lifecycle.triangle', expected_revision=0, document={'kind': 'unpin'})
        second = self.library.append_event('lifecycle.triangle', expected_revision=1, document={'kind': 'retire'})
        self.library.database.unlink()
        AssetLibrary.rebuild_index(self.library.root)
        self.assertEqual(AssetLibrary(self.library.root).events('lifecycle.triangle'), [first, second])
        with self.assertRaises(RigError):
            self.library.append_event('../escape', expected_revision=0, document={})

    def test_readers_wait_for_active_durable_annotation_and_event_commits(self):
        reference = self.register()
        for kind in ('annotation', 'event'):
            with self.subTest(kind=kind):
                committed = threading.Event()
                release = threading.Event()
                reader_started = threading.Event()
                results = []
                errors = []
                original = self.library.storage.append
                def paused(*args, **kwargs):
                    original(*args, **kwargs)
                    committed.set()
                    self.assertTrue(release.wait(5))
                def writer():
                    try:
                        if kind == 'annotation':
                            self.library.update_annotations(reference, expected_revision=0, tags=[], favorite=False, note='committed')
                        else:
                            self.library.append_event('concurrency', expected_revision=0, document={'ok': True})
                    except BaseException as error:
                        errors.append(error)
                def reader():
                    reader_started.set()
                    try:
                        results.append(self.library.annotations(reference) if kind == 'annotation' else self.library.events('concurrency'))
                    except BaseException as error:
                        errors.append(error)
                with patch.object(self.library.storage, 'append', paused):
                    a = threading.Thread(target=writer)
                    a.start()
                    self.assertTrue(committed.wait(5))
                    b = threading.Thread(target=reader)
                    b.start()
                    self.assertTrue(reader_started.wait(5))
                    b.join(.1)
                    release.set()
                    a.join(5)
                    b.join(5)
                self.assertFalse(a.is_alive() or b.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(results)


    def version(self, number):
        manifest = deepcopy(self.fixture.manifest)
        manifest['version'] = number
        return seal(manifest)

    def test_index_only_history_blocks_writes_and_recovery_instead_of_being_discarded(self):
        self.register()
        # Mimic a pre-durability writer on the same root: SQLite rows only.
        with patch.object(self.library.storage, 'append', lambda *a, **k: None), \
                patch.object(self.library.storage, 'check_projection', lambda *a, **k: None):
            orphan = self.library.register(self.version('2.0.0'), self.fixture.sources)
        with self.assertRaises(RigError) as raised:
            self.library.register(self.version('3.0.0'), self.fixture.sources)
        self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')
        with self.assertRaises(RigError):
            self.library.recover()
        self.assertIn(orphan, self.library.list_references())
        self.assertEqual(self.library.resolve(orphan)['version'], '2.0.0')

    def test_gc_refuses_while_index_only_version_depends_on_orphan_blobs(self):
        reference = self.register()
        storage = self.library.storage
        # An interrupted registration leaves canonical blobs with no commit.
        for f in self.fixture.files:
            path = storage.root / 'blobs' / f['sha256']
            payload = path.read_bytes()
            path.unlink()
            storage._publish(storage.root / 'blobs', f['sha256'], payload)
        commits = sorted((storage.root / 'commits').iterdir())
        stale_plan = self.library.analyze_gc()
        self.assertEqual(stale_plan['candidates'], [])
        for path in commits:
            path.unlink()
        # Commit records lost while the index survives: every blob is now
        # unreachable canonically but still required by an indexed version.
        for operation in (self.library.analyze_gc, lambda: self.library.apply_gc(stale_plan)):
            with self.subTest(operation=operation), self.assertRaises(RigError) as raised:
                operation()
            self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')
        for f in self.fixture.files:
            self.assertTrue((storage.root / 'blobs' / f['sha256']).exists())
        self.assertEqual(self.library.inspect(reference), self.fixture.manifest)

    def test_gc_refuses_index_only_version_sharing_uncommitted_blobs(self):
        # Interrupted durable registration: blobs published, no commit record.
        def interrupted(record, payloads=()):
            for digest, payload in dict(payloads).items():
                self.library.storage._publish(self.library.storage.root / 'blobs', digest, payload)
            raise OSError('interrupted after blob publication')
        with patch.object(self.library.storage, 'append', interrupted):
            with self.assertRaises(OSError):
                self.register()
        self.assertEqual(len(self.library.analyze_gc()['candidates']), len(self.fixture.files))
        # A pre-durability writer then records the same bytes in SQLite only.
        with patch.object(self.library.storage, 'append', lambda *a, **k: None), \
                patch.object(self.library.storage, 'check_projection', lambda *a, **k: None):
            other = self.library.register(self.version('9.0.0'), self.fixture.sources)
        with self.assertRaises(RigError) as raised:
            self.library.analyze_gc()
        self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')
        self.assertEqual(self.library.resolve(other)['version'], '9.0.0')

    def test_missing_canonical_blob_is_a_typed_durability_error(self):
        reference = self.register()
        (self.library.storage.root / 'blobs' / self.fixture.files[0]['sha256']).unlink()
        for operation in (lambda: self.library.resolve(reference),
                          lambda: self.library.inspect(reference),
                          self.library.recover):
            with self.subTest(operation=operation), self.assertRaises(RigError) as raised:
                operation()
            self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')

    def test_storage_filesystem_errors_are_typed(self):
        import errno
        from asset_library_storage import Storage
        reference = self.register()
        def denied(*_args, **_kwargs):
            raise OSError(errno.EIO, 'Input/output error')
        manifest = self.version('2.0.0')
        with patch.object(Path, 'iterdir', denied):
            with self.assertRaises(RigError) as raised:
                self.library.storage.append(dict(schema_version=1, kind='version', value=manifest))
        self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')
        orphan = b'Uncommitted unreachable payload'
        digest = hashlib.sha256(orphan).hexdigest()
        self.library.storage._publish(self.library.storage.root / 'blobs', digest, orphan)
        plan = self.library.analyze_gc()
        with patch.object(Path, 'unlink', denied):
            with self.assertRaises(RigError) as raised:
                self.library.apply_gc(plan)
        self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')
        # A failing staging cleanup must not replace the typed publish error.
        import asset_library_storage
        def full(_fd):
            raise OSError(errno.ENOSPC, 'No space left on device')
        with patch.object(asset_library_storage.os, 'fsync', full), patch.object(Path, 'unlink', denied):
            with self.assertRaises(RigError) as raised:
                self.library.storage._publish(self.library.storage.root / 'blobs', 'f' * 64, b'payload')
        self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')
        self.assertIsInstance(self.library.storage, Storage)
        self.assertEqual(self.library.resolve(reference), self.fixture.manifest)

    def test_crash_after_ready_before_flag_keeps_stale_handles_durable_and_upgrade_finishes(self):
        from asset_library_storage import Storage
        reference, (migrator, old_writer, _unused) = self.legacy_handles()
        original_ready = Storage.ready
        def crash_after_ready(storage):
            original_ready(storage)
            raise RuntimeError('simulated crash before durability flag commit')
        with patch.object(Storage, 'ready', crash_after_ready):
            with self.assertRaises(RuntimeError):
                migrator.upgrade_storage()
        with closing(sqlite3.connect(self.library.database)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='durability'").fetchone())
        newer = old_writer.register(self.version('2.0.0'), self.fixture.sources)
        self.assertIsNotNone(old_writer.storage)
        fresh = AssetLibrary(self.library.root)
        fresh.register(self.version('3.0.0'), self.fixture.sources)
        status = fresh.upgrade_storage()
        self.assertEqual(status['versions'], 3)
        with closing(sqlite3.connect(self.library.database)) as db:
            self.assertEqual(db.execute('SELECT canonical FROM durability').fetchall(), [(1,)])
        fresh.recover()
        self.assertEqual(fresh.resolve(newer)['version'], '2.0.0')
        self.assertEqual(fresh.resolve(reference), self.fixture.manifest)

    def test_index_only_annotation_and_event_rows_block_recovery(self):
        reference = self.register()
        with patch.object(self.library.storage, 'append', lambda *a, **k: None):
            self.library.update_annotations(reference, expected_revision=0, tags=['index-only'], favorite=False, note='only in SQLite')
        with self.assertRaises(RigError):
            self.library.recover()
        with closing(sqlite3.connect(self.library.database)) as db:
            self.assertEqual(db.execute('SELECT revision FROM annotations').fetchone(), (1,))
        other = foundation_fixture.AssetLibraryTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        other.register()
        with closing(sqlite3.connect(other.library.database)) as db, db:
            db.execute('INSERT INTO asset_events VALUES(?,?,?)', ('lifecycle', 1, canonical_bytes({'kind': 'pin'})))
        with self.assertRaises(RigError):
            other.library.recover()
        with closing(sqlite3.connect(other.library.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM asset_events').fetchone(), (1,))

    def test_upgrade_completes_layout_left_by_interrupted_migration(self):
        reference, (migrator, *_rest) = self.legacy_handles()
        (self.library.root / 'records').mkdir(mode=0o700)
        status = migrator.upgrade_storage()
        self.assertTrue(status['durable'])
        self.assertEqual(AssetLibrary(self.library.root).resolve(reference), self.fixture.manifest)

    def test_incomplete_layout_is_a_typed_durability_error(self):
        self.register()
        (self.library.storage.root / 'staging').rmdir()
        with self.assertRaises(RigError) as raised:
            AssetLibrary(self.library.root)
        self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')

    def test_disk_full_during_publication_is_typed_and_rolls_back(self):
        import errno
        import asset_library_storage
        def full(_fd):
            raise OSError(errno.ENOSPC, 'No space left on device')
        with patch.object(asset_library_storage.os, 'fsync', full):
            with self.assertRaises(RigError) as raised:
                self.register()
        self.assertEqual(raised.exception.report['code'], 'LIBRARY_DURABILITY')
        self.assertEqual(self.library.list_references(), [])
        self.assertEqual(list((self.library.storage.root / 'staging').iterdir()), [])

    def test_verified_reads_share_access_while_writer_lock_is_held(self):
        reference = self.register()
        with closing(sqlite3.connect(self.library.database, timeout=0)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            reader = AssetLibrary(self.library.root)
            self.assertEqual(reader.resolve(reference), self.fixture.manifest)
            self.assertEqual(reader.inspect(reference), self.fixture.manifest)
            self.assertEqual(reader.read_own_file(reference, 'mesh/triangle.obj'), self.fixture.mesh.read_bytes())
            writer.rollback()

    def test_verified_reads_work_on_read_only_database(self):
        reference = self.register()
        os.chmod(self.library.database, 0o400)
        self.addCleanup(os.chmod, self.library.database, 0o600)
        self.assertEqual(self.library.resolve(reference), self.fixture.manifest)
        self.assertEqual(self.library.read_file(reference, 'mesh/triangle.obj'), self.fixture.mesh.read_bytes())


if __name__ == '__main__':
    unittest.main()
