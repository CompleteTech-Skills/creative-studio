"""Independent exact-neutral smoke test using public operations and native deformation."""
from copy import deepcopy
import json
import unittest

import numpy as np

from rig_contracts import validate_contract
from rig_facial import validate_eyelid_bindings
from rig_math import axis_angle, qmatrix, quaternion
from rig_operations import pose_operation, solve_operation
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from test_rig_specializations import eyelid_fixture


class NormalizedEyeRest(unittest.TestCase):
    def test_public_locked_eye_at_rest(self):
        rig = eyelid_fixture()
        eye = next(bone for bone in rig.skeleton['bones'] if bone['id'] == 'eye.left')
        rest = axis_angle([7, -2, 1], .6)
        eye['rest']['rotation'] = rest.tolist()
        rig.manifest['facial']['eyelids'][0].update(up_radians=1e-16, down_radians=1e-16)
        rig.manifest['controllers']['constraints'] = [dict(
            schema_version=1, id='lock', type='hinge', bone=eye['id'],
            axis=[1, 0, 0], minimum=0., maximum=0.)]
        rig.manifest['controllers']['ik'] = [dict(
            schema_version=1, id='chain', bones=['root', 'head', 'eye.right'],
            solver='two-bone', pole=[0., 1., 2.], iterations=8, tolerance=.001)]
        validate_contract(rig.manifest['facial'], 'FacialController')
        validate_eyelid_bindings(rig)
        validate_skeleton(rig.skeleton)
        for mesh in rig.geometry.meshes:
            validate_skin(rig.weights[mesh.id], mesh, rig.skeleton)
        world = evaluate_fk(rig.skeleton)
        args = dict(gaze=(world[eye['id']][:3, 3] + [0., 1., 2.]).tolist())
        default = rig.geometry.by_id()['lid.left'].morph_defaults['lift']
        for method in ('pose', 'ik'):
            with self.subTest(method=method):
                if method == 'pose':
                    pose = pose_operation(rig, args)
                else:
                    result = solve_operation(rig, dict(**args, chain='chain', target=world['eye.right'][:3, 3].tolist()))
                    self.assertEqual(result['report']['pose'], result['pose'])
                    pose = result['pose']
                final_rotation = pose['bones'][eye['id']]['rotation']
                np.testing.assert_array_equal(final_rotation, quaternion(rest))
                np.testing.assert_array_equal(qmatrix(final_rotation), qmatrix(rest))
                expected = deepcopy(pose)
                expected['morphs']['lid.left']['lift'] = default
                positions = evaluate_deformation(rig, pose)['lid.left']['positions']
                neutral = evaluate_deformation(rig, expected)['lid.left']['positions']
                distance = np.max(np.linalg.norm(positions - neutral, axis=1))
                actual = pose['morphs']['lid.left']['lift']
                print(json.dumps(dict(method=method, rest=rest.tolist(), actual_rotation=final_rotation,
                                      max_local_matrix_difference=0.0, default=default,
                                      actual=actual, max_vertex_error_metres=distance)), flush=True)
                self.assertEqual((actual, distance), (default, 0.))


if __name__ == '__main__':
    unittest.main(verbosity=2)
