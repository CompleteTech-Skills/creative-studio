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

    def steering_error(self, count, translation, bend, angle):
        """Relative error of the elbow's achieved pole rotation about the reach axis."""
        skeleton = chain_skeleton(count)
        pose = neutral_pose(skeleton)
        pose['bones']['b0'] = dict(translation=[translation]*3)
        for index in range(1, count-1):
            pose['bones'][f'b{index}'] = dict(rotation=quaternion([1, 0, 0], bend))
        world = evaluate_fk(skeleton, pose)
        root, elbow, second = (world[name][:3, 3] for name in ('b0', 'b1', 'b2'))
        end = world[f'b{count-1}'][:3, 3]
        axis = (second-root)/np.linalg.norm(second-root)
        bent = elbow-root-axis*np.dot(elbow-root, axis)
        bent /= np.linalg.norm(bent)
        other = np.cross(axis, bent)
        pole = root+axis*np.dot(elbow-root, axis)+(math.cos(angle)*bent+math.sin(angle)*other)*2.
        declaration = dict(boundaries.chain(count), solver='fabrik')
        result = solve_ik(skeleton, pose, declaration, end.tolist(), pole=pole.tolist())
        self.assertTrue(result.converged, result.report())
        solved = evaluate_fk(skeleton, result.pose)['b1'][:3, 3]
        perp = solved-root-axis*np.dot(solved-root, axis)
        achieved = math.atan2(float(np.dot(perp, other)), float(np.dot(perp, bent)))
        return abs(achieved-angle)/angle

    def test_measurable_pole_changes_still_steer_interior_joints(self):
        # Small but resolvable changes, including 1e-12 rad at exact
        # coordinates, must not be mistaken for rounding (see PR #529).
        cases = [(count, 0., bend, angle) for count in (3, 4) for bend in (1e-2, .3, .8, 2.)
                 for angle in (1e-12, 1e-8, 1e-6, 1e-4, 1e-2, .5, math.pi/2, math.pi-1e-3)]
        cases += [(count, 1e3, bend, angle) for count in (3, 4) for bend in (1e-2, .8)
                  for angle in (1e-8, 1e-6, 1e-4, 1e-2)]
        cases += [(count, 1e6, bend, angle) for count in (3, 4)
                  for bend, angle in ((1e-4, 1e-4), (1e-2, 1e-6), (1e-2, 1e-2), (.8, 1e-6), (.8, 1e-2))]
        for count, translation, bend, angle in cases:
            with self.subTest(count=count, translation=translation, bend=bend, angle=angle):
                self.assertLessEqual(self.steering_error(count, translation, bend, angle), 2e-3)

if __name__ == '__main__':
    unittest.main()
