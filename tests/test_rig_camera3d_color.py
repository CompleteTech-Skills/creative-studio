"""Shared color/camera binding contracts and optional actual native WB proof."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from camera3d_color import apply_camera_with_color, validate_color_binding
from lookdev_color import configuration


class CameraColorTests(unittest.TestCase):
    def setUp(self):
        self.optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
        self.render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
        self.world = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

    def test_uses_shared_hash_and_exposure_without_mutating_inputs(self):
        color = configuration(view='Standard', exposure=1, temperature_kelvin=3200)
        originals = deepcopy((self.optics, self.render, color))
        actual = validate_color_binding(self.optics, self.render, self.world, color)
        self.assertEqual(actual['sha256'], color['sha256'])
        self.assertEqual(actual['exposure_stops'], 1)
        self.assertEqual((self.optics, self.render, color), originals)
        self.assertIsNot(actual, color)

    def test_matches_native_output_contracts_and_rejects_disagreements(self):
        linear = configuration(output='scene-linear-exr')
        render = dict(self.render, output='exr', color_space='linear-srgb')
        self.assertEqual(validate_color_binding(self.optics, render, self.world, linear)['output'],
                         'scene-linear-exr')
        for color, value in ((linear, self.render), (configuration(), render),
                             (configuration(), dict(self.render, color_space='linear-srgb'))):
            with self.subTest(output=color['output'], render=value), self.assertRaisesRegex(ValueError, 'disagrees'):
                validate_color_binding(self.optics, value, self.world, color)

    def test_rejects_tampered_color_before_touching_the_native_scene(self):
        color = configuration(); color['temperature_kelvin'] = 3200
        with self.assertRaises(ValueError):
            apply_camera_with_color(None, self.optics, self.render,
                                    world_matrix=self.world, color_configuration=color)

    def test_rejects_nonrigid_pose_before_native_mutation(self):
        world = deepcopy(self.world); world[0][0] = 2
        with self.assertRaises(ValueError):
            apply_camera_with_color(None, self.optics, self.render,
                                    world_matrix=world, color_configuration=configuration())

    def test_shared_exposure_supports_the_existing_camera_domain(self):
        for exposure in (-32, 32):
            color = configuration(exposure=exposure)
            self.assertEqual(validate_color_binding(self.optics, self.render, self.world, color)['exposure_stops'],
                             exposure)

    @unittest.skipUnless(os.environ.get('HATCH_CAMERA3D_COLOR_TEST') == '1',
                         'explicit actual Blender camera/shared WB acceptance')
    def test_actual_camera_shared_white_balance(self):
        from rig_blender import find_blender
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(os.environ.get('HATCH_CAMERA_COLOR_EVIDENCE', str(Path(temp) / 'evidence')))
            command = [find_blender(), '--background', '--factory-startup', '--disable-autoexec',
                '--threads', '1', '--python-exit-code', '1', '--python',
                str(ROOT / 'scripts/verify_camera3d_color.py'), '--', '--destination', str(destination)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-12000:])
            report = json.loads((destination / 'acceptance.json').read_text())
            self.assertTrue(report['ok'])
            self.assertGreater(report['png_white_balance_mean_difference'], .01)
            self.assertGreater(report['png_tint_mean_difference'], .01)
            self.assertTrue(report['saved_and_reopened'])
            self.assertTrue(report['source_material_and_lighting_unchanged'])
            self.assertEqual(report['linear_exr_maximum_difference'], 0)


if __name__ == '__main__':
    unittest.main()
