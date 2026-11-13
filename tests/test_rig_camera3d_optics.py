"""Physical camera adapter tests; optional actual Blender gate is explicit."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from camera3d_blender import camera_world, optical_plan
from camera3d_math import field_of_view, projection_matrix, project, world_transform, scene_camera_optics, with_field_of_view


class CameraOptics(unittest.TestCase):
    def setUp(self):
        self.optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
        self.render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
        self.camera = json.loads((ROOT / 'examples/camera3d/main-camera.json').read_text())

    def test_fov_gate_and_shift_mapping(self):
        self.assertAlmostEqual(math.degrees(field_of_view(self.optics, self.render)[0]), 39.597752709, places=8)
        self.optics['projection'].update(shift_x=.1, shift_y=.2)
        self.render['overscan'] = 2
        self.render['max_pixels'] = 360 * 240 * 4
        plan = optical_plan(self.optics, self.render)
        self.assertEqual(plan['sensor_width'], 72)
        self.assertAlmostEqual(plan['shift_x'], -.05)
        self.assertAlmostEqual(plan['shift_y'], -.2 / 3)
        self.assertAlmostEqual(projection_matrix(self.optics, self.render)[0, 2], -.1)

    def test_orthographic_depth_independence(self):
        self.optics['projection'] = dict(type='orthographic', orthographic_height_m=3,
                                        shift_x=.2, shift_y=-.1, near_clip_m=.1, far_clip_m=100)
        plan = optical_plan(self.optics, self.render)
        self.assertAlmostEqual(plan['ortho_scale'], 4.5)
        matrix = projection_matrix(self.optics, self.render)
        world = world_transform(self.camera['transform'])
        self.assertEqual(project([1, 0, -1], world, matrix, 360, 240)['pixel'],
                         project([1, 0, -20], world, matrix, 360, 240)['pixel'])
        with self.assertRaisesRegex(ValueError, 'no optical field of view'):
            field_of_view(self.optics, self.render)
        self.optics['depth_of_field']['enabled'] = True
        with self.assertRaisesRegex(ValueError, 'orthographic'):
            optical_plan(self.optics, self.render)

    def test_native_scene_camera_keeps_vertical_projection(self):
        source = dict(projection='perspective', near=.1, far=100,
                      vertical_fov=math.pi / 3, orthographic_height=4)
        before = deepcopy(source)
        result = scene_camera_optics(source)
        self.assertEqual(source, before)
        self.assertAlmostEqual(field_of_view(result, self.render)[1], math.pi / 3)
        source['projection'] = 'orthographic'
        self.assertEqual(scene_camera_optics(source)['projection']['orthographic_height_m'], 4)
        source['unexpected'] = True
        with self.assertRaises(ValueError):
            scene_camera_optics(source)

    def test_fov_edits_derive_focal_without_mutating_source(self):
        before = deepcopy(self.optics)
        edited = with_field_of_view(self.optics, self.render, math.pi / 4)
        self.assertAlmostEqual(field_of_view(edited, self.render)[1], math.pi / 4)
        self.assertEqual(self.optics, before)
        for angle in (0, math.pi, float('nan'), True):
            with self.assertRaises(ValueError):
                with_field_of_view(self.optics, self.render, angle)

    def test_parent_and_rig_offsets_compose_and_cycle_rejected(self):
        camera = self.camera
        pointer = deepcopy(camera['optics'])
        pointer.update(id=camera['id'], kind=camera['kind'], path='camera.json')
        identity = deepcopy(camera['transform'])
        mount = deepcopy(identity); mount['translation'] = [0, 2, 0]
        rig = dict(schema_version=1, kind='camera3d-rig', id='crane', version='1.0.0',
                   metadata=deepcopy(camera['metadata']), nodes=[
                       dict(id='base', parent=None, transform=identity, camera=None),
                       dict(id='mount', parent='base', transform=mount, camera=pointer)])
        parent = deepcopy(identity); parent['translation'] = [3, 0, 0]
        camera['transform']['translation'] = [0, 0, 1]
        result = camera_world(camera, rig=rig, parent_world=world_transform(parent))
        self.assertEqual(result[:3, 3].tolist(), [3., 2., 1.])
        rig['nodes'][0]['parent'] = 'mount'
        with self.assertRaisesRegex(ValueError, 'cycle'):
            camera_world(camera, rig=rig)

    def test_missing_scene_parent_and_invalid_parameters(self):
        self.camera['parent'] = dict(scene=dict(self.camera['optics'], kind='scene3d-scene'), node='vehicle')
        with self.assertRaisesRegex(ValueError, 'scene-parent'):
            camera_world(self.camera)
        for kwargs in [dict(exposure=float('nan')), dict(exposure=33), dict(motion_blur=1),
                       dict(shutter_frames=-1), dict(shutter_frames=True)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                optical_plan(self.optics, self.render, **kwargs)
        self.optics['depth_of_field']['aperture_blades'] = 2
        with self.assertRaisesRegex(ValueError, 'blades'):
            optical_plan(self.optics, self.render)

    @unittest.skipUnless(os.environ.get('HATCH_CAMERA3D_BLENDER_TEST') == '1', 'explicit actual Blender optical acceptance gate')
    def test_actual_blender_optical_acceptance(self):
        from rig_blender import find_blender
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(os.environ['HATCH_CAMERA_EVIDENCE']) if os.environ.get('HATCH_CAMERA_EVIDENCE') else Path(temp) / 'evidence'
            command = [find_blender(), '--background', '--factory-startup', '--disable-autoexec',
                       '--python-exit-code', '1', '--python', str(ROOT / 'scripts/verify_camera3d_optics.py'),
                       '--', '--destination', str(destination)]
            env = dict(os.environ)
            env['HATCH_CAMERA_TEST_SITE'] = os.environ.get('HATCH_CAMERA_TEST_SITE') or str(Path(__import__('jsonschema').__file__).parent.parent)
            result = subprocess.run(command, capture_output=True, text=True, timeout=180, env=env)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-12000:])
            evidence = json.loads((destination / 'acceptance.json').read_text())
            self.assertTrue(evidence['ok'], evidence)
            self.assertLess(evidence['maximum_projection_error'], 2e-5)
            self.assertTrue(evidence['saved_and_reopened'])
            self.assertGreater(evidence['dof_pixel_difference'], .01)


if __name__ == '__main__':
    unittest.main()
