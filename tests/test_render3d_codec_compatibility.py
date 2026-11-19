"""Synthetic native-writer EXR compatibility controls, not rendered artwork.

Public verifier acceptance covers its existing RGBA and scalar-Z contracts.
The separately labeled native control exercises DWA custom/layered names without
claiming that those channel layouts are admitted by the public verifier.
"""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from render3d_verify import decode_image, verify_image

try:
    import numpy as np
    import OpenEXR
    DEPENDENCIES = True
except ImportError:
    DEPENDENCIES = False


# Compression IDs 0..9, with the native codec's scanline block height. Every
# multi-line codec gets a second chunk whose last block is only partly filled.
CODECS = (
    ('NO', 1), ('RLE', 1), ('ZIPS', 1), ('ZIP', 16), ('PIZ', 32),
    ('PXR24', 16), ('B44', 32), ('B44A', 32), ('DWAA', 32), ('DWAB', 256),
)


def output(depth, pass_name):
    return dict(format='exr', bit_depth=depth, color_space='linear',
                alpha='straight' if pass_name == 'combined' else 'opaque',
                passes=[pass_name])


def samples(width, height, depth):
    """Vary every row/channel, including nonbinary FLOAT values and HDR blue."""
    y, x = np.indices((height, width))
    red = .1234567 + (x % 9) * .0612345 + (y % 11) * .0323456
    dtype = np.float16 if depth == 16 else np.float32
    return {name: np.asarray(values, dtype=dtype) for name, values in dict(
        R=red, G=red * 1.25 + .125, B=red * 2 + 1,
        A=.25 + ((x + y) % 7) * .0512345,
    ).items()}


def tolerance(codec, depth):
    # These are bounds for the small finite fixtures, not codec quality claims.
    # PXR24 rounds FLOAT mantissas; B44/B44A quantize HALF blocks; DWA's RGB
    # transform is lossy at both precisions. Other combinations are lossless.
    if codec == 'PXR24' and depth == 32:
        return dict(rtol=3e-5, atol=1e-6)
    if codec in ('B44', 'B44A') and depth == 16:
        return dict(rtol=.02, atol=.04)
    if codec in ('DWAA', 'DWAB'):
        return dict(rtol=.02, atol=.04)
    return dict(rtol=0, atol=0)


@unittest.skipUnless(DEPENDENCIES, 'optional OpenEXR/numpy packages unavailable')
class EXRCodecCompatibility(unittest.TestCase):
    def write_fixture(self, path, codec, values):
        # The writer replaces entries in its input dict with Channel objects;
        # retain separate source arrays for independent numerical assertions.
        compression = getattr(OpenEXR, codec + '_COMPRESSION')
        with OpenEXR.File({'compression': compression}, dict(values)) as writer:
            writer.write(str(path))
        with OpenEXR.File(str(path), separate_channels=True) as native:
            self.assertEqual(native.header()['compression'], compression)
            self.assertEqual(set(native.channels()), set(values))
            for name, expected in values.items():
                actual = native.channels()[name].pixels
                self.assertEqual(actual.shape, expected.shape)
                self.assertEqual(actual.dtype, expected.dtype)
                self.assertTrue(np.isfinite(actual).all())
                np.testing.assert_allclose(actual, expected,
                                           **tolerance(codec, expected.dtype.itemsize * 8))

    def check_public_fixture(self, codec, depth, width, height, pass_name):
        source = samples(width, height, depth)
        values = source if pass_name == 'combined' else {'Z': source['B']}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic.exr'
            self.write_fixture(path, codec, values)
            payload = path.read_bytes()
            kwargs = dict(output=output(depth, pass_name), resolution=[width, height],
                          pass_name=pass_name)
            verified = verify_image(path, **kwargs)
            decoded, pixels = decode_image(path, **kwargs)
            self.assertEqual(verified, decoded)
            self.assertEqual(decoded['format'], 'exr')
            self.assertEqual(decoded['bit_depth'], depth)
            self.assertEqual(decoded['pass_name'], pass_name)
            self.assertEqual(decoded['resolution'], [width, height])
            self.assertEqual(decoded['bytes'], len(payload))
            self.assertEqual(decoded['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual(set(decoded['channels']), set(values))
            self.assertEqual(set(pixels), set(values))
            self.assertEqual(sum(array.size for array in pixels.values()),
                             width * height * len(values))
            for name, expected in values.items():
                with self.subTest(channel=name):
                    actual = pixels[name]
                    self.assertEqual(actual.shape, (height, width))
                    self.assertTrue(np.isfinite(actual).all())
                    np.testing.assert_allclose(actual, expected, **tolerance(codec, depth))
                    statistics = decoded['channels'][name]
                    self.assertEqual(statistics['type'], 'half' if depth == 16 else 'float')
                    self.assertEqual(statistics['minimum'], float(actual.min()))
                    self.assertEqual(statistics['maximum'], float(actual.max()))
            if pass_name == 'combined':
                self.assertGreater(float(pixels['B'].max()), 1)
                self.assertGreater(float(pixels['A'].min()), 0)
                self.assertLess(float(pixels['A'].max()), 1)

    def test_native_dwa_custom_and_layered_channel_format_controls(self):
        """Native interoperability only; these layouts are not public admission."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'native-layered.exr'
            for codec, block_height in CODECS[-2:]:
                for depth in (16, 32):
                    with self.subTest(codec=codec, depth=depth):
                        rgba = samples(7, block_height + 3, depth)
                        values = {f'diffuse.{name}': array for name, array in rgba.items()}
                        values.update(Z=rgba['B'], payload=rgba['R'])
                        self.write_fixture(path, codec, values)


def public_test(codec, block_height, depth, pass_name):
    def test(self):
        for width, height in ((3, 2), (7, block_height + 3)):
            with self.subTest(width=width, height=height):
                self.check_public_fixture(codec, depth, width, height, pass_name)
    return test


for _codec, _height in CODECS:
    for _depth in (16, 32):
        setattr(EXRCodecCompatibility, f'test_{_codec.lower()}_rgba_{_depth}',
                public_test(_codec, _height, _depth, 'combined'))
        if _codec in ('DWAA', 'DWAB'):
            setattr(EXRCodecCompatibility, f'test_{_codec.lower()}_scalar_z_{_depth}',
                    public_test(_codec, _height, _depth, 'depth'))


if __name__ == '__main__':
    unittest.main()
