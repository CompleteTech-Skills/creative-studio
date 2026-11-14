"""Actual canonical document bytes, immutable identities, and hostile inputs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from asset_library import AssetLibrary
from asset_library_documents import DocumentPublisher
from rig_errors import RigError


class DocumentPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.publisher = DocumentPublisher(self.library, artifact_root=self.root)

    def publish(self, payload=b'{"kind":"director-project","id":"project"}', **kwargs):
        args = dict(artifact_id='project', kind='director-project', schema_version=1,
                    semantic_version='1.0.0', dependencies=[], fixture=True)
        args.update(kwargs)
        return self.publisher.publish_document(payload, **args)

    def test_actual_registration_restart_root_relative_path_and_readback(self):
        data = b'{"kind":"director-project","id":"project"}'
        ref = self.publish(data)
        self.assertEqual(ref['path'], 'library/records/blobs/' + hashlib.sha256(data).hexdigest())
        self.assertEqual((self.root / ref['path']).read_bytes(), data)
        reopened = DocumentPublisher(AssetLibrary(self.library.root), artifact_root=self.root)
        self.assertEqual(reopened.read_ref(ref), data)
        registered, = self.library.list_references()
        manifest = self.library.resolve(registered)
        self.assertEqual(manifest['identity']['kind'], 'document')
        self.assertEqual(manifest['identity']['subtype'], ref['kind'])
        self.assertEqual(self.library.read_file(registered, 'document.payload'), data)
        meta = json.loads(self.library.read_file(registered, 'document-metadata.json'))
        self.assertEqual(meta['artifact'], ref)
        self.assertEqual(manifest['provenance']['sources'][0]['sha256'], ref['sha256'])
        self.assertEqual(manifest['provenance']['fixture'], True)
        self.assertEqual(manifest['publication']['integration'], 'not-exercised')
        self.assertEqual(self.library.annotations(registered)['revision'], 0)
        self.assertEqual(self.library.events('editor-history'), [])

    def test_idempotent_and_every_ref_metadata_conflict(self):
        ref = self.publish()
        self.assertEqual(self.publish(), ref)
        for kwargs in (dict(payload=b'{"new":true}'), dict(kind='director-patch'),
                       dict(schema_version=2), dict(fixture=False)):
            with self.subTest(kwargs=kwargs), self.assertRaises(RigError) as caught:
                self.publish(**kwargs)
            self.assertEqual(caught.exception.report['code'], 'LIBRARY_DOCUMENT_CONFLICT')
        self.assertEqual(len(self.library.list_references()), 1)

    def test_exact_registered_dependency_closure_and_mismatch_rejection(self):
        parent = self.publish()
        child = self.publish(b'{"revision":2}', artifact_id='Project:revision.2', dependencies=[parent])
        self.assertEqual(child['dependencies'], [parent])
        manifests = [self.library.resolve(r) for r in self.library.list_references()]
        nested = next(m for m in manifests if m['metadata']['category'] == 'document' and m['dependencies'])
        dep = nested['dependencies'][0]['target']
        self.assertEqual(self.library.read_file(dep, 'document.payload'), self.publisher.read_ref(parent))
        wrong = {**parent, 'sha256': '0'*64}
        with self.assertRaises(RigError):
            self.publish(b'{"bad":2}', artifact_id='bad', dependencies=[wrong])
        with self.assertRaises(RigError):
            self.publish(b'{"bad":3}', artifact_id='bad', dependencies=[parent, parent])
        self.assertEqual(len(self.library.list_references()), 2)

    def test_actual_concurrent_idempotency_and_conflicting_payloads(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            refs = list(pool.map(lambda _: self.publish(), range(2)))
        self.assertEqual(refs[0], refs[1])
        def publish(data):
            try:
                return self.publish(data, artifact_id='race')
            except RigError as error:
                return error.report['code']
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(publish, (b'{"a":1}', b'{"a":2}')))
        self.assertEqual(sum(isinstance(value, dict) for value in outcomes), 1)
        self.assertIn('LIBRARY_DOCUMENT_CONFLICT', outcomes)

    def test_stale_history_does_not_register_or_activate(self):
        register = self.library.register
        def race(manifest, sources, **kwargs):
            self.publish(b'{"winner":true}', semantic_version='2.0.0')
            return register(manifest, sources, **kwargs)
        # Race only the outer registration; the nested publication uses real API.
        def once(manifest, sources, **kwargs):
            with patch.object(self.library, 'register', register):
                return race(manifest, sources, **kwargs)
        with patch.object(self.library, 'register', once), self.assertRaises(RigError) as caught:
            self.publish()
        self.assertEqual(caught.exception.report['code'], 'LIBRARY_DOCUMENT_CONFLICT')
        refs = self.library.list_references()
        self.assertEqual([r['version'] for r in refs], ['2.0.0'])
        self.assertEqual(self.library.events('editor-history'), [])

    def test_bounds_malformed_and_path_alias_rejection_before_registration(self):
        cases = [dict(payload=b''), dict(payload='{}'), dict(schema_version='1'),
                 dict(schema_version=True), dict(fixture='true'), dict(semantic_version='latest'),
                 dict(dependencies={}), dict(dependencies=[{}]), dict(artifact_id='../escape')]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(RigError):
                self.publish(**kwargs)
        with patch('asset_library_documents.MAX_FILE_BYTES', 4), self.assertRaises(RigError):
            self.publish(b'12345')
        ref = self.publish()
        for path in ('../escape', 'C:/escape', 'library/records/blobs/CON', ref['path'].upper()):
            with self.subTest(path=path), self.assertRaises(RigError):
                self.publisher.read_ref({**ref, 'path': path})
        self.assertEqual(len(self.library.list_references()), 1)

    def test_source_blob_symlink_and_root_replacement_fail_closed(self):
        ref = self.publish()
        blob = self.root / ref['path']
        content = blob.read_bytes()
        target = self.root / 'outside'
        target.write_bytes(content)
        blob.unlink()
        blob.symlink_to(target)
        with self.assertRaises(RigError):
            self.publisher.read_ref(ref)
        blob.unlink()
        blob.write_bytes(content)
        self.library.root.rename(self.root / 'old-library')
        AssetLibrary(self.root / 'library', create=True)
        with self.assertRaises(RigError):
            self.publish(b'{"replacement":true}', artifact_id='other')

    def test_no_optional_3d_imports(self):
        code = """
import sys
from asset_library import AssetLibrary
from asset_library_documents import DocumentPublisher
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as temp:
    lib=AssetLibrary(Path(temp)/'library',create=True)
    DocumentPublisher(lib).publish_document(b'{}','project','director-project',1,'1.0.0',[],True)
assert not {'numpy','scipy','PIL','trimesh','bpy'} & sys.modules.keys()
"""
        subprocess.run([sys.executable, '-c', code], check=True)
