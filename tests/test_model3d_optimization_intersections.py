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
    def test_exactly_collinear_triangle_with_overflowing_edges_is_degenerate(self):
        # Edge differences overflow float64, so the float filter rescales and
        # misses the exact collinearity; a zero normal must not report contact.
        H=2.0**1022;u=2.0**971
        mesh=self.pair([[-H,-3*H,0],[H,3*H,0],[-H+u,-3*H+3*u,0]],[[100,0,-1],[101,0,1],[100,1,1]])
        with self.assertRaises(RigError) as caught:triangle_contacts(mesh)
        self.assertEqual(caught.exception.report['code'],'MODEL3D_INTERSECTION_DEGENERATE')
    def test_noncoplanar_vertex_touching_a_face_is_contact(self):
        mesh=self.pair([[-1,-1,0],[1,-1,0],[0,1,0]],[[0,0,0],[0,0,1],[1,0,1]])
        self.assertEqual(triangle_contacts(mesh)['contacts'],[dict(triangles=[0,1],kind='noncoplanar')])
    def test_coplanar_boundary_and_proper_crossings_and_edge_edge_touch_are_contacts(self):
        # B's lower vertices lie exactly on A's edges (closed vertex-in-triangle).
        boundary=self.pair([[0,0,0],[4,0,0],[2,-2,0]],[[1,-1,0],[3,-1,0],[2,1,0]])
        self.assertEqual(triangle_contacts(boundary)['contacts'][0]['kind'],'coplanar')
        # Hexagram: only proper edge crossings, no vertex inside the other.
        hexagram=self.pair([[0,0,0],[6,0,0],[3,6,0]],[[0,4,0],[6,4,0],[3,-2,0]])
        self.assertEqual(triangle_contacts(hexagram)['contacts'][0]['kind'],'coplanar')
        # Noncoplanar: B's edge meets A's edge exactly at (.5,0,0).
        touching=self.pair([[0,0,0],[1,0,0],[.5,-1,0]],[[.25,0,-1],[.75,0,1],[.5,1,-1]])
        self.assertEqual(triangle_contacts(touching)['contact_count'],1)
    def test_exact_edge_crossing_parameter_keeps_boundary_contacts(self):
        # Each edge/plane crossing point lies exactly on the other triangle's
        # boundary: the first and third pairs touch at one point, the second
        # crosses along a segment ending on edges. A rounded crossing parameter
        # moves those points just outside and loses the contact.
        for a,b in (([[2,2,2],[2,-1,1],[1,-1,1]],[[2,0,3],[0,-2,1],[1,-3,-3]]),
                    ([[-3,-2,1],[-2,2,-3],[0,-1,3]],[[2,3,-1],[-3,0,-1],[2,1,-2]]),
                    ([[1,-1,2],[1,0,1],[3,1,-2]],[[3,-2,-1],[-1,2,0],[0,0,3]])):
            with self.subTest(a=a):
                self.assertEqual(triangle_contacts(self.pair(a,b))['contact_count'],1)
    def test_tiny_scale_coplanar_touch_is_exact(self):
        for scale in (2.0**-1000,5e-324*2**40):
            with self.subTest(scale=scale):
                m=self.pair([[0,0,0],[2,0,0],[0,2,0]],[[2,0,0],[3,0,0],[2,1,0]]);m.positions=m.positions*scale
                self.assertEqual(triangle_contacts(m)['contacts'][0]['kind'],'coplanar')
                m=self.pair([[0,0,0],[2,0,0],[0,2,0]],[[2.5,0,0],[3,0,0],[2.5,1,0]]);m.positions=m.positions*scale
                self.assertEqual(triangle_contacts(m)['contact_count'],0)
    def test_coplanar_touch_found_only_by_zero_orientation_segment_test(self):
        # B touches A's bottom edge at one point; neither first vertex is inside
        # the other triangle, so only a closed segment test reports it.
        mesh=self.pair([[0,0,0],[2,0,0],[1,2,0]],[[1,-2,0],[1,0,0],[2,-1,0]])
        self.assertEqual(triangle_contacts(mesh)['contacts'],[dict(triangles=[0,1],kind='coplanar')])
    def test_tiny_scale_noncoplanar_crossing_and_miss_are_exact(self):
        # Plane distances are cubic in the scale and underflow as floats.
        for b,expected in (([[0,0,-1],[0,0,1],[0,.5,1]],1),([[3,0,-1],[3,0,1],[3,.5,1]],0)):
            with self.subTest(expected=expected):
                m=self.pair([[-1,-1,0],[1,-1,0],[0,1,0]],b);m.positions=m.positions*2.0**-1000
                self.assertEqual(triangle_contacts(m)['contact_count'],expected)
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
    def test_mixed_large_integer_and_float_parts_reject_lossy_promotion(self):
        value=10**16
        integer=Mesh('integer',np.array([[value+1,0,0],[value+2,0,0],[value+1,1,0]],dtype=np.int64),np.array([[0,1,2]]))
        floating=Mesh('floating',np.array([[value,0,0],[value,1,0],[value-2,0,0]],dtype=float),np.array([[0,1,2]]))
        with self.assertRaises(RigError) as error:geometry_contacts(Geometry([integer,floating],[{}]))
        self.assertEqual(error.exception.report['code'],'MODEL3D_INTERSECTION_NUMERIC')
        self.assertEqual(triangle_contacts(integer)['contact_count'],0)
    def test_narrow_indices_are_widened_before_part_offset(self):
        positions=np.zeros((255,3));positions[:3]=[[0,0,0],[3,0,0],[0,3,0]]
        first=Mesh('first',positions,np.array([[0,1,2]],dtype=np.uint8))
        second=Mesh('second',np.array([[.1,.1,0],[.2,.1,0],[.1,.2,0]]),np.array([[0,1,2]],dtype=np.uint8))
        self.assertEqual(geometry_contacts(Geometry([first,second],[{}]))['contact_count'],1)
    def test_exact_work_and_contact_results_charge_memory_budget(self):
        points=np.array([[0.,0,0],[1,0,0],[0,1,0]])
        mesh=Mesh('coincident',np.tile(points,(150,1)),np.arange(450).reshape(-1,3))
        with self.assertRaises(RigError) as error:triangle_contacts(mesh,limits={'memory_bytes':500000})
        self.assertEqual(error.exception.report['code'],'MODEL3D_INTERSECTION_BUDGET')
        small=Mesh('small',np.tile(points,(3,1)),np.arange(9).reshape(-1,3))
        with self.assertRaises(RigError):triangle_contacts(small,limits={'memory_bytes':200000})
        report=triangle_contacts(small,limits={'memory_bytes':300000})
        self.assertEqual(report['contact_count'],3)
        self.assertLessEqual(report['conservative_working_bytes']+report['contact_count']*8192,300000)
    def test_invalid_limits_and_nonfinite_reject(self):
        mesh=self.pair([[0,0,0],[1,0,0],[0,1,0]],[[0,0,0],[1,0,0],[0,1,0]])
        for kwargs in [dict(max_pairs=True),dict(max_pairs=10**1000),dict(max_triangles=0)]:
            with self.assertRaises(RigError):triangle_contacts(mesh,**kwargs)
        mesh.positions[0,0]=np.nan
        with self.assertRaises(RigError):triangle_contacts(mesh)

if __name__=='__main__':unittest.main()
