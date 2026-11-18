"""Native half-open recipe ownership when absolute cycle boundaries coincide."""
from copy import deepcopy
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


class RigRuntimeCollapsedPrefixTests(unittest.TestCase):
    durations = (.001, .004, .04499999999999999, .05000000000000002)
    origin = float(2 ** 50)
    fields = ('state', 'entered', 'time', 'source_pose', '_input_pose',
              '_source_input_pose', '_source_contact_correction',
              'root_offset', 'transition_count')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)
        self.mesh = self.rig.geometry.meshes[0]

    def make_runtime(self, final_tracks=()):
        names = []
        for index, duration in enumerate(self.durations):
            name = 'phase-' + str(index)
            clip = deepcopy(self.rig.clips['neutral'])
            tracks = [dict(schema_version=1, target=self.mesh.id, path='weights',
                           interpolation='STEP', times=[0.], values=[[float(index == 3)]])]
            if index == 3:
                tracks.extend(final_tracks)
            clip.update(id=name, duration=duration, tracks=tracks, loop=True,
                        root_motion='in-place', events=[], contacts=[])
            validate_animation(self.rig, clip)
            self.rig.clips[name] = clip
            names.append(name)
        recipes = {'static-neutral': 'neutral', 'static-positive': names}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), now=self.origin, blend_seconds=0.)
        runtime.request('static-positive', self.origin)
        return runtime, recipes

    def assert_geometry(self, first, second):
        actual_fk, expected_fk = (evaluate_fk(self.rig.skeleton, p) for p in (first, second))
        self.assertEqual(set(actual_fk), set(expected_fk))
        for bone, expected in expected_fk.items():
            np.testing.assert_array_equal(actual_fk[bone], expected)
        actual_skin, expected_skin = (evaluate_deformation(self.rig, p) for p in (first, second))
        for mesh, expected in expected_skin.items():
            np.testing.assert_array_equal(actual_skin[mesh]['positions'], expected['positions'])

    def test_collapsed_cycle_owns_fourth_clip_at_single_rounded_prefix(self):
        elapsed = .25
        duration = math.fsum(self.durations)
        self.assertEqual(duration, .1)
        self.assertEqual(self.origin + 2 * duration, self.origin + 3 * duration)
        phase = elapsed % duration
        self.assertEqual(phase, math.fsum(self.durations[:3]))
        self.assertLess((phase - self.durations[0]) - self.durations[1], self.durations[2])
        runtime, recipes = self.make_runtime()
        fourth = recipes['static-positive'][3]
        expected = evaluate_animation(self.rig, fourth, 0.)['pose']
        result = runtime.evaluate(self.origin + elapsed)
        self.assertEqual(result['pose']['morphs'][self.mesh.id]['blink'], 1.)
        self.assert_geometry(result['pose'], expected)
        self.assertEqual(result['status'], 'playing')
        self.assertEqual(result['elapsed'], elapsed)
        json.dumps(result, allow_nan=False)

    def test_collapsed_cycle_rejects_fourth_clip_skin_overflow_and_restores_playback(self):
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
        tracks = [dict(schema_version=1, target=bone['id'], path='scale', interpolation='STEP',
                       times=[0.], values=[[2.5e8] * 3]) for bone in self.rig.skeleton['bones']]
        runtime, recipes = self.make_runtime(tracks)
        fourth = recipes['static-positive'][3]
        expected = evaluate_animation(self.rig, fourth, 0.)['pose']
        self.assertTrue(all(np.isfinite(matrix).all()
                            for matrix in evaluate_fk(self.rig.skeleton, expected).values()))
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as native:
            evaluate_deformation(self.rig, expected)
        self.assertEqual(native.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')

        before = runtime.evaluate(self.origin)
        self.assertIsNotNone(runtime.source_pose)
        self.assertIsNotNone(runtime._source_input_pose)
        state = {field: deepcopy(getattr(runtime, field)) for field in self.fields}
        documents = deepcopy((self.rig.manifest, self.rig.clips, recipes, runtime.recipes))
        sources = {Path(self.rig.root) / entry['path'] for entry in self.rig.manifest['source']['dependencies']}
        source_bytes = {path: path.read_bytes() for path in sources}
        arrays = (self.mesh.positions, skin.indices, skin.weights, skin.inverse_bind)
        original_arrays = [array.copy() for array in arrays]
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as failure:
            runtime.evaluate(self.origin + .25)
        self.assertEqual(failure.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
        json.dumps(failure.exception.report, allow_nan=False)
        for field, saved in state.items():
            if field == 'root_offset':
                np.testing.assert_array_equal(getattr(runtime, field), saved)
            else:
                self.assertEqual(getattr(runtime, field), saved, field)
        self.assertEqual((self.rig.manifest, self.rig.clips, recipes, runtime.recipes), documents)
        for path, content in source_bytes.items():
            self.assertEqual(path.read_bytes(), content)
        for actual, original in zip(arrays, original_arrays):
            np.testing.assert_array_equal(actual, original)
        self.assert_geometry(runtime.evaluate(self.origin)['pose'], before['pose'])


if __name__ == '__main__':
    unittest.main()
