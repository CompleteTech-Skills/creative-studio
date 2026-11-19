"""Native synthetic regression for exact neutral under a rigid, rotated parent."""
from copy import deepcopy
import json
import unittest
import numpy as np
from rig_contracts import validate_contract
from rig_facial import validate_eyelid_bindings
from rig_math import axis_angle, qmatrix
from rig_operations import pose_operation, solve_operation
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from test_rig_specializations import eyelid_fixture


def locked_eye():
    rig = eyelid_fixture()
    rotation = axis_angle([1, 2, 3], .6)
    matrix = qmatrix(rotation)
    rig.skeleton['bones'][0]['rest']['rotation'] = rotation.tolist()
    for bone in rig.skeleton['bones']:
        bone['head'] = (matrix @ bone['head']).tolist()
        bone['tail'] = (matrix @ bone['tail']).tolist()
    validate_skeleton(rig.skeleton)
    world = evaluate_fk(rig.skeleton)
    for mesh in rig.geometry.meshes:
        mesh.positions = mesh.positions @ matrix.T
        mesh.morphs['lift']['POSITION'] = mesh.morphs['lift']['POSITION'] @ matrix.T
        rig.weights[mesh.id].inverse_bind = np.linalg.inv(world['head'])[None, :, :]
        validate_skin(rig.weights[mesh.id], mesh, rig.skeleton)
    facial = rig.manifest['facial']
    facial['eyelids'][0].update(up_radians=1e-16, down_radians=1e-16)
    validate_contract(facial, 'FacialController')
    validate_eyelid_bindings(rig)
    rig.manifest['controllers']['constraints'] = [dict(schema_version=1,
        id='lock-lid', bone='eye.left', type='hinge', axis=[1, 0, 0], minimum=0., maximum=0.)]
    rig.manifest['controllers']['ik'] = [dict(schema_version=1, id='chain',
        bones=['root', 'head', 'eye.right'], solver='two-bone',
        pole=[0.,1.,2.], iterations=8, tolerance=.001)]
    return rig, world


class LockedEyeDefault(unittest.TestCase):
    def test_public_default_and_vertices(self):
        rig, world = locked_eye()
        args = dict(gaze=(world['eye.left'][:3, 3] + [1., 0., 2.]).tolist())
        local_rest = next(b['rest']['rotation'] for b in rig.skeleton['bones'] if b['id'] == 'eye.left')
        default = rig.geometry.by_id()['lid.left'].morph_defaults['lift']
        for name in ('pose', 'solve'):
            with self.subTest(name=name):
                if name == 'pose':
                    actual = pose_operation(rig, args)
                else:
                    result = solve_operation(rig, {**args, 'chain':'chain', 'target':world['eye.right'][:3,3].tolist()})
                    self.assertEqual(result['report']['pose'], result['pose'])
                    actual = result['pose']
                self.assertEqual(actual['bones']['eye.left']['rotation'], local_rest)
                expected = deepcopy(actual)
                expected['morphs']['lid.left']['lift'] = default
                vertices = evaluate_deformation(rig, actual)['lid.left']['positions']
                expected_vertices = evaluate_deformation(rig, expected)['lid.left']['positions']
                error = float(np.max(np.linalg.norm(vertices - expected_vertices, axis=1)))
                weight = actual['morphs']['lid.left']['lift']
                print(json.dumps(dict(operation=name, eye_local_rotation=local_rest,
                      weight=weight, authored_default=default, max_vertex_error_metres=error)), flush=True)
                self.assertAlmostEqual(weight, default, places=14)
                self.assertLess(error, 1e-14)

if __name__ == '__main__':
    unittest.main(verbosity=2)
