"""Independent numeric scene transform/coordinate references over rig math."""
from copy import deepcopy
import math
import unittest

import numpy as np

from rig_errors import RigError
from rig_math import compose, slerp
from scene3d_contracts import read_document
from scene3d_math import (evaluate_transforms, from_blender, from_gltf, local_matrix,
                          overlay_point, overlay_rotation, to_blender, to_gltf)
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SceneTransformTests(unittest.TestCase):
    def scene(self):
        return read_document(ROOT / 'examples/scene3d-contracts/example-scene.json')

    def test_rig_trs_semantics_unchanged_without_pivot(self):
        transform = dict(translation=[2, 3, 4], rotation=[0, 0, math.sqrt(0.5), math.sqrt(0.5)], scale=[2, 3, 4])
        np.testing.assert_allclose(local_matrix(transform), compose(**transform), atol=1e-12)

    def test_pivot_rotation_parent_child_has_independent_numeric_reference(self):
        scene = self.scene()
        scene['nodes'][0]['transform'] = dict(translation=[10, 0, 0], rotation=[0, 0, math.sqrt(0.5), math.sqrt(0.5)], scale=[1, 1, 1], pivot=[1, 0, 0])
        scene['nodes'][1]['transform']['translation'] = [3, 0, 0]
        result = evaluate_transforms(scene)
        # Point (3,0,0) relative pivot (1,0,0) -> (0,2,0), then
        # restore pivot and translate (10,0,0) -> (11,2,0).
        np.testing.assert_allclose(result['mesh']['world_position'], [11, 2, 0], atol=1e-12)
        np.testing.assert_allclose(result['root']['world_position'], [11, -1, 0], atol=1e-12)

    def test_blender_and_gltf_conversion_independent_basis_and_roundtrip(self):
        m = compose([2, 3, 4], [0, 0, math.sqrt(0.5), math.sqrt(0.5)], [2, 3, 4])
        blender = to_blender(m)
        np.testing.assert_allclose(blender[:3, 3], [2, -4, 3], atol=1e-12)
        np.testing.assert_allclose(from_blender(blender), m, atol=1e-12)
        flat = to_gltf(m)
        self.assertEqual(flat[12:15], [2, 3, 4])
        np.testing.assert_allclose(from_gltf(flat), m, atol=1e-12)
        # Canonical +Y rotation is Blender +Z rotation, not double conversion.
        y = compose(rotation=[0, math.sqrt(0.5), 0, math.sqrt(0.5)])
        np.testing.assert_allclose(to_blender(y) @ [1, 0, 0, 1], [0, 1, 0, 1], atol=1e-12)

    def test_shear_is_explicit_and_world_matrix_remains_exact(self):
        scene = self.scene()
        scene['nodes'][0]['transform']['scale'] = [2, 1, 1]
        scene['nodes'][1]['transform']['rotation'] = [0, 0, math.sin(math.pi / 8), math.cos(math.pi / 8)]
        result = evaluate_transforms(scene)['mesh']
        self.assertFalse(result['decomposable'])
        self.assertIsNone(result['world_orientation'])
        self.assertIsNone(result['world_scale'])
        np.testing.assert_allclose(result['world_matrix'][0][:2], [math.sqrt(2), -math.sqrt(2)], atol=1e-12)

    def test_visibility_inherits_and_evaluation_never_mutates(self):
        scene = self.scene()
        scene['nodes'][0]['visible'] = False
        before = deepcopy(scene)
        result = evaluate_transforms(scene)
        self.assertFalse(any(x['visible'] for x in result.values()))
        self.assertEqual(scene, before)

    def test_unimplemented_dynamic_evaluators_fail_explicitly(self):
        scene = self.scene()
        scene['nodes'][1]['constraints'] = [dict(id='follow', type='copy-transform', target='camera', influence=1)]
        with self.assertRaisesRegex(RigError, 'Static transform'):
            evaluate_transforms(scene)

    def test_overlay_origin_units_rotation_and_depth_are_explicit(self):
        np.testing.assert_allclose(overlay_point([150, 50], pixels_per_meter=100, origin=[50, 150], depth=2), [1, 1, 2])
        q = overlay_rotation(math.pi / 2)
        np.testing.assert_allclose(local_matrix(dict(translation=[0, 0, 0], rotation=q, scale=[1, 1, 1])) @ [1, 0, 0, 1], [0, -1, 0, 1], atol=1e-12)
        with self.assertRaises(RigError):
            overlay_point([0, 0], pixels_per_meter=0, origin=[0, 0], depth=0)

    def test_shortest_path_rotation_remains_rig_authority(self):
        np.testing.assert_allclose(slerp([0, 0, 0, 1], [0, 0, 0, -1], 0.5), [0, 0, 0, 1])

    def test_malformed_basis_matrices_rejected(self):
        with self.assertRaises(RigError):
            from_gltf([0] * 15)
        with self.assertRaises(RigError):
            to_blender(np.zeros((4, 4)))
