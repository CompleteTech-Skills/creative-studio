"""Independent binary64 elapsed-phase oracles for PR #504 / issue #530.

Authored against 40c43775624d74a8712f0e539844b2655b9fc06c. Synthetic fixtures
execute native sampling, controls, FK and skinning without mocks. Interior
playback uses the same elapsed value reported to callers; discrete schedule
endpoints retain their own half-open ownership and completion semantics.
"""
from copy import deepcopy
from dataclasses import asdict
from fractions import Fraction
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, validate_animation
from rig_errors import RigError
from rig_models import SkinWeights, validate_geometry
from rig_skeleton import evaluate_fk
from rig_skinning import evaluate_deformation, validate_skin
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture
from test_rig_math import chain_skeleton


class RigRuntimeElapsedPhaseTests(unittest.TestCase):
    STATE_FIELDS = ('state', 'entered', 'time', 'source_pose', '_input_pose',
                    '_source_input_pose', '_source_contact_correction',
                    'root_offset', 'transition_count')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)

    def step_clip(self, *, duration=1., step=.5, loop=True, scales=False):
        mesh = self.rig.geometry.meshes[0]
        tracks = [dict(schema_version=1, target=mesh.id, path='weights',
                       interpolation='STEP', times=[0., step], values=[[0.], [1.]])]
        if scales:
            tracks.extend(dict(schema_version=1, target=bone['id'], path='scale',
                               interpolation='STEP', times=[0., step],
                               values=[[1.] * 3, [2.5e8] * 3])
                          for bone in self.rig.skeleton['bones'])
        else:
            tracks.append(dict(schema_version=1, target='b1', path='scale',
                               interpolation='STEP', times=[0., step],
                               values=[[1.] * 3, [1.25] * 3]))
        clip = deepcopy(self.rig.clips['neutral'])
        clip.update(id='elapsed-step', duration=duration, loop=loop,
                    root_motion='in-place', contacts=[], events=[], tracks=tracks)
        validate_animation(self.rig, clip)
        self.rig.clips[clip['id']] = clip
        return clip

    def runtime(self, clip, entered):
        recipes = {'static-neutral': 'neutral', 'static-positive': clip['id']}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), now=entered, blend_seconds=0.)
        runtime.request('static-positive', entered)
        self.assertIsNotNone(runtime.source_pose)
        self.assertIsNotNone(runtime._source_input_pose)
        return runtime

    def assert_same_geometry(self, actual, expected):
        worlds = [evaluate_fk(self.rig.skeleton, pose) for pose in (actual, expected)]
        skins = [evaluate_deformation(self.rig, pose) for pose in (actual, expected)]
        for actual_arrays, expected_arrays in (
                worlds, [{name: mesh['positions'] for name, mesh in skin.items()}
                         for skin in skins]):
            self.assertEqual(set(actual_arrays), set(expected_arrays))
            for name, reference in expected_arrays.items():
                self.assertTrue(np.isfinite(actual_arrays[name]).all(), name)
                np.testing.assert_array_equal(actual_arrays[name], reference, err_msg=name)

    def assert_tree_equal(self, actual, expected):
        if isinstance(expected, np.ndarray):
            np.testing.assert_array_equal(actual, expected)
        elif isinstance(expected, dict):
            self.assertEqual(set(actual), set(expected))
            for key in expected:
                self.assert_tree_equal(actual[key], expected[key])
        elif isinstance(expected, (tuple, list)):
            self.assertEqual(len(actual), len(expected))
            for a, b in zip(actual, expected):
                self.assert_tree_equal(a, b)
        else:
            self.assertEqual(actual, expected)

    def preserve_asset(self):
        def snapshot():
            return (deepcopy((self.rig.manifest, self.rig.clips)),
                    asdict(self.rig.geometry),
                    {name: asdict(skin) for name, skin in self.rig.weights.items()},
                    {descriptor['path']: (Path(self.rig.root) / descriptor['path']).read_bytes()
                     for descriptor in self.rig.manifest['source']['dependencies']})
        original = snapshot()
        self.addCleanup(lambda: self.assert_tree_equal(snapshot(), original))

    def test_adjacent_interior_samples_match_reported_elapsed_at_both_origins(self):
        clip = self.step_clip()
        self.preserve_asset()
        # Binary64 subtraction rounds this represented-clock difference to 2.5.
        # Subtracting exact rationals first instead yields a phase below .5.
        self.assertEqual(2.51 - .01, 2.5)
        self.assertLess(float((Fraction(2.51) - Fraction(.01)) % 1), .5)
        for entered in (0., .01):
            runtime = self.runtime(clip, entered)
            boundary = entered + 2.5
            for now, blink in ((math.nextafter(boundary, -math.inf), 0.),
                               (boundary, 1.),
                               (math.nextafter(boundary, math.inf), 1.)):
                with self.subTest(entered=entered, now=now):
                    frame = runtime.evaluate(now)
                    self.assertEqual(frame['elapsed'], now - entered)
                    native = evaluate_animation(self.rig, clip, frame['elapsed'], loop=True)
                    self.assertEqual(native['pose']['morphs']['strip']['blink'], blink)
                    self.assertEqual(frame['status'], 'playing')
                    self.assertEqual(frame['control_stage'], 'native')
                    json.dumps(frame, allow_nan=False)
                    self.assert_same_geometry(frame['pose'], native['pose'])
                    self.assertEqual(frame['pose']['morphs']['strip']['blink'], blink)

    def test_native_skin_failure_rejects_same_elapsed_and_restores_all_nine_fields(self):
        self.rig.manifest['skeleton'] = chain_skeleton(36)
        mesh = self.rig.geometry.meshes[0]
        mesh.positions[:, 0] *= 1e8
        rest = evaluate_fk(self.rig.skeleton)
        count = len(mesh.positions)
        skin = SkinWeights(mesh.id, ['b35'], np.zeros((count, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (count, 1)),
                           np.array([np.linalg.inv(rest['b35'])]))
        self.rig.weights = {mesh.id: skin}
        validate_geometry(self.rig.geometry)
        validate_skin(skin, mesh, self.rig.skeleton)
        clip = self.step_clip(scales=True)
        self.preserve_asset()
        # The native pose has finite FK. Its valid last-bone skin overflows only
        # when transforming actual vertices, so FK-only admission is insufficient.
        for elapsed in (2.5, math.nextafter(2.5, math.inf)):
            native = evaluate_animation(self.rig, clip, elapsed, loop=True)
            self.assertTrue(all(np.isfinite(matrix).all()
                                for matrix in evaluate_fk(self.rig.skeleton,
                                                          native['pose']).values()))
            with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as error:
                evaluate_deformation(self.rig, native['pose'])
            self.assertEqual(error.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
        for entered in (0., .01):
            boundary = entered + 2.5
            for now in (boundary, math.nextafter(boundary, math.inf)):
                with self.subTest(entered=entered, now=now):
                    runtime = self.runtime(clip, entered)
                    prior_time = math.nextafter(boundary, -math.inf)
                    before = runtime.evaluate(prior_time)
                    native = evaluate_animation(self.rig, clip, prior_time-entered, loop=True)
                    self.assert_same_geometry(before['pose'], native['pose'])
                    snapshot = {key: deepcopy(getattr(runtime, key)) for key in self.STATE_FIELDS}
                    with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as error:
                        runtime.evaluate(now)
                    self.assertEqual(error.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
                    json.dumps(error.exception.report, allow_nan=False)
                    for key, value in snapshot.items():
                        with self.subTest(field=key):
                            self.assert_tree_equal(getattr(runtime, key), value)
                    self.assert_same_geometry(runtime.evaluate(prior_time)['pose'], before['pose'])

    def test_loop_start_keeps_half_open_endpoint_and_native_neighbor_controls(self):
        clip = self.step_clip()
        self.preserve_asset()
        for entered in (0., .01):
            runtime = self.runtime(clip, entered)
            boundary = entered + 1.
            for now, blink in ((math.nextafter(boundary, -math.inf), 1.),
                               (boundary, 0.),
                               (math.nextafter(boundary, math.inf), 0.)):
                with self.subTest(entered=entered, now=now):
                    frame = runtime.evaluate(now)
                    native = evaluate_animation(self.rig, clip, now-entered, loop=True)
                    self.assertEqual(frame['pose']['morphs']['strip']['blink'], blink)
                    self.assertEqual(frame['transition_count'], 1)
                    self.assert_same_geometry(frame['pose'], native['pose'])

    def test_completion_captures_endpoint_when_reported_subtraction_is_interior(self):
        clip = self.step_clip(duration=.2, step=.2, loop=False)
        self.preserve_asset()
        entered = .15
        boundary = entered + clip['duration']
        self.assertLess(boundary-entered, clip['duration'])
        recipes = {'notification-safe': clip['id'], 'idle': 'neutral'}
        runtime = RigStateRuntime(self.rig, 'notification-safe', recipes=recipes,
                                  selected=list(recipes), now=entered, blend_seconds=.05)
        before = runtime.evaluate(math.nextafter(boundary, -math.inf))
        self.assertEqual(before['state'], 'notification-safe')
        self.assertEqual(before['pose']['morphs']['strip']['blink'], 0.)
        endpoint = evaluate_animation(self.rig, clip, clip['duration'], loop=False)['pose']
        frame = runtime.evaluate(boundary)
        self.assertEqual(frame['state'], 'idle')
        self.assertEqual(frame['elapsed'], 0.)
        self.assertEqual(frame['transition_count'], 1)
        self.assert_same_geometry(runtime.source_pose, endpoint)
        self.assert_same_geometry(runtime._source_input_pose, endpoint)
        self.assert_same_geometry(frame['pose'], endpoint)
        json.dumps(frame, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
