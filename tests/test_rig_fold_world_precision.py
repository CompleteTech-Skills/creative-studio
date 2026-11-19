"""A closed chain steered by its pole must rotate coherently at translated roots."""
from copy import deepcopy
import unittest

import numpy as np

from rig_ik import solve_ik, validate_chain
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from test_rig_fold_matching_boundaries import (
    angular_error, chain, explicit_lbs, quaternion, rectangle_case, skinned_rig,
)
from test_rig_math import chain_skeleton


POLE_DIRECTION = np.array([1., 2., 3.])


def square(shift, length):
    skeleton = chain_skeleton(5)
    pose = neutral_pose(skeleton)
    pose['bones']['b0'] = dict(translation=[shift, shift, 0.])
    for index, axis in enumerate(([0, 1, 0], [1, 0, 0], [0, -1, 0], [-1, 0, 0]), 1):
        pose['bones'][f'b{index}'] = dict(translation=(length * np.array(axis)).tolist())
    return skeleton, pose


def reference_root_rotation(pose, source):
    """Literal shortest rotation onto the pole composed with the seed root."""
    a = source / np.linalg.norm(source)
    b = POLE_DIRECTION / np.linalg.norm(POLE_DIRECTION)
    # All fixtures have a non-antiparallel first segment and pole.
    turn = np.r_[np.cross(a, b), 1. + np.dot(a, b)]
    turn /= np.linalg.norm(turn)
    seed = np.array(pose['bones']['b0'].get('rotation', [0., 0., 0., 1.]))
    vector = turn[3]*seed[:3] + seed[3]*turn[:3] + np.cross(turn[:3], seed[:3])
    scalar = turn[3]*seed[3] - np.dot(turn[:3], seed[:3])
    expected = deepcopy(pose)
    expected['bones']['b0']['rotation'] = [*vector, scalar]
    return expected


class FoldWorldPrecisionTests(unittest.TestCase):
    def assert_rotated_seed(self, skeleton, pose, tolerance, *, check_square_frames):
        original = evaluate_fk(skeleton, pose)
        origin = original['b0'][:3, 3]
        np.testing.assert_array_equal(original['b4'][:3, 3], origin)
        declaration = chain(5)
        declaration['tolerance'] = tolerance
        declaration['pole'] = (origin + POLE_DIRECTION).tolist()
        validate_chain(skeleton, declaration)
        reference = reference_root_rotation(pose, original['b1'][:3, 3] - origin)
        expected = evaluate_fk(skeleton, reference)
        attainable = np.linalg.norm(expected['b4'][:3, 3] - origin)
        self.assertLess(attainable, tolerance)

        result = solve_ik(skeleton, pose, declaration, origin)
        observed = evaluate_fk(skeleton, result.pose)
        measured = np.linalg.norm(observed['b4'][:3, 3] - origin)
        self.assertAlmostEqual(result.residual_m, measured, delta=1e-16)
        self.assertTrue(result.reachable)
        with self.subTest(oracle='attainable closure'):
            self.assertLessEqual(measured, tolerance, f'rigid rotation attains {attainable:g} m')
            self.assertTrue(result.converged)
        if check_square_frames:
            # The square starts exactly on the coordinate axes even in the
            # translated fixtures. Its entire rigid reorientation is observable.
            with self.subTest(oracle='joint frames'):
                for name in original:
                    np.testing.assert_allclose(observed[name][:3, :3], expected[name][:3, :3],
                                               rtol=0, atol=1e-12, err_msg=name)
            with self.subTest(oracle='blended vertices'):
                rig = skinned_rig(skeleton)
                mesh_id = rig.geometry.meshes[0].id
                actual = evaluate_deformation(rig, result.pose)[mesh_id]['positions']
                bound = max(1e-12, 8*np.spacing(np.max(np.abs(origin))))
                np.testing.assert_allclose(actual, explicit_lbs(rig, expected),
                                           rtol=0, atol=bound)
        else:
            # At the offset microscopic rectangle the world-space seed direction
            # itself is quantized. Check the internal shape independently of the
            # root steering convention used to accommodate that quantization.
            for i in range(1, 5):
                name, parent = f'b{i}', f'b{i-1}'
                with self.subTest(oracle='relative rotation', bone=name):
                    before = original[parent][:3, :3].T @ original[name][:3, :3]
                    after = observed[parent][:3, :3].T @ observed[name][:3, :3]
                    np.testing.assert_allclose(after, before, rtol=0, atol=1e-12)

    def test_translated_unit_square_keeps_attainable_closure(self):
        self.assert_rotated_seed(*square(1e8, 1.), 1e-8, check_square_frames=True)

    def test_unit_square_at_origin(self):
        self.assert_rotated_seed(*square(0., 1.), 1e-8, check_square_frames=True)

    def test_translated_microscopic_square_preserves_frames_and_blended_geometry(self):
        self.assert_rotated_seed(*square(1e6, 2**-18), 1e-8, check_square_frames=True)

    def test_microscopic_square_at_origin(self):
        self.assert_rotated_seed(*square(0., 2**-18), 1e-8, check_square_frames=True)

    def test_translated_rotated_rectangle_keeps_attainable_strict_closure(self):
        self.assert_rotated_seed(*rectangle_case(distant=True), 1e-12, check_square_frames=False)

    def test_endpoint_goal_and_limit_run_after_translated_pole_change(self):
        skeleton, pose = square(1e6, 1.)
        origin = np.array(pose['bones']['b0']['translation'])
        goal = quaternion([1, 0, 0], .9)
        declaration = chain(5)
        limit = dict(schema_version=1, id='endpoint-limit', bone='b4', type='hinge',
                     axis=[1, 0, 0], minimum=0., maximum=.2)
        for constraints in ([], [limit]):
            with self.subTest(limited=bool(constraints)):
                result = solve_ik(skeleton, pose, declaration, origin, pole=origin+POLE_DIRECTION,
                                  orientation=goal, constraints=constraints)
                world = evaluate_fk(skeleton, result.pose)
                measured = np.linalg.norm(world['b4'][:3, 3] - origin)
                self.assertAlmostEqual(result.residual_m, measured, delta=1e-16)
                self.assertLessEqual(measured, declaration['tolerance'])
                error = angular_error(world['b4'], goal)
                self.assertAlmostEqual(result.orientation_residual_rad, error, delta=5e-8)
                self.assertEqual(result.converged, not constraints)
                if constraints:
                    self.assertGreater(error, .1)
                    self.assertTrue(any(c['constraint'] == limit['id'] for c in result.constraints_applied))
                    relative = world['b3'][:3, :3].T @ world['b4'][:3, :3]
                    self.assertGreaterEqual(np.trace(relative), 1 + 2*np.cos(.2) - 1e-12)


if __name__ == '__main__':
    unittest.main()
