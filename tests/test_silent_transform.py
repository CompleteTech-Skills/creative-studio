"""Declared raster/FPS conversions; synthetic pixels are engineering fixtures."""
from copy import deepcopy
from fractions import Fraction
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_transform as transform
import silent_video as video


class _TransformFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = []
        for index in range(4):
            name = '%06d.png' % index
            image = Image.new('RGB', (64, 48), (30 + index * 20, 120, 80))
            image.save(self.root / name)
            self.paths.append(name)
        self.source = video.build_source(self.root, self.paths, {'numerator': 24, 'denominator': 1})
        self.output = self.root / 'converted'


class TransformTests(_TransformFixture):
    def test_preserve_requires_explicit_resize_and_fps_conversion_policies(self):
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            transform.transform(self.source, self.root, transform.profile(80, 80, self.source['fps']), self.output)
        with self.assertRaisesRegex(ValueError, 'FPS'):
            transform.transform(self.source, self.root, transform.profile(64, 48, {'numerator':30,'denominator':1}), self.output)
        for width, height in [(63,48), (4096,4096), (True,48), (0,48)]:
            with self.subTest(width=width), self.assertRaises(ValueError):
                transform.profile(width,height,self.source['fps'])
        self.assertFalse(self.output.exists())

    def test_fill_extreme_aspect_ratio_keeps_bounded_target_allocation(self):
        source=Image.new('RGB',(4096,2),(40,80,120))
        settings=transform.profile(2,4096,{'numerator':24,'denominator':1},fit='fill')
        output=transform._raster(source,settings)
        self.assertEqual(output.size,(2,4096));self.assertEqual(output.getpixel((1,2048)),(40,80,120))
        source.close();output.close()

    def test_customizing_returned_schemas_cannot_mutate_media_authority(self):
        profile_schema = transform.profile_schema()
        profile_schema['properties']['fps']['properties']['numerator']['maximum'] = 1
        source_schema = transform.schema()
        source_schema['properties']['source']['properties']['width']['maximum'] = 2
        transform.profile(64,48,self.source['fps'])
        video.validate_source(self.source)

if __name__ == '__main__': unittest.main()
