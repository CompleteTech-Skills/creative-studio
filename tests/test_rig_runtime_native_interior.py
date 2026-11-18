"""Public elapsed time must select the native channel sample inside a loop."""
from copy import deepcopy
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
from rig_test_helpers import chain_skeleton, fixture


class RigRuntimeNativeInteriorTests(unittest.TestCase):
    origin = .1
    fields = ('state', 'entered', 'time', 'source_pose', '_input_pose',
              '_source_input_pose', '_source_contact_correction',
              'root_offset', 'transition_count')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)
        self.mesh = self.rig.geometry.meshes[0]

    def make_clip(self, *, skin_overflow=False, duration=1., times=(0., .375, .4)):
        tracks = [dict(schema_version=1, target=self.mesh.id, path='weights',
                       interpolation='STEP', times=list(times), values=[[0.], [1.], [0.]])]
        if skin_overflow:
            self.rig.manifest['skeleton'] = chain_skeleton(36)
            self.mesh.positions[:, 0] *= 1e8
            count = len(self.mesh.positions)
            rest = evaluate_fk(self.rig.skeleton)
            skin = SkinWeights(self.mesh.id, ['b35'], np.zeros((count, 4), dtype=np.uint16),
                               np.tile([1., 0., 0., 0.], (count, 1)),
                               np.array([np.linalg.inv(rest['b35'])]))
            self.rig.weights = {self.mesh.id: skin}
            validate_geometry(self.rig.geometry)
            validate_skin(skin, self.mesh, self.rig.skeleton)
            tracks.extend(dict(schema_version=1, target=bone['id'], path='scale',
                               interpolation='STEP', times=list(times),
                               values=[[1.] * 3, [2.5e8] * 3, [1.] * 3])
                          for bone in self.rig.skeleton['bones'])
        clip = deepcopy(self.rig.clips['neutral'])
        clip.update(id='interior-step', duration=duration, loop=True, root_motion='in-place',
                    tracks=tracks, contacts=[], events=[])
        validate_animation(self.rig, clip)
        self.rig.clips[clip['id']] = clip
        return clip

    def make_runtime(self, clip):
        recipes = {'static-neutral': 'neutral', 'static-positive': clip['id']}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), now=self.origin, blend_seconds=0.)
        runtime.request('static-positive', self.origin)
        return runtime, recipes

    def assert_native_frame(self, runtime, clip, now, blink):
        result = runtime.evaluate(now)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['status'], 'playing')
        self.assertEqual(result['elapsed'], now - self.origin)
        native = evaluate_animation(self.rig, clip, result['elapsed'], loop=True)
        self.assertEqual(native['pose']['morphs'][self.mesh.id]['blink'], blink)
        self.assertEqual(result['pose']['morphs'], native['pose']['morphs'])
        actual_fk = evaluate_fk(self.rig.skeleton, result['pose'])
        for bone, matrix in evaluate_fk(self.rig.skeleton, native['pose']).items():
            self.assertTrue(np.isfinite(matrix).all())
            np.testing.assert_array_equal(actual_fk[bone], matrix)
        expected = evaluate_deformation(self.rig, native['pose'])[self.mesh.id]['positions']
        self.assertTrue(np.isfinite(expected).all())
        np.testing.assert_array_equal(evaluate_deformation(self.rig, result['pose'])
                                      [self.mesh.id]['positions'], expected)
        return result

    def test_ordinary_origin_keeps_native_interior_step_and_vertices(self):
        clip = self.make_clip()
        runtime, _ = self.make_runtime(clip)
        self.assertEqual(1.5 - self.origin, 1.4)
        self.assertLess((1.5 - self.origin) % clip['duration'], .4)
        for now, blink in ((1.47, 0.), (1.49, 1.), (1.5, 1.), (1.51, 0.)):
            with self.subTest(now=now):
                self.assert_native_frame(runtime, clip, now, blink)

    def test_native_interior_skin_failure_restores_transaction_and_source(self):
        clip = self.make_clip(skin_overflow=True)
        self.assert_skin_failure_restores_playback(clip, 1.47, 1.5, 1.51)

    def test_collapsed_boundaries_keep_public_elapsed_step_and_vertices(self):
        clip = self.make_clip(duration=.1, times=(0., .01, .02))
        now = float(2**50)
        duration = Fraction(clip['duration'])
        exact_elapsed = Fraction(now) - Fraction(self.origin)
        cycles, exact_phase = divmod(exact_elapsed, duration)
        for cycle in (cycles, cycles + 1):
            self.assertEqual(float(Fraction(self.origin) + cycle*duration), now)
        native = evaluate_animation(self.rig, clip, now - self.origin, loop=True)
        self.assertGreaterEqual(native['time'], .01)
        self.assertLess(native['time'], .02)
        self.assertGreaterEqual(exact_phase, .02)
        runtime, _ = self.make_runtime(clip)
        for clock, blink in ((math.nextafter(now, -math.inf), 0.), (now, 1.),
                             (math.nextafter(now, math.inf), 0.)):
            with self.subTest(clock=clock):
                self.assert_native_frame(runtime, clip, clock, blink)

    def test_collapsed_native_skin_failure_restores_transaction_and_source(self):
        clip = self.make_clip(skin_overflow=True, duration=.1, times=(0., .01, .02))
        now = float(2**50)
        self.assert_skin_failure_restores_playback(clip, math.nextafter(now, -math.inf),
                                                   now, math.nextafter(now, math.inf))

    def assert_skin_failure_restores_playback(self, clip, before_time, now, after_time):
        runtime, recipes = self.make_runtime(clip)
        native = evaluate_animation(self.rig, clip, now - self.origin, loop=True)
        self.assertLess(native['time'], clip['tracks'][0]['times'][-1])
        self.assertEqual(native['pose']['morphs'][self.mesh.id]['blink'], 1.)
        self.assertTrue(all(np.isfinite(matrix).all()
                            for matrix in evaluate_fk(self.rig.skeleton, native['pose']).values()))
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as native_failure:
            evaluate_deformation(self.rig, native['pose'])
        self.assertEqual(native_failure.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
        before = self.assert_native_frame(runtime, clip, before_time, 0.)
        self.assertIsNotNone(runtime.source_pose)
        self.assertIsNotNone(runtime._source_input_pose)
        snapshot = {field: deepcopy(getattr(runtime, field)) for field in self.fields}
        documents = deepcopy((self.rig.manifest, self.rig.clips, recipes, runtime.recipes))
        source_paths = [Path(self.rig.root) / entry['path']
                        for entry in self.rig.manifest['source']['dependencies']]
        source_bytes = {path: path.read_bytes() for path in source_paths}
        skin = self.rig.weights[self.mesh.id]
        arrays = (self.mesh.positions, self.mesh.morphs['blink']['POSITION'],
                  skin.indices, skin.weights, skin.inverse_bind)
        originals = [array.copy() for array in arrays]
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as failure:
            runtime.evaluate(now)
        self.assertEqual(failure.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
        json.dumps(failure.exception.report, allow_nan=False)
        for field, value in snapshot.items():
            if field == 'root_offset':
                np.testing.assert_array_equal(runtime.root_offset, value)
            else:
                self.assertEqual(getattr(runtime, field), value, field)
        self.assertEqual((self.rig.manifest, self.rig.clips, recipes, runtime.recipes), documents)
        for path, content in source_bytes.items():
            self.assertEqual(path.read_bytes(), content)
        for actual, original in zip(arrays, originals):
            np.testing.assert_array_equal(actual, original)
        self.assertEqual(self.assert_native_frame(runtime, clip, before_time, 0.), before)
        self.assert_native_frame(runtime, clip, after_time, 0.)


if __name__ == '__main__':
    unittest.main()
