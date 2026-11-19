"""Public pose/IK results must sample authored lids after their last bone solve."""
from copy import deepcopy
import math
import unittest

import numpy as np

from rig_constraints import apply_constraints
from rig_facial import gaze_at, record_constrained_gaze_rotations, refresh_gaze_eyelids
from rig_ik import solve_ik
from rig_math import axis_angle
from rig_operations import pose_operation, solve_operation
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone
from rig_skinning import evaluate_deformation
from test_rig_specializations import eyelid_fixture


def final_pose_fixture():
    rig = eyelid_fixture()
    rig.manifest['controllers']['constraints'] = [dict(
        schema_version=1, id='z-look-level', type='aim', bone='eye.left',
        axis=[0, 0, 1], point=[-.1, 1, 2], influence=.5)]
    rig.manifest['controllers']['ik'] = [dict(
        schema_version=1, id='chain', bones=['root', 'head', 'eye.right'],
        solver='two-bone', pole=[0, 1, 2], iterations=8, tolerance=.001)]
    return rig


def gaze_target(pitch):
    return [-.1, 1 + math.tan(pitch) * 2, 2]


class FinalGazeEyelidTests(unittest.TestCase):
    def assert_final_response(self, rig, pose):
        # This fixture has identity rest axes. Read final FK independently of
        # the facial sampler, including parent motion introduced by IK.
        world = evaluate_fk(rig.skeleton, pose)
        direction = np.linalg.solve(world['head'][:3, :3],
                                    world['eye.left'][:3, :3] @ [0, 0, 1])
        pitch = math.atan2(direction[1], math.hypot(direction[0], direction[2]))
        endpoint, limit = (.8, .5) if pitch >= 0 else (-.2, .4)
        expected = .25 + (endpoint - .25) * min(abs(pitch) / limit, 1.)
        self.assertAlmostEqual(pose['morphs']['lid.left']['lift'], expected, places=11)
        return pitch

    def test_pose_samples_final_constraint_pass_for_up_and_down(self):
        for pitch in (.6, -.6):
            with self.subTest(pitch=pitch):
                rig = final_pose_fixture()
                actual = pose_operation(rig, {'gaze': gaze_target(pitch)})
                self.assertAlmostEqual(self.assert_final_response(rig, actual), pitch / 4, places=11)

    def test_solve_samples_final_ik_pose_and_report(self):
        rig = final_pose_fixture()
        parameters = dict(gaze=gaze_target(.6), chain='chain', target=[.1, 1., 0.])
        before = deepcopy(parameters)
        posed = pose_operation(rig, {'gaze': parameters['gaze']})
        solved = solve_operation(rig, parameters)
        pose_pitch = self.assert_final_response(rig, posed)
        solve_pitch = self.assert_final_response(rig, solved['pose'])
        self.assertGreater(abs(solve_pitch - pose_pitch), .01)
        self.assertEqual(solved['report']['pose'], solved['pose'])
        self.assertEqual(parameters, before)

    def test_parent_rotation_does_not_become_lid_pitch(self):
        rig = final_pose_fixture()
        rig.manifest['controllers']['constraints'] = []
        pose = rotate_bone(rig.skeleton, neutral_pose(rig.skeleton),
                           'root', axis_angle([1, 0, 0], -.4))
        parent = evaluate_fk(rig.skeleton, pose)['root']
        target = (parent @ [*gaze_target(.2), 1.])[:3].tolist()
        actual = pose_operation(rig, {'pose': pose, 'gaze': target})
        self.assertAlmostEqual(self.assert_final_response(rig, actual), .2, places=11)

    def test_final_refresh_preserves_exact_unowned_morphs_and_input(self):
        rig = final_pose_fixture()
        rig.geometry.by_id()['lid.left'].morphs['spare'] = deepcopy(
            rig.geometry.by_id()['lid.left'].morphs['lift'])
        rig.manifest['facial']['expressions'] = {'detail': dict(
            schema_version=1, morphs={'lid.left::spare': .3, 'lid.right::lift': .4},
            bones={}, incompatible=[])}
        initial = neutral_pose(rig.skeleton)
        initial['morphs'] = {'lid.left': {'lift': -.1}, 'lid.right': {'lift': .63}}
        before = deepcopy(initial)
        for operation, extra in ((pose_operation, {}),
                                 (solve_operation, dict(chain='chain', target=[.1, 1., 0.]))):
            with self.subTest(operation=operation.__name__):
                actual = operation(rig, dict(pose=initial, gaze=gaze_target(.6),
                                            expressions={'detail': .5}, **extra))
                if operation is solve_operation:
                    actual = actual['pose']
                self.assert_final_response(rig, actual)
                self.assertEqual(actual['morphs']['lid.left']['spare'], .15)
                self.assertEqual(actual['morphs']['lid.right']['lift'], .2)
                self.assertEqual(initial, before)

    def test_neutral_target_restores_authored_nonzero_default(self):
        rig = final_pose_fixture()
        active = pose_operation(rig, {'gaze': gaze_target(.6)})
        actual = pose_operation(rig, {'pose': active, 'gaze': [-.1, 1., 2.]})
        self.assertAlmostEqual(self.assert_final_response(rig, actual), 0., places=11)
        self.assertAlmostEqual(actual['morphs']['lid.left']['lift'], .25, places=11)

    def test_release_then_public_operations_do_not_reactivate_owned_lids(self):
        rig = final_pose_fixture()
        active = pose_operation(rig, {'gaze': gaze_target(.6)})
        active['morphs']['lid.right'] = {'lift': .7}
        released = gaze_at(rig, active, None)
        before = deepcopy(released)
        for operation, extra in ((pose_operation, {}),
                                 (solve_operation, dict(chain='chain', target=[.1, 1., 0.]))):
            with self.subTest(operation=operation.__name__):
                actual = operation(rig, dict(pose=released, **extra))
                if operation is solve_operation:
                    actual = actual['pose']
                self.assertNotIn('lid.left', actual['morphs'])
                self.assertEqual(actual['morphs']['lid.right']['lift'], .7)
                # Removing the override must leave deformation at the authored
                # default even when the solver changes the parent transform.
                explicit_default = deepcopy(actual)
                explicit_default['morphs']['lid.left'] = {'lift': .25}
                np.testing.assert_allclose(
                    evaluate_deformation(rig, actual)['lid.left']['positions'],
                    evaluate_deformation(rig, explicit_default)['lid.left']['positions'])
        self.assertEqual(released, before)

    def test_head_follow_release_survives_an_intervening_constraint_pass(self):
        rig = final_pose_fixture()
        authored = rotate_bone(rig.skeleton, neutral_pose(rig.skeleton),
                               'head', axis_angle([0, 1, 0], -.26))
        expected = pose_operation(rig, {'pose': authored})
        initially_active = pose_operation(rig, {'pose': authored, 'gaze': gaze_target(.6), 'head_follow': .35})
        self.assertNotEqual(initially_active['bones']['head']['rotation'], authored['bones']['head']['rotation'])
        active = pose_operation(rig, {'pose': initially_active})
        self.assertNotEqual(active['bones']['eye.left']['rotation'],
                            initially_active['bones']['eye.left']['rotation'])
        actual = pose_operation(rig, {'pose': active, 'gaze': None})
        self.assertEqual(actual, expected)
        for mesh, geometry in evaluate_deformation(rig, expected).items():
            np.testing.assert_allclose(evaluate_deformation(rig, actual)[mesh]['positions'], geometry['positions'])

    def test_gaze_release_after_ik_solve_survives_its_constraint_pass(self):
        # solve_ik reruns the manifest constraints after pose_operation records
        # gaze ownership; the release must still recognise the constrained eye.
        rig = final_pose_fixture()
        authored = rotate_bone(rig.skeleton, neutral_pose(rig.skeleton),
                               'head', axis_angle([0, 1, 0], -.26))
        solve = dict(pose=authored, chain='chain', target=[.1, 1., 0.])
        ungazed = solve_operation(rig, solve)['pose']
        gazed = solve_operation(rig, dict(solve, gaze=gaze_target(.6)))['pose']
        self.assertNotEqual(gazed['bones']['eye.left'], ungazed['bones']['eye.left'])
        # Release restores the authored eye under the solved head, then applies
        # the (non-idempotent, influence .5) aim constraint once.
        reset = deepcopy(ungazed)
        reset['bones']['eye.left'].pop('rotation', None)
        expected = pose_operation(rig, {'pose': reset})
        actual = pose_operation(rig, {'pose': gazed, 'gaze': None})
        self.assertEqual(actual, expected)
        for mesh, geometry in evaluate_deformation(rig, expected).items():
            np.testing.assert_allclose(evaluate_deformation(rig, actual)[mesh]['positions'], geometry['positions'])

    def test_no_gaze_preserves_authored_pose_overrides(self):
        rig = final_pose_fixture()
        initial = neutral_pose(rig.skeleton)
        initial['morphs'] = {'lid.left': {'lift': -.17}, 'lid.right': {'lift': .73}}
        for operation, extra in ((pose_operation, {}),
                                 (solve_operation, dict(chain='chain', target=[.1, 1., 0.]))):
            with self.subTest(operation=operation.__name__):
                actual = operation(rig, dict(pose=initial, **extra))
                if operation is solve_operation:
                    actual = actual['pose']
                self.assertEqual(actual['morphs'], initial['morphs'])

    def test_no_binding_keeps_legacy_pose_and_solve_results(self):
        for bindings in (None, []):
            with self.subTest(bindings=bindings):
                rig = final_pose_fixture()
                if bindings is None:
                    rig.manifest['facial'].pop('eyelids')
                else:
                    rig.manifest['facial']['eyelids'] = bindings
                initial = neutral_pose(rig.skeleton)
                initial['morphs'] = {'lid.left': {'lift': -.17}, 'lid.right': {'lift': .73}}
                parameters = dict(pose=initial, gaze=gaze_target(.6),
                                  chain='chain', target=[.1, 1., 0.])
                expected, _ = apply_constraints(rig.skeleton,
                    gaze_at(rig, initial, parameters['gaze']),
                    rig.manifest['controllers']['constraints'])
                for name, change in expected['gaze_overrides']['rotations'].items():
                    self.assertIsNone(change[0])
                    change[1] = expected['bones'][name]['rotation']
                self.assertEqual(pose_operation(rig, dict(pose=initial, gaze=parameters['gaze'])), expected)
                expected_solve = solve_ik(rig.skeleton, expected,
                    rig.manifest['controllers']['ik'][0], parameters['target'],
                    constraints=rig.manifest['controllers']['constraints'])
                # The solve's own constraint pass is carried into gaze ownership.
                record_constrained_gaze_rotations(expected, expected_solve.pose)
                self.assertEqual(solve_operation(rig, parameters),
                                 dict(pose=expected_solve.pose, report=expected_solve.report()))
                self.assertEqual(expected['morphs'], initial['morphs'])
                self.assertIs(refresh_gaze_eyelids(rig, initial), initial)


if __name__ == '__main__':
    unittest.main()
