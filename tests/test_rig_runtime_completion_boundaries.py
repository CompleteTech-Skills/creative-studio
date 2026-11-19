"""Independent endpoint and large-direction oracles on synthetic native rigs.

Authored against 4fb21a90. No sampler, control, FK, or skin owner is mocked.
Completion is a discrete endpoint operation even when subtraction of the
absolute boundary would round to an interior local time.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from rig_animation import clip_from_poses, evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_errors import RigError
from rig_math import axis_angle, qbetween, qmatrix, unit
from rig_models import SkinWeights, validate_geometry
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation, validate_skin
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture
from test_rig_math import chain_skeleton


class RigRuntimeCompletionBoundaryTests(unittest.TestCase):
    STATE_FIELDS = ('state', 'entered', 'time', 'source_pose', '_input_pose',
                    '_source_input_pose', '_source_contact_correction',
                    'root_offset', 'transition_count')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rig = fixture(temporary.name)

    def step_clip(self, name, duration, tracks):
        clip = deepcopy(self.rig.clips['neutral'])
        clip.update(id=name, duration=duration, loop=False, tracks=tracks,
                    contacts=[], events=[])
        validate_contract(clip, 'AnimationClip')
        validate_animation(self.rig, clip)
        self.rig.clips[name] = clip
        return clip

    @staticmethod
    def track(bone, path, duration, begin, end):
        return dict(schema_version=1, target=bone, path=path,
                    interpolation='STEP', times=[0., duration], values=[begin, end])

    def runtime(self, outgoing, start, blend):
        recipes = {'notification-safe': outgoing, 'idle': 'neutral'}
        return RigStateRuntime(self.rig, 'notification-safe', recipes=recipes,
                               selected=list(recipes), now=start, blend_seconds=blend)

    def assert_geometry(self, actual, expected):
        validate_contract(actual, 'Pose')
        actual_fk, expected_fk = (evaluate_fk(self.rig.skeleton, pose)
                                  for pose in (actual, expected))
        for name, matrix in actual_fk.items():
            self.assertTrue(np.isfinite(matrix).all(), name)
            np.testing.assert_allclose(matrix, expected_fk[name], rtol=0, atol=1e-11,
                                       err_msg=name)
        actual_skin, expected_skin = (evaluate_deformation(self.rig, pose)
                                      for pose in (actual, expected))
        for name, mesh in actual_skin.items():
            self.assertTrue(np.isfinite(mesh['positions']).all(), name)
            np.testing.assert_allclose(mesh['positions'], expected_skin[name]['positions'],
                                       rtol=0, atol=1e-11, err_msg=name)

    def test_translation_completion_at_nonzero_entry_captures_exact_step_endpoint(self):
        clip = self.step_clip('step-travel', .2, [
            self.track('b0', 'translation', .2, [0., 0., 0.], [1., 0., 0.])])
        boundary = .15 + .2
        self.assertLess(boundary - .15, clip['duration'])  # Cancellation witness.
        endpoint = evaluate_animation(self.rig, clip, clip['duration'])['pose']
        interior = evaluate_animation(self.rig, clip, math.nextafter(.2, 0.))['pose']
        for blend in (0., .05):
            with self.subTest(blend=blend):
                runtime = self.runtime(clip['id'], .15, blend)
                before = runtime.evaluate(math.nextafter(boundary, -math.inf))
                self.assertEqual(before['state'], 'notification-safe')
                self.assert_geometry(before['pose'], interior)
                at = runtime.evaluate(boundary)
                self.assertEqual(at['state'], 'idle')
                self.assertEqual(at['elapsed'], 0.)
                self.assert_geometry(runtime.source_pose, endpoint)
                self.assert_geometry(runtime._source_input_pose, endpoint)
                self.assert_geometry(at['pose'], endpoint)
                for time in (math.nextafter(boundary, math.inf), boundary + .025,
                             boundary + .05):
                    self.assert_geometry(runtime.evaluate(time)['pose'], endpoint)

    def test_rotation_completion_retains_endpoint_for_nonzero_crossfade(self):
        clip = self.step_clip('step-turn', .1, [
            self.track('b1', 'rotation', .1, [0., 0., 0., 1.],
                       axis_angle([0., 0., 1.], 1.).tolist())])
        boundary = 4. + .1
        self.assertLess(boundary - 4., clip['duration'])
        endpoint = evaluate_animation(self.rig, clip, .1)['pose']
        interior = evaluate_animation(self.rig, clip, math.nextafter(.1, 0.))['pose']
        for blend in (0., .05):
            with self.subTest(blend=blend):
                runtime = self.runtime(clip['id'], 4., blend)
                before = runtime.evaluate(math.nextafter(boundary, -math.inf))
                self.assertEqual(before['state'], 'notification-safe')
                self.assert_geometry(before['pose'], interior)
                at = runtime.evaluate(boundary)
                self.assertEqual(at['state'], 'idle')
                self.assert_geometry(runtime.source_pose, endpoint)
                self.assert_geometry(runtime._source_input_pose, endpoint)
                expected = endpoint if blend else neutral_pose(self.rig.skeleton)
                self.assert_geometry(at['pose'], expected)
                for time in (math.nextafter(boundary, math.inf), boundary + .0125,
                             boundary + .05, boundary + .075):
                    fraction = min((time - boundary) / blend, 1.) if blend else 1.
                    expected = neutral_pose(self.rig.skeleton)
                    expected['bones']['b1'] = {
                        'rotation': axis_angle([0., 0., 1.], 1. - fraction).tolist()}
                    self.assert_geometry(runtime.evaluate(time)['pose'], expected)

    def test_multiclip_completion_uses_final_clip_endpoint_and_stitched_root(self):
        first = self.step_clip('first-step', .1, [
            self.track('b0', 'translation', .1, [0., 0., 0.], [.4, 0., 0.])])
        last = self.step_clip('last-step', .1, [
            self.track('b0', 'translation', .1, [10., 0., 0.], [11., 0., 0.]),
            self.track('b1', 'rotation', .1, [0., 0., 0., 1.],
                       axis_angle([0., 0., 1.], 1.).tolist())])
        endpoint = evaluate_animation(self.rig, last, .1)['pose']
        endpoint['bones']['b0']['translation'] = [1.4, 0., 0.]
        interior = evaluate_animation(self.rig, last, math.nextafter(.1, 0.))['pose']
        interior['bones']['b0']['translation'] = [.4, 0., 0.]
        boundary = .15 + (first['duration'] + last['duration'])
        for blend in (0., .05):
            with self.subTest(blend=blend):
                runtime = self.runtime([first['id'], last['id']], .15, blend)
                before = runtime.evaluate(math.nextafter(boundary, -math.inf))
                self.assertEqual(before['state'], 'notification-safe')
                self.assert_geometry(before['pose'], interior)
                at = runtime.evaluate(boundary)
                self.assertEqual(at['state'], 'idle')
                self.assert_geometry(runtime.source_pose, endpoint)
                self.assert_geometry(runtime._source_input_pose, endpoint)
                expected = deepcopy(endpoint) if blend else neutral_pose(self.rig.skeleton)
                expected['bones'].setdefault('b0', {})['translation'] = [1.4, 0., 0.]
                self.assert_geometry(at['pose'], expected)
                time = boundary + .0125
                fraction = (time - boundary) / blend if blend else 1.
                expected = neutral_pose(self.rig.skeleton)
                expected['bones']['b0'] = {'translation': [1.4, 0., 0.]}
                expected['bones']['b1'] = {
                    'rotation': axis_angle([0., 0., 1.], 1. - fraction).tolist()}
                self.assert_geometry(runtime.evaluate(time)['pose'], expected)

    def test_predecessor_is_interior_when_boundary_subtraction_rounds_up(self):
        # A correction must select exact completion explicitly, not snap nearby
        # local samples with an epsilon (or indiscriminately add one ULP).
        clip = self.step_clip('no-early-step', .2, [
            self.track('b1', 'rotation', .2, [0., 0., 0., 1.],
                       axis_angle([0., 0., 1.], 1.).tolist())])
        runtime = self.runtime(clip['id'], .1, .05)
        boundary = .1 + .2
        before = runtime.evaluate(math.nextafter(boundary, -math.inf))
        self.assertEqual(before['state'], 'notification-safe')
        self.assert_geometry(before['pose'], neutral_pose(self.rig.skeleton))
        at = runtime.evaluate(boundary)
        self.assertEqual(at['state'], 'idle')
        self.assert_geometry(runtime.source_pose, evaluate_animation(self.rig, clip, .2)['pose'])

    def test_internal_clip_boundary_is_half_open_at_nonzero_entry(self):
        mesh = self.rig.geometry.meshes[0]
        first = self.step_clip('morph-off', .2, [
            self.track(mesh.id, 'weights', .2, [0.], [0.])])
        second = self.step_clip('morph-on', .2, [
            self.track(mesh.id, 'weights', .2, [1.], [1.])])
        for looping in (False, True):
            with self.subTest(looping=looping):
                second['loop'] = looping
                state = 'static-positive' if looping else 'notification-safe'
                recipes = {state: [first['id'], second['id']], 'idle': 'neutral'}
                runtime = RigStateRuntime(self.rig, state, recipes=recipes,
                                          selected=list(recipes), now=.15, blend_seconds=0.)
                boundary = .15 + .2
                for time, clip, amount in (
                        (math.nextafter(boundary, -math.inf), first, 0.),
                        (boundary, second, 1.),
                        (math.nextafter(boundary, math.inf), second, 1.)):
                    with self.subTest(time=time):
                        result = runtime.evaluate(time)
                        self.assertEqual(result['state'], state)
                        self.assertEqual(result['pose']['morphs'][mesh.id]['blink'], amount)
                        self.assert_geometry(result['pose'], evaluate_animation(self.rig, clip, 0.)['pose'])
                        json.dumps(result, allow_nan=False)

    def test_unavailable_completion_holds_composed_boundary_pose_during_long_blend(self):
        mesh = self.rig.geometry.meshes[0]
        clip = self.step_clip('short-morph', .125, [
            self.track(mesh.id, 'weights', .125, [1.], [1.])])
        recipes = {'sad': 'neutral', 'startled': clip['id']}
        runtime = RigStateRuntime(self.rig, 'sad', recipes=recipes,
                                  selected=list(recipes), blend_seconds=.5)
        source = runtime.evaluate(0.)['pose']
        self.assert_geometry(runtime.request('startled', 0.)['pose'], source)
        before_time = math.nextafter(.125, -math.inf)
        before = runtime.evaluate(before_time)
        self.assertEqual(before['status'], 'playing')
        self.assertAlmostEqual(before['pose']['morphs'][mesh.id]['blink'], before_time / .5)
        at = runtime.evaluate(.125)
        expected = neutral_pose(self.rig.skeleton)
        expected['morphs'][mesh.id] = {'blink': .25}
        self.assert_geometry(at['pose'], expected)
        for time in (.125, math.nextafter(.125, math.inf), .25, .5, 1.):
            with self.subTest(time=time):
                result = runtime.evaluate(time)
                self.assertEqual(result['state'], 'startled')
                self.assertEqual(result['status'], 'completion-unavailable')
                self.assertEqual(result['pending_completion'], 'idle')
                self.assertEqual(result['pose']['morphs'][mesh.id]['blink'], .25)
                self.assert_geometry(result['pose'], at['pose'])
                json.dumps(result, allow_nan=False)

    def test_final_contact_convergence_uses_final_geometry_after_all_native_solves(self):
        goal = [.5, 1.5, .2]
        original_chain = self.rig.manifest['controllers']['ik'][0]
        first = dict(deepcopy(original_chain), id='first-arm', solver='fabrik',
                     iterations=1, tolerance=1e-7)
        second = dict(deepcopy(original_chain), id='second-arm', solver='two-bone',
                      tolerance=1e-7)
        self.rig.manifest['controllers']['ik'] = [first, second]
        clip = deepcopy(self.rig.clips['neutral'])
        clip.update(id='shared-goal', contacts=[
            dict(schema_version=1, id='first-contact', chain=first['id'],
                 start=0., end=1., target=goal, kind='hand'),
            dict(schema_version=1, id='second-contact', chain=second['id'],
                 start=0., end=1., target=goal, kind='hand')])
        validate_animation(self.rig, clip)
        self.rig.clips[clip['id']] = clip
        native = evaluate_animation(self.rig, clip, .5)
        runtime = RigStateRuntime(self.rig, 'static-positive',
                                  recipes={'static-positive': clip['id']},
                                  selected=['static-positive'], blend_seconds=0.)
        result = runtime.evaluate(.5)
        self.assert_geometry(result['pose'], native['pose'])
        self.assertEqual(result['solve_contacts'], native['contacts'])
        self.assertFalse(native['contacts'][0]['converged'])
        self.assertGreater(native['contacts'][0]['residual_m'], first['tolerance'])
        self.assertTrue(native['contacts'][1]['converged'])
        endpoint = evaluate_fk(self.rig.skeleton, result['pose'])['b2'][:3, 3]
        measured = math.dist(endpoint, goal)
        self.assertLess(measured, first['tolerance'])
        json.dumps(native, allow_nan=False)
        json.dumps(result, allow_nan=False)
        for owner, reports in (('native', native['final_contacts']),
                               ('runtime', result['contacts'])):
            for report in reports:
                with self.subTest(owner=owner, chain=report['chain']):
                    self.assertAlmostEqual(report['residual_m'], measured, places=14)
                    self.assertTrue(report['reachable'])
                    self.assertTrue(report['converged'])
        self.assertEqual(result['status'], 'playing')

    def test_invalid_exact_endpoint_rejects_skin_overflow_and_restores_all_state(self):
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
        np.testing.assert_allclose(np.abs(mesh.positions[:, 0]), 1e7)
        clip = self.step_clip('endpoint-overflow', .2, [
            self.track(bone['id'], 'scale', .2, [1.] * 3, [2.5e8] * 3)
            for bone in self.rig.skeleton['bones']])
        endpoint = evaluate_animation(self.rig, clip, .2)['pose']
        self.assertTrue(all(np.isfinite(matrix).all()
                            for matrix in evaluate_fk(self.rig.skeleton, endpoint).values()))
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as native:
            evaluate_deformation(self.rig, endpoint)
        self.assertEqual(native.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')

        recipes = {'static-neutral': 'neutral', 'notification-safe': clip['id'], 'idle': 'neutral'}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), now=.1, blend_seconds=.05)
        runtime.request('notification-safe', .15)
        before = runtime.evaluate(.25)
        self.assertIsNotNone(runtime.source_pose)
        self.assertIsNotNone(runtime._source_input_pose)
        saved = deepcopy({name: getattr(runtime, name) for name in self.STATE_FIELDS})
        documents = deepcopy((self.rig.manifest, self.rig.clips, recipes))
        sources = {item['path']: (Path(self.rig.root) / item['path']).read_bytes()
                   for item in self.rig.manifest['source']['dependencies']}
        positions = mesh.positions.copy()
        weights = (skin.indices.copy(), skin.weights.copy(), skin.inverse_bind.copy())
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as failure:
            runtime.evaluate(.15 + .2)
        self.assertEqual(failure.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
        json.dumps(failure.exception.report, allow_nan=False)
        for name, expected in saved.items():
            if name == 'root_offset':
                np.testing.assert_array_equal(getattr(runtime, name), expected)
            else:
                self.assertEqual(getattr(runtime, name), expected, name)
        self.assertEqual((self.rig.manifest, self.rig.clips, recipes), documents)
        for path, contents in sources.items():
            self.assertEqual((Path(self.rig.root) / path).read_bytes(), contents)
        np.testing.assert_array_equal(mesh.positions, positions)
        for actual, expected in zip((skin.indices, skin.weights, skin.inverse_bind), weights):
            np.testing.assert_array_equal(actual, expected)
        self.assert_geometry(runtime.evaluate(saved['time'])['pose'], before['pose'])

    def test_native_world_aim_survives_large_finite_loop_displacement(self):
        self.rig.manifest['controllers']['constraints'] = [dict(
            schema_version=1, id='root-world-aim', bone='b0', type='aim',
            axis=[0., 1., 0.], point=[0., 2., 0.], influence=1.)]
        poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
        poses[1]['bones']['b0'] = {'translation': [1., 0., 0.]}
        self.rig.clips['travel'] = clip_from_poses(
            self.rig, 'travel', [0., 1.], poses, loop=True, root_motion='embedded')
        for time in (.5, 1e152, 1e155):
            with self.subTest(time=time):
                runtime = RigStateRuntime(self.rig, 'static-positive',
                                          recipes={'static-positive': 'travel'},
                                          selected=['static-positive'], blend_seconds=0.)
                with np.errstate(over='ignore', invalid='ignore'):
                    result = runtime.evaluate(time)
                self.assertEqual(result['status'], 'playing')
                world = evaluate_fk(self.rig.skeleton, result['pose'])['b0']
                direction = np.array([0., 2., 0.]) - world[:3, 3]
                expected = direction / math.hypot(*direction)
                actual = world[:3, :3] @ np.array([0., 1., 0.])
                actual /= math.hypot(*actual)
                self.assertAlmostEqual(world[0, 3] / time, 1.)
                for matrix in evaluate_fk(self.rig.skeleton, result['pose']).values():
                    self.assertTrue(np.isfinite(matrix).all())
                for deformed in evaluate_deformation(self.rig, result['pose']).values():
                    self.assertTrue(np.isfinite(deformed['positions']).all())
                json.dumps(result, allow_nan=False)
                np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-12)

    def test_loop_cycle_interior_matches_native_without_terminal_step_flash(self):
        mesh = self.rig.geometry.meshes[0]
        clip = self.step_clip('terminal-only-morph', .3, [
            self.track(mesh.id, 'weights', .3, [0.], [1.])])
        clip.update(loop=True, root_motion='in-place')
        validate_animation(self.rig, clip)
        runtime = RigStateRuntime(self.rig, 'static-positive',
                                  recipes={'static-positive': clip['id']},
                                  selected=['static-positive'], now=.2, blend_seconds=0.)
        for time in (math.nextafter(2.3, -math.inf), 2.3,
                     math.nextafter(2.3, math.inf)):
            with self.subTest(time=time):
                native = evaluate_animation(self.rig, clip, time - .2, loop=True)
                self.assertEqual(native['pose']['morphs'][mesh.id]['blink'], 0.)
                result = runtime.evaluate(time)
                self.assertEqual(result['state'], 'static-positive')
                self.assertEqual(result['status'], 'playing')
                json.dumps(result, allow_nan=False)
                self.assert_geometry(result['pose'], native['pose'])
                self.assertEqual(result['pose']['morphs'][mesh.id]['blink'], 0.)

        # Preserve the ordinary half-open internal-boundary rule while fixing
        # the cycle interior: the next authored clip starts at its boundary.
        first = self.step_clip('ordinary-off', .2, [
            self.track(mesh.id, 'weights', .2, [0.], [0.])])
        second = self.step_clip('ordinary-on', .2, [
            self.track(mesh.id, 'weights', .2, [1.], [1.])])
        ordinary = self.runtime([first['id'], second['id']], .15, 0.)
        boundary = .15 + .2
        for time, selected in ((math.nextafter(boundary, -math.inf), first),
                               (boundary, second),
                               (math.nextafter(boundary, math.inf), second)):
            with self.subTest(ordinary_time=time):
                result = ordinary.evaluate(time)
                self.assert_geometry(result['pose'],
                                     evaluate_animation(self.rig, selected, 0.)['pose'])

    def test_nonlooping_state_without_completion_finishes_long_transition_fade(self):
        mesh = self.rig.geometry.meshes[0]
        clip = self.step_clip('short-no-completion', .125, [
            self.track(mesh.id, 'weights', .125, [1.], [1.])])
        recipes = {'static-neutral': 'neutral', 'static-positive': clip['id']}
        runtime = RigStateRuntime(self.rig, 'static-neutral', recipes=recipes,
                                  selected=list(recipes), blend_seconds=.5)
        source = runtime.evaluate(0.)['pose']
        self.assert_geometry(runtime.request('static-positive', 0.)['pose'], source)
        endpoint = evaluate_animation(self.rig, clip, .125)['pose']
        self.assertEqual(endpoint['morphs'][mesh.id]['blink'], 1.)
        for time, amount in ((.125, .25), (.25, .5), (.5, 1.), (2., 1.)):
            with self.subTest(time=time):
                result = runtime.evaluate(time)
                expected = deepcopy(endpoint)
                expected['morphs'][mesh.id]['blink'] = amount
                self.assertEqual(result['state'], 'static-positive')
                self.assertEqual(result['status'], 'playing')
                self.assertIsNone(result['pending_completion'])
                json.dumps(result, allow_nan=False)
                self.assert_geometry(result['pose'], expected)
                self.assertEqual(result['pose']['morphs'][mesh.id]['blink'], amount)

    def test_accurate_total_captures_tiny_final_clip_and_validates_its_endpoint(self):
        mesh = self.rig.geometry.meshes[0]
        durations = [.1, .2, .3, 1e-17]
        boundary = math.fsum(durations)
        self.assertEqual(boundary, .6)
        self.assertEqual(sum(durations), boundary)
        self.assertGreater(((durations[0] + durations[1]) + durations[2]), boundary)
        clips = [self.step_clip('accurate-end-' + str(index), duration, [
            self.track(mesh.id, 'weights', duration, [float(index == 3)],
                       [float(index == 3)])])
                 for index, duration in enumerate(durations)]
        with self.subTest(endpoint='usable'):
            runtime = self.runtime([clip['id'] for clip in clips], 0., .05)
            before = runtime.evaluate(math.nextafter(boundary, -math.inf))
            self.assertEqual(before['state'], 'notification-safe')
            self.assert_geometry(before['pose'], evaluate_animation(self.rig, clips[2], 0.)['pose'])
            endpoint = evaluate_animation(self.rig, clips[-1], durations[-1])['pose']
            at = runtime.evaluate(boundary)
            self.assertEqual(at['state'], 'idle')
            self.assertEqual(at['elapsed'], 0.)
            self.assert_geometry(runtime.source_pose, endpoint)
            self.assert_geometry(runtime._source_input_pose, endpoint)
            self.assert_geometry(at['pose'], endpoint)
            json.dumps(at, allow_nan=False)

        # The tiny final interval has no representable interior at this
        # absolute time, but its exact endpoint still requires native admission.
        self.rig.manifest['skeleton'] = chain_skeleton(36)
        mesh.positions[:, 0] *= 1e8
        rest = evaluate_fk(self.rig.skeleton)
        count = len(mesh.positions)
        skin = SkinWeights(mesh.id, ['b35'], np.zeros((count, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (count, 1)),
                           np.array([np.linalg.inv(rest['b35'])]))
        self.rig.weights = {mesh.id: skin}
        validate_geometry(self.rig.geometry)
        validate_skin(skin, mesh, self.rig.skeleton)
        invalid = self.step_clip('tiny-invalid-endpoint', durations[-1], [
            self.track(bone['id'], 'scale', durations[-1], [1.] * 3, [2.5e8] * 3)
            for bone in self.rig.skeleton['bones']])
        endpoint = evaluate_animation(self.rig, invalid, durations[-1])['pose']
        self.assertTrue(all(np.isfinite(matrix).all()
                            for matrix in evaluate_fk(self.rig.skeleton, endpoint).values()))
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as native:
            evaluate_deformation(self.rig, endpoint)
        self.assertEqual(native.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
        runtime = self.runtime([clip['id'] for clip in clips[:-1]] + [invalid['id']], 0., .05)
        before = runtime.evaluate(.5)
        saved = deepcopy({name: getattr(runtime, name) for name in self.STATE_FIELDS})
        documents = deepcopy((self.rig.manifest, self.rig.clips))
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as failure:
            runtime.evaluate(boundary)
        self.assertEqual(failure.exception.report['code'], 'RIG_NONFINITE_DEFORMATION')
        json.dumps(failure.exception.report, allow_nan=False)
        for name, expected in saved.items():
            if name == 'root_offset':
                np.testing.assert_array_equal(getattr(runtime, name), expected)
            else:
                self.assertEqual(getattr(runtime, name), expected, name)
        self.assertEqual((self.rig.manifest, self.rig.clips), documents)
        self.assert_geometry(runtime.evaluate(saved['time'])['pose'], before['pose'])

    def test_accurate_prefix_selects_fourth_clip_at_exact_internal_boundary(self):
        mesh = self.rig.geometry.meshes[0]
        durations = [.1, .2, .3, .4]
        boundary = math.fsum(durations[:3])
        self.assertEqual(boundary, .6)
        clips = [self.step_clip('accurate-prefix-' + str(index), duration, [
            self.track(mesh.id, 'weights', duration, [float(index == 3)],
                       [float(index == 3)])])
                 for index, duration in enumerate(durations)]
        for looping in (False, True):
            with self.subTest(looping=looping):
                clips[-1]['loop'] = looping
                state = 'static-positive' if looping else 'notification-safe'
                recipes = {state: [clip['id'] for clip in clips], 'idle': 'neutral'}
                runtime = RigStateRuntime(self.rig, state, recipes=recipes,
                                          selected=list(recipes), blend_seconds=0.)
                for time, selected, amount in (
                        (math.nextafter(boundary, -math.inf), clips[2], 0.),
                        (boundary, clips[3], 1.),
                        (math.nextafter(boundary, math.inf), clips[3], 1.)):
                    with self.subTest(time=time):
                        result = runtime.evaluate(time)
                        self.assertEqual(result['state'], state)
                        self.assert_geometry(result['pose'],
                                             evaluate_animation(self.rig, selected, 0.)['pose'])
                        self.assertEqual(result['pose']['morphs'][mesh.id]['blink'], amount)
                        json.dumps(result, allow_nan=False)

    def test_rounding_blend_end_does_not_advance_embedded_loop_clock(self):
        start_pose = neutral_pose(self.rig.skeleton)
        end_pose = neutral_pose(self.rig.skeleton)
        end_pose['bones']['b0'] = {'translation': [1., 0., 0.]}
        clip = clip_from_poses(self.rig, 'short-travel', [0., 1. / 64.],
                               [start_pose, end_pose], loop=True, root_motion='embedded')
        self.rig.clips[clip['id']] = clip
        recipes = {'static-neutral': 'neutral', 'static-positive': clip['id']}
        entry = float(2 ** 50)
        now = entry + .3
        self.assertEqual(now - entry, .25)
        expected = evaluate_animation(self.rig, clip, now - entry, loop=True)['pose']
        self.assertEqual(evaluate_fk(self.rig.skeleton, expected)['b0'][0, 3], 16.)
        for initial in ('static-positive', 'static-neutral'):
            for blend in (0., .3):
                with self.subTest(initial=initial, blend=blend):
                    runtime = RigStateRuntime(self.rig, initial, recipes=recipes,
                                              selected=list(recipes), now=entry,
                                              blend_seconds=blend)
                    if initial == 'static-neutral':
                        runtime.request('static-positive', entry)
                        self.assertIsNotNone(runtime.source_pose)
                    else:
                        self.assertIsNone(runtime.source_pose)
                    result = runtime.evaluate(now)
                    self.assertEqual(result['state'], 'static-positive')
                    self.assertEqual(result['elapsed'], .25)
                    self.assert_geometry(result['pose'], expected)

    def test_native_unit_and_between_keep_large_finite_directions(self):
        for direction in ([-3., 4., 0.], [-3e155, 4e155, 0.],
                          [-1e308, 1e308, 0.]):
            with self.subTest(direction=direction):
                # Scale before hypot so even an unrepresentable length is not
                # confused with an unrepresentable unit direction.
                scaled = np.array(direction) / max(abs(value) for value in direction)
                expected = scaled / math.hypot(*scaled)
                with np.errstate(over='ignore', invalid='ignore'):
                    actual_unit = unit(direction)
                    rotation = qbetween([0., 1., 0.], direction)
                np.testing.assert_allclose(actual_unit, expected, rtol=0, atol=1e-14)
                np.testing.assert_allclose(qmatrix(rotation) @ [0., 1., 0.], expected,
                                           rtol=0, atol=1e-14)


if __name__ == '__main__':
    unittest.main()
