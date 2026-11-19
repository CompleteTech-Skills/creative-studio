"""Shared color/camera binding contracts and optional actual native WB proof."""
from builtins import __import__ as native_import
from contextlib import contextmanager
from copy import deepcopy
import hashlib
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
from camera3d_blender import apply_camera
from lookdev_color import apply_configuration, configuration
from rig_errors import RigError


class ColorScene(dict):
    """Synthetic backend with a display-dependent view and a render RNA clamp."""

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

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


class CameraObject(dict):
    type = 'CAMERA'
    name = 'Existing camera'
    parent = None
    constraints = ()
    animation_data = None
    library = None

    def __init__(self):
        super().__init__()
        self.data = SimpleNamespace(animation_data=None, users=0, get={'hatch_camera3d_owned': True}.get)
        self.matrix_basis = ['original pose']
        self.fail_hash = False

    @property
    def matrix_world(self):
        return self.matrix_basis

    @matrix_world.setter
    def matrix_world(self, value):
        self.matrix_basis = value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == 'hatch_color_sha256' and self.fail_hash:
            self.fail_hash = False
            raise RuntimeError('camera metadata rejected')

    def __delitem__(self, key):
        super().__delitem__(key)
        if key == 'hatch_color_sha256' and self.fail_hash:
            self.fail_hash = False
            raise RuntimeError('camera metadata rejected')

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other


