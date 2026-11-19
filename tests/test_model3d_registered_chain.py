"""Independent registered-chain consistency tests over actual offline GLBs.

Synthetic Blender envelopes below are deliberately forged semantic claims. The
GLBs, registry publication, native packing and Khronos checks are real; these
tests do not execute Blender or authenticate a historical producer process.
The real fixed Blender producer/reopen control lives in the native import suite.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asset_library import file_record
from asset_library_contracts import seal
from model3d_mesh_asset import build_asset
from model3d_mesh_import import import_canonical
from model3d_validation import validate_file
from rig_errors import RigError
from rig_gltf import export_geometry, read_gltf
from rig_interop import validate_gltf
from rig_models import Geometry, Mesh
import numpy as np

import test_model3d_registered_provenance as fixture
from test_model3d_mesh_asset import record, provenance, source


@unittest.skipUnless(shutil.which('node') and
                     (fixture.native_fixture.VALIDATOR / 'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class RegisteredChainTests(unittest.TestCase):
    # Reuse fixture construction without rerunning inherited acceptance tests.
    setUp = fixture.RegisteredProvenanceTests.setUp
    native_registration = fixture.RegisteredProvenanceTests.native_registration
    synthetic_blender_claim = fixture.RegisteredProvenanceTests.synthetic_blender_claim

    def refuse_blender_receipt(self, mutate):
        ref = self.synthetic_blender_claim(mutate_report=mutate)
        with self.assertRaises(RigError) as caught:
            with import_canonical(self.library, ref):
                pass
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

    def test_complete_synthetic_receipts_preserve_expected_worker_and_both_stages(self):
        ref = self.synthetic_blender_claim()
        report = json.loads(self.library.read_file(ref, 'import-report.json'))['blender']
        worker = Path(__file__).resolve().parents[1] / 'scripts/asset_import_worker.py'
        self.assertEqual(report['worker_sha256'], hashlib.sha256(worker.read_bytes()).hexdigest())
        for mode in ('create', 'reopen'):
            self.assertEqual(report[mode]['status'], 'completed')
            self.assertEqual(report[mode]['exit_code'], 0)
            self.assertEqual(report[mode]['argv'][-1], mode)
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.provenance['canonical_ref'], ref)
            self.assertEqual(imported.model.geometry.triangle_count, 1)

    def test_missing_worker_digest_refuses(self):
        self.refuse_blender_receipt(lambda report: report.pop('worker_sha256'))

    def test_different_package_worker_digest_refuses(self):
        self.refuse_blender_receipt(lambda report: report.update(worker_sha256='0' * 64))

    def test_missing_create_stage_refuses(self):
        self.refuse_blender_receipt(lambda report: report.pop('create'))

    def test_missing_reopen_stage_refuses(self):
        self.refuse_blender_receipt(lambda report: report.pop('reopen'))

    def test_failed_create_stage_refuses(self):
        self.refuse_blender_receipt(lambda report: report['create'].update(status='failed'))

    def test_nonzero_reopen_exit_refuses(self):
        self.refuse_blender_receipt(lambda report: report['reopen'].update(exit_code=1))

    def test_reopen_command_cannot_claim_create_mode(self):
        def mutate(report):
            report['reopen']['argv'][-1] = 'create'
        self.refuse_blender_receipt(mutate)

    def test_peak_resident_measurement_cannot_exceed_declared_budget(self):
        def mutate(report):
            stage = report['reopen']
            stage['peak_rss_bytes'] = stage['resident_budget_bytes'] + 1
        self.refuse_blender_receipt(mutate)

    def test_virtual_limit_must_agree_with_fixed_command(self):
        self.refuse_blender_receipt(lambda report: report['create'].update(virtual_limit_bytes=1))

    def test_default_morph_cannot_masquerade_as_static_blender_fixture(self):
        ref = self.synthetic_blender_claim(deformation='morph')
        encoded = self.root / 'checked-morph.glb'
        encoded.write_bytes(self.library.read_file(ref, 'normalized.glb'))
        native = read_gltf(encoded)
        mesh, = native.json['meshes']
        self.assertEqual(mesh['weights'], [1.])
        target = mesh['primitives'][0]['targets'][0]['POSITION']
        deltas = native.accessor(target)
        np.testing.assert_array_equal(deltas, np.asarray(((0., 0., 1.),) * 3))
        # The adversary has exactly the fixed base coordinates/winding, while
        # the standards-valid default morph visibly displaces every vertex.
        base = native.accessor(mesh['primitives'][0]['attributes']['POSITION'])
        np.testing.assert_array_equal(base, np.asarray(fixture.FIXED_TRIANGLE))
        with self.assertRaises(RigError) as caught:
            with import_canonical(self.library, ref):
                pass
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

    def test_encoded_animation_cannot_masquerade_as_static_blender_fixture(self):
        ref = self.synthetic_blender_claim(deformation='animation')
        encoded = self.root / 'checked-animation.glb'
        encoded.write_bytes(self.library.read_file(ref, 'normalized.glb'))
        native = read_gltf(encoded)
        channel, = native.json['animations'][0]['channels']
        self.assertEqual(channel['target']['path'], 'translation')
        with self.assertRaises(RigError) as caught:
            with import_canonical(self.library, ref):
                pass
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

    def test_encoded_skin_cannot_masquerade_as_static_blender_fixture(self):
        ref = self.synthetic_blender_claim(deformation='skin')
        encoded = self.root / 'checked-skin.glb'
        encoded.write_bytes(self.library.read_file(ref, 'normalized.glb'))
        native = read_gltf(encoded)
        self.assertEqual(len(native.json['skins']), 1)
        self.assertEqual(native.json['nodes'][0]['skin'], 0)
        attributes = native.json['meshes'][0]['primitives'][0]['attributes']
        self.assertIn('JOINTS_0', attributes)
        self.assertIn('WEIGHTS_0', attributes)
        with self.assertRaises(RigError) as caught:
            with import_canonical(self.library, ref):
                pass
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

    def test_native_morph_remains_a_genuine_positive(self):
        ref, _ = self.native_registration(deformation='morph')
        with import_canonical(self.library, ref) as imported:
            mesh, = imported.model.geometry.meshes
            self.assertTrue(mesh.morphs)
            self.assertIn(1., mesh.morph_defaults.values())

    def test_native_animation_remains_a_genuine_positive(self):
        ref, _ = self.native_registration(deformation='animation')
        with import_canonical(self.library, ref) as imported:
            self.assertTrue(imported.model.clips)

    def native_with_runtime_alias(self, *, different_bytes=False):
        ref, manifest = self.native_registration(original_name='Géométrie.glb',
                                                  embedded_fixture=False)
        root = self.root / 'dual-entry'
        root.mkdir()
        paths = {}
        for item in manifest['files']:
            path = root / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.library.read_file(ref, item['path']))
            paths[item['path']] = path
        alias = root / 'runtime/asset.glb'
        alias.parent.mkdir()
        if different_bytes:
            geometry = Geometry([Mesh('other', np.asarray(((0., 0., 0.),
                (3., 0., 0.), (0., 1., 0.))), np.asarray(((0, 1, 2),)))], [{}])
            export_geometry(geometry, alias)
        else:
            alias.write_bytes(paths['normalized.glb'].read_bytes())
        checked = validate_gltf(alias)
        self.assertTrue(checked['ok'], checked)
        self.assertEqual(checked['validator'], '2.0.0-dev.3.10')
        paths['runtime/asset.glb'] = alias
        manifest['files'].append(file_record(alias, 'runtime/asset.glb', 'payload'))
        manifest['publication']['inventory'].append('runtime/asset.glb')
        manifest['version'] = '1.2.0'
        manifest = seal(manifest)
        registered = self.library.register(manifest, paths)
        self.assertEqual(self.library.resolve(registered), manifest)
        self.assertIn('normalized.glb', paths)
        self.assertEqual(alias.read_bytes() == paths['normalized.glb'].read_bytes(),
                         not different_bytes)
        return root, registered, manifest

    def build_native_alias(self, *, different_bytes=False, declared_name='Géométrie.glb',
                           declared_fixture=True, source_fixture=True):
        root, ref, manifest = self.native_with_runtime_alias(different_bytes=different_bytes)
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.model.source_files, [imported.root / 'normalized.glb'])
        geometry = record(root, 'normalized.glb', 'model/gltf-binary')
        original = record(root, 'source/model.glb', 'model/gltf-binary')
        retained = source(original)
        retained['original_filename'] = declared_name
        retained['provenance']['origin'] = 'imported'
        retained['provenance']['fixture'] = source_fixture
        supplied = provenance()
        supplied['origin'] = 'imported'
        supplied['fixture'] = declared_fixture
        canonical = root / 'canonical.json'
        canonical.write_text(json.dumps(dict(canonical_ref=ref, manifest=manifest)), encoding='utf-8')
        evidence = record(root, 'canonical.json', 'application/json')
        document = build_asset(root, geometry, asset_id='native-dual-entry', version='1.0.0',
            category='prop', sources=[retained], provenance=supplied, canonical_evidence=evidence)
        (root / 'asset.json').write_text(json.dumps(document), encoding='utf-8')
        checked = validate_file(root / 'asset.json', root)
        self.assertTrue(checked['payloads_verified'])
        self.assertTrue(checked['geometry_inspected'])
        return document

    def test_same_byte_runtime_alias_preserves_native_positive(self):
        document = self.build_native_alias()
        self.assertEqual(document['sources'][0]['original_filename'], 'Géométrie.glb')
        self.assertTrue(document['provenance']['fixture'])

    def test_same_byte_runtime_alias_cannot_suppress_original_filename(self):
        try:
            document = self.build_native_alias(declared_name='Contradictory.glb')
        except RigError as error:
            self.assertIn(error.report['code'], ('MODEL3D_PROVENANCE', 'MODEL3D_REFERENCE',
                                                  'MODEL3D_SOURCE'))
        else:
            self.assertEqual(document['sources'][0]['original_filename'], 'Géométrie.glb',
                             'Runtime alias suppressed exact registered native original-name evidence')

    def test_same_byte_runtime_alias_cannot_remove_registered_fixture_claim(self):
        with self.assertRaises(RigError) as caught:
            self.build_native_alias(declared_fixture=False, source_fixture=False)
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_PROVENANCE')

    def test_same_byte_runtime_alias_cannot_remove_source_fixture_claim(self):
        with self.assertRaises(RigError) as caught:
            self.build_native_alias(source_fixture=False)
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_PROVENANCE')

    def test_different_byte_runtime_alias_preserves_verified_native_positive(self):
        document = self.build_native_alias(different_bytes=True)
        self.assertEqual(document['sources'][0]['original_filename'], 'Géométrie.glb')
        self.assertEqual(document['measurements']['vertices'], 3)


if __name__ == '__main__':
    unittest.main()
