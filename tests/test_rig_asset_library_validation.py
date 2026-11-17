"""Exact normalized GLB verification through native decoders and durable storage."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from asset_library import AssetLibrary
from asset_library_import import ImportPipeline
from asset_library_validation import _generated_workspace, validate_asset
from rig_errors import RigError, object_hash
from rig_gltf import read_gltf
from rig_interop import validate_gltf
import test_asset_library as library_fixture
import test_rig_asset_library_import as import_fixture


def _stage_record(payload):
    record = dict(path='normalized.glb', bytes=len(payload),
                  sha256=hashlib.sha256(payload).hexdigest(), role='payload', format='glb')
    manifest = dict(files=[record], dependencies=[], identity={'kind': 'prop'},
                    metadata={'style_refs': []}, provenance={'parents': []})
    return Mock(inspect=Mock(return_value=manifest), read_file=Mock(return_value=payload))


class VerificationRejectionTests(unittest.TestCase):
    def test_generated_workspace_counts_active_primitive_copies(self):
        primitive = dict(attributes={'POSITION': 0}, targets=[{'NORMAL': 0}])
        description = dict(nodes=[dict(mesh=0, skin=0), dict(mesh=0, skin=0)],
                           meshes=[dict(primitives=[primitive])],
                           skins=[dict(joints=[2], inverseBindMatrices=1)],
                           accessors=[dict(count=3), dict(count=1)])
        document = SimpleNamespace(json=description)
        limits = {'bones': 8}
        normal_only = _generated_workspace(document, [0], limits)
        self.assertEqual(_generated_workspace(document, [0, 1], limits), 2 * normal_only)
        primitive['targets'][0]['POSITION'] = 0
        authored_positions = _generated_workspace(document, [0], limits)
        self.assertEqual(normal_only - authored_positions, 4 * 24 * 3)
        description['accessors'][1]['count'] = 2
        self.assertGreater(_generated_workspace(document, [0], limits), authored_positions)

    def test_real_obj_registration_is_outside_the_normalized_glb_lane(self):
        fixture = library_fixture.AssetLibraryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        reference = fixture.register()
        with self.assertRaises(RigError) as error:
            validate_asset(fixture.library, reference)
        self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_UNSUPPORTED')

    def test_labeled_glb_must_have_glb_bytes(self):
        library = _stage_record(b'o triangle\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        with patch('asset_library_validation.read_gltf') as native, self.assertRaises(RigError) as error:
            validate_asset(library, {})
        self.assertEqual(error.exception.report['code'], 'SCENE3D_IMPORT_FORMAT')
        native.assert_not_called()

    def test_resource_free_admission_runs_before_both_validators(self):
        library = _stage_record(b'fixture')
        for collection in ('buffers', 'images'):
            with (
                self.subTest(collection=collection),
                patch('asset_library_validation._json',
                      return_value={collection: [{'uri': 'example.png'}]}),
                patch('asset_library_validation.read_gltf') as native,
                patch('asset_library_validation.validate_gltf') as independent,
                self.assertRaises(RigError) as error,
            ):
                validate_asset(library, {})
            self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_EXTERNAL')
            native.assert_not_called()
            independent.assert_not_called()
            self.assertNotIn('example.png', json.dumps(error.exception.report))


@unittest.skipUnless(shutil.which('node') and (import_fixture.VALIDATOR / 'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class NativeVerificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.library.upgrade_storage()
        self.license = self.root / 'license.txt'
        self.license.write_bytes(b'Engineering test fixture. CC0-1.0.\n')

    def import_asset(self, source=None, *, category='prop', scale=2):
        if source is None:
            source = import_fixture.NativeImportTests.external_source(SimpleNamespace(root=self.root))
            document = json.loads(source.read_bytes())
            document['nodes'][0]['translation'] = [3, -1, 2]
            source.write_text(json.dumps(document), encoding='utf-8')
        identity = dict(import_fixture.IDENTITY, kind=category)
        return ImportPipeline(self.library).import_asset(
            source, identity=identity, version='1.0.0', metadata=import_fixture.METADATA,
            license_file=self.license, fixture=True, unit_scale=scale)

    def test_textured_import_reopens_and_verifies_without_library_changes(self):
        reference = self.import_asset()
        image = (self.root / 'resources/paint.png').read_bytes()
        shutil.rmtree(self.root / 'resources')
        (self.root / 'fixture.gltf').unlink()
        self.license.unlink()
        reopened = AssetLibrary(self.library.root)
        before = {p.relative_to(reopened.root): p.read_bytes() for p in reopened.root.rglob('*') if p.is_file()}
        manifest = reopened.inspect(reference, verify_payloads=False)
        record = next(f for f in manifest['files'] if f['path'] == 'normalized.glb')
        report = validate_asset(reopened, reference)
        self.assertEqual(report, validate_asset(reopened, reference))
        after = {p.relative_to(reopened.root): p.read_bytes() for p in reopened.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(report['reference'], reference)
        self.assertEqual(report['payload'], {key: record[key] for key in ('path', 'bytes', 'sha256')})
        self.assertEqual(report['independent_validation']['status'], 'passed')
        pinned = json.loads((import_fixture.ROOT / 'tools/rig/package.json').read_bytes())
        self.assertEqual(report['independent_validation']['version'], pinned['dependencies']['gltf-validator'])
        self.assertEqual(report['independent_validation']['asset_sha256'], record['sha256'])
        self.assertEqual(report['status'], 'technically-verified')
        self.assertFalse(report['production_approved'])
        self.assertEqual(report['visual_review'], 'unreviewed')
        self.assertEqual(report['capabilities'], ['material', 'mesh', 'texture'])
        self.assertEqual(report['geometry'], dict(vertices=3, triangles=1, bounds=[[6, -2, 4], [8, 0, 4]],
                                                  dimensions_m=[2, 2, 0], surface_area_m2=2))
        self.assertEqual([(node['node'], node['parent']) for node in report['hierarchy']], [(1, None), (0, 1)])
        self.assertEqual(report['hierarchy'][1]['world_matrix'],
                         [[2, 0, 0, 6], [0, 2, 0, -2], [0, 0, 2, 4], [0, 0, 0, 1]])
        self.assertEqual(report['meshes'][0]['texcoord_sets'], 1)
        self.assertEqual(report['meshes'][0]['boundary_edges'], 3)
        self.assertEqual(report['meshes'][0]['material_slot'], 0)
        self.assertEqual(report['authored_textures'], 1)
        self.assertEqual(report['textures'], [dict(width=2, height=2, mime_type='image/png',
                                                    sha256=hashlib.sha256(image).hexdigest())])
        for private in (str(self.root), 'fixture.gltf', 'paint.png'):
            self.assertNotIn(private, json.dumps(report))

    def test_rig_geometry_skin_and_binary_key_times(self):
        source = import_fixture.ROOT / 'examples/rig-3d/humanoid/exports/character.glb'
        reference = self.import_asset(source, category='rig', scale=1)
        reopened = AssetLibrary(self.library.root)
        with (patch('asset_library_validation.validate_gltf') as independent,
              self.assertRaises(RigError) as error):
            validate_asset(reopened, reference, limits={'keyframes': 1000})
        self.assertEqual(error.exception.report['code'], 'RIG_LIMIT_KEYFRAMES')
        independent.assert_not_called()

        report = validate_asset(reopened, reference)
        self.assertEqual((report['geometry']['vertices'], report['geometry']['triangles']), (3830, 7536))
        self.assertEqual((report['skeleton']['bones'], report['skeleton']['deform_bones']), (57, 56))
        self.assertEqual(len(report['skins']), 31)
        self.assertEqual(report['native_rigid_bindings'], 0)
        self.assertRegex(report['skins'][0]['native_inverse_bind_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual((report['skins'][0]['joints'], report['skins'][0]['metrics']['vertices'],
                          report['skins'][0]['metrics']['influences']), (56, 146, 4))
        self.assertIn('morph', report['capabilities'])
        self.assertEqual([c['metrics']['keyframes'] for c in report['clips']], [175, 200, 2, 425, 261, 425])
        self.assertEqual([c['metrics']['tracks'] for c in report['clips']], [7, 8, 1, 17, 9, 17])
        source_glb = read_gltf(source)
        walk = next(animation for animation in source_glb.json['animations'] if animation['name'] == 'walk')
        sampler = walk['samplers'][walk['channels'][0]['sampler']]
        expected = source_glb.accessor(sampler['input']).ravel().tolist()
        walk_report = report['clips'][4]
        self.assertEqual(walk_report['source_end_exact'], dict(numerator=2, denominator=1))
        self.assertEqual(walk_report['tracks'][0]['key_times_sha256'], object_hash(expected))

    def animated_source(self, names=None, *, morph=False):
        source = import_fixture.NativeImportTests.external_source(SimpleNamespace(root=self.root))
        document = json.loads(source.read_bytes())
        buffer_path = self.root / 'resources/triangle.bin'
        payload = bytearray(buffer_path.read_bytes())

        def accessor(values, width, kind, **metadata):
            offset = len(payload)
            payload.extend(struct.pack(f'<{len(values)}f', *values))
            views = document['bufferViews']
            views.append(dict(buffer=0, byteOffset=offset, byteLength=len(payload) - offset))
            accessors = document['accessors']
            accessors.append(dict(bufferView=len(views) - 1, componentType=5126,
                                  count=len(values) // width, type=kind, **metadata))
            return len(accessors) - 1

        if morph:
            deltas = accessor([0.0, 0.0, 0.25] * 3, 3, 'VEC3',
                              min=[0.0, 0.0, 0.25], max=[0.0, 0.0, 0.25])
            primitives = document['meshes'][0]['primitives']
            primitives[0]['targets'] = [dict(POSITION=deltas)]
            primitives.append(dict(primitives[0]))
        document['animations'] = []
        for index, times in enumerate(([0.0], [0.0, 1 / 128], [0.0, 2.0])):
            sample_times = accessor(times, 1, 'SCALAR', min=[times[0]], max=[times[-1]])
            translations = accessor([0.0, 0.0, 0.0] * len(times), 3, 'VEC3')
            document['animations'].append(dict(
                samplers=[{'input': sample_times, 'output': translations}],
                channels=[dict(sampler=0, target=dict(node=0, path='translation'))]))
            animation = document['animations'][-1]
            if names:
                animation['name'] = names[index]
            if morph:
                weights = accessor([0.5] * len(times), 1, 'SCALAR')
                animation['samplers'].append(dict(input=sample_times, output=weights, interpolation='STEP'))
                animation['channels'].append(dict(sampler=1, target=dict(node=0, path='weights')))
        document['buffers'][0]['byteLength'] = len(payload)
        buffer_path.write_bytes(payload)
        source.write_text(json.dumps(document), encoding='utf-8')
        return source

    def test_unskinned_animation_reports_exact_source_end_and_rigid_fallback(self):
        report = validate_asset(self.library, self.import_asset(self.animated_source()))
        self.assertEqual(report['skins'], [])
        self.assertEqual(report['native_rigid_bindings'], 1)
        self.assertNotIn('skin', report['capabilities'])
        self.assertIn('animation', report['capabilities'])
        exact = [dict(numerator=n, denominator=d) for n, d in ((0, 1), (1, 128), (2, 1))]
        self.assertEqual([clip['source_end_exact'] for clip in report['clips']], exact)
        floor = dict(zip(('numerator', 'denominator'), (1 / 24).as_integer_ratio()))
        self.assertEqual([clip['native_playback_duration_exact'] for clip in report['clips']],
                         [floor, floor, exact[-1]])

    def test_named_clip_collision_fails_after_khronos_validates_normalized_bytes(self):
        source = self.animated_source(['Walk', 'Walk-2', 'Walk'])
        reference = self.import_asset(source, category='animation')
        payload = self.library.read_file(reference, 'normalized.glb')
        staged = self.root / 'normalized.glb'
        staged.write_bytes(payload)
        independent = validate_gltf(staged)
        self.assertTrue(independent['ok'])
        self.assertIs(independent['issues']['truncated'], False)
        self.assertEqual(independent['asset_sha256'], hashlib.sha256(payload).hexdigest())
        with self.assertRaises(RigError) as error:
            validate_asset(self.library, reference)
        self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_ANIMATION')
        self.assertEqual((error.exception.report['expected'], error.exception.report['observed']), (3, 2))

    def test_weights_channel_expands_to_one_track_per_active_primitive(self):
        source = self.animated_source(['Walk', 'Walk-1', 'Walk'], morph=True)
        report = validate_asset(self.library, self.import_asset(source, category='animation'))
        self.assertEqual([clip['metrics']['tracks'] for clip in report['clips']], [3, 3, 3])
        self.assertEqual([(track['path'], track['interpolation'], track['keys'])
                          for track in report['clips'][2]['tracks']],
                         [('translation', 'LINEAR', 2), ('weights', 'STEP', 2), ('weights', 'STEP', 2)])

    def test_image_without_texture_object_does_not_verify_texture_category(self):
        source = import_fixture.NativeImportTests.external_source(SimpleNamespace(root=self.root))
        document = json.loads(source.read_bytes())
        del document['textures']
        document['materials'] = [{}]
        source.write_text(json.dumps(document), encoding='utf-8')
        reference = self.import_asset(source, category='texture')
        with self.assertRaises(RigError) as error:
            validate_asset(self.library, reference)
        self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_CATEGORY')

    def test_early_byte_and_native_budgets_and_missing_khronos(self):
        reference = self.import_asset()
        with patch.object(self.library, 'read_file', wraps=self.library.read_file) as read:
            for limit, code in [('source_bytes', 'RIG_LIMIT_SOURCE'),
                                ('dependency_bytes', 'RIG_LIMIT_DEPENDENCIES'),
                                ('memory_bytes', 'RIG_LIMIT_MEMORY')]:
                with self.subTest(limit=limit), self.assertRaises(RigError) as error:
                    validate_asset(self.library, reference, limits={limit: 1})
                self.assertEqual(error.exception.report['code'], code)
            read.assert_not_called()
        with self.assertRaises(RigError) as error:
            validate_asset(self.library, reference, limits={'vertices': 2})
        self.assertEqual(error.exception.report['code'], 'RIG_LIMIT_VERTICES')
        with patch.dict(os.environ, HATCH_GLTF_VALIDATOR_ROOT=str(self.root / 'missing-validator')):
            with self.assertRaises(RigError) as error:
                validate_asset(self.library, reference)
        self.assertEqual(error.exception.report['code'], 'RIG_VALIDATOR_MISSING')
        self.assertNotIn(str(self.root), json.dumps(error.exception.report))

    def test_canonical_owner_missing_or_corrupt_bytes_fail_before_decode(self):
        reference = self.import_asset()
        manifest = self.library.inspect(reference, verify_payloads=False)
        digest = next(f['sha256'] for f in manifest['files'] if f['path'] == 'normalized.glb')
        path = self.library.storage.root / 'blobs' / digest
        original = path.read_bytes()
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    path.write_bytes(bytes(len(original)))
                else:
                    path.unlink()
                try:
                    with patch('asset_library_validation.read_gltf') as native, self.assertRaises(RigError) as error:
                        validate_asset(self.library, reference)
                    self.assertIn(error.exception.report['code'], ('LIBRARY_DURABILITY', 'LIBRARY_CORRUPT'))
                    self.assertNotIn(str(self.root), json.dumps(error.exception.report))
                    native.assert_not_called()
                finally:
                    path.write_bytes(original)

    def test_both_validators_must_succeed(self):
        reference = self.import_asset()
        rejected = dict(ok=False, issues=dict(numErrors=1, messages=[str(self.root)]))
        with (patch('asset_library_validation.validate_gltf', return_value=rejected),
              patch('asset_library_validation.load_character_model') as native,
              self.assertRaises(RigError) as error):
            validate_asset(self.library, reference)
        self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_VALIDATION')
        self.assertNotIn(str(self.root), json.dumps(error.exception.report))
        native.assert_not_called()

        payload = self.library.read_file(reference, 'normalized.glb')
        unknown_version = dict(ok=True, issues=dict(numErrors=0, numWarnings=0, truncated=False),
                               validator=str(self.root),
                               asset_sha256=hashlib.sha256(payload).hexdigest())
        with (patch('asset_library_validation.validate_gltf', return_value=unknown_version),
              self.assertRaises(RigError) as error):
            validate_asset(self.library, reference)
        self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_VALIDATION')
        self.assertNotIn(str(self.root), json.dumps(error.exception.report))

        with patch('asset_library_validation.load_character_model',
                   side_effect=RigError('RIG_TEXTURE_DECODE', str(self.root))) as native:
            with self.assertRaises(RigError) as error:
                validate_asset(self.library, reference)
        self.assertEqual(error.exception.report['code'], 'RIG_TEXTURE_DECODE')
        self.assertNotIn(str(self.root), json.dumps(error.exception.report))
        native.assert_called_once()

    def test_real_khronos_issue_cap_fails_even_with_zero_errors(self):
        source = import_fixture.NativeImportTests.external_source(SimpleNamespace(root=self.root))
        document = json.loads(source.read_bytes())
        document['materials'].extend({} for _ in range(1001))
        source.write_text(json.dumps(document), encoding='utf-8')
        reference = self.import_asset(source)
        normalized = self.root / 'normalized.glb'
        normalized.write_bytes(self.library.read_file(reference, 'normalized.glb'))
        independent = validate_gltf(normalized)
        self.assertTrue(independent['ok'])
        self.assertEqual(independent['issues']['numErrors'], 0)
        self.assertIs(independent['issues']['truncated'], True)
        with self.assertRaises(RigError) as error:
            validate_asset(self.library, reference)
        self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_VALIDATION')

    def test_incomplete_or_malformed_khronos_counts_fail_before_native_decode(self):
        reference = self.import_asset()
        for issue_overrides in (dict(truncated=True), dict(truncated=None), dict(truncated=0),
                                dict(numErrors=False), dict(numErrors=0.0), dict(numErrors=-1),
                                dict(numWarnings=False), dict(numWarnings=0.0), dict(numWarnings=-1)):
            issues = {**dict(truncated=False, numErrors=0, numWarnings=0), **issue_overrides}
            if issues['truncated'] is None:
                del issues['truncated']
            with (self.subTest(issues=issues),
                  patch('asset_library_validation.validate_gltf', return_value=dict(ok=True, issues=issues)),
                  patch('asset_library_validation.load_character_model') as native,
                  self.assertRaises(RigError) as error):
                validate_asset(self.library, reference)
            self.assertEqual(error.exception.report['code'], 'ASSET_TECHNICAL_VALIDATION')
            native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
