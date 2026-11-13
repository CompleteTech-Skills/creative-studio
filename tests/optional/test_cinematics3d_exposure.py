from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
import cinematics3d_color as color
import cinematics3d_exposure as exposure

ROOT = Path(__file__).resolve().parents[2]


class ExposureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT/'tests/fixtures/cinematics-color/profile'
        cls.inventory = color.capture_config(cls.root)
        cls.source = dict(kind='cinematics-color-source', schema_version=1,
                          config_closure_sha256=cls.inventory['closure_sha256'], working_space='Linear Rec.709',
                          primaries=deepcopy(color.REC709_D65), transfer='linear', alpha_mode='straight',
                          view_applied=False, exposure_applied=False,
                          source_manifest_sha256='a'*64, source_artifact_sha256='b'*64)

    def meter(self, value=.18, alpha=1., **kwargs):
        pixels = np.array([[value, value, value, alpha]], dtype=np.float32)
        return exposure.measure(self.root, self.inventory, self.source, pixels, **kwargs)

    def test_actual_profile_neutral_gray_and_hdr_measurements(self):
        gray = self.meter()
        self.assertAlmostEqual(gray['log2_mean_luminance'], math.log2(.18), places=5)
        self.assertAlmostEqual(sum(gray['histogram_alpha_weights']), 1.)
        self.assertEqual(gray['visible_over_one_pixels'], 0)
        hdr = self.meter(16.)
        self.assertAlmostEqual(hdr['log2_mean_luminance'], 4., places=5)
        self.assertEqual(hdr['visible_over_one_pixels'], 1)
        self.assertFalse(hdr['source_render_verified'])

    def test_alpha_weighted_meter_and_premultiplied_input_match(self):
        pixels = np.array([[.18,.18,.18,1.], [1.,1.,1.,.25], [100.,100.,100.,0.]], dtype=np.float32)
        expected = (math.log2(.18)+.25*math.log2(1.))/1.25
        meter = exposure.measure(self.root, self.inventory, self.source, pixels)
        self.assertAlmostEqual(meter['log2_mean_luminance'], expected, places=5)
        self.assertEqual(meter['transparent_pixels'], 1)
        source = deepcopy(self.source); source['alpha_mode'] = 'premultiplied'
        pixels[:,:3] *= pixels[:,3:4]
        actual = exposure.measure(self.root, self.inventory, source, pixels)
        self.assertAlmostEqual(actual['log2_mean_luminance'], expected, places=5)

    def test_zero_negative_and_fully_transparent_are_explicit(self):
        for value in (0., -1.):
            meter = self.meter(value)
            self.assertAlmostEqual(meter['log2_mean_luminance'], -40.)
            self.assertEqual(meter['nonpositive_visible_pixels'], 1)
        empty = self.meter(100., alpha=0.)
        self.assertIsNone(empty['log2_mean_luminance'])
        advice = exposure.advise(empty, Fraction(0), enabled=True)
        self.assertEqual(advice['correction_stops'], 0.)

    def test_authored_animation_stays_separate_and_applies_only_through_color(self):
        meter = self.meter(.09)
        advice = exposure.advise(meter, Fraction(0), enabled=True, authored_stops=2.)
        self.assertAlmostEqual(advice['correction_stops'], 1., places=5)
        self.assertEqual(advice['authored_stops'], 2.)
        self.assertAlmostEqual(advice['total_stops'], 3., places=5)
        self.assertEqual(advice['exposure_applications'], 0)
        pixels = np.array([[.09,.09,.09,1.]], dtype=np.float32)
        plan = color.make_plan(self.root, self.inventory, self.source, target='scene-linear', display='', view='',
                               exposure_stops=advice['total_stops'])
        output, receipt = color.transform_pixels(self.root, plan, pixels)
        np.testing.assert_allclose(output[0,:3], .72, atol=1e-5)
        self.assertEqual(receipt['exposure_applications'], 1)
        disabled = exposure.advise(meter, Fraction(0), authored_stops=2.)
        self.assertEqual(disabled['total_stops'], 2.)

    def test_temporal_stabilization_resume_and_exact_rational_clock(self):
        meter = self.meter(.18)
        initial = exposure.advise(meter, Fraction(0), enabled=True)
        dark = self.meter(.09)
        time = Fraction(1001, 30000)
        next_state = exposure.advise(dark, time, previous=initial, enabled=True)
        expected = -math.expm1(-float(time)/.5)
        self.assertAlmostEqual(next_state['correction_stops'], expected, places=5)
        self.assertEqual(next_state['source_time'], dict(numerator=1001, denominator=30000))
        self.assertEqual(next_state, exposure.advise(dark, time, previous=deepcopy(initial), enabled=True))
        reset = exposure.advise(dark, Fraction(0), previous=next_state, enabled=True, reset=True)
        self.assertAlmostEqual(reset['correction_stops'], 1., places=5)
        self.assertIsNone(reset['previous_state_sha256'])

    def test_seek_policy_sequence_and_switch_changes_require_reset(self):
        meter = self.meter()
        state = exposure.advise(meter, Fraction(1), enabled=True)
        for kwargs in (dict(source_time=Fraction(1)), dict(source_time=Fraction(0)),
                       dict(source_time=Fraction(2), sequence_id='other'),
                       dict(source_time=Fraction(2), enabled=False),
                       dict(source_time=Fraction(2), settings=exposure.policy(target_gray=.2))):
            args = dict(source_time=Fraction(2), enabled=True); args.update(kwargs)
            with self.assertRaises(ValueError):
                exposure.advise(meter, previous=state, **args)

    def test_bounds_and_malformed_history_reject(self):
        meter = self.meter()
        for clock in (0., True, Fraction(-1), Fraction(2**54), Fraction(1,2**54)):
            with self.assertRaises(ValueError):
                exposure.advise(meter, clock)
        for value in (10**400, -(10**400), float('nan'), True):
            with self.assertRaises(ValueError):
                exposure.policy(target_gray=value)
        state = exposure.advise(meter, Fraction(0), enabled=True)
        forged = deepcopy(state); forged['correction_stops'] = 100.
        with self.assertRaises(ValueError):
            exposure.advise(meter, Fraction(1), previous=forged, enabled=True)
        forged = deepcopy(meter); forged['log2_mean_luminance'] = 20.
        with self.assertRaises(ValueError):
            exposure.advise(forged, Fraction(0))
        with self.assertRaises(ValueError):
            exposure.advise(self.meter(0.), Fraction(0), enabled=True, authored_stops=24.)

    def test_forged_record_complexity_is_bounded_before_hashing(self):
        meter = self.meter()
        forged = deepcopy(meter); forged['color_execution'] = {'large': 'x'*4097}
        with patch.object(exposure, 'object_hash', side_effect=AssertionError('hashing started')):
            with self.assertRaises(ValueError):
                exposure.advise(forged, Fraction(0))
        forged = deepcopy(meter); forged['histogram_alpha_weights'] = [0.]*257
        with self.assertRaises(ValueError):
            exposure.advise(forged, Fraction(0))

    def test_meter_preflight_precedes_optional_ocio_processing(self):
        pixels = np.ones((1,4), dtype=np.float32)
        with patch.object(color, 'make_plan', side_effect=AssertionError('processing started')):
            for value in (pixels.astype(np.uint8), pixels[:,:3], np.ones((0,4),dtype=np.float32),
                          np.ones((exposure.MAX_PIXELS+1,4),dtype=np.float32)):
                with self.assertRaises(ValueError):
                    exposure.measure(self.root, self.inventory, self.source, value)
            with self.assertRaises(ValueError):
                exposure.measure(self.root, self.inventory, self.source, pixels, max_work_bytes=1)
        for pixels in (np.array([[float('nan'),0,0,1]],dtype=np.float32), np.array([[0,0,0,2]],dtype=np.float32)):
            with self.assertRaises(ValueError):
                exposure.measure(self.root,self.inventory,self.source,pixels)


if __name__ == '__main__':
    unittest.main()
