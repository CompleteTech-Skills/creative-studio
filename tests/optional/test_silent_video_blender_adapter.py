"""Opt-in native Blender 4.5.3 evidence for the silent-video PNG adapter."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS.parent / 'scripts'))
import silent_video as video


@unittest.skipUnless(os.environ.get('HATCH_SILENT_BLENDER_TEST') == '1',
                     'set HATCH_SILENT_BLENDER_TEST=1 for the native Blender fixture')
class SilentBlenderAdapter(unittest.TestCase):
    def test_cycles_pose_pngs_are_admitted_with_exact_order_and_hashes(self):
        # The opt-in runner provisions this executable. The worker is fixed repository code.
        blender = Path(os.environ['HATCH_BLENDER']).resolve(strict=True)
        worker = TESTS / 'fixtures' / 'silent_video_blender' / 'render.py'
        with tempfile.TemporaryDirectory(prefix='silent-blender-') as directory:
            root = Path(directory)
            result = subprocess.run(
                [str(blender), '--background', '--factory-startup', '--disable-autoexec',
                 '--threads', '2', '--python-exit-code', '1', '--python', str(worker)],
                cwd=root, capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((root / 'blender-frame-manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['engine'], 'CYCLES')
            self.assertEqual({path.name for path in root.glob('*.png')},
                             {'pose-0001.png', 'pose-0002.png'})

            frames = []
            for index, (name, object_x, background_x) in enumerate(
                (('pose-0001.png', 8, 24), ('pose-0002.png', 24, 8))
            ):
                with self.subTest(frame=name):
                    path = root / name
                    data = path.read_bytes()
                    self.assertEqual(data[24:26], bytes((8, 2)))  # PNG IHDR depth and RGB type.
                    with Image.open(path) as image:
                        self.assertEqual((image.format, image.mode, image.size),
                                         ('PNG', 'RGB', (32, 32)))
                        self.assertEqual(image.getpixel((object_x, 16)), (255, 0, 0))
                        self.assertEqual(image.getpixel((background_x, 16)), (0, 0, 0))
                    frames.append({'index': index, 'path': name,
                                   'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})

            self.assertNotEqual(frames[0]['sha256'], frames[1]['sha256'])
            source = video.source_from_blender_manifest(root, manifest)
            self.assertEqual(source, {
                'schema_version': 1, 'kind': 'silent-frame-source', 'origin': 'blender3d',
                'format': 'png', 'precision': 8, 'alpha': 'opaque', 'color': 'untagged-sdr',
                'width': 32, 'height': 32, 'fps': {'numerator': 30000, 'denominator': 1001},
                'frames': frames,
            })

            # Replace one file with the other valid PNG while retaining the original manifest.
            (root / frames[1]['path']).write_bytes((root / frames[0]['path']).read_bytes())
            with self.assertRaisesRegex(ValueError, 'inventory differs from actual bytes/order'):
                video.source_from_blender_manifest(root, manifest)


if __name__ == '__main__':
    unittest.main()
