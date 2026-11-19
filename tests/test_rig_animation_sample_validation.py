"""Independent sampled-pose safety regressions using real synthetic rig fixtures.

Key admission and sample usability are different boundaries: finite, valid keys
can interpolate outside native scale/morph domains or overflow composed FK.
These tests exercise native owners and transactional state playback without
mocking sampling, controls, FK, or skin deformation.
"""
from copy import deepcopy
import tempfile
import unittest

import numpy as np

from rig_animation import (apply_animation_controls, evaluate_animation,
                           evaluate_track, validate_animation)
from rig_contracts import validate_contract
from rig_errors import RigError
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation, generate_skin_weights
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture
from test_rig_math import chain_skeleton


class RigAnimationSampleValidationTests(unittest.TestCase):
    STATE_FIELDS = ('state', 'entered', 'time', 'source_pose', '_input_pose',
                    '_source_input_pose', '_source_contact_correction',
                    'root_offset', 'transition_count')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rig = fixture(temporary.name)
        self.mesh = self.rig.geometry.meshes[0]
        self.morph = next(iter(self.mesh.morphs))

    def track(self, path, *, target=None, value=None, tangent=0., interpolation='CUBICSPLINE'):
        width = len(self.mesh.morphs) if path == 'weights' else 3
        if target is None:
            target = self.mesh.id if path == 'weights' else 'b0'
        if value is None:
            value = 0. if path == 'weights' else 1.
        result = dict(schema_version=1, target=target, path=path,
                      interpolation=interpolation, times=[0., 1.],
                      values=[[value] * width, [value] * width])
        if interpolation == 'CUBICSPLINE':
            result.update(in_tangents=[[0.] * width, [-tangent] * width],
                          out_tangents=[[tangent] * width, [0.] * width])
        return result

    def clip(self, name, tracks, *, contacts=None):
        clip = deepcopy(self.rig.clips['neutral'])
        clip.update(id=name, tracks=tracks, contacts=deepcopy(contacts or []))
        # Every adverse fixture must pass both schema and native key admission.
        validate_contract(clip, 'AnimationClip')
        validate_animation(self.rig, clip)
        self.rig.clips[name] = clip
        return clip

    def raw_pose(self, clip, time):
        pose = neutral_pose(self.rig.skeleton)
        for track in clip['tracks']:
            value = evaluate_track(track, time)
            if track['path'] == 'weights':
                pose['morphs'][track['target']] = dict(zip(self.mesh.morphs, value.tolist()))
            else:
                pose['bones'].setdefault(track['target'], {})[track['path']] = value.tolist()
        return pose

    def assert_usable(self, pose):
        validate_contract(pose, 'Pose')
        for matrix in evaluate_fk(self.rig.skeleton, pose).values():
            self.assertTrue(np.isfinite(matrix).all())
        for mesh in evaluate_deformation(self.rig, pose).values():
            self.assertTrue(np.isfinite(mesh['positions']).all())

    def assert_rig_error(self, code, operation):
        with self.assertRaises(RigError) as failure:
            operation()
        self.assertEqual(failure.exception.report['code'], code)
        self.assertTrue(failure.exception.report['message'])

    def assert_atomic_rejection(self, runtime, operation, code):
        before = runtime.evaluate(runtime.time)
        saved = deepcopy({field: getattr(runtime, field) for field in self.STATE_FIELDS})
        manifest, clips = deepcopy(self.rig.manifest), deepcopy(self.rig.clips)
        fk = evaluate_fk(self.rig.skeleton, before['pose'])
        skin = evaluate_deformation(self.rig, before['pose'])
        self.assert_rig_error(code, operation)
        for field, expected in saved.items():
            if field == 'root_offset':
                np.testing.assert_array_equal(getattr(runtime, field), expected)
            else:
                self.assertEqual(getattr(runtime, field), expected, field)
        self.assertEqual(self.rig.manifest, manifest)
        self.assertEqual(self.rig.clips, clips)
        after = runtime.evaluate(saved['time'])
        for name, matrix in evaluate_fk(self.rig.skeleton, after['pose']).items():
            np.testing.assert_array_equal(matrix, fk[name], err_msg=name)
        for name, mesh in evaluate_deformation(self.rig, after['pose']).items():
            np.testing.assert_array_equal(mesh['positions'], skin[name]['positions'], err_msg=name)

    def test_negative_control_native_owners_reject_unusable_interior_samples(self):
        for tangent, expected in ((-8., -1.), (-4., 0.)):
            with self.subTest(path='scale', tangent=tangent):
                clip = self.clip('scale-owner', [self.track('scale', tangent=tangent)])
                for time in (0., 1.):
                    self.assert_usable(self.raw_pose(clip, time))
                pose = self.raw_pose(clip, .5)
                np.testing.assert_array_equal(pose['bones']['b0']['scale'], [expected] * 3)
                self.assert_rig_error('RIG_SINGULAR_TRANSFORM',
                                      lambda: evaluate_fk(self.rig.skeleton, pose))
        for tangent in (-12., 12.):
            with self.subTest(path='weights', tangent=tangent):
                clip = self.clip('morph-owner', [self.track('weights', tangent=tangent)])
                for time in (0., 1.):
                    self.assert_usable(self.raw_pose(clip, time))
                pose = self.raw_pose(clip, .5)
                self.assertEqual(pose['morphs'][self.mesh.id][self.morph], tangent / 4.)
                self.assert_rig_error('RIG_MORPH_RANGE',
                                      lambda: evaluate_deformation(self.rig, pose))

    def test_native_evaluation_rejects_zero_and_negative_cubic_scale_before_return(self):
        for tangent in (-8., -4.):
            clip = self.clip('unsafe-scale', [self.track('scale', tangent=tangent)])
            for controls in (False, True):
                with self.subTest(tangent=tangent, controls=controls):
                    self.assert_rig_error('RIG_SINGULAR_TRANSFORM', lambda: evaluate_animation(
                        self.rig, clip, .5, contacts=controls, constraints=controls))

    def test_native_evaluation_rejects_cubic_morph_overshoot_in_both_directions(self):
        for tangent in (-12., 12.):
            clip = self.clip('unsafe-morph', [self.track('weights', tangent=tangent)])
            for controls in (False, True):
                with self.subTest(tangent=tangent, controls=controls):
                    self.assert_rig_error('RIG_MORPH_RANGE', lambda: evaluate_animation(
                        self.rig, clip, .5, contacts=controls, constraints=controls))

    def test_native_control_entry_rejects_unusable_sample_even_without_controls(self):
        for path, tangent, code in (('scale', -8., 'RIG_SINGULAR_TRANSFORM'),
                                    ('weights', 12., 'RIG_MORPH_RANGE')):
            clip = self.clip('control-input', [self.track(path, tangent=tangent)])
            pose = self.raw_pose(clip, .5)
            original = deepcopy(pose)
            with self.subTest(path=path):
                self.assert_rig_error(code, lambda: apply_animation_controls(
                    self.rig, clip, pose, .5, contacts=False, constraints=False))
                self.assertEqual(pose, original)

    def test_finite_cubic_channels_cannot_return_nonfinite_composed_fk(self):
        # Individual scales are finite and positive at the adverse sample, but
        # forty parent/child scale products exceed float64's representation.
        self.rig.manifest['skeleton'] = chain_skeleton(40)
        self.rig.weights = generate_skin_weights(self.rig.geometry, self.rig.skeleton)
        clip = self.clip('composition-overflow', [
            self.track('scale', target=bone['id'], tangent=1e9)
            for bone in self.rig.skeleton['bones']])
        for time in (0., 1.):
            self.assert_usable(self.raw_pose(clip, time))
        pose = self.raw_pose(clip, .5)
        self.assertTrue(all(np.isfinite(bone['scale']).all() and min(bone['scale']) > 0
                            for bone in pose['bones'].values()))
        with np.errstate(over='ignore', invalid='ignore'):
            self.assert_rig_error('RIG_NONFINITE', lambda: evaluate_fk(self.rig.skeleton, pose))
            self.assert_rig_error('RIG_NONFINITE', lambda: evaluate_animation(
                self.rig, clip, .5, contacts=False, constraints=False))

    def runtime_with_release_snapshot(self):
        bad = self.clip('unsafe-scale', [self.track('scale', tangent=-8.)])
        plant = dict(schema_version=1, id='plant', chain='arm', start=0., end=1.,
                     target=[0., 2., 0.], kind='hand')
        self.clip('planted', deepcopy(self.rig.clips['neutral']['tracks']), contacts=[plant])
        recipes = {'static-neutral': 'planted', 'static-positive': bad['id'],
                   'assistance-needed': 'neutral'}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), blend_seconds=.75)
        runtime.request('static-positive', 0.)
        runtime.evaluate(.125)
        self.assertIsNotNone(runtime.source_pose)
        self.assertIsNotNone(runtime._input_pose)
        self.assertIsNotNone(runtime._source_input_pose)
        self.assertTrue(runtime._source_contact_correction)
        return runtime

    def test_runtime_sample_failure_restores_all_nine_fields_and_release_provenance(self):
        for action in ('evaluate', 'request', 'cancel'):
            with self.subTest(action=action):
                runtime = self.runtime_with_release_snapshot()
                operation = (lambda: runtime.request('assistance-needed', .5)) if action == 'request' else (
                    lambda: getattr(runtime, action)(.5))
                self.assert_atomic_rejection(runtime, operation, 'RIG_SINGULAR_TRANSFORM')

    def test_runtime_morph_failure_preserves_prior_fk_skin_and_sources(self):
        self.clip('unsafe-morph', [self.track('weights', tangent=12.)])
        runtime = RigStateRuntime(self.rig, 'static-positive',
                                  recipes={'static-positive': 'unsafe-morph'},
                                  selected=['static-positive'], blend_seconds=0)
        runtime.evaluate(.125)
        self.assert_atomic_rejection(runtime, lambda: runtime.evaluate(.5), 'RIG_MORPH_RANGE')

    def test_failure_after_completion_edge_restores_previous_state_and_time(self):
        self.clip('unsafe-scale', [self.track('scale', tangent=-8.)])
        recipes = {'notification-safe': 'neutral', 'idle': 'unsafe-scale'}
        runtime = RigStateRuntime(self.rig, 'notification-safe', recipes=recipes,
                                  selected=list(recipes), blend_seconds=.2)
        runtime.evaluate(.25)
        self.assert_atomic_rejection(runtime, lambda: runtime.evaluate(1.5), 'RIG_SINGULAR_TRANSFORM')

    def test_bounded_cubic_linear_step_with_native_controls_and_exact_boundaries_work(self):
        self.rig.manifest['controllers']['constraints'] = [dict(
            schema_version=1, id='elbow-limit', bone='b1', type='hinge',
            axis=[0., 0., 1.], minimum=-2.8, maximum=2.8)]
        plant = dict(schema_version=1, id='plant', chain='arm', start=0., end=1.,
                     target=[0., 1.5, 0.], kind='hand')
        for interpolation in ('CUBICSPLINE', 'LINEAR', 'STEP'):
            scale = self.track('scale', interpolation=interpolation)
            scale['values'][1] = [1.2] * 3
            weights = self.track('weights', interpolation=interpolation)
            weights['values'] = [[-2.] * len(self.mesh.morphs), [2.] * len(self.mesh.morphs)]
            clip = self.clip('bounded', [scale, weights], contacts=[plant])
            runtime = RigStateRuntime(self.rig, 'static-positive',
                                      recipes={'static-positive': clip['id']},
                                      selected=['static-positive'], blend_seconds=0)
            for time in (0., .25, .5, .75, 1.):
                with self.subTest(interpolation=interpolation, time=time):
                    result = evaluate_animation(self.rig, clip, time)
                    self.assert_usable(result['pose'])
                    amount = result['pose']['morphs'][self.mesh.id][self.morph]
                    self.assertGreaterEqual(amount, -2.)
                    self.assertLessEqual(amount, 2.)
                    if time in (0., 1.):
                        self.assertEqual(amount, -2. if time == 0. else 2.)
                    self.assertEqual(len(result['final_contacts']), 1)
                    endpoint = evaluate_fk(self.rig.skeleton, result['pose'])['b2'][:3, 3]
                    self.assertAlmostEqual(result['final_contacts'][0]['residual_m'],
                                           float(np.linalg.norm(endpoint - plant['target'])), places=10)
                    self.assert_usable(runtime.evaluate(time)['pose'])


    def test_runtime_rejects_nonfinite_skinning_even_when_sampled_fk_is_finite(self):
        from rig_models import SkinWeights, validate_geometry
        from rig_skinning import validate_skin

        self.rig.manifest['skeleton'] = chain_skeleton(36)
        self.mesh.positions[:, 0] *= 1e8
        rest = evaluate_fk(self.rig.skeleton)
        count = len(self.mesh.positions)
        skin = SkinWeights(
            self.mesh.id, ['b35'], np.zeros((count, 4), dtype=np.uint16),
            np.tile([1., 0., 0., 0.], (count, 1)),
            np.array([np.linalg.inv(rest['b35'])]))
        self.rig.weights = {self.mesh.id: skin}
        validate_geometry(self.rig.geometry)
        validate_skin(skin, self.mesh, self.rig.skeleton)
        np.testing.assert_allclose(np.abs(self.mesh.positions[:, 0]), 1e7)
        clip = self.clip('deformation-overflow', [
            self.track('scale', target=bone['id'], tangent=1e9)
            for bone in self.rig.skeleton['bones']])
        for time in (0., 1.):
            self.assert_usable(evaluate_animation(self.rig, clip, time)['pose'])

        # Local scales and all composed bone matrices remain finite. The
        # overflow occurs only when the real skin palette transforms vertices.
        sampled = self.raw_pose(clip, .5)
        validate_contract(sampled, 'Pose')
        world = evaluate_fk(self.rig.skeleton, sampled)
        self.assertTrue(all(np.isfinite(matrix).all() for matrix in world.values()))
        self.assertGreater(float(np.max(np.abs(world['b35']))), 1e300)
        with np.errstate(over='ignore', invalid='ignore'):
            self.assert_rig_error('RIG_NONFINITE_DEFORMATION',
                                  lambda: evaluate_deformation(self.rig, sampled))

        recipes = {'static-neutral': 'neutral', 'static-positive': clip['id']}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), blend_seconds=.2)
        runtime.request('static-positive', 0.)
        self.assert_usable(runtime.evaluate(.125)['pose'])
        self.assertIsNotNone(runtime.source_pose)
        self.assertIsNotNone(runtime._input_pose)
        self.assertIsNotNone(runtime._source_input_pose)
        with np.errstate(over='ignore', invalid='ignore'):
            self.assert_atomic_rejection(runtime, lambda: runtime.evaluate(.5),
                                         'RIG_NONFINITE_DEFORMATION')

    def test_runtime_refuses_unusable_precontrol_pose_even_if_joint_lock_repairs_skin(self):
        from rig_animation import clip_from_poses
        from rig_math import axis_angle
        from rig_models import SkinWeights, validate_geometry
        from rig_skinning import validate_skin

        self.rig.manifest['skeleton'] = chain_skeleton(36)
        self.mesh.positions[:, 1] *= 1e6
        rest = evaluate_fk(self.rig.skeleton)
        count = len(self.mesh.positions)
        skin = SkinWeights(self.mesh.id, ['b35'], np.zeros((count, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (count, 1)),
                           np.array([np.linalg.inv(rest['b35'])]))
        self.rig.weights = {self.mesh.id: skin}
        validate_geometry(self.rig.geometry)
        validate_skin(skin, self.mesh, self.rig.skeleton)
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='lock-end', bone='b35', type='locked', axes=[0, 1, 2])]
        neutral = neutral_pose(self.rig.skeleton)
        turned = deepcopy(neutral)
        for bone in self.rig.skeleton['bones'][:-1]:
            turned['bones'][bone['id']] = {'scale': [4.4e8, 1., 1.]}
        turned['bones']['b35'] = {'rotation': axis_angle([0., 0., 1.], np.pi / 2).tolist()}
        clip = clip_from_poses(self.rig, 'locked-strip', [0., .5, 1.], [neutral, turned, neutral])
        self.rig.clips[clip['id']] = clip
        self.assert_usable(self.raw_pose(clip, .125))
        raw = self.raw_pose(clip, .5)
        validate_contract(raw, 'Pose')
        self.assertTrue(all(np.isfinite(matrix).all()
                            for matrix in evaluate_fk(self.rig.skeleton, raw).values()))
        with np.errstate(over='ignore', invalid='ignore'):
            self.assert_rig_error('RIG_NONFINITE_DEFORMATION',
                                  lambda: evaluate_deformation(self.rig, raw))
            self.assert_usable(evaluate_animation(self.rig, clip, .5)['pose'])

        recipes = {'static-neutral': 'neutral', 'static-positive': clip['id']}
        for action in ('evaluate', 'cancel'):
            with self.subTest(action=action):
                runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                          selected=list(recipes), blend_seconds=.1)
                runtime.request('static-positive', 0.)
                self.assert_usable(runtime.evaluate(.125)['pose'])
                self.assert_usable(runtime._input_pose)
                self.assertIsNotNone(runtime._source_input_pose)
                with np.errstate(over='ignore', invalid='ignore'):
                    self.assert_atomic_rejection(runtime, lambda: getattr(runtime, action)(.5),
                                                 'RIG_NONFINITE_DEFORMATION')

    def test_future_fk_overflow_refuses_only_its_actual_step_endpoint(self):
        self.rig.manifest['skeleton'] = chain_skeleton(40)
        self.rig.weights = generate_skin_weights(self.rig.geometry, self.rig.skeleton)
        scales = [self.track('scale', target=bone['id'], interpolation='STEP')
                  for bone in self.rig.skeleton['bones']]
        for track in scales:
            track['values'][1] = [4e8] * 3
        root = self.track('translation', target='b0', interpolation='LINEAR')
        root['values'] = [[0., 0., 0.], [.5, 0., 0.]]
        for explicit_root in (False, True):
            with self.subTest(explicit_root=explicit_root):
                tracks = scales + ([root] if explicit_root else [])
                clip = self.clip('bad-terminal-fk', tracks)
                clip['loop'] = False
                runtime = RigStateRuntime(self.rig, 'static-positive',
                                          recipes={'static-positive': clip['id']},
                                          selected=['static-positive'], blend_seconds=0.)
                with np.errstate(over='ignore', invalid='ignore'):
                    self.assert_rig_error('RIG_NONFINITE',
                                          lambda: evaluate_animation(self.rig, clip, 1.))
                    for time in (0., .5):
                        expected = evaluate_animation(self.rig, clip, time)['pose']
                        self.assert_usable(expected)
                        try:
                            actual = runtime.evaluate(time)['pose']
                        except RigError as exc:
                            self.fail(f'Finite sample at {time}s refused: {exc.report["code"]}')
                        actual_fk = evaluate_fk(self.rig.skeleton, actual)
                        actual_skin = evaluate_deformation(self.rig, actual)
                        for name, matrix in evaluate_fk(self.rig.skeleton, expected).items():
                            np.testing.assert_array_equal(actual_fk[name], matrix, err_msg=name)
                        for name, mesh in evaluate_deformation(self.rig, expected).items():
                            np.testing.assert_array_equal(actual_skin[name]['positions'], mesh['positions'],
                                                          err_msg=name)
                    self.assert_atomic_rejection(runtime, lambda: runtime.evaluate(1.), 'RIG_NONFINITE')


if __name__ == '__main__':
    unittest.main()
