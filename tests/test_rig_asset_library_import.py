"""Offline native import with real Khronos validation and canonical reopening."""
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import struct
import shutil
import tempfile
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
        with self.assertRaisesRegex(RigError, 'missing|read|source|Source'):
            self.import_source(path)
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
