"""Opt-in actual triplanar gate, outside ordinary 2D test discovery."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))

@unittest.skipUnless(os.environ.get('HATCH_LOOKDEV_TRIPLANAR_TEST') == '1', 'Enable actual triplanar shader gate.')
class NativeTriplanarTests(unittest.TestCase):
    def test_actual_axial_projection_blending_seam_and_packed_reopen(self):
        import numpy as np
        from lookdev_textures import encode_png
        repo = Path(__file__).resolve().parents[2]
        output = Path(os.environ['HATCH_LOOKDEV_TRIPLANAR_EVIDENCE'])
        output.mkdir(parents=True, exist_ok=True)
        self.assertFalse((output/'job.json').exists(), 'Choose fresh immutable evidence.')
        pixels = np.zeros((4,4,4), np.float32)
        for y in range(4):
            for x in range(4): pixels[y,x] = [(x+1)/5, (y+1)/5, .2, .5]
        encoded = encode_png(pixels, encoding='linear-srgb')
        document = json.loads((repo/'examples/lookdev-contracts/albedo.json').read_text())
        document.update(width=4, height=4, filter='nearest', encoding='linear-srgb')
        document['file'].update(bytes=len(encoded), sha256=hashlib.sha256(encoded).hexdigest())
        (output/'original.png').write_bytes(encoded)
        (output/'job.json').write_text(json.dumps(dict(scripts=str(repo/'scripts'), document=document,
            output=str(output), native_site=os.environ.get('HATCH_LOOKDEV_TEST_SITE'))))
        result = subprocess.run([os.environ['HATCH_BLENDER'],'--background','--factory-startup',
            '--disable-autoexec','--python-use-system-env','--threads','1','--python-exit-code','1',
            '--python',str(repo/'tests/fixtures/lookdev_triplanar_probe.py'),'--',str(output/'job.json')],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
        (output/'render.log').write_text(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout[-6000:])
        report=json.loads((output/'report.json').read_text())
        self.assertEqual(len(report['cases']), 9)
        self.assertTrue(report['saved_and_reopened'])
        self.assertTrue(report['rollback_preserved_caller_nodes_and_images'])
        self.assertEqual(report['alpha_sha256'],hashlib.sha256((output/'alpha.exr').read_bytes()).hexdigest())
        self.assertEqual(report['source_sha256'], hashlib.sha256(encoded).hexdigest())
        for case in report['cases']:
            self.assertEqual(case['sha256'],hashlib.sha256((output/(case['label']+'.exr')).read_bytes()).hexdigest())
            self.assertLess(case['maximum_error'],2e-5)
