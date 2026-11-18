"""Declared poles must steer the first segment of an already closed native chain."""
import math
import unittest

import numpy as np

from test_rig_fold_matching_boundaries import backfold_case, chain
from test_rig_math import chain_skeleton
from rig_ik import solve_ik, validate_chain
from rig_skeleton import evaluate_fk, neutral_pose


class FoldPolePrecisionTests(unittest.TestCase):
    def assert_pole_steers_elbow(self, angle):
        direction = np.array([math.sin(angle), math.cos(angle), 0.])
        for length, origin in ((1., [0., 0., 0.]), (1000., [2., -3., .5])):
            with self.subTest(angle=angle, length=length, origin=origin):
                skeleton, pose = backfold_case()
                pose['bones']['b0'] = dict(translation=origin)
                for joint in ('b1', 'b2', 'b3', 'b4'):
                    pose['bones'].setdefault(joint, {})['translation'] = [0., length, 0.]
                declaration = chain(5)
                declaration['pole'] = (origin + direction).tolist()
                validate_chain(skeleton, declaration)
                target = evaluate_fk(skeleton, pose)['b4'][:3, 3]
                np.testing.assert_allclose(target, origin, rtol=0, atol=1e-10)

                result = solve_ik(skeleton, pose, declaration, target)
                world = evaluate_fk(skeleton, result.pose)
                residual = np.linalg.norm(world['b4'][:3, 3] - target)
                self.assertAlmostEqual(result.residual_m, residual, delta=1e-12)
                self.assertLessEqual(residual, declaration['tolerance'])
                self.assertTrue(result.converged, result.report())
                elbow = world['b1'][:3, 3] - world['b0'][:3, 3]
                self.assertLess(np.linalg.norm(elbow - length * direction), 1e-8)

    def test_small_declared_change(self):
        self.assert_pole_steers_elbow(1e-5)

    def test_larger_declared_change(self):
        self.assert_pole_steers_elbow(2e-5)

    def test_declared_changes_either_side_of_opposite_direction(self):
        for delta in (-1e-5, 1e-5):
            self.assert_pole_steers_elbow(math.pi + delta)

    def test_exact_matching_and_opposite_directions(self):
        for angle in (0., math.pi):
            self.assert_pole_steers_elbow(angle)

    def test_exact_oblique_pole_direction_is_independent_of_distance(self):
        skeleton = chain_skeleton(5)
        pose = neutral_pose(skeleton)
        segment = np.array([3., 2., 7.])
        for index in range(1, 5):
            offset = segment if index % 2 else -segment
            pose['bones'][f'b{index}'] = dict(translation=offset.tolist())
        target = evaluate_fk(skeleton, pose)['b4'][:3, 3]
        np.testing.assert_array_equal(target, np.zeros(3))

        for sign in (1, -1):
            expected = sign * segment
            for distance_factor in (1, 3):
                with self.subTest(sign=sign, distance_factor=distance_factor):
                    declaration = chain(5)
                    declaration['pole'] = (distance_factor * expected).tolist()
                    validate_chain(skeleton, declaration)
                    result = solve_ik(skeleton, pose, declaration, target)
                    world = evaluate_fk(skeleton, result.pose)
                    residual = np.linalg.norm(world['b4'][:3, 3] - target)
                    self.assertAlmostEqual(result.residual_m, residual, delta=1e-12)
                    self.assertLessEqual(residual, declaration['tolerance'])
                    self.assertTrue(result.converged, result.report())
                    elbow = world['b1'][:3, 3] - world['b0'][:3, 3]
                    self.assertLess(np.linalg.norm(elbow - expected), 1e-8)

    def test_oblique_near_opposite_pole_keeps_its_measurable_offset(self):
        axis = np.array([3., 2., 7.])
        axis /= np.linalg.norm(axis)
        tangent = np.cross(axis, [0., 0., 1.])
        tangent /= np.linalg.norm(tangent)
        length = 1000.
        skeleton = chain_skeleton(5)
        pose = neutral_pose(skeleton)
        for index in range(1, 5):
            segment = length * axis if index % 2 else -length * axis
            pose['bones'][f'b{index}'] = dict(translation=segment.tolist())
        target = evaluate_fk(skeleton, pose)['b4'][:3, 3]
        np.testing.assert_array_equal(target, np.zeros(3))

        for delta in (1e-5, 1e-8, 1e-12):
            direction = -math.cos(delta) * axis + math.sin(delta) * tangent
            declaration = chain(5)
            declaration['pole'] = direction.tolist()
            validate_chain(skeleton, declaration)
            result = solve_ik(skeleton, pose, declaration, target)
            world = evaluate_fk(skeleton, result.pose)
            with self.subTest(delta=delta, joint='endpoint'):
                residual = np.linalg.norm(world['b4'][:3, 3] - target)
                self.assertAlmostEqual(result.residual_m, residual, delta=1e-12)
                self.assertLessEqual(residual, declaration['tolerance'])
                self.assertTrue(result.converged, result.report())
            with self.subTest(delta=delta, joint='elbow'):
                elbow = world['b1'][:3, 3] - world['b0'][:3, 3]
                # Resolve even the 1e-9 m offset from the exactly opposite elbow.
                tolerance = min(1e-8, length * math.sin(delta) / 10)
                self.assertLess(np.linalg.norm(elbow - length * direction), tolerance)


if __name__ == '__main__':
    unittest.main()
