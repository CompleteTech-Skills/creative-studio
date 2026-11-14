"""Explicit actual FFmpeg codec-control gate; excluded from portable discovery."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import silent_profiles as profiles
import silent_video as video




import os
import tempfile
from PIL import Image


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


if __name__ == '__main__': unittest.main()
