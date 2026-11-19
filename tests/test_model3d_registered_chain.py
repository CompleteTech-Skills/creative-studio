"""Independent registered-chain consistency tests over actual offline GLBs.

Synthetic Blender envelopes below are deliberately forged semantic claims. The
GLBs, registry publication, native packing and Khronos checks are real; these
tests do not execute Blender or authenticate a historical producer process.
The real fixed Blender producer/reopen control lives in the native import suite.
"""
from copy import deepcopy
import hashlib
import json
from io import BytesIO
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asset_library import file_record
from asset_library_contracts import seal
from asset_library_import import ImportPipeline
from rig_contracts import DEFAULT_LIMITS
from model3d_mesh_asset import build_asset
from model3d_mesh_import import import_canonical
from model3d_validation import validate_file
from rig_errors import RigError
from rig_gltf import GltfBuilder, export_geometry, read_gltf
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

    def appearance_registration(self, profile):
        """Publish actual native bytes with an explicitly selected appearance.

        The enclosing Blender envelope, when requested, remains synthetic. No
        validator, registry, packing, or decoder result is mocked here.
        """
        original_export = fixture.export_geometry

        def export(geometry, path, **kwargs):
            original_export(geometry, path, **kwargs)
            document = read_gltf(path)
            builder = GltfBuilder()
            builder.json = deepcopy(document.json)
            builder.binary = bytearray(document.buffers[0])
            part = builder.json['meshes'][0]['primitives'][0]
            if profile == 'pbr':
                builder.json['materials'] = [dict(name='Explicit Red Coat',
                    pbrMetallicRoughness=dict(baseColorFactor=[1, 0, 0, 1]))]
            elif profile != 'empty-material':
                builder.json.pop('materials', None)
                part.pop('material', None)
            if profile in ('image', 'texture', 'textured-material'):
                encoded = BytesIO()
                Image.new('RGB', (2, 2), 'red').save(encoded, format='PNG')
                builder.json['images'] = [dict(name='Explicit Red Image',
                    mimeType='image/png', bufferView=builder.view(encoded.getvalue()))]
            if profile in ('texture', 'textured-material'):
                builder.json['textures'] = [dict(source=0)]
            if profile == 'sampler':
                builder.json['samplers'] = [dict(wrapS=33071)]
            if profile == 'textured-material':
                part['attributes']['TEXCOORD_0'] = builder.accessor(
                    [[0., 0.], [1., 0.], [0., 1.]], 'VEC2', target=34962)
                part['material'] = 0
                builder.json['materials'] = [dict(name='Explicit Red Texture Coat',
                    pbrMetallicRoughness=dict(baseColorTexture=dict(index=0)))]
            builder.write(path)

        def register(*args, **kwargs):
            # Override only the fixture's default material-free producer profile
            # for these deliberately contradictory, self-consistent claims.
            kwargs['material_free'] = False
            with patch.object(fixture, 'export_geometry', export):
                return fixture.RegisteredProvenanceTests.native_registration(self, *args, **kwargs)
        return register

    def refuse_appearance(self, profile):
        with patch.object(self, 'native_registration', self.appearance_registration(profile)):
            ref = self.synthetic_blender_claim()
        path = self.root / 'appearance.glb'
        path.write_bytes(self.library.read_file(ref, 'source/model.glb'))
        document = read_gltf(path)
        expected = {'pbr': 'materials', 'empty-material': 'materials',
                    'image': 'images', 'texture': 'textures', 'sampler': 'samplers',
                    'textured-material': 'textures'}[profile]
        self.assertTrue(document.json[expected])
        with self.assertRaises(RigError) as caught:
            with import_canonical(self.library, ref):
                pass
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

    def test_fixed_blender_claim_refuses_authored_red_pbr_material(self):
        self.refuse_appearance('pbr')

    def test_fixed_blender_claim_refuses_empty_authored_material_slot(self):
        self.refuse_appearance('empty-material')

    def test_fixed_blender_claim_refuses_unused_embedded_image(self):
        self.refuse_appearance('image')

    def test_fixed_blender_claim_refuses_unused_texture(self):
        self.refuse_appearance('texture')

    def test_fixed_blender_claim_refuses_unused_sampler(self):
        self.refuse_appearance('sampler')

    def test_fixed_blender_claim_refuses_textured_material(self):
        self.refuse_appearance('textured-material')

    def test_generic_native_authored_red_pbr_material_remains_valid(self):
        ref, _ = self.appearance_registration('pbr')()
        with import_canonical(self.library, ref) as imported:
            material, = imported.model.geometry.materials
            self.assertEqual(material['name'], 'Explicit Red Coat')
            self.assertEqual(material['pbrMetallicRoughness']['baseColorFactor'], [1, 0, 0, 1])

    def test_generic_native_texture_material_remains_valid(self):
        ref, _ = self.appearance_registration('textured-material')()
        with import_canonical(self.library, ref) as imported:
            geometry = imported.model.geometry
            self.assertEqual(geometry.materials[0]['name'], 'Explicit Red Texture Coat')
            self.assertEqual(len(geometry.images), 1)
            self.assertEqual(geometry.textures, [dict(source=0)])
            self.assertIn('TEXCOORD_0', geometry.meshes[0].attributes)

    def test_material_free_synthetic_profile_matches_fixed_contract(self):
        ref = self.synthetic_blender_claim()
        for name in ('source/model.glb', 'normalized.glb'):
            path = self.root / name.replace('/', '-')
            path.write_bytes(self.library.read_file(ref, name))
            document = read_gltf(path)
            for field in ('materials', 'images', 'textures', 'samplers'):
                self.assertFalse(document.json.get(field), (name, field))
            for mesh in document.json['meshes']:
                for part in mesh['primitives']:
                    self.assertNotIn('material', part)
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.model.geometry.triangle_count, 1)

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'), 'Requires pinned Blender 4.5.3')
    def test_actual_fixed_blender_output_has_no_authored_appearance(self):
        # This control launches real Blender create/reopen; callers must hold the
        # repository native-validation lock. Synthetic tests above do not.
        ref = ImportPipeline(self.library).import_blender_fixture(
            identity=fixture.native_fixture.IDENTITY, version='1.0.0',
            metadata=fixture.native_fixture.METADATA, license_file=self.license)
        for name in ('source/model.glb', 'normalized.glb'):
            path = self.root / name.replace('/', '-')
            path.write_bytes(self.library.read_file(ref, name))
            document = read_gltf(path)
            for field in ('materials', 'images', 'textures', 'samplers'):
                self.assertFalse(document.json.get(field), (name, field))
            self.assertNotIn('material', document.json['meshes'][0]['primitives'][0])
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.model.geometry.triangle_count, 1)

    @staticmethod
    def receipt_budget(*, timeout=None, memory=None, file_limit=None):
        def mutate(report):
            if timeout is not None:
                report['shared_wall_timeout_seconds'] = timeout
            for mode in ('create', 'reopen'):
                stage = report[mode]
                if timeout is not None:
                    stage['argv'][2] = f'--cpu={timeout}'
                if memory is not None:
                    stage['resident_budget_bytes'] = memory
                    stage['virtual_limit_bytes'] = memory * 8
                    stage['argv'][1] = f'--as={memory * 8}'
                    # Keep every independent field feasible at the new budget.
                    stage['argv'][3] = f'--fsize={memory // 66}'
                if file_limit is not None:
                    stage['argv'][3] = f'--fsize={file_limit}'
        return mutate

    def accept_budget(self, **kwargs):
        ref = self.synthetic_blender_claim(mutate_report=self.receipt_budget(**kwargs))
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.provenance['canonical_ref'], ref)
            self.assertEqual(imported.model.geometry.triangle_count, 1)

    def test_claimed_timeout_one_second_is_valid_consistency_control(self):
        # A synthetic .1-second total is feasible; this does not time Blender.
        self.assertEqual(ImportPipeline(self.library, limits=dict(timeout_seconds=1)).limits['timeout_seconds'], 1)
        self.accept_budget(timeout=1)

    def test_claimed_timeout_at_producer_maximum_is_valid(self):
        self.assertEqual(DEFAULT_LIMITS['timeout_seconds'], 300)
        self.accept_budget(timeout=300)

    def test_claimed_timeout_above_producer_maximum_refuses(self):
        with self.assertRaises(RigError) as caught:
            ImportPipeline(self.library, limits=dict(timeout_seconds=301))
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMITS')
        self.refuse_blender_receipt(self.receipt_budget(timeout=301))

    def test_claimed_timeout_zero_refuses(self):
        with self.assertRaises(RigError) as caught:
            ImportPipeline(self.library, limits=dict(timeout_seconds=0))
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMITS')
        self.refuse_blender_receipt(self.receipt_budget(timeout=0))

    def test_claimed_memory_at_blender_minimum_is_valid_consistency_control(self):
        minimum = 384 * 1024**2
        pipeline = ImportPipeline(self.library, limits=dict(memory_bytes=minimum))
        self.assertEqual(pipeline.limits['memory_bytes'], minimum)
        self.assertEqual(pipeline.limits['dependency_bytes'], minimum // 66)
        self.accept_budget(memory=minimum)

    def test_claimed_memory_below_blender_minimum_refuses(self):
        from asset_import_blender import create_fixture
        minimum = 384 * 1024**2
        limits = ImportPipeline(self.library, limits=dict(memory_bytes=minimum - 1)).limits
        # Contract-only early refusal: no process is launched or presented as
        # runtime Blender evidence. Executable discovery precedes this check.
        with patch('asset_import_blender.find_blender', return_value='<blender>'), \
             patch('asset_import_blender._run') as run:
            with self.assertRaises(RigError) as caught:
                create_fixture(self.root / 'below-blender-budget', limits)
        self.assertEqual(caught.exception.report['code'], 'ASSET_BLENDER_BUDGET')
        run.assert_not_called()
        self.refuse_blender_receipt(self.receipt_budget(memory=minimum - 1))

    def test_claimed_memory_at_producer_maximum_is_valid(self):
        self.assertEqual(DEFAULT_LIMITS['memory_bytes'], 2 * 1024**3)
        self.accept_budget(memory=2 * 1024**3)

    def test_claimed_memory_above_producer_maximum_refuses(self):
        excessive = 2 * 1024**3 + 1
        with self.assertRaises(RigError) as caught:
            ImportPipeline(self.library, limits=dict(memory_bytes=excessive))
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMITS')
        self.refuse_blender_receipt(self.receipt_budget(memory=excessive))

    def test_claimed_file_cap_at_memory_quotient_is_valid(self):
        memory = 2 * 1024**3
        self.assertEqual(ImportPipeline(self.library).limits['dependency_bytes'], memory // 66)
        self.accept_budget(file_limit=memory // 66)

    def test_claimed_file_cap_one_above_memory_quotient_refuses(self):
        self.refuse_blender_receipt(self.receipt_budget(file_limit=(2 * 1024**3) // 66 + 1))

    def test_claimed_restrictive_file_cap_covering_actual_inputs_is_valid(self):
        # Any file cap >= each retained input can come from restrictive caller
        # source/dependency limits. It need not equal the maximum memory ratio.
        self.accept_budget(file_limit=192 * 1024)

    def retained_input_file_cap(self, delta=0):
        def mutate(report):
            # At this point the fixture has written the exact native original
            # which becomes source/model.glb in the synthetic Blender envelope.
            limit = max(report['source_bytes'],
                        (self.root / 'roundtrip.glb').stat().st_size) + delta
            self.assertGreater(limit, 0)
            for mode in ('create', 'reopen'):
                report[mode]['argv'][3] = f'--fsize={limit}'
        return mutate

    def test_claimed_file_cap_equal_to_largest_retained_input_is_valid(self):
        ref = self.synthetic_blender_claim(mutate_report=self.retained_input_file_cap())
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.model.geometry.triangle_count, 1)

    def test_claimed_file_cap_below_retained_input_refuses(self):
        self.refuse_blender_receipt(self.retained_input_file_cap(-1))

    def test_legacy_runtime_geometry_with_identical_evidence_alias_remains_valid(self):
        template = Path(__file__).resolve().parents[1] / 'examples/asset-library-foundation'
        manifest = json.loads((template / 'manifest.json').read_text())
        runtime = self.root / 'legacy.glb'
        geometry = Geometry([Mesh('triangle', np.asarray(fixture.FIXED_TRIANGLE),
            np.asarray(((0, 1, 2),)))], [{}])
        export_geometry(geometry, runtime, provenance=dict(fixture=True))
        checked = validate_gltf(runtime)
        self.assertTrue(checked['ok'], checked)
        native = file_record(runtime, 'runtime/asset.glb', 'geometry')
        alias = file_record(runtime, 'normalized.glb', 'evidence')
        license = manifest['files'][1]
        manifest['files'] = [native, alias, license]
        manifest['provenance']['sources'] = [native]
        manifest['compatibility']['formats'] = ['glb']
        manifest['publication']['inventory'] = [item['path'] for item in manifest['files']]
        self.assertFalse(manifest['provenance'].get('tools'))
        self.assertFalse(manifest['provenance'].get('conversions'))
        ref = self.library.register(seal(manifest), {
            native['path']: runtime, alias['path']: runtime,
            license['path']: template / license['path']})
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.provenance['source_entry'], 'runtime/asset.glb')
            self.assertEqual((imported.root / 'runtime/asset.glb').read_bytes(),
                             (imported.root / 'normalized.glb').read_bytes())
            geometry_record = record(imported.root, imported.provenance['source_entry'], 'model/gltf-binary')
            (imported.root / 'canonical.json').write_text(json.dumps(dict(
                canonical_ref=ref, manifest=self.library.resolve(ref))), encoding='utf-8')
            document = build_asset(imported.root, geometry_record, asset_id='legacy-alias',
                version='1.0.0', category='prop', sources=[source(geometry_record)],
                provenance=provenance(), canonical_evidence=record(
                    imported.root, 'canonical.json', 'application/json'))
            (imported.root / 'asset.json').write_text(json.dumps(document), encoding='utf-8')
            checked = validate_file(imported.root / 'asset.json', imported.root)
            self.assertTrue(checked['payloads_verified'])
            self.assertTrue(checked['geometry_inspected'])
            self.assertEqual(document['geometry']['sha256'], native['sha256'])


if __name__ == '__main__':
    unittest.main()
