"""Closed-chain rigid turns on strongly anisotropic native seeds."""
from copy import deepcopy
import math
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_errors import RigError
from rig_ik import solve_ik, validate_controllers
from rig_math import axis_angle, qmatrix, qmul
from rig_models import SkinWeights
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import chain_skeleton, fixture
from test_rig_fold_review_boundaries import closed


def track(bone, path, before, after):
    return dict(schema_version=1, target=bone, path=path, times=[0.0, 0.5],
                values=[before, after], interpolation='STEP')


def nested_parent(scales, offset):
    skeleton, pose, chain = closed('two-bone', parent_scale=scales, first=offset)
    root = skeleton['bones'][0]
    parent = deepcopy(root)
    parent['id'] = 'extra'
    root['parent'] = parent['id']
    skeleton['root'] = parent['id']
    skeleton['bones'].insert(0, parent)
    factors = [2.**(math.frexp(value)[1] // 2) if value > 1 else 1. for value in scales]
    pose['bones']['extra'] = dict(scale=factors)
    pose['bones']['b0']['scale'] = [value / factor for value, factor in zip(scales, factors)]
    return skeleton, pose, chain


class FoldRotatedSeedTests(unittest.TestCase):
    def check_native_turn(self, parent_rotation, seed_rotation, turn_rotation, power):
        with tempfile.TemporaryDirectory() as directory:
            rig = fixture(directory)
            rig.manifest['skeleton'] = chain_skeleton(4)
            mesh = rig.geometry.meshes[0]
            count = len(mesh.positions)
            inverse = np.linalg.inv(evaluate_fk(rig.skeleton)['b1'])[None]
            skin = SkinWeights(mesh.id, ['b1'], np.zeros((count, 4), dtype=np.uint16),
                               np.tile([1., 0., 0., 0.], (count, 1)), inverse)
            rig.weights = {mesh.id: skin}
            validate_skin(skin, mesh, rig.skeleton)

            parent_axes = qmatrix(parent_rotation)
            world_turn = parent_axes @ qmatrix(turn_rotation) @ parent_axes.T
            scale = [2.**power, .125, 2.**power]
            offset = np.array([2.**(-power), 0., 0.])
            pole = world_turn @ (parent_axes @ (scale * (qmatrix(seed_rotation) @ offset)))
            names = ['b1', 'b2', 'b3']
            chain = rig.manifest['controllers']['ik'][0]
            chain.update(bones=names, solver='two-bone', pole=pole.tolist())
            validate_controllers(rig.skeleton, rig.manifest['controllers'])

            identity, zero, tail = [0., 0., 0., 1.], [0.]*3, [0., 1., 0.]
            tracks = [
                track('b0', 'scale', [1.]*3, scale),
                track('b0', 'rotation', identity, parent_rotation.tolist()),
                track('b1', 'rotation', identity, seed_rotation.tolist()),
                track('b1', 'translation', tail, zero),
                track('b2', 'translation', tail, offset.tolist()),
                track('b3', 'translation', tail, (-offset).tolist()),
            ]
            contact = dict(schema_version=1, id='plant', chain=chain['id'],
                           start=.5, end=1., target=zero, kind='hand')
            clip = dict(deepcopy(rig.clips['neutral']), id='rotated-seed', tracks=tracks, contacts=[contact])
            validate_animation(rig, clip)
            rig.clips = {clip['id']: clip}
            seed = evaluate_animation(rig, clip, .5, contacts=False)['pose']
            initial_fk = evaluate_fk(rig.skeleton, seed)
            initial_vertices = evaluate_deformation(rig, seed)[mesh.id]['positions']

            def check(pose):
                validate_contract(pose, 'Pose')
                world = evaluate_fk(rig.skeleton, pose)
                np.testing.assert_allclose(world['b2'][:3, 3], pole, rtol=0, atol=3e-14)
                np.testing.assert_allclose(world['b3'][:3, 3], zero, rtol=0, atol=3e-14)
                for name in names:
                    expected = world_turn @ initial_fk[name][:3, :3]
                    size = np.max(np.abs(expected), axis=1, keepdims=True)
                    np.testing.assert_allclose(world[name][:3, :3] / size, expected / size,
                                               rtol=0, atol=3e-14)
                vertices = evaluate_deformation(rig, pose)[mesh.id]['positions']
                self.assertTrue(np.isfinite(vertices).all())
                np.testing.assert_allclose(vertices, (world_turn @ initial_vertices.T).T, rtol=3e-14,
                                           atol=3e-14 * np.max(np.abs(initial_vertices)))

            witness = deepcopy(seed)
            witness['bones']['b1']['rotation'] = qmul(turn_rotation, seed_rotation).tolist()
            check(witness)
            direct = solve_ik(rig.skeleton, seed, chain, zero)
            self.assertTrue(direct.converged)
            check(direct.pose)
            animated = evaluate_animation(rig, clip, .5)
            self.assertTrue(animated['contacts'][0]['converged'])
            check(animated['pose'])
            runtime = RigStateRuntime(rig, 'static-positive', recipes={'static-positive': clip['id']},
                                      selected=['static-positive'])
            self.assertEqual(runtime.evaluate(.25)['status'], 'playing')
            sample = runtime.evaluate(.5)
            self.assertEqual(sample['status'], 'playing')
            self.assertEqual(runtime.time, .5)
            check(sample['pose'])

    def test_oblique_parent_and_mixed_seed_admit_known_rigid_turns(self):
        cases = (
            (axis_angle([1., 1., 1.], math.pi / 4), axis_angle([1., 0., 0.], math.pi / 4),
             axis_angle([0., -1., 0.], math.pi / 2), (13, 29)),
            (np.array([0., 0., 0., 1.]), axis_angle([1., 2., 3.], math.pi / 4),
             axis_angle([0., 1., 0.], .2), (15, 29)),
        )
        for case, (parent, seed, turn, powers) in enumerate(cases):
            for power in powers:
                with self.subTest(case=case, power=power):
                    self.check_native_turn(parent, seed, turn, power)

    def test_each_dominant_parent_axis_admits_oblique_rigid_half_turns(self):
        seed_rotation = axis_angle([1., 2., 3.], math.pi / 4)
        parent_rotation = axis_angle([2., .3, 1.], .9)
        for axis in range(3):
            for power in (15, 29, 44):
                with self.subTest(axis=axis, power=power):
                    large = 2.**power
                    scales = [.125] * 3
                    scales[axis] = large
                    skeleton, pose, chain = nested_parent(scales, [1./large, 0., 0.])
                    pose['bones']['extra']['rotation'] = parent_rotation.tolist()
                    pose['bones']['b1']['rotation'] = seed_rotation.tolist()
                    validate_skeleton(skeleton)
                    validate_contract(pose, 'Pose')
                    turn_axis = [.3, 1.7, 1.]
                    turn_axis[axis] = 0.
                    witness = deepcopy(pose)
                    witness['bones']['b1']['rotation'] = qmul(
                        axis_angle(turn_axis, math.pi), seed_rotation).tolist()
                    native = evaluate_fk(skeleton, witness)
                    pole = native['b2'][:3, 3]
                    target = native['b3'][:3, 3]
                    solution = solve_ik(skeleton, pose, chain, target, pole=pole)
                    self.assertTrue(solution.converged)
                    actual = evaluate_fk(skeleton, solution.pose)
                    np.testing.assert_allclose(actual['b2'][:3, 3], pole, rtol=0, atol=1e-14)
                    np.testing.assert_allclose(actual['b3'][:3, 3], target, rtol=0, atol=1e-14)

    def test_near_parallel_root_columns_do_not_hide_transverse_shear(self):
        large = 2.**50
        scales = [large, 1., .25]
        skeleton, pose, chain = nested_parent(scales, [1./large, 0., 0.])
        pose['bones']['b1']['rotation'] = axis_angle([1., 2., 3.], math.pi / 4).tolist()
        validate_skeleton(skeleton)
        validate_contract(pose, 'Pose')
        source = evaluate_fk(skeleton, pose)['b2'][:3, 3]
        turn = qmatrix(axis_angle([1., 0., 0.], .6)) @ np.diag([-1., 1., -1.])
        before = deepcopy(pose)
        pole = turn @ source
        try:
            solution = solve_ik(skeleton, pose, chain, [0.]*3, pole=pole)
        except RigError as error:
            self.assertEqual(error.report['code'], 'RIG_SHEAR')
        else:
            self.assertTrue(solution.converged)
            seed_rotation = qmatrix(pose['bones']['b1']['rotation'])
            final = solution.pose['bones']['b1']
            relative = qmatrix(final['rotation']) @ np.diag(final['scale']) @ seed_rotation.T
            # A large X column conceals these independent Y/Z changes if the
            # test uses only a world-column norm to evaluate the accepted turn.
            transverse = np.diag(scales[1:]) @ relative[1:, 1:] @ np.diag(1/np.asarray(scales[1:]))
            singular = np.linalg.svd(transverse, compute_uv=False)
            np.testing.assert_allclose(singular, [1., 1.], rtol=0, atol=1e-12)
            actual = evaluate_fk(skeleton, solution.pose)['b2'][:3, 3]
            np.testing.assert_allclose(actual / math.hypot(*actual), pole / math.hypot(*pole),
                                       rtol=0, atol=2e-14)
        self.assertEqual(pose, before)
