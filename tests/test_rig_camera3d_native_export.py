"""Native camera export boundary checks and explicit actual Blender gate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from camera3d_native_export import export_camera_library


class NativeCameraExportTests(unittest.TestCase):
    def setUp(self):
        self.optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
        self.render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
        self.world = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

    def test_invalid_controls_fail_before_native_or_output_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'export'
            with self.assertRaises(ValueError):
                export_camera_library(destination, self.optics, self.render, self.world, exposure=True)
            self.assertFalse(destination.exists())

    def test_existing_destination_is_preserved_before_native_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            sentinel = destination / 'keep.txt'; sentinel.write_bytes(b'original')
            with self.assertRaises(ValueError):
                export_camera_library(destination, self.optics, self.render, self.world)
            self.assertEqual(sentinel.read_bytes(), b'original')

    @unittest.skipUnless(os.environ.get('HATCH_CAMERA3D_NATIVE_EXPORT_TEST') == '1',
                         'explicit actual Blender camera library gate')
    def test_actual_native_library_state_and_source_preservation(self):
        from rig_blender import find_blender
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(os.environ.get('HATCH_CAMERA_NATIVE_EXPORT_EVIDENCE', str(Path(temp) / 'evidence')))
            command = [find_blender(), '--background', '--factory-startup', '--disable-autoexec',
                '--threads', '2', '--python-exit-code', '1', '--python',
                str(ROOT / 'scripts/verify_camera3d_native_export.py'), '--',
                '--destination', str(destination)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-12000:])
            report = json.loads((destination / 'acceptance.json').read_text())
            self.assertTrue(report['source_project_unchanged'])
            self.assertTrue(report['rejected_unsupported_without_leak'])
            self.assertEqual(len(report['cases']), 2)


if __name__ == '__main__':
    unittest.main()
