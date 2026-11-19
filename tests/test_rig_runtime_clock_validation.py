"""Independent clock and final-contact oracles for issue #530.

Authored against 7397872bbab54942b977fc36d0589ced398c59eb. These synthetic
fixtures execute native controls, FK and skinning; no solver is mocked. Exact
represented durations define the schedule, with one rounding at each absolute
boundary. Numerical evidence does not establish visual or production approval.
"""
from copy import deepcopy
from fractions import Fraction
import json
import math
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_errors import object_hash
from rig_skeleton import evaluate_fk
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture
from rig_validation import contact_metrics, validate_rig


class RigRuntimeClockValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rig = fixture(temporary.name)

    def clock_clips(self, scale=1.):
        mesh = self.rig.geometry.meshes[0]
        for name, duration, amount in (('open', .1*scale, 0.),
                                       ('closed', .2*scale, 1.)):
            clip = deepcopy(self.rig.clips['neutral'])
            clip.update(id=name, duration=duration, loop=True, root_motion='in-place',
                        tracks=[dict(schema_version=1, target=mesh.id, path='weights',
                                     interpolation='STEP', times=[0., duration],
                                     values=[[amount], [amount]])])
            validate_animation(self.rig, clip)
            self.rig.clips[name] = clip
        return (Fraction(self.rig.clips['open']['duration']),
                Fraction(self.rig.clips['closed']['duration']))

    def runtime(self, repetitions=1, entered=0.):
        state = 'static-positive'
        return RigStateRuntime(self.rig, state,
                               recipes={state: ['open', 'closed']*repetitions},
                               selected=[state], now=entered, blend_seconds=0.)

    def assert_clock_pose(self, runtime, time, amount):
        frame = runtime.evaluate(time)
        json.dumps(frame, allow_nan=False)
        self.assertEqual(frame['status'], 'playing')
        self.assertEqual(frame['control_stage'], 'native')
        self.assertEqual(frame['transition_count'], 0)
        self.assertEqual(frame['contacts'], [])
        mesh = self.rig.geometry.meshes[0]
        actual = evaluate_deformation(self.rig, frame['pose'])[mesh.id]['positions']
        # The neutral rig's actual stored morph delta, rather than an assumed
        # .2m value (the GLB fixture roundtrip represents that value in float32).
        expected = mesh.positions + amount*mesh.morphs['blink']['POSITION']
        self.assertTrue(np.isfinite(actual).all())
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-12)
        self.assertEqual(frame['pose']['morphs'][mesh.id]['blink'], amount)
        reference = evaluate_animation(self.rig, 'closed' if amount else 'open', 0.)
        actual_fk = evaluate_fk(self.rig.skeleton, frame['pose'])
        expected_fk = evaluate_fk(self.rig.skeleton, reference['pose'])
        for bone in expected_fk:
            self.assertTrue(np.isfinite(actual_fk[bone]).all())
            np.testing.assert_allclose(actual_fk[bone], expected_fk[bone], rtol=0, atol=1e-12)
        return actual

    def test_equivalent_recipe_unrolling_owns_exact_point_nine_boundary(self):
        a, b = self.clock_clips()
        boundary = float(3*(a+b))
        self.assertEqual(boundary, .9)
        short, unrolled = self.runtime(), self.runtime(4)
        # Both descriptions are the identical infinite A,B,A,B... schedule.
        mesh = self.rig.geometry.meshes[0]
        frames = [runtime.evaluate(boundary) for runtime in (short, unrolled)]
        vertices = [evaluate_deformation(self.rig, frame['pose'])[mesh.id]['positions']
                    for frame in frames]
        np.testing.assert_array_equal(vertices[0], vertices[1])
        first = self.assert_clock_pose(short, boundary, 0.)
        second = self.assert_clock_pose(unrolled, boundary, 0.)
        np.testing.assert_array_equal(first, second)

    def test_adjacent_times_preserve_half_open_cycle_boundary(self):
        a, b = self.clock_clips()
        boundary = float(3*(a+b))
        for time, amount in ((math.nextafter(boundary, -math.inf), 1.),
                             (math.nextafter(boundary, math.inf), 0.)):
            for repetitions in (1, 4):
                with self.subTest(time=time, repetitions=repetitions):
                    self.assert_clock_pose(self.runtime(repetitions), time, amount)

    def test_scaled_future_cycle_boundaries_are_rounded_after_multiplication(self):
        for scale in (.5, 2., 4.):
            a, b = self.clock_clips(scale)
            for cycles in (3, 9, 33):
                boundary = float(cycles*(a+b))
                for repetitions in (1, 4):
                    with self.subTest(scale=scale, cycles=cycles, repetitions=repetitions):
                        self.assert_clock_pose(self.runtime(repetitions), boundary, 0.)

    def test_cycle_plus_prefix_rounds_once_at_absolute_boundary(self):
        a, b = self.clock_clips()
        for cycles in (3, 7, 31):
            boundary = float(cycles*(a+b)+a)
            for repetitions in (1, 4):
                with self.subTest(cycles=cycles, repetitions=repetitions):
                    self.assert_clock_pose(self.runtime(repetitions), boundary, 1.)

    def test_nonzero_entry_and_interior_future_samples(self):
        a, b = self.clock_clips()
        entered = 8.
        for cycles in (0, 3, 33):
            for within, amount in ((a/2, 0.), (a+b/2, 1.)):
                time = float(Fraction(entered)+cycles*(a+b)+within)
                for repetitions in (1, 4):
                    with self.subTest(cycles=cycles, amount=amount, repetitions=repetitions):
                        self.assert_clock_pose(self.runtime(repetitions, entered), time, amount)

    def test_unrolled_collapsed_period_keeps_native_rounded_fourth_clip_start(self):
        self.clock_clips()
        names = []
        durations = (.001, .004, .04499999999999999, .05000000000000002)
        for index, duration in enumerate(durations):
            clip = deepcopy(self.rig.clips['closed' if index == 3 else 'open'])
            name = 'clip-' + str(index)
            clip.update(id=name, duration=duration)
            clip['tracks'][0]['times'][-1] = duration
            validate_animation(self.rig, clip)
            self.rig.clips[name] = clip
            names.append(name)
        origin = float(2**50)
        self.assertEqual(.25 % math.fsum(durations), math.fsum(durations[:3]))
        for repetitions in (1, 3, 4):
            with self.subTest(repetitions=repetitions):
                state = 'static-positive'
                runtime = RigStateRuntime(self.rig, state, recipes={state: names*repetitions},
                                          selected=[state], now=origin, blend_seconds=0.)
                self.assert_clock_pose(runtime, origin+.25, 1.)

    def test_unrolling_preserves_exact_phase_when_absolute_edges_collapse(self):
        self.clock_clips()
        for name, duration, distance in (('open', .03, .125), ('closed', .07, .25)):
            clip = self.rig.clips[name]
            clip.update(duration=duration, root_motion='embedded')
            clip['tracks'][0]['times'][-1] = duration
            clip['tracks'].append(dict(schema_version=1, target='b0', path='translation',
                                       interpolation='LINEAR', times=[0., duration],
                                       values=[[0., 0., 0.], [distance, 0., 0.]]))
            validate_animation(self.rig, clip)
        origin = float(2**50)
        # Multiple clip starts round to this one timestamp. The exact elapsed
        # clock lies at the end of the fifth pair, still inside its second clip.
        a, b = Fraction(.03), Fraction(.07)
        cycles, phase = divmod(Fraction(.5), a+b)
        self.assertEqual(cycles, 4)
        self.assertLess(a, phase)
        self.assertLess(phase, a+b)
        expected = evaluate_animation(self.rig, 'closed', float(phase-a),
                                      _root_offset=np.array([cycles*.375+.125, 0., 0.]))['pose']
        reference = evaluate_deformation(self.rig, expected)['strip']['positions']
        for repetitions in (1, 4):
            with self.subTest(repetitions=repetitions):
                frame = self.runtime(repetitions, origin).evaluate(origin+.5)
                self.assertEqual(frame['pose']['morphs']['strip']['blink'], 1.)
                np.testing.assert_array_equal(evaluate_deformation(self.rig, frame['pose'])
                                              ['strip']['positions'], reference)

    def test_unrolling_preserves_small_embedded_motion_after_large_opposite_travel(self):
        self.clock_clips()
        for name, distance in (('open', float(2**29)), ('closed', -(2**29-2**-24))):
            clip = self.rig.clips[name]
            clip.update(duration=.5, root_motion='embedded', tracks=[dict(
                schema_version=1, target='b0', path='translation', interpolation='LINEAR',
                times=[0., .5], values=[[0., 0., 0.], [distance, 0., 0.]])])
            validate_animation(self.rig, clip)
        # One pair lasts one second and travels 2**-24. Check both a whole
        # pair and the next half-pair so both cycle and prefix are exercised.
        for time, name, x in ((float(2**25), 'open', 2.),
                              (2**25+.5, 'closed', 2.+2**29)):
            target = evaluate_animation(self.rig, name, 0., _root_offset=[x, 0., 0.])['pose']
            expected = evaluate_deformation(self.rig, target)['strip']['positions']
            for repetitions in (1, 2, 4):
                with self.subTest(time=time, repetitions=repetitions):
                    frame = self.runtime(repetitions).evaluate(time)
                    self.assertEqual(frame['pose']['bones']['b0']['translation'], [x, 0., 0.])
                    np.testing.assert_array_equal(evaluate_deformation(self.rig, frame['pose'])
                                                  ['strip']['positions'], expected)

    def test_single_rounded_start_owns_zero_without_losing_neighboring_phase(self):
        self.clock_clips()
        clip = self.rig.clips['open']
        clip['tracks'][0].update(times=[0., 1e-18], values=[[0.], [1.]])
        validate_animation(self.rig, clip)
        state = 'static-positive'
        runtime = RigStateRuntime(self.rig, state, recipes={state: 'open'},
                                  selected=[state], blend_seconds=0.)
        boundary = float(3*Fraction(.1))
        self.assertGreater(Fraction(boundary)-3*Fraction(.1), Fraction(1e-18))
        for time, expected in ((math.nextafter(boundary, -math.inf), 1.),
                               (boundary, 0.), (math.nextafter(boundary, math.inf), 1.)):
            with self.subTest(time=time):
                self.assert_clock_pose(runtime, time, expected)

    def contact_clip(self, *, repaired=True, oriented=True):
        source = deepcopy(self.rig.manifest['controllers']['ik'][0])
        self.rig.manifest['controllers']['ik'] = [
            dict(deepcopy(source), id='fast', solver='fabrik', iterations=1, tolerance=1e-7)]
        names = ['fast']
        if repaired:
            self.rig.manifest['controllers']['ik'].append(
                dict(deepcopy(source), id='exact', solver='two-bone', tolerance=1e-7))
            names.append('exact')
        clip = deepcopy(self.rig.clips['neutral'])
        clip.update(id='shared-pose', contacts=[
            dict(schema_version=1, id=name, chain=name, start=0., end=1.,
                 target=[.5, 1.5, .2], kind='hand') for name in names])
        if oriented:
            for contact in clip['contacts']:
                contact['orientation'] = [0., 0., 0., 1.]
        self.rig.clips = {clip['id']: clip}
        validate_animation(self.rig, clip)
        return clip

    def assert_final_receipts(self, clip, evaluated):
        json.dumps(evaluated, allow_nan=False)
        world = evaluate_fk(self.rig.skeleton, evaluated['pose'])
        chains = {chain['id']: chain for chain in self.rig.manifest['controllers']['ik']}
        goals = {contact['id']: contact for contact in clip['contacts']}
        for row in evaluated['final_contacts']:
            chain, contact = chains[row['chain']], goals[row['id']]
            endpoint = world[chain['bones'][-1]]
            measured = math.dist(endpoint[:3, 3], contact['target'])
            self.assertAlmostEqual(row['residual_m'], measured, delta=1e-14)
            if 'orientation' in contact:
                # All these goals are identity and fixtures have unit scale:
                # independently measure SO(3) error from the final FK matrix.
                rotation = endpoint[:3, :3]
                np.testing.assert_allclose(rotation.T @ rotation, np.eye(3), atol=1e-12)
                angle = math.acos(float(np.clip((np.trace(rotation)-1)/2, -1., 1.)))
                self.assertAlmostEqual(row['orientation_residual_rad'], angle, delta=1e-7)
            else:
                self.assertIsNone(row['orientation_residual_rad'])
        for mesh in evaluate_deformation(self.rig, evaluated['pose']).values():
            self.assertTrue(np.isfinite(mesh['positions']).all())

    def test_final_geometry_repairs_earlier_failed_solve_without_rewriting_history(self):
        clip = self.contact_clip()
        before = deepcopy((self.rig.manifest, self.rig.clips))
        evaluated = evaluate_animation(self.rig, clip, .5)
        self.assert_final_receipts(clip, evaluated)
        historical = evaluated['contacts'][0]
        self.assertEqual(historical['id'], 'fast')
        self.assertFalse(historical['converged'])
        self.assertGreater(historical['residual_m'], .01)
        for row in evaluated['final_contacts']:
            self.assertTrue(row['reachable'])
            self.assertTrue(row['converged'])
            self.assertLess(row['residual_m'], 1e-7)
            self.assertLess(row['orientation_residual_rad'], 1e-7)
        state = 'static-positive'
        runtime = RigStateRuntime(self.rig, state, recipes={state: clip['id']},
                                  selected=[state], blend_seconds=0.)
        frame = runtime.evaluate(.5)
        self.assertEqual(frame['status'], 'playing')
        self.assertEqual(frame['solve_contacts'], evaluated['contacts'])
        self.assertEqual(frame['contacts'], evaluated['final_contacts'])
        self.assertEqual((self.rig.manifest, self.rig.clips), before)

    def test_contact_metrics_accept_actual_final_geometry_after_repair(self):
        clip = self.contact_clip()
        # Check every validation sample through actual final FK and receipts.
        for time in np.linspace(0., 1., 7, endpoint=False):
            evaluated = evaluate_animation(self.rig, clip, float(time))
            self.assert_final_receipts(clip, evaluated)
            self.assertFalse(evaluated['contacts'][0]['converged'])
            self.assertTrue(all(row['converged'] for row in evaluated['final_contacts']))
        metrics = contact_metrics(self.rig, clip)
        json.dumps(metrics, allow_nan=False)
        self.assertEqual(metrics['samples'], 14)
        self.assertEqual(metrics['orientation_samples'], 14)
        self.assertEqual(metrics['clip_content_sha256'], object_hash(clip))
        self.assertLess(metrics['max_contact_error_m'], 1e-7)
        self.assertLess(metrics['max_contact_orientation_error_rad'], 1e-7)
        self.assertTrue(metrics['orientation_converged'])
        self.assertTrue(all(row['orientation_converged'] for row in metrics['contacts']))

    def test_overlapping_positional_contact_counts_errors_and_drift_match_fk(self):
        clip = self.contact_clip(oriented=False)
        for chain in self.rig.manifest['controllers']['ik']:
            chain['solver'] = 'two-bone'
        earlier, later = clip['contacts']
        # A target beyond full extension makes its initial residual nonzero.
        # Its maximum residual during overlap then differs from its planted drift.
        earlier['target'] = [0., 2.25, 0.]
        later.update(start=.04, end=.06, target=[1., 1., 0.])
        early_times = np.linspace(0., 1., 7, endpoint=False).tolist()
        late_times = np.linspace(.04, .06, 7, endpoint=False).tolist()
        times = sorted(early_times + late_times)
        endpoints = {t: evaluate_fk(self.rig.skeleton, evaluate_animation(
            self.rig, clip, t)['pose'])['b2'][:3, 3] for t in times}
        metrics = contact_metrics(self.rig, clip)
        self.assertEqual((metrics['samples'], metrics['final_samples']), (14, 21))
        rows = {row['id']: row for row in metrics['contacts']}
        self.assertEqual(set(rows), {earlier['id'], later['id']})
        errors, drifts = [], []
        for contact, active_times, expected_count in ((earlier, times, 14), (later, late_times, 7)):
            points = [endpoints[t] for t in active_times]
            errors.append(max(math.dist(p, contact['target']) for p in points))
            drifts.append(max(math.dist(p, points[0]) for p in points))
            row = rows[contact['id']]
            self.assertEqual((row['samples'], row['final_samples']), (7, expected_count))
            self.assertAlmostEqual(row['max_error_m'], errors[-1], delta=1e-12)
            self.assertAlmostEqual(row['max_drift_m'], drifts[-1], delta=1e-12)
        self.assertGreater(max(errors) - max(drifts), .1)
        report = validate_rig(self.rig)
        for actual in (metrics, report['metrics']):
            self.assertAlmostEqual(actual['max_contact_error_m'], max(errors), delta=1e-12)
            self.assertAlmostEqual(actual['max_planted_drift_m'], max(drifts), delta=1e-12)

    def test_validation_report_accepts_repaired_final_contacts(self):
        clip = self.contact_clip()
        report = validate_rig(self.rig)
        validate_contract(report, 'RigValidationReport')
        json.dumps(report, allow_nan=False)
        self.assertEqual(next(row['status'] for row in report['checks']
                              if row['name'] == 'contacts'), 'passed')
        self.assertTrue(report['ok'])
        prefix = f"contact_orientation/{clip['id']}/{object_hash(clip)}/"
        self.assertTrue(report['metrics'][prefix+'converged'])
        self.assertEqual(report['metrics'][prefix+'samples'], 14)
        self.assertLess(report['metrics'][prefix+'max_error_rad'], 1e-7)

    def test_unrepaired_final_solver_failure_still_fails_validation(self):
        clip = self.contact_clip(repaired=False)
        evaluated = evaluate_animation(self.rig, clip, .5)
        self.assert_final_receipts(clip, evaluated)
        self.assertTrue(evaluated['final_contacts'][0]['reachable'])
        self.assertFalse(evaluated['final_contacts'][0]['converged'])
        self.assertGreater(evaluated['final_contacts'][0]['residual_m'], .01)
        metrics = contact_metrics(self.rig, clip)
        self.assertFalse(metrics['orientation_converged'])
        report = validate_rig(self.rig)
        validate_contract(report, 'RigValidationReport')
        self.assertFalse(report['ok'])
        self.assertEqual(next(row['status'] for row in report['checks']
                              if row['name'] == 'contacts'), 'failed')

    def test_final_unreachable_goal_below_legacy_contact_bound_still_fails(self):
        clip = self.contact_clip()
        for contact in clip['contacts']:
            contact['target'] = [0., 2.001, 0.]
        validate_animation(self.rig, clip)
        evaluated = evaluate_animation(self.rig, clip, .5)
        self.assert_final_receipts(clip, evaluated)
        for row in evaluated['final_contacts']:
            self.assertFalse(row['reachable'])
            self.assertFalse(row['converged'])
            self.assertAlmostEqual(row['residual_m'], .001, delta=1e-12)
            self.assertLess(row['orientation_residual_rad'], 1e-7)
        metrics = contact_metrics(self.rig, clip)
        report = validate_rig(self.rig)
        validate_contract(report, 'RigValidationReport')
        # Final position is inside the old geometry-scaled bound, but outside
        # native reach. A final-angle-only validator must not admit this pose.
        self.assertLess(metrics['max_contact_error_m'], report['metrics']['contact_tolerance_m'])
        self.assertFalse(metrics['orientation_converged'])
        self.assertFalse(report['ok'])
        self.assertEqual(next(row['status'] for row in report['checks']
                              if row['name'] == 'contacts'), 'failed')

    def test_position_only_repair_keeps_legacy_orientation_absence(self):
        clip = self.contact_clip(oriented=False)
        evaluated = evaluate_animation(self.rig, clip, .5)
        self.assert_final_receipts(clip, evaluated)
        self.assertFalse(evaluated['contacts'][0]['converged'])
        self.assertTrue(all(row['converged'] for row in evaluated['final_contacts']))
        metrics = contact_metrics(self.rig, clip)
        self.assertEqual(metrics['orientation_samples'], 0)
        self.assertIsNone(metrics['max_contact_orientation_error_rad'])
        self.assertTrue(all(row['orientation_converged'] is None for row in metrics['contacts']))
        self.assertTrue(validate_rig(self.rig)['ok'])


if __name__ == '__main__':
    unittest.main()
