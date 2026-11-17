"""Shared color/camera binding contracts and optional actual native WB proof."""
from builtins import __import__ as native_import
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from camera3d_color import apply_camera_with_color, validate_color_binding
from lookdev_color import configuration
from rig_errors import RigError


class ColorScene(dict):
    """Synthetic backend with a display-dependent view and a render RNA clamp."""

    def __init__(self):
        super().__init__(hatch_color_sha256='previous-scene-hash')
        self.camera = {'hatch_color_sha256': 'previous-camera-hash'}
        self.display_settings = SimpleNamespace(display_device='PreviousDevice')
        bounds = SimpleNamespace(hard_min=-32, hard_max=32)

        class ViewSettings(SimpleNamespace):
            @property
            def view_transform(self):
                return self.view

            @view_transform.setter
            def view_transform(self, value):
                if value == 'Filmic':
                    raise TypeError('view unavailable')
                self.view = value

        self.view_settings = ViewSettings(
            view='Standard', look='PreviousLook', exposure=-1, gamma=1.2,
            use_curve_mapping=True, use_hdr_view=True, use_white_balance=False,
            white_balance_temperature=5000, white_balance_tint=2,
            bl_rna=SimpleNamespace(properties={
                'exposure': bounds,
                'white_balance_temperature': SimpleNamespace(hard_min=1800, hard_max=10000),
                'white_balance_tint': SimpleNamespace(hard_min=-150, hard_max=150),
            }),
        )

        class RenderSettings(SimpleNamespace):
            def __setattr__(self, name, value):
                if name == 'resolution_y' and value == 64:
                    value = 63
                super().__setattr__(name, value)

        self.render = RenderSettings(
            engine='CYCLES', resolution_x=320, resolution_y=240,
            resolution_percentage=50, pixel_aspect_x=1., pixel_aspect_y=1.,
            use_motion_blur=False, motion_blur_shutter=.25,
            bl_rna=SimpleNamespace(properties={
                name: SimpleNamespace(hard_min=0, hard_max=4000)
                for name in ('resolution_x', 'resolution_y', 'resolution_percentage',
                             'pixel_aspect_x', 'pixel_aspect_y', 'use_motion_blur',
                             'motion_blur_shutter')
            }),
            image_settings=SimpleNamespace(file_format='JPEG', color_depth='8',
                                           color_mode='RGB', color_management='OVERRIDE',
                                           exr_codec='PIZ'),
        )

    def state(self):
        return deepcopy((dict(self), vars(self.display_settings), vars(self.view_settings),
                         vars(self.render), self.camera))


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

    def test_native_color_rejections_preserve_existing_camera_and_scene(self):
        for failure, color, error in (
                ('white balance unavailable', configuration(), 'lacks explicit view white balance'),
                ('temperature outside RNA range', configuration(temperature_kelvin=20000),
                 'exceeds actual backend bounds'),
                ('OCIO view unavailable', configuration(view='Filmic'), 'view/look unavailable')):
            with self.subTest(failure=failure):
                scene = ColorScene()
                if failure == 'white balance unavailable':
                    del scene.view_settings.use_white_balance
                original = scene.state()
                camera = scene.camera
                with patch('camera3d_color.apply_camera') as apply_camera:
                    with self.assertRaisesRegex(RigError, error):
                        apply_camera_with_color(scene, self.optics, self.render,
                                                world_matrix=self.world, color_configuration=color,
                                                camera_object=camera)
                    apply_camera.assert_not_called()
                self.assertIs(scene.camera, camera)
                self.assertEqual(scene.state(), original)

    def test_late_render_clamp_rolls_back_shared_color_and_render(self):
        scene = ColorScene()
        original = scene.state()
        active_camera = scene.camera
        native_bounds = SimpleNamespace(hard_min=-1e9, hard_max=1e9)
        optics_rna = SimpleNamespace(properties={
            name: native_bounds
            for name in ('sensor_width', 'shift_x', 'shift_y', 'clip_start', 'clip_end', 'lens',
                         'use_dof', 'focus_distance', 'aperture_fstop', 'aperture_blades',
                         'aperture_rotation', 'aperture_ratio')
        })
        new_camera = SimpleNamespace(bl_rna=optics_rna, dof=SimpleNamespace(bl_rna=optics_rna))
        cameras = Mock()
        cameras.new.return_value = new_camera
        bpy = SimpleNamespace(data=SimpleNamespace(cameras=cameras, objects=Mock()))
        mathutils = SimpleNamespace(Matrix=Mock())
        color = configuration(exposure=2, output='scene-linear-exr')
        render = dict(self.render, width=96, height=64, output='exr', color_space='linear-srgb')
        def import_backend(name, *args, **kwargs):
            if name == 'bpy':
                return bpy
            if name == 'mathutils':
                return mathutils
            return native_import(name, *args, **kwargs)

        with (patch('builtins.__import__', new=import_backend),
              patch('camera3d_blender._configure_camera')):
            with self.assertRaisesRegex(ValueError, 'cannot represent resolution_y=64'):
                apply_camera_with_color(scene, self.optics, render, world_matrix=self.world,
                                        color_configuration=color)
        cameras.remove.assert_called_once_with(new_camera)
        bpy.data.objects.new.assert_not_called()
        self.assertIs(scene.camera, active_camera)
        self.assertEqual(scene.state(), original)

    def test_traceability_names_existing_tests(self):
        """Every test cited in the #76 traceability table must exist in its file."""
        import re
        text = (ROOT / 'references/camera3d-traceability.md').read_text()
        cited = []
        for line in text.splitlines():
            current = None
            for match in re.finditer(r'`(tests/test_\w+\.py)`|`(test_\w+)`', line):
                if match.group(1):
                    current = match.group(1)
                elif current is not None:
                    cited.append((current, match.group(2)))
        self.assertGreaterEqual(len(cited), 20)
        sources = {}
        for path, name in cited:
            source = sources.setdefault(path, (ROOT / path).read_text())
            with self.subTest(path=path, test=name):
                self.assertRegex(source, r'\n    def ' + re.escape(name) + r'\(self')
        for task in range(1, 5):
            self.assertIn(f'## Task {task}:', text)
        self.assertIn('**ISO is unsupported.**', text)
        for directory in ('camera-optics-gate/', 'camera-color-gate/'):
            self.assertIn(directory, text)

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
