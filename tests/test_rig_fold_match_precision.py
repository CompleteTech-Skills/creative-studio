"""Native closed-chain pole changes must survive the matching precision boundary.

These synthetic fixtures run real FK, IK, constraints and blended skinning. The
position oracle is the literal rotation of the original closed chain, rather
than the solver's convergence flag or a dot product that loses small angles.
"""
from copy import deepcopy
import json
import math
import unittest

import numpy as np

from rig_errors import RigError
from rig_ik import match_fk_to_ik, solve_ik
from rig_models import Geometry, Mesh, RigAsset, SkinWeights
from rig_skeleton import evaluate_fk, neutral_pose, skeleton_from_heads
from rig_skinning import evaluate_deformation, validate_skin


def closed_fixture(length=1.):
    """Four equal alternating segments with a genuine two-influence skin."""
    skeleton = skeleton_from_heads([
        dict(id=f'b{i}', role=f'joint-{i}', parent=f'b{i-1}' if i else None,
             head=[0., i*length, 0.], tail=[0., (i+1)*length, 0.], deform=True)
        for i in range(5)], source='procedural-fixture',
        reference='Synthetic closed-chain pole precision', confidence=1.)
    pose = neutral_pose(skeleton)
    for name in ('b1', 'b2', 'b3'):
        pose['bones'][name] = dict(rotation=[1., 0., 0., 0.])
    names = [bone['id'] for bone in skeleton['bones']]
    positions = length*np.array([
        [x, i+y, z] for i in range(5)
        for x, y, z in ((.13, .02, .07), (-.17, .09, .04), (.06, -.08, -.11))])
    mesh = Mesh('precision-mesh', positions, np.arange(15).reshape(-1, 3))
    rest = evaluate_fk(skeleton)
    skin = SkinWeights(mesh.id, names,
                       np.array([[i, (i+1) % 5] for i in range(5) for _ in range(3)]),
                       np.tile([.65, .35], (15, 1)),
                       np.array([np.linalg.inv(rest[name]) for name in names]))
    rig = RigAsset({'skeleton': skeleton}, Geometry([mesh]), {mesh.id: skin})
    declaration = dict(schema_version=1, id='precision', bones=names, solver='fabrik',
                       pole=[0., length, 0.], tolerance=1e-8, iterations=128)
    return rig, pose, declaration


def rotation_toward_pole(angle):
    """A literal world Z rotation taking +Y to [sin(angle), cos(angle), 0]."""
    sine, cosine = math.sin(angle), math.cos(angle)
    return np.array([[cosine, sine, 0.], [-sine, cosine, 0.], [0., 0., 1.]])


