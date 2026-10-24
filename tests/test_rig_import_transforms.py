"""Real glTF vector transforms checked against independent literal expectations."""
from pathlib import Path
import tempfile
import unittest
import numpy as np

from rig_gltf import GltfBuilder, export_geometry
from rig_import import load_character_model
from rig_jobs import create_run
from rig_errors import RigError, sha256


class RigImportTransformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, extension, *, reflected=False, handedness=1., morph=False, normals=True, zero_tangent=False, multiple=False):
        builder = GltfBuilder()
        builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
        positions = [[0, 0, 0], [1, 1, 0], [0, 0, 1]]
        normal = np.array([1, -1, 0.])/np.sqrt(2)
        tangent = np.array([0, 0, 0.]) if zero_tangent else np.array([1, 1, 0.])/np.sqrt(2)
        attrs = {'POSITION': builder.accessor(positions, 'VEC3', bounds=True),
                 'TANGENT': builder.accessor(np.tile([*tangent, handedness], (3, 1)), 'VEC4'),
                 'TEXCOORD_0': builder.accessor([[0, 0], [1, 0], [0, 1]], 'VEC2')}
        if normals:
            attrs['NORMAL'] = builder.accessor(np.tile(normal, (3, 1)), 'VEC3')
        primitive = {'attributes': attrs, 'indices': builder.accessor([0, 1, 2], 'SCALAR', 5123)}
        mesh = {'primitives': [primitive]}
        if morph:
            primitive['targets'] = [{
                'POSITION': builder.accessor(np.tile([0, 0, .2], (3, 1)), 'VEC3'),
                'NORMAL': builder.accessor(np.tile([.15, 0, .1], (3, 1)), 'VEC3'),
                'TANGENT': builder.accessor(np.tile([0, 0, .25], (3, 1)), 'VEC3')}]
            mesh.update(weights=[.35], extras={'targetNames': ['bend']})
            if multiple:
                primitive['targets'].append({
                    'POSITION': builder.accessor(np.tile([0, .1, 0], (3, 1)), 'VEC3'),
                    'NORMAL': builder.accessor(np.tile([0, .05, 0], (3, 1)), 'VEC3'),
                    'TANGENT': builder.accessor(np.tile([.1, 0, 0], (3, 1)), 'VEC3')})
                mesh.update(weights=[.35, -.2], extras={'targetNames': ['bend', 'twist']})
        builder.json['meshes'] = [mesh]
        builder.json['nodes'] = [{'mesh': 0, 'scale': [-2 if reflected else 2, 1, .5]}]
        builder.json['scenes'] = [{'nodes': [0]}]
        path = self.root/('reflected' if reflected else 'scaled')
        path = path.with_suffix('.'+extension)
        builder.write(path)
        return path

    def test_nonuniform_transform_preserves_orthogonal_tangent_frame_and_source(self):
        for extension in ('glb', 'gltf'):
            for reflected in (False, True):
                for handedness in (-1., 1.):
                    with self.subTest(extension=extension, reflected=reflected, handedness=handedness):
                        path = self.source(extension, reflected=reflected, handedness=handedness)
                        before = path.read_bytes()
                        imported = load_character_model(path)
                        mesh = imported.geometry.meshes[0]
                        # Independent geometric vectors: transformed triangle U edge and normal.
                        sign = -1 if reflected else 1
                        expected_t = np.array([sign*2, 1, 0])/np.sqrt(5)
                        expected_n = np.array([sign, -2, 0])/np.sqrt(5)
                        np.testing.assert_allclose(mesh.attributes['TANGENT'][:, :3], np.tile(expected_t, (3, 1)), atol=1e-7)
                        np.testing.assert_allclose(mesh.attributes['NORMAL'], np.tile(expected_n, (3, 1)), atol=1e-7)
                        self.assertLess(abs(np.dot(mesh.attributes['NORMAL'][0], mesh.attributes['TANGENT'][0, :3])), 1e-7)
                        np.testing.assert_array_equal(mesh.attributes['TANGENT'][:, 3], np.full(3, sign*handedness))
                        face = mesh.positions[mesh.triangles[0]]
                        face_normal = np.cross(face[1]-face[0], face[2]-face[0])
                        self.assertGreater(np.dot(face_normal, expected_n), 0)
                        np.testing.assert_array_equal(mesh.attributes['TEXCOORD_0'], [[0, 0], [1, 0], [0, 1]])
                        self.assertEqual(path.read_bytes(), before)
                        # Re-encode baked geometry and decode without a transform: basis survives.
                        out = self.root/'baked.glb'
                        export_geometry(imported.geometry, out)
                        again = load_character_model(out).geometry.meshes[0]
                        np.testing.assert_allclose(again.attributes['TANGENT'], mesh.attributes['TANGENT'], atol=1e-7)

    def test_direction_morphs_preserve_weighted_transformed_vectors(self):
        for extension in ('glb', 'gltf'):
            for reflected in (False, True):
                with self.subTest(extension=extension, reflected=reflected):
                    path = self.source(extension, reflected=reflected, morph=True)
                    mesh = load_character_model(path).geometry.meshes[0]
                    sign = -1 if reflected else 1
                    for weight in (-.5, 0., .35, 1., 2.):
                        # Literal transformed components, calculated without importer helpers.
                        n = np.array([sign*(1/np.sqrt(2)+weight*.15)/2, -1/np.sqrt(2), weight*.2])
                        t = np.array([sign*np.sqrt(2), 1/np.sqrt(2), weight*.125])
                        for semantic, expected in (('NORMAL', n), ('TANGENT', t)):
                            actual = mesh.attributes[semantic][0, :3]+weight*mesh.morphs['bend'][semantic][0]
                            np.testing.assert_allclose(actual/np.linalg.norm(actual), expected/np.linalg.norm(expected), atol=1e-7)
                    np.testing.assert_allclose(mesh.morphs['bend']['POSITION'], np.tile([0, 0, .1], (3, 1)), atol=1e-7)
                    self.assertEqual(mesh.morph_defaults, {'bend': .35})

    def test_saved_run_keeps_exact_original_source_bytes(self):
        path = self.source('gltf', reflected=True, morph=True)
        original = sha256(path)
        rig = create_run(path, self.root/'run', identifier='scaled-fixture', fixture=True)
        self.assertEqual(rig.manifest['source']['sha256'], original)
        self.assertEqual(sha256(rig.root/rig.manifest['source']['path']), original)
        expected = np.array([-2, 1, 0])/np.sqrt(5)
        np.testing.assert_allclose(rig.geometry.meshes[0].attributes['TANGENT'][0, :3], expected, atol=1e-7)

    def test_tangents_without_normals_still_transform_as_directions(self):
        path = self.source('glb', normals=False)
        mesh = load_character_model(path).geometry.meshes[0]
        self.assertNotIn('NORMAL', mesh.attributes)
        np.testing.assert_allclose(mesh.attributes['TANGENT'][0], [2/np.sqrt(5), 1/np.sqrt(5), 0, 1], atol=1e-7)

    def test_invalid_direction_inputs_fail_without_changing_originals(self):
        for kwargs, code in (({'zero_tangent': True}, 'RIG_ATTRIBUTE'),
                             ({'normals': False, 'morph': True}, 'RIG_MORPH_ATTRIBUTE')):
            with self.subTest(kwargs=kwargs):
                path = self.source('gltf', **kwargs)
                original = path.read_bytes()
                with self.assertRaises(RigError) as caught:
                    load_character_model(path)
                self.assertEqual(caught.exception.report['code'], code)
                self.assertEqual(original, path.read_bytes())

    def test_multiple_weighted_direction_morphs_keep_linear_composition(self):
        path = self.source('glb', reflected=True, morph=True, multiple=True)
        mesh = load_character_model(path).geometry.meshes[0]
        for first, second in ((.35, -.2), (1., 1.), (-.5, 2.)):
            expected_n = np.array([-(1/np.sqrt(2)+first*.15)/2,
                                   -1/np.sqrt(2)+second*.05, first*.2])
            expected_t = np.array([-np.sqrt(2)-second*.2, 1/np.sqrt(2), first*.125])
            for semantic, expected in (('NORMAL', expected_n), ('TANGENT', expected_t)):
                actual = (mesh.attributes[semantic][0, :3]
                          +first*mesh.morphs['bend'][semantic][0]
                          +second*mesh.morphs['twist'][semantic][0])
                np.testing.assert_allclose(actual/np.linalg.norm(actual),
                                           expected/np.linalg.norm(expected), atol=1e-7)
