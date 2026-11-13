"""Exact asset duplication, canonical closure and publication recovery."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from asset_library import AssetLibrary
from asset_library_contracts import seal
from asset_library_versions import AssetVersions
from rig_errors import RigError
import test_asset_library_versions as versions_fixture

ROOT = Path(__file__).resolve().parents[1]


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = versions_fixture.VersionTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.library = self.fixture.library
        self.versions = self.fixture.versions
        self.source = self.fixture.initial()

    def identity(self, asset_id='triangle-copy'):
        identity = deepcopy(self.fixture.manifest['identity'])
        identity.update(id=asset_id, slug=asset_id, name='Independent duplicate',
                        created_at='2026-11-13T00:00:00Z')
        return identity

    def test_exact_payload_and_required_closure_retain_provenance_without_selection(self):
        dependency = deepcopy(self.fixture.manifest)
        dependency['identity'] = self.identity('dependency')
        dependency = seal(dependency)
        target = self.library.register(dependency, self.fixture.sources)
        source = self.fixture.newer(self.source)
        source['dependencies'] = [dict(target=target, purpose='Retained mesh', required=True,
            origin='authored', relationship='direct', capabilities=[])]
        source['publication']['integration'] = 'native-reference'
        source = seal(source)
        reference = self.versions.create(source, self.fixture.sources, previous=self.source)
        self.versions.promote(reference, expected_revision=0)
        self.library.update_annotations(reference, expected_revision=0, tags=['old'], favorite=True, note='Prior review')
        duplicate = self.versions.duplicate(reference, identity=self.identity(), version='2.0.0')
        result = self.library.resolve(duplicate)
        self.assertEqual(result['files'], source['files'])
        self.assertEqual(result['dependencies'], source['dependencies'])
        self.assertEqual(result['provenance']['sources'], source['provenance']['sources'])
        self.assertEqual(result['provenance']['license_evidence'], source['provenance']['license_evidence'])
        self.assertEqual(result['metadata']['license'], source['metadata']['license'])
        self.assertEqual(result['metadata']['allowed_uses'], source['metadata']['allowed_uses'])
        self.assertEqual(result['metadata']['author'], source['metadata']['author'])
        self.assertTrue(result['provenance']['fixture'])
        self.assertEqual(result['provenance']['method'], 'fixture')
        self.assertIn(reference, result['provenance']['parents'])
        self.assertEqual(result['publication']['integration'], 'not-exercised')
        self.assertEqual(result['metadata']['review_status'], 'unreviewed')
        self.assertEqual(result['previews'], [])
        self.assertIsNone(self.versions.state(duplicate['id'])['promoted'])
        self.assertEqual(self.library.annotations(duplicate)['tags'], [])
        self.assertFalse(self.library.annotations(duplicate)['favorite'])
        for record in result['files']:
            original = self.library.read_file(reference, record['path'])
            copied = self.library.read_file(duplicate, record['path'])
            self.assertEqual(copied, original)
            self.assertEqual(hashlib.sha256(copied).hexdigest(), record['sha256'])
        self.assertEqual(self.library.resolve(target), dependency)
        self.assertEqual(self.library.resolve(reference), source)

    def test_new_identity_cannot_change_native_kind_or_attribution(self):
        for identity in [self.fixture.manifest['identity'], self.identity() | {'kind': 'rig'},
                         self.identity() | {'subtype': 'other'}, self.identity() | {'attribution': 'Someone else'}]:
            with self.subTest(identity=identity):
                with self.assertRaises(RigError): self.versions.duplicate(self.source, identity=identity)
        self.assertEqual(len(self.library.list_references()), 1)

    def test_existing_or_concurrently_created_identity_cannot_be_overwritten(self):
        identity = self.identity()
        first = self.versions.duplicate(self.source, identity=identity)
        with self.assertRaises(RigError): self.versions.duplicate(self.source, identity=identity, version='3.0.0')
        self.assertEqual(self.library.resolve(first)['version'], '1.0.0')
        barrier = threading.Barrier(2); outcomes = []
        def writer():
            versions = AssetVersions(AssetLibrary(self.library.root))
            original = versions.history
            def history(asset_id):
                result = original(asset_id); barrier.wait(timeout=10); return result
            try:
                with patch.object(versions, 'history', side_effect=history):
                    versions.duplicate(self.source, identity=self.identity('race-copy'))
                outcomes.append('ok')
            except RigError: outcomes.append('conflict')
        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ['ok', 'conflict'])

    def test_damaged_required_source_closure_rejects_before_new_publication(self):
        record = self.fixture.manifest['files'][0]
        with self.library._connect() as db:
            self.library._begin_writer(db)
            db.execute('UPDATE blobs SET payload=? WHERE sha256=?', (b'broken', record['sha256']))
        with self.assertRaises(RigError): self.versions.duplicate(self.source, identity=self.identity())
        self.assertEqual(len(self.library.list_references()), 1)

    def test_real_process_exit_after_canonical_commit_recovers_complete_duplicate(self):
        script = '''
import json, os, sys
from asset_library import AssetLibrary
from asset_library_versions import AssetVersions
library = AssetLibrary(sys.argv[1])
original = library.storage.append
def crash(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(23)
library.storage.append = crash
AssetVersions(library).duplicate(json.loads(sys.argv[2]), identity=json.loads(sys.argv[3]))
'''
        temporary = self.fixture.root / 'crash-staging'; temporary.mkdir()
        env = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        result = subprocess.run([sys.executable, '-c', script, str(self.library.root),
            json.dumps(self.source), json.dumps(self.identity())], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        reopened = AssetLibrary(self.library.root)
        reopened.recover()
        copied = next(ref for ref in reopened.list_references() if ref['id'] == 'triangle-copy')
        manifest = reopened.resolve(copied)
        self.assertIn(self.source, manifest['provenance']['parents'])
        for item in manifest['files']:
            self.assertEqual(reopened.read_file(copied, item['path']), self.library.read_file(self.source, item['path']))
        with self.assertRaises(RigError): AssetVersions(reopened).duplicate(self.source, identity=self.identity())


if __name__ == '__main__': unittest.main()
