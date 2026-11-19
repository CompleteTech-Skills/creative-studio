"""Gaze-owned lids measure the final eye rotation in the parent's rest frame."""
import unittest

import numpy as np

from rig_facial import refresh_gaze_eyelids
from rig_math import axis_angle, qmatrix, qmul
from rig_operations import pose_operation, solve_operation
from rig_skeleton import evaluate_fk, neutral_pose, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from test_rig_specializations import eyelid_fixture


def oriented_fixture(parent_scale, eye_rotation):
    rig = eyelid_fixture()
    root, _, left_eye, _ = rig.skeleton['bones']
    parent_rotation = axis_angle([1, 2, 3], .6)
    root['rest'].update(rotation=parent_rotation.tolist(), scale=parent_scale)
    left_eye['rest']['rotation'] = eye_rotation.tolist()
    parent_frame = qmatrix(parent_rotation) @ np.diag(parent_scale)
    for bone in rig.skeleton['bones']:
        head, tail = parent_frame @ bone['head'], parent_frame @ bone['tail']
        bone.update(head=head.tolist(), tail=tail.tolist(), length=float(np.linalg.norm(tail-head)))
    validate_skeleton(rig.skeleton)
    inverse_bind = np.linalg.inv(evaluate_fk(rig.skeleton)['head'])
    for mesh in rig.geometry.meshes:
        mesh.positions = mesh.positions @ parent_frame.T
        mesh.morphs['lift']['POSITION'] = mesh.morphs['lift']['POSITION'] @ parent_frame.T
        rig.weights[mesh.id].inverse_bind = inverse_bind[None, :, :]
        validate_skin(rig.weights[mesh.id], mesh, rig.skeleton)
    return rig, parent_frame


