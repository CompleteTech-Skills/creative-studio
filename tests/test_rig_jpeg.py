"""Actual warning-aware JPEG decode, preservation and bounded admission."""
import hashlib
import base64
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import simplejpeg

import rig_jpeg
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


def jpeg(mode='RGB', progressive=False, size=(32, 32)):
    rng = np.random.default_rng(7)
    channels = {'L': 1, 'RGB': 3, 'CMYK': 4}[mode]
    shape = (*size[::-1], channels) if channels > 1 else size[::-1]
    image = Image.fromarray(rng.integers(0, 256, shape, dtype=np.uint8), mode=mode)
    output = io.BytesIO()
    image.save(output, format='JPEG', progressive=progressive)
    return output.getvalue()


def verify(data, **options):
    return rig_jpeg.verify_jpeg(data, **({'max_encoded_bytes': 1024 * 1024,
                                        'max_working_bytes': 64 * 1024 * 1024} | options))


class StrictJpegTests(unittest.TestCase):
    def test_real_baseline_progressive_colors_and_sizes_preserve_bytes(self):
        for mode in ('L', 'RGB', 'CMYK'):
            for progressive in (False, True):
                for size in ((1, 1), (32, 32), (31, 17)):
                    with self.subTest(mode=mode, progressive=progressive, size=size):
                        data = jpeg(mode, progressive, size)
                        before = hashlib.sha256(data).hexdigest()
                        report = verify(data, expected_size=size)
                        self.assertEqual((report['width'], report['height']), size)
                        self.assertEqual(report['sha256'], before)
                        self.assertEqual(report['encoded_bytes'], len(data))
                        self.assertTrue(report['strict_warnings'])
                        self.assertEqual(report['decoder_version'], '1.9.0')
                        self.assertEqual(hashlib.sha256(data).hexdigest(), before)

    def test_actual_entropy_damage_recovered_by_pillow_is_rejected(self):
        original = (ROOT/'tests/fixtures/strict-jpeg-valid.jpg').read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         'f7aa64cf9f5f369a45ee48c602577ff4f7d7da1bdb1fc674ea907dae6cd284ec')
        # Removing just EOI and putting it back is the exact valid control.
        self.assertEqual(verify(original[:-2] + b'\xff\xd9')['width'], 32)
        for cut in (5, 20, 100, 300):
            with self.subTest(cut=cut):
                damaged = original[:-cut] + b'\xff\xd9'
                digest = hashlib.sha256(damaged).hexdigest()
                with Image.open(io.BytesIO(damaged)) as image:
                    image.verify()
                with Image.open(io.BytesIO(damaged)) as image:
                    image.load()  # Actual original silent-recovery behavior.
                self.assertEqual(simplejpeg.decode_jpeg(damaged, strict=False).shape, (32, 32, 3))
                with self.assertRaises(RigError) as caught:
                    verify(damaged)
                self.assertEqual(caught.exception.report['code'], 'RIG_TEXTURE_DECODE')
                self.assertIn('premature', caught.exception.report['message'])
                self.assertEqual(hashlib.sha256(damaged).hexdigest(), digest)

    def test_actual_arithmetic_baseline_and_progressive_fixtures(self):
        expected = {
            'strict-jpeg-arithmetic.jpg': '6b3145c4fdf55376dd5a13cca8fd9afc6d37aecbff02084949841d96340b09f6',
            'strict-jpeg-arithmetic-progressive.jpg': 'e9e6439d8160dbdb6f0275c590b8f463f32917a905eedb5e891a20d5ff9cf809',
        }
        for name, digest in expected.items():
            data = (ROOT/'tests/fixtures'/name).read_bytes()
            report = verify(data)
            self.assertEqual(report['sha256'], digest)
            self.assertEqual((report['width'], report['height']), (32, 32))

    def test_header_pixel_working_and_identity_limits_precede_pixel_decode(self):
        data = jpeg()
        for options in ({'max_pixels': 1023}, {'max_working_bytes': 1024 * 16 - 1},
                        {'expected_size': (31, 32)}, {'max_encoded_bytes': len(data) - 1}):
            with self.subTest(options=options), patch.object(simplejpeg, 'decode_jpeg') as decode:
                with self.assertRaises(RigError): verify(data, **options)
                decode.assert_not_called()
        report = verify(data, max_pixels=1024, max_working_bytes=1024 * 16,
                        max_encoded_bytes=len(data))
        self.assertEqual(report['estimated_working_bytes'], 1024 * 16)

    def test_oversized_actual_sof_dimensions_reject_before_full_decode(self):
        data = bytearray(jpeg())
        marker = data.index(b'\xff\xc0')
        data[marker + 5:marker + 9] = (65000).to_bytes(2, 'big') * 2
        with patch.object(simplejpeg, 'decode_jpeg') as decode:
            with self.assertRaises(RigError) as caught: verify(bytes(data))
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_TEXTURE')
            decode.assert_not_called()

    def test_progressive_scan_limit_and_metadata_markers_are_not_confused(self):
        data = jpeg(progressive=True)
        count = verify(data)['scan_count']
        self.assertGreater(count, 1)
        self.assertEqual(verify(data, max_scans=count)['scan_count'], count)
        with patch.object(simplejpeg, 'decode_jpeg_header') as decode:
            with self.assertRaises(RigError) as caught: verify(data, max_scans=count - 1)
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_TEXTURE')
            decode.assert_not_called()
        metadata = b'opaque\xff\xda' * 1000
        data = data[:2] + b'\xff\xed' + (len(metadata) + 2).to_bytes(2, 'big') + metadata + data[2:]
        self.assertEqual(verify(data, max_scans=count)['scan_count'], count)

    def test_structural_damage_and_trailing_data_are_explicit(self):
        data = jpeg()
        for bad in (b'', b'not jpeg', b'\xff\xd8', data[:-2], data + b'garbage',
                    b'\xff\xd8\xff\xe1\x00\x01\xff\xd9',
                    b'\xff\xd8\xff\xe1\xff\xff\xff\xd9'):
            with self.subTest(length=len(bad)), self.assertRaises(RigError):
                verify(bad)

    def test_all_native_decoder_calls_explicitly_enable_strict_warnings(self):
        with patch.object(simplejpeg, 'decode_jpeg_header', wraps=simplejpeg.decode_jpeg_header) as header:
            with patch.object(simplejpeg, 'decode_jpeg', wraps=simplejpeg.decode_jpeg) as decode:
                verify(jpeg())
                self.assertIs(header.call_args.kwargs['strict'], True)
                self.assertIs(decode.call_args.kwargs['strict'], True)

    def test_missing_or_untested_dependency_never_looks_verified(self):
        with patch.object(rig_jpeg, 'import_module', side_effect=ImportError('absent')):
            with self.assertRaises(RigError) as caught: verify(jpeg())
            self.assertEqual(caught.exception.report['code'], 'RIG_TEXTURE_VERIFIER_UNAVAILABLE')
        with patch.object(rig_jpeg.metadata, 'version', return_value='0.0.0'):
            with self.assertRaises(RigError) as caught: verify(jpeg())
            self.assertEqual(caught.exception.report['code'], 'RIG_TEXTURE_VERIFIER_UNAVAILABLE')

    def test_noninteger_boolean_and_huge_limits_are_rejected_structurally(self):
        for key in ('max_encoded_bytes', 'max_working_bytes', 'max_pixels', 'max_scans'):
            for value in (True, False, 0, -1, 1.0, float('nan'), 10 ** 400):
                with self.subTest(key=key, value=str(value)), self.assertRaises(RigError):
                    verify(jpeg(), **{key: value})
        for size in ([32, 32], (32.0, 32), (True, 32), (0, 32)):
            with self.assertRaises(RigError): verify(jpeg(), expected_size=size)

    def test_importing_boundary_does_not_import_3d_decoder_dependencies(self):
        code = """
import importlib.abc,sys
class NoOptional(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name.split('.')[0] in ('numpy','simplejpeg','PIL'): raise RuntimeError(name)
sys.meta_path.insert(0,NoOptional())
import rig_jpeg
assert not any(x in sys.modules for x in ('numpy','simplejpeg','PIL'))
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT/'scripts',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_missing_package_environment_returns_actionable_failure(self):
        # -S deliberately excludes every site package, not just a mocked import.
        data = base64.b64encode(jpeg()).decode('ascii')
        code = f"""
import base64
from rig_jpeg import verify_jpeg
from rig_errors import RigError
try:
 verify_jpeg(base64.b64decode({data!r}), max_encoded_bytes=100000, max_working_bytes=100000)
except RigError as exc:
 assert exc.report['code'] == 'RIG_TEXTURE_VERIFIER_UNAVAILABLE', exc.report
 assert 'requirements-rig.txt' in exc.report['correction']
else: raise AssertionError('Missing optional decoder was treated as verified')
"""
        result = subprocess.run([sys.executable, '-S', '-c', code], cwd=ROOT/'scripts',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
