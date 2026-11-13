"""Codec-control admission; actual encodes belong to the opt-in media gate."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_profiles as profiles
import silent_video as video


class ProfileControlTests(unittest.TestCase):
    def test_quality_levels_use_codec_specific_scales_and_bounded_threads(self):
        expected = {'h264-mp4': 20, 'vp9-webm': 30, 'av1-webm': 32}
        for name, value in expected.items():
            result = profiles.quality_settings(video.PROFILES[name], fps={'numerator': 60000, 'denominator': 1001})
            self.assertEqual(result['rate_control'], {'mode': 'quality', 'value': value})
            self.assertEqual(result['gop'], 120)
        with self.assertRaises(ValueError):
            profiles.quality_settings(video.PROFILES['h264-mp4'], threads=3)

    def test_codec_speed_quality_and_bitrate_constraints_are_not_interchangeable(self):
        base = profiles.quality_settings(video.PROFILES['h264-mp4'])
        for field, value in [('speed', 6), ('speed', 'medium -map 1:a'), ('threads', True), ('gop', 0)]:
            controls = deepcopy(base); controls[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                profiles.validate_encoding(video.PROFILES['h264-mp4'], controls)
        controls = deepcopy(base); controls['rate_control']['value'] = 52
        with self.assertRaises(ValueError): profiles.validate_encoding(video.PROFILES['h264-mp4'], controls)
        controls = deepcopy(base); controls['rate_control'] = {'mode':'constrained','bitrate':500000,'maxrate':100000,'bufsize':1000000}
        with self.assertRaises(ValueError): profiles.validate_encoding(video.PROFILES['h264-mp4'], controls)
        controls = deepcopy(base); controls['audio'] = 'forbidden'
        with self.assertRaises(ValueError): profiles.validate_encoding(video.PROFILES['h264-mp4'], controls)

    def test_excessive_numeric_controls_have_typed_admission_errors(self):
        base = profiles.quality_settings(video.PROFILES['h264-mp4'])
        for field in ('threads', 'gop'):
            controls = deepcopy(base)
            controls[field] = 10 ** 400
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'numeric/nesting'):
                profiles.validate_encoding(video.PROFILES['h264-mp4'], controls)
        for field in ('value', 'bitrate', 'maxrate', 'bufsize'):
            controls = deepcopy(base)
            controls['rate_control'] = {'mode': 'constrained', 'bitrate': 100000,
                                        'maxrate': 200000, 'bufsize': 400000}
            if field == 'value':
                controls['rate_control'] = {'mode': 'quality', 'value': 10 ** 400}
            else:
                controls['rate_control'][field] = 10 ** 400
            with self.subTest(field=field), self.assertRaises(ValueError):
                profiles.validate_encoding(video.PROFILES['h264-mp4'], controls)

    def test_named_profiles_capture_exact_versioned_controls_and_detect_tampering(self):
        for name, profile in video.PROFILES.items():
            for quality in profiles.QUALITY[profile['codec']]:
                captured = profiles.capture_quality_profile(profile, quality,
                    fps={'numerator': 30000, 'denominator': 1001}, threads=2)
                self.assertEqual(profiles.validate_quality_profile(captured), captured)
                self.assertEqual(captured['schema_version'], 1)
                self.assertEqual(captured['encoder'], profile['encoder'])
                changed = deepcopy(captured)
                changed['encoding']['gop'] += 1
                with self.assertRaises(ValueError): profiles.validate_quality_profile(changed)
                changed = deepcopy(captured); changed['schema_version'] = True
                with self.assertRaises(ValueError): profiles.validate_quality_profile(changed)
                changed = deepcopy(captured); changed['encoder'] = 'unprobed-hardware'
                with self.assertRaises(ValueError): profiles.validate_quality_profile(changed)
        captured['encoding']['threads'] = 1
        self.assertEqual(profiles.capture_quality_profile(video.PROFILES['av1-webm'], threads=2)['encoding']['threads'], 2)

    def test_named_profiles_reject_malformed_selection_and_rate(self):
        for field in ('codec', 'encoder'):
            for value in ([], {}, None, True):
                profile = deepcopy(video.PROFILES['h264-mp4']); profile[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    profiles.capture_quality_profile(profile)
        for rate in ([], None, {}, {'numerator': 10 ** 400, 'denominator': 1},
                     {'numerator': 24, 'denominator': 0}):
            if rate is None:
                continue  # None explicitly selects the default rate.
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                profiles.capture_quality_profile(video.PROFILES['h264-mp4'], fps=rate)

    def test_profile_rate_is_admitted_before_copying_untrusted_nested_values(self):
        nested = []
        for _ in range(1500): nested = [nested]
        with self.assertRaises(ValueError):
            profiles.capture_quality_profile(video.PROFILES['h264-mp4'],
                fps={'numerator': nested, 'denominator': 1})

    def test_storage_estimates_preserve_rational_duration_and_honest_uncertainty(self):
        profile = video.PROFILES['h264-mp4']
        settings = profiles.quality_settings(profile)
        estimate = profiles.estimate_storage(profile, settings, frame_count=30,
            fps={'numerator': 30000, 'denominator': 1001}, max_output_bytes=100000)
        self.assertIsNone(estimate['nominal_video_bytes'])
        self.assertIsNone(estimate['measured_sample'])
        self.assertEqual(estimate['duration'], {'numerator': 1001, 'denominator': 1000})
        self.assertEqual(estimate['output_byte_range'], {'minimum': 0, 'maximum': 100000})
        settings['rate_control'] = {'mode': 'two-pass', 'bitrate': 1000000}
        estimate = profiles.estimate_storage(profile, settings, frame_count=30,
            fps={'numerator': 30000, 'denominator': 1001}, max_output_bytes=100000)
        self.assertEqual(estimate['nominal_video_bytes'], 125125)
        self.assertTrue(estimate['nominal_target_exceeds_budget'])
        for count in (True, 0, 18001, 10 ** 400):
            with self.assertRaises(ValueError):
                profiles.estimate_storage(profile, settings, frame_count=count,
                    fps={'numerator': 24, 'denominator': 1})

    def test_generated_arguments_are_typed_and_never_accept_extra_flags(self):
        for name, profile in video.PROFILES.items():
            controls = profiles.quality_settings(profile)
            args = profiles.arguments(profile, controls)
            self.assertIn('-crf', args)
            self.assertIn('-threads', args)
            controls['rate_control'] = {'mode':'two-pass','bitrate':100000}
            self.assertIn('-b:v', profiles.arguments(profile, controls))
            self.assertNotIn('-crf', profiles.arguments(profile, controls))


if __name__ == '__main__': unittest.main()
