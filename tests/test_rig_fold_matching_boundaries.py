"""Independent native FK/IK matching boundaries for issue #528.

Synthetic finite chains exercise actual FK, FABRIK and blended vertex skinning.
No solver is mocked. Large translations use absolute comparison tolerances so
relative tolerance cannot conceal a changed rotation or joint position.
"""
from copy import deepcopy
import json
import math
import tempfile
import unittest

import numpy as np

from test_rig_math import chain_skeleton
from rig_test_helpers import fixture
from rig_contracts import validate_contract
from rig_errors import RigError
from rig_ik import match_fk_to_ik, solve_ik, validate_chain
from rig_models import Geometry, Mesh, RigAsset, SkinWeights
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation, validate_skin
from rig_validation import validate_rig


def quaternion(axis, angle):
    vector = np.asarray(axis, dtype=float)
    return [*(vector / np.linalg.norm(vector) * math.sin(angle / 2)), math.cos(angle / 2)]


def rotation(quat):
    x, y, z, w = np.asarray(quat) / np.linalg.norm(quat)
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])


def angular_error(matrix, goal):
    relative = rotation(goal).T @ matrix[:3, :3]
    return math.acos(float(np.clip((np.trace(relative)-1)/2, -1., 1.)))


def chain(count):
    return dict(schema_version=1, id='boundary', bones=[f'b{i}' for i in range(count)],
                solver='fabrik', pole=[0., 0., 2.], tolerance=1e-8, iterations=128)


def skinned_rig(skeleton):
    """An off-axis triangle for every joint, each with two genuine influences."""
    names = [bone['id'] for bone in skeleton['bones']]
    positions = np.array([[x, i+y, z] for i in range(len(names))
                          for x, y, z in ((.13, .02, .07), (-.17, .09, .04), (.06, -.08, -.11))])
    indices = np.array([[i, (i+1) % len(names)] for i in range(len(names)) for _ in range(3)])
    weights = np.tile([.65, .35], (len(positions), 1))
    rest = evaluate_fk(skeleton)
    mesh = Mesh('boundary-mesh', positions, np.arange(len(positions)).reshape(-1, 3))
    skin = SkinWeights(mesh.id, names, indices, weights,
                       np.array([np.linalg.inv(rest[name]) for name in names]))
    return RigAsset({'skeleton': skeleton}, Geometry([mesh], [dict(name='fixture')]), {mesh.id: skin})


def explicit_lbs(rig, world):
    """Literal weighted world*inverse-bind products, independent of skinning API."""
    mesh = rig.geometry.meshes[0]
    skin = rig.weights[mesh.id]
    vertices = np.column_stack((mesh.positions, np.ones(len(mesh.positions))))
    expected = np.zeros((len(vertices), 3))
    for row, vertex in enumerate(vertices):
        for index, weight in zip(skin.indices[row], skin.weights[row]):
            matrix = world[skin.joints[index]] @ skin.inverse_bind[index]
            expected[row] += weight * (matrix @ vertex)[:3]
    return expected


def rectangle_case(*, distant=False):
    skeleton = chain_skeleton(5)
    pose = neutral_pose(skeleton)
    if distant:
        pose['bones']['b0'] = dict(translation=[1e6, 1e6, 0.],
                                   rotation=quaternion([0, 0, 1], .103))
        for i, length in enumerate((1e-6, 1.33e-6, 1e-6, 1.33e-6), 1):
            pose['bones'][f'b{i}'] = dict(translation=[0., length, 0.])
    for name in ('b1', 'b2', 'b3'):
        pose['bones'].setdefault(name, {})['rotation'] = quaternion([0, 0, 1], math.pi/2)
    return skeleton, pose


def backfold_case():
    skeleton = chain_skeleton(5)
    pose = neutral_pose(skeleton)
    for name in ('b1', 'b2', 'b3'):
        pose['bones'][name] = dict(rotation=quaternion([1, 0, 0], math.pi))
    return skeleton, pose


