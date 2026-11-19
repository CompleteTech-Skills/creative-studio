"""Registered scene/owner contracts over real offline glTF and immutable I/O.

Fixed-worker claims deliberately use synthetic process receipts: Blender does
not run here. Real native normalization, Khronos checks, registration and public
consumers execute. The existing fixed-appearance suite owns the real Blender
producer/reopen control. No inherited test classes are rediscovered here.
"""
from copy import deepcopy
import hashlib
import json
import shutil
import unittest
from unittest.mock import patch

import numpy as np

from model3d_mesh_asset import build_asset
from model3d_mesh_import import import_canonical
from model3d_validation import validate_file
from rig_errors import RigError
from rig_gltf import GltfBuilder, read_gltf
from rig_interop import validate_gltf
import test_model3d_registered_provenance as fixtures
import test_model3d_registered_chain as chain_fixtures
from test_model3d_mesh_asset import record, provenance, source


PROFILES = ('alternate_scene', 'unused_mesh', 'unused_instance',
            'camera', 'unused_camera', 'light', 'unused_light')


def add_inventory(document, profile):
    """Author ordinary, standards-valid resources outside the fixed profile."""
    if profile == 'alternate_scene':
        extra = len(document['nodes'])
        document['nodes'].append(dict(mesh=0, translation=[4., 0., 0.]))
        document['scenes'].append(dict(name='Alternate authored scene', nodes=[0, extra]))
    elif profile == 'unused_mesh':
        document['meshes'].append(deepcopy(document['meshes'][0]))
    elif profile == 'unused_instance':
        document['nodes'].append(dict(mesh=0, translation=[4., 0., 0.]))
    elif profile in ('camera', 'unused_camera'):
        document['cameras'] = [dict(type='perspective',
            perspective=dict(yfov=0.8, znear=0.1, zfar=100.))]
        if profile == 'camera':
            document['scenes'][0]['nodes'].append(len(document['nodes']))
            document['nodes'].append(dict(camera=0, translation=[0., 0., 5.]))
    elif profile in ('light', 'unused_light'):
        document.setdefault('extensionsUsed', []).append('KHR_lights_punctual')
        document.setdefault('extensions', {})['KHR_lights_punctual'] = dict(
            lights=[dict(type='directional', color=[1., 0., 0.], intensity=2.)])
        if profile == 'light':
            document['scenes'][0]['nodes'].append(len(document['nodes']))
            document['nodes'].append(dict(extensions=dict(KHR_lights_punctual=dict(light=0))))
    else:
        raise AssertionError('Unknown authored inventory profile: ' + profile)


