"""FK-to-IK matching on valid tiny chains (issue #548).

Every segment longer than the 1e-8 m validity threshold is a valid chain, but
its first-joint perpendicular can be shorter than the solver's absolute 1e-8 m
pole cutoff. Matching then substituted an arbitrary bend axis and both solvers
moved joints by about a fifth of a segment while reporting convergence.
"""
from copy import deepcopy
import math
import unittest

import numpy as np

import test_rig_fold_matching_boundaries as boundaries
from test_rig_math import skeleton_from_heads
from rig_ik import match_fk_to_ik, solve_ik
from rig_skeleton import evaluate_fk, neutral_pose

quaternion = boundaries.quaternion


def scaled_chain(segment, count=3):
    return skeleton_from_heads([dict(id=f'b{i}', role=f'joint-{i}', parent=f'b{i-1}' if i else None,
        head=[0, i*segment, 0], tail=[0, (i+1)*segment, 0], deform=True) for i in range(count)],
        source='procedural-fixture', reference='Independent scaled chain', confidence=1.)


def bent_pose(skeleton, bend, *, translation=None):
    pose = neutral_pose(skeleton)
    pose['bones']['b0'] = dict(rotation=quaternion([.3, .2, 1], .61),
                               **({'translation': translation} if translation else {}))
    pose['bones']['b1'] = dict(rotation=quaternion([1, 0, 0], bend))
    return pose


class RigTinyChainMatchingTests(unittest.TestCase):
    def assert_preserved(self, skeleton, pose, solver, segment):
        declaration = dict(boundaries.chain(len(skeleton['bones'])), solver=solver, tolerance=1e-13*segment/1e-8)
        before = evaluate_fk(skeleton, pose)
        saved = deepcopy(pose)
        targets = match_fk_to_ik(skeleton, pose, declaration)
        result = solve_ik(skeleton, pose, declaration, **targets)
        after = evaluate_fk(skeleton, result.pose)
        self.assertTrue(result.converged, result.report())
        self.assertEqual(pose, saved)
        moved = max(float(np.linalg.norm(after[name][:3, 3]-before[name][:3, 3])) for name in before)
        self.assertLessEqual(moved, 1e-9*segment, (segment, moved))
        for name in before:
            np.testing.assert_allclose(after[name][:3, :3], before[name][:3, :3], rtol=0, atol=1e-9)

    def test_tiny_valid_chains_preserve_joints_for_both_solvers(self):
        for segment in (1.5e-8, 3e-8, 1e-7):
            skeleton = scaled_chain(segment)
            for bend in (.3, 1., 2.):
                for solver in ('two-bone', 'fabrik'):
                    with self.subTest(segment=segment, bend=bend, solver=solver):
                        self.assert_preserved(skeleton, bent_pose(skeleton, bend), solver, segment)

    def test_nearly_straight_chains_match_without_rejecting_resolved_bends(self):
        # A resolved perpendicular can be shorter than unit()'s absolute 1e-10
        # floor; matching must normalise it rather than reject a valid chain.
        for segment, bend in ((1.5e-8, 1e-3), (1e-7, 1e-3), (1e-6, 1e-4)):
            skeleton = scaled_chain(segment)
            for solver in ('two-bone', 'fabrik'):
                with self.subTest(segment=segment, bend=bend, solver=solver):
                    self.assert_preserved(skeleton, bent_pose(skeleton, bend), solver, segment)
        skeleton = scaled_chain(1.)
        for bend in (1e-8, 1e-11, 1e-13):
            for solver in ('two-bone', 'fabrik'):
                with self.subTest(segment=1., bend=bend, solver=solver):
                    pose = bent_pose(skeleton, bend)
                    declaration = dict(boundaries.chain(3), solver=solver)
                    before = evaluate_fk(skeleton, pose)
                    result = solve_ik(skeleton, pose, declaration, **match_fk_to_ik(skeleton, pose, declaration))
                    after = evaluate_fk(skeleton, result.pose)
                    self.assertTrue(result.converged, result.report())
                    moved = max(float(np.linalg.norm(after[name][:3, 3]-before[name][:3, 3])) for name in before)
                    # Two-bone conditioning of a near-straight unit chain, as on main.
                    self.assertLessEqual(moved, 1e-7)

    def test_ordinary_scale_matching_poles_are_unchanged(self):
        skeleton = scaled_chain(1.)
        for bend in (.3, 1., 2.):
            pose = bent_pose(skeleton, bend)
            world = evaluate_fk(skeleton, pose)
            root, first, end = (world[name][:3, 3] for name in ('b0', 'b1', 'b2'))
            direction = (end-root)/np.linalg.norm(end-root)
            perpendicular = first-root-direction*np.dot(first-root, direction)
            expected = root+perpendicular/np.linalg.norm(perpendicular)*np.linalg.norm(end-root)
            for solver in ('two-bone', 'fabrik'):
                with self.subTest(bend=bend, solver=solver):
                    pole = match_fk_to_ik(skeleton, pose, dict(boundaries.chain(3), solver=solver))['pole']
                    np.testing.assert_allclose(pole, expected, rtol=0, atol=1e-15)
                    self.assert_preserved(skeleton, pose, solver, 1.)

    def test_straight_chain_keeps_existing_degenerate_fallback(self):
        skeleton = scaled_chain(1.)
        pose = neutral_pose(skeleton)
        pole = match_fk_to_ik(skeleton, pose, dict(boundaries.chain(3), solver='fabrik'))['pole']
        self.assertTrue(np.isfinite(pole).all())
        world = evaluate_fk(skeleton, pose)
        offset = np.asarray(pole)-world['b0'][:3, 3]
        self.assertAlmostEqual(float(np.dot(offset, [0, 1, 0])), 0., delta=1e-15)


if __name__ == '__main__':
    unittest.main()
