import base64
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from model3d_blender import roundtrip_named_part, _verify
from rig_errors import RigError, read_json, sha256
from rig_gltf import read_gltf
from rig_import import load_character_model
from rig_models import Geometry, Mesh


def fixture_geometry():
    points = np.array([[0, 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, .5]], float)
    faces = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    return Geometry([Mesh('body', points, faces, name='Shell', region='central'),
                     Mesh('fin', points / 2 + [2, 0, 1], faces.copy(), name='Fin mesh', region='side')],
                    [{'name': 'Clay', 'pbrMetallicRoughness': {
                        'baseColorFactor': [.5, .25, .125, 1], 'metallicFactor': .2, 'roughnessFactor': .75}}])


def mutate_glb(source, destination, mutate):
    """Change native-output JSON without re-exporting through the authoring code."""
    document = read_gltf(source)
    binary = bytearray(document.buffers[0])
    mutate(document.json, binary)
    document.json['buffers'][0]['byteLength'] = len(binary)
    encoded = json.dumps(document.json, separators=(',', ':')).encode()
    encoded += b' ' * (-len(encoded) % 4)
    binary += b'\0' * (-len(binary) % 4)
    payload = struct.pack('<II', len(encoded), 0x4E4F534A) + encoded
    payload += struct.pack('<II', len(binary), 0x004E4942) + binary
    destination.write_bytes(struct.pack('<4sII', b'glTF', 2, len(payload) + 12) + payload)


def material_for_part(document, part):
    node, = (n for n in document['nodes'] if n['name'] == part)
    primitive, = document['meshes'][node['mesh']]['primitives']
    return document['materials'][primitive['material']], primitive


def add_texture(document, binary, primitive):
    # A real decodable texture and vertex-aligned UVs ensure that rejection is
    # about this authoring profile, rather than a broken texture fixture.
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGBA', (1, 1), (255, 0, 0, 255)).save(image, format='PNG')
    document['images'] = [{'uri': 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()}]
    document['textures'] = [{'source': 0}]
    for mesh in document['meshes']:
        for bound in mesh['primitives']:
            if bound['material'] != primitive['material']:
                continue
            count = document['accessors'][bound['attributes']['POSITION']]['count']
            binary += b'\0' * (-len(binary) % 4)
            view = len(document['bufferViews'])
            document['bufferViews'].append({'buffer': 0, 'byteOffset': len(binary), 'byteLength': count * 8})
            binary += bytes(count * 8)
            bound['attributes']['TEXCOORD_0'] = len(document['accessors'])
            document['accessors'].append({'bufferView': view, 'componentType': 5126, 'count': count, 'type': 'VEC2'})
    return {'index': 0}


class NamedBlenderPartTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.dest = Path(work.name) / 'result'
        self.geometry = fixture_geometry()
        self.args = dict(part='fin', offset_m=[.125, .25, -.5], base_color=[.25, .125, .75, 1], fixture=True)

    def test_invalid_edits_preserve_destination(self):
        for edit in (dict(part='absent'), dict(offset_m=[float('nan'), 0, 0]), dict(offset_m=[0, 0, 0]),
                     dict(offset_m=[True, 0, 0]), dict(base_color=[1, 1, 1, .5]),
                     dict(base_color=[.5, .25, .125, 1]), dict(fixture=1), dict(timeout=0)):
            with self.subTest(edit=edit), self.assertRaises(RigError):
                roundtrip_named_part(self.geometry, self.dest, **{**self.args, **edit})
            self.assertFalse(self.dest.exists())

    def test_profile_rejects_truncated_names_and_unsupported_attributes(self):
        self.geometry.meshes[0].name = 'é' * 32
        with self.assertRaisesRegex(RigError, '63 UTF-8 bytes'):
            roundtrip_named_part(self.geometry, self.dest, **self.args)
        self.geometry.meshes[0].name = 'body'
        self.geometry.meshes[0].attributes['TEXCOORD_0'] = np.zeros((4, 2))
        with self.assertRaisesRegex(RigError, 'triangle indices only'):
            roundtrip_named_part(self.geometry, self.dest, **self.args)
        self.assertFalse(self.dest.exists())

    def test_existing_destination_is_preserved(self):
        self.dest.mkdir()
        (self.dest/'retained').write_bytes(b'original')
        with self.assertRaisesRegex(RigError, 'new destination'):
            roundtrip_named_part(self.geometry, self.dest, **self.args)
        self.assertEqual((self.dest/'retained').read_bytes(), b'original')

    def test_missing_runtime_cannot_publish_a_partial_result(self):
        with self.assertRaisesRegex(RigError, 'Install Blender'):
            roundtrip_named_part(self.geometry, self.dest, executable=self.dest.parent/'absent', **self.args)
        self.assertEqual(list(self.dest.parent.iterdir()), [])

    def test_unusable_destination_parent_is_a_structured_refusal(self):
        blocker = self.dest.parent / 'file'
        blocker.write_bytes(b'kept')
        for destination in (self.dest.parent / 'absent' / 'result', blocker / 'result'):
            with self.subTest(destination=destination.parent.name):
                with self.assertRaises(RigError) as caught:
                    roundtrip_named_part(self.geometry, destination, **self.args)
                self.assertEqual(caught.exception.report['code'], 'MODEL3D_EXPORT_PATH')
        self.assertEqual(blocker.read_bytes(), b'kept')

    def test_publication_failures_are_structured_and_leave_no_stage(self):
        import model3d_blender
        from unittest import mock

        def fake_run(job, *, executable=None, timeout=None):
            for key in ('output', 'export'):
                if key in job:
                    Path(job[key]).write_bytes(key.encode())
            return {'blender_version': '4.5.3'}

        def racing_writer(stage, destination):
            destination.mkdir()
            (destination / 'other').write_bytes(b'theirs')
            raise FileExistsError(17, 'File exists')

        def unsupported(stage, destination):
            raise OSError(22, 'Invalid argument')

        for failure, code in ((racing_writer, 'MODEL3D_EXPORT_EXISTS'), (unsupported, 'MODEL3D_EXPORT_PUBLISH')):
            with self.subTest(code=code):
                shutil.rmtree(self.dest, ignore_errors=True)
                with mock.patch.object(model3d_blender, '_run', fake_run), \
                        mock.patch.object(model3d_blender, '_verify'), \
                        mock.patch.object(model3d_blender, '_rename_no_replace', failure), \
                        self.assertRaises(RigError) as caught:
                    roundtrip_named_part(self.geometry, self.dest, **self.args)
                self.assertEqual(caught.exception.report['code'], code)
                self.assertEqual([p.name for p in self.dest.parent.iterdir() if p.name.startswith('.model3d-')], [])
                if code == 'MODEL3D_EXPORT_EXISTS':
                    self.assertEqual((self.dest / 'other').read_bytes(), b'theirs')
                else:
                    self.assertFalse(self.dest.exists())

    def test_float32_rounding_rejects_reversed_source_and_edited_faces(self):
        for part, origin, offset in [('body', 8192., .125), ('fin', 0., 8192.)]:
            with self.subTest(part=part):
                geometry = fixture_geometry()
                mesh = geometry.by_id()[part]
                mesh.positions = np.array([[origin, 0, 0], [origin + 2**-11, 1, 0],
                                           [origin + 2**-10, 3, 0]])
                mesh.triangles = np.array([[0, 1, 2]])
                with self.assertRaisesRegex(RigError, 'face winding'):
                    roundtrip_named_part(geometry, self.dest, executable=self.dest.parent/'absent',
                                         **{**self.args, 'offset_m': [offset, 0, 0]})
                self.assertFalse(self.dest.exists())

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST') == '1', 'Enable pinned Blender integration.')
    def test_native_shared_material_and_mesh_coordinates_survive_both_reopens(self):
        original = fixture_geometry()
        report = roundtrip_named_part(self.geometry, self.dest, **self.args)
        self.assertEqual(read_json(self.dest/'receipt.json'), report)
        self.assertEqual(report['verified_parts'], ['body', 'fin'])
        self.assertEqual(report['triangles_per_export'], 8)
        self.assertTrue(report['edit']['copy_material'])
        self.assertEqual(len(set(report['files'].values())), 5)
        for filename, digest in report['files'].items():
            self.assertEqual(sha256(self.dest/filename), digest)
        for mesh in self.geometry.meshes:
            np.testing.assert_array_equal(mesh.positions, original.by_id()[mesh.id].positions)
        self.assertEqual(self.geometry.materials, original.materials)
        # The independent reader must reject the pre-edit artifact presented as the result.
        with self.assertRaisesRegex(RigError, 'oriented triangles'):
            _verify(self.dest/'reopened-source.glb', self.geometry, 'fin', np.array(self.args['offset_m']),
                    self.args['base_color'], 'edited.fin', True)
        for edit in ('reverse-body-face', 'recolor-body'):
            with self.subTest(edit=edit):
                changed = fixture_geometry()
                if edit == 'reverse-body-face':
                    face = changed.meshes[0].triangles[0]
                    face[:] = face[::-1].copy()
                else:
                    changed.materials[0]['pbrMetallicRoughness']['baseColorFactor'][0] = 0
                with self.assertRaises(RigError):
                    _verify(self.dest/'edited.glb', changed, 'fin', np.array(self.args['offset_m']),
                            self.args['base_color'], 'edited.fin', True)

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST') == '1', 'Enable pinned Blender integration.')
    def test_native_unshared_material_keeps_its_name(self):
        self.geometry.meshes = self.geometry.meshes[1:]
        result = roundtrip_named_part(self.geometry, self.dest, **self.args)
        self.assertFalse(result['edit']['copy_material'])
        self.assertEqual(result['edit']['material_name'], 'Clay')

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST') == '1', 'Enable pinned Blender integration.')
    def test_native_output_material_and_fixture_negative_controls(self):
        roundtrip_named_part(self.geometry, self.dest, **self.args)
        changed = self.dest.parent / 'changed.glb'
        delta = np.array(self.args['offset_m'])
        for filename, edited_part in [('reopened-source.glb', None), ('edited.glb', 'fin')]:
            source = self.dest / filename
            for part in ('body', 'fin'):
                for mutation in ('baseColorTexture', 'metallicRoughnessTexture', 'normalTexture',
                                 'occlusionTexture', 'emissiveTexture', 'doubleSided', 'unlit',
                                 'alphaMode', 'emissiveFactor', 'unknown-extension'):
                    with self.subTest(export=filename, part=part, mutation=mutation):
                        def mutate(document, binary):
                            material, primitive = material_for_part(document, part)
                            if mutation.endswith('Texture'):
                                target = material['pbrMetallicRoughness'] if mutation in (
                                    'baseColorTexture', 'metallicRoughnessTexture') else material
                                target[mutation] = add_texture(document, binary, primitive)
                            elif mutation == 'doubleSided':
                                material['doubleSided'] = True
                            elif mutation == 'alphaMode':
                                material['alphaMode'] = 'MASK'
                            elif mutation == 'emissiveFactor':
                                material['emissiveFactor'] = [1, 0, 0]
                            else:
                                extension = 'KHR_materials_unlit' if mutation == 'unlit' else 'TEST_material_appearance'
                                material['extensions'] = {extension: {}}
                                document['extensionsUsed'] = [extension]
                        mutate_glb(source, changed, mutate)
                        # The independent reader must actually admit each control.
                        self.assertEqual(set(load_character_model(changed).geometry.by_id()), {'body', 'fin'})
                        with self.assertRaisesRegex(RigError, 'material'):
                            _verify(changed, self.geometry, edited_part, delta,
                                    self.args['base_color'], 'edited.fin', True)
            for number, fixture in ((0, False), (1, True)):
                with self.subTest(export=filename, fixture=number):
                    def mutate(document, binary):
                        for node in document['nodes']:
                            node['extras']['hatchFixture'] = number
                    mutate_glb(source, changed, mutate)
                    with self.assertRaisesRegex(RigError, 'fixture provenance'):
                        _verify(changed, self.geometry, edited_part, delta,
                                self.args['base_color'], 'edited.fin', fixture)

            def defaults(document, binary):
                for material in document['materials']:
                    material.update(alphaMode='OPAQUE', doubleSided=False,
                                    emissiveFactor=[0, 0, 0], alphaCutoff=.5, extras={'note': 'metadata'})
            mutate_glb(source, changed, defaults)
            _verify(changed, self.geometry, edited_part, delta, self.args['base_color'], 'edited.fin', True)
