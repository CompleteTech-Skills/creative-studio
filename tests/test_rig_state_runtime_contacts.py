"""Regression oracles solve real contacts after complete runtime placement."""
from copy import deepcopy
import tempfile
import unittest

import numpy as np

from rig_test_helpers import fixture
from rig_animation import clip_from_poses, evaluate_animation
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime
from rig_errors import RigError


class RigStateRuntimeContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name)
        self.root = self.rig.skeleton['root']

    def clip(self, name, start=0., end=.5, *, motion='embedded', contact=True,
             duration=1.):
        poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
        for pose, x in zip(poses, (start, end)):
            pose['bones'][self.root] = {'translation': [x, 0., 0.]}
        contacts = [dict(schema_version=1, id='plant', chain='arm',
                         start=0., end=duration, target=[start, 1.5, 0.], kind='foot')]
        clip = clip_from_poses(self.rig, name, [0., duration], poses, loop=True,
                               root_motion=motion, contacts=contacts if contact else [])
        self.rig.clips[name] = clip
        return clip

    def preserve_inputs(self, recipes):
        manifest, clips, original_recipes = deepcopy((self.rig.manifest,
                                                       self.rig.clips, recipes))

        def check():
            self.assertEqual(self.rig.manifest, manifest)
            self.assertEqual(self.rig.clips, clips)
            self.assertEqual(recipes, original_recipes)

        self.addCleanup(check)

    def runtime(self, recipes, initial='static-positive'):
        self.preserve_inputs(recipes)
        return RigStateRuntime(self.rig, initial, recipes=recipes,
                               selected=list(recipes), blend_seconds=0)

    def test_control_transition_policy_requires_an_explicit_supported_string(self):
        clip = self.clip('policy-control')
        for policy in (None, True, 1, [], {}, np.array(['continuous', 'project']), 'unknown'):
            with self.subTest(policy=repr(policy)):
                with self.assertRaisesRegex(RigError, 'Control transition'):
                    RigStateRuntime(self.rig, 'static-positive',
                        recipes={'static-positive': clip['id']},
                        selected=['static-positive'], control_transition=policy)

    def assert_pose_geometry(self, actual, expected):
        actual_fk = evaluate_fk(self.rig.skeleton, actual)
        expected_fk = evaluate_fk(self.rig.skeleton, expected)
        self.assertEqual(set(actual_fk), set(expected_fk))
        for bone in expected_fk:
            np.testing.assert_allclose(actual_fk[bone], expected_fk[bone],
                                       rtol=0, atol=1e-9, err_msg=bone)
        actual_meshes = evaluate_deformation(self.rig, actual)
        expected_meshes = evaluate_deformation(self.rig, expected)
        self.assertEqual(set(actual_meshes), set(expected_meshes))
        for mesh in expected_meshes:
            np.testing.assert_allclose(actual_meshes[mesh]['positions'],
                                       expected_meshes[mesh]['positions'],
                                       rtol=0, atol=1e-9, err_msg=mesh)

    def placed_clip(self, clip, offset):
        # Oracle authored in world space: place keys and contact targets before
        # native evaluation, leaving the rig's authored world-space pole fixed.
        result = deepcopy(clip)
        displacement = np.array([offset, 0., 0.])
        for track in result['tracks']:
            if track['target'] == self.root and track['path'] == 'translation':
                track['values'] = (np.array(track['values']) + displacement).tolist()
        for contact in result['contacts']:
            contact['target'] = (np.array(contact['target']) + displacement).tolist()
        return result

    def test_embedded_contact_loops_match_native_fk_and_skin(self):
        clip = self.clip('planted-travel')
        runtime = self.runtime({'static-positive': clip['id']})
        for time in (0., .25, .875, 1., 1.25, 2.25, 3.75):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)
                self.assertTrue(expected['contacts'][0]['converged'])
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected['pose'])

    def test_in_place_contact_loops_are_native_control(self):
        clip = self.clip('planted-inplace', motion='in-place')
        runtime = self.runtime({'static-positive': clip['id']})
        first = evaluate_animation(self.rig, clip, .25, loop=True)['pose']
        for time in (.25, 1.25, 2.25, 3.25):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)['pose']
                self.assert_pose_geometry(expected, first)
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected)

    def test_post_solve_translation_is_detectable_despite_correct_endpoint(self):
        clip = self.clip('negative-control')
        self.preserve_inputs({'static-positive': clip['id']})
        local = evaluate_animation(self.rig, clip, .25, loop=True)['pose']
        translated = deepcopy(local)
        translated['bones'][self.root]['translation'][0] += 1.
        expected = evaluate_animation(self.rig, clip, 2.25, loop=True)['pose']
        a, b = (evaluate_fk(self.rig.skeleton, p) for p in (translated, expected))
        np.testing.assert_allclose(a['b2'][:3, 3], b['b2'][:3, 3], rtol=0, atol=1e-9)
        self.assertGreater(np.linalg.norm(a['b1'][:3, 3]-b['b1'][:3, 3]), .05)
        old_mesh = evaluate_deformation(self.rig, translated)['strip']['positions']
        native_mesh = evaluate_deformation(self.rig, expected)['strip']['positions']
        self.assertGreater(np.max(np.abs(old_mesh-native_mesh)), .001)

    def test_displaced_recipe_entry_solves_against_fixed_world_pole(self):
        self.clip('source-travel', contact=False)
        target = self.clip('target-travel', start=4., end=4.5)
        recipes = {'static-neutral': 'source-travel',
                   'static-positive': 'target-travel'}
        runtime = self.runtime(recipes, initial='static-neutral')
        # Source root is .5 * .5 = .25 when entering the target at time .5.
        # Align target start 4 to .25: all root keys/plants shift by -3.75.
        oracle = self.placed_clip(target, -3.75)
        runtime.request('static-positive', .5)
        for elapsed in (0., .25, .875, 1.25, 2.25):
            with self.subTest(elapsed=elapsed):
                expected = evaluate_animation(self.rig, oracle, elapsed, loop=True)
                np.testing.assert_allclose(
                    expected['pose']['bones'][self.root]['translation'],
                    [.25 + .5*elapsed, 0., 0.], rtol=0, atol=1e-12)
                self.assertTrue(expected['contacts'][0]['converged'])
                self.assert_pose_geometry(runtime.evaluate(.5+elapsed)['pose'],
                                          expected['pose'])

    def test_multi_clip_alignment_includes_sequence_cycles_before_ik(self):
        first = self.clip('sequence-a', start=.5, end=1.)
        second = self.clip('sequence-b', start=4., end=4.25)
        runtime = self.runtime({'static-positive': ['sequence-a', 'sequence-b']})
        # A begins at .5 and ends at 1; B therefore shifts by -3.
        # One complete A+B sequence travels .5 + .25 = .75.
        samples = [
            (0., first, 0., 0.),
            (.25, first, .25, 0.),
            (1., second, 0., -3.),
            (1.25, second, .25, -3.),
            (2., first, 0., .75),
            (2.25, first, .25, .75),
            (3.25, second, .25, -2.25),
            (4.75, first, .75, 1.5),
        ]
        for time, clip, local_time, offset in samples:
            with self.subTest(time=time, clip=clip['id']):
                oracle = self.placed_clip(clip, offset)
                expected = evaluate_animation(self.rig, oracle, local_time, loop=False)
                self.assertTrue(expected['contacts'][0]['converged'])
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected['pose'])

    def test_world_aim_constraint_observes_loop_displacement(self):
        clip = self.clip('aim-travel', contact=False)
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='world-aim', bone='b1', type='aim',
                 axis=[0., 1., 0.], point=[-1., 3., 2.])
        ]
        runtime = self.runtime({'static-positive': clip['id']})
        for time in (.25, 1.25, 2.25, 3.75):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)
                self.assertTrue(expected['constraints_applied'])
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected['pose'])


    def test_authored_contact_cycles_survive_root_translation_limit(self):
        clip = self.clip('limited-root-travel')
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='root-slide-limit', bone=self.root,
                 type='translation', axes=[0], minimum=0., maximum=.2)
        ]
        runtime = self.runtime({'static-positive': clip['id']})
        # The authored cycle remains .5 even though the solved endpoint root
        # is clamped to .2. Contact placement uses the authored displacement.
        for time in (0., .25, .875, 1., 1.25, 2.25, 2.875, 3.25):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)
                target = [.5*int(time), 1.5, 0.]
                root = [min(.5*time, .2), 0., 0.]
                np.testing.assert_allclose(expected['contacts'][0]['target'],
                                           target, rtol=0, atol=1e-12)
                np.testing.assert_allclose(
                    expected['pose']['bones'][self.root]['translation'],
                    root, rtol=0, atol=1e-12)
                self.assertTrue(expected['contacts'][0]['converged'])
                native_fk = evaluate_fk(self.rig.skeleton, expected['pose'])
                np.testing.assert_allclose(native_fk['b2'][:3, 3], target,
                                           rtol=0, atol=1e-9)
                actual = runtime.evaluate(time)['pose']
                np.testing.assert_allclose(actual['bones'][self.root]['translation'],
                                           root, rtol=0, atol=1e-12)
                self.assert_pose_geometry(actual, expected['pose'])

    def assert_final_contact(self, result, target, *, converged=True):
        report = result['contacts'][0]
        endpoint = evaluate_fk(self.rig.skeleton, result['pose'])['b2'][:3, 3]
        residual = np.linalg.norm(endpoint-np.array(target))
        np.testing.assert_allclose(report['target'], target, rtol=0, atol=1e-12)
        self.assertAlmostEqual(report['residual_m'], residual, places=12)
        self.assertEqual(report['converged'], converged)
        if converged:
            self.assertLess(residual, 1e-6)
            self.assertTrue(report['reachable'])
        for mesh in evaluate_deformation(self.rig, result['pose']).values():
            self.assertTrue(np.isfinite(mesh['positions']).all())

    def check_transition_contact(self, duration, *, blend_seconds=None):
        self.clip('still-plant', end=0., duration=duration)
        moving = self.clip('moving-plant', end=1., duration=duration)
        recipes = {'static-neutral': 'still-plant',
                   'static-positive': 'moving-plant'}
        self.preserve_inputs(recipes)
        kwargs = {} if blend_seconds is None else {'blend_seconds': blend_seconds}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), **kwargs)
        source = runtime.evaluate(0.)['pose']
        runtime.request('static-positive', 0.)
        for fraction in (.25, .5, .75):
            time = duration*fraction
            result = runtime.evaluate(time)
            np.testing.assert_allclose(evaluate_fk(self.rig.skeleton, result['pose'])['b2'][:3, 3],
                                       [0., 1.5, 0.], rtol=0, atol=1e-6)
            self.assert_final_contact(result, [0., 1.5, 0.])
            # The blended root trajectory remains, while IK must correct the
            # limb. Merely returning the unblended native target would fail.
            self.assertAlmostEqual(result['pose']['bones'][self.root]['translation'][0],
                                   fraction*fraction)
            # The old blend drifts despite both native endpoint poses being
            # solved. Its actual skin also differs from the corrected result.
            from rig_animation import blend_poses
            native = evaluate_animation(self.rig, moving, time)['pose']
            old = blend_poses(self.rig, source, native, fraction)
            old_fk = evaluate_fk(self.rig.skeleton, old)
            self.assertGreater(np.linalg.norm(old_fk['b2'][:3, 3]-[0., 1.5, 0.]),
                               1e-3)
            old_mesh = evaluate_deformation(self.rig, old)['strip']['positions']
            final_mesh = evaluate_deformation(self.rig, result['pose'])['strip']['positions']
            self.assertGreater(np.max(np.abs(old_mesh-final_mesh)), 1e-4)

    def test_default_transition_blend_keeps_reachable_world_contact(self):
        self.check_transition_contact(.2)

    def test_nondefault_transition_blend_keeps_reachable_world_contact(self):
        self.check_transition_contact(1., blend_seconds=1.)

    def test_contact_blend_interruption_freezes_final_pose_then_uses_incoming_goals(self):
        self.clip('still-plant', end=0.)
        self.clip('moving-plant', end=1.)
        recipes = {'static-neutral': 'still-plant', 'static-positive': 'moving-plant',
                   'assistance-needed': 'still-plant'}
        self.preserve_inputs(recipes)
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), blend_seconds=1.,
                                  control_transition='project')
        runtime.request('static-positive', 0.)
        before = runtime.evaluate(.5)
        self.assert_final_contact(before, [0., 1.5, 0.])
        cancelled = runtime.cancel(.5)
        self.assert_pose_geometry(runtime.source_pose, before['pose'])
        # Native placement aligns the new root to x=.25 and therefore places
        # the incoming authored plant at x=.25. It overrides crossfade continuity
        # immediately; reporting convergence against an interpolated goal would
        # hide that the actual authored plant was not reached.
        self.assert_final_contact(cancelled, [.25, 1.5, 0.])
        halfway = runtime.evaluate(.75)
        self.assert_final_contact(halfway, [.25, 1.5, 0.])
        interrupted = runtime.request('assistance-needed', .75)
        self.assert_pose_geometry(runtime.source_pose, halfway['pose'])
        self.assert_final_contact(interrupted, [.25, 1.5, 0.])
        later = runtime.evaluate(1.)
        self.assert_final_contact(later, [.25, 1.5, 0.])

    def test_blended_world_aim_is_reapplied_to_final_geometry(self):
        self.clip('still-aim', end=0., contact=False)
        self.clip('moving-aim', end=1., contact=False)
        target = np.array([-1., 3., 2.])
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='world-aim', bone='b1', type='aim',
                 axis=[0., 1., 0.], point=target.tolist())]
        recipes = {'static-neutral': 'still-aim', 'static-positive': 'moving-aim'}
        self.preserve_inputs(recipes)
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), blend_seconds=1.)
        runtime.request('static-positive', 0.)
        first = evaluate_deformation(self.rig, runtime.evaluate(0.)['pose'])['strip']['positions']
        for time in (.25, .5, .75):
            result = runtime.evaluate(time)
            world = evaluate_fk(self.rig.skeleton, result['pose'])['b1']
            direction = target-world[:3, 3]
            axis = world[:3, 1]
            np.testing.assert_allclose(axis/np.linalg.norm(axis),
                                       direction/np.linalg.norm(direction), rtol=0, atol=1e-9)
            self.assertTrue(result['constraints_applied'])
            mesh = evaluate_deformation(self.rig, result['pose'])['strip']['positions']
            self.assertGreater(np.max(np.abs(mesh-first)), .001)

    def test_blended_contact_orientation_uses_the_active_authored_interval(self):
        self.clip('still-plant', end=0.)
        moving = self.clip('oriented-plant', end=.5)
        moving['contacts'][0]['end'] = .5
        orientation = [0., 0., np.sin(.3), np.cos(.3)]
        moving['contacts'].append(dict(schema_version=1, id='plant', chain='arm',
                                       start=.5, end=1., target=[0., 1.5, 0.],
                                       kind='foot', orientation=orientation))
        recipes = {'static-neutral': 'still-plant', 'static-positive': 'oriented-plant'}
        self.preserve_inputs(recipes)
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), blend_seconds=1.)
        runtime.request('static-positive', 0.)
        self.assertIsNone(runtime.evaluate(.25)['contacts'][0]['orientation_residual_rad'])
        result = runtime.evaluate(.75)
        self.assert_final_contact(result, [0., 1.5, 0.])
        self.assertLess(result['contacts'][0]['orientation_residual_rad'], 1e-5)
        endpoint = evaluate_fk(self.rig.skeleton, result['pose'])['b2']
        expected_rotation = [[np.cos(.6), -np.sin(.6), 0.],
                             [np.sin(.6), np.cos(.6), 0.], [0., 0., 1.]]
        np.testing.assert_allclose(endpoint[:3, :3], expected_rotation, rtol=0, atol=1e-9)

    def test_unreachable_contact_reports_actual_failure_and_geometry(self):
        clip = self.clip('unreachable-plant', end=0.)
        clip['contacts'][0]['target'] = [0., 10., 0.]
        runtime = self.runtime({'static-positive': clip['id']})
        result = runtime.evaluate(.5)
        self.assertEqual(result['status'], 'controls-unsatisfied')
        self.assert_final_contact(result, [0., 10., 0.], converged=False)
        self.assertFalse(result['contacts'][0]['reachable'])
        self.assertAlmostEqual(result['contacts'][0]['residual_m'], 8.)
        expected = evaluate_animation(self.rig, clip, .5)
        self.assert_pose_geometry(result['pose'], expected['pose'])

    def test_multiple_contacts_report_residuals_on_final_pose(self):
        self.rig.manifest['controllers']['ik'].append({
            **deepcopy(self.rig.manifest['controllers']['ik'][0]), 'id': 'other'})
        clip = self.clip('conflicting-chains', end=0.)
        clip['contacts'].append(dict(schema_version=1, id='second-plant', chain='other',
                                     start=0., end=1., target=[.5, 1.5, 0.], kind='foot'))
        runtime = self.runtime({'static-positive': clip['id']})
        result = runtime.evaluate(.5)
        self.assertEqual(result['status'], 'controls-unsatisfied')
        self.assert_final_contact(result, [0., 1.5, 0.], converged=False)
        self.assertAlmostEqual(result['contacts'][0]['residual_m'], .5)
        self.assertTrue(result['contacts'][1]['converged'])

    def check_same_clip_fractional_control(self, constraint, *, contact=False, rotation=0.):
        from rig_math import axis_angle
        poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
        for pose in poses:
            pose['bones']['b1'] = {'rotation': axis_angle([0., 0., 1.], rotation).tolist()}
        contacts = ([dict(schema_version=1, id='plant', chain='arm', start=0., end=1.,
                          target=[0., 1.5, 0.], kind='foot')] if contact else [])
        clip = clip_from_poses(self.rig, 'partial-constant', [0., 1.], poses,
                               loop=True, contacts=contacts)
        self.rig.clips[clip['id']] = clip
        self.rig.manifest['controllers']['constraints'] = [constraint]
        recipes = {state: clip['id'] for state in
                   ('static-neutral', 'static-positive', 'assistance-needed')}
        self.preserve_inputs(recipes)
        expected = evaluate_animation(self.rig, clip, 0.)
        raw = evaluate_animation(self.rig, clip, 0., contacts=False, constraints=False)
        controlled_mesh = evaluate_deformation(self.rig, expected['pose'])['strip']['positions']
        raw_mesh = evaluate_deformation(self.rig, raw['pose'])['strip']['positions']
        self.assertGreater(np.max(np.abs(controlled_mesh-raw_mesh)), .001)
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes))

        def check(result):
            self.assert_pose_geometry(result['pose'], expected['pose'])
            self.assertEqual(len(result['contacts']), len(expected['contacts']))
            for actual, native in zip(result['contacts'], expected['contacts']):
                self.assertAlmostEqual(actual['residual_m'], native['residual_m'], places=12)
                self.assertEqual(actual['converged'], native['converged'])

        check(runtime.evaluate(0.))
        check(runtime.request('static-positive', 0.))
        # Identical authored input must keep exactly the native influence at
        # entry, throughout the default .2s blend, and on both sides of its end.
        for time in (.05, .1, .199999, .2, .200001):
            with self.subTest(time=time):
                check(runtime.evaluate(time))
        check(runtime.request('assistance-needed', .25))
        before_cancel = runtime.evaluate(.3)
        check(before_cancel)
        check(runtime.cancel(.3))
        self.assert_pose_geometry(runtime.source_pose, before_cancel['pose'])
        for time in (.35, .499999, .5, .500001):
            with self.subTest(cancel_time=time):
                check(runtime.evaluate(time))

    def test_same_clip_half_aim_does_not_replay_fractional_constraint(self):
        self.check_same_clip_fractional_control(
            dict(schema_version=1, id='partial-aim', bone='b1', type='aim',
                 axis=[0., 1., 0.], point=[-1., 3., 2.], influence=.5))

    def test_same_clip_half_copy_position_does_not_replay_fractional_constraint(self):
        self.check_same_clip_fractional_control(
            dict(schema_version=1, id='partial-copy', bone='b1', type='copy-position',
                 target='b0', influence=.5))

    def test_same_clip_half_copy_rotation_does_not_replay_fractional_constraint(self):
        self.check_same_clip_fractional_control(
            dict(schema_version=1, id='partial-copy', bone='b1', type='copy-rotation',
                 target='b0', influence=.5), rotation=.8)

    def test_same_clip_half_aim_and_contact_match_native_control_passes(self):
        self.check_same_clip_fractional_control(
            dict(schema_version=1, id='partial-aim', bone='b1', type='aim',
                 axis=[0., 1., 0.], point=[-1., 3., 2.], influence=.5), contact=True)

    def test_interrupted_fractional_fade_preserves_authored_input_snapshot(self):
        from rig_math import axis_angle
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='half-copy', bone='b1', type='copy-rotation',
                 target='b0', influence=.5)]
        recipes = {'static-neutral': 'neutral', 'static-positive': 'bend',
                   'assistance-needed': 'neutral'}
        self.preserve_inputs(recipes)
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes))

        def check_angle(result, angle):
            expected = neutral_pose(self.rig.skeleton)
            expected['bones']['b1'] = {'rotation': axis_angle([0., 0., 1.], angle).tolist()}
            self.assert_pose_geometry(result['pose'], expected)

        runtime.request('static-positive', 0.)
        # bend(.1)=.16 rad, crossfade weight=.5 -> .08 raw, half-copy -> .04.
        before = runtime.evaluate(.1)
        check_angle(before, .04)
        entered = runtime.request('assistance-needed', .1)
        check_angle(entered, .04)
        self.assert_pose_geometry(runtime.source_pose, before['pose'])
        # Frozen raw .08 fades a quarter toward neutral, then half-copy -> .03.
        halfway = runtime.evaluate(.15)
        check_angle(halfway, .03)
        check_angle(runtime.cancel(.15), .03)
        self.assert_pose_geometry(runtime.source_pose, halfway['pose'])
        # The native quaternion owner switches to normalized linear blending
        # for this small .06rad raw rotation; compute that literal quaternion
        # before the half-copy, rather than assuming ideal angular SLERP.
        final_angle = np.arctan2(.75*np.sin(.03), .75*np.cos(.03)+.25)
        check_angle(runtime.evaluate(.2), final_angle)
        check_angle(runtime.evaluate(.350001), 0.)

    def test_outgoing_contact_release_fades_from_actual_source_geometry(self):
        from rig_animation import blend_poses
        self.clip('outgoing-plant', end=0.)
        target = self.clip('released-neutral', end=0., contact=False)
        recipes = {'static-neutral': 'outgoing-plant', 'static-positive': 'released-neutral'}
        self.preserve_inputs(recipes)
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes))
        source = runtime.evaluate(0.)['pose']
        entered = runtime.request('static-positive', 0.)
        self.assert_pose_geometry(entered['pose'], source)
        self.assertEqual(entered['contacts'], [])
        self.assertEqual(entered['control_stage'], 'contact-release-blend')
        native_target = evaluate_animation(self.rig, target, .1)['pose']
        for bone in self.rig.skeleton['bones']:
            native_target['bones'].setdefault(bone['id'], {}).update(deepcopy(bone['rest']))
        midway = runtime.evaluate(.1)
        expected = blend_poses(self.rig, source, native_target, .5)
        self.assert_pose_geometry(midway['pose'], expected)
        self.assertEqual(midway['contacts'], [])
        self.assert_pose_geometry(runtime.evaluate(.2)['pose'], native_target)

    def test_contact_release_preserves_hinge_limit_across_quaternion_wrap(self):
        from rig_math import axis_angle, signed_twist
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='release-hinge', bone='b1', type='hinge',
                 axis=[1., 0., 0.], minimum=-2.8, maximum=2.8)]
        source_clip = self.clip('limited-plant', end=0.)
        source_clip['contacts'][0]['target'] = [0., .5, 0.]
        poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
        for pose in poses:
            pose['bones']['b1'] = {'rotation': axis_angle([1., 0., 0.], 2.6).tolist()}
        target_clip = clip_from_poses(self.rig, 'limited-release', [0., 1.], poses, loop=True)
        self.rig.clips[target_clip['id']] = target_clip
        recipes = {'static-neutral': source_clip['id'], 'static-positive': target_clip['id']}
        self.preserve_inputs(recipes)
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes))
        source = runtime.evaluate(0.)['pose']
        target = evaluate_animation(self.rig, target_clip, 0.)['pose']
        angle = lambda pose: signed_twist(pose['bones']['b1']['rotation'], [1., 0., 0.])
        # Both endpoints are legal, but shortest-quaternion blending goes
        # through pi and violates this anatomical hinge's hard maximum.
        self.assertLess(angle(source), -2.6)
        self.assertAlmostEqual(angle(target), 2.6)
        runtime.request('static-positive', 0.)
        changed = set()
        for time in np.linspace(0., .199999, 21):
            result = runtime.evaluate(float(time))
            self.assertGreaterEqual(angle(result['pose']), -2.8-1e-12)
            self.assertLessEqual(angle(result['pose']), 2.8+1e-12)
            changed.update(item['constraint'] for item in result['constraints_applied'])
            self.assertTrue(all(np.isfinite(mesh['positions']).all()
                                for mesh in evaluate_deformation(self.rig, result['pose']).values()))
        self.assertIn('release-hinge', changed)
        self.assert_pose_geometry(runtime.evaluate(.2)['pose'], target)

    def test_fractional_control_contact_release_and_interruptions_fade_actual_source(self):
        from rig_animation import blend_poses
        self.clip('partial-outgoing-plant', end=0.)
        target = self.clip('partial-released-neutral', end=0., contact=False)
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='partial-aim', bone='b1', type='aim',
                 axis=[0., 1., 0.], point=[-1., 3., 2.], influence=.5)]
        recipes = {'static-positive': 'partial-outgoing-plant',
                   'assistance-needed': target['id'], 'static-neutral': target['id']}
        self.preserve_inputs(recipes)
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes=recipes,
                                  selected=list(recipes))
        source = runtime.evaluate(0.)['pose']
        once_controlled_target = evaluate_animation(self.rig, target, 0.)['pose']

        def check(result, expected):
            self.assert_pose_geometry(result['pose'], expected)
            self.assertEqual(result['contacts'], [])
            self.assertEqual(result['constraints_applied'], [])
            self.assertEqual(result['control_stage'], 'contact-release-blend')

        check(runtime.request('assistance-needed', 0.), source)
        midway = runtime.evaluate(.1)
        check(midway, blend_poses(self.rig, source, once_controlled_target, .5))
        # Cancel into a contact-free neutral during the release. The new fade
        # starts from the actual release output, not its unconstrained input.
        check(runtime.cancel(.1), midway['pose'])
        self.assert_pose_geometry(runtime.source_pose, midway['pose'])
        quarter = runtime.evaluate(.15)
        check(quarter, blend_poses(self.rig, midway['pose'], once_controlled_target, .25))
        # An ordinary request can interrupt the release again without replaying
        # the half-influence aim or dropping the outgoing geometric correction.
        check(runtime.request('assistance-needed', .15), quarter['pose'])
        self.assert_pose_geometry(runtime.source_pose, quarter['pose'])
        check(runtime.evaluate(.25),
              blend_poses(self.rig, quarter['pose'], once_controlled_target, .5))
        final = runtime.evaluate(.350001)
        self.assertEqual(final['control_stage'], 'native')
        self.assert_pose_geometry(final['pose'], once_controlled_target)



if __name__ == '__main__':
    unittest.main()
