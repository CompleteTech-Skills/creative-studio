"""Declared raster/FPS conversions; synthetic pixels are engineering fixtures."""
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import sys
import tempfile
import unittest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_transform as transform
import silent_video as video


class TransformTests(unittest.TestCase):
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

    def test_preserve_requires_explicit_resize_and_fps_conversion_policies(self):
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            transform.transform(self.source, self.root, transform.profile(80, 80, self.source['fps']), self.output)
        with self.assertRaisesRegex(ValueError, 'FPS'):
            transform.transform(self.source, self.root, transform.profile(64, 48, {'numerator':30,'denominator':1}), self.output)
        for width, height in [(63,48), (4096,4096), (True,48), (0,48)]:
            with self.subTest(width=width), self.assertRaises(ValueError):
                transform.profile(width,height,self.source['fps'])
        self.assertFalse(self.output.exists())

    def test_exact_nearest_duplicate_map_and_full_pixel_reverification(self):
        profile = transform.profile(64,48,{'numerator':48,'denominator':1},fps_policy='nearest')
        result = transform.transform(self.source, self.root, profile, self.output)
        self.assertEqual([item['source_index'] for item in result['mapping']], [0,1,1,2,2,3,3,3])
        self.assertEqual(result['duration_delta'], {'numerator':0,'denominator':1})
        self.assertEqual(transform.verify(self.output,source_root=self.root),result)
        self.assertEqual((self.output/'frames/000000.png').read_bytes(),(self.root/self.paths[0]).read_bytes())

    def test_nearest_duration_quantization_is_recorded_as_an_exact_fraction(self):
        self.source['fps']={'numerator':30,'denominator':1}
        profile=transform.profile(64,48,{'numerator':24,'denominator':1},fps_policy='nearest')
        result=transform.transform(self.source,self.root,profile,self.output)
        self.assertEqual(len(result['mapping']),3)
        self.assertEqual(result['duration_delta'],{'numerator':-1,'denominator':120})

    def test_letterbox_fill_and_opt_in_stretch_have_declared_geometry(self):
        for policy in ('letterbox','fill','stretch'):
            with self.subTest(policy=policy):
                settings=transform.profile(80,80,self.source['fps'],fit=policy)
                output=self.root/policy
                result=transform.transform(self.source,self.root,settings,output)
                transform.verify(output,source_root=self.root)
                with Image.open(output/'frames/000000.png') as image:
                    self.assertEqual(image.size,(80,80))
                    self.assertEqual(image.getpixel((40,40)),(30,120,80))
                    self.assertEqual(image.getpixel((40,0)),(0,0,0) if policy=='letterbox' else (30,120,80))
                self.assertEqual(result['source']['width'],80)

    def test_tampering_and_cancellation_never_become_verified(self):
        settings=transform.profile(64,48,self.source['fps'])
        with self.assertRaisesRegex(ValueError,'cancelled'):
            transform.transform(self.source,self.root,settings,self.output,cancel=lambda:True)
        self.assertFalse(self.output.exists())
        transform.transform(self.source,self.root,settings,self.output)
        path=self.output/'transform.json';original=json.loads(path.read_text())
        bad=deepcopy(original);bad['mapping'][0]['source_index']=1
        path.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):transform.verify(self.output)
        path.write_text(json.dumps(original))
        Image.new('RGB',(64,48),(240,0,0)).save(self.output/'frames/000000.png')
        with self.assertRaises(ValueError):transform.verify(self.output)

    def test_fill_extreme_aspect_ratio_keeps_bounded_target_allocation(self):
        source=Image.new('RGB',(4096,2),(40,80,120))
        settings=transform.profile(2,4096,{'numerator':24,'denominator':1},fit='fill')
        output=transform._raster(source,settings)
        self.assertEqual(output.size,(2,4096));self.assertEqual(output.getpixel((1,2048)),(40,80,120))
        source.close();output.close()


if __name__ == '__main__':unittest.main()
