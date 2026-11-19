"""Independent native fixtures for large IK lengths and unavailable contact metrics.

These are synthetic engineering geometry tests; they exercise native FK, skin,
contacts and runtime playback without claiming visual or production approval.
"""
from copy import deepcopy
import json
import math
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_errors import RigError
from rig_ik import solve_ik, validate_controllers
from rig_models import SkinWeights, validate_geometry
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import chain_skeleton, fixture
from rig_validation import contact_metrics, validate_rig


def step(bone, path, times, values):
    return dict(schema_version=1, target=bone, path=path, interpolation='STEP',
                times=times, values=values)


class RigContactNumericBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)
        self.mesh = self.rig.geometry.meshes[0]
        self.neutral = deepcopy(self.rig.clips['neutral'])

    def bind_root(self, count):
        self.rig.manifest['skeleton'] = chain_skeleton(count)
        validate_skeleton(self.rig.skeleton)
        validate_geometry(self.rig.geometry)
        vertices = len(self.mesh.positions)
        skin = SkinWeights(self.mesh.id, ['b0'], np.zeros((vertices, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (vertices, 1)), np.eye(4)[None])
        validate_skin(skin, self.mesh, self.rig.skeleton)
        self.rig.weights = {self.mesh.id: skin}

    def install_clip(self, tracks, contacts, name):
        clip = dict(deepcopy(self.neutral), id=name, tracks=tracks, contacts=contacts)
        validate_controllers(self.rig.skeleton, self.rig.manifest['controllers'])
        validate_animation(self.rig, clip)
        validate_contract(clip, 'AnimationClip')
        self.rig.clips = {name: clip}
        return clip

    def large_chain(self, solver='two-bone'):
        # All authored local values obey the native bounds. Seventeen parent
        # scales contribute 2**493; local segments 2**25 become exactly 2**518.
        self.bind_root(20)
        names = ['b17', 'b18', 'b19']
        chain = self.rig.manifest['controllers']['ik'][0]
        chain.update(bones=names, solver=solver)
        tracks = [step(f'b{i}', 'scale', [0., .5], [[1.] * 3, [2.**29] * 3])
                  for i in range(17)]
        tracks += [step(f'b{i}', 'translation', [0., .5],
                        [[0., float(i > 0), 0.], [0., 0., 0.]]) for i in range(17)]
        tracks += [step('b17', 'translation', [0., .5], [[0., 1., 0.], [0., -2.**26, 0.]]),
                   step('b18', 'translation', [0., .5], [[0., 1., 0.], [0., 2.**25, 0.]]),
                   step('b19', 'translation', [0., .5], [[0., 1., 0.], [0., 2.**25, 0.]])]
        contact = dict(schema_version=1, id='match', chain='arm', start=.5, end=1.,
                       target=[0., 0., 0.], kind='hand')
        clip = self.install_clip(tracks, [contact], 'large-chain')
        pose = evaluate_animation(self.rig, clip, .5, contacts=False)['pose']
        world = self.assert_geometry(pose)
        expected = [[0., -2.**519, 0.], [0., -2.**518, 0.], [0., 0., 0.]]
        np.testing.assert_array_equal([world[name][:3, 3] for name in names], expected)
        self.assertEqual([math.dist(world[a][:3, 3], world[b][:3, 3])
                          for a, b in zip(names, names[1:])], [2.**518, 2.**518])
        self.assertEqual(math.dist(world[names[-1]][:3, 3], contact['target']), 0.)
        return clip, pose, chain

    def assert_geometry(self, pose):
        validate_contract(pose, 'Pose')
        world = evaluate_fk(self.rig.skeleton, pose)
        for matrix in world.values():
            self.assertTrue(np.isfinite(matrix).all())
        deformed = evaluate_deformation(self.rig, pose)
        for mesh in self.rig.geometry.meshes:
            self.assertTrue(np.isfinite(deformed[mesh.id]['positions']).all())
        return world

    def assert_matched_large_chain(self, pose):
        world = self.assert_geometry(pose)
        np.testing.assert_array_equal(world['b19'][:3, 3], [0., 0., 0.])
        self.assertEqual([math.dist(world[a][:3, 3], world[b][:3, 3])
                          for a, b in (('b17', 'b18'), ('b18', 'b19'))], [2.**518] * 2)
        actual = evaluate_deformation(self.rig, pose)[self.mesh.id]['positions']
        np.testing.assert_array_equal(actual, self.mesh.positions * 2.**29)

    def assert_contact(self, row):
        self.assertTrue(row['reachable'])
        self.assertTrue(row['converged'])
        self.assertEqual(row['residual_m'], 0.)
        json.dumps(row, allow_nan=False)

    def assert_native_large(self, frame):
        json.dumps(frame, allow_nan=False)
        self.assert_matched_large_chain(frame['pose'])
        for key in ('contacts', 'final_contacts'):
            self.assertEqual(len(frame[key]), 1)
            self.assert_contact(frame[key][0])

    def test_two_bone_preserves_already_satisfied_large_segments(self):
        _, pose, chain = self.large_chain()
        with np.errstate(over='ignore', invalid='ignore'):
            solved = solve_ik(self.rig.skeleton, pose, chain, [0., 0., 0.])
        self.assert_contact(solved.report())
        self.assert_matched_large_chain(solved.pose)

    def test_native_animation_accepts_already_satisfied_large_segments(self):
        clip, _, _ = self.large_chain()
        with np.errstate(over='ignore', invalid='ignore'):
            frame = evaluate_animation(self.rig, clip, .5)
        self.assert_native_large(frame)

    def test_runtime_advances_into_usable_large_segment_contact(self):
        clip, _, _ = self.large_chain()
        runtime = RigStateRuntime(self.rig, 'static-positive',
                                  recipes={'static-positive': clip['id']},
                                  selected=['static-positive'], blend_seconds=0)
        before = runtime.evaluate(.25)
        self.assertEqual(before['status'], 'playing')
        self.assert_geometry(before['pose'])
        json.dumps(before, allow_nan=False)
        with np.errstate(over='ignore', invalid='ignore'):
            frame = runtime.evaluate(.5)
        self.assertEqual((frame['status'], runtime.time), ('playing', .5))
        self.assert_matched_large_chain(frame['pose'])
        self.assertEqual(len(frame['contacts']), 1)
        self.assert_contact(frame['contacts'][0])
        json.dumps(frame, allow_nan=False)

    def test_fabrik_large_segment_native_and_runtime_control(self):
        clip, pose, chain = self.large_chain('fabrik')
        with np.errstate(over='ignore', invalid='ignore'):
            solved = solve_ik(self.rig.skeleton, pose, chain, [0., 0., 0.])
            self.assert_contact(solved.report())
            self.assert_matched_large_chain(solved.pose)
            self.assert_native_large(evaluate_animation(self.rig, clip, .5))
            runtime = RigStateRuntime(self.rig, 'static-positive',
                                      recipes={'static-positive': clip['id']},
                                      selected=['static-positive'], blend_seconds=0)
            self.assertEqual(runtime.evaluate(.25)['status'], 'playing')
            frame = runtime.evaluate(.5)
        self.assertEqual(runtime.time, .5)
        self.assert_matched_large_chain(frame['pose'])
        self.assert_contact(frame['contacts'][0])
        json.dumps(frame, allow_nan=False)

    def test_ordinary_two_bone_bends_and_retains_real_segment_lengths(self):
        chain = self.rig.manifest['controllers']['ik'][0]
        pose = evaluate_animation(self.rig, self.neutral, .25, contacts=False)['pose']
        solved = solve_ik(self.rig.skeleton, pose, chain, [0., 1.5, 0.])
        world = self.assert_geometry(solved.pose)
        self.assertTrue(solved.reachable)
        self.assertTrue(solved.converged)
        np.testing.assert_allclose(world['b2'][:3, 3], [0., 1.5, 0.], rtol=0, atol=1e-12)
        self.assertGreater(world['b1'][2, 3], .5)
        for a, b in (('b0', 'b1'), ('b1', 'b2')):
            self.assertAlmostEqual(math.dist(world[a][:3, 3], world[b][:3, 3]), 1., places=12)
        json.dumps(solved.report(), allow_nan=False)

    def controlled_pose(self, tracks, target):
        contact = dict(schema_version=1, id='hand', chain='arm', start=.5, end=1.,
                       target=target, kind='hand')
        clip = self.install_clip(tracks, [contact], 'two-bone-control')
        seed = evaluate_animation(self.rig, clip, .5, contacts=False)['pose']
        chain = self.rig.manifest['controllers']['ik'][0]
        solved = solve_ik(self.rig.skeleton, seed, chain, target)
        yield solved.pose, [solved.report()], None
        native = evaluate_animation(self.rig, clip, .5)
        yield native['pose'], native['contacts'] + native['final_contacts'], None
        state = 'static-positive'
        runtime = RigStateRuntime(self.rig, state, recipes={state: clip['id']},
                                  selected=[state], blend_seconds=0.)
        self.assertEqual(runtime.evaluate(.25)['status'], 'playing')
        frame = runtime.evaluate(.5)
        self.assertEqual(runtime.time, .5)
        yield frame['pose'], frame['solve_contacts'] + frame['contacts'], frame['status']

    def test_long_and_short_two_bone_segments_bend_to_target(self):
        length = 60_000_000.
        target = [0., length, 0.]
        for a, b in ((length, 1.), (1., length)):
            tracks = [step('b1', 'translation', [0.], [[0., a, 0.]]),
                      step('b2', 'translation', [0.], [[0., b, 0.]])]
            with self.subTest(a=a, b=b):
                for pose, receipts, status in self.controlled_pose(tracks, target):
                    world = self.assert_geometry(pose)
                    root, elbow, end = (world[name][:3, 3] for name in ('b0', 'b1', 'b2'))
                    self.assertAlmostEqual(math.dist(root, elbow), a, delta=1e-7)
                    self.assertAlmostEqual(math.dist(elbow, end), b, delta=1e-7)
                    # The requested positive-Z pole bends the one-metre segment
                    # almost perpendicular to the long side in either ordering.
                    self.assertAlmostEqual(elbow[2], 1., delta=1e-7)
                    error = math.dist(end, target)
                    self.assertLess(error, 1e-7)
                    for row in receipts:
                        self.assertTrue(row['reachable'] and row['converged'])
                        self.assertAlmostEqual(row['residual_m'], error, delta=1e-12)
                    if status is not None:
                        self.assertEqual(status, 'playing')

    def test_near_equal_segments_admit_only_targets_within_folded_range(self):
        scale = float(2**29)
        for first, target_x, reachable in ((1.0000000018626454, 1.0000001192092896, True),
                                           (1.0000000018626458, 1.000000238418579, False)):
            target = [target_x, 0., 0.]
            tracks = [step('b0', 'scale', [0.], [[scale] * 3]),
                      step('b1', 'translation', [0.], [[first, 0., 0.]]),
                      step('b2', 'translation', [0.], [[-1., 0., 0.]])]
            # The exactly opposing FK offsets already attain the closest endpoint.
            minimum = (first - 1.) * scale
            self.assertEqual(minimum - target_x, 0. if reachable else 2.**-23)
            with self.subTest(first=first):
                for pose, receipts, status in self.controlled_pose(tracks, target):
                    end = self.assert_geometry(pose)['b2'][:3, 3]
                    np.testing.assert_array_equal(end, [minimum, 0., 0.])
                    for row in receipts:
                        self.assertEqual(row['reachable'], reachable)
                        self.assertEqual(row['converged'], reachable)
                        self.assertEqual(row['residual_m'], math.dist(end, target))
                    if status is not None:
                        self.assertEqual(status, 'playing' if reachable else 'controls-unsatisfied')

    def moving_contact(self, exponents, x, *, orientation):
        # Expand X, translate a branch, then cancel the inherited scale before
        # its locked IK chain. Endpoint distances are independently known.
        half = len(exponents)
        first = 2 * half
        names = [f'b{i}' for i in range(first, first + 3)]
        self.bind_root(first + 3)
        controllers = self.rig.manifest['controllers']
        controllers['ik'][0]['bones'] = names
        controllers['constraints'] = [dict(schema_version=1, id='lock-' + name,
                                           bone=name, type='locked', axes=[0, 1, 2])
                                      for name in names[:-1]]
        powers = exponents + [-power for power in reversed(exponents)]
        tracks = [step(f'b{i}', 'scale', [0.], [[2.**power, 1., 1.]])
                  for i, power in enumerate(powers)]
        tracks.append(step(f'b{half}', 'translation', [0., .5], [[-x, 1., 0.], [x, 1., 0.]]))
        goal = [0., float(first + 2), 0.]
        contact = dict(schema_version=1, id='hand', kind='hand', chain='arm',
                       start=0., end=1., target=goal)
        if orientation:
            contact['orientation'] = [0., 0., 0., 1.]
        clip = self.install_clip(tracks, [contact], 'moving-contact')
        points = []
        for time, sign in ((0., -1.), (.75, 1.)):
            frame = evaluate_animation(self.rig, clip, time)
            json.dumps(frame, allow_nan=False)
            world = self.assert_geometry(frame['pose'])
            endpoint = world[names[-1]][:3, 3]
            np.testing.assert_array_equal(endpoint, [sign * math.ldexp(x, sum(exponents)), goal[1], 0.])
            for key in ('contacts', 'final_contacts'):
                self.assertEqual(frame[key][0]['residual_m'], math.dist(endpoint, goal))
                self.assertFalse(frame[key][0]['converged'])
            points.append(endpoint)
        return clip, math.dist(points[0], goal), math.dist(points[1], points[0])

    def test_finite_contact_measurements_retain_measured_values(self):
        for orientation in (False, True):
            with self.subTest(orientation=orientation):
                clip, error, drift = self.moving_contact([3], .25, orientation=orientation)
                self.assertEqual((error, drift), (2., 4.))
                metrics = contact_metrics(self.rig, clip)
                self.assertEqual(metrics['samples'], 7)
                self.assertEqual(metrics['max_contact_error_m'], error)
                self.assertEqual(metrics['max_planted_drift_m'], drift)
                json.dumps(metrics, allow_nan=False)
                report = validate_rig(self.rig)
                validate_contract(report, 'RigValidationReport')
                json.dumps(report, allow_nan=False)
                row = next(row for row in report['checks'] if row['name'] == 'contacts')
                self.assertEqual((row['status'], row['value']), ('failed', drift))
                self.assertEqual(report['metrics']['max_contact_error_m'], error)
                self.assertEqual(report['metrics']['max_planted_drift_m'], drift)

    def test_unrepresentable_drift_rejects_structured_for_both_orientation_modes(self):
        for orientation in (False, True):
            with self.subTest(orientation=orientation), np.errstate(over='ignore', invalid='ignore'):
                clip, error, drift = self.moving_contact([24] * 42 + [15], 1.45, orientation=orientation)
                self.assertEqual(error, 1.303327522775179e308)
                self.assertTrue(math.isfinite(error))
                self.assertTrue(math.isinf(drift))
                with self.assertRaises(RigError) as failure:
                    contact_metrics(self.rig, clip)
                self.assertTrue(failure.exception.report['code'].startswith('RIG_'))
                json.dumps(failure.exception.report, allow_nan=False)

    def test_failed_contact_measurement_never_reports_success_or_invented_zero(self):
        for orientation in (False, True):
            with np.errstate(over='ignore', invalid='ignore'):
                clip, error, drift = self.moving_contact([24] * 42 + [15], 1.45, orientation=orientation)
                self.assertTrue(math.isfinite(error) and math.isinf(drift))
                report = validate_rig(self.rig)
            validate_contract(report, 'RigValidationReport')
            json.dumps(report, allow_nan=False)
            self.assertFalse(report['ok'])
            rows = {row['name']: row for row in report['checks']}
            self.assertEqual(rows['clip:' + clip['id']]['status'], 'failed')
            with self.subTest(orientation=orientation, field='contacts.status'):
                self.assertIn(rows['contacts']['status'], ('failed', 'unavailable'))
            with self.subTest(orientation=orientation, field='contacts.value'):
                self.assertIsNone(rows['contacts'].get('value'))
            for field in ('max_contact_error_m', 'max_planted_drift_m'):
                with self.subTest(orientation=orientation, field=field):
                    self.assertIsNone(report['metrics'].get(field))


if __name__ == '__main__':
    unittest.main()
