"""Tests require actual vertex movement and reject removed/corrupt bindings."""
from copy import deepcopy
import math
from pathlib import Path
import tempfile
import unittest
import numpy as np

from test_rig_math import chain_skeleton
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from rig_math import axis_angle
from rig_models import Geometry, Mesh, RigAsset, SkinWeights, load_geometry, load_weights, save_geometry, save_weights
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone, skeleton_from_heads
from rig_skinning import (copy_weights, edit_weights, evaluate_deformation, generate_skin_weights,
                          smooth_weights, validate_skin)


def test_geometry():
    return Geometry([Mesh('strip', np.array([[.1, 0, 0], [-.1, 0, 0], [.1, 2, 0], [-.1, 2, 0.]]),
                          np.array([[0, 1, 2], [1, 3, 2]]))], [dict(name='fixture')])


def bound_fixture():
    skeleton = chain_skeleton(2);geometry = test_geometry()
    world = evaluate_fk(skeleton)
    skin = SkinWeights('strip', ['b0', 'b1'], np.array([[0, 0, 0, 0], [0, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]], dtype=np.uint16),
                       np.array([[1, 0, 0, 0.]]*4), np.array([np.linalg.inv(world['b0']), np.linalg.inv(world['b1'])]))
    return RigAsset({'skeleton': skeleton}, geometry, {'strip': skin})


def pruning_fixture():
    """Ten actual segments: three near and seven equally farther away.

    Unit segments give sigma=.3. Moving a parallel segment by
    .3*sqrt(2*log(2)) halves its Gaussian score at every fixture vertex.
    Thus the near:far scores are 2:1, independent of the common Z offset.
    The expected retained ratios below are hand-derived from those scores.
    """
    offset = .3*math.sqrt(2*math.log(2))
    specs = [dict(id='root', role='root', parent=None, head=[0, -1, 0],
                  tail=[0, 0, 0], deform=False)]
    for i in range(10):
        x = 0 if i in (1, 4, 8) else offset
        specs.append(dict(id=f'j{i}', role=f'part-{i}', parent='root',
                          head=[x, 0, 0], tail=[x, 1, 0], deform=True))
    skeleton = skeleton_from_heads(specs, source='procedural-fixture',
                                   reference='Analytic pruning fixture', confidence=1.)
    geometry = Geometry([Mesh('pruning', np.array([[0, .2, 0], [0, .8, 0], [0, .5, .1]]),
                              np.array([[0, 1, 2]]))], [dict(name='fixture')])
    return geometry, skeleton