class EyelidLocalPitchTests(unittest.TestCase):
    def test_omitted_explicit_and_antipodal_rest_return_exact_default(self):
        rest = axis_angle([2, 3, -1], .7)
        rig, _ = oriented_fixture([1., 1., 1.], rest)
        rig.manifest['facial']['eyelids'][0].update(up_radians=1e-16, down_radians=1e-16)
        for rotation in (None, rest, -rest):
            with self.subTest(rotation=rotation):
                pose = neutral_pose(rig.skeleton)
                pose['bones']['head'] = dict(rotation=axis_angle([2, 1, 3], .9).tolist(),
                                             scale=[.6, 2., 1.3])
                if rotation is not None:
                    pose['bones']['eye.left'] = {'rotation': rotation.tolist()}
                sampled = refresh_gaze_eyelids(rig, pose)
                self.assertEqual(sampled['morphs']['lid.left']['lift'], .25)

    def test_public_pose_and_ik_keep_locked_eye_at_authored_default(self):
        rig, _ = oriented_fixture([1., 1., 1.], axis_angle([0, 1, 0], .8))
        rig.manifest['facial']['eyelids'][0].update(up_radians=1e-16, down_radians=1e-16)
        rig.manifest['controllers']['constraints'] = [dict(
            schema_version=1, id='locked-pitch', bone='eye.left', type='hinge',
            axis=[1, 0, 0], minimum=0., maximum=0.)]
        rig.manifest['controllers']['ik'] = [dict(
            schema_version=1, id='chain', bones=['root', 'head', 'eye.right'],
            solver='two-bone', pole=[0, 1, 2], iterations=8, tolerance=.001)]
        world = evaluate_fk(rig.skeleton)
        gaze = (world['eye.left'][:3, 3] + [0., 1., 2.]).tolist()
        pose = pose_operation(rig, dict(gaze=gaze))
        solved = solve_operation(rig, dict(gaze=gaze, chain='chain',
                                           target=world['eye.right'][:3, 3].tolist()))
        self.assertEqual(solved['report']['pose'], solved['pose'])
        for name, result in (('pose', pose), ('ik', solved['pose'])):
            with self.subTest(operation=name):
                self.assertEqual(result['morphs']['lid.left']['lift'], .25)
                actual = evaluate_deformation(rig, result)['lid.left']['positions']
                at_default = dict(result, morphs={})
                np.testing.assert_array_equal(
                    actual, evaluate_deformation(rig, at_default)['lid.left']['positions'])

    def test_representable_tiny_local_pitch_keeps_both_directions(self):
        # A rolled rest eye leaves zero X/Y quaternion components, so adding
        # these small parent-space pitches survives storage in absolute XYZW.
        rest = axis_angle([0, 0, 1], .7)
        rig = eyelid_fixture()
        eye = next(bone for bone in rig.skeleton['bones'] if bone['id'] == 'eye.left')
        eye['rest']['rotation'] = rest.tolist()
        validate_skeleton(rig.skeleton)
        rig.manifest['facial']['eyelids'][0].update(up_radians=1e-16, down_radians=2e-16)
        for pitch, expected in ((5e-17, .525), (-5e-17, .1375)):
            for sign in (1, -1):
                with self.subTest(pitch=pitch, sign=sign):
                    pose = neutral_pose(rig.skeleton)
                    rotation = qmul(axis_angle([-1, 0, 0], pitch), rest)
                    pose['bones']['eye.left'] = {'rotation': (sign * rotation).tolist()}
                    sampled = refresh_gaze_eyelids(rig, pose)
                    self.assertAlmostEqual(sampled['morphs']['lid.left']['lift'], expected, places=14)

    def test_one_representable_component_step_from_an_oblique_eye_rest(self):
        rig = eyelid_fixture()
        eye = next(bone for bone in rig.skeleton['bones'] if bone['id'] == 'eye.left')
        eye['rest']['rotation'] = [.5, .5, .5, .5]
        validate_skeleton(rig.skeleton)
        stepped = float(np.nextafter(.5, 1.))
        step = stepped - .5
        # Raising only X by one step rotates rest forward down by one step
        # to first order. The second-order error is far below weight precision.
        rig.manifest['facial']['eyelids'][0]['down_radians'] = 2 * step
        pose = neutral_pose(rig.skeleton)
        pose['bones']['eye.left'] = dict(rotation=[stepped, .5, .5, .5])
        sampled = refresh_gaze_eyelids(rig, pose)
        self.assertAlmostEqual(sampled['morphs']['lid.left']['lift'], .025, places=14)

    def test_tiny_pitch_survives_a_rotated_scaled_parent(self):
        rig, frame = oriented_fixture([2., .5, 1.5], axis_angle([1, 0, 0], 0.))
        rig.manifest['facial']['eyelids'][0].update(up_radians=1e-16, down_radians=1e-16)
        forward = np.linalg.solve(frame, [0, 0, 1])
        up = np.linalg.solve(frame, [0, 1, 0])
        axis = np.cross(forward, up)
        for pitch, expected in ((5e-17, .525), (-5e-17, .025)):
            with self.subTest(pitch=pitch):
                pose = neutral_pose(rig.skeleton)
                pose['bones']['eye.left'] = dict(rotation=axis_angle(axis, pitch).tolist())
                sampled = refresh_gaze_eyelids(rig, pose)
                self.assertAlmostEqual(sampled['morphs']['lid.left']['lift'], expected, places=14)

    def test_nonzero_pitch_uses_rotated_and_scaled_parent_rest_axes(self):
        # Equal X/Y scales allow a rolled rest eye without shearing its rest transform.
        rest = axis_angle([0, 0, 1], -.8)
        for scale in ([1., 1., 1.], [2., 2., .5]):
            rig, frame = oriented_fixture(scale, rest)
            # Rotating about this axis tips authored forward toward authored up.
            forward = np.linalg.solve(frame, [0, 0, 1])
            up = np.linalg.solve(frame, [0, 1, 0])
            axis = np.cross(forward, up)
            for pitch, expected in ((.2, .47), (-.2, .025)):
                with self.subTest(scale=scale, pitch=pitch):
                    pose = neutral_pose(rig.skeleton)
                    pose['bones']['head'] = dict(rotation=axis_angle([1, -2, 1], .9).tolist(),
                                                 scale=[.9, 1.2, 3.])
                    pose['bones']['eye.left'] = dict(rotation=qmul(axis_angle(axis, pitch), rest).tolist())
                    sampled = refresh_gaze_eyelids(rig, pose)
                    self.assertAlmostEqual(sampled['morphs']['lid.left']['lift'], expected, places=13)


if __name__ == '__main__':
    unittest.main()
