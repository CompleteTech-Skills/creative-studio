"""Real native contact-orientation acceptance regressions for issue #496.

Authored against 410dbd093c336f4cc41e6deadc4a04a2c1309fc6 before the validation
implementation. These are executable numerical tests, not captured execution
evidence. No solver is mocked. The angular oracle uses rotation matrices rather
than the production quaternion residual helper.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from rig_test_helpers import fixture
from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_errors import object_hash
from rig_models import rig_signature
from rig_skeleton import evaluate_fk
from rig_validation import contact_metrics, validate_rig


# Native solve_ik uses this angular bound; IKChain.tolerance is positional.
NATIVE_ANGULAR_TOLERANCE = 1e-5


def x_quaternion(angle):
    return [math.sin(angle / 2), 0., 0., math.cos(angle / 2)]


def rotation_matrix(quaternion):
    """Independent XYZW-to-matrix conversion, including q/-q equivalence."""
    x, y, z, w = np.asarray(quaternion, dtype=float)
    magnitude = math.sqrt(x*x + y*y + z*z + w*w)
    x, y, z, w = x/magnitude, y/magnitude, z/magnitude, w/magnitude
    return np.array([
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
    ])


def angular_error(world_matrix, goal):
    """SO(3) geodesic from final FK, without qangle or solver metadata."""
    rotation = np.asarray(world_matrix[:3, :3], dtype=float)
    # Fixtures have unit scale; reject a broken fixture instead of hiding scale.
    np.testing.assert_allclose(rotation.T @ rotation, np.eye(3), atol=1e-10)
    relative = rotation_matrix(goal).T @ rotation
    cosine = float(np.clip((np.trace(relative)-1) / 2, -1., 1.))
    return math.acos(cosine)


class RigContactOrientationValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='contact-orientation-')
        self.addCleanup(temporary.cleanup)
        self.original = fixture(temporary.name)
        self.original_manifest = deepcopy(self.original.manifest)
        self.original_clips = deepcopy(self.original.clips)
        self.original_positions = {
            mesh.id: mesh.positions.copy() for mesh in self.original.geometry.meshes
        }

    def tearDown(self):
        # Variants are isolated copies; preserve existing source/geometry fixtures.
        self.assertEqual(self.original.manifest, self.original_manifest)
        self.assertEqual(self.original.clips, self.original_clips)
        for mesh in self.original.geometry.meshes:
            np.testing.assert_array_equal(mesh.positions, self.original_positions[mesh.id])

    def case(self, solver, angle=None, inherited=False):
        rig = deepcopy(self.original)
        chain = rig.manifest['controllers']['ik'][0]
        chain['solver'] = solver
        chain.pop('orientation', None)
        rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='endpoint-hinge', bone='b2', type='hinge',
                 axis=[1., 0., 0.], minimum=0., maximum=.2)
        ]
        clip = deepcopy(rig.clips['neutral'])
        clip['contacts'] = [
            dict(schema_version=1, id='hold', chain='arm', start=0., end=1.,
                 target=[0., 2., 0.], kind='hand')
        ]
        if angle is not None:
            owner = chain if inherited else clip['contacts'][0]
            owner['orientation'] = x_quaternion(angle)
        rig.clips = {clip['id']: clip}
        validate_animation(rig, clip)
        return rig, clip

    def final_errors(self, rig, clip, contact):
        chains = {chain['id']: chain for chain in rig.manifest['controllers']['ik']}
        chain = chains[contact['chain']]
        goal = contact.get('orientation', chain.get('orientation'))
        errors = []
        native = []
        for time in np.linspace(contact['start'], contact['end'], 7, endpoint=False):
            evaluated = evaluate_animation(rig, clip, float(time))
            world = evaluate_fk(rig.skeleton, evaluated['pose'])[chain['bones'][-1]]
            self.assertLess(np.linalg.norm(world[:3, 3]-contact['target']), .002)
            errors.append(angular_error(world, goal) if goal is not None else None)
            native.append(next(entry for entry in evaluated['contacts']
                               if entry['id'] == contact['id']))
        return errors, native

    def assert_outcome(self, rig, clip, expected_ok, expected_error, oriented=True):
        manifest_before, clip_before = deepcopy(rig.manifest), deepcopy(clip)
        metrics = contact_metrics(rig, clip)
        row = metrics['contacts'][0]
        self.assertEqual(metrics['samples'], 7)
        self.assertEqual(row['samples'], 7)
        self.assertLess(metrics['max_contact_error_m'], 1e-8)
        self.assertLess(metrics['max_planted_drift_m'], 1e-8)
        self.assertEqual(row['id'], 'hold')
        self.assertEqual(row['max_error_m'], metrics['max_contact_error_m'])
        self.assertEqual(row['max_drift_m'], metrics['max_planted_drift_m'])
        errors, native = self.final_errors(rig, clip, clip['contacts'][0])
        if oriented:
            observed = max(errors)
            self.assertAlmostEqual(observed, expected_error, delta=1e-7)
            self.assertAlmostEqual(row['max_orientation_error_rad'], observed, delta=1e-7)
            self.assertAlmostEqual(metrics['max_contact_orientation_error_rad'], observed, delta=1e-7)
            self.assertEqual(row['orientation_samples'], 7)
            self.assertEqual(metrics['orientation_samples'], 7)
            self.assertEqual(row['orientation_tolerance_rad'], NATIVE_ANGULAR_TOLERANCE)
            self.assertEqual(row['orientation_converged'], expected_ok)
            self.assertTrue(all(entry['converged'] == expected_ok for entry in native))
        else:
            self.assertTrue(all(error is None for error in errors))
            self.assertIsNone(row['max_orientation_error_rad'])
            self.assertIsNone(metrics['max_contact_orientation_error_rad'])
            self.assertIsNone(row['orientation_tolerance_rad'])
            self.assertEqual(row['orientation_samples'], 0)
            self.assertEqual(metrics['orientation_samples'], 0)
            self.assertTrue(all(entry['converged'] for entry in native))
        report = validate_rig(rig)
        validate_contract(report, 'RigValidationReport')
        json.dumps(metrics, allow_nan=False)
        self.assertEqual(report['rig_sha256'], rig_signature(rig.manifest))
        checks = {check['name']: check['status'] for check in report['checks']}
        for name in ('structure', 'skin', 'bind-pose', 'clip:'+clip['id'], 'key-budget'):
            self.assertEqual(checks[name], 'passed')
        self.assertEqual(checks['contacts'], 'passed' if expected_ok else 'failed')
        self.assertEqual(report['ok'], expected_ok)
        self.assertEqual(report['metrics']['orientation_samples'], 7 if oriented else 0)
        actual = report['metrics']['max_contact_orientation_error_rad']
        if oriented:
            self.assertAlmostEqual(actual, expected_error, delta=1e-7)
        else:
            self.assertIsNone(actual)
        self.assertEqual(rig.manifest, manifest_before)
        self.assertEqual(clip, clip_before)

    def test_orientation_diagnostics_bind_evaluated_clip_content(self):
        rig, clip = self.case('two-bone', .1)
        clip['id'] = 'clip:contact_orientation'
        rig.clips = {clip['id']: clip}

        def measured_metrics():
            report = validate_rig(rig)
            validate_contract(report, 'RigValidationReport')
            self.assertEqual(report['rig_sha256'], rig_signature(rig.manifest))
            return {key: value for key, value in report['metrics'].items()
                    if key.startswith('contact_orientation/')}

        original_digest = object_hash(clip)
        prefix = f"contact_orientation/{clip['id']}/{original_digest}/"
        first = measured_metrics()
        self.assertEqual(first[prefix + 'clip_version'], clip['version'])
        self.assertEqual(first[prefix + 'samples'], 7)
        self.assertTrue(first[prefix + 'converged'])
        self.assertLess(first[prefix + 'max_error_rad'], 1e-7)
        self.assertEqual(first, measured_metrics())
        self.assertEqual(contact_metrics(rig, clip)['clip_content_sha256'], original_digest)

        # An in-memory edit leaves the manifest and clip identity intact.
        clip['contacts'][0]['orientation'] = x_quaternion(1.2)
        amended_digest = object_hash(clip)
        self.assertNotEqual(amended_digest, original_digest)
        amended_prefix = f"contact_orientation/{clip['id']}/{amended_digest}/"
        amended = measured_metrics()
        self.assertNotIn(prefix + 'clip_version', amended)
        self.assertEqual(amended[amended_prefix + 'clip_version'], clip['version'])
        self.assertFalse(amended[amended_prefix + 'converged'])
        self.assertAlmostEqual(amended[amended_prefix + 'max_error_rad'], 1.)
        self.assertEqual(contact_metrics(rig, clip)['clip_content_sha256'], amended_digest)

        del clip['contacts'][0]['orientation']
        self.assertEqual(measured_metrics(), {})

    def test_reached_hinge_limited_impossible_goal_is_rejected(self):
        for solver in ('two-bone', 'fabrik'):
            for inherited in (False, True):
                with self.subTest(solver=solver, inherited=inherited):
                    rig, clip = self.case(solver, 1.2, inherited)
                    self.assert_outcome(rig, clip, False, 1.)

    def test_feasible_goal_is_accepted_including_quaternion_sign(self):
        for solver in ('two-bone', 'fabrik'):
            for inherited in (False, True):
                for sign in (1., -1.):
                    with self.subTest(solver=solver, inherited=inherited, sign=sign):
                        rig, clip = self.case(solver, .1, inherited)
                        owner = rig.manifest['controllers']['ik'][0] if inherited else clip['contacts'][0]
                        owner['orientation'] = [sign*x for x in owner['orientation']]
                        self.assert_outcome(rig, clip, True, 0.)

    def test_explicit_goal_overrides_chain_default_in_both_directions(self):
        for solver in ('two-bone', 'fabrik'):
            for explicit, inherited, accepted, error in ((.1, 1.2, True, 0.), (1.2, .1, False, 1.)):
                with self.subTest(solver=solver, explicit=explicit):
                    rig, clip = self.case(solver, explicit)
                    rig.manifest['controllers']['ik'][0]['orientation'] = x_quaternion(inherited)
                    self.assert_outcome(rig, clip, accepted, error)

    def test_position_tolerance_does_not_relax_native_angular_tolerance(self):
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                rig, clip = self.case(solver, .2002)
                rig.manifest['controllers']['ik'][0]['tolerance'] = .01
                self.assert_outcome(rig, clip, False, .0002)

    def test_position_only_legacy_and_no_contact_controls(self):
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                rig, clip = self.case(solver)
                self.assert_outcome(rig, clip, True, None, oriented=False)
                clip['contacts'] = []
                metrics = contact_metrics(rig, clip)
                self.assertEqual(metrics['contacts'], [])
                self.assertEqual(metrics['samples'], 0)
                self.assertEqual(metrics['orientation_samples'], 0)
                self.assertIsNone(metrics['max_contact_orientation_error_rad'])
                report = validate_rig(rig)
                self.assertTrue(report['ok'])
                self.assertEqual(report['metrics']['orientation_samples'], 0)
                self.assertIsNone(report['metrics']['max_contact_orientation_error_rad'])

    def test_later_contact_cannot_hide_final_pose_orientation_failure(self):
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                rig, clip = self.case(solver, .1)
                later_chain = deepcopy(rig.manifest['controllers']['ik'][0])
                later_chain['id'] = 'later-arm'
                rig.manifest['controllers']['ik'].append(later_chain)
                later = deepcopy(clip['contacts'][0])
                later.update(id='later-hold', chain='later-arm', orientation=x_quaternion(.2))
                clip['contacts'].append(later)
                validate_animation(rig, clip)
                errors, native = self.final_errors(rig, clip, clip['contacts'][0])
                self.assertTrue(all(entry['converged'] for entry in native))
                self.assertTrue(all(entry['orientation_residual_rad'] < 1e-7 for entry in native))
                self.assertAlmostEqual(max(errors), .1, delta=1e-7)
                metrics = contact_metrics(rig, clip)
                rows = {row['id']: row for row in metrics['contacts']}
                self.assertLess(metrics['max_contact_error_m'], 1e-8)
                self.assertLess(metrics['max_planted_drift_m'], 1e-8)
                self.assertAlmostEqual(rows['hold']['max_orientation_error_rad'], .1, delta=1e-7)
                self.assertFalse(rows['hold']['orientation_converged'])
                self.assertLess(rows['later-hold']['max_orientation_error_rad'], 1e-7)
                self.assertTrue(rows['later-hold']['orientation_converged'])
                self.assertEqual(metrics['orientation_samples'], 14)
                self.assertAlmostEqual(metrics['max_contact_orientation_error_rad'], .1, delta=1e-7)
                report = validate_rig(rig)
                self.assertFalse(report['ok'])
                self.assertEqual(next(check['status'] for check in report['checks']
                                      if check['name'] == 'contacts'), 'failed')
                self.assertEqual(report['metrics']['orientation_samples'], 14)
                self.assertAlmostEqual(report['metrics']['max_contact_orientation_error_rad'], .1, delta=1e-7)

    def test_short_overlap_checks_every_active_orientation(self):
        for solver in ('two-bone', 'fabrik'):
            for later_angle, expected_ok in ((.2, False), (.1, True)):
                with self.subTest(solver=solver, later_angle=later_angle):
                    rig, clip = self.case(solver, .1)
                    chain = deepcopy(rig.manifest['controllers']['ik'][0])
                    chain['id'] = 'later-arm'
                    rig.manifest['controllers']['ik'].append(chain)
                    earlier = clip['contacts'][0]
                    later = deepcopy(earlier)
                    later.update(id='later-hold', chain='later-arm', start=.04, end=.06,
                                 orientation=x_quaternion(later_angle))
                    clip['contacts'].append(later)
                    validate_animation(rig, clip)

                    for time, expected_ids in ((.04, ['hold', 'later-hold']),
                                               (.05, ['hold', 'later-hold']), (.06, ['hold'])):
                        evaluated = evaluate_animation(rig, clip, time)
                        self.assertEqual([row['id'] for row in evaluated['contacts']], expected_ids)
                        self.assertTrue(all(row['converged'] for row in evaluated['contacts']))
                    evaluated = evaluate_animation(rig, clip, .05)
                    final = evaluate_fk(rig.skeleton, evaluated['pose'])[chain['bones'][-1]]
                    actual_error = angular_error(final, earlier['orientation'])
                    self.assertAlmostEqual(actual_error, abs(later_angle - .1), delta=1e-7)

                    report = validate_rig(rig)
                    validate_contract(report, 'RigValidationReport')
                    self.assertEqual(report['ok'], expected_ok)
                    prefix = f"contact_orientation/{clip['id']}/{object_hash(clip)}/"
                    self.assertEqual(report['metrics'][prefix + 'samples'], 21)
                    self.assertAlmostEqual(report['metrics'][prefix + 'max_error_rad'],
                                           actual_error, delta=1e-7)
                    self.assertEqual(report['metrics'][prefix + 'converged'], expected_ok)

                    metrics = contact_metrics(rig, clip)
                    rows = {row['id']: row for row in metrics['contacts']}
                    self.assertEqual(metrics['samples'], 14)
                    self.assertLess(metrics['max_contact_error_m'], 1e-8)
                    self.assertLess(metrics['max_planted_drift_m'], 1e-8)
                    self.assertEqual(rows['hold']['samples'], 7)
                    self.assertEqual(rows['hold']['orientation_samples'], 14)
                    self.assertEqual(rows['later-hold']['orientation_samples'], 7)
                    self.assertTrue(rows['later-hold']['orientation_converged'])
                    self.assertEqual(rows['hold']['orientation_converged'], expected_ok)

    def test_aggregate_counts_only_required_goals_across_contacts_and_clips(self):
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                rig, clip = self.case(solver, .1)
                positional_chain = deepcopy(rig.manifest['controllers']['ik'][0])
                positional_chain['id'] = 'position-arm'
                rig.manifest['controllers']['ik'].append(positional_chain)
                positional = deepcopy(clip['contacts'][0])
                positional.update(id='position-hold', chain='position-arm')
                del positional['orientation']
                clip['contacts'].insert(0, positional)
                metrics = contact_metrics(rig, clip)
                rows = {row['id']: row for row in metrics['contacts']}
                self.assertEqual(metrics['samples'], 14)
                self.assertEqual(metrics['orientation_samples'], 7)
                self.assertIsNone(rows['position-hold']['max_orientation_error_rad'])
                self.assertEqual(rows['position-hold']['orientation_samples'], 0)
                self.assertLess(rows['hold']['max_orientation_error_rad'], 1e-7)
                self.assertTrue(validate_rig(rig)['ok'])
                bad = deepcopy(clip)
                bad['id'] = 'bad-hold'
                bad['contacts'] = [deepcopy(clip['contacts'][1])]
                bad['contacts'][0]['orientation'] = x_quaternion(1.2)
                rig.clips = {bad['id']: bad, clip['id']: clip}
                report = validate_rig(rig)
                validate_contract(report, 'RigValidationReport')
                self.assertFalse(report['ok'])
                self.assertEqual(report['metrics']['orientation_samples'], 14)
                self.assertAlmostEqual(report['metrics']['max_contact_orientation_error_rad'], 1., delta=1e-7)

    def test_required_orientation_preserves_native_nonconvergence(self):
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                rig, clip = self.case(solver, .1)
                # Below legacy geometry-scaled contact bounds, above native reach.
                clip['contacts'][0]['target'][1] += .001
                errors, native = self.final_errors(rig, clip, clip['contacts'][0])
                self.assertLess(max(errors), 1e-7)
                self.assertTrue(all(not entry['converged'] for entry in native))
                metrics = contact_metrics(rig, clip)
                self.assertLess(metrics['max_contact_orientation_error_rad'], 1e-7)
                self.assertFalse(metrics['contacts'][0]['orientation_converged'])
                report = validate_rig(rig)
                self.assertLess(metrics['max_contact_error_m'], report['metrics']['contact_tolerance_m'])
                self.assertLess(metrics['max_planted_drift_m'], report['metrics']['contact_tolerance_m'])
                self.assertFalse(report['ok'])
                self.assertEqual(next(check['status'] for check in report['checks']
                                      if check['name'] == 'contacts'), 'failed')
                # The same unreachable position retains historical position-only acceptance.
                del clip['contacts'][0]['orientation']
                self.assertTrue(validate_rig(rig)['ok'])


if __name__ == '__main__':
    unittest.main()
