"""Native validator regressions for finite residuals, drift, displacement and pose signatures."""
from copy import deepcopy
import json
import math
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_errors import RigError
from rig_ik import validate_controllers
from rig_models import SkinWeights, validate_geometry
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from rig_test_helpers import chain_skeleton, fixture
from rig_validation import contact_metrics, validate_rig


def step(bone, path, times, values):
    return dict(schema_version=1, target=bone, path=path, interpolation='STEP',
                times=times, values=values)


def checks(report):
    return {row['name']: row['status'] for row in report['checks']}


class RigValidationFiniteMetricTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)
        self.mesh = self.rig.geometry.meshes[0]
        self.neutral = deepcopy(self.rig.clips['neutral'])

    def bind(self, bone_count, joint):
        self.rig.manifest['skeleton'] = chain_skeleton(bone_count)
        validate_skeleton(self.rig.skeleton)
        validate_geometry(self.rig.geometry)
        rest = evaluate_fk(self.rig.skeleton)[joint]
        vertices = len(self.mesh.positions)
        skin = SkinWeights(self.mesh.id, [joint], np.zeros((vertices, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (vertices, 1)), np.array([np.linalg.inv(rest)]))
        validate_skin(skin, self.mesh, self.rig.skeleton)
        self.rig.weights = {self.mesh.id: skin}

    def install_clip(self, tracks, contacts=()):
        clip = dict(deepcopy(self.neutral), id='metric-fixture', tracks=tracks, contacts=list(contacts))
        validate_controllers(self.rig.skeleton, self.rig.manifest['controllers'])
        validate_animation(self.rig, clip)
        self.rig.clips = {clip['id']: clip}
        return clip

    def moving_contact(self, exponents, x):
        # Expand X, move the branch origin, then restore unit scale before the IK chain.
        # Lock its two rotation channels so the contact endpoint keeps a known trajectory.
        half = len(exponents)
        first = 2 * half
        chain = [f'b{i}' for i in range(first, first + 3)]
        self.bind(first + 3, 'b0')
        controllers = self.rig.manifest['controllers']
        controllers['ik'][0]['bones'] = chain
        controllers['constraints'] = [
            dict(schema_version=1, id='lock-' + name, bone=name, type='locked', axes=[0, 1, 2])
            for name in chain[:-1]]
        values = exponents + [-power for power in reversed(exponents)]
        tracks = [step(f'b{i}', 'scale', [0.], [[2.**power, 1., 1.]]) for i, power in enumerate(values)]
        tracks.append(step(f'b{half}', 'translation', [0., .5], [[-x, 1., 0.], [x, 1., 0.]]))
        goal = [0., float(first + 2), 0.]
        contact = dict(schema_version=1, id='hand', kind='hand', chain='arm', start=0., end=1.,
                       target=goal, orientation=[0., 0., 0., 1.])
        clip = self.install_clip(tracks, [contact])
        points = []
        for time, sign in ((0., -1.), (.75, 1.)):
            frame = evaluate_animation(self.rig, clip, time)
            json.dumps(frame, allow_nan=False)
            endpoint = evaluate_fk(self.rig.skeleton, frame['pose'])[chain[-1]][:3, 3]
            np.testing.assert_array_equal(endpoint, [sign * math.ldexp(x, sum(exponents)), goal[1], 0.])
            points.append(endpoint)
            self.assertTrue(np.isfinite(evaluate_deformation(self.rig, frame['pose'])[self.mesh.id]['positions']).all())
            for field in ('contacts', 'final_contacts'):
                self.assertEqual(frame[field][0]['residual_m'], math.dist(endpoint, goal))
                self.assertFalse(frame[field][0]['converged'])
        return clip, math.dist(points[0], goal), math.dist(points[1], points[0])

    def animated_strip(self, factors, *, diagonal=False):
        # With binding weight one, x (and optionally z) is multiplied by the exact
        # power-of-two product. The y translations cancel against the inverse bind.
        if diagonal:
            self.mesh.positions[:, 0] = np.copysign(1.45, self.mesh.positions[:, 0])
            self.mesh.positions[:, 2] = 1.45
        self.bind(len(factors), f'b{len(factors) - 1}')
        self.rig.manifest['controllers'] = dict(ik=[], constraints=[])
        tracks = [step(f'b{i}', 'scale', [0., .5], [[1., 1., 1.], [s, 1., s if diagonal else 1.]])
                  for i, s in enumerate(factors)]
        clip = self.install_clip(tracks)
        max_distance = 0.
        for time, amount in ((0., 1.), (.63, math.prod(factors))):
            frame = evaluate_animation(self.rig, clip, time)
            actual = evaluate_deformation(self.rig, frame['pose'])[self.mesh.id]['positions']
            explicit = self.mesh.positions * [amount, 1., amount if diagonal else 1.]
            self.assertTrue(np.isfinite(actual).all())
            np.testing.assert_array_equal(actual, explicit)
            max_distance = max(max_distance, max(math.dist(a, b) for a, b in zip(actual, self.mesh.positions)))
        return clip, max_distance

    def strict_report(self, report):
        validate_contract(report, 'RigValidationReport')
        json.dumps(report, allow_nan=False)

    def test_representable_final_contact_error_and_planted_drift(self):
        with np.errstate(over='ignore', invalid='ignore'):
            clip, error, drift = self.moving_contact([23] * 23, 1.)
            self.assertTrue(math.isfinite(drift))
            self.assertEqual(drift, 2 * error)
            self.assertGreater(error, 1e155)
            metrics = contact_metrics(self.rig, clip)
            report = validate_rig(self.rig)
        for owner, row, expected in (
                ('clip error', metrics, {'max_contact_error_m': error, 'max_planted_drift_m': drift}),
                ('contact', metrics['contacts'][0], {'max_error_m': error, 'max_drift_m': drift}),
                ('rig', report['metrics'], {'max_contact_error_m': error, 'max_planted_drift_m': drift})):
            for key, value in expected.items():
                with self.subTest(owner=owner, key=key):
                    self.assertEqual(row[key], value)
        with self.subTest(format='strict JSON'):
            self.strict_report(report)
            json.dumps(metrics, allow_nan=False)
        self.assertEqual(metrics['samples'], 7)
        self.assertFalse(metrics['orientation_converged'])
        self.assertFalse(report['ok'])
        self.assertEqual(checks(report)['contacts'], 'failed')

    def test_finite_animated_vertex_displacement_has_a_serializable_success_report(self):
        with np.errstate(over='ignore', invalid='ignore'):
            _, expected = self.animated_strip([2.**23] * 23)
            self.assertTrue(math.isfinite(expected))
            self.assertGreater(expected, 1e155)
            report = validate_rig(self.rig, require_animation=True)
        self.assertTrue(report['ok'])
        self.assertEqual(report['metrics']['max_vertex_displacement_m'], expected)
        self.assertEqual(report['metrics']['distinct_sampled_poses'], 2)
        self.strict_report(report)

    def test_finite_large_pose_signatures_retain_distinct_animation(self):
        with np.errstate(over='ignore', invalid='ignore'):
            clip, expected = self.animated_strip([2.**29] * 35)
            self.assertTrue(math.isfinite(expected))
            self.assertGreater(expected, 1e304)
            report = validate_rig(self.rig, require_animation=True)
        for name in ('clip:' + clip['id'], 'animated-geometry'):
            with self.subTest(check=name):
                self.assertEqual(checks(report)[name], 'passed')
        self.assertTrue(report['ok'])
        self.assertEqual(report['metrics']['distinct_sampled_poses'], 2)
        self.assertEqual(report['metrics']['max_vertex_displacement_m'], expected)
        self.strict_report(report)

    def test_unrepresentable_drift_rejects_with_a_finite_structured_failure(self):
        with np.errstate(over='ignore', invalid='ignore'):
            clip, error, drift = self.moving_contact([23] * 44 + [11], 1.45)
            self.assertTrue(math.isfinite(error))
            self.assertTrue(math.isinf(drift))
            with self.assertRaises(RigError) as failure:
                contact_metrics(self.rig, clip)
            self.assertTrue(failure.exception.report['code'].startswith('RIG_'))
            json.dumps(failure.exception.report, allow_nan=False)
            report = validate_rig(self.rig)
        self.assertFalse(report['ok'])
        self.assertEqual(checks(report)['clip:' + clip['id']], 'failed')
        self.strict_report(report)

    def test_unrepresentable_vertex_distance_returns_a_finite_failure_report(self):
        with np.errstate(over='ignore', invalid='ignore'):
            clip, distance = self.animated_strip([2.**29] * 35 + [2.**8], diagonal=True)
            self.assertTrue(math.isinf(distance))
            report = validate_rig(self.rig, require_animation=True)
        self.assertFalse(report['ok'])
        self.assertEqual(checks(report)['clip:' + clip['id']], 'failed')
        self.strict_report(report)

    def test_ordinary_contact_motion_keeps_the_measured_distances(self):
        clip, error, drift = self.moving_contact([3], .25)
        self.assertEqual((error, drift), (2., 4.))
        metrics = contact_metrics(self.rig, clip)
        self.assertEqual(metrics['max_contact_error_m'], error)
        self.assertEqual(metrics['max_planted_drift_m'], drift)
        self.assertEqual(metrics['samples'], 7)
        self.assertFalse(metrics['orientation_converged'])
        json.dumps(metrics, allow_nan=False)

    def test_ordinary_pose_signatures_keep_five_decimal_precision(self):
        for scale, count in ((1. + 1e-7, 1), (2., 2)):
            with self.subTest(scale=scale):
                _, distance = self.animated_strip([scale])
                report = validate_rig(self.rig)
                self.assertTrue(report['ok'])
                self.assertEqual(report['metrics']['distinct_sampled_poses'], count)
                self.assertEqual(report['metrics']['max_vertex_displacement_m'], distance)
                self.strict_report(report)


if __name__ == '__main__':
    unittest.main()