class RigFoldMatchPrecisionTests(unittest.TestCase):
    def assert_native_report(self, rig, result, declaration, target, orientation=None):
        world = evaluate_fk(rig.skeleton, result.pose)
        matrices = np.array(list(world.values()))
        self.assertTrue(np.isfinite(matrices).all())
        for matrix in matrices:
            np.testing.assert_allclose(matrix[:3, :3].T @ matrix[:3, :3],
                                       np.eye(3), rtol=0, atol=2e-12)
        measured = float(np.linalg.norm(world['b4'][:3, 3]-target))
        self.assertAlmostEqual(result.residual_m, measured, delta=1e-12)
        expected = result.reachable and measured <= declaration['tolerance']
        if orientation is None:
            self.assertIsNone(result.orientation_residual_rad)
        else:
            x, y, z, w = np.asarray(orientation)/np.linalg.norm(orientation)
            goal = np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                             [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                             [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])
            relative = goal.T @ world['b4'][:3, :3]
            angular = math.acos(float(np.clip((np.trace(relative)-1)/2, -1., 1.)))
            self.assertAlmostEqual(result.orientation_residual_rad, angular, delta=5e-8)
            expected = expected and angular <= 1e-5
        self.assertEqual(result.converged, expected)
        self.assertGreaterEqual(result.iterations, 0)
        self.assertLessEqual(result.iterations, declaration['iterations'])
        json.dumps(result.report(), allow_nan=False)
        return world

    def assert_skin(self, rig, pose, world):
        mesh = rig.geometry.meshes[0]
        skin = rig.weights[mesh.id]
        self.assertLess(validate_skin(skin, mesh, rig.skeleton)['weight_sum_max_error'], 1e-12)
        expected = np.zeros_like(mesh.positions)
        for row, vertex in enumerate(mesh.positions):
            for index, weight in zip(skin.indices[row], skin.weights[row]):
                expected[row] += weight*(world[skin.joints[index]] @ skin.inverse_bind[index]
                                         @ np.append(vertex, 1.))[:3]
        actual = evaluate_deformation(rig, pose)[mesh.id]['positions']
        self.assertTrue(np.isfinite(actual).all())
        np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-9)
        return actual

    def assert_pole_change(self, magnitude, explicit):
        for length in (1., 1000.):
            for sign in (-1., 1.):
                angle = sign*magnitude
                with self.subTest(length=length, angle=angle, explicit=explicit):
                    rig, pose, declaration = closed_fixture(length)
                    before = evaluate_fk(rig.skeleton, pose)
                    vertices = self.assert_skin(rig, pose, before)
                    target = before['b4'][:3, 3].copy()
                    np.testing.assert_array_equal(target, np.zeros(3))
                    direction = np.array([math.sin(angle), math.cos(angle), 0.])
                    pole = length*direction
                    # Exercise both the chain declaration and call-site override.
                    kwargs = {'pole': pole} if explicit else {}
                    if not explicit:
                        declaration['pole'] = pole.tolist()
                    saved = deepcopy((rig.skeleton, pose, declaration))
                    result = solve_ik(rig.skeleton, pose, declaration, target, **kwargs)
                    self.assertEqual((rig.skeleton, pose, declaration), saved)
                    after = self.assert_native_report(rig, result, declaration, target)
                    self.assertTrue(result.reachable)
                    self.assertTrue(result.converged)
                    error = float(np.linalg.norm(after['b1'][:3, 3]-length*direction))
                    self.assertLess(error, 1e-8,
                                    f'pole ignored: length={length}, angle={angle}, '
                                    f'elbow_error_m={error}, iterations={result.iterations}, '
                                    f'endpoint_residual_m={result.residual_m}')
                    rotation = rotation_toward_pole(angle)
                    transform = np.eye(4)
                    transform[:3, :3] = rotation
                    for name in before:
                        np.testing.assert_allclose(after[name], transform @ before[name],
                                                   rtol=0, atol=1e-8)
                    points = np.array([after[name][:3, 3] for name in declaration['bones']])
                    np.testing.assert_allclose(np.linalg.norm(np.diff(points, axis=0), axis=1),
                                               np.full(4, length), rtol=0, atol=2e-9)
                    moved = self.assert_skin(rig, result.pose, after)
                    np.testing.assert_allclose(moved, vertices @ rotation.T, rtol=0, atol=1e-8)
                    self.assertGreater(np.max(np.linalg.norm(moved-vertices, axis=1)),
                                       length*abs(math.sin(angle))*.1)

    def test_declared_microradian_pole_change_moves_native_chain(self):
        self.assert_pole_change(1e-6, explicit=False)

    def test_override_microradian_pole_change_moves_native_chain(self):
        self.assert_pole_change(1e-6, explicit=True)

    def test_declared_tenth_microradian_pole_change_moves_native_chain(self):
        self.assert_pole_change(1e-7, explicit=False)

    def test_override_tenth_microradian_pole_change_moves_native_chain(self):
        self.assert_pole_change(1e-7, explicit=True)

    def test_larger_pole_change_passing_control(self):
        for explicit in (False, True):
            self.assert_pole_change(1e-5, explicit)

    def test_exact_match_preserves_every_world_transform_and_blended_vertex(self):
        for length in (1., 1000.):
            with self.subTest(length=length):
                rig, pose, declaration = closed_fixture(length)
                before = evaluate_fk(rig.skeleton, pose)
                vertices = self.assert_skin(rig, pose, before)
                targets = match_fk_to_ik(rig.skeleton, pose, declaration)
                saved = deepcopy(pose)
                result = solve_ik(rig.skeleton, pose, declaration, **targets)
                self.assertEqual(pose, saved)
                after = self.assert_native_report(rig, result, declaration,
                                                 targets['target'], targets['orientation'])
                self.assertTrue(result.converged)
                for name in before:
                    np.testing.assert_allclose(after[name], before[name], rtol=0, atol=1e-12)
                np.testing.assert_allclose(self.assert_skin(rig, result.pose, after),
                                           vertices, rtol=0, atol=1e-12)

    def test_changed_pole_still_applies_endpoint_orientation_and_endpoint_limit(self):
        goal = [math.sin(.3), 0., 0., math.cos(.3)]
        for limited in (False, True):
            with self.subTest(limited=limited):
                rig, pose, declaration = closed_fixture()
                pole = [math.sin(1e-5), math.cos(1e-5), 0.]
                constraints = ([dict(schema_version=1, id='endpoint-limit', bone='b4',
                                     type='hinge', axis=[1, 0, 0], minimum=0., maximum=.2)]
                               if limited else [])
                result = solve_ik(rig.skeleton, pose, declaration, [0., 0., 0.],
                                  pole=pole, orientation=goal, constraints=constraints)
                after = self.assert_native_report(rig, result, declaration, np.zeros(3), goal)
                self.assert_skin(rig, result.pose, after)
                self.assertEqual(result.converged, not limited)
                if limited:
                    local = result.pose['bones']['b4']['rotation']
                    angle = 2*math.acos(float(np.clip(abs(local[3]), 0., 1.)))
                    self.assertLessEqual(angle, .2+1e-12)
                    self.assertGreater(result.orientation_residual_rad, .1)
                    self.assertTrue(any(c['constraint'] == 'endpoint-limit'
                                        for c in result.constraints_applied))

    def test_exact_match_still_enforces_joint_limit_and_reports_actual_residual(self):
        rig, pose, declaration = closed_fixture()
        targets = match_fk_to_ik(rig.skeleton, pose, declaration)
        limit = dict(schema_version=1, id='bend-limit', bone='b1', type='hinge',
                     axis=[1, 0, 0], minimum=0., maximum=.4)
        result = solve_ik(rig.skeleton, pose, declaration, **targets, constraints=[limit])
        after = self.assert_native_report(rig, result, declaration,
                                         targets['target'], targets['orientation'])
        self.assert_skin(rig, result.pose, after)
        local = result.pose['bones']['b1']['rotation']
        angle = 2*math.acos(float(np.clip(abs(local[3]), 0., 1.)))
        self.assertLessEqual(angle, .4+1e-12)
        self.assertTrue(any(c['constraint'] == 'bend-limit' for c in result.constraints_applied))
        self.assertGreater(result.residual_m, declaration['tolerance'])
        self.assertFalse(result.converged)

    def test_closed_seed_does_not_hide_unreachable_target(self):
        rig, pose, declaration = closed_fixture()
        target = np.array([0., 5., 0.])
        result = solve_ik(rig.skeleton, pose, declaration, target)
        after = self.assert_native_report(rig, result, declaration, target)
        self.assert_skin(rig, result.pose, after)
        self.assertFalse(result.reachable)
        self.assertFalse(result.converged)
        self.assertAlmostEqual(result.residual_m, 1., delta=1e-12)

    def test_closed_matching_keeps_runtime_segment_length_guard(self):
        for length in (0., 1e-8, 2e-8):
            with self.subTest(length=length):
                rig, pose, declaration = closed_fixture()
                for name in ('b1', 'b2', 'b3', 'b4'):
                    pose['bones'].setdefault(name, {})['translation'] = [0., length, 0.]
                if length > 1e-8:
                    targets = match_fk_to_ik(rig.skeleton, pose, declaration)
                    result = solve_ik(rig.skeleton, pose, declaration, **targets)
                    self.assert_native_report(rig, result, declaration,
                                              targets['target'], targets['orientation'])
                    self.assertTrue(result.converged)
                else:
                    for operation in (lambda: match_fk_to_ik(rig.skeleton, pose, declaration),
                                      lambda: solve_ik(rig.skeleton, pose, declaration, [0., 0., 0.])):
                        with self.assertRaises(RigError) as error:
                            operation()
                        self.assertEqual(error.exception.report['code'], 'RIG_IK_LENGTH')


if __name__ == '__main__':
    unittest.main()
