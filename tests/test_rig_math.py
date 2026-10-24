"""Independent coordinate expectations and malformed-transform regression tests."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import math
import tempfile
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from rig_errors import RigError
from rig_math import (BLENDER_FROM_CANONICAL, axis_angle, compose, decompose, qbetween,
                      qmatrix, quaternion, slerp, transform_points)
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone, skeleton_from_heads, validate_skeleton
from rig_gltf import GltfBuilder, export_geometry, node_matrix
from rig_import import load_character_model
from rig_errors import sha256


def chain_skeleton(count=3):
    return skeleton_from_heads([dict(id=f'b{i}', role=f'joint-{i}', parent=f'b{i-1}' if i else None,
        head=[0, i, 0], tail=[0, i+1, 0], deform=True) for i in range(count)],
        source='procedural-fixture', reference='Independent unit chain', confidence=1.)


class RigMathTests(unittest.TestCase):
    def test_gltf_invertibility_is_independent_of_units_and_axis_magnitudes(self):
        for scale in ([1e-6]*3, [1e6]*3, [1e-100]*3, [1e100]*3,
                      [1e-6, 1, 1e6], [-1e-6, 2e-6, 3e-6]):
            for form in ('matrix', 'trs'):
                with self.subTest(scale=scale, form=form):
                    matrix = np.diag([*scale, 1.])
                    node = {'matrix': matrix.T.ravel().tolist()} if form == 'matrix' else {'scale': scale}
                    saved = deepcopy(node)
                    actual = node_matrix(node)
                    np.testing.assert_array_equal(actual, matrix)
                    np.testing.assert_allclose(np.linalg.inv(actual) @ actual, np.eye(4), atol=1e-14)
                    self.assertEqual(node, saved)
        shear = np.array([[2e-6, 1e-6, 0, .5], [0, 3e-6, 0, -.5], [0, 0, 4e-6, 1], [0, 0, 0, 1.]])
        np.testing.assert_array_equal(node_matrix({'matrix': shear.T.ravel().tolist()}), shear)

    def test_gltf_singular_unstable_and_nonrepresentable_inverses_fail(self):
        singular = np.eye(4);singular[:, 1] = singular[:, 0]
        unstable = np.eye(4);unstable[:3, 1] = [1, 1e-14, 0]
        nonaffine = np.eye(4);nonaffine[3, 0] = .1
        infinite = np.eye(4);infinite[0, 0] = np.inf
        translation_overflow = np.diag([1e-6]*3+[1.]);translation_overflow[0, 3] = 1e308
        for matrix in (singular, unstable, nonaffine, infinite,
                       np.diag([1e-320]*3+[1.]), translation_overflow):
            with self.subTest(matrix=matrix.tolist()), self.assertRaises(RigError):
                node_matrix({'matrix': matrix.T.ravel().tolist()})
        for scale in ([0, 1, 1], [1, 0, 1], [1, 1, 0]):
            with self.subTest(scale=scale), self.assertRaises(RigError):node_matrix({'scale': scale})

    def test_extreme_multimesh_import_fails_before_losing_handedness_or_normals(self):
        # An ordinary second mesh makes the whole asset's extent valid, so a
        # bad tiny mesh cannot hide behind the global geometry-extent guard.
        with tempfile.TemporaryDirectory() as temporary:
            for scale in ([-1e-108]*3, [1e-160, 1, 1], [1e108]*3):
                with self.subTest(scale=scale):
                    builder = GltfBuilder()
                    builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
                    attributes = {'POSITION': builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3'),
                                  'NORMAL': builder.accessor([[1, 0, 0]]*3, 'VEC3'),
                                  'TANGENT': builder.accessor([[0, 1, 0, 1]]*3, 'VEC4')}
                    builder.json['meshes'] = [dict(primitives=[dict(attributes=attributes)])]
                    builder.json['nodes'] = [dict(mesh=0, scale=scale), dict(mesh=0)]
                    builder.json['scenes'] = [dict(nodes=[0, 1])]
                    for extension in ('glb', 'gltf'):
                        path = Path(temporary)/('extreme.'+extension);builder.write(path)
                        before = sha256(path)
                        with self.assertRaises(RigError) as error:load_character_model(path)
                        self.assertEqual(error.exception.report['code'], 'RIG_GLTF_NUMERIC_RANGE')
                        self.assertEqual(sha256(path), before)

    def test_composed_world_and_import_scale_receive_numeric_range_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            for nested in (False, True):
                builder = GltfBuilder()
                builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
                builder.json['meshes'] = [dict(primitives=[dict(attributes={
                    'POSITION': builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3')})])]
                if nested:
                    builder.json['nodes'] = [dict(scale=[1e-6]*3, children=[i+1]) for i in range(18)]
                    builder.json['nodes'] += [dict(mesh=0), dict(mesh=0)]
                    roots = [0, 19];import_scale = 1.
                else:
                    builder.json['nodes'] = [dict(mesh=0, scale=[-1e-102, 1e-102, 1e-102]),
                                             dict(mesh=0, scale=[1e6]*3)]
                    roots = [0, 1];import_scale = 1e-6
                builder.json['scenes'] = [dict(nodes=roots)]
                path = Path(temporary)/'composed.glb';builder.write(path);before = sha256(path)
                with self.subTest(nested=nested), self.assertRaises(RigError) as error:
                    load_character_model(path, scale=import_scale)
                self.assertEqual(error.exception.report['code'], 'RIG_GLTF_NUMERIC_RANGE')
                self.assertEqual(sha256(path), before)

    def test_real_gltf_and_glb_scale_import_roundtrip_preserves_geometry_and_source(self):
        positions = np.array([[0, 0, 0], [2, 0, 0], [0, 3, 1]], dtype=float)
        normal = np.array([0, -1, 3.]);normal /= np.linalg.norm(normal)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for extension in ('glb', 'gltf'):
                for index, scale in enumerate(([1e-6]*3, [1e6]*3, [1e-6, 1, 1e6], [-1e-6, 2e-6, 3e-6])):
                    with self.subTest(extension=extension, scale=scale):
                        matrix = np.diag([*scale, 1.])
                        matrix[:3, 3] = [2e-5, 3e-5, -1e-5]
                        builder = GltfBuilder()
                        builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
                        primitive = dict(attributes={'POSITION': builder.accessor(positions, 'VEC3'),
                            'NORMAL': builder.accessor(np.tile(normal, (3, 1)), 'VEC3')},
                            indices=builder.accessor([0, 1, 2], 'SCALAR', 5125))
                        builder.json['meshes'] = [dict(name='Engineering triangle', primitives=[primitive])]
                        builder.json['nodes'] = [dict(mesh=0, matrix=matrix.T.ravel().tolist())]
                        builder.json['scenes'] = [dict(nodes=[0])]
                        source = root/f'source-{index}.{extension}';builder.write(source)
                        source_hash = sha256(source)
                        model = load_character_model(source);mesh = model.geometry.meshes[0]
                        expected = positions * scale + matrix[:3, 3]
                        np.testing.assert_allclose(mesh.positions, expected, rtol=1e-14, atol=1e-18)
                        expected_normal = np.linalg.solve(matrix[:3, :3].T, normal)
                        expected_normal /= np.linalg.norm(expected_normal)
                        np.testing.assert_allclose(mesh.attributes['NORMAL'], np.tile(expected_normal, (3, 1)), atol=1e-7)
                        expected_faces = [[0, 2, 1]] if np.prod(scale) < 0 else [[0, 1, 2]]
                        np.testing.assert_array_equal(mesh.triangles, expected_faces)
                        destination = root/f'roundtrip-{index}.{extension}'
                        export_geometry(model.geometry, destination, provenance={'fixture': True})
                        reloaded = load_character_model(destination)
                        np.testing.assert_allclose(reloaded.geometry.meshes[0].positions, expected, rtol=1e-6, atol=1e-12)
                        self.assertTrue(model.report['fixture']);self.assertTrue(reloaded.report['fixture'])
                        self.assertEqual(sha256(source), source_hash)
                        if index == 0:
                            from rig_contracts import DEFAULT_LIMITS
                            with self.assertRaises(RigError) as limited:
                                load_character_model(source, limits={**DEFAULT_LIMITS, 'source_bytes': 8})
                            self.assertEqual(limited.exception.report['code'], 'RIG_LIMIT_SOURCE')
                            self.assertEqual(sha256(source), source_hash)
                            from rig import main
                            run = root/('cli-'+extension)
                            output = io.StringIO()
                            with redirect_stdout(output):
                                code = main(['import', '--model', str(source), '--run-dir', str(run)])
                            self.assertEqual(code, 0, output.getvalue())
                            manifest = json.loads((run/'rig.json').read_text())
                            self.assertEqual(sha256(run/manifest['source']['path']), source_hash)
                            self.assertEqual(sha256(source), source_hash)
                            builder.json['nodes'][0]['matrix'][0] = 0
                            bad = root/('singular.'+extension);builder.write(bad)
                            bad_hash = sha256(bad);destination = root/('bad-cli-'+extension)
                            output = io.StringIO()
                            with redirect_stdout(output):
                                code = main(['import', '--model', str(bad), '--run-dir', str(destination)])
                            self.assertEqual(code, 1)
                            self.assertEqual(json.loads(output.getvalue())['error']['code'], 'RIG_SINGULAR_TRANSFORM')
                            self.assertFalse(destination.exists())
                            self.assertEqual(sha256(bad), bad_hash)

    def test_column_vector_transform_against_literal_matrix(self):
        expected = np.array([[0, -3, 0, 4], [2, 0, 0, 5], [0, 0, 4, 6], [0, 0, 0, 1.]])
        actual = compose([4, 5, 6], [0, 0, math.sqrt(.5), math.sqrt(.5)], [2, 3, 4])
        np.testing.assert_allclose(actual, expected, atol=1e-12)
        np.testing.assert_allclose(transform_points(actual, np.array([[1, 2, 3.]])), [[-2, 7, 18]], atol=1e-12)

    def test_blender_conversion_is_proper_and_reversible(self):
        np.testing.assert_allclose(transform_points(BLENDER_FROM_CANONICAL, np.array([[1, 2, 3.]])), [[1, -3, 2]])
        self.assertAlmostEqual(np.linalg.det(BLENDER_FROM_CANONICAL), 1)

    def test_slerp_handles_opposite_sign_and_half_angle(self):
        q = slerp([0, 0, 0, 1], [0, 0, 1, 0], .5)
        np.testing.assert_allclose(qmatrix(q) @ [1, 0, 0], [0, 1, 0], atol=1e-12)
        np.testing.assert_allclose(slerp(q, -q, .2), q, atol=1e-12)

    def test_antiparallel_rotation_is_finite_and_exact(self):
        np.testing.assert_allclose(qmatrix(qbetween([0, 1, 0], [0, -1, 0])) @ [0, 1, 0], [0, -1, 0], atol=1e-12)

    def test_invalid_transforms_fail_closed(self):
        for q in ([0, 0, 0, 0], [float('nan'), 0, 0, 1]):
            with self.assertRaises(RigError):quaternion(q)
        with self.assertRaises(RigError):compose(scale=[1, 0, 1])
        shear = np.eye(4);shear[0, 1] = .3
        with self.assertRaises(RigError):decompose(shear)

    def test_fk_parent_rotation_propagates_without_mutation(self):
        skeleton = chain_skeleton();saved = deepcopy(skeleton)
        pose = rotate_bone(skeleton, neutral_pose(skeleton), 'b0', axis_angle([0, 0, 1], math.pi/2))
        world = evaluate_fk(skeleton, pose)
        np.testing.assert_allclose(world['b2'][:3, 3], [-2, 0, 0], atol=1e-12)
        self.assertEqual(skeleton, saved)
        np.testing.assert_allclose(evaluate_fk(skeleton)['b2'][:3, 3], [0, 2, 0])

    def test_duplicate_cycle_missing_parent_and_false_head_are_rejected(self):
        for mutate in (lambda s:s['bones'].append(deepcopy(s['bones'][0])),
                       lambda s:s['bones'][1].update(parent='b2'),
                       lambda s:s['bones'][2].update(parent='missing'),
                       lambda s:s['bones'][1].update(head=[7, 7, 7])):
            skeleton = chain_skeleton();mutate(skeleton)
            with self.assertRaises(RigError):validate_skeleton(skeleton)

    def test_seeded_random_trees_reconstruct_independent_translations(self):
        rng = np.random.default_rng(6381)
        for _ in range(20):
            heads = rng.normal(size=(20, 3))
            specs = [dict(id=f'b{i}', role=f'part-{i}', parent=None if i == 0 else f'b{rng.integers(i)}',
                          head=head.tolist(), tail=(head+[0, .3, 0]).tolist()) for i, head in enumerate(heads)]
            skeleton = skeleton_from_heads(specs)
            world = evaluate_fk(skeleton)
            np.testing.assert_allclose(np.array([world[f'b{i}'][:3, 3] for i in range(20)]), heads, atol=1e-10)
