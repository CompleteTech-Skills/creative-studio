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
import time
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

    def test_canonical_publish_rejects_changed_admitted_bytes_without_index_commit(self):
        import asset_library_storage as storage
        actual = storage.publish_verified_file
        def corrupt_source(source_fd, destination, **kwargs):
            os.lseek(source_fd, 0, os.SEEK_SET)
            os.write(source_fd, b'changed admitted bytes')
            return actual(source_fd, destination, **kwargs)
        with patch.object(storage, 'publish_verified_file', side_effect=corrupt_source):
            with self.assertRaises(RigError) as caught:
                self.register()
        self.assertEqual(caught.exception.report['code'], 'LIBRARY_DURABILITY')
        self.assertEqual(self.library.list_references(), [])
        self.assertEqual(list((self.library.storage.root/'blobs').glob('[0-9a-f]'*64)), [])
        self.assertEqual(list((self.library.storage.root/'commits').iterdir()), [])
        # Canonical ownership policy retains empty wrappers in mutable staging,
        # while immutable namespaces remain empty after failed admission.
        for wrapper in (self.library.storage.root/'staging').iterdir():
            self.assertTrue(wrapper.is_dir())
            self.assertFalse(wrapper.is_symlink())
            self.assertEqual(list(wrapper.iterdir()), [])

    def test_canonical_file_cleanup_failure_retains_commit_for_explicit_recovery(self):
        import asset_library_storage as storage
        actual = storage.publish_verified_file
        def interrupt_after_commit(source_fd, destination, **kwargs):
            receipt = actual(source_fd, destination, **kwargs)
            if Path(destination).parent.name == 'commits':
                raise storage.VerifiedFileCleanupError(receipt)
            return receipt
        with patch.object(storage, 'publish_verified_file', side_effect=interrupt_after_commit):
            with self.assertRaises(RigError) as caught:
                self.register()
        report = caught.exception.report
        self.assertEqual(report['code'], 'LIBRARY_PUBLICATION_CLEANUP')
        self.assertTrue(report['observed']['file_committed'])
        retained = Path(report['preserved'])
        self.assertEqual(hashlib.sha256(retained.read_bytes()).hexdigest(), report['observed']['receipt']['sha256'])
        self.assertEqual(self.library.list_references(), [])
        result = self.library.recover()
        self.assertEqual(result['versions'], 1)
        reference, = self.library.list_references()
        self.assertEqual(self.library.resolve(reference), self.fixture.manifest)

    @unittest.skipUnless(os.name == 'posix', 'Actual POSIX descriptor namespace interleaving.')
    def test_canonical_publish_preserves_foreign_stage_replacements(self):
        import atomic_publish
        actual = atomic_publish.rename_no_replace_at
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                payload = b'immutable native publication ' + str(interrupt).encode()
                digest = hashlib.sha256(payload).hexdigest()
                destination = self.library.storage.root/'blobs'/digest
                held = []
                def replace_outer(source_fd, source_name, destination_fd, destination_name):
                    identity = os.fstat(source_fd)
                    stage_root = self.library.storage.root/'staging'
                    outer = next(path.name for path in stage_root.iterdir()
                        if path.name.startswith('.file-') and
                        (lambda info: (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino))(
                            path.stat(follow_symlinks=False)))
                    wrapper = stage_root/outer
                    preserved = wrapper.with_name(wrapper.name+'-retained')
                    wrapper.rename(preserved)
                    wrapper.mkdir(mode=0o700)
                    sentinel = wrapper/'foreign-sentinel'
                    sentinel.write_bytes(b'foreign replacement must survive')
                    held.append((preserved, sentinel))
                    if interrupt:
                        raise OSError('injected publication interruption')
                    return actual(source_fd, source_name, destination_fd, destination_name)
                with patch.object(atomic_publish, 'rename_no_replace_at', side_effect=replace_outer):
                    if interrupt:
                        with self.assertRaises(RigError):
                            self.library.storage._publish(destination.parent, digest, payload)
                    else:
                        self.library.storage._publish(destination.parent, digest, payload)
                preserved, sentinel = held[0]
                self.assertEqual(sentinel.read_bytes(), b'foreign replacement must survive')
                self.assertEqual(list(preserved.iterdir()), [])
                if interrupt:
                    self.assertFalse(destination.exists())
                else:
                    self.assertEqual(destination.read_bytes(), payload)

    def test_canonical_publish_preserves_a_racing_foreign_destination(self):
        import asset_library_storage as storage
        actual = storage.publish_verified_file
        payload = b'admitted bytes'
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.library.storage.root/'blobs'/digest
        def race(source_fd, target, **kwargs):
            Path(target).write_bytes(b'foreign destination')
            return actual(source_fd, target, **kwargs)
        with patch.object(storage, 'publish_verified_file', side_effect=race):
            with self.assertRaises(RigError):
                self.library.storage._publish(destination.parent, digest, payload)
        self.assertEqual(destination.read_bytes(), b'foreign destination')

    def test_two_process_document_retries_publish_one_exact_canonical_version(self):
        code = '''
import json, sys, time
from pathlib import Path
from asset_library import AssetLibrary
from asset_library_documents import DocumentPublisher
root, ready, gate = map(Path, sys.argv[1:])
publisher = DocumentPublisher(AssetLibrary(root))
ready.write_text('ready')
deadline = time.monotonic() + 20
while not gate.exists():
    if time.monotonic() > deadline: raise RuntimeError('publication gate timed out')
    time.sleep(.01)
reference = publisher.publish_document(b'{"fixture":"two-process-publication"}',
    'two-process', 'director-project', 1, '1.0.0', [], True)
print(json.dumps(reference))
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'scripts'))
        gate = self.root/'publication-go'
        ready = [self.root/f'publisher-{index}.ready' for index in range(2)]
        workers = [subprocess.Popen([sys.executable, '-c', code, str(self.library.root),
                    str(marker), str(gate)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env) for marker in ready]
        try:
            deadline = time.monotonic() + 20
            while not all(marker.exists() for marker in ready) and time.monotonic() < deadline:
                if any(worker.poll() is not None for worker in workers):
                    break
                time.sleep(.01)
            self.assertTrue(all(marker.exists() for marker in ready), 'Both actual publishers must reach the gate.')
            gate.write_text('publish')
            references = []
            for worker in workers:
                output, error = worker.communicate(timeout=30)
                self.assertEqual(worker.returncode, 0, error)
                references.append(json.loads(output))
            self.assertEqual(references[0], references[1])
            reopened = AssetLibrary(self.library.root)
            self.assertEqual(len(reopened.list_references()), 1)
            from asset_library_documents import DocumentPublisher
            self.assertEqual(DocumentPublisher(reopened).read_ref(references[0]),
                             b'{"fixture":"two-process-publication"}')
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate(timeout=5)

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


if __name__ == '__main__':
    unittest.main()
