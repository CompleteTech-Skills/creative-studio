"""Numerical compositing tests; native renderer proof is an external gate."""
from pathlib import Path
import sys
import unittest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from silent_render3d import composite_srgb


class LinearCompositeTests(unittest.TestCase):
    def test_linear_over_preserves_opaque_and_transparent_and_half_coverage(self):
        image = Image.new('RGBA', (3, 1))
        image.putdata([(255, 255, 255, 128), (255, 255, 255, 0), (255, 255, 255, 255)])
        result = composite_srgb(image, [0, 0, 0])
        self.assertEqual(list(result.getdata()), [(188, 188, 188), (0, 0, 0), (255, 255, 255)])
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


if __name__ == '__main__': unittest.main()