@contextmanager
def synthetic_backend(scene):
    native_bounds = SimpleNamespace(hard_min=-1e9, hard_max=1e9)
    optics_rna = SimpleNamespace(properties={
        name: native_bounds
        for name in ('sensor_width', 'shift_x', 'shift_y', 'clip_start', 'clip_end', 'lens',
                     'use_dof', 'focus_distance', 'aperture_fstop', 'aperture_blades',
                     'aperture_rotation', 'aperture_ratio')
    })
    new_camera = SimpleNamespace(bl_rna=optics_rna, dof=SimpleNamespace(bl_rna=optics_rna),
                                 animation_data=None, users=0, get={}.get)
    cameras = Mock()
    cameras.new.return_value = new_camera
    bpy = SimpleNamespace(data=SimpleNamespace(cameras=cameras, objects=Mock()),
                          context=SimpleNamespace(scene=scene, view_layer=Mock()))
    mathutils = SimpleNamespace(Matrix=list)

    def import_backend(name, *args, **kwargs):
        if name == 'bpy':
            return bpy
        if name == 'mathutils':
            return mathutils
        return native_import(name, *args, **kwargs)

    with (patch('builtins.__import__', new=import_backend),
          patch('camera3d_blender._configure_camera')):
        yield bpy, new_camera


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
        color = configuration(exposure=2, output='scene-linear-exr')
        render = dict(self.render, width=96, height=64, output='exr', color_space='linear-srgb')
        with synthetic_backend(scene) as (bpy, new_camera):
            with self.assertRaisesRegex(ValueError, 'cannot represent resolution_y=64'):
                apply_camera_with_color(scene, self.optics, render, world_matrix=self.world,
                                        color_configuration=color)
        cameras = bpy.data.cameras
        cameras.remove.assert_called_once_with(new_camera)
        bpy.data.objects.new.assert_not_called()
        self.assertIs(scene.camera, active_camera)
        self.assertEqual(scene.state(), original)

    def test_effective_color_fields_reject_silent_backend_substitutions(self):
        for group, field, fallback in (
                ('display_settings', 'display_device', 'PreviousDevice'),
                ('view_settings', 'view_transform', 'Standard'),
                ('view_settings', 'look', 'PreviousLook'),
                ('view_settings', 'gamma', 1.2),
                ('view_settings', 'exposure', -1),
                ('view_settings', 'use_curve_mapping', True),
                ('view_settings', 'use_hdr_view', True),
                ('view_settings', 'use_white_balance', False),
                ('view_settings', 'white_balance_temperature', 5000),
                ('view_settings', 'white_balance_tint', 2),
                ('image_settings', 'file_format', 'JPEG'),
                ('image_settings', 'color_depth', '8'),
                ('image_settings', 'color_management', 'OVERRIDE'),
                ('image_settings', 'color_mode', 'RGB'),
                ('image_settings', 'exr_codec', 'PIZ')):
            with self.subTest(field=field):
                scene = ColorScene()
                owner = scene.render if group == 'image_settings' else scene
                current = getattr(owner, group)

                class Substituted(type(current)):
                    def __setattr__(self, name, value):
                        super().__setattr__(name, fallback if name == field else value)

                setattr(owner, group, Substituted(**vars(current)))
                original = scene.state()
                profile = configuration(output='scene-linear-exr')
                with self.assertRaisesRegex(RigError, 'cannot represent ' + field):
                    apply_configuration(scene, profile)
                self.assertEqual(scene.state(), original)

    def test_final_readback_detects_late_field_change_and_accepts_float32(self):
        from struct import pack, unpack
        scene = ColorScene()
        class FloatView(type(scene.view_settings)):
            def __setattr__(self, name, value):
                if name == 'exposure':
                    value, = unpack('f', pack('f', value))
                super().__setattr__(name, value)

        scene.view_settings = FloatView(**vars(scene.view_settings))
        color = configuration(exposure=0.123456789)
        camera = CameraObject()
        scene.objects = {camera.name: camera}
        with synthetic_backend(scene):
            _, receipt = apply_camera_with_color(scene, self.optics, self.render, world_matrix=self.world,
                                                 color_configuration=color, camera_object=camera)
        self.assertNotEqual(scene.view_settings.exposure, color['exposure_stops'])
        self.assertEqual(receipt['configuration_sha256'], color['sha256'])

        class ResetsExposure(SimpleNamespace):
            def __setattr__(self, name, value):
                super().__setattr__(name, value)
                if name == 'exr_codec' and value == 'ZIP':
                    scene.view_settings.exposure = 0

        scene.render.image_settings = ResetsExposure(**vars(scene.render.image_settings))
        saved_exposure = scene.view_settings.exposure
        with self.assertRaisesRegex(RigError, 'cannot represent exposure'):
            apply_configuration(scene, configuration(exposure=1, output='scene-linear-exr'))
        self.assertEqual(scene.view_settings.exposure, saved_exposure)
        self.assertEqual(scene['hatch_color_sha256'], receipt['configuration_sha256'])
        self.assertEqual(camera['hatch_color_sha256'], receipt['configuration_sha256'])

    def test_standalone_exposure_and_camera_switch_binding_lifecycle(self):
        key = 'hatch_color_sha256'
        for switch in (False, True):
            for exposure in (0, 1):
                with self.subTest(switch=switch, exposure=exposure):
                    scene = ColorScene()
                    bound = CameraObject()
                    target = CameraObject() if switch else bound
                    if switch:
                        target.name = 'Other camera'
                    target[key] = 'former-binding'
                    scene.objects = {cam.name: cam for cam in (bound, target)}
                    color = configuration()
                    with synthetic_backend(scene):
                        apply_camera_with_color(scene, self.optics, self.render, world_matrix=self.world,
                                                color_configuration=color, camera_object=bound)
                        apply_camera(scene, self.optics, self.render, world_matrix=self.world,
                                     exposure=exposure, camera_object=target)
                    self.assertIs(scene.camera, target)
                    self.assertEqual(scene.get(key), color['sha256'] if exposure == 0 else None)
                    preserved = color['sha256'] if exposure == 0 and not switch else None
                    self.assertEqual(bound.get(key), preserved)
                    self.assertEqual(target.get(key), preserved)

    def test_standalone_invalidation_failure_restores_all_bindings(self):
        scene = ColorScene()
        key = 'hatch_color_sha256'
        bound, target = CameraObject(), CameraObject()
        target.name = 'Other camera'
        scene.objects = {cam.name: cam for cam in (bound, target)}
        target[key] = 'former-binding'
        original = target.data
        color = configuration(exposure=1)
        with synthetic_backend(scene):
            apply_camera_with_color(scene, self.optics, self.render, world_matrix=self.world,
                                    color_configuration=color, camera_object=bound)
            target.fail_hash = True
            with self.assertRaisesRegex(RuntimeError, 'camera metadata rejected'):
                apply_camera(scene, self.optics, self.render, world_matrix=self.world,
                             exposure=0, camera_object=target)
        self.assertIs(scene.camera, bound)
        self.assertIs(target.data, original)
        self.assertEqual(scene.view_settings.exposure, 1)
        self.assertEqual(scene[key], color['sha256'])
        self.assertEqual(bound[key], color['sha256'])
        self.assertEqual(target[key], 'former-binding')

    def test_scene_only_configuration_releases_stale_active_camera_binding(self):
        scene = ColorScene()
        camera = CameraObject()
        scene.objects = {camera.name: camera}
        color = configuration()
        with synthetic_backend(scene):
            apply_camera_with_color(scene, self.optics, self.render, world_matrix=self.world,
                                    color_configuration=color, camera_object=camera)
        apply_configuration(scene, color)
        self.assertEqual(camera['hatch_color_sha256'], color['sha256'])
        changed = configuration(tint=15)
        apply_configuration(scene, changed)
        self.assertNotIn('hatch_color_sha256', camera)
        self.assertEqual(scene['hatch_color_sha256'], changed['sha256'])

    def test_shared_camera_scene_only_and_explicit_activation(self):
        first, second = ColorScene(), ColorScene()
        camera = CameraObject()
        first.objects = second.objects = {camera.name: camera}
        key = 'hatch_color_sha256'
        bound, changed = configuration(exposure=1), configuration(tint=15)
        with synthetic_backend(first):
            apply_camera_with_color(first, self.optics, self.render, world_matrix=self.world,
                                    color_configuration=bound, camera_object=camera)
            camera.users_scene = (first, second)
            second.camera = camera
            apply_configuration(second, bound)
            apply_configuration(second, changed)
            self.assertEqual(camera.get(key), bound['sha256'])
            self.assertEqual(first.get(key), bound['sha256'])
            self.assertEqual(second.get(key), changed['sha256'])
            original_data = camera.data
            with self.assertRaisesRegex(ValueError, 'shared camera'):
                apply_camera_with_color(second, self.optics, self.render, world_matrix=self.world,
                                        color_configuration=configuration(), camera_object=camera)
            self.assertIs(camera.data, original_data)
            self.assertEqual(camera.get(key), bound['sha256'])
            self.assertEqual(second.get(key), changed['sha256'])
            apply_configuration(first, changed)
            self.assertNotIn(key, camera)

    def test_update_drift_restores_profile_camera_and_bindings(self):
        for group, field, replacement in (
                ('view_settings', 'exposure', 3),
                ('view_settings', 'white_balance_tint', 40),
                ('display_settings', 'display_device', 'Other'),
                ('image_settings', 'exr_codec', 'PIZ')):
            with self.subTest(field=field):
                scene = ColorScene()
                camera = CameraObject()
                scene.objects = {camera.name: camera}
                original = scene.state()
                active, data, pose = scene.camera, camera.data, camera.matrix_basis.copy()
                owner = scene.render if group == 'image_settings' else scene
                color = configuration(exposure=1, output='scene-linear-exr')
                render = dict(self.render, output='exr', color_space='linear-srgb')
                with synthetic_backend(scene) as (bpy, created):
                    bpy.context.view_layer.update.side_effect = lambda: setattr(getattr(owner, group), field, replacement)
                    with self.assertRaisesRegex(RigError, 'cannot represent ' + field):
                        apply_camera_with_color(scene, self.optics, render, world_matrix=self.world,
                                                color_configuration=color, camera_object=camera)
                    bpy.data.cameras.remove.assert_called_once_with(created)
                self.assertIs(scene.camera, active)
                self.assertIs(camera.data, data)
                self.assertEqual(camera.matrix_basis, pose)
                self.assertEqual(dict(camera), {})
                self.assertEqual(scene.state(), original)

    def test_ambiguous_legacy_shared_tag_is_preserved(self):
        first, second = ColorScene(), ColorScene()
        camera = CameraObject()
        camera.users_scene = (first, second)
        first.camera = second.camera = camera
        key = 'hatch_color_sha256'
        first[key] = camera[key] = configuration()['sha256']
        changed = configuration(tint=15)
        apply_configuration(second, changed)
        self.assertEqual(camera[key], first[key])
        original = first.state()
        with self.assertRaisesRegex(RigError, 'Ambiguous legacy shared camera'):
            apply_configuration(first, changed)
        self.assertEqual(first.state()[:-1], original[:-1])
        self.assertIs(first.camera, camera)
        self.assertEqual(camera[key], first[key])

    def test_foreground_probe_rejects_wrong_radiance_or_second_transform(self):
        import numpy as np
        from verify_camera3d_color import foreground_error
        expected = (.3, .3, .3, 1.)
        pixels = np.full((64, 96, 4), expected)
        self.assertEqual(foreground_error(pixels, expected, 1e-5), 0)
        once = pixels.copy()
        once[..., :3] *= 2
        reference = once[28:36, 44:52, :]
        self.assertEqual(foreground_error(once, reference, 2e-4), 0)
        twice = once.copy()
        twice[..., :3] *= 2
        for wrong in (pixels, twice):
            with self.assertRaisesRegex(AssertionError, 'Foreground maximum error'):
                foreground_error(wrong, reference, 2e-4)
        for wrong in (np.zeros_like(pixels), once, np.full_like(pixels, np.nan)):
            with self.assertRaisesRegex(AssertionError, 'Foreground maximum error'):
                foreground_error(wrong, expected, 1e-5)

    def test_late_camera_hash_failure_restores_existing_camera(self):
        for original_metadata in ({}, {'hatch_color_sha256': 'previous-camera-hash'}):
            with self.subTest(original_metadata=original_metadata):
                scene = ColorScene()
                camera = CameraObject()
                camera.update(original_metadata)
                camera.fail_hash = True
                scene.objects = {camera.name: camera}
                previous_data = camera.data
                previous_active = scene.camera
                original = scene.state()
                with synthetic_backend(scene) as (bpy, new_camera):
                    with self.assertRaisesRegex(RuntimeError, 'camera metadata rejected'):
                        apply_camera_with_color(scene, self.optics, self.render, world_matrix=self.world,
                                                color_configuration=configuration(), camera_object=camera)
                bpy.data.cameras.remove.assert_called_once_with(new_camera)
                self.assertIs(camera.data, previous_data)
                self.assertIs(scene.camera, previous_active)
                self.assertEqual(camera.matrix_basis, ['original pose'])
                self.assertEqual(dict(camera), original_metadata)
                self.assertEqual(scene.state(), original)

    def test_old_camera_cleanup_failure_keeps_committed_camera_and_color(self):
        scene = ColorScene()
        camera = CameraObject()
        scene.objects = {camera.name: camera}
        previous_data = camera.data
        color = configuration(exposure=2)
        with synthetic_backend(scene) as (bpy, new_camera):
            bpy.data.cameras.remove.side_effect = RuntimeError('cannot reclaim old camera')
            activated, receipt = apply_camera_with_color(
                scene, self.optics, self.render, world_matrix=self.world,
                color_configuration=color, camera_object=camera)
        bpy.data.cameras.remove.assert_called_once_with(previous_data)
        self.assertIs(activated, camera)
        self.assertIs(scene.camera, camera)
        self.assertIs(camera.data, new_camera)
        self.assertEqual(camera['hatch_color_sha256'], color['sha256'])
        self.assertEqual(scene['hatch_color_sha256'], color['sha256'])
        self.assertEqual(receipt['configuration_sha256'], color['sha256'])
        self.assertEqual(scene.view_settings.exposure, 2)

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
            self.assertGreater(report['png_one_stop_mean_increase'], .01)
            self.assertEqual(set(report['png_direct_foreground_errors']),
                             {'neutral', 'temperature-3200', 'tint-100', 'one-stop'})
            self.assertLessEqual(max(report['png_direct_foreground_errors'].values()), 2e-4)
            self.assertEqual(report['exr_expected_foreground_rgba'], [.3, .3, .3, 1.])
            self.assertEqual(len(report['exr_foreground_errors']), 2)
            self.assertLessEqual(max(report['exr_foreground_errors']), 1e-5)
            self.assertTrue(report['saved_and_reopened'])
            self.assertTrue(report['standalone_exposure_checked'])
            self.assertEqual(report['binding_transactions'], dict(
                shared_activation_rejected=True, scene_only_owner_preserved=True,
                callback_exposure_before=1., callback_exposure_after=3., restored_exposure=1.))
            hashes = report['source_sha256']
            self.assertTrue({
                '.github/workflows/rig-3d.yml', 'tests/test_rig_camera3d_color.py',
                'scripts/verify_camera3d_color.py', 'scripts/lookdev_contracts.py',
                'scripts/camera3d_contracts.py', 'scripts/rig_errors.py', 'scripts/rig_math.py',
                'scripts/rig_blender.py', 'scripts/camera3d_color.py', 'scripts/lookdev_color.py',
                'scripts/camera3d_blender.py', 'scripts/camera3d_math.py',
                'examples/camera3d/optics.json', 'examples/camera3d/render.json',
            }.issubset(hashes))
            for path, digest in hashes.items():
                self.assertEqual(digest, hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), path)
            self.assertTrue(report['source_material_and_lighting_unchanged'])
            self.assertEqual(report['linear_exr_maximum_difference'], 0)


if __name__ == '__main__':
    unittest.main()
