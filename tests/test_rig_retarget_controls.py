"""Authored retarget controls must affect real weighted geometry and fail closed."""
from copy import deepcopy
import math
import tempfile
import unittest
import numpy as np
from rig_test_helpers import fixture
from rig_errors import RigError
from rig_math import axis_angle, decompose, qangle, qmul, qmatrix
from rig_models import Geometry, Mesh, SkinWeights
from rig_retarget import build_retarget_profile, retarget_pose, retarget_animation
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone, skeleton_from_heads
from rig_skinning import evaluate_deformation


def dof(rotation='free', **changes):
    result = dict(translation=[True]*3, scale=[True]*3, rotation=rotation)
    result.update(changes)
    return result


def twist_chain(rig, *, rolled=False):
    result = deepcopy(rig);result.manifest['id'] = 'target'
    rotation = qmatrix(axis_angle([0, 0, 1], .45)) if rolled else np.eye(3)
    joints = [('b0', 'joint-0', 0.), ('b1', 'joint-1', 1.),
              ('twist-a', 'twist-a', 4/3), ('twist-b', 'twist-b', 5/3), ('b2', 'joint-2', 2.)]
    specs = []
    for i, (name, role, y) in enumerate(joints):
        specs.append(dict(id=name, role=role, parent=joints[i-1][0] if i else None,
            head=(rotation @ [0., y, 0.]).tolist(), tail=(rotation @ [0., y+.2, 0.]).tolist(), deform=True))
    result.manifest['skeleton'] = skeleton_from_heads(specs)
    result.manifest['controllers'] = dict(schema_version=1, constraints=[], ik=[])
    positions = np.array([rotation @ [x, y, 0.] for _, _, y in joints for x in (.1, -.1)])
    result.geometry = Geometry([Mesh('strip', positions, np.array([[0, 1, 2], [1, 3, 2]]))])
    world = evaluate_fk(result.skeleton);names = [name for name, _, _ in joints]
    result.weights = {'strip': SkinWeights('strip', names,
        np.repeat(np.arange(5, dtype=np.uint16), 2)[:, None], np.ones((10, 1)),
        np.array([np.linalg.inv(world[name]) for name in names]))}
    result.clips = {}
    return result


def distribution():
    return {'b1': dict(axis=[0., 1., 0.], recipients=[
        dict(bone='twist-a', weight=1/3), dict(bone='twist-b', weight=1/3)])}


class RetargetControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.source = fixture(self.temp.name)

    def test_twist_is_shared_with_actual_weighted_vertices_and_preserves_mapped_endpoint(self):
        source = self.source;target = twist_chain(source)
        profile = build_retarget_profile(source, target, twist_distribution=distribution())
        pose = rotate_bone(source.skeleton, neutral_pose(source.skeleton), 'b1', axis_angle([0, 1, 0], .9))
        actual = retarget_pose(source, target, pose, profile)
        vertices = evaluate_deformation(target, actual)['strip']['positions']
        for i, angle in enumerate((0., .3, .6, .9, .9)):
            y = target.geometry.meshes[0].positions[2*i, 1]
            np.testing.assert_allclose(vertices[2*i], [.1*math.cos(angle), y, -.1*math.sin(angle)], atol=1e-12)
        np.testing.assert_allclose(evaluate_fk(target.skeleton, actual)['b2'],
                                   evaluate_fk(source.skeleton, pose)['b2'], atol=1e-12)
        self.assertGreater(np.max(np.abs(vertices-target.geometry.meshes[0].positions)), .05)

    def test_rest_calibration_and_swing_survive_distribution(self):
        source = self.source;target = twist_chain(source, rolled=True)
        profile = build_retarget_profile(source, target, twist_distribution=distribution())
        delta = qmul(axis_angle([1, 0, 0], .35), axis_angle([0, 1, 0], .9))
        pose = rotate_bone(source.skeleton, neutral_pose(source.skeleton), 'b1', delta)
        plain = retarget_pose(source, target, pose, build_retarget_profile(source, target))
        shared = retarget_pose(source, target, pose, profile)
        # Mapped world-space joint orientations remain calibrated even though
        # the intermediate weighted twist bones now carry different rotations.
        before = decompose(evaluate_fk(target.skeleton, plain)['b2'])['rotation']
        after = decompose(evaluate_fk(target.skeleton, shared)['b2'])['rotation']
        self.assertLess(qangle(before, after), 1e-8)
        self.assertGreater(np.max(np.abs(evaluate_deformation(target, plain)['strip']['positions']-
                                        evaluate_deformation(target, shared)['strip']['positions'])), .01)

    def test_invalid_twist_topology_ownership_weights_and_axes_are_rejected(self):
        target = twist_chain(self.source)
        changes = [lambda d:d['b1'].update(axis=[0, 0, 0]),
                   lambda d:d['b1']['recipients'][0].update(weight=1.),
                   lambda d:d['b1']['recipients'][1].update(bone='twist-a'),
                   lambda d:d['b1']['recipients'][1].update(bone='b2'),
                   lambda d:d['b1']['recipients'].reverse(),
                   lambda d:d['b1']['recipients'][0].update(bone='missing'),
                   lambda d:d.update(missing=d.pop('b1'))]
        for change in changes:
            controls = distribution();change(controls)
            with self.subTest(change=change), self.assertRaises(RigError):
                build_retarget_profile(self.source, target, twist_distribution=controls)
        with self.assertRaises(RigError):
            build_retarget_profile(self.source, target, twist_distribution=distribution(),
                                   supported_dofs={'twist-a': dof('locked')})
        target.skeleton['bones'][2]['rest']['translation'][0] = .1
        with self.assertRaises(RigError):
            build_retarget_profile(self.source, target, twist_distribution=distribution())

    def test_supported_translation_scale_and_twist_dofs_change_mesh_as_specified(self):
        source = self.source;target = deepcopy(source);target.manifest['id'] = 'target'
        profile = build_retarget_profile(source, target, supported_dofs={
            'b0': dof('twist', axis=[0, 1, 0], translation=[False, True, False], scale=[True, False, False])})
        pose = neutral_pose(source.skeleton)
        pose['bones']['b0'] = dict(translation=[.3, .4, .5], scale=[2., 3., 4.],
            rotation=axis_angle([0, 1, 0], .6).tolist())
        result = retarget_pose(source, target, pose, profile)
        np.testing.assert_allclose(result['bones']['b0']['translation'], [0, .4, 0], atol=1e-12)
        np.testing.assert_allclose(result['bones']['b0']['scale'], [2, 1, 1], atol=1e-12)
        self.assertLess(qangle(result['bones']['b0']['rotation'], axis_angle([0, 1, 0], .6)), 1e-8)
        np.testing.assert_allclose(evaluate_deformation(target, result)['strip']['positions'][0],
                                   [.2*math.cos(.6), .4, -.2*math.sin(.6)], atol=1e-12)
        # Exercise swing rejection separately from nonuniform scale, since the
        # existing TRS evaluator intentionally rejects sheared world matrices.
        pose['bones']['b0']['scale'] = [1., 1., 1.]
        pose['bones']['b0']['rotation'] = qmul(axis_angle([1, 0, 0], .5), axis_angle([0, 1, 0], .6)).tolist()
        result = retarget_pose(source, target, pose, profile)
        self.assertLess(qangle(result['bones']['b0']['rotation'], axis_angle([0, 1, 0], .6)), 1e-8)
        locked = build_retarget_profile(source, target, supported_dofs={'b0': dof('locked')})
        result = retarget_pose(source, target, pose, locked)
        self.assertLess(qangle(result['bones']['b0']['rotation']), 1e-8)

    def test_supported_dof_admission_and_constraint_conflicts_fail_closed(self):
        target = deepcopy(self.source);target.manifest['id'] = 'target'
        for controls in ({'missing': dof()}, {'b1': dof('twist')},
                         {'b1': dof('twist', axis=[0, 0, 0])}, {'b1': dof('locked', axis=[0, 1, 0])},
                         {'b1': dof(translation=[True, False])}, {'b1': dof(scale=[1, 0, 0])}):
            with self.subTest(controls=controls), self.assertRaises(RigError):
                build_retarget_profile(self.source, target, supported_dofs=controls)
        target.manifest['controllers']['constraints'] = [dict(schema_version=1, id='hinge', bone='b1',
            type='hinge', axis=[1, 0, 0], minimum=.4, maximum=.8)]
        profile = build_retarget_profile(self.source, target, supported_dofs={'b1': dof('locked')})
        with self.assertRaises(RigError) as caught:
            retarget_pose(self.source, target, neutral_pose(self.source.skeleton), profile)
        self.assertEqual(caught.exception.code, 'RIG_RETARGET_DOF_CONFLICT')

    def test_restricted_contact_chain_is_explicitly_unsupported_and_profile_report_is_retained(self):
        source = self.source;target = deepcopy(source);target.manifest['id'] = 'target'
        profile = build_retarget_profile(source, target, supported_dofs={'b1': dof('locked')})
        clip = deepcopy(source.clips['neutral'])
        clip['contacts'] = [dict(schema_version=1, id='hold', chain='arm', start=0., end=1.,
                                  target=[0., 2., 0.], kind='hand')]
        with self.assertRaises(RigError) as caught:retarget_animation(source, target, clip, profile=profile)
        self.assertEqual(caught.exception.code, 'RIG_RETARGET_DOF_CONTACT')
        clip['contacts'] = []
        report = retarget_animation(source, target, clip, profile=profile)['report']
        self.assertEqual(report['supported_dofs'], profile['supported_dofs'])
        self.assertEqual(report['twist_distribution'], {})

    def test_legacy_profile_without_optional_controls_remains_identical(self):
        source = self.source;pose = rotate_bone(source.skeleton, neutral_pose(source.skeleton), 'b1', axis_angle([0, 0, 1], .4))
        old = build_retarget_profile(source, source)
        explicit = build_retarget_profile(source, source, supported_dofs={}, twist_distribution={})
        self.assertEqual(retarget_pose(source, source, pose, old), retarget_pose(source, source, pose, explicit))