class RigSkinningTests(unittest.TestCase):
    def test_known_pivot_rotation_moves_real_vertices(self):
        rig = bound_fixture()
        pose = rotate_bone(rig.skeleton, neutral_pose(rig.skeleton), 'b1', axis_angle([0, 0, 1], math.pi/2))
        vertices = evaluate_deformation(rig, pose)['strip']['positions']
        np.testing.assert_allclose(vertices[2], [-1, 1.1, 0], atol=1e-12)
        np.testing.assert_allclose(vertices[:2], rig.geometry.meshes[0].positions[:2], atol=1e-12)
        self.assertGreater(np.linalg.norm(vertices-rig.geometry.meshes[0].positions), 1)

    def test_bind_pose_reconstructs_geometry(self):
        rig = bound_fixture();report = validate_skin(rig.weights['strip'], rig.geometry.meshes[0], rig.skeleton)
        self.assertLess(report['inverse_bind_max_error'], 1e-12)
        np.testing.assert_allclose(evaluate_deformation(rig)['strip']['positions'], rig.geometry.meshes[0].positions)

    def test_removed_skin_and_zero_weights_cannot_pass(self):
        rig = bound_fixture();rig.weights = {}
        with self.assertRaises(RigError):evaluate_deformation(rig)
        rig = bound_fixture();rig.weights['strip'].weights[:] = 0
        with self.assertRaises(RigError):validate_skin(rig.weights['strip'], rig.geometry.meshes[0], rig.skeleton)

    def test_wrong_joint_matrix_nonfinite_and_negative_weights_fail(self):
        for change in (lambda s:s.indices.__setitem__((0, 0), 9),
                       lambda s:s.weights.__setitem__((0, 0), -.1),
                       lambda s:s.weights.__setitem__((0, 0), np.nan),
                       lambda s:s.inverse_bind.__setitem__((0, 0, 3), 1)):
            rig = bound_fixture();change(rig.weights['strip'])
            with self.assertRaises(RigError):validate_skin(rig.weights['strip'], rig.geometry.meshes[0], rig.skeleton)

    def test_diffusion_distance_rigid_and_locked_manual_weights(self):
        geometry = test_geometry();skeleton = chain_skeleton(2)
        for method in ('distance', 'diffusion', 'rigid'):
            weights = generate_skin_weights(geometry, skeleton, method=method)
            self.assertLess(validate_skin(weights['strip'], geometry.meshes[0], skeleton)['weight_sum_max_error'], 1e-12)
        weight = edit_weights(weights['strip'], {0: {'b1': 1}})
        updated = generate_skin_weights(geometry, skeleton, previous={'strip': weight})['strip']
        self.assertEqual(updated.joints[int(updated.indices[0, 0])], 'b1')
        self.assertEqual(updated.locked.tolist(), [0])
        copied = copy_weights(updated, {1: 0})
        smoothed = smooth_weights(geometry.meshes[0], copied)
        np.testing.assert_allclose(smoothed.weights[0], copied.weights[0])

    def test_actual_morph_delta_is_applied_before_skinning(self):
        rig = bound_fixture();mesh = rig.geometry.meshes[0]
        delta = np.zeros_like(mesh.positions);delta[:, 2] = .4
        mesh.morphs['expression'] = {'POSITION': delta}
        pose = neutral_pose(rig.skeleton);pose['morphs'] = {'strip': {'expression': .5}}
        np.testing.assert_allclose(evaluate_deformation(rig, pose)['strip']['positions'][:, 2], [.2]*4)

    def test_portable_binary_roundtrip_and_tamper_detection(self):
        rig = bound_fixture()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            descriptor = save_geometry(root, rig.geometry)
            duplicate = save_geometry(root, rig.geometry)
            self.assertEqual(descriptor, duplicate)
            np.testing.assert_allclose(load_geometry(root, descriptor).meshes[0].positions, rig.geometry.meshes[0].positions)
            descriptor = save_weights(root, rig.weights['strip'])
            np.testing.assert_allclose(load_weights(root, descriptor).weights, rig.weights['strip'].weights)
            (root/descriptor['data']['path']).write_bytes(b'corrupt')
            with self.assertRaises(RigError):load_weights(root, descriptor)


    def test_real_top_four_and_eight_pruning_has_expected_ratios_and_stable_ties(self):
        geometry, skeleton = pruning_fixture()
        for width, expected_indices, scores in (
            (4, [1, 4, 8, 0], [2, 2, 2, 1]),
            (8, [1, 4, 8, 0, 2, 3, 5, 6], [2, 2, 2, 1, 1, 1, 1, 1]),
        ):
            with self.subTest(influences=width):
                limits = {**DEFAULT_LIMITS, 'influences': width}
                skin = generate_skin_weights(geometry, skeleton, method='distance', limits=limits)['pruning']
                expected = np.array(scores, dtype=float)/sum(scores)
                np.testing.assert_array_equal(skin.indices, np.tile(expected_indices, (3, 1)))
                np.testing.assert_allclose(skin.weights, np.tile(expected, (3, 1)), atol=1e-12)
                self.assertTrue(np.all(np.count_nonzero(skin.weights, axis=1) == width))
                self.assertLess(validate_skin(skin, geometry.meshes[0], skeleton, limits)['weight_sum_max_error'], 1e-12)
                rig = RigAsset({'skeleton': skeleton}, geometry, {'pruning': skin})
                np.testing.assert_allclose(evaluate_deformation(rig)['pruning']['positions'],
                                           geometry.meshes[0].positions, atol=1e-12)
                self.assertEqual(skin.joints, [f'j{i}' for i in range(10)])

    def test_diffusion_candidate_boundary_and_memory_fallback_are_deterministic(self):
        geometry = test_geometry()
        for count in (64, 65):
            # Coincident segments have equal scores; stable ties select the first four.
            specs = [dict(id=f'b{i}', role=f'part-{i}', parent='b0' if i else None,
                          head=[0, 0, 0], tail=[0, 1, 0]) for i in range(count)]
            skeleton = skeleton_from_heads(specs)
            for method in ('auto', 'diffusion', 'segmented'):
                with self.subTest(candidates=count, method=method):
                    first = generate_skin_weights(geometry, skeleton, method=method)['strip']
                    second = generate_skin_weights(geometry, skeleton, method=method)['strip']
                    self.assertEqual(first.method, 'diffusion' if count == 64 else 'distance')
                    if count == 64:
                        self.assertIsNone(first.fallback_reason)
                    else:
                        self.assertIn('bound', first.fallback_reason)
                        self.assertIn('segment-distance', first.fallback_reason)
                    self.assertEqual(first.fallback_reason, second.fallback_reason)
                    np.testing.assert_array_equal(first.indices, np.tile([0, 1, 2, 3], (4, 1)))
                    np.testing.assert_allclose(first.weights, .25, atol=1e-12)
                    np.testing.assert_array_equal(first.indices, second.indices)
                    np.testing.assert_array_equal(first.weights, second.weights)
                    self.assertLess(validate_skin(first, geometry.meshes[0], skeleton)['weight_sum_max_error'], 1e-12)
                    if count == 65 and method == 'diffusion':
                        with tempfile.TemporaryDirectory() as directory:
                            descriptor = save_weights(Path(directory), first)
                            restored = load_weights(Path(directory), descriptor)
                            self.assertEqual(restored.fallback_reason, first.fallback_reason)
                            self.assertEqual(restored.method, 'distance')
                            np.testing.assert_array_equal(restored.weights, first.weights)
        # Geometry needs 384 estimated bytes; the two-candidate diffusion estimate
        # is 240 bytes. A 400-byte budget admits geometry but forces fallback.
        geometry, skeleton = pruning_fixture()
        geometry.meshes[0].region = 'unused-semantic-label'
        regions = {'pruning': ['j1', 'j4']}
        limits = {**DEFAULT_LIMITS, 'memory_bytes': 400}
        fallback = generate_skin_weights(geometry, skeleton, method='diffusion',
                                         regions=regions, limits=limits)['pruning']
        self.assertEqual(fallback.method, 'distance')
        self.assertIn('bound', fallback.fallback_reason)
        np.testing.assert_array_equal(fallback.indices[:, :2], np.tile([1, 4], (3, 1)))
        np.testing.assert_allclose(fallback.weights, np.tile([.5, .5, 0, 0], (3, 1)), atol=1e-12)

    def test_disconnected_islands_and_isolated_vertex_keep_independent_weights(self):
        skeleton = chain_skeleton(2)
        lower = np.array([[.1, 0, 0], [-.1, 0, 0], [0, .2, .1]])
        upper = lower + [0, 1.8, 0]
        mesh = Mesh('islands', np.vstack([lower, upper, [0, 1, .2]]),
                    np.array([[0, 1, 2], [3, 4, 5]]))
        geometry = Geometry([mesh], [dict(name='fixture')])
        original = deepcopy(geometry)
        skin = generate_skin_weights(geometry, skeleton, method='diffusion', iterations=8)['islands']
        self.assertEqual(skin.method, 'diffusion')
        self.assertIsNone(skin.fallback_reason)
        for start, points in ((0, lower), (3, upper)):
            part = Geometry([Mesh('part', points, np.array([[0, 1, 2]]))], geometry.materials)
            separate = generate_skin_weights(part, skeleton, method='diffusion', iterations=8)['part']
            np.testing.assert_array_equal(skin.indices[start:start+3], separate.indices)
            np.testing.assert_allclose(skin.weights[start:start+3], separate.weights, atol=1e-12)
        # The isolated midpoint is equidistant from both bones and has no neighbours.
        np.testing.assert_allclose(skin.weights[6], [.5, .5, 0, 0], atol=1e-12)
        self.assertLess(validate_skin(skin, mesh, skeleton)['weight_sum_max_error'], 1e-12)
        np.testing.assert_array_equal(mesh.positions, original.meshes[0].positions)
        np.testing.assert_array_equal(mesh.triangles, original.meshes[0].triangles)
        rig = RigAsset({'skeleton': skeleton}, geometry, {'islands': skin})
        np.testing.assert_allclose(evaluate_deformation(rig)['islands']['positions'], mesh.positions, atol=1e-12)

    def test_smoothing_preserves_isolated_vertices_at_full_strength_and_smooths_edges(self):
        rig = bound_fixture()
        mesh = Mesh('strip', np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [2, 2, 0.]]),
                    np.array([[0, 1, 2]]))
        skin = edit_weights(rig.weights['strip'],
                            {0: {'b0': 1}, 1: {'b1': 1}, 2: {'b1': 1},
                             3: {'b0': 3, 'b1': 1}}, lock=False)
        before = deepcopy(skin)
        for strength in (.3, 1.):
            with self.subTest(strength=strength):
                smoothed = smooth_weights(mesh, skin, iterations=1, strength=strength)
                validate_skin(smoothed, mesh, rig.skeleton)
                actual = np.zeros((4, 2))
                for vertex in range(4):
                    for joint, weight in zip(smoothed.indices[vertex], smoothed.weights[vertex]):
                        actual[vertex, joint] += weight
                np.testing.assert_allclose(actual,
                    [[1-strength, strength], [.5*strength, 1-.5*strength],
                     [.5*strength, 1-.5*strength], [.75, .25]], atol=1e-12)
                np.testing.assert_array_equal(skin.indices, before.indices)
                np.testing.assert_array_equal(skin.weights, before.weights)
        # Manual locks are authoritative even when connected neighbours differ.
        locked = edit_weights(skin, {0: {'b0': 1}})
        smoothed = smooth_weights(mesh, locked, iterations=2, strength=1)
        np.testing.assert_array_equal(smoothed.indices[0], locked.indices[0])
        np.testing.assert_array_equal(smoothed.weights[0], locked.weights[0])
        np.testing.assert_array_equal(smoothed.weights[3], skin.weights[3])

    def test_eight_influence_manual_locks_survive_regeneration_and_missing_joint_fails(self):
        geometry, skeleton = pruning_fixture()
        limits = {**DEFAULT_LIMITS, 'influences': 8}
        skin = generate_skin_weights(geometry, skeleton, method='distance', limits=limits)['pruning']
        edited = edit_weights(skin, {0: {f'j{i}': i+1 for i in range(8)}}, max_influences=8)
        np.testing.assert_allclose(edited.weights[0], np.arange(8, 0, -1)/36, atol=1e-12)
        self.assertEqual(edited.locked.tolist(), [0])
        regenerated = generate_skin_weights(geometry, skeleton, method='diffusion',
                                             previous={'pruning': edited}, limits=limits)['pruning']
        np.testing.assert_array_equal(regenerated.indices[0], edited.indices[0])
        np.testing.assert_allclose(regenerated.weights[0], edited.weights[0], atol=1e-12)
        self.assertEqual(regenerated.locked.tolist(), [0])
        validate_skin(regenerated, geometry.meshes[0], skeleton, limits)
        # A removed joint must reject a retained manual edit, preserving the input.
        incompatible = deepcopy(skeleton)
        incompatible['bones'] = [b for b in incompatible['bones'] if b['id'] != 'j7']
        before = deepcopy(edited)
        with self.assertRaises(RigError) as caught:
            generate_skin_weights(geometry, incompatible, previous={'pruning': edited}, limits=limits)
        self.assertEqual(caught.exception.report['code'], 'RIG_WEIGHT_EDIT')
        np.testing.assert_array_equal(edited.weights, before.weights)
        np.testing.assert_array_equal(edited.indices, before.indices)

    def test_separate_semantic_rigid_parts_keep_materials_and_pivot_motion(self):
        skeleton = chain_skeleton(2)
        base = Mesh('base', np.array([[0, 0, 0], [1, 0, 0], [0, .5, 0.]]),
                    np.array([[0, 1, 2]]), material=0, region='b0')
        hinge = Mesh('hinge', np.array([[0, 1, 0], [1, 1, 0], [0, 2, 0.]]),
                     np.array([[0, 1, 2]]), material=1, region='b1')
        geometry = Geometry([base, hinge], [dict(name='base-metal'), dict(name='hinge-metal')])
        before = deepcopy(geometry)
        weights = generate_skin_weights(geometry, skeleton, method='segmented')
        self.assertEqual(set(weights), {'base', 'hinge'})
        rig = RigAsset({'skeleton': skeleton}, geometry, weights)
        pose = rotate_bone(skeleton, neutral_pose(skeleton), 'b1', axis_angle([0, 0, 1], math.pi/2))
        moved = evaluate_deformation(rig, pose)
        np.testing.assert_allclose(moved['base']['positions'], base.positions, atol=1e-12)
        np.testing.assert_allclose(moved['hinge']['positions'], [[0, 1, 0], [0, 2, 0], [-1, 1, 0]], atol=1e-12)
        self.assertEqual(geometry.materials, before.materials)
        for mesh, saved in zip(geometry.meshes, before.meshes):
            self.assertEqual((mesh.id, mesh.region, mesh.material), (saved.id, saved.region, saved.material))
            np.testing.assert_array_equal(mesh.positions, saved.positions)
            np.testing.assert_array_equal(mesh.triangles, saved.triangles)
            self.assertEqual(weights[mesh.id].method, 'rigid')
            np.testing.assert_allclose(weights[mesh.id].weights[:, 0], 1)
        del rig.weights['hinge']
        with self.assertRaises(RigError) as caught:
            evaluate_deformation(rig, pose)
        self.assertEqual(caught.exception.report['code'], 'RIG_MISSING_SKIN')


    def test_weighted_lbs_uses_literal_inverse_binds_in_skin_joint_order(self):
        rig = bound_fixture()
        skin = rig.weights['strip']
        # Both rest orientations are identity; b1's head is (0,1,0).
        skin.joints = ['b1', 'b0']
        skin.inverse_bind = np.array([
            [[1, 0, 0, 0], [0, 1, 0, -1], [0, 0, 1, 0], [0, 0, 0, 1]],
            np.eye(4),
        ], dtype=float)
        skin.indices[:] = [0, 1, 0, 0]
        skin.weights[:] = [.75, .25, 0, 0]
        validate_skin(skin, rig.geometry.meshes[0], rig.skeleton)
        np.testing.assert_allclose(evaluate_deformation(rig)['strip']['positions'],
                                   rig.geometry.meshes[0].positions, atol=1e-12)
        pose = rotate_bone(rig.skeleton, neutral_pose(rig.skeleton), 'b1',
                           axis_angle([0, 0, 1], math.pi/2))
        # .75*(-1,1.1,0) + .25*(.1,2,0), derived about pivot (0,1,0).
        np.testing.assert_allclose(evaluate_deformation(rig, pose)['strip']['positions'][2],
                                   [-.725, 1.325, 0], atol=1e-12)
        skin.inverse_bind = skin.inverse_bind[::-1].copy()
        with self.assertRaises(RigError) as caught:
            validate_skin(skin, rig.geometry.meshes[0], rig.skeleton)
        self.assertEqual(caught.exception.report['code'], 'RIG_BIND_MATRIX')
