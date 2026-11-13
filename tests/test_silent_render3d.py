"""Numerical compositing tests; native renderer proof is an external gate."""
from pathlib import Path
from copy import deepcopy
import sys
import tempfile
import unittest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from silent_render3d import composite_srgb, prepare
import silent_video as video


class LinearCompositeTests(unittest.TestCase):
    def test_linear_over_preserves_opaque_and_transparent_and_half_coverage(self):
        image = Image.new('RGBA', (3, 1))
        image.putdata([(255, 255, 255, 128), (255, 255, 255, 0), (255, 255, 255, 255)])
        result = composite_srgb(image, [0, 0, 0])
        self.assertEqual([result.getpixel((index, 0)) for index in range(3)],
                         [(188, 188, 188), (0, 0, 0), (255, 255, 255)])
        self.assertEqual(result.mode, 'RGB')

    def test_arbitrary_background_and_rgb_identity(self):
        transparent = Image.new('RGBA', (2, 40), (220, 80, 20, 0))
        result = composite_srgb(transparent, [12, 128, 240])
        self.assertEqual(result.getpixel((0, 0)), (12, 128, 240))
        self.assertEqual(result.getpixel((1, 39)), (12, 128, 240))
        rgb = Image.new('RGB', (2, 40), (55, 113, 240))
        self.assertEqual(composite_srgb(rgb, [0, 0, 0]).tobytes(), rgb.tobytes())

    def test_background_is_explicit_and_bounded(self):
        for value in ([0, 0], [0, 0, 256], [0, True, 0], [0, 0, .5], 'black'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                composite_srgb(Image.new('RGBA', (1, 1)), value)

    def test_unsupported_or_excessive_native_inventory_fails_before_decode(self):
        # Only malformed admission metadata is exercised here: no mock worker
        # output or synthetic metadata is counted as native render acceptance.
        request = {'settings': {'resolution': [64, 48], 'output': {
            'format': 'png', 'bit_depth': 8, 'color_space': 'srgb',
            'passes': ['combined'], 'alpha': 'straight'}},
            'fps': {'numerator': 24, 'denominator': 1}, 'frame_range': {'start': 0, 'end': 1}}
        result = {'images': [{'frame': 0, 'pass_name': 'combined', 'format': 'png', 'bit_depth': 8,
            'resolution': [64, 48], 'file': {'path': 'frames/000000.png', 'sha256': '0' * 64, 'bytes': 64}}]}
        cases = []
        for field, value in [('format', 'exr'), ('bit_depth', 16), ('color_space', 'linear')]:
            invalid = deepcopy(request)
            invalid['settings']['output'][field] = value
            cases.append((invalid, result, 'sRGB PNG8'))
        invalid = deepcopy(request)
        invalid['fps']['numerator'] = 0
        cases.append((invalid, result, 'fps'))
        invalid = deepcopy(request)
        invalid['frame_range']['end'] = video.MAX_FRAMES + 1
        cases.append((invalid, result, 'frame range'))
        invalid = deepcopy(result)
        invalid['images'][0]['file']['bytes'] = video.MAX_FRAME_BYTES + 1
        cases.append((request, invalid, 'bounds'))
        many = deepcopy(request)
        many['frame_range']['end'] = 33
        images = []
        for index in range(33):
            item = deepcopy(result['images'][0])
            item.update(frame=index)
            item['file'].update(path='frames/%06d.png' % index, bytes=video.MAX_FRAME_BYTES)
            images.append(item)
        cases.append((many, {'images': images}, 'aggregate'))
        with tempfile.TemporaryDirectory() as tmp:
            for invalid_request, invalid_result, message in cases:
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    prepare(invalid_request, invalid_result, None, Path(tmp) / 'new',
                            snapshot_root=None, expected_backend=None)
            self.assertFalse((Path(tmp) / 'new').exists())


if __name__ == '__main__': unittest.main()
