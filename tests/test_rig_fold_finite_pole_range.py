"""Scaling an explicitly attainable pole by a finite positive factor preserves its ray."""
import unittest
import numpy as np
from rig_ik import solve_ik
from rig_skeleton import evaluate_fk
from test_rig_fold_review_boundaries import closed


class FinitePoleRange(unittest.TestCase):
    def test_quarter_turn_to_y_is_independent_of_pole_distance(self):
        for solver in ('two-bone', 'fabrik'):
            skeleton, pose, chain = closed(solver, parent_scale=[1., 1., 1.], first=[1., 0., 0.])
            for pole_distance in (1., 1e153, 1e155):
                with self.subTest(solver=solver, pole_distance=pole_distance):
                    result = solve_ik(skeleton, pose, chain, [0., 0., 0.], pole=[0., pole_distance, 0.])
                    self.assertTrue(result.converged)
                    world = evaluate_fk(skeleton, result.pose)
                    np.testing.assert_allclose(world['b2'][:3, 3], [0., 1., 0.], atol=1e-14, rtol=0)
                    self.assertLessEqual(result.residual_m, chain['tolerance'])
