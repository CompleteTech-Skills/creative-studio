"""Real interchange regressions for the shared native material validator."""
from copy import deepcopy
import io
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from rig_errors import RigError, sha256
from rig_gltf import GltfBuilder, export_geometry
from rig_import import load_character_model
from rig_models import save_geometry, load_geometry, validate_geometry


class RigMaterialValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stream = io.BytesIO()
        Image.new('RGBA', (2, 2), (40, 80, 120, 255)).save(stream, format='PNG')
        self.image = stream.getvalue()

    def source(self, *, extension='glb', mutate=None, uv_sets=(0, 1), color_values=None, color_kind='VEC4'):
        builder = GltfBuilder()
        position = builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', bounds=True)
        attrs = {'POSITION': position,
                 'NORMAL': builder.accessor([[0, 0, 1]] * 3, 'VEC3'),
                 'TANGENT': builder.accessor([[1, 0, 0, 1]] * 3, 'VEC4')}
        if color_values is not None:
            attrs['COLOR_0'] = builder.accessor(color_values, color_kind)
        for uv in uv_sets:
            attrs['TEXCOORD_' + str(uv)] = builder.accessor([[0, 0], [1, 0], [0, 1]], 'VEC2')
        builder.json.update(
            images=[{'mimeType': 'image/png', 'bufferView': builder.view(self.image)}],
            textures=[{'source': 0, 'sampler': 0}],
            samplers=[{'magFilter': 9729, 'minFilter': 9987, 'wrapS': 10497, 'wrapT': 33071}],
            materials=[{
                'name': 'Engineering fixture material',
                'pbrMetallicRoughness': {
                    'baseColorFactor': [0.2, 0.3, 0.4, 1.0], 'metallicFactor': 0.3, 'roughnessFactor': 0.7,
                    'baseColorTexture': {'index': 0}, 'metallicRoughnessTexture': {'index': 0, 'texCoord': 1}},
                'normalTexture': {'index': 0, 'scale': -2.0},
                'occlusionTexture': {'index': 0, 'strength': 0.8},
                'emissiveTexture': {'index': 0}, 'emissiveFactor': [0.1, 0.2, 0.3],
                'alphaMode': 'MASK', 'alphaCutoff': 1.5, 'doubleSided': True}],
            meshes=[{'name': 'triangle', 'primitives': [{'attributes': attrs, 'material': 0}]}],
            nodes=[{'mesh': 0}], scenes=[{'nodes': [0]}])
        builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
        if mutate:
            mutate(builder.json)
        path = self.root / ('source.' + extension)
        builder.write(path)
        return path, deepcopy(builder.json)

    def reject(self, mutate, *, code=None, uv_sets=(0, 1)):
        for extension in ('gltf', 'glb'):
            with self.subTest(extension=extension):
                path, _ = self.source(extension=extension, mutate=mutate, uv_sets=uv_sets)
                before = path.read_bytes()
                with self.assertRaises(RigError) as caught:
                    load_character_model(path)
                if code:
                    self.assertEqual(caught.exception.report['code'], code)
                self.assertEqual(path.read_bytes(), before)

    def test_all_core_slots_roundtrip_bytes_and_both_uv_sets(self):
        for extension in ('glb', 'gltf'):
            with self.subTest(extension=extension):
                path, document = self.source(extension=extension)
                digest = sha256(path)
                model = load_character_model(path)
                self.assertTrue(model.report['fixture'])
                self.assertEqual(model.geometry.materials, document['materials'])
                self.assertEqual(model.geometry.samplers, document['samplers'])
                self.assertEqual(model.geometry.textures, document['textures'])
                self.assertEqual(model.geometry.images[0].data, self.image)
                np.testing.assert_allclose(model.geometry.meshes[0].attributes['TEXCOORD_1'], [[0, 0], [1, 0], [0, 1]])
                native = save_geometry(self.root, model.geometry)
                restored = load_geometry(self.root, native)
                self.assertEqual(restored.materials, model.geometry.materials)
                exported = self.root / ('exported.' + extension)
                export_geometry(restored, exported)
                reopened = load_character_model(exported)
                self.assertEqual(reopened.geometry.materials, document['materials'])
                self.assertEqual(reopened.geometry.images[0].data, self.image)
                self.assertEqual(sha256(path), digest)

    def test_missing_and_invalid_texture_indices_are_rejected(self):
        for slot, parent in (('baseColorTexture', 'pbrMetallicRoughness'),
                             ('metallicRoughnessTexture', 'pbrMetallicRoughness'),
                             ('normalTexture', None), ('occlusionTexture', None), ('emissiveTexture', None)):
            for bad in ({}, {'index': -1}, {'index': 99999}, {'index': True}, {'index': 0.0}, {'index': '0'}, None, []):
                with self.subTest(slot=slot, bad=bad):
                    def mutate(doc):
                        material = doc['materials'][0]
                        (material[parent] if parent else material)[slot] = bad
                    self.reject(mutate, code='RIG_TEXTURE_REFERENCE')
        # Original reported failure: a binding to an entirely absent texture list.
        self.reject(lambda d: d.update(textures=[]), code='RIG_TEXTURE_REFERENCE')

    def test_primitive_material_index_is_typed_before_lookup(self):
        for bad in (-1, 1, True, 0.0, '0', None, {}):
            with self.subTest(material=bad):
                self.reject(lambda d: d['meshes'][0]['primitives'][0].update(material=bad), code='RIG_MATERIAL')

    def test_image_sampler_links_and_sampler_parameters_are_rejected(self):
        for texture in ({}, {'source': 99999}, {'source': -1}, {'source': True}, {'source': 0.0}, {'source': '0'},
                        {'source': 0, 'sampler': 1}, {'source': 0, 'sampler': False}, {'source': 0, 'sampler': 0.0}, None):
            with self.subTest(texture=texture):
                self.reject(lambda d: d.update(textures=[texture]), code='RIG_TEXTURE_REFERENCE')
        for key, bad in (('magFilter', 9987), ('minFilter', -1), ('wrapS', 1),
                         ('wrapT', True), ('wrapT', '10497'), ('magFilter', 9729.0)):
            with self.subTest(key=key):
                self.reject(lambda d: d['samplers'][0].update({key: bad}), code='RIG_TEXTURE_SAMPLER')
        self.reject(lambda d: d.update(samplers=[None]), code='RIG_TEXTURE_SAMPLER')
        self.reject(lambda d: d.update(samplers=[]), code='RIG_TEXTURE_REFERENCE')

    def test_each_assigned_mesh_must_supply_effective_uv_binding(self):
        self.reject(lambda d: None, code='RIG_TEXTURE_UV', uv_sets=(0,))
        def other_primitive(doc):
            primitive = deepcopy(doc['meshes'][0]['primitives'][0])
            del primitive['attributes']['TEXCOORD_1']
            doc['meshes'][0]['primitives'].append(primitive)
        self.reject(other_primitive, code='RIG_TEXTURE_UV')
        for bad in (-1, True, 0.0, '1', None):
            self.reject(lambda d: d['materials'][0]['normalTexture'].update(texCoord=bad), code='RIG_TEXTURE_REFERENCE')

    def test_texture_transform_override_and_supported_extension_slots_preserve(self):
        slots = {
            'KHR_materials_clearcoat': ['clearcoatTexture', 'clearcoatRoughnessTexture', 'clearcoatNormalTexture'],
            'KHR_materials_iridescence': ['iridescenceTexture', 'iridescenceThicknessTexture'],
            'KHR_materials_sheen': ['sheenColorTexture', 'sheenRoughnessTexture'],
            'KHR_materials_specular': ['specularTexture', 'specularColorTexture'],
            'KHR_materials_transmission': ['transmissionTexture'], 'KHR_materials_volume': ['thicknessTexture'],
            'KHR_materials_anisotropy': ['anisotropyTexture']}
        def extended(doc):
            doc['extensionsUsed'] = ['KHR_texture_transform', *slots]
            transform = {'texCoord': 0, 'offset': [-0.2, 0.3], 'scale': [-1.0, 0.0], 'rotation': -0.5}
            doc['materials'][0]['pbrMetallicRoughness']['metallicRoughnessTexture']['extensions'] = {'KHR_texture_transform': transform}
            doc['materials'][0]['extensions'] = {
                name: {field: {'index': 0, 'texCoord': 0} for field in fields} for name, fields in slots.items()}
        for extension in ('glb', 'gltf'):
            path, document = self.source(extension=extension, mutate=extended, uv_sets=(0,))
            result = load_character_model(path)
            self.assertEqual(result.geometry.materials, document['materials'])
            self.assertEqual(result.geometry.extensions_used, document['extensionsUsed'])
        for name, fields in slots.items():
            for field in fields:
                with self.subTest(extension=name, field=field):
                    self.reject(lambda d: d['materials'][0].update(extensions={name: {field: {'index': 99999}}}), code='RIG_TEXTURE_REFERENCE')
        for transform in ({'texCoord': 1}, {'offset': [0]}, {'rotation': '0'}, {'scale': None}, []):
            with self.subTest(transform=transform):
                def mutate(doc):
                    doc['materials'][0]['normalTexture']['extensions'] = {'KHR_texture_transform': transform}
                self.reject(mutate, code='RIG_TEXTURE_UV' if transform == {'texCoord': 1} else 'RIG_MATERIAL', uv_sets=(0,))

    def test_core_parameters_reject_wrong_shapes_types_and_ranges(self):
        bad_pbr = [('baseColorFactor', [1, 1, 1]), ('baseColorFactor', [1, 1, 1, 2]),
                   ('baseColorFactor', [1, True, 1, 1]), ('metallicFactor', -0.1),
                   ('roughnessFactor', 1.1), ('metallicFactor', '0.5')]
        for field, value in bad_pbr:
            with self.subTest(field=field, value=value):
                self.reject(lambda d: d['materials'][0]['pbrMetallicRoughness'].update({field: value}), code='RIG_MATERIAL')
        for field, value in [('emissiveFactor', [0, 0, 2]), ('emissiveFactor', [0, 0]),
                             ('doubleSided', 1), ('alphaCutoff', -1), ('alphaMode', 'UNKNOWN'),
                             ('pbrMetallicRoughness', []), ('extensions', [])]:
            with self.subTest(field=field):
                self.reject(lambda d: d['materials'][0].update({field: value}), code='RIG_MATERIAL')
        self.reject(lambda d: d['materials'][0]['normalTexture'].update(scale='1'), code='RIG_MATERIAL')
        self.reject(lambda d: d['materials'][0]['occlusionTexture'].update(strength=2), code='RIG_MATERIAL')

    def test_real_cli_rejects_bad_binding_before_run_publication(self):
        path, _ = self.source(mutate=lambda d: d['materials'][0]['pbrMetallicRoughness'].update(
            baseColorTexture={'index': 99999}))
        before = sha256(path)
        destination = self.root / 'rejected-run'
        cli = Path(__file__).resolve().parents[1] / 'scripts' / 'rig.py'
        result = subprocess.run([sys.executable, str(cli), 'import', '--model', str(path),
                                 '--run-dir', str(destination)], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'RIG_TEXTURE_REFERENCE')
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(destination.exists())
        self.assertEqual(sha256(path), before)

    def test_vertex_color_rgb_and_alpha_ranges_are_validated(self):
        for extension in ('glb', 'gltf'):
            path, _ = self.source(extension=extension)
            geometry = load_character_model(path).geometry
            geometry.meshes[0].attributes['COLOR_0'] = np.array([[0, 1, 0, 1], [1, 0, 1, 0], [0.5, 0.5, 0.5, 0.5]])
            exported = self.root / ('colored.' + extension)
            export_geometry(geometry, exported)
            imported = load_character_model(exported)
            np.testing.assert_allclose(imported.geometry.meshes[0].attributes['COLOR_0'], geometry.meshes[0].attributes['COLOR_0'])
            for channel in range(4):
                for bad in (-0.1, 1.1):
                    candidate = deepcopy(geometry)
                    candidate.meshes[0].attributes['COLOR_0'][0, channel] = bad
                    with self.subTest(extension=extension, channel=channel, bad=bad):
                        with self.assertRaises(RigError) as caught:
                            validate_geometry(candidate)
                        self.assertEqual(caught.exception.report['code'], 'RIG_COLOR_RANGE')
                        malformed, _ = self.source(extension=extension, color_values=candidate.meshes[0].attributes['COLOR_0'])
                        before = sha256(malformed)
                        with self.assertRaises(RigError) as imported_error:
                            load_character_model(malformed)
                        self.assertEqual(imported_error.exception.report['code'], 'RIG_COLOR_RANGE')
                        self.assertEqual(sha256(malformed), before)
            rgb, _ = self.source(extension=extension, color_values=[[0, 1, 0], [1, 0, 1], [0.5, 0.5, 0.5]], color_kind='VEC3')
            rgb_imported = load_character_model(rgb)
            np.testing.assert_allclose(rgb_imported.geometry.meshes[0].attributes['COLOR_0'][:, 3], [1, 1, 1])

    def test_native_save_and_export_share_guard_without_mutation(self):
        path, _ = self.source()
        geometry = load_character_model(path).geometry
        for value in (float('nan'), float('inf'), -float('inf'), 10**400):
            with self.subTest(value=str(value)[:20]):
                candidate = deepcopy(geometry)
                candidate.materials[0]['normalTexture']['scale'] = value
                before = set(self.root.iterdir())
                for validate in (validate_geometry,
                                 lambda g: save_geometry(self.root, g),
                                 lambda g: export_geometry(g, self.root / 'bad.glb')):
                    with self.assertRaises(RigError):
                        validate(candidate)
                self.assertEqual(set(self.root.iterdir()), before)
        geometry.materials[0]['pbrMetallicRoughness']['baseColorTexture']['index'] = 99999
        with self.assertRaises(RigError):
            export_geometry(geometry, self.root / 'bad.glb')
        self.assertFalse((self.root / 'bad.glb').exists())


if __name__ == '__main__':
    unittest.main()
