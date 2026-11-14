"""Real media and adversarial acceptance for the separate video-only export path.

All input images in this module are synthetic test fixtures, never production
art. The integration cases execute the installed FFmpeg and ffprobe binaries.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import zlib

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_video as video

MEDIA_AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and video.secure_publication_supported())
if os.environ.get('HATCH_SILENT_MEDIA_TEST') == '1' and not MEDIA_AVAILABLE:
    raise RuntimeError('HATCH_SILENT_MEDIA_TEST requires real FFmpeg and ffprobe')


class SilentContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='silent-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = []
        for index in range(6):
            path = self.root / ('%06d.png' % index)
            # Each frame has a distinct moving patch so pixel verification can
            # detect reordered or frozen content, not merely frame cardinality.
            image = Image.new('RGB', (64, 48), (18 + index * 20, 36, 80))
            image.paste((230, 200, 20), (index * 6, 12, index * 6 + 12, 32))
            image.save(path)
            self.paths.append(path.name)
        self.source = video.build_source(self.root, self.paths,
                                         fps={'numerator': 24, 'denominator': 1},
                                         origin='synthetic')
        self.request = video.make_request(self.source)

    def test_committed_schema_parity(self):
        root = Path(__file__).resolve().parents[1] / 'schemas' / 'silent-video-v1'
        actual = video.schemas()
        self.assertEqual(set(path.stem.removesuffix('.schema') for path in root.glob('*.schema.json')),
                         set(actual))
        for name, schema in actual.items():
            self.assertEqual(json.loads((root / (name + '.schema.json')).read_text()), schema)

    def test_default_request_is_explicitly_audio_forbidden(self):
        self.assertEqual(self.request['audio'], {'policy': 'forbidden'})
        self.assertEqual(self.request['profile'], 'h264-mp4')
        video.validate_request(self.request)

    def test_audio_policy_cannot_be_relaxed_or_silence_substituted(self):
        for policy in ('silent', 'muted', 'optional', 'allowed', None):
            with self.subTest(policy=policy):
                value = deepcopy(self.request)
                value['audio']['policy'] = policy
                with self.assertRaises(ValueError):
                    video.validate_request(value)

    def test_unknown_fields_and_injected_flags_are_rejected(self):
        for key, value in [('ffmpeg_args', ['-map', '1:a']),
                           ('audio_input', 'speech.wav'),
                           ('codec', 'copy'), ('container', 'mkv')]:
            with self.subTest(key=key):
                request = deepcopy(self.request)
                request[key] = value
                with self.assertRaises(ValueError):
                    video.validate_request(request)

    def test_unsupported_or_injected_profile_is_rejected(self):
        for profile in ('h264', 'h264-mp4 -map 1:a', '../mp4', 'audio-mp4', ''):
            with self.subTest(profile=profile):
                with self.assertRaises(ValueError):
                    video.make_request(self.source, profile=profile)

    def test_invalid_exact_fps_is_rejected(self):
        for fps in ({'numerator': 0, 'denominator': 1},
                    {'numerator': 24, 'denominator': 0},
                    {'numerator': True, 'denominator': 1},
                    {'numerator': 24.0, 'denominator': 1},
                    {'numerator': 24, 'denominator': 1, 'extra': 1}):
            with self.subTest(fps=fps):
                with self.assertRaises(ValueError):
                    video.build_source(self.root, self.paths, fps=fps,
                                       origin='synthetic')

    def test_unsafe_source_paths_fail_before_media_execution(self):
        for path in ('../outside.png', '/tmp/outside.png', 'C:\\outside.png',
                     'sub/../../outside.png'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    video.build_source(self.root, [path],
                                       fps={'numerator': 24, 'denominator': 1},
                                       origin='synthetic')

    def test_symlink_source_is_rejected(self):
        link = self.root / 'linked.png'
        try:
            link.symlink_to(self.root / self.paths[0])
        except (OSError, NotImplementedError):
            self.skipTest('OS does not permit test symlink creation')
        with self.assertRaises(ValueError):
            video.build_source(self.root, ['linked.png'],
                               fps={'numerator': 24, 'denominator': 1},
                               origin='synthetic')

    def test_non_image_source_is_rejected(self):
        (self.root / 'pretend.png').write_bytes(b'not an image')
        with self.assertRaises(ValueError):
            video.build_source(self.root, ['pretend.png'],
                               fps={'numerator': 24, 'denominator': 1},
                               origin='synthetic')

    def test_high_precision_png_is_rejected_before_lossy_pillow_decode(self):
        def chunk(kind, payload):
            return (struct.pack('>I', len(payload)) + kind + payload +
                    struct.pack('>I', zlib.crc32(kind + payload)))
        for color_type, pixel in ((2, b'\x12\x34\x56\x78\x9a\xbc'),
                                  (6, b'\x12\x34\x56\x78\x9a\xbc\xff\x00')):
            # Real 16-bit PNGs; ff00 alpha is nonopaque but Pillow presents
            # it as 255 after reducing precision to RGBA8.
            raw = b'\x00' + pixel * 2
            png = (b'\x89PNG\r\n\x1a\n' +
                   chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 16, color_type, 0, 0, 0)) +
                   chunk(b'IDAT', zlib.compress(raw * 2)) + chunk(b'IEND', b''))
            path = self.root / ('precision-%d.png' % color_type)
            path.write_bytes(png)
            with self.subTest(color_type=color_type):
                with Image.open(path) as im:
                    im.load()
                    self.assertIn(im.mode, ('RGB', 'RGBA'))
                with self.assertRaisesRegex(ValueError, '8-bit'):
                    video.build_source(self.root, [path.name],
                                       fps={'numerator': 24, 'denominator': 1})

    def test_rgb_png_transparency_is_not_silently_discarded(self):
        path = self.root / 'transparent-rgb.png'
        Image.new('RGB', (2, 2), (10, 20, 30)).save(path, transparency=(10, 20, 30))
        with Image.open(path) as im:
            self.assertEqual(im.mode, 'RGB')
            self.assertEqual(im.convert('RGBA').getchannel('A').getextrema(), (0, 0))
        with self.assertRaisesRegex(ValueError, 'transparency'):
            video.build_source(self.root, [path.name], fps={'numerator': 24, 'denominator': 1})

    def test_mismatched_frame_dimensions_are_rejected(self):
        Image.new('RGB', (32, 48)).save(self.root / self.paths[-1])
        with self.assertRaises(ValueError):
            video.build_source(self.root, self.paths,
                               fps={'numerator': 24, 'denominator': 1},
                               origin='synthetic')

    def test_missing_tools_are_reported_as_unavailable(self):
        with patch('silent_video.shutil.which', return_value=None):
            report = video.capabilities()
        self.assertIn('unavailable', json.dumps(report).lower())

    def test_huge_integer_fails_as_contract_error(self):
        request = deepcopy(self.request)
        request['limits']['timeout_seconds'] = 10**1000
        with self.assertRaises(ValueError):
            video.validate_request(request)

    def test_profile_schema_rejects_mixed_codec_and_container(self):
        import jsonschema
        invalid = deepcopy(video.PROFILES['h264-mp4'])
        invalid['container'] = 'webm'
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid, video.schemas()['profile'])

    def test_resource_limits_reject_unbounded_or_nonfinite_values(self):
        for key, value in [('max_output_bytes', 0), ('max_log_bytes', -1),
                           ('timeout_seconds', float('inf')),
                           ('timeout_seconds', True)]:
            request = deepcopy(self.request)
            request['limits'][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    video.validate_request(request)


if __name__ == '__main__':
    unittest.main()
