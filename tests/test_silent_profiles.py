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


import os
import tempfile
from PIL import Image


@unittest.skipUnless(os.environ.get('HATCH_SILENT_MEDIA_TEST') == '1', 'explicit actual FFmpeg control gate')
class ActualEncoderControls(unittest.TestCase):
    def test_all_codecs_quality_bitrate_constrained_two_pass_and_real_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for index in range(6):
                name = '%06d.png' % index
                image = Image.new('RGB', (64, 48), (30 + 15 * index, 40, 90))
                image.paste((220, 180, 40), (index * 5, 12, index * 5 + 10, 32))
                image.save(root / name)
                paths.append(name)
            source = video.build_source(root, paths, {'numerator': 24, 'denominator': 1})
            capabilities = video.capabilities()
            for name, profile in video.PROFILES.items():
                if capabilities['profiles'][name]['status'] != 'supported':
                    if name == 'av1-webm':
                        continue  # Optional, honestly reported by capabilities.
                    self.fail('required encoder unavailable: ' + name)
                for control in ({'mode':'quality','value':20 if name=='h264-mp4' else 30},
                                {'mode':'bitrate','bitrate':100000},
                                {'mode':'constrained','bitrate':100000,'maxrate':200000,'bufsize':400000},
                                {'mode':'two-pass','bitrate':100000}):
                    with self.subTest(profile=name, mode=control['mode']):
                        settings = profiles.quality_settings(profile, 'preview')
                        settings['rate_control'] = control
                        request = video.make_request(source, name, encoding=settings)
                        output = root / (name + '-' + control['mode'] + profile['extension'])
                        events = []
                        receipt = video.encode(request, root, output, progress=events.append)
                        self.assertEqual(receipt['frame_count'], 6)
                        self.assertEqual(receipt['audio_stream_count'], 0)
                        actual = [event for event in events if event['phase'] == 'encoding']
                        self.assertTrue(actual)
                        self.assertEqual(actual[-1]['frame'], 6)
                        self.assertEqual(actual[-1]['status'], 'end')
                        if control['mode'] == 'two-pass':
                            self.assertEqual(len(receipt['passes']), 2)
                            self.assertTrue(any(event['phase'] == 'encode-pass-1' for event in events))
                        video.validate_receipt(receipt, output, request, root)
