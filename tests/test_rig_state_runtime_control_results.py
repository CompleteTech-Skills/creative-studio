"""Independent final-pose contact oracles for bounded state playback.

Authored against PR #504 head 563278a2 before seeing the correction. Fixtures
use actual native FK, IK and skin deformation; no provider or renderer is used.
"""
from copy import deepcopy
import tempfile
import unittest

import numpy as np

from rig_animation import blend_poses, clip_from_poses, evaluate_animation
from rig_math import axis_angle, decompose, qangle
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture


class RigStateRuntimeControlResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name)
        self.target = np.array([0., 1.5, 0.])
        self.make_clip('small', 1.)
        self.make_clip('large', 1.5)
        self.recipes = {'static-neutral': 'small', 'static-positive': 'large',
                        'assistance-needed': 'small', 'notification-safe': 'large',
                        'idle': 'small'}

    def make_clip(self, name, scale, *, target=None, orientation=None):
        pose = neutral_pose(self.rig.skeleton)
        pose['bones']['b0'] = {'scale': [scale] * 3}
        contact = dict(schema_version=1, id='plant', chain='arm', start=0.,
                       end=1., target=list(self.target if target is None else target),
                       kind='foot')
        if orientation is not None:
            contact['orientation'] = orientation
        clip = clip_from_poses(self.rig, name, [0., 1.], [pose, pose],
                               loop=True, contacts=[contact])
        self.rig.clips[name] = clip
        return clip

    def runtime(self, initial='static-neutral', recipes=None):
        recipes = self.recipes if recipes is None else recipes
        return RigStateRuntime(self.rig, initial, recipes=recipes,
                               selected=list(recipes), blend_seconds=.5)

    def assert_contact_result(self, result, *, satisfied=True):
        fk = evaluate_fk(self.rig.skeleton, result['pose'])
        if satisfied:
            np.testing.assert_allclose(fk['b2'][:3, 3], self.target,
                                       rtol=0, atol=1e-6)
        self.assertIn('contacts', result)
        self.assertEqual(len(result['contacts']), 1)
        report = result['contacts'][0]
        self.assertEqual((report['id'], report['chain']), ('plant', 'arm'))
        measured = np.linalg.norm(fk['b2'][:3, 3] - report['target'])
        self.assertAlmostEqual(report['residual_m'], measured, places=10)
        self.assertEqual(report['converged'], satisfied)
        if satisfied:
            self.assertTrue(report['reachable'])
            self.assertIsNone(report['orientation_residual_rad'])
        for mesh in evaluate_deformation(self.rig, result['pose']).values():
            self.assertTrue(np.isfinite(mesh['positions']).all())
        return report

    def placed_scale_oracle(self, scale):
        # Independent oracle: author the final blended scale directly, then
        # execute the native clip. No runtime offset or runtime helper is used.
        # The rest-valued small clip legitimately omits its scale channel.
        # Start from large, whose authored non-rest scale must be represented.
        oracle = deepcopy(self.rig.clips['large'])
        tracks = [track for track in oracle['tracks']
                  if track['target'] == 'b0' and track['path'] == 'scale']
        self.assertEqual(len(tracks), 1)
        tracks[0]['values'] = [[scale] * 3 for _ in tracks[0]['values']]
        np.testing.assert_allclose(tracks[0]['values'],
                                   [[scale] * 3] * len(tracks[0]['times']),
                                   rtol=0, atol=0)
        return oracle

    def assert_geometry_matches_placed_scale(self, result, scale):
        oracle = self.placed_scale_oracle(scale)
        expected = evaluate_animation(self.rig, oracle, .25)['pose']
        actual_fk = evaluate_fk(self.rig.skeleton, result['pose'])
        expected_fk = evaluate_fk(self.rig.skeleton, expected)
        for bone in expected_fk:
            np.testing.assert_allclose(actual_fk[bone], expected_fk[bone],
                                       rtol=0, atol=1e-8, err_msg=bone)
        actual_mesh = evaluate_deformation(self.rig, result['pose'])
        expected_mesh = evaluate_deformation(self.rig, expected)
        for name in expected_mesh:
            np.testing.assert_allclose(actual_mesh[name]['positions'],
                                       expected_mesh[name]['positions'], rtol=0, atol=1e-8)

    def test_nonzero_request_blend_keeps_fixed_target_and_actual_geometry(self):
        runtime = self.runtime()
        self.assert_contact_result(runtime.request('static-positive', 0.))
        for time in (.125, .25, .375, .5):
            with self.subTest(time=time):
                result = runtime.evaluate(time)
                self.assertEqual(result['status'], 'playing')
                self.assert_contact_result(result)
                self.assert_geometry_matches_placed_scale(result, 1. + time)

    def test_cancel_blend_keeps_fixed_target_and_reports_final_pose(self):
        runtime = self.runtime('static-positive')
        self.assert_contact_result(runtime.cancel(.2))
        for elapsed in (.125, .25, .375, .5):
            with self.subTest(elapsed=elapsed):
                result = runtime.evaluate(.2 + elapsed)
                self.assertEqual(result['state'], 'static-neutral')
                self.assertEqual(result['status'], 'playing')
                self.assert_contact_result(result)
                self.assert_geometry_matches_placed_scale(result, 1.5 - elapsed)

    def test_completion_blend_keeps_fixed_target_and_reports_final_pose(self):
        runtime = self.runtime('notification-safe')
        for time in (1., 1.125, 1.25, 1.375, 1.5):
            with self.subTest(time=time):
                result = runtime.evaluate(time)
                self.assertEqual(result['state'], 'idle')
                self.assertEqual(result['status'], 'playing')
                self.assert_contact_result(result)
                self.assert_geometry_matches_placed_scale(result, 2.5 - time)

    def test_interrupted_blend_keeps_fixed_target_and_captured_geometry(self):
        runtime = self.runtime()
        runtime.request('static-positive', 0.)
        before = runtime.evaluate(.25)
        interrupted = runtime.request('assistance-needed', .25)
        self.assert_contact_result(before)
        self.assert_contact_result(interrupted)
        self.assert_geometry_matches_placed_scale(interrupted, 1.25)
        for elapsed in (.125, .25, .5):
            result = runtime.evaluate(.25 + elapsed)
            self.assert_contact_result(result)
            self.assert_geometry_matches_placed_scale(result, 1.25 - .5 * elapsed)

    def test_unreachable_contact_is_explicit_and_residual_matches_returned_pose(self):
        self.make_clip('unreachable', 1., target=[100., 100., 0.])
        runtime = self.runtime('static-positive', {'static-positive': 'unreachable'})
        result = runtime.evaluate(.25)
        self.assertEqual(result['status'], 'controls-unsatisfied')
        report = self.assert_contact_result(result, satisfied=False)
        self.assertFalse(report['reachable'])
        self.assertGreater(report['residual_m'], 100.)
        np.testing.assert_allclose(report['target'], [100., 100., 0.], rtol=0, atol=0)

    def test_orientation_nonconvergence_is_not_hidden_by_reached_position(self):
        desired = axis_angle([1., 0., 0.], 1.2).tolist()
        self.rig.manifest['controllers']['constraints'] = [dict(
            schema_version=1, id='hand-limit', bone='b2', type='hinge',
            axis=[1., 0., 0.], minimum=0., maximum=.2)]
        self.make_clip('limited', 1., target=[0., 2., 0.], orientation=desired)
        runtime = self.runtime('static-positive', {'static-positive': 'limited'})
        result = runtime.evaluate(.25)
        self.assertEqual(result['status'], 'controls-unsatisfied')
        report = self.assert_contact_result(result, satisfied=False)
        self.assertTrue(report['reachable'])
        self.assertLess(report['residual_m'], 1e-6)
        actual = decompose(evaluate_fk(self.rig.skeleton, result['pose'])['b2'])['rotation']
        self.assertAlmostEqual(report['orientation_residual_rad'], qangle(actual, desired), places=10)
        self.assertGreater(report['orientation_residual_rad'], .9)

    def test_unavailable_completion_preserves_successful_contact_diagnostics(self):
        runtime = self.runtime('notification-safe', {'notification-safe': 'large'})
        result = runtime.evaluate(2.)
        self.assertEqual(result['status'], 'completion-unavailable')
        self.assertEqual(result['pending_completion'], 'idle')
        self.assert_contact_result(result)

    def test_negative_control_postsolve_blend_breaks_plant_and_skinned_geometry(self):
        first = evaluate_animation(self.rig, 'small', .25)['pose']
        second = evaluate_animation(self.rig, 'large', .25)['pose']
        bad = blend_poses(self.rig, first, second, .5)
        endpoint = evaluate_fk(self.rig.skeleton, bad)['b2'][:3, 3]
        self.assertGreater(np.linalg.norm(endpoint - self.target), .08)
        oracle = self.placed_scale_oracle(1.25)
        good = evaluate_animation(self.rig, oracle, .25)['pose']
        a = evaluate_deformation(self.rig, bad)['strip']['positions']
        b = evaluate_deformation(self.rig, good)['strip']['positions']
        self.assertGreater(np.max(np.abs(a - b)), .01)


if __name__ == '__main__':
    unittest.main()
