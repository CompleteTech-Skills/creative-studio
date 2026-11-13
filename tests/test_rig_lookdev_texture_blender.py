"""Opt-in real texture rendering and native persistence, without a fake backend."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


@unittest.skipUnless(os.environ.get('HATCH_LOOKDEV_TEXTURE_TEST') == '1',
                     'Enable HATCH_LOOKDEV_TEXTURE_TEST=1 for actual texture renders.')
class TextureBlenderTests(unittest.TestCase):
    def test_normal_map_on_six_mesh_cases_and_packed_reopen(self):
        import numpy as np
        from PIL import Image, ImageChops, ImageStat
        from lookdev_textures import normal_from_height, encode_png, decode_texture
        root = Path(__file__).resolve().parents[1]
        blender = os.environ['HATCH_BLENDER']
        evidence = os.environ.get('HATCH_LOOKDEV_TEXTURE_EVIDENCE')
        temporary = None if evidence else tempfile.TemporaryDirectory(prefix='texture-native-')
        if temporary:
            self.addCleanup(temporary.cleanup)
        output = Path(evidence or temporary.name)
        output.mkdir(parents=True, exist_ok=True)
        self.assertFalse((output / 'job.json').exists(), 'Use a new evidence directory.')
        y, x = np.mgrid[:32, :32]
        height = (0.5 + 0.2 * np.sin(x * 2 * np.pi / 8) + 0.2 * np.cos(y * 2 * np.pi / 8)).astype(np.float32)
        source = encode_png(normal_from_height(height, strength=2))
        decoded = decode_texture(source, role='normal', encoding='data')
        (output / 'normal.png').write_bytes(source)
        job = {'scripts': str(root / 'scripts'), 'output': str(output),
               'pixels': decoded.pixels.tolist(), 'source_sha256': hashlib.sha256(source).hexdigest()}
        (output / 'job.json').write_text(json.dumps(job))
        result = subprocess.run([blender, '--background', '--factory-startup', '--disable-autoexec',
            '--threads', '1', '--python-exit-code', '1', '--python',
            str(root / 'tests/fixtures/lookdev_texture_probe.py'), '--', str(output / 'job.json')],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
        (output / 'render.log').write_text(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout[-5000:])
        report = json.loads((output / 'report.json').read_text())
        self.assertTrue(report['packed'])
        self.assertEqual(report['source_sha256'], job['source_sha256'])
        self.assertLess(report['pixel_max_error'], 1e-7)
        measurements = {}
        for case in ('curved', 'hard-edge', 'mirrored', 'rotated', 'nonuniform', 'deforming'):
            before = Image.open(output / (case + '-flat.png')).convert('RGB')
            after = Image.open(output / (case + '-normal.png')).convert('RGB')
            delta = sum(ImageStat.Stat(ImageChops.difference(before, after)).mean) / 3
            self.assertGreater(delta, 1, case)
            measurements[case] = delta
        for record in report['renders']:
            self.assertEqual(hashlib.sha256((output / record['file']).read_bytes()).hexdigest(), record['sha256'])
        (output / 'measurements.json').write_text(json.dumps(measurements, indent=2))


if __name__ == '__main__':
    unittest.main()
