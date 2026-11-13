from unittest.mock import patch
import unittest
import numpy as np
from rig_errors import RigError
from rig_surface_compare import match_rows


class SurfaceComparisonTests(unittest.TestCase):
    def test_bin_boundary_and_capacity_reassignment_preserve_multiplicity(self):
        expected=np.array([[.99,0.,0.],[.99,0.,0.],[2.,0.,0.]])
        actual=np.array([[1.01,0.,0.],[1.01,0.,0.],[2.01,0.,0.]])
        self.assertEqual(match_rows(expected,actual,tolerance=.1)['records'],3)
        # First source has two candidates and prefers the only candidate for the
        # second source. A greedy match would reject this valid multiset.
        expected=np.array([[0.,0.,0.],[.15,0.,0.]])
        actual=np.array([[.075,0.,0.],[-.09,0.,0.]])
        self.assertEqual(match_rows(expected,actual,tolerance=.1)['records'],2)
        with self.assertRaises(RigError):
            match_rows(np.array([[0.,0.,0.],[0.,0.,0.]]),np.array([[0.,0.,0.],[1.,0.,0.]]))

    def test_oriented_faces_allow_cyclic_order_but_not_winding_reversal(self):
        triangle=np.array([[0.,0.,0.,1.,0.,0.,0.,1.,0.]])
        self.assertEqual(match_rows(triangle,np.roll(triangle,3,axis=1),cyclic_width=3)['records'],1)
        with self.assertRaises(RigError):match_rows(triangle,triangle.reshape(1,3,3)[:,::-1].reshape(1,9),cyclic_width=3)
        # Tiny faces must not pass reversed winding merely because every corner
        # is inside the component tolerance.
        tiny=triangle*1e-8
        with self.assertRaises(RigError):match_rows(tiny,tiny.reshape(1,3,3)[:,::-1].reshape(1,9),cyclic_width=3)

    def test_face_multiplicity_and_corner_attributes_are_bound(self):
        triangle=np.array([[0.,0.,0.,.1,1.,0.,0.,.2,0.,1.,0.,.3]])
        actual=np.vstack([np.roll(triangle,4,axis=1),triangle])
        self.assertEqual(match_rows(np.repeat(triangle,2,axis=0),actual,cyclic_width=4)['records'],2)
        changed=triangle.copy();changed[0,3]=.9
        with self.assertRaises(RigError):match_rows(triangle,changed,cyclic_width=4)

    def test_preallocation_work_and_position_budgets_fail_explicitly(self):
        points=np.array([[0.,0.,0.],[1.,0.,0.]])
        with patch('rig_surface_compare.np.unique',side_effect=AssertionError('must not allocate')):
            with self.assertRaises(RigError) as caught:match_rows(points,points,memory_bytes=1)
        self.assertEqual(caught.exception.report['code'],'RIG_ROUNDTRIP_COMPARE_LIMIT')
        with self.assertRaises(RigError) as caught:match_rows(points,points+.000001,maximum_work=1)
        self.assertEqual(caught.exception.report['code'],'RIG_ROUNDTRIP_COMPARE_LIMIT')
        with self.assertRaises(RigError):match_rows(points,points+.009,tolerance=.01,position_tolerance=.01)
        self.assertLess(match_rows(points,points+np.array([.009,0.,0.]),tolerance=.01,position_tolerance=.01)['max_position_error'],.01)
