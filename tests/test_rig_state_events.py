from copy import deepcopy
import tempfile
import unittest
import numpy as np
from rig_test_helpers import fixture
from rig_errors import RigError
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime


class RigStateEventsTests(unittest.TestCase):
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

    def test_canonical_events_are_executable_only_and_catalog_data_is_copied(self):
        from state_catalog import load_library
        from rig_states import map_states
        library = load_library()
        mapping = map_states(self.rig, recipes={'static-positive':'bend', 'idle':'missing'},
                             selected=['static-positive', 'idle'])
        self.assertEqual(set(mapping['events'].values()), {'static-positive'})
        for event in library.states['static-positive']['events']:
            self.assertEqual(mapping['events'][event], 'static-positive')
        mapping['recipes']['static-positive']['events'].append('test-only')
        self.assertNotIn('test-only', load_library().states['static-positive']['events'])
        event = library.states['static-positive']['events'][0]
        self.assertEqual(self.runtime().dispatch(event, .1)['state'], 'static-positive')

    def test_event_compiler_rejects_ambiguous_catalog_event(self):
        from types import SimpleNamespace
        from rig_state_events import compile_state_events
        records = {'first': {'events':['same'], 'aliases':['one']},
                   'second': {'events':['same'], 'aliases':['two']}}
        with self.assertRaises(RigError) as failure:
            compile_state_events(SimpleNamespace(states=records), {'first':{}, 'second':{}})
        self.assertEqual(failure.exception.report['code'], 'RIG_STATE_EVENT')

    def test_dispatch_and_recovery_roll_back_due_completion_and_budget_failure(self):
        runtime = self.runtime('notification-safe')
        runtime.recipes['idle']['legal_transitions'] = []
        before = deepcopy((runtime.state, runtime.entered, runtime.time,
                           runtime.source_pose, runtime.root_offset, runtime.transition_count))
        with self.assertRaises(RigError):
            runtime.dispatch('state:positive-static', 1.1)
        self.assertEqual((runtime.state, runtime.entered, runtime.time,
                          runtime.source_pose, runtime.transition_count),
                         (before[0], before[1], before[2], before[3], before[5]))
        np.testing.assert_array_equal(runtime.root_offset, before[4])
        runtime = self.runtime('static-positive', transition_budget=1)
        runtime.recover(.1)
        before = runtime.evaluate(.1)['pose']
        with self.assertRaises(RigError) as failure:
            runtime.recover(.2)
        self.assertEqual(failure.exception.report['code'], 'RIG_STATE_BUDGET')
        self.assertEqual((runtime.state, runtime.time, runtime.entered, runtime.transition_count),
                         ('idle', .1, .1, 1))
        np.testing.assert_allclose(self.positions(runtime.evaluate(.1)['pose']),
                                   self.positions(before), atol=1e-12)

    def test_reduced_motion_returned_pose_is_isolated_and_recovery_selects_still(self):
        from rig_skeleton import neutral_pose, rotate_bone
        from rig_math import axis_angle
        neutral = neutral_pose(self.rig.skeleton)
        signal = rotate_bone(self.rig.skeleton, neutral, 'b1', axis_angle([0,0,1], .4))
        signal['morphs']['strip'] = {'blink': .6}
        recipes = {'static-neutral':'neutral', 'static-positive':'bend', 'idle':'neutral'}
        alternatives = {'static-neutral':neutral, 'static-positive':signal, 'idle':neutral}
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes=recipes,
                                  selected=list(recipes), reduced_motion=True,
                                  reduced_motion_poses=alternatives, blend_seconds=10.)
        first = runtime.evaluate(0.)
        first['pose']['bones'].clear()
        first['pose']['morphs'].clear()
        expected = self.positions(signal)
        np.testing.assert_allclose(self.positions(runtime.evaluate(100.)['pose']), expected)
        recovered = runtime.recover(100.)
        self.assertEqual((recovered['state'], recovered['status'], recovered['pending_completion']),
                         ('idle', 'held-reduced-motion', None))
        np.testing.assert_allclose(self.positions(recovered['pose']), self.positions(neutral))
        self.assertGreater(np.max(np.abs(expected-self.positions(neutral))), .01)

    def test_reduced_motion_keeps_time_and_policy_guards_and_validates_optional_inputs(self):
        from rig_skeleton import neutral_pose
        neutral = neutral_pose(self.rig.skeleton)
        recipes = {'static-neutral':'neutral', 'static-positive':'bend'}
        alternatives = {name:deepcopy(neutral) for name in recipes}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), reduced_motion=True,
                                  reduced_motion_poses=alternatives, transition_budget=1)
        runtime.dispatch('state:positive-static', .1)
        for now in (-1., 0., float('nan'), float('inf'), True, '1'):
            with self.subTest(now=now), self.assertRaises(RigError):
                runtime.recover(now)
            self.assertEqual((runtime.state, runtime.time, runtime.transition_count),
                             ('static-positive', .1, 1))
        with self.assertRaises(RigError):
            runtime.dispatch('state:static-neutral', .2)
        with self.assertRaises(RigError):
            runtime.cancel(.2)
        self.assertEqual((runtime.state, runtime.time, runtime.transition_count),
                         ('static-positive', .1, 1))
        for alternatives in ([], 'neutral', {'static-neutral':None}):
            with self.subTest(alternatives=alternatives), self.assertRaises(RigError):
                self.runtime(reduced_motion=False, reduced_motion_poses=alternatives)


if __name__ == '__main__':unittest.main()
