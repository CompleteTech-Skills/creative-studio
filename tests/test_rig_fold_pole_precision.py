"""Declared poles must steer the first segment of an already closed native chain."""
import math
import unittest

import numpy as np

from test_rig_fold_matching_boundaries import backfold_case, chain
from rig_ik import solve_ik, validate_chain
from rig_skeleton import evaluate_fk


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


if __name__ == '__main__':
    unittest.main()
