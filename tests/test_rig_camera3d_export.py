"""Camera interchange tests against independent core glTF projection equations."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import numpy as np
from camera3d_export import export_gltf, export_plan
from camera3d_math import projection_matrix, world_transform


def gltf_projection(camera):
    # Khronos core glTF finite projection definitions, independent of the
    # Camera3D gate/lens implementation and of this exporter's code path.
    result = np.zeros((4, 4))
    if camera['type'] == 'perspective':
        p = camera['perspective']
        result[0, 0] = 1 / (p['aspectRatio'] * math.tan(p['yfov'] / 2))
        result[1, 1] = 1 / math.tan(p['yfov'] / 2)
        result[2, 2] = (p['znear'] + p['zfar']) / (p['znear'] - p['zfar'])
        result[2, 3] = 2 * p['zfar'] * p['znear'] / (p['znear'] - p['zfar'])
        result[3, 2] = -1
    else:
        p = camera['orthographic']
        result[0, 0] = 1 / p['xmag']
        result[1, 1] = 1 / p['ymag']
        result[2, 2] = 2 / (p['znear'] - p['zfar'])
        result[2, 3] = (p['znear'] + p['zfar']) / (p['znear'] - p['zfar'])
        result[3, 3] = 1
    return result


class CameraExportTests(unittest.TestCase):
    def setUp(self):
        self.optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
        self.render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
        self.world = world_transform(dict(translation=[2, 1, -3], scale=[1, 1, 1],
            rotation=[0, math.sin(.3), 0, math.cos(.3)]))

    def test_projection_for_all_gate_policies_and_non_square_pixels(self):
        for kind in ('perspective', 'orthographic'):
            for fit in ('horizontal', 'vertical', 'auto'):
                for policy in ('crop', 'expand'):
                    optics = deepcopy(self.optics)
                    if kind == 'orthographic':
                        optics['projection'] = dict(type=kind, orthographic_height_m=.1,
                            near_clip_m=.1, far_clip_m=100, shift_x=0, shift_y=0)
                    else:
                        optics['projection']['sensor_fit'] = fit
                    render = dict(self.render, gate_fit=policy, overscan=1.3,
                                  max_pixels=1000000,
                                  pixel_aspect=dict(numerator=2, denominator=1))
                    plan = export_plan(optics, render, self.world)
                    np.testing.assert_allclose(gltf_projection(plan['gltf']['cameras'][0]),
                                               projection_matrix(optics, render), atol=1e-12)

    def test_pose_uses_column_major_matrix_without_basis_conversion(self):
        plan = export_plan(self.optics, self.render, self.world)
        matrix = np.array(plan['gltf']['nodes'][0]['matrix']).reshape(4, 4).T
        np.testing.assert_allclose(matrix, self.world, atol=1e-12)
        point = np.array([.2, .4, -4, 1])
        np.testing.assert_allclose(np.linalg.inv(matrix) @ (self.world @ point), point, atol=1e-12)

    def test_active_losses_require_explicit_acknowledgement_and_are_preserved(self):
        self.optics['projection']['shift_x'] = .1
        self.optics['depth_of_field']['enabled'] = True
        original = deepcopy(self.optics)
        with self.assertRaisesRegex(ValueError, 'allow_loss'):
            export_plan(self.optics, self.render, self.world)
        plan = export_plan(self.optics, self.render, self.world, allow_loss=True,
                           exposure=1, motion_blur=True)
        self.assertEqual(self.optics, original)
        self.assertEqual(plan['canonical']['optics'], original)
        self.assertEqual({item['code'] for item in plan['loss_report']['losses']},
            {'principal-point-shift', 'physical-depth-of-field', 'exposure', 'motion-blur'})
        self.assertFalse(plan['loss_report']['exact_core_projection'])

    def test_focus_only_loss_does_not_claim_projection_was_changed(self):
        self.optics['depth_of_field']['enabled'] = True
        report = export_plan(self.optics, self.render, self.world, allow_loss=True)['loss_report']
        self.assertTrue(report['exact_core_projection'])
        self.assertFalse(report['active_effects_preserved'])

    def test_orthographic_dof_can_be_exported_with_explicit_loss(self):
        self.optics['projection'] = dict(type='orthographic', orthographic_height_m=.1,
            near_clip_m=.1, far_clip_m=100, shift_x=0, shift_y=0)
        self.optics['depth_of_field']['enabled'] = True
        plan = export_plan(self.optics, self.render, self.world, allow_loss=True)
        self.assertEqual(plan['gltf']['cameras'][0]['type'], 'orthographic')
        self.assertEqual(plan['loss_report']['losses'][0]['code'], 'physical-depth-of-field')

    def test_fingerprint_changes_with_optics_and_preserves_input(self):
        original = deepcopy(self.optics)
        a = export_plan(self.optics, self.render, self.world)
        self.optics['depth_of_field']['focus_distance_m'] += 1
        b = export_plan(self.optics, self.render, self.world)
        self.assertNotEqual(a['loss_report']['canonical_sha256'], b['loss_report']['canonical_sha256'])
        self.assertEqual(a['canonical']['optics'], original)

    def test_rejects_invalid_pose_and_nonboolean_policy(self):
        invalid = self.world.copy(); invalid[0, 0] *= 2
        with self.assertRaises(ValueError):
            export_plan(self.optics, self.render, invalid)
        with self.assertRaises(ValueError):
            export_plan(self.optics, self.render, self.world, allow_loss=1)

    def test_publication_is_new_and_receipt_checks_real_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'camera'
            receipt = export_gltf(destination, self.optics, self.render, self.world, validate=False)
            self.assertFalse(receipt['independently_validated'])
            for name, digest in receipt['files'].items():
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), digest)
            with self.assertRaises(ValueError):
                export_gltf(destination, self.optics, self.render, self.world, validate=False)
            self.assertEqual(json.loads((destination / 'canonical.json').read_text())['optics'], self.optics)


if __name__ == '__main__':
    unittest.main()
