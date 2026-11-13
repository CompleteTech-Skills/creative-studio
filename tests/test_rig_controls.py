import math
import unittest
import numpy as np
from test_rig_math import chain_skeleton
from rig_constraints import apply_constraints, constraint_order
from rig_errors import RigError
from rig_ik import match_fk_to_ik, solve_ik
from rig_math import axis_angle, qangle
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone, skeleton_from_heads


def ik_chain(count=3, solver='two-bone'):
    return dict(schema_version=1, id='limb', bones=[f'b{i}' for i in range(count)], solver=solver,
                pole=[0, 0, 2], tolerance=1e-5, iterations=128)


class RigControlTests(unittest.TestCase):
    def test_analytic_reachable_target_and_geometric_pole(self):
        skeleton = chain_skeleton()
        solved = solve_ik(skeleton, neutral_pose(skeleton), ik_chain(), [1, 1, 0])
        self.assertTrue(solved.converged, solved.report())
        world = evaluate_fk(skeleton, solved.pose)
        np.testing.assert_allclose(world['b2'][:3, 3], [1, 1, 0], atol=1e-8)
        np.testing.assert_allclose(world['b1'][:3, 3], [.5, .5, math.sqrt(.5)], atol=1e-8)

    def test_unreachable_target_has_real_residual(self):
        skeleton = chain_skeleton()
        result = solve_ik(skeleton, neutral_pose(skeleton), ik_chain(), [3, 0, 0])
        self.assertFalse(result.reachable);self.assertFalse(result.converged)
        self.assertAlmostEqual(result.residual_m, 1., places=8)

    def test_full_extension_and_fold_are_finite(self):
        skeleton = chain_skeleton()
        for target in ([0, 2, 0], [0, 0, 0], [0, 1e-9, 0]):
            result = solve_ik(skeleton, neutral_pose(skeleton), ik_chain(), target)
            self.assertTrue(result.converged, result.report())

    def test_fabrik_multisegment_chain(self):
        skeleton = chain_skeleton(4)
        result = solve_ik(skeleton, neutral_pose(skeleton), ik_chain(4, 'fabrik'), [1.2, 1.8, .2])
        self.assertTrue(result.converged, result.report())
        self.assertLess(result.residual_m, 1e-5)

    def test_fk_ik_matching_preserves_endpoint(self):
        skeleton = chain_skeleton();pose = neutral_pose(skeleton)
        pose = rotate_bone(skeleton, pose, 'b1', axis_angle([1, 0, 0], .5))
        targets = match_fk_to_ik(skeleton, pose, ik_chain())
        result = solve_ik(skeleton, pose, ik_chain(), targets['target'], pole=targets['pole'], orientation=targets['orientation'])
        self.assertTrue(result.converged)

    def test_requested_endpoint_orientation_cannot_bypass_joint_limits(self):
        skeleton = chain_skeleton()
        original = neutral_pose(skeleton)
        limit = dict(schema_version=1, id='endpoint-hinge', bone='b2', type='hinge',
                     axis=[1, 0, 0], minimum=0, maximum=.2)
        for inherited in (False, True):
            with self.subTest(inherited=inherited):
                chain = ik_chain()
                kwargs = dict(constraints=[limit])
                orientation = axis_angle([1, 0, 0], 1.2).tolist()
                if inherited:
                    chain['orientation'] = orientation
                else:
                    kwargs['orientation'] = orientation
                result = solve_ik(skeleton, original, chain, [0, 2, 0], **kwargs)
                self.assertAlmostEqual(qangle(result.pose['bones']['b2']['rotation']), .2)
                np.testing.assert_allclose(evaluate_fk(skeleton, result.pose)['b2'][:3, 3],
                                           [0, 2, 0], atol=1e-8)
                self.assertFalse(result.converged)
                self.assertAlmostEqual(result.orientation_residual_rad, 1.)
                self.assertAlmostEqual(result.report()['orientation_residual_rad'], 1.)
                self.assertTrue(any(c['constraint'] == 'endpoint-hinge'
                                    for c in result.constraints_applied))
        self.assertEqual(original, neutral_pose(skeleton))

    def test_orientation_residual_distinguishes_satisfied_and_unrequested_goals(self):
        skeleton=chain_skeleton();pose=neutral_pose(skeleton)
        oriented=solve_ik(skeleton,pose,ik_chain(),[0,2,0],
                          orientation=axis_angle([0,1,0],.7))
        self.assertTrue(oriented.converged)
        self.assertLess(oriented.orientation_residual_rad,1e-5)
        positional=solve_ik(skeleton,pose,ik_chain(),[0,2,0])
        self.assertTrue(positional.converged)
        self.assertIsNone(positional.orientation_residual_rad)

    def test_orientation_goal_does_not_replay_unrelated_fractional_constraint(self):
        specs=[dict(id='b0',parent=None,head=[0,0,0],tail=[0,1,0]),
               dict(id='b1',parent='b0',head=[0,1,0],tail=[0,2,0]),
               dict(id='b2',parent='b1',head=[0,2,0],tail=[0,3,0]),
               dict(id='copy',parent='b0',head=[2,0,0],tail=[2,1,0]),
               dict(id='target',parent='b0',head=[4,0,0],tail=[4,1,0])]
        skeleton=skeleton_from_heads([dict(s,role=s['id']) for s in specs])
        constraint=dict(schema_version=1,id='fractional',bone='copy',type='copy-position',
                        target='target',influence=.5)
        for orientation in (None,[0,0,0,1]):
            with self.subTest(orientation=orientation):
                result=solve_ik(skeleton,neutral_pose(skeleton),ik_chain(),[0,2,0],
                                constraints=[constraint],orientation=orientation)
                np.testing.assert_allclose(evaluate_fk(skeleton,result.pose)['copy'][:3,3],[3,0,0],atol=1e-10)
                self.assertEqual(sum(c['constraint']=='fractional' for c in result.constraints_applied),1)

    def test_hinge_and_prismatic_limits_act_on_actual_pose(self):
        skeleton = chain_skeleton();pose = rotate_bone(skeleton, neutral_pose(skeleton), 'b1', axis_angle([1, 0, 0], 1.2))
        pose['bones']['b1']['translation'] = [0, 3, 0]
        constraints = [dict(schema_version=1, id='hinge', bone='b1', type='hinge', axis=[1, 0, 0], minimum=0, maximum=.4),
                       dict(schema_version=1, id='slide', bone='b1', type='translation', axes=[1], minimum=0, maximum=.2)]
        fixed, changes = apply_constraints(skeleton, pose, constraints)
        self.assertAlmostEqual(qangle(fixed['bones']['b1']['rotation']), .4)
        self.assertAlmostEqual(fixed['bones']['b1']['translation'][1], 1.2)
        self.assertEqual(len(changes), 2)

    def test_constraint_cycles_and_invalid_chains_fail(self):
        skeleton = chain_skeleton()
        with self.assertRaises(RigError):constraint_order(skeleton, [dict(schema_version=1, id='copy', bone='b0', type='copy-position', target='b2')])
        chain = ik_chain();chain['bones'] = ['b0', 'b2', 'b1']
        with self.assertRaises(RigError):solve_ik(skeleton, neutral_pose(skeleton), chain, [1, 1, 0])
