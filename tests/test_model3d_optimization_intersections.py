from pathlib import Path
import sys
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_optimization_intersections import triangle_contacts,geometry_contacts
from rig_models import Mesh,Geometry
from rig_errors import RigError

class TriangleContacts(unittest.TestCase):
    def pair(self,a,b):return Mesh('test',np.array(a+b,dtype=float),np.array([[0,1,2],[3,4,5]]))
    def test_actual_crossing_nonadjacent_triangles_and_source_unchanged(self):
        mesh=self.pair([[-1,-1,0],[1,-1,0],[0,1,0]],[[0,0,-1],[0,0,1],[0,.5,1]])
        before=mesh.positions.copy();report=triangle_contacts(mesh)
        self.assertEqual(report['contacts'],[dict(triangles=[0,1],kind='noncoplanar')])
        np.testing.assert_array_equal(mesh.positions,before)
    def test_coplanar_overlap_containment_and_disjoint(self):
        a=[[0,0,0],[2,0,0],[0,2,0]]
        for b in [[[.1,.1,0],[.2,.1,0],[.1,.2,0]],[[2,0,0],[3,0,0],[2,1,0]]]:
            self.assertEqual(triangle_contacts(self.pair(a,b))['contacts'][0]['kind'],'coplanar')
        self.assertEqual(triangle_contacts(self.pair(a,[[2,2,0],[3,2,0],[2,3,0]]))['contacts'],[])
    def test_exact_tiny_plane_separation_is_not_epsilon_contact(self):
        a=[[0,0,0],[1,0,0],[0,1,0]];b=[[0,0,1e-100],[1,0,1e-100],[0,1,1e-100]]
        self.assertEqual(triangle_contacts(self.pair(a,b))['contact_count'],0)
    def test_large_offset_and_small_scale_keep_crossing_result(self):
        a=[[-1,-1,0],[1,-1,0],[0,1,0]];b=[[0,0,-1],[0,0,1],[0,.5,1]]
        for scale,offset in [(1e-50,0),(1,1e10)]:
            m=self.pair(a,b);m.positions=m.positions*scale+offset
            self.assertEqual(triangle_contacts(m)['contact_count'],1)
    def test_indexed_adjacency_excluded_but_duplicate_positions_report_contact(self):
        mesh=Mesh('quad',np.array([[0.,0,0],[1,0,0],[0,1,0],[1,1,0]]),np.array([[0,1,2],[1,3,2]]))
        self.assertEqual(triangle_contacts(mesh)['adjacent_pairs_excluded'],1)
        dup=self.pair([[0,0,0],[1,0,0],[0,1,0]],[[0,0,0],[1,0,0],[0,1,0]])
        self.assertEqual(triangle_contacts(dup)['contact_count'],1)
    def test_pair_budget_and_degenerate_fail_with_structured_diagnostics(self):
        mesh=self.pair([[0,0,0],[1,0,0],[0,1,0]],[[0,0,0],[1,0,0],[0,1,0]])
        mesh.positions=np.concatenate([mesh.positions,mesh.positions[:3]]);mesh.triangles=np.array([[0,1,2],[3,4,5],[6,7,8]])
        with self.assertRaises(RigError):triangle_contacts(mesh,max_pairs=1)
        with self.assertRaises(RigError):triangle_contacts(mesh,max_triangles=2)
        mesh.triangles=np.array([[0,0,1]])
        with self.assertRaises(RigError):triangle_contacts(mesh)
    def test_cross_part_contacts_retain_mesh_and_local_triangle_identity(self):
        m=self.pair([[0,0,0],[1,0,0],[0,1,0]],[[0,0,0],[1,0,0],[0,1,0]])
        a=Mesh('left',m.positions[:3],np.array([[0,1,2]]))
        b=Mesh('right',m.positions[3:],np.array([[0,1,2]]))
        result=geometry_contacts(Geometry([a,b],[{}]))
        self.assertEqual(result['contacts'][0]['parts'],[{'mesh':'left','triangle':0},{'mesh':'right','triangle':0}])
    def test_invalid_limits_and_nonfinite_reject(self):
        mesh=self.pair([[0,0,0],[1,0,0],[0,1,0]],[[0,0,0],[1,0,0],[0,1,0]])
        for kwargs in [dict(max_pairs=True),dict(max_pairs=10**1000),dict(max_triangles=0)]:
            with self.assertRaises(RigError):triangle_contacts(mesh,**kwargs)
        mesh.positions[0,0]=np.nan
        with self.assertRaises(RigError):triangle_contacts(mesh)

if __name__=='__main__':unittest.main()