class RigFoldMatchingBoundaryTests(unittest.TestCase):
    def assert_native_geometry(self, rig, pose):
        world = evaluate_fk(rig.skeleton, pose)
        for matrix in world.values():
            self.assertTrue(np.isfinite(matrix).all())
            np.testing.assert_allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), rtol=0, atol=1e-12)
        mesh = rig.geometry.meshes[0]
        report = validate_skin(rig.weights[mesh.id], mesh, rig.skeleton)
        self.assertLess(report['weight_sum_max_error'], 1e-12)
        actual = evaluate_deformation(rig, pose)[mesh.id]['positions']
        self.assertTrue(np.isfinite(actual).all())
        np.testing.assert_allclose(actual, explicit_lbs(rig, world), rtol=0, atol=5e-10)
        return world, actual

    def assert_report(self, result, declaration, target, world, orientation=None):
        endpoint = world[declaration['bones'][-1]]
        measured = float(np.linalg.norm(endpoint[:3, 3]-target))
        self.assertTrue(math.isfinite(result.residual_m))
        self.assertAlmostEqual(result.residual_m, measured, delta=1e-12)
        expected = result.reachable and measured <= declaration['tolerance']
        if orientation is not None:
            error = angular_error(endpoint, orientation)
            self.assertAlmostEqual(result.orientation_residual_rad, error, delta=5e-8)
            expected = expected and error <= 1e-5
        self.assertEqual(result.converged, expected, result.report())
        self.assertLessEqual(result.iterations, declaration['iterations'])
        json.dumps(result.report(), allow_nan=False)

    def assert_matching_preserves(self, skeleton, pose):
        declaration = chain(len(skeleton['bones']))
        validate_chain(skeleton, declaration)
        rig = skinned_rig(skeleton)
        before, vertices = self.assert_native_geometry(rig, pose)
        saved = deepcopy(pose)
        targets = match_fk_to_ik(skeleton, pose, declaration)
        self.assertTrue(np.isfinite(targets['pole']).all())
        result = solve_ik(skeleton, pose, declaration, **targets)
        after, solved_vertices = self.assert_native_geometry(rig, result.pose)
        self.assert_report(result, declaration, targets['target'], after, targets['orientation'])
        self.assertTrue(result.converged, result.report())
        self.assertEqual(pose, saved)
        # Both oracles run even if one detects a mismatch.
        with self.subTest(oracle='all-joint-world-matrices'):
            names = list(before)
            np.testing.assert_allclose(np.array([after[name] for name in names]),
                                       np.array([before[name] for name in names]),
                                       rtol=0, atol=1e-12, err_msg=str(names))
        with self.subTest(oracle='actual-blended-vertices'):
            np.testing.assert_allclose(solved_vertices, vertices, rtol=0, atol=1e-12)

    def test_ordinary_nonclosed_matching_preserves_every_joint_and_blended_vertex(self):
        skeleton = chain_skeleton(4)
        pose = neutral_pose(skeleton)
        pose['bones']['b1'] = dict(rotation=quaternion([0, 0, 1], math.radians(135)))
        pose['bones']['b2'] = dict(rotation=quaternion([0, 0, 1], math.radians(120)))
        before = evaluate_fk(skeleton, pose)
        self.assertAlmostEqual(np.linalg.norm(before['b3'][:3, 3]-before['b0'][:3, 3]),
                               .261052384440103, places=12)
        self.assert_matching_preserves(skeleton, pose)

    def test_ordinary_matching_passing_control(self):
        skeleton = chain_skeleton(4)
        pose = neutral_pose(skeleton)
        pose['bones']['b1'] = dict(rotation=quaternion([0, 0, 1], .4))
        self.assert_matching_preserves(skeleton, pose)

    def test_distant_microscopic_closed_rectangle_matching_preserves_all_fk_and_skin(self):
        skeleton, pose = rectangle_case(distant=True)
        world = evaluate_fk(skeleton, pose)
        np.testing.assert_array_equal(world['b4'][:3, 3], world['b0'][:3, 3])
        lengths = [np.linalg.norm(world[f'b{i+1}'][:3, 3]-world[f'b{i}'][:3, 3]) for i in range(4)]
        self.assertTrue(all(length > 1e-8 for length in lengths), lengths)
        self.assert_matching_preserves(skeleton, pose)

    def test_distant_rectangle_has_valid_native_structure_and_skin(self):
        skeleton, pose = rectangle_case(distant=True)
        declaration = chain(5)
        validate_chain(skeleton, declaration)
        rig = skinned_rig(skeleton)
        self.assert_native_geometry(rig, pose)
        with tempfile.TemporaryDirectory(prefix='fold-boundary-') as temporary:
            native = fixture(temporary, animated=False)
            native.manifest['skeleton'] = skeleton
            native.manifest['controllers']['ik'] = [declaration]
            native.geometry, native.weights = rig.geometry, rig.weights
            report = validate_rig(native)
            validate_contract(report, 'RigValidationReport')
            self.assertTrue(report['ok'], report)
            checks = {item['name']: item['status'] for item in report['checks']}
            self.assertEqual(checks['structure'], 'passed')
            self.assertEqual(checks['skin'], 'passed')
            self.assertEqual(checks['bind-pose'], 'passed')
            json.dumps(report, allow_nan=False)

    def test_unit_closed_rectangle_matching_passing_control(self):
        self.assert_matching_preserves(*rectangle_case())

    def test_backfolded_chain_matching_passing_control(self):
        self.assert_matching_preserves(*backfold_case())

    def assert_changed_backfold_pole(self, pole):
        skeleton, pose = backfold_case()
        rig = skinned_rig(skeleton)
        before, vertices = self.assert_native_geometry(rig, pose)
        target = before['b0'][:3, 3].copy()
        np.testing.assert_allclose(before['b4'][:3, 3], target, rtol=0, atol=1e-12)
        declaration = chain(5)
        result = solve_ik(skeleton, pose, declaration, target, pole=pole)
        after, solved_vertices = self.assert_native_geometry(rig, result.pose)
        self.assert_report(result, declaration, target, after)
        self.assertTrue(result.converged, result.report())
        np.testing.assert_allclose(after['b4'][:3, 3], target, rtol=0, atol=declaration['tolerance'])
        for i in range(4):
            self.assertAlmostEqual(np.linalg.norm(after[f'b{i+1}'][:3, 3]-after[f'b{i}'][:3, 3]), 1., delta=1e-12)
        with self.subTest(oracle='first-segment-follows-explicit-pole'):
            direction = (np.asarray(pole)-target) / np.linalg.norm(np.asarray(pole)-target)
            np.testing.assert_allclose(after['b1'][:3, 3]-target, direction, rtol=0, atol=1e-8)
        with self.subTest(oracle='actual-blended-vertices-move'):
            self.assertGreater(np.max(np.linalg.norm(solved_vertices-vertices, axis=1)), .1)

    def test_backfolded_chain_changed_positive_z_pole_controls_first_segment(self):
        self.assert_changed_backfold_pole([0., 0., 4.])

    def test_backfolded_chain_changed_negative_y_pole_controls_first_segment(self):
        self.assert_changed_backfold_pole([0., -4., 0.])

    def test_two_bone_explicit_poles_passing_control(self):
        skeleton = chain_skeleton(3)
        pose = neutral_pose(skeleton)
        pose['bones']['b1'] = dict(rotation=quaternion([1, 0, 0], math.pi))
        declaration = chain(3)
        declaration['solver'] = 'two-bone'
        rig = skinned_rig(skeleton)
        for pole in ([0., 0., 4.], [0., -4., 0.]):
            with self.subTest(pole=pole):
                result = solve_ik(skeleton, pose, declaration, [0., 0., 0.], pole=pole)
                after, _ = self.assert_native_geometry(rig, result.pose)
                self.assert_report(result, declaration, [0., 0., 0.], after)
                self.assertTrue(result.converged, result.report())
                np.testing.assert_allclose(after['b1'][:3, 3], np.asarray(pole)/4., rtol=0, atol=1e-12)

    def test_runtime_segment_guard_rejects_boundary_and_accepts_measurable_control(self):
        skeleton = chain_skeleton(3)
        for length in (0., 1e-8, 2e-8):
            with self.subTest(length=length):
                pose = neutral_pose(skeleton)
                pose['bones']['b1'] = dict(translation=[0., length, 0.], rotation=quaternion([1, 0, 0], math.pi))
                pose['bones']['b2'] = dict(translation=[0., length, 0.])
                declaration = chain(3)
                if length > 1e-8:
                    self.assert_matching_preserves(skeleton, pose)
                else:
                    for operation in (lambda: match_fk_to_ik(skeleton, pose, declaration),
                                      lambda: solve_ik(skeleton, pose, declaration, [0., 0., 0.])):
                        with self.assertRaises(RigError) as error:
                            operation()
                        self.assertEqual(error.exception.report['code'], 'RIG_IK_LENGTH')

    def test_matched_backfold_enforces_hard_limit_in_native_pass(self):
        skeleton, pose = backfold_case()
        declaration = chain(5)
        targets = match_fk_to_ik(skeleton, pose, declaration)
        limit = dict(schema_version=1, id='bend-limit', bone='b1', type='hinge',
                     axis=[1, 0, 0], minimum=0., maximum=.4)
        result = solve_ik(skeleton, pose, declaration, **targets, constraints=[limit])
        after, _ = self.assert_native_geometry(skinned_rig(skeleton), result.pose)
        angle = 2*math.acos(float(np.clip(abs(result.pose['bones']['b1']['rotation'][3]), 0, 1)))
        self.assertLessEqual(angle, .4+1e-12)
        self.assertTrue(any(item['constraint'] == 'bend-limit' for item in result.constraints_applied))
        self.assert_report(result, declaration, targets['target'], after, targets['orientation'])

    def test_matched_backfold_endpoint_orientation_runs_native_pass_and_limit(self):
        skeleton, pose = backfold_case()
        declaration = chain(5)
        targets = match_fk_to_ik(skeleton, pose, declaration)
        targets['orientation'] = quaternion([1, 0, 0], .6)
        for limited in (False, True):
            with self.subTest(limited=limited):
                constraints = ([dict(schema_version=1, id='endpoint-limit', bone='b4', type='hinge',
                                     axis=[1, 0, 0], minimum=0., maximum=.2)] if limited else [])
                result = solve_ik(skeleton, pose, declaration, **targets, constraints=constraints)
                after, _ = self.assert_native_geometry(skinned_rig(skeleton), result.pose)
                self.assert_report(result, declaration, targets['target'], after, targets['orientation'])
                self.assertEqual(result.converged, not limited, result.report())
                if limited:
                    self.assertGreater(result.orientation_residual_rad, .1)
                    angle = 2*math.acos(float(np.clip(abs(result.pose['bones']['b4']['rotation'][3]), 0, 1)))
                    self.assertLessEqual(angle, .2+1e-12)
                    self.assertTrue(any(item['constraint'] == 'endpoint-limit' for item in result.constraints_applied))


if __name__ == '__main__':
    unittest.main()
