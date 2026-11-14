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
import threading
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

    def test_explicit_native_owner_bridge_requires_actual_canonical_payload(self):
        from test_asset_library import AssetLibraryTests
        owner = AssetLibraryTests('test_actual_geometry_registration_restart_relocation_and_exact_bytes')
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        target = self.library.register(owner.manifest, owner.sources)
        file = owner.manifest['files'][0]
        native = dict(artifact_id='triangle', kind='prop', schema_version=1, semantic_version='1.0.0',
            path='library/records/blobs/' + file['sha256'], sha256=file['sha256'], byte_size=file['bytes'],
            media_type=file['mime_type'], provenance_ref=None, fixture=True, dependencies=[])
        with self.assertRaises(RigError):
            self.publish(dependencies=[native])
        def resolve(ref):
            if ref != native:
                raise RigError('NATIVE_EXACT_REF', 'Native identity differs from registered source.')
            return target
        publisher = DocumentPublisher(self.library, artifact_root=self.root, native_dependency_resolver=resolve)
        ref = publisher.publish_document(b'{"native":"triangle"}', 'project', 'director-project', 1, '1.0.0', [native], True)
        self.assertEqual(publisher.read_ref(native), owner.mesh.read_bytes())
        manifest = self.library.resolve(next(r for r in self.library.list_references() if r['id'] != 'triangle'))
        self.assertEqual(manifest['dependencies'][0]['target'], target)
        self.assertEqual(ref['dependencies'], [native])
        with self.assertRaises(RigError):
            publisher.publish_document(b'{}', 'bad', 'director-project', 1, '1.0.0', [{**native, 'kind':'other'}], True)
        bad = DocumentPublisher(self.library, artifact_root=self.root, native_dependency_resolver=lambda ref: target)
        with self.assertRaises(RigError):
            bad.publish_document(b'{}', 'bad', 'director-project', 1, '1.0.0', [{**native, 'sha256':'0'*64}], True)
        unrelated = self.publish(b'{"unrelated":true}', artifact_id='unrelated')
        self.assertEqual(self.publisher.read_ref(unrelated), b'{"unrelated":true}')

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

    def test_identical_blob_different_refs_rejected_including_cross_identity_race(self):
        first = self.publish(b'{}', artifact_id='first')
        with self.assertRaises(RigError) as caught:
            self.publish(b'{}', artifact_id='second')
        self.assertEqual(caught.exception.report['code'], 'LIBRARY_DOCUMENT_CONFLICT')
        self.assertEqual(self.publisher.read_ref(first), b'{}')
        original = self.library.register
        barrier = threading.Barrier(2)
        def register(*args, **kwargs):
            barrier.wait(timeout=10)
            return original(*args, **kwargs)
        def publish(artifact_id):
            try:
                return self.publish(b'{"race":true}', artifact_id=artifact_id)
            except RigError as error:
                return error.report['code']
        with patch.object(self.library, 'register', register), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, ('race-a', 'race-b')))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('LIBRARY_DOCUMENT_CONFLICT', results)
        self.assertEqual(len(self.library.list_references()), 2)
        self.assertEqual(self.library.events('editor-history'), [])

    def test_delayed_alias_writer_cannot_poison_successful_publication(self):
        original = self.library.register
        waiting, release = threading.Event(), threading.Event()
        def register(manifest, sources, **kwargs):
            if manifest['identity']['name'] == 'late':
                waiting.set()
                if not release.wait(timeout=10):
                    raise AssertionError('Late writer was not released.')
            return original(manifest, sources, **kwargs)
        def late():
            try:
                return self.publish(b'{}', artifact_id='late')
            except RigError as error:
                return error.report['code']
        with patch.object(self.library, 'register', register), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(late)
            self.assertTrue(waiting.wait(timeout=10))
            first = self.publish(b'{}', artifact_id='first')
            release.set()
            self.assertEqual(result.result(timeout=10), 'LIBRARY_DOCUMENT_CONFLICT')
        self.assertEqual(self.publisher.read_ref(first), b'{}')
        self.assertEqual(self.publish(b'{}', artifact_id='first'), first)
        self.assertEqual(len(self.library.list_references()), 1)

    def test_native_sibling_or_self_path_aliases_fail_before_owner_resolution(self):
        native = dict(artifact_id='native', kind='prop', schema_version=1, semantic_version='1.0.0',
            path='library/native/source.json', sha256=hashlib.sha256(b'{}').hexdigest(), byte_size=2,
            media_type='application/json', provenance_ref=None, fixture=True, dependencies=[])
        owner = unittest.mock.Mock(side_effect=AssertionError('Aliased request must fail before native owner invocation.'))
        publisher = DocumentPublisher(self.library, artifact_root=self.root, native_dependency_resolver=owner)
        for dependencies in ([native, {**native, 'artifact_id':'other'}],
                             [{**native, 'path':'library/records/blobs/' + native['sha256']}],
                             [native, {**native, 'artifact_id':'other', 'path':native['path'].upper()}]):
            with self.subTest(dependencies=dependencies), self.assertRaises(RigError):
                publisher.publish_document(b'{}', 'project', 'director-project', 1, '1.0.0', dependencies, True)
        owner.assert_not_called()
        self.assertEqual(self.library.list_references(), [])

    def test_raw_canonical_document_malformed_metadata_fails_typed(self):
        from asset_library_contracts import seal
        from asset_library_documents import _identity
        ref = self.publish()
        existing, = self.library.list_references()
        manifest = deepcopy(self.library.resolve(existing))
        manifest['identity'].update(id=_identity('malformed'), slug=_identity('malformed'), name='malformed')
        bad_metadata = b'{}'
        record = manifest['files'][1]
        record.update(sha256=hashlib.sha256(bad_metadata).hexdigest(), bytes=len(bad_metadata))
        manifest['provenance']['license_evidence'] = [deepcopy(record)]
        bad_source = b'{"malformed-source":true}'
        manifest['files'][0].update(sha256=hashlib.sha256(bad_source).hexdigest(), bytes=len(bad_source))
        manifest['provenance']['sources'] = [deepcopy(manifest['files'][0])]
        payloads = {'document.payload': bad_source, 'document-metadata.json': bad_metadata}
        sources = {}
        for name, payload in payloads.items():
            path = self.root / name
            path.write_bytes(payload)
            sources[name] = path
        self.library.register(seal(manifest), sources)
        with self.assertRaises(RigError) as caught:
            self.publisher.read_ref({**ref, 'artifact_id':'malformed'})
        self.assertEqual(caught.exception.report['code'], 'LIBRARY_DOCUMENT_METADATA')
        self.assertEqual(self.publisher.read_ref(ref), b'{"kind":"director-project","id":"project"}')
        self.assertEqual(self.publish(), ref)

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
import threading
from pathlib import Path
with tempfile.TemporaryDirectory() as temp:
    lib=AssetLibrary(Path(temp)/'library',create=True)
    DocumentPublisher(lib).publish_document(b'{}','project','director-project',1,'1.0.0',[],True)
assert not {'numpy','scipy','PIL','trimesh','bpy'} & sys.modules.keys()
"""
        subprocess.run([sys.executable, '-c', code], check=True)