@unittest.skipUnless(
    shutil.which('node') and (fixtures.native_fixture.VALIDATOR / 'package.json').is_file(),
    'Requires the pinned offline Khronos glTF validator',
)
class RegisteredSceneContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RegisteredProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def register_scene(self, profile, *, blender_claim):
        original_export = fixtures.export_geometry

        def export(geometry, path, **kwargs):
            original_export(geometry, path, **kwargs)
            document = read_gltf(path)
            builder = GltfBuilder()
            builder.json = deepcopy(document.json)
            builder.binary = bytearray(document.buffers[0])
            add_inventory(builder.json, profile)
            builder.write(path)

        with patch.object(fixtures, 'export_geometry', export):
            if blender_claim:
                ref = self.fixture.synthetic_blender_claim()
            else:
                ref, _ = self.fixture.native_registration(material_free=True)
        # Setup checks are intentionally outside the rejection assertion. Both
        # actual GLBs must validate, and the authored inventory must survive the
        # real native packer before a consumer refusal can count as evidence.
        documents = []
        for name in ('source/model.glb', 'normalized.glb'):
            path = self.fixture.root / ('checked-' + name.replace('/', '-'))
            path.write_bytes(self.fixture.library.read_file(ref, name))
            checked = validate_gltf(path)
            self.assertTrue(checked['ok'], checked)
            self.assertEqual(checked['validator'], '2.0.0-dev.3.10')
            documents.append(read_gltf(path).json)
        original, normalized = documents
        for key in ('meshes', 'cameras', 'extensions', 'extensionsUsed'):
            self.assertEqual(normalized.get(key), original.get(key))
        self.assertEqual(normalized['nodes'][:len(original['nodes'])], original['nodes'])
        self.assertEqual(len(normalized['scenes']), len(original['scenes']))
        return ref, normalized

    def assert_fixed_claim_refused(self, profile):
        ref, _ = self.register_scene(profile, blender_claim=True)
        with self.assertRaises(RigError) as caught:
            with import_canonical(self.fixture.library, ref):
                pass
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

    def test_fixed_claim_rejects_alternate_scene_with_extra_instance(self):
        self.assert_fixed_claim_refused('alternate_scene')

    def test_fixed_claim_rejects_unused_mesh_resource(self):
        self.assert_fixed_claim_refused('unused_mesh')

    def test_fixed_claim_rejects_unused_mesh_instance(self):
        self.assert_fixed_claim_refused('unused_instance')

    def test_fixed_claim_rejects_active_camera(self):
        self.assert_fixed_claim_refused('camera')

    def test_fixed_claim_rejects_unused_camera_resource(self):
        self.assert_fixed_claim_refused('unused_camera')

    def test_fixed_claim_rejects_active_punctual_light(self):
        self.assert_fixed_claim_refused('light')

    def test_fixed_claim_rejects_unused_punctual_light_resource(self):
        self.assert_fixed_claim_refused('unused_light')

    def test_clean_synthetic_fixed_profile_still_imports(self):
        ref = self.fixture.synthetic_blender_claim()
        with import_canonical(self.fixture.library, ref) as imported:
            self.assertEqual(imported.provenance['canonical_ref'], ref)
            mesh, = imported.model.geometry.meshes
            np.testing.assert_allclose(mesh.positions, fixtures.FIXED_TRIANGLE, atol=1e-6, rtol=0)
            np.testing.assert_array_equal(mesh.triangles, [[0, 1, 2]])

    def test_native_authored_inventory_is_retained_without_fixed_worker_restrictions(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                fixture = fixtures.RegisteredProvenanceTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                self.fixture = fixture
                ref, expected = self.register_scene(profile, blender_claim=False)
                manifest = fixture.library.resolve(ref)
                before = {item['path']: fixture.library.read_file(ref, item['path'])
                          for item in manifest['files']}
                with import_canonical(fixture.library, ref) as imported:
                    self.assertEqual(imported.provenance['canonical_ref'], ref)
                    self.assertEqual(imported.model.geometry.triangle_count, 1)
                    self.assertEqual(read_gltf(imported.root / 'normalized.glb').json, expected)
                    for name, payload in before.items():
                        self.assertEqual((imported.root / name).read_bytes(), payload)
                self.assertEqual(fixture.library.resolve(ref), manifest)
                for name, payload in before.items():
                    self.assertEqual(fixture.library.read_file(ref, name), payload)


@unittest.skipUnless(
    shutil.which('node') and (fixtures.native_fixture.VALIDATOR / 'package.json').is_file(),
    'Requires the pinned offline Khronos glTF validator',
)
class RegisteredLegacyOwnerContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = chain_fixtures.RegisteredChainTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def prepare_distinct_runtime(self):
        root, ref, manifest = self.fixture.native_with_runtime_alias(different_bytes=True)
        inventory = {item['path']: item for item in manifest['files']}
        self.assertNotEqual(inventory['runtime/asset.glb']['sha256'], inventory['normalized.glb']['sha256'])
        self.assertTrue(manifest['provenance']['fixture'])
        self.assertEqual(manifest['provenance']['source_records'][0]['original_filename'], 'Géométrie.glb')
        with import_canonical(self.fixture.library, ref) as imported:
            self.assertEqual(imported.provenance['source_entry'], 'normalized.glb')
        with import_canonical(self.fixture.library, ref, entry='runtime/asset.glb') as imported:
            self.assertEqual(imported.provenance['source_entry'], 'runtime/asset.glb')
        canonical = root / 'canonical.json'
        canonical.write_text(json.dumps(dict(canonical_ref=ref, manifest=manifest)), encoding='utf-8')
        # Include every prior registered version and physical history byte. The
        # builder must neither rewrite source/native payloads nor registry data.
        roots = (root, self.fixture.library.root)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                  for directory in roots for path in directory.rglob('*') if path.is_file()}
        self.addCleanup(self.assert_files_unchanged, roots, before)
        return root, record(root, 'canonical.json', 'application/json')

    def assert_files_unchanged(self, roots, before):
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                 for directory in roots for path in directory.rglob('*') if path.is_file()
                 and path != roots[0] / 'asset.json'}
        self.assertEqual(after, before, 'Builder changed native/source/registry history bytes')

    def build_distinct_runtime(self, root, evidence, *, declared_name='Géométrie.glb',
                               declared_fixture=True, source_fixture=True):
        geometry = record(root, 'runtime/asset.glb', 'model/gltf-binary')
        retained = source(record(root, 'source/model.glb', 'model/gltf-binary'))
        retained['original_filename'] = declared_name
        retained['provenance'].update(origin='imported', fixture=source_fixture)
        supplied = provenance()
        supplied.update(origin='imported', fixture=declared_fixture)
        return build_asset(root, geometry, asset_id='distinct-legacy-runtime', version='1.0.0',
            category='prop', sources=[retained], provenance=supplied, canonical_evidence=evidence)

    def test_distinct_runtime_preserves_correct_registered_owner_positive(self):
        root, evidence = self.prepare_distinct_runtime()
        document = self.build_distinct_runtime(root, evidence)
        self.assertTrue(document['provenance']['fixture'])
        self.assertTrue(document['sources'][0]['provenance']['fixture'])
        self.assertEqual(document['sources'][0]['original_filename'], 'Géométrie.glb')
        (root / 'asset.json').write_text(json.dumps(document), encoding='utf-8')
        validated = validate_file(root / 'asset.json', root)
        self.assertTrue(validated['payloads_verified'])
        self.assertTrue(validated['geometry_inspected'])

    def assert_owner_refused(self, **kwargs):
        root, evidence = self.prepare_distinct_runtime()
        # Registry/import setup has completed before this expected rejection.
        with self.assertRaises(RigError) as caught:
            self.build_distinct_runtime(root, evidence, **kwargs)
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_PROVENANCE')

    def test_distinct_runtime_cannot_drop_owner_fixture_classification(self):
        self.assert_owner_refused(declared_fixture=False)

    def test_distinct_runtime_cannot_drop_original_source_fixture_classification(self):
        self.assert_owner_refused(source_fixture=False)

    def test_distinct_runtime_cannot_drop_both_fixture_classifications(self):
        self.assert_owner_refused(declared_fixture=False, source_fixture=False)

    def test_distinct_runtime_cannot_contradict_registered_original_filename(self):
        self.assert_owner_refused(declared_name='Contradictory.glb')


if __name__ == '__main__':
    unittest.main()
