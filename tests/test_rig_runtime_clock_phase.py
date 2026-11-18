"""Native loop phase, deformation admission and exact integer-clock regressions."""
from copy import deepcopy
import json
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, evaluate_track, validate_animation
from rig_errors import RigError
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture


class RigRuntimeClockPhaseTests(unittest.TestCase):
    fields = ('state', 'entered', 'time', 'source_pose', '_input_pose',
              '_source_input_pose', '_source_contact_correction',
              'root_offset', 'transition_count')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)

    def add_loop(self, name, duration, track):
        clip = deepcopy(self.rig.clips['neutral'])
        clip.update(id=name, duration=duration, tracks=[track], loop=True,
                    root_motion='in-place', events=[], contacts=[])
        validate_animation(self.rig, clip)
        self.rig.clips[name] = clip
        return clip

    def runtime(self, clip='neutral', now=0.):
        recipes = {'static-neutral': 'neutral', 'static-positive': clip}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), now=now, blend_seconds=0.)
        runtime.request('static-positive', now)
        self.assertIsNotNone(runtime.source_pose)
        self.assertIsNotNone(runtime._source_input_pose)
        return runtime

    def assert_geometry(self, actual, expected):
        for evaluator in (lambda pose: evaluate_fk(self.rig.skeleton, pose),
                          lambda pose: {mesh: data['positions'] for mesh, data in
                                        evaluate_deformation(self.rig, pose).items()}):
            actual_arrays, expected_arrays = evaluator(actual), evaluator(expected)
            self.assertEqual(set(actual_arrays), set(expected_arrays))
            for name, expected_array in expected_arrays.items():
                self.assertTrue(np.isfinite(actual_arrays[name]).all(), name)
                np.testing.assert_array_equal(actual_arrays[name], expected_array, err_msg=name)

    def assert_rollback(self, runtime, operation, code):
        before = runtime.evaluate(runtime.time)['pose']
        snapshot = {key: deepcopy(getattr(runtime, key)) for key in self.fields}
        documents = deepcopy((self.rig.manifest, self.rig.clips))
        with self.assertRaises(RigError) as error:
            operation()
        self.assertEqual(error.exception.report['code'], code)
        json.dumps(error.exception.report, allow_nan=False)
        for key, expected in snapshot.items():
            if key == 'root_offset':
                np.testing.assert_array_equal(runtime.root_offset, expected)
            else:
                self.assertEqual(getattr(runtime, key), expected, key)
        self.assertEqual((self.rig.manifest, self.rig.clips), documents)
        self.assert_geometry(runtime.evaluate(snapshot['time'])['pose'], before)

    def test_step_inside_loop_matches_native_morph_and_vertices_at_ordinary_time(self):
        clip = self.add_loop('blink-loop', .1, dict(
            schema_version=1, target='strip', path='weights', interpolation='STEP',
            times=[0., .05], values=[[0.], [1.]]))
        runtime = self.runtime(clip['id'])
        for time, blink in ((.25, 0.), (.65, 0.), (1.04, 0.), (1.05, 0.), (1.06, 1.)):
            with self.subTest(time=time):
                native = evaluate_animation(self.rig, clip, time, loop=True)
                self.assertEqual(native['time'], time % clip['duration'])
                self.assertEqual(native['pose']['morphs']['strip']['blink'], blink)
                actual = runtime.evaluate(time)
                self.assertEqual(actual['status'], 'playing')
                json.dumps(actual, allow_nan=False)
                self.assert_geometry(actual['pose'], native['pose'])
                self.assertEqual(actual['pose']['morphs']['strip']['blink'], blink)

    def test_cubic_inside_loop_rejects_same_native_geometry_at_both_clock_origins(self):
        track = dict(schema_version=1, target='b0', path='scale', interpolation='CUBICSPLINE',
                     times=[.4, .5], values=[[1.] * 3, [1.] * 3],
                     out_tangents=[[-80.] * 3, [0.] * 3], in_tangents=[[0.] * 3, [80.] * 3])
        clip = self.add_loop('scale-loop', 1.05, track)
        raw = neutral_pose(self.rig.skeleton)
        raw['bones']['b0'] = {'scale': evaluate_track(track, 1.5 % clip['duration']).tolist()}
        self.assertLess(max(raw['bones']['b0']['scale']), 0.)
        for operation in (lambda: evaluate_deformation(self.rig, raw),
                          lambda: evaluate_animation(self.rig, clip, 1.5, loop=True)):
            with self.assertRaises(RigError) as error:
                operation()
            self.assertEqual(error.exception.report['code'], 'RIG_SINGULAR_TRANSFORM')
        for origin in (0., float(2 ** 50)):
            with self.subTest(origin=origin):
                runtime = self.runtime(clip['id'], now=origin)
                before = runtime.evaluate(origin + .75)
                native = evaluate_animation(self.rig, clip, .75, loop=True)
                self.assert_geometry(before['pose'], native['pose'])
                self.assert_rollback(runtime, lambda: runtime.evaluate(origin + 1.5),
                                     'RIG_SINGULAR_TRANSFORM')

    def test_constructor_rejects_integer_clock_that_would_round(self):
        for timestamp in (2 ** 54 + 1, 2 ** 54 + 3):
            with self.subTest(timestamp=timestamp):
                self.assertNotEqual(int(float(timestamp)), timestamp)
                with self.assertRaises(RigError) as error:
                    RigStateRuntime(self.rig, 'static-neutral', recipes={'static-neutral': 'neutral'},
                                    selected=['static-neutral'], now=timestamp)
                self.assertEqual(error.exception.report['code'], 'RIG_STATE_TIME')
                json.dumps(error.exception.report, allow_nan=False)

    def test_playback_rejects_inexact_integer_times_without_committing(self):
        for timestamp in (2 ** 54 + 1, 2 ** 54 + 3):
            for method in ('evaluate', 'request', 'cancel'):
                with self.subTest(timestamp=timestamp, method=method):
                    runtime = self.runtime(now=2 ** 54)
                    arguments = ('static-positive', timestamp) if method == 'request' else (timestamp,)
                    self.assert_rollback(runtime, lambda: getattr(runtime, method)(*arguments),
                                         'RIG_STATE_TIME')

    def test_exact_large_integer_repeats_and_backwards_rejection(self):
        start = 2 ** 54
        later = start + 4
        self.assertEqual(int(float(later)), later)
        runtime = self.runtime(now=start)
        expected = evaluate_animation(self.rig, 'neutral', 4., loop=True)['pose']
        for timestamp in (later, later):
            result = runtime.evaluate(timestamp)
            self.assertEqual(result['elapsed'], 4.)
            self.assert_geometry(result['pose'], expected)
        self.assert_rollback(runtime, lambda: runtime.request('static-positive', start),
                             'RIG_STATE_TIME')


if __name__ == '__main__':
    unittest.main()
