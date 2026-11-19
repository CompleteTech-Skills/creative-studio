import math
import unittest
from copy import deepcopy
import numpy as np
from rig_errors import RigError
from rig_ik import solve_ik
from rig_skeleton import evaluate_fk,neutral_pose
from test_rig_math import chain_skeleton
from test_rig_fold_matching_boundaries import chain


def closed(solver, *, parent_scale, first, origin=(0.,0.,0.), root_scale=(1.,1.,1.)):
    count = 3 if solver == 'two-bone' else 5
    skeleton = chain_skeleton(count+1)
    pose = neutral_pose(skeleton)
    pose['bones']['b0'] = dict(scale=list(parent_scale), translation=list(origin))
    pose['bones']['b1'] = dict(translation=[0.,0.,0.], scale=list(root_scale))
    first = np.array(first)
    side = np.array([0.,0.,1.])
    offsets = [first,-first] if count == 3 else [first,side,-first,-side]
    for idx, offset in enumerate(offsets,2):
        pose['bones'][f'b{idx}'] = dict(translation=offset.tolist())
    declaration = chain(count)
    declaration.update(solver=solver,bones=[f'b{i}' for i in range(1,count+1)])
    return skeleton,pose,declaration


class FoldReview(unittest.TestCase):
    def test_impossible_pole_is_invariant_to_distance(self):
        for solver in ('two-bone','fabrik'):
            for factor in (1.,1e16):
                with self.subTest(solver=solver,factor=factor):
                    skeleton,pose,declaration = closed(solver,parent_scale=[2.,3.,4.],first=[1.,0.,0.])
                    with self.assertRaises(RigError) as caught:
                        solve_ik(skeleton,pose,declaration,[0.,0.,0.],pole=factor*np.array([1.,2.,3.]))
                    self.assertEqual(caught.exception.report['code'],'RIG_SHEAR')

    def test_impossible_yz_pole_is_invariant_to_translation_along_x(self):
        for solver in ('two-bone','fabrik'):
            for shift in (0.,1e8):
                with self.subTest(solver=solver,shift=shift):
                    origin = np.array([shift,0.,0.])
                    skeleton,pose,declaration = closed(solver,parent_scale=[2.,3.,4.],first=[0.,1e-7,0.],origin=origin)
                    with self.assertRaises(RigError) as caught:
                        solve_ik(skeleton,pose,declaration,origin,pole=origin+[0.,1.,.1])
                    self.assertEqual(caught.exception.report['code'],'RIG_SHEAR')

    def test_rigid_attainable_turn_with_nonsingular_seed_scale(self):
        for solver in ('two-bone','fabrik'):
            for scale_y in (1.2e-10,1.01e-10):
                with self.subTest(solver=solver,scale_y=scale_y):
                    skeleton,pose,declaration = closed(solver,parent_scale=[2.,1.,1.],first=[1.,0.,1.],root_scale=[1.,scale_y,1.])
                    original = evaluate_fk(skeleton,pose)
                    witness = deepcopy(pose)
                    witness['bones']['b1']['rotation'] = [math.sin(math.pi/4),0.,0.,math.cos(math.pi/4)]
                    world = evaluate_fk(skeleton,witness)
                    turn = world['b1'][:3,:3] @ np.linalg.inv(original['b1'][:3,:3])
                    np.testing.assert_allclose(turn.T @ turn,np.eye(3),atol=1e-14,rtol=0.)
                    self.assertAlmostEqual(float(np.linalg.det(turn)),1.,delta=1e-14)
                    target = world[declaration['bones'][-1]][:3,3]
                    np.testing.assert_array_equal(target,np.zeros(3))
                    pole = world['b2'][:3,3]
                    result = solve_ik(skeleton,pose,declaration,target,pole=pole)
                    self.assertTrue(result.converged)
                    observed = evaluate_fk(skeleton,result.pose)
                    np.testing.assert_allclose(observed['b2'][:3,3],pole,atol=1e-12,rtol=0.)
