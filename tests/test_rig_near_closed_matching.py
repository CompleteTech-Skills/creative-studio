"""FK-to-IK matching just above the folded-closure threshold.

Between the `1e-9` meter closure test and the `1e-8` meter pole-degeneracy
cutoff, a reach-distance matching pole has a perpendicular too short to steer
the bend plane; the solver substituted an arbitrary axis and moved the elbow
of a valid unit chain by about 0.59 m while reporting convergence. Matching
must preserve every evaluated joint and blended vertex for both solvers,
including under rotated and translated roots.

A two-bone solve at a reach of a few nanometers derives its direction from the
difference of nearly equal points, so its result carries rounding noise of
order machine epsilon divided by the reach (a few 1e-9 m on a unit chain).
That bounded conditioning is accepted below; a lost bend plane is not.
"""
from copy import deepcopy
import math
import unittest

import numpy as np

import test_rig_fold_matching_boundaries as boundaries
from test_rig_math import chain_skeleton
from rig_ik import MIN_MATCHING_POLE_DISTANCE, match_fk_to_ik, solve_ik
from rig_skeleton import evaluate_fk, neutral_pose

quaternion = boundaries.quaternion
# Conditioning bound for nanometer reach; 7 orders below the defect it guards.
NEAR_CLOSED_ATOL = 1e-7


def near_closed_pose(skeleton, gap, *, root_rotation=None, root_translation=None):
    """Two-segment fold whose endpoint lies about `gap` meters from the root."""
    pose = neutral_pose(skeleton)
    pose['bones']['b0'] = dict(rotation=root_rotation or quaternion([0, 0, 1], .6),
                               **({'translation': root_translation} if root_translation else {}))
    pose['bones']['b1'] = dict(rotation=quaternion([1, 0, 0], math.pi-gap))
    return pose


class RigNearClosedMatchingTests(unittest.TestCase):
    assert_native_geometry = boundaries.RigFoldMatchingBoundaryTests.assert_native_geometry
    assert_report = boundaries.RigFoldMatchingBoundaryTests.assert_report

    def assert_preserved(self, skeleton, pose, solver, atol):
        declaration = dict(boundaries.chain(len(skeleton['bones'])), solver=solver)
        rig = boundaries.skinned_rig(skeleton)
        before, vertices = self.assert_native_geometry(rig, pose)
        saved = deepcopy(pose)
        targets = match_fk_to_ik(skeleton, pose, declaration)
        self.assertTrue(np.isfinite(targets['pole']).all())
        result = solve_ik(skeleton, pose, declaration, **targets)
        after, solved = self.assert_native_geometry(rig, result.pose)
        self.assert_report(result, declaration, targets['target'], after, targets['orientation'])
        self.assertTrue(result.converged, result.report())
        self.assertEqual(pose, saved)
        names = list(before)
        np.testing.assert_allclose(np.array([after[name] for name in names]),
                                   np.array([before[name] for name in names]),
                                   rtol=0, atol=atol, err_msg=str(names))
        np.testing.assert_allclose(solved, vertices, rtol=0, atol=atol)
        return targets, before

    def test_reach_between_closure_and_pole_degeneracy_preserves_both_solvers(self):
        skeleton = chain_skeleton(3)
        for gap in (1.2e-9, 2e-9, 5e-9, 7.7e-9, 9.9e-9):
            pose = near_closed_pose(skeleton, gap)
            world = evaluate_fk(skeleton, pose)
            reach = np.linalg.norm(world['b2'][:3, 3]-world['b0'][:3, 3])
            self.assertTrue(1e-9 < reach < 1e-8, reach)
            for solver in ('two-bone', 'fabrik'):
                with self.subTest(gap=gap, solver=solver):
                    targets, _ = self.assert_preserved(skeleton, pose, solver, NEAR_CLOSED_ATOL)
                    root = world['b0'][:3, 3]
                    self.assertAlmostEqual(np.linalg.norm(np.asarray(targets['pole'])-root),
                                           MIN_MATCHING_POLE_DISTANCE, delta=1e-15)

    def test_window_under_off_axis_root_rotation_and_translation(self):
        skeleton = chain_skeleton(3)
        rotations = (quaternion([.3, .2, 1], .61), quaternion([1, -.4, .2], 2.3), quaternion([0, 1, 0], 1.1))
        for root_rotation in rotations:
            for translation in (None, [3., -2., 5.]):
                pose = near_closed_pose(skeleton, 5e-9, root_rotation=root_rotation,
                                        root_translation=translation)
                for solver in ('two-bone', 'fabrik'):
                    with self.subTest(rotation=root_rotation, translation=translation, solver=solver):
                        self.assert_preserved(skeleton, pose, solver, NEAR_CLOSED_ATOL)

    def test_ordinary_matching_pole_remains_reach_distance(self):
        skeleton = chain_skeleton(3)
        for gap, atol in ((2e-6, 1e-9), (1e-3, 1e-11), (.3, 1e-12), (1., 1e-12), (2.5, 1e-12)):
            pose = near_closed_pose(skeleton, gap)
            world = evaluate_fk(skeleton, pose)
            root, end = world['b0'][:3, 3], world['b2'][:3, 3]
            self.assertGreater(np.linalg.norm(end-root), MIN_MATCHING_POLE_DISTANCE)
            for solver in ('two-bone', 'fabrik'):
                with self.subTest(gap=gap, solver=solver):
                    targets, _ = self.assert_preserved(skeleton, pose, solver, atol)
                    self.assertAlmostEqual(np.linalg.norm(np.asarray(targets['pole'])-root),
                                           np.linalg.norm(end-root), delta=1e-12)


if __name__ == '__main__':
    unittest.main()
