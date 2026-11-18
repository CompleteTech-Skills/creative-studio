from copy import deepcopy
import math
import unittest
from test_rig_math import chain_skeleton
from test_rig_fold_matching_boundaries import chain
from rig_ik import solve_ik
from rig_skeleton import evaluate_fk,neutral_pose
import numpy as np

class Integration(unittest.TestCase):
    def test_translated_scaled(self):
        for solver in ('two-bone','fabrik'):
            with self.subTest(solver=solver):
                count=3 if solver=='two-bone' else 5
                s=chain_skeleton(count+1);p=neutral_pose(s)
                p['bones']['b0']={'translation':[1000.,1000.,1000.],'scale':[2.,1.,1.]}
                first=[1,1,0]; back=[-1,-1,0]
                offsets=[first,back] if count==3 else [first,[0,0,1],back,[0,0,-1]]
                for i,t in enumerate(offsets,2):p['bones'][f'b{i}']={'translation':t}
                w=deepcopy(p)
                w['bones']['b1']={'rotation':[math.sin(.175),0.,0.,math.cos(.175)]}
                ow=evaluate_fk(s,p); fw=evaluate_fk(s,w); origin=ow['b1'][:3,3]; pole=fw['b2'][:3,3]
                np.testing.assert_array_equal(fw[f'b{count}'][:3,3],origin)
                turn=fw['b1'][:3,:3]@np.linalg.inv(ow['b1'][:3,:3])
                np.testing.assert_allclose(turn.T@turn,np.eye(3),rtol=0,atol=1e-14)
                c=chain(count);c.update(bones=[f'b{i}' for i in range(1,count+1)],solver=solver)
                solution=solve_ik(s,p,c,origin,pole=pole)
                self.assertTrue(solution.converged)
                computed=evaluate_fk(s,solution.pose)
                np.testing.assert_allclose(computed['b2'][:3,3],pole,rtol=0,atol=8*np.spacing(1000.))
if __name__=='__main__':unittest.main()
