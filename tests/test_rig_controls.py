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


def orientation_skeleton(count):
    specs = [dict(id=f'b{i}', parent=f'b{i-1}' if i else None,
                  head=[0, i, 0], tail=[0, i+1, 0]) for i in range(count)]
    specs.extend([
        dict(id='follower', parent='b0', head=[2, 0, 0], tail=[2, 1, 0]),
        dict(id='transitive', parent='b0', head=[4, 0, 0], tail=[4, 1, 0]),
        dict(id='tip', parent=f'b{count-1}', head=[0, count, 0], tail=[0, count+1, 0]),
        dict(id='tip-child', parent='tip', head=[0, count+1, 0], tail=[0, count+2, 0]),
        dict(id='aim-child', parent=f'b{count-1}', head=[1, count, 0], tail=[1, count+1, 0]),
        dict(id='tracker', parent='b0', head=[6, 0, 0], tail=[6, 1, 0]),
        dict(id='look', parent='b0', head=[8, 0, 0], tail=[8, 1, 0]),
        dict(id='anchor', parent='b0', head=[10, 0, 0], tail=[10, 1, 0]),
    ])
    return skeleton_from_heads([dict(s, role=s['id']) for s in specs])


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

    def test_endpoint_orientation_precedes_direct_and_transitive_followers(self):
        # Full extension forces one pass even for FABRIK, so stale followers
        # cannot accidentally catch up on a later iteration.
        for count, solver in ((3, 'two-bone'), (4, 'fabrik')):
            skeleton = orientation_skeleton(count)
            endpoint = f'b{count-1}'
            original = rotate_bone(skeleton, neutral_pose(skeleton), endpoint,
                                   axis_angle([1, 0, 0], 1.2))
            for inherited in (False, True):
                for requested, maximum, final_angle in ((.2, .2, .2), (1.2, .2, .2),
                                                        (.7, None, .7)):
                    with self.subTest(solver=solver, inherited=inherited,
                                      requested=requested, maximum=maximum):
                        chain = ik_chain(count, solver)
                        goal = axis_angle([1, 0, 0], requested).tolist()
                        constraints = [
                            dict(schema_version=1, id='second', bone='transitive',
                                 type='copy-rotation', target='follower'),
                            dict(schema_version=1, id='first', bone='follower',
                                 type='copy-rotation', target=endpoint),
                        ]
                        if maximum is not None:
                            constraints.append(dict(schema_version=1, id='endpoint-limit',
                                                    bone=endpoint, type='hinge', axis=[1, 0, 0],
                                                    minimum=0, maximum=maximum))
                        kwargs = dict(constraints=constraints)
                        if inherited:chain['orientation'] = goal
                        else:kwargs['orientation'] = goal
                        result = solve_ik(skeleton, original, chain, [0, count-1, 0], **kwargs)
                        world = evaluate_fk(skeleton, result.pose)
                        expected = [[1, 0, 0],
                                    [0, math.cos(final_angle), -math.sin(final_angle)],
                                    [0, math.sin(final_angle), math.cos(final_angle)]]
                        for bone in (endpoint, 'follower', 'transitive'):
                            np.testing.assert_allclose(world[bone][:3, :3], expected, atol=1e-10)
                        self.assertEqual(result.iterations, 1)
                        self.assertTrue(result.reachable)
                        self.assertEqual(result.converged, requested == final_angle)
                        self.assertAlmostEqual(result.residual_m, 0., places=10)
                        self.assertAlmostEqual(result.orientation_residual_rad,
                                               abs(requested-final_angle), places=7)
                        self.assertAlmostEqual(result.report()['orientation_residual_rad'],
                                               abs(requested-final_angle), places=7)
            np.testing.assert_allclose(original['bones'][endpoint]['rotation'],
                                       axis_angle([1, 0, 0], 1.2), atol=1e-12)

    def test_endpoint_orientation_precedes_hierarchy_and_aim_consumers(self):
        for count, solver in ((3, 'two-bone'), (4, 'fabrik')):
            for inherited in (False, True):
                with self.subTest(solver=solver, inherited=inherited):
                    skeleton = orientation_skeleton(count)
                    endpoint = f'b{count-1}'
                    original = rotate_bone(skeleton, neutral_pose(skeleton), endpoint,
                                           axis_angle([1, 0, 0], 1.2))
                    point = np.array([2., count+2., 1.])
                    constraints = [
                        dict(schema_version=1, id='track-descendant', bone='tracker',
                             type='copy-position', target='tip-child'),
                        dict(schema_version=1, id='aim-at-descendant', bone='look',
                             type='aim', target='tip-child', axis=[0, 1, 0]),
                        dict(schema_version=1, id='aim-from-descendant', bone='aim-child',
                             type='aim', point=point.tolist(), axis=[0, 1, 0]),
                    ]
                    chain = ik_chain(count, solver)
                    goal = axis_angle([1, 0, 0], .6).tolist()
                    kwargs = dict(constraints=constraints)
                    if inherited:chain['orientation'] = goal
                    else:kwargs['orientation'] = goal
                    result = solve_ik(skeleton, original, chain, [0, count-1, 0], **kwargs)
                    self.assertTrue(result.converged, result.report())
                    world = evaluate_fk(skeleton, result.pose)
                    expected_tip = [0, count-1+2*math.cos(.6), 2*math.sin(.6)]
                    np.testing.assert_allclose(world['tip-child'][:3, 3], expected_tip, atol=1e-10)
                    np.testing.assert_allclose(world['tracker'][:3, 3], expected_tip, atol=1e-10)
                    for bone, target in (('look', np.array(expected_tip)), ('aim-child', point)):
                        direction = target-world[bone][:3, 3]
                        direction /= np.linalg.norm(direction)
                        np.testing.assert_allclose(world[bone][:3, 1], direction, atol=1e-10)

    def test_orientation_uses_constrained_parent_world_frame(self):
        for count, solver in ((3, 'two-bone'), (4, 'fabrik')):
            with self.subTest(solver=solver):
                skeleton = orientation_skeleton(count)
                original = rotate_bone(skeleton, neutral_pose(skeleton), 'anchor',
                                       axis_angle([0, 1, 0], .4))
                endpoint = f'b{count-1}'
                constraints = [
                    dict(schema_version=1, id='follower', bone='follower',
                         type='copy-rotation', target=endpoint),
                    dict(schema_version=1, id='parent', bone='b1',
                         type='copy-rotation', target='anchor'),
                ]
                result = solve_ik(skeleton, original, ik_chain(count, solver), [0, count-1, 0],
                                  constraints=constraints, orientation=axis_angle([1, 0, 0], .6))
                self.assertTrue(result.converged, result.report())
                world = evaluate_fk(skeleton, result.pose)
                expected = [[1, 0, 0], [0, math.cos(.6), -math.sin(.6)],
                            [0, math.sin(.6), math.cos(.6)]]
                np.testing.assert_allclose(world[endpoint][:3, :3], expected, atol=1e-10)
                np.testing.assert_allclose(world['follower'][:3, :3], expected, atol=1e-10)
                np.testing.assert_allclose(world['b1'][:3, :3],
                                           [[math.cos(.4), 0, math.sin(.4)], [0, 1, 0],
                                            [-math.sin(.4), 0, math.cos(.4)]], atol=1e-10)

    def test_fabrik_followers_observe_final_pose_after_multiple_passes(self):
        skeleton = orientation_skeleton(4)
        original = rotate_bone(skeleton, neutral_pose(skeleton), 'b3',
                               axis_angle([1, 0, 0], 1.2))
        constraints = [
            dict(schema_version=1, id='second', bone='transitive',
                 type='copy-rotation', target='follower'),
            dict(schema_version=1, id='first', bone='follower',
                 type='copy-rotation', target='b3'),
        ]
        target = np.array([1.2, 1.8, .2])
        goal = axis_angle([1, 0, 0], .6)
        result = solve_ik(skeleton, original, ik_chain(4, 'fabrik'), target,
                          constraints=constraints, orientation=goal)
        self.assertTrue(result.converged, result.report())
        self.assertGreater(result.iterations, 1)
        self.assertLessEqual(result.iterations, 128)
        world = evaluate_fk(skeleton, result.pose)
        expected = [[1, 0, 0], [0, math.cos(.6), -math.sin(.6)],
                    [0, math.sin(.6), math.cos(.6)]]
        for bone in ('b3', 'follower', 'transitive'):
            np.testing.assert_allclose(world[bone][:3, :3], expected, atol=1e-10)
        measured = float(np.linalg.norm(world['b3'][:3, 3]-target))
        self.assertAlmostEqual(result.residual_m, measured, places=12)
        self.assertLess(measured, 1e-5)

    def test_orientation_barrier_rejects_invalid_dependencies(self):
        skeleton = orientation_skeleton(3)
        follower = dict(schema_version=1, id='follow', bone='follower',
                        type='copy-rotation', target='b2')
        invalid = [
            [follower, dict(schema_version=1, id='cycle', bone='b2',
                            type='copy-rotation', target='follower')],
            [follower, dict(follower)],
            [dict(follower, target='missing')],
        ]
        for constraints in invalid:
            with self.subTest(constraints=constraints):
                with self.assertRaises(RigError):
                    solve_ik(skeleton, neutral_pose(skeleton), ik_chain(), [0, 2, 0],
                             constraints=constraints, orientation=axis_angle([1, 0, 0], .2))

    def test_orientation_barrier_applies_fractional_prefix_and_suffix_once(self):
        specs = [
            dict(id='b0', parent=None, head=[0, 0, 0], tail=[0, 1, 0]),
            dict(id='before', parent='b0', head=[2, 0, 0], tail=[2, 1, 0]),
            dict(id='before-target', parent='b0', head=[4, 0, 0], tail=[4, 1, 0]),
            dict(id='b1', parent='b0', head=[0, 1, 0], tail=[0, 2, 0]),
            dict(id='b2', parent='b1', head=[0, 2, 0], tail=[0, 3, 0]),
            dict(id='follower', parent='b0', head=[0, 0, 2], tail=[0, 1, 2]),
            dict(id='after', parent='b0', head=[6, 0, 0], tail=[6, 1, 0]),
            dict(id='after-target', parent='b0', head=[8, 0, 0], tail=[8, 1, 0]),
        ]
        skeleton = skeleton_from_heads([dict(s, role=s['id']) for s in specs])
        constraints = [
            dict(schema_version=1, id='before', bone='before', type='copy-position',
                 target='before-target', influence=.5),
            dict(schema_version=1, id='limit', bone='b2', type='hinge',
                 axis=[1, 0, 0], minimum=0, maximum=.2),
            dict(schema_version=1, id='follow', bone='follower', type='copy-rotation', target='b2'),
            dict(schema_version=1, id='after', bone='after', type='copy-position',
                 target='after-target', influence=.5),
        ]
        original = rotate_bone(skeleton, neutral_pose(skeleton), 'b2', axis_angle([1, 0, 0], 1.2))
        for orientation in (None, axis_angle([1, 0, 0], .2)):
            with self.subTest(orientation=orientation):
                result = solve_ik(skeleton, original, ik_chain(), [0, 2, 0],
                                  constraints=constraints, orientation=orientation)
                world = evaluate_fk(skeleton, result.pose)
                np.testing.assert_allclose(world['before'][:3, 3], [3, 0, 0], atol=1e-10)
                np.testing.assert_allclose(world['after'][:3, 3], [7, 0, 0], atol=1e-10)
                np.testing.assert_allclose(world['follower'][:3, :3], world['b2'][:3, :3], atol=1e-10)
                for name in ('before', 'after'):
                    self.assertEqual(sum(c['constraint'] == name for c in result.constraints_applied), 1)
                self.assertTrue(result.converged)
                if orientation is None:self.assertIsNone(result.orientation_residual_rad)

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
