"""A representable pole change below a scaled parent must retain a rigid fold."""
from copy import deepcopy
import math
import unittest

import numpy as np

from rig_errors import RigError
from rig_ik import solve_ik, validate_chain
from rig_math import qmatrix, qmul
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from test_rig_fold_matching_boundaries import chain, explicit_lbs, quaternion, skinned_rig
from test_rig_math import chain_skeleton


class FoldParentFrameTests(unittest.TestCase):
    def assert_rigid_skin(self, rig, pose, original, origin):
        world = evaluate_fk(rig.skeleton, pose)
        # b0 is outside the IK chain. Every other joint and its influences
        # undergo the same rigid world rotation about the head of b1.
        np.testing.assert_array_equal(world['b0'], original['b0'])
        turn = world['b1'][:3, :3] @ np.linalg.inv(original['b1'][:3, :3])
        np.testing.assert_allclose(turn.T @ turn, np.eye(3), rtol=0, atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(turn), 1., delta=1e-12)
        motion = np.eye(4)
        motion[:3, :3] = turn
        motion[:3, 3] = origin - turn @ origin
        expected = {name: matrix if name == 'b0' else motion @ matrix
                    for name, matrix in original.items()}
        for name in original:
            np.testing.assert_allclose(world[name], expected[name], rtol=0, atol=1e-12,
                                       err_msg=name)
        mesh_id = rig.geometry.meshes[0].id
        actual = evaluate_deformation(rig, pose)[mesh_id]['positions']
        np.testing.assert_allclose(actual, explicit_lbs(rig, expected), rtol=0, atol=1e-12)
        return world

    def assert_attainable_pole(self, solver, parent_scale, parent_rotation, first_offset):
        count = 3 if solver == 'two-bone' else 5
        skeleton = chain_skeleton(count + 1)
        pose = neutral_pose(skeleton)
        pose['bones']['b0'] = dict(scale=parent_scale, rotation=parent_rotation,
                                   translation=[13., -7., 2.])
        first = np.array(first_offset)
        side = np.array([0., 0., 1.])
        offsets = (first, -first) if count == 3 else (first, side, -first, -side)
        for index, offset in enumerate(offsets, 2):
            pose['bones'][f'b{index}'] = dict(translation=offset.tolist())

        original = evaluate_fk(skeleton, pose)
        origin = original['b1'][:3, 3]
        endpoint = f'b{count}'
        np.testing.assert_allclose(original[endpoint][:3, 3], origin, rtol=0, atol=1e-12)
        rig = skinned_rig(skeleton)
        # A literal local quarter turn commutes with the parent's equal Y/Z
        # scales. This finite TRS witnesses an attainable pole and exact closure.
        reference = deepcopy(pose)
        reference['bones']['b1'] = dict(rotation=quaternion([1., 0., 0.], math.pi / 2))
        witness = self.assert_rigid_skin(rig, reference, original, origin)
        pole = witness['b2'][:3, 3]
        np.testing.assert_allclose(witness[endpoint][:3, 3], origin, rtol=0, atol=1e-12)
        self.assertGreater(np.linalg.norm(pole - original['b2'][:3, 3]), 1.)

        declaration = chain(count)
        declaration.update(bones=[f'b{i}' for i in range(1, count + 1)],
                           solver=solver, pole=pole.tolist())
        validate_chain(skeleton, declaration)
        result = solve_ik(skeleton, pose, declaration, origin)
        # The chosen rigid turn may have a different twist from the witness.
        # Check the requested elbow and the whole shape under the returned turn.
        observed = self.assert_rigid_skin(rig, result.pose, original, origin)
        np.testing.assert_allclose(observed['b2'][:3, 3], pole, rtol=0, atol=1e-12)
        measured = np.linalg.norm(observed[endpoint][:3, 3] - origin)
        self.assertAlmostEqual(result.residual_m, measured, delta=1e-15)
        self.assertLess(measured, 1e-12)
        self.assertTrue(result.reachable)
        self.assertTrue(result.converged, result.report())

    def closed_parent_case(self, solver, parent_scale, parent_rotation, root_rotation, first):
        count = 3 if solver == 'two-bone' else 5
        skeleton = chain_skeleton(count + 1)
        pose = neutral_pose(skeleton)
        pose['bones']['b0'] = dict(scale=parent_scale, rotation=parent_rotation)
        pose['bones']['b1'] = dict(translation=[0., 0., 0.], rotation=root_rotation)
        first = np.array(first)
        side = np.array([0., 0., 1.])
        offsets = (first, -first) if count == 3 else (first, side, -first, -side)
        for index, offset in enumerate(offsets, 2):
            pose['bones'][f'b{index}'] = dict(translation=offset.tolist())
        declaration = chain(count)
        declaration.update(bones=[f'b{i}' for i in range(1, count + 1)], solver=solver)
        return skeleton, pose, declaration

    def assert_local_turn_witness(self, case, angle, axis=(1., 0., 0.), scale=None):
        skeleton, pose, declaration = case
        original = evaluate_fk(skeleton, pose)
        origin = original['b1'][:3, 3]
        endpoint = declaration['bones'][-1]
        reference = deepcopy(pose)
        reference['bones']['b1']['rotation'] = qmul(
            quaternion(axis, angle), pose['bones']['b1']['rotation']).tolist()
        if scale is not None:
            reference['bones']['b1']['scale'] = scale
        witness = evaluate_fk(skeleton, reference)
        pole = witness['b2'][:3, 3]
        np.testing.assert_allclose(witness[endpoint][:3, 3], origin, rtol=0, atol=1e-12)

        result = solve_ik(skeleton, pose, declaration, origin, pole=pole)
        observed = self.assert_rigid_skin(skinned_rig(skeleton), result.pose, original, origin)
        np.testing.assert_allclose(observed['b2'][:3, 3], pole, rtol=0, atol=1e-12)
        measured = np.linalg.norm(observed[endpoint][:3, 3] - origin)
        self.assertAlmostEqual(result.residual_m, measured, delta=1e-15)
        self.assertLess(measured, 1e-12)
        self.assertTrue(result.converged, result.report())

    def test_small_attainable_turn_is_rigid(self):
        identity = [0., 0., 0., 1.]
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                case = self.closed_parent_case(solver, [2., 1., 1.], identity, identity, [1., 1., 0.])
                self.assert_local_turn_witness(case, 1e-6)

    def test_adjacent_unattainable_direction_reports_shear(self):
        identity = [0., 0., 0., 1.]
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                skeleton, pose, declaration = self.closed_parent_case(
                    solver, [2., 1., 1.], identity, identity, [1., 1., 0.])
                with self.assertRaises(RigError) as error:
                    solve_ik(skeleton, pose, declaration, [0., 0., 0.], pole=[2., 1.0000002, 0.])
                self.assertEqual(error.exception.report['code'], 'RIG_SHEAR')

    def test_attainable_turn_under_rotated_highly_axial_parent(self):
        parent = (np.array([1., 2., 3., 4.]) / math.sqrt(30.)).tolist()
        root = (np.array([-1., 2., -1., 1.]) / math.sqrt(7.)).tolist()
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                case = self.closed_parent_case(solver, [128., 1., 1.], parent, root, [1., 2., 1.])
                self.assert_local_turn_witness(case, 1.1)

    def test_turn_reversing_compressed_parent_axis(self):
        parent = (np.array([1., 2., 3., 4.]) / math.sqrt(30.)).tolist()
        root = (np.array([-1., 2., -1., 1.]) / math.sqrt(7.)).tolist()
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                case = self.closed_parent_case(solver, [1 / 128., 1., 1.], parent, root, [1., 2., 1.])
                self.assert_local_turn_witness(case, math.pi, [0., 1., 2.])

    def test_triaxial_parent_accepts_principal_axis_half_turn(self):
        parent = (np.array([1., 2., 3., 4.]) / math.sqrt(30.)).tolist()
        root = (np.array([-1., 2., -1., 1.]) / math.sqrt(7.)).tolist()
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                case = self.closed_parent_case(solver, [128., 2., 1.], parent, root, [1., 2., 1.])
                self.assert_local_turn_witness(case, math.pi)

    def test_triaxial_parent_accepts_turn_with_changed_local_scales(self):
        identity = [0., 0., 0., 1.]
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                skeleton, pose, declaration = self.closed_parent_case(
                    solver, [3., 2., 1.], identity, identity, [1., 1., 1.])
                original = evaluate_fk(skeleton, pose)
                origin = original['b1'][:3, 3]
                # A world Z quarter turn maps the elbow (3, 2, 1) here and
                # is representable with local scales (3/2, 2/3, 1).
                pole = [-2., 3., 1.]
                result = solve_ik(skeleton, pose, declaration, origin, pole=pole)
                observed = self.assert_rigid_skin(skinned_rig(skeleton), result.pose, original, origin)
                np.testing.assert_allclose(observed['b2'][:3, 3], pole, rtol=0, atol=1e-12)
                self.assertTrue(result.converged, result.report())

    def test_rotated_triaxial_parent_accepts_scaled_quarter_turn(self):
        parent = (np.array([1., 2., 3., 4.]) / math.sqrt(30.)).tolist()
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                case = self.closed_parent_case(
                    solver, [128., 2., 1.], parent, [0., 0., 0., 1.], [1., 2., 1.])
                self.assert_local_turn_witness(case, -math.pi / 2, scale=[1., 2., .5])

    def test_axial_permutation_followed_by_parent_axis_turn(self):
        parent = (np.array([1., 2., 3., 4.]) / math.sqrt(30.)).tolist()
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                skeleton, pose, declaration = self.closed_parent_case(
                    solver, [128., 1., 1.], parent, [0., 0., 0., 1.], [1., 2., 1.])
                original = evaluate_fk(skeleton, pose)
                origin = original['b1'][:3, 3]
                reference = deepcopy(pose)
                rotation = qmul(quaternion([1., 0., 0.], 1.1), quaternion([0., 0., 1.], math.pi / 2))
                reference['bones']['b1'].update(rotation=rotation.tolist(), scale=[128., 1 / 128., 1.])
                pole = evaluate_fk(skeleton, reference)['b2'][:3, 3]
                result = solve_ik(skeleton, pose, declaration, origin, pole=pole)
                observed = self.assert_rigid_skin(skinned_rig(skeleton), result.pose, original, origin)
                np.testing.assert_allclose(observed['b2'][:3, 3], pole, rtol=0, atol=1e-12)
                self.assertTrue(result.converged, result.report())

    def test_triaxial_axis_exchange_with_roundoff_in_the_returned_pose(self):
        parent = (np.array([1., 2., 3., 4.]) / math.sqrt(30.)).tolist()
        for solver in ('two-bone', 'fabrik'):
            with self.subTest(solver=solver):
                skeleton, pose, declaration = self.closed_parent_case(
                    solver, [32., 2., 1.], parent, [0., 0., 0., 1.], [1., 2., 1.])
                original = evaluate_fk(skeleton, pose)
                origin = original['b1'][:3, 3]
                # Exchange X and Z and negate all three axes. The explicit
                # world half turn is attainable with local scales (32, 1, 1/32).
                frame = qmatrix(parent)
                turn = frame @ (-np.eye(3)[:, [2, 1, 0]]) @ frame.T
                pole = origin + turn @ (original['b2'][:3, 3] - origin)
                result = solve_ik(skeleton, pose, declaration, origin, pole=pole)
                observed = self.assert_rigid_skin(skinned_rig(skeleton), result.pose, original, origin)
                np.testing.assert_allclose(observed['b2'][:3, 3], pole, rtol=0, atol=1e-12)
                self.assertTrue(result.converged, result.report())

    def test_two_bone_oblique_first_segment_under_axial_scale(self):
        self.assert_attainable_pole('two-bone', [2., 1., 1.], [0., 0., 0., 1.], [1., 1., 0.])

    def test_fabrik_oblique_first_segment_under_axial_scale(self):
        self.assert_attainable_pole('fabrik', [2., 1., 1.], [0., 0., 0., 1.], [1., 1., 0.])

    def test_rotated_uniform_parent(self):
        self.assert_attainable_pole('fabrik', [2., 2., 2.],
                                    quaternion([0., 0., 1.], .6), [0., 1., 0.])

    def test_perpendicular_first_segment_under_axial_scale(self):
        self.assert_attainable_pole('fabrik', [2., 1., 1.], [0., 0., 0., 1.], [0., 1., 0.])

    def test_oblique_first_segment_under_uniform_parent(self):
        self.assert_attainable_pole('fabrik', [1., 1., 1.], [0., 0., 0., 1.], [1., 1., 0.])


if __name__ == '__main__':
    unittest.main()
