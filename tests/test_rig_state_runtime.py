from copy import deepcopy
import tempfile
import unittest
import numpy as np
from rig_test_helpers import fixture
from rig_errors import RigError
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime


class RigStateRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name)
        self.recipes = {'static-neutral':'neutral', 'static-positive':'bend',
                        'assistance-needed':'bend', 'idle':'neutral', 'notification-safe':'bend'}

    def runtime(self, initial='static-neutral', **kwargs):
        return RigStateRuntime(self.rig, initial, recipes=self.recipes,
                               selected=list(self.recipes), **kwargs)

    def positions(self, pose):
        return evaluate_deformation(self.rig, pose)['strip']['positions']

    def test_interrupt_and_cancel_preserve_actual_deformation_at_boundary(self):
        runtime = self.runtime(blend_seconds=.5)
        runtime.request('static-positive', 0.)
        before = runtime.evaluate(.25)['pose']
        self.assertGreater(np.max(np.abs(self.positions(before)-self.positions(self.runtime().evaluate(0)['pose']))), .01)
        cancelled = runtime.cancel(.25)['pose']
        np.testing.assert_allclose(self.positions(before), self.positions(cancelled), atol=1e-12)
        halfway = runtime.evaluate(.4)['pose']
        interrupted = runtime.request('assistance-needed', .4)['pose']
        np.testing.assert_allclose(self.positions(halfway), self.positions(interrupted), atol=1e-12)
        moved = runtime.evaluate(.65)['pose']
        self.assertGreater(np.max(np.abs(self.positions(moved)-self.positions(interrupted))), .01)

    def test_completion_enters_selected_target_at_exact_boundary(self):
        runtime = self.runtime('notification-safe', blend_seconds=0)
        result = runtime.evaluate(1.25)
        self.assertEqual(result['state'], 'idle')
        self.assertAlmostEqual(result['elapsed'], .25)
        self.assertEqual(result['transition_count'], 1)

    def test_unavailable_completion_reports_and_holds(self):
        runtime = RigStateRuntime(self.rig, 'notification-safe', recipes={'notification-safe':'bend'}, selected=['notification-safe'])
        result = runtime.evaluate(2)
        self.assertEqual(result['status'], 'completion-unavailable')
        self.assertEqual(result['pending_completion'], 'idle')
        np.testing.assert_allclose(self.positions(result['pose']), self.positions(runtime.evaluate(3)['pose']))

    def test_rejected_transitions_and_coalescing_preserve_state(self):
        runtime = self.runtime('static-positive')
        with self.assertRaises(RigError):runtime.request('static-neutral', .1)
        self.assertEqual(runtime.state, 'static-positive')
        self.assertEqual(runtime.entered, 0)
        runtime.request('static-positive', .2)
        self.assertEqual(runtime.entered, 0)
        with self.assertRaises(RigError):runtime.request('notification-safe', .2)
        with self.assertRaises(RigError):runtime.request('missing', .2)
        self.assertEqual(runtime.transition_count, 0)

    def test_monotonic_finite_time_and_budget(self):
        runtime = self.runtime(transition_budget=1)
        runtime.request('static-positive', .1)
        for now in [-1, .05, float('nan'), float('inf'), True, '1']:
            with self.assertRaises(RigError):runtime.evaluate(now)
        with self.assertRaises(RigError):runtime.cancel(.2)
        self.assertEqual(runtime.state, 'static-positive')
        for amount in [-1, float('nan'), 11, True, '1']:
            with self.assertRaises(RigError):self.runtime(blend_seconds=amount)

    def test_modified_clip_invalidates_runtime(self):
        runtime = self.runtime()
        self.rig.clips['bend']['fps'] = 10
        with self.assertRaises(RigError) as failure:runtime.evaluate(0)
        self.assertEqual(failure.exception.report['code'], 'RIG_STATE_STALE')

    def test_cancel_requires_selected_executable_neutral(self):
        runtime = RigStateRuntime(self.rig, 'notification-safe', recipes={'notification-safe':'bend'}, selected=['notification-safe'])
        with self.assertRaises(RigError):runtime.cancel(.1)
        self.assertEqual(runtime.state, 'notification-safe')
        self.assertEqual(runtime.time, 0)

    def test_multiple_clip_recipe_advances_to_next_clip(self):
        recipes = deepcopy(self.recipes)
        recipes['static-positive'] = ['bend', 'neutral']
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes=recipes,
                                  selected=list(recipes), blend_seconds=0)
        result = runtime.evaluate(1.25)
        expected = self.runtime().evaluate(0)['pose']
        np.testing.assert_allclose(self.positions(result['pose']), self.positions(expected))

    def test_root_motion_stitches_different_authored_origins_and_loop_cycles(self):
        from rig_animation import clip_from_poses
        from rig_skeleton import neutral_pose
        root = self.rig.skeleton['root']
        for name, begin, end in [('move-a', 10., 11.), ('move-b', 20., 22.)]:
            poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
            for pose, x in zip(poses, [begin, end]):
                pose['bones'][root] = {'translation': [x, 0., 0.]}
            self.rig.clips[name] = clip_from_poses(self.rig, name, [0., 1.], poses, loop=True)
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes={'static-positive':['move-a','move-b']},
                                  selected=['static-positive'], blend_seconds=0)
        self.assertAlmostEqual(runtime.evaluate(1.25)['pose']['bones'][root]['translation'][0], 11.5)
        self.assertAlmostEqual(runtime.evaluate(2.25)['pose']['bones'][root]['translation'][0], 13.25)

    def test_in_place_cycle_matches_animation_evaluator_and_mixed_recipes(self):
        from rig_animation import clip_from_poses, evaluate_animation
        from rig_skeleton import neutral_pose
        root = self.rig.skeleton['root']
        poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
        poses[1]['bones'][root] = {'translation': [1., 0., 0.]}
        self.rig.clips['inplace'] = clip_from_poses(self.rig, 'inplace', [0., 1.], poses,
                                                  loop=True, root_motion='in-place')
        self.rig.clips['embedded'] = clip_from_poses(self.rig, 'embedded', [0., 1.], poses,
                                                   loop=True, root_motion='embedded')
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes={'static-positive':'inplace'},
                                  selected=['static-positive'], blend_seconds=0)
        expected = evaluate_animation(self.rig, 'inplace', 1.25, loop=True)['pose']
        np.testing.assert_allclose(self.positions(runtime.evaluate(1.25)['pose']), self.positions(expected))
        mixed = RigStateRuntime(self.rig, 'static-positive',
                                recipes={'static-positive':['inplace','embedded']},
                                selected=['static-positive'], blend_seconds=0)
        self.assertAlmostEqual(mixed.evaluate(1.25)['pose']['bones'][root]['translation'][0], .25)
        self.assertAlmostEqual(mixed.evaluate(2.25)['pose']['bones'][root]['translation'][0], 1.25)

    def test_sparse_target_fades_old_morphs_to_authored_default(self):
        from rig_animation import clip_from_poses
        from rig_skeleton import neutral_pose
        poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
        for pose in poses:
            pose['morphs']['strip'] = {'blink': 1.}
        self.rig.clips['blink'] = clip_from_poses(self.rig, 'blink', [0., 1.], poses, loop=True)
        recipes = {'static-neutral': 'neutral', 'static-positive': 'blink'}
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes=recipes,
                                  selected=list(recipes), blend_seconds=.5)
        initial = runtime.evaluate(0)['pose']
        runtime.cancel(0)
        halfway = runtime.evaluate(.25)['pose']
        self.assertAlmostEqual(halfway['morphs']['strip']['blink'], .5)
        neutral = self.runtime().evaluate(0)['pose']
        np.testing.assert_allclose(self.positions(halfway),
                                   (self.positions(initial)+self.positions(neutral))*.5)
        final = runtime.evaluate(.5)['pose']
        np.testing.assert_allclose(self.positions(final), self.positions(neutral))

    def test_completion_budget_failure_restores_all_runtime_state(self):
        runtime = self.runtime(transition_budget=1)
        runtime.recipes['static-neutral']['completion'] = 'static-positive'
        runtime.recipes['static-positive']['completion'] = 'static-neutral'
        with self.assertRaises(RigError) as failure:
            runtime.evaluate(3.)
        self.assertEqual(failure.exception.report['code'], 'RIG_STATE_BUDGET')
        self.assertEqual((runtime.state, runtime.time, runtime.entered, runtime.transition_count),
                         ('static-neutral', 0., 0., 0))
        self.assertIsNone(runtime.source_pose)

    def test_request_checks_policy_after_due_completion_and_rejects_atomically(self):
        runtime = self.runtime('notification-safe')
        # At this time the active state is idle, so a request follows idle's
        # edges instead of the already-finished notification's edge set.
        runtime.recipes['notification-safe']['legal_transitions'] = []
        runtime.recipes['idle']['legal_transitions'] = ['static-positive']
        result = runtime.request('static-positive', 1.1)
        self.assertEqual(result['state'], 'static-positive')
        self.assertEqual(result['transition_count'], 2)
        other = self.runtime('notification-safe')
        other.recipes['idle']['legal_transitions'] = []
        with self.assertRaises(RigError):other.request('static-positive', 1.1)
        self.assertEqual((other.state, other.time, other.transition_count), ('notification-safe', 0., 0))

    def test_compiled_events_preserve_catalog_and_dispatch_aliases_through_policy(self):
        from state_catalog import load_library
        runtime = self.runtime()
        self.assertEqual(runtime.recipes['static-positive']['events'],
                         load_library().states['static-positive']['events'])
        self.assertEqual(runtime.mapping['events']['state:positive-static'], 'static-positive')
        result = runtime.dispatch('state:positive-static', .1)
        self.assertEqual(result['state'], 'static-positive')
        before = (runtime.state, runtime.time, runtime.transition_count)
        for event in ('state:static-neutral', 'state:missing', '', None, {}):
            with self.subTest(event=event), self.assertRaises(RigError):runtime.dispatch(event, .2)
            self.assertEqual((runtime.state, runtime.time, runtime.transition_count), before)

    def test_reduced_motion_holds_authored_geometry_and_uses_explicit_events(self):
        from rig_skeleton import neutral_pose, rotate_bone
        from rig_math import axis_angle
        recipes = {'static-neutral': 'neutral', 'notification-safe': 'bend', 'idle': 'neutral'}
        neutral = neutral_pose(self.rig.skeleton)
        signal = rotate_bone(self.rig.skeleton, neutral, 'b1', axis_angle([0,0,1], .4))
        signal['morphs'] = {'strip': {'blink': .6}}
        alternatives = {'static-neutral': neutral, 'notification-safe': signal, 'idle': neutral}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes, selected=list(recipes),
                                  reduced_motion=True, reduced_motion_poses=alternatives)
        # Alternative inputs are copied; they do not become live mutable controls.
        alternatives['notification-safe']['morphs']['strip']['blink'] = 0.
        result = runtime.dispatch('state:safe-notification', .1)
        expected = deepcopy(signal);expected['morphs']['strip']['blink'] = .6
        np.testing.assert_allclose(self.positions(result['pose']), self.positions(expected))
        self.assertEqual(result['status'], 'held-reduced-motion')
        later = runtime.evaluate(100.)
        self.assertEqual(later['state'], 'notification-safe')
        np.testing.assert_allclose(self.positions(later['pose']), self.positions(expected))
        reset = runtime.cancel(100.)
        np.testing.assert_allclose(self.positions(reset['pose']), self.positions(neutral))
        self.assertEqual(runtime.mapping['recipes']['notification-safe']['reduced_motion'],
                         __import__('state_catalog').load_library().states['notification-safe']['reduced_motion'])

    def test_reduced_motion_alternatives_require_named_current_real_poses(self):
        from rig_skeleton import neutral_pose
        alternative = neutral_pose(self.rig.skeleton)
        common = dict(recipes={'static-neutral':'neutral'}, selected=['static-neutral'], reduced_motion=True)
        for alternatives in (None, {}, {'missing': alternative}):
            with self.subTest(alternatives=alternatives), self.assertRaises(RigError):
                RigStateRuntime(self.rig, 'static-neutral', reduced_motion_poses=alternatives, **common)
        for change in (lambda p:p.update(id='unrelated'), lambda p:p.update(rig_version='9.0.0'),
                       lambda p:p['bones'].update(missing={'translation':[0,0,0]}),
                       lambda p:p['morphs'].update(strip={'missing':1.}),
                       lambda p:p['morphs'].update(strip={'blink':float('nan')})):
            pose = deepcopy(alternative);change(pose)
            with self.subTest(change=change), self.assertRaises(RigError):
                RigStateRuntime(self.rig, 'static-neutral', reduced_motion_poses={'static-neutral':pose}, **common)
        with self.assertRaises(RigError):self.runtime(reduced_motion='true')
        runtime = RigStateRuntime(self.rig, 'static-neutral', reduced_motion_poses={'static-neutral':alternative}, **common)
        self.rig.clips['neutral']['fps'] = 11
        with self.assertRaises(RigError):runtime.evaluate(0.)

    def test_explicit_recovery_uses_catalog_target_and_fails_atomically_if_missing(self):
        runtime = self.runtime('static-positive', blend_seconds=.5)
        before = runtime.evaluate(.25)['pose']
        recovered = runtime.recover(.25)
        self.assertEqual(recovered['state'], 'idle')
        np.testing.assert_allclose(self.positions(recovered['pose']), self.positions(before), atol=1e-12)
        missing = RigStateRuntime(self.rig, 'static-positive', recipes={'static-positive':'bend'},
                                  selected=['static-positive'])
        with self.assertRaises(RigError):missing.recover(.2)
        self.assertEqual((missing.state, missing.time, missing.transition_count), ('static-positive', 0., 0))


if __name__ == '__main__':unittest.main()
