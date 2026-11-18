"""Independent transition-admission regressions authored before the correction.

The policy rejects physical discontinuities during nonzero blends. These tests
exercise real native FK, contacts and skinning on explicitly synthetic geometry.
"""
from copy import deepcopy
import tempfile
import unittest

import numpy as np

from rig_animation import clip_from_poses, evaluate_animation
from rig_errors import RigError
from rig_math import axis_angle
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture


class RigStateRuntimeTransitionAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rig = fixture(temporary.name)
        self.make_clip('plant-a', [0., 1.5, 0.])
        self.make_clip('plant-b', [1., 1.5, 0.])
        self.make_clip('plant-large', [0., 1.5, 0.], scale=1.5)

    def make_clip(self, name, target, *, scale=1., chain='arm', orientation=None):
        pose = neutral_pose(self.rig.skeleton)
        pose['bones']['b0'] = {'scale': [scale] * 3}
        contact = dict(schema_version=1, id='plant', chain=chain, start=0.,
                       end=1., target=target, kind='foot')
        if orientation is not None:
            contact['orientation'] = orientation
        self.rig.clips[name] = clip_from_poses(
            self.rig, name, [0., 1.], [pose, pose], loop=True, contacts=[contact])

    def runtime(self, recipes, initial='static-neutral', *, blend_seconds=.5):
        return RigStateRuntime(self.rig, initial, recipes=recipes,
                               selected=list(recipes), blend_seconds=blend_seconds)

    def snapshot(self, runtime):
        return deepcopy({name: getattr(runtime, name) for name in
                         ('state', 'entered', 'time', 'source_pose',
                          'root_offset', 'transition_count')})

    def assert_same_geometry(self, first, second):
        a, b = (evaluate_fk(self.rig.skeleton, pose) for pose in (first, second))
        self.assertEqual(set(a), set(b))
        for name in a:
            np.testing.assert_allclose(a[name], b[name], rtol=0, atol=1e-8,
                                       err_msg=name)
        a, b = (evaluate_deformation(self.rig, pose) for pose in (first, second))
        self.assertEqual(set(a), set(b))
        for name in a:
            np.testing.assert_allclose(a[name]['positions'], b[name]['positions'],
                                       rtol=0, atol=1e-8, err_msg=name)

    def assert_atomic_rejection(self, runtime, operation):
        before = runtime.evaluate(runtime.time)
        saved = self.snapshot(runtime)
        with self.assertRaises(RigError) as failure:
            operation()
        self.assertEqual(failure.exception.report['code'], 'RIG_STATE_CONTACT_TRANSITION')
        for name, value in saved.items():
            if name == 'root_offset':
                np.testing.assert_array_equal(getattr(runtime, name), value)
            else:
                self.assertEqual(getattr(runtime, name), value, name)
        self.assert_same_geometry(before['pose'], runtime.evaluate(saved['time'])['pose'])

    def test_different_target_request_rejects_and_preserves_fk_skin_and_state(self):
        runtime = self.runtime({'static-neutral': 'plant-a', 'static-positive': 'plant-b'})
        runtime.evaluate(.25)
        self.assert_atomic_rejection(runtime, lambda: runtime.request('static-positive', .25))

    def test_different_target_interrupt_rejects_and_preserves_captured_blend(self):
        runtime = self.runtime({'static-neutral': 'plant-a', 'static-positive': 'plant-large',
                                'assistance-needed': 'plant-b'})
        initial = runtime.evaluate(0.)
        entered = runtime.request('static-positive', 0.)
        self.assert_same_geometry(initial['pose'], entered['pose'])
        halfway = runtime.evaluate(.25)
        self.assertIsNotNone(runtime.source_pose)
        self.assert_atomic_rejection(runtime, lambda: runtime.request('assistance-needed', .25))
        self.assert_same_geometry(halfway['pose'], runtime.evaluate(.25)['pose'])
        finished = runtime.evaluate(.5)
        expected = evaluate_animation(self.rig, 'plant-large', .5)
        self.assert_same_geometry(finished['pose'], expected['pose'])

    def test_different_target_cancel_rejects_and_preserves_fk_skin_and_state(self):
        runtime = self.runtime({'static-neutral': 'plant-a', 'static-positive': 'plant-b'},
                               initial='static-positive')
        runtime.evaluate(.25)
        self.assert_atomic_rejection(runtime, lambda: runtime.cancel(.25))

    def test_different_target_completion_rejects_and_restores_preboundary_state(self):
        runtime = self.runtime({'notification-safe': 'plant-b', 'idle': 'plant-a'},
                               initial='notification-safe')
        runtime.evaluate(.75)
        self.assert_atomic_rejection(runtime, lambda: runtime.evaluate(1.))
        self.assertEqual(runtime.state, 'notification-safe')
        self.assertEqual(runtime.transition_count, 0)

    def test_orientation_change_rejects_despite_matching_endpoint_position(self):
        self.make_clip('orientation-a', [0., 1.5, 0.], orientation=[0., 0., 0., 1.])
        self.make_clip('orientation-b', [0., 1.5, 0.],
                       orientation=axis_angle([0., 1., 0.], .8).tolist())
        a, b = (evaluate_animation(self.rig, name, .25)['pose']
                for name in ('orientation-a', 'orientation-b'))
        fa, fb = (evaluate_fk(self.rig.skeleton, pose) for pose in (a, b))
        np.testing.assert_allclose(fa['b2'][:3, 3], fb['b2'][:3, 3], rtol=0, atol=1e-8)
        self.assertGreater(np.max(np.abs(fa['b2'][:3, :3] - fb['b2'][:3, :3])), .1)
        runtime = self.runtime({'static-neutral': 'orientation-a', 'static-positive': 'orientation-b'})
        runtime.evaluate(.25)
        self.assert_atomic_rejection(runtime, lambda: runtime.request('static-positive', .25))

    def test_new_chain_pole_rejects_bend_snap_with_unchanged_endpoint(self):
        reverse = deepcopy(self.rig.manifest['controllers']['ik'][0])
        reverse.update(id='reverse-arm', pole=[0., 0., -1.])
        self.rig.manifest['controllers']['ik'].append(reverse)
        self.make_clip('reverse-plant', [0., 1.5, 0.], chain='reverse-arm')
        a, b = (evaluate_animation(self.rig, name, .25)['pose']
                for name in ('plant-a', 'reverse-plant'))
        fa, fb = (evaluate_fk(self.rig.skeleton, pose) for pose in (a, b))
        np.testing.assert_allclose(fa['b2'][:3, 3], fb['b2'][:3, 3], rtol=0, atol=1e-8)
        self.assertGreater(np.linalg.norm(fa['b1'][:3, 3] - fb['b1'][:3, 3]), 1.)
        da, db = (evaluate_deformation(self.rig, pose)['strip']['positions'] for pose in (a, b))
        self.assertGreater(np.max(np.abs(da - db)), .01)
        runtime = self.runtime({'static-neutral': 'plant-a', 'static-positive': 'reverse-plant'})
        runtime.evaluate(.25)
        self.assert_atomic_rejection(runtime, lambda: runtime.request('static-positive', .25))

    def test_compatible_contact_accepts_blend_and_preserves_boundary_geometry(self):
        runtime = self.runtime({'static-neutral': 'plant-a', 'static-positive': 'plant-large'})
        before = runtime.evaluate(.25)
        after = runtime.request('static-positive', .25)
        self.assertEqual(after['state'], 'static-positive')
        self.assertEqual(after['transition_count'], 1)
        self.assertEqual(after['status'], 'playing')
        self.assert_same_geometry(before['pose'], after['pose'])
        for time in (.375, .5, .625, .75):
            result = runtime.evaluate(time)
            np.testing.assert_allclose(evaluate_fk(self.rig.skeleton, result['pose'])['b2'][:3, 3],
                                       [0., 1.5, 0.], rtol=0, atol=1e-6)
            self.assertTrue(result['contacts'][0]['converged'])
        expected = evaluate_animation(self.rig, 'plant-large', .5)
        self.assert_same_geometry(result['pose'], expected['pose'])

    def test_zero_blend_explicitly_allows_immediate_different_contact(self):
        runtime = self.runtime({'static-neutral': 'plant-a', 'static-positive': 'plant-b'},
                               blend_seconds=0.)
        before = runtime.evaluate(.25)
        after = runtime.request('static-positive', .25)
        self.assertEqual(after['state'], 'static-positive')
        self.assertEqual(after['status'], 'playing')
        self.assertEqual(after['transition_count'], 1)
        expected = evaluate_animation(self.rig, 'plant-b', 0.)
        self.assert_same_geometry(after['pose'], expected['pose'])
        a, b = (evaluate_fk(self.rig.skeleton, pose)['b2'][:3, 3]
                for pose in (before['pose'], after['pose']))
        self.assertAlmostEqual(np.linalg.norm(a - b), 1., places=8)


if __name__ == '__main__':
    unittest.main()
