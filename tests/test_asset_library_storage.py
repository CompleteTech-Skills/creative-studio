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


if __name__ == '__main__':
    unittest.main()
