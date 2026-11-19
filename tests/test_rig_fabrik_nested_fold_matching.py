"""FABRIK pole steering at nested near-pi folds (issue #549).

Consecutive near-pi folds give interior joints a short neighbour axis that is
nearly parallel to the matching pole offset. Steering by the resulting
unresolved angle moved an already matched seed by about 0.77 m while
reporting convergence. Matching must preserve every joint and blended vertex,
and measurable pole changes must still steer interior joints.
"""
from copy import deepcopy
import math
import unittest

import numpy as np

import test_rig_fold_matching_boundaries as boundaries
from test_rig_math import chain_skeleton
from rig_ik import match_fk_to_ik, solve_ik
from rig_skeleton import evaluate_fk, neutral_pose

quaternion = boundaries.quaternion


def zigzag_pose(skeleton, gap, *, root_rotation=None, root_translation=None):
    pose = neutral_pose(skeleton)
    pose['bones']['b0'] = dict(rotation=root_rotation or quaternion([.3, .2, 1], .61),
                               **({'translation': root_translation} if root_translation else {}))
    for name in [bone['id'] for bone in skeleton['bones']][1:-1]:
        pose['bones'][name] = dict(rotation=quaternion([1, 0, 0], math.pi-gap))
    return pose


class RigFabrikNestedFoldMatchingTests(unittest.TestCase):
    assert_native_geometry = boundaries.RigFoldMatchingBoundaryTests.assert_native_geometry
    assert_report = boundaries.RigFoldMatchingBoundaryTests.assert_report

    def assert_preserved(self, skeleton, pose, atol):
        declaration = dict(boundaries.chain(len(skeleton['bones'])), solver='fabrik')
        rig = boundaries.skinned_rig(skeleton)
        before, vertices = self.assert_native_geometry(rig, pose)
        saved = deepcopy(pose)
        targets = match_fk_to_ik(skeleton, pose, declaration)
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

    def test_nested_near_pi_folds_preserve_matched_seed(self):
        for count in (4, 5):
            skeleton = chain_skeleton(count)
            for gap in (5e-9, 5e-8, 5e-7, 1e-5, 1e-3, .1):
                with self.subTest(count=count, gap=gap):
                    self.assert_preserved(skeleton, zigzag_pose(skeleton, gap), 1e-10)

    def test_nested_folds_under_rotated_and_translated_roots(self):
        skeleton = chain_skeleton(4)
        for rotation in (quaternion([1, -.4, .2], 2.3), quaternion([0, 1, 0], 1.1)):
            for translation in (None, [3., -2., 5.]):
                for gap in (5e-8, 5e-7):
                    with self.subTest(rotation=rotation, translation=translation, gap=gap):
                        pose = zigzag_pose(skeleton, gap, root_rotation=rotation, root_translation=translation)
                        self.assert_preserved(skeleton, pose, 1e-10)

    def test_measurable_pole_change_still_steers_interior_joints(self):
        skeleton = chain_skeleton(3)
        pose = neutral_pose(skeleton)
        pose['bones']['b1'] = dict(rotation=quaternion([1, 0, 0], .8))
        declaration = dict(boundaries.chain(3), solver='fabrik')
        world = evaluate_fk(skeleton, pose)
        root, elbow, end = (world[name][:3, 3] for name in ('b0', 'b1', 'b2'))
        axis = (end-root)/np.linalg.norm(end-root)
        before = elbow-root-axis*np.dot(elbow-root, axis)
        for angle in (1e-3, .5, math.pi/2, math.pi):
            with self.subTest(angle=angle):
                c, s = math.cos(angle), math.sin(angle)
                other = np.cross(axis, before/np.linalg.norm(before))
                wanted = c*before/np.linalg.norm(before)+s*other
                pole = root+axis*np.dot(elbow-root, axis)+wanted*2.
                result = solve_ik(skeleton, pose, declaration, end.tolist(), pole=pole.tolist())
                solved = evaluate_fk(skeleton, result.pose)
                new_elbow = solved['b1'][:3, 3]
                perp = new_elbow-root-axis*np.dot(new_elbow-root, axis)
                self.assertTrue(result.converged, result.report())
                self.assertGreater(float(np.dot(perp/np.linalg.norm(perp), wanted)), 1-1e-9)


if __name__ == '__main__':
    unittest.main()
