"""Offline native import with real Khronos validation and canonical reopening."""
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import struct
import shutil
import tempfile
import traceback
import unittest
from unittest.mock import patch

from PIL import Image

from asset_library import AssetLibrary
from asset_library_import import ImportPipeline, resolve_import
from rig_errors import RigError
from rig_gltf import read_gltf
from rig_import import load_character_model

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = Path(os.environ.get('HATCH_GLTF_VALIDATOR_ROOT',
                               ROOT / 'tools/rig/node_modules/gltf-validator'))
IDENTITY = dict(id='triangle', namespace='fixtures', library='test-library',
                slug='triangle', name='Triangle', kind='prop', subtype='mesh',
                created_at='2026-10-23T00:00:00Z', attribution='Test author', owner='Test author')
METADATA = dict(description='Authored test geometry', category='geometry',
                author='Test author', publisher='Test author', source_url='local:test-fixture',
                license='CC0-1.0', allowed_uses=['test'], keywords=[], style_refs=[],
                capabilities=[], review_status='unreviewed')


@unittest.skipUnless(shutil.which('node') and (VALIDATOR / 'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class NativeImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library_path = self.root / 'library'
        self.library = AssetLibrary(self.library_path, create=True)
        self.library.upgrade_storage()
        self.pipeline = ImportPipeline(self.library)
        self.license = self.root / 'license.txt'
        self.license.write_bytes(b'Engineering test fixture. CC0-1.0.\n')

    def import_source(self, path, **kwargs):
        return self.pipeline.import_asset(path, identity=IDENTITY, version='1.0.0',
                                          metadata=METADATA, license_file=self.license, **kwargs)

    def external_source(self):
        source = self.root / 'fixture.gltf'
        (self.root / 'resources').mkdir()
        buffer = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
        (self.root / 'resources/triangle.bin').write_bytes(buffer)
        image = BytesIO()
        Image.new('RGB', (2, 2), 'red').save(image, format='PNG')
        (self.root / 'resources/paint.png').write_bytes(image.getvalue())
        value = dict(asset=dict(version='2.0'), scene=0, scenes=[dict(nodes=[0])],
                     nodes=[dict(mesh=0)], meshes=[dict(primitives=[
                         dict(attributes=dict(POSITION=0, TEXCOORD_0=1), material=0)])],
                     buffers=[dict(byteLength=len(buffer), uri='resources/triangle.bin')],
                     bufferViews=[dict(buffer=0, byteLength=len(buffer), target=34962)],
                     accessors=[dict(bufferView=0, componentType=5126, count=3, type='VEC3',
                                     min=[0, 0, 0], max=[1, 1, 0]),
                                dict(componentType=5126, count=3, type='VEC2')],
                     images=[dict(uri='resources/paint.png')], textures=[dict(source=0)],
                     materials=[dict(pbrMetallicRoughness=dict(baseColorTexture=dict(index=0)))])
        source.write_text(json.dumps(value), encoding='utf-8')
        return source

    def test_external_gltf_is_normalized_and_reopens_with_exact_sources(self):
        path = self.external_source()
        probe = self.pipeline.probe(path)
        self.assertFalse(probe['geometry_validated'])
        self.assertEqual({entry['path']: entry['roles'] for entry in probe['files']},
                         {'model.gltf': ['source'], 'resources/paint.png': ['texture'],
                          'resources/triangle.bin': ['buffer']})
        sources = {'source/model.gltf': path.read_bytes(), 'license.txt': self.license.read_bytes()}
        for name in ('resources/paint.png', 'resources/triangle.bin'):
            sources['source/' + name] = (self.root / name).read_bytes()
        scale = 1.000000001
        reference = self.import_source(path, fixture=True, unit_scale=scale)
        path.unlink()
        shutil.rmtree(self.root / 'resources')
        self.license.unlink()
        reopened = AssetLibrary(self.library_path)
        manifest = reopened.resolve(reference)
        self.assertEqual(len(manifest['provenance']['sources']), 3)
        self.assertEqual(manifest['previews'], [])
        self.assertTrue(manifest['provenance']['fixture'])
        for logical_path, original in sources.items():
            self.assertEqual(reopened.read_file(reference, logical_path), original)
        report = resolve_import(reopened, reference)
        self.assertEqual(report['source_sha256'], hashlib.sha256(sources['source/model.gltf']).hexdigest())
        output = reopened.read_file(reference, 'normalized.glb')
        self.assertEqual(report['normalized_sha256'], hashlib.sha256(output).hexdigest())
        self.assertEqual(manifest['provenance']['conversions'][0]['outputs'][0]['sha256'],
                         report['normalized_sha256'])
        self.assertFalse(report['diagnostic']['visual_approval'])
        self.assertEqual(report['diagnostic']['geometry'], 'base-mesh-before-morphs-and-skinning')
        self.assertIn('punctual light range', report['unchanged_metadata_lengths'])
        with Image.open(BytesIO(reopened.read_file(reference, 'diagnostic.png'))) as preview:
            self.assertEqual(preview.size, (256, 256))
        normalized = self.root / 'normalized.glb'
        normalized.write_bytes(output)
        doc = read_gltf(normalized).json
        self.assertEqual(doc['nodes'][1]['matrix'][0], scale)
        self.assertEqual(doc['images'][0]['bufferView'], 1)
        self.assertEqual(doc['textures'], [dict(source=0)])
        self.assertEqual(doc['buffers'][0].keys(), {'byteLength'})
        self.assertTrue(load_character_model(normalized).report['fixture'])

    def test_real_animated_glb_keeps_skin_morph_material_and_animation_records(self):
        path = ROOT / 'examples/rig-3d/humanoid/exports/character.glb'
        reference = self.import_source(path, unit_scale=2.0)
        original = read_gltf(path).json
        normalized = self.root / 'normalized.glb'
        normalized.write_bytes(self.library.read_file(reference, 'normalized.glb'))
        output = read_gltf(normalized).json
        for category in ('skins', 'animations', 'materials', 'textures', 'meshes'):
            self.assertEqual(original.get(category), output.get(category))
        self.assertTrue(original['skins'])
        self.assertTrue(original['animations'])
        self.assertTrue(any('targets' in primitive for mesh in original['meshes']
                            for primitive in mesh['primitives']))
        report = resolve_import(self.library, reference)
        self.assertGreater(report['native_observation']['morph_targets'], 0)
        self.assertTrue(report['native_observation']['clips'])

    def test_missing_resource_and_invalid_units_leave_library_empty(self):
        path = self.external_source()
        (self.root / 'resources/paint.png').unlink()
        with self.assertRaises(RigError) as error:
            self.import_source(path)
        self.assertEqual(error.exception.report['code'], 'LIBRARY_SOURCE')
        with self.assertRaises(RigError) as error:
            self.import_source(path, unit_scale=10**400)
        self.assertEqual(error.exception.report['code'], 'ASSET_IMPORT_SCALE')
        self.assertEqual(self.library.list_references(), [])

    def test_unavailable_independent_validator_leaves_library_empty(self):
        path = self.external_source()
        with patch.dict(os.environ, HATCH_GLTF_VALIDATOR_ROOT=str(self.root / 'missing')):
            with self.assertRaises(RigError) as error:
                self.import_source(path)
        self.assertEqual(error.exception.report['code'], 'RIG_VALIDATOR_MISSING')
        self.assertEqual(self.library.list_references(), [])

    def test_required_unavailable_extension_and_malformed_record_are_typed(self):
        path = self.external_source()
        value = json.loads(path.read_bytes())
        value['extensionsUsed'] = value['extensionsRequired'] = ['EXT_meshopt_compression']
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(RigError) as error:
            self.pipeline.probe(path)
        self.assertEqual(error.exception.report['code'], 'RIG_GLTF_EXTENSION')
        del value['extensionsUsed'], value['extensionsRequired']
        value['asset'] = []
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(RigError) as error:
            self.pipeline.probe(path)
        self.assertEqual(error.exception.report['code'], 'ASSET_IMPORT_FORMAT')
        self.assertEqual(self.library.list_references(), [])

    def test_projected_output_and_combined_memory_fail_before_writing_or_validation(self):
        source = self.external_source()
        closure_bytes = sum(entry['bytes'] for entry in self.pipeline.probe(source)['files'])
        cases = (
            (dict(source_bytes=source.stat().st_size), 'ASSET_IMPORT_LIMIT'),
            (dict(dependency_bytes=closure_bytes), 'ASSET_IMPORT_LIMIT'),
            (dict(memory_bytes=2 * 1024**2 + 100_000), 'RIG_LIMIT_MEMORY'),
        )
        for limits, code in cases:
            with self.subTest(limits=limits):
                self.pipeline = ImportPipeline(self.library, limits=limits)
                with patch('asset_library_import._write_glb') as write, \
                     patch('asset_library_import.validate_gltf') as validator:
                    with self.assertRaises(RigError) as error:
                        self.import_source(source, unit_scale=2.0, fixture=True)
                self.assertEqual(error.exception.report['code'], code)
                write.assert_not_called()
                validator.assert_not_called()
        self.assertEqual(self.library.list_references(), [])

    def test_postcommit_cleanup_does_not_fail_import_after_canonical_registration(self):
        source = self.external_source()
        cleanup = tempfile.TemporaryDirectory.cleanup

        def cleanup_error(directory):
            cleanup(directory)
            raise OSError('synthetic temporary cleanup failure')

        with patch.object(tempfile.TemporaryDirectory, 'cleanup', autospec=True, side_effect=cleanup_error):
            with patch.object(self.library, 'resolve', side_effect=OSError('synthetic resolution failure')) as resolve:
                reference = self.import_source(source)
                resolve.assert_not_called()
        reopened = AssetLibrary(self.library_path)
        self.assertEqual(reopened.list_references(), [reference])
        self.assertTrue(reopened.read_file(reference, 'normalized.glb').startswith(b'glTF'))

    def test_source_license_and_validator_failures_do_not_disclose_private_paths(self):
        source = self.external_source()
        marker = 'private-path-marker'
        missing_path = self.root / marker / 'missing.gltf'
        try:
            self.pipeline.probe(missing_path)
        except RigError as error:
            self.assertEqual(error.report['code'], 'LIBRARY_SOURCE')
            self.assertNotIn(marker, traceback.format_exc())
            self.assertNotIn(str(self.root), json.dumps(error.report))
        else:
            self.fail('Missing private source must fail')

        license = self.license
        self.license = self.root / marker / 'license.txt'
        with self.assertRaises(RigError) as error:
            self.import_source(source)
        self.assertEqual(error.exception.report['code'], 'LIBRARY_SOURCE')
        self.assertEqual(error.exception.report['asset'], 'license evidence')
        self.assertNotIn(marker, json.dumps(error.exception.report))
        self.license = license

        synthetic = RigError('RIG_VALIDATOR_MISSING', f'Cannot open {missing_path}',
                             asset=str(missing_path), observed=f'Module stack: {missing_path}')
        with patch('asset_library_import.validate_gltf', side_effect=synthetic):
            with self.assertRaises(RigError) as error:
                self.import_source(source)
        self.assertEqual(error.exception.report['code'], 'RIG_VALIDATOR_MISSING')
        self.assertNotIn(marker, json.dumps(error.exception.report))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual(self.library.list_references(), [])

    @unittest.skipUnless(
        os.environ.get('HATCH_BLENDER') and Path('/usr/bin/prlimit').is_file(),
        'Requires pinned Blender 4.5.3 on Linux with prlimit',
    )
    def test_blender_fixture_retains_original_and_two_verified_derivatives(self):
        reference = self.pipeline.import_blender_fixture(
            identity=IDENTITY, version='1.0.0', metadata=METADATA, license_file=self.license)
        reopened = AssetLibrary(self.library_path)
        manifest = reopened.resolve(reference)
        provenance = manifest['provenance']
        report = resolve_import(reopened, reference)
        original = provenance['sources'][0]
        blender, normalization = provenance['conversions']
        self.assertTrue(provenance['fixture'])
        self.assertEqual(original['path'], 'source/fixture.blend')
        self.assertEqual(original['role'], 'source')
        self.assertEqual(original['format'], 'binary')
        self.assertEqual(blender['inputs'], [original])
        self.assertEqual(normalization['inputs'], blender['outputs'])
        self.assertEqual(report['blender']['inspection']['build'], '67807e1800cc')
        self.assertEqual(report['blender']['inspection']['external_dependencies'], 0)
        self.assertEqual(report['native_observation']['triangles'], 1)
        self.assertEqual(report['native_observation']['vertices'], 3)
        for record, digest, magic in (
            (original, report['blender']['source_sha256'], b'BLENDER'),
            (blender['outputs'][0], report['blender']['derivative_sha256'], b'glTF'),
            (normalization['outputs'][0], report['normalized_sha256'], b'glTF'),
        ):
            payload = reopened.read_file(reference, record['path'])
            self.assertTrue(payload.startswith(magic))
            self.assertEqual(hashlib.sha256(payload).hexdigest(), record['sha256'])
            self.assertEqual(record['sha256'], digest)
        for name in ('source-khronos.json', 'normalized-khronos.json'):
            validation = json.loads(reopened.read_file(reference, name))
            self.assertEqual(validation['validator'], '2.0.0-dev.3.10')
            self.assertTrue(validation['ok'])

    def test_regular_import_rejects_blender_paths_without_worker(self):
        with patch('asset_import_blender.create_fixture') as worker:
            with self.assertRaises(RigError) as error:
                self.import_source(self.root / 'unknown.blend', fixture=True)
            self.assertEqual(error.exception.report['code'], 'SCENE3D_IMPORT_FORMAT')
            worker.assert_not_called()
        self.assertEqual(self.library.list_references(), [])
