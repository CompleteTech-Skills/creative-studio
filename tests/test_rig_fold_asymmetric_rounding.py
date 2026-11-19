"""A rounded X coordinate must not hide an attainable turn about scaled Y."""
from copy import deepcopy
import math
import unittest

import numpy as np

from rig_ik import solve_ik
from rig_skeleton import evaluate_fk
from test_rig_fold_review_boundaries import closed


class AsymmetricPoleRounding(unittest.TestCase):
    def test_pole_from_rigid_fk_remains_attainable_after_x_translation(self):
        for solver in ('two-bone', 'fabrik'):
            for shift in (0., 1000., 1e6, 1e8):
                with self.subTest(solver=solver, shift=shift):
                    origin = np.array([shift, 0., 0.])
                    skeleton, pose, chain = closed(solver, parent_scale=[1., 2., 1.],
                                                   first=[1., 1., 0.], origin=origin)
                    before = evaluate_fk(skeleton, pose)
                    reference = deepcopy(pose)
                    reference['bones']['b1']['rotation'] = [0., math.sin(.175), 0., math.cos(.175)]
                    witness = evaluate_fk(skeleton, reference)
                    turn = witness['b1'][:3, :3] @ np.linalg.inv(before['b1'][:3, :3])
                    np.testing.assert_allclose(turn.T @ turn, np.eye(3), rtol=0, atol=1e-14)
                    self.assertAlmostEqual(np.linalg.det(turn), 1., delta=1e-14)
                    endpoint = chain['bones'][-1]
                    self.assertLessEqual(np.linalg.norm(witness[endpoint][:3, 3] - origin), 1e-14)
                    pole = witness['b2'][:3, 3]
                    result = solve_ik(skeleton, pose, chain, origin, pole=pole)
                    self.assertTrue(result.converged)
                    actual = evaluate_fk(skeleton, result.pose)
                    self.assertAlmostEqual(result.residual_m,
                                           np.linalg.norm(actual[endpoint][:3, 3] - origin), delta=1e-15)
                    # X spacing depends on translation. Y and Z retain full precision.
                    self.assertLessEqual(abs(actual['b2'][0, 3] - pole[0]), max(2 * math.ulp(shift), 1e-13))
                    np.testing.assert_allclose(actual['b2'][1:3, 3], pole[1:3], rtol=0, atol=1e-13)
