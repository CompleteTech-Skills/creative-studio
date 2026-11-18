"""Analytic real-mesh tests for explicit bounded topology operations."""
import copy
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import numpy as np
from rig_errors import RigError
from rig_models import Mesh
from model3d_topology import (clean_mesh, face_components, fill_planar_hole,
                              orient_mesh, shade_mesh, subdivide_mesh, topology_report)


def tetra():
    return Mesh('tetra', np.array([[0,0,0],[1,0,0],[0,1,0],[0,0,1]], float),
                np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3]], np.int64),
                name='Authored shell', region='body', source_node=7)


def volume(mesh):
    p, t = mesh.positions, mesh.triangles
    return np.sum(p[t[:,0]] * np.cross(p[t[:,1]], p[t[:,2]])) / 6


class TopologyTests(unittest.TestCase):
    def assertCode(self, code, operation):
        with self.assertRaises(RigError) as error:
            operation()
        self.assertEqual(error.exception.report['code'], code)

    def test_closed_tetra_report(self):
        mesh = tetra(); result = topology_report(mesh)
        self.assertEqual(result['boundary_edges'], [])
        self.assertEqual(result['nonmanifold_edges'], [])
        self.assertEqual(result['inconsistent_edges'], [])
        self.assertEqual(result['face_components'], [[0,1,2,3]])
        self.assertEqual(result['duplicate_positions'], 0)
        self.assertEqual(result['degenerate_faces'], [])

    def test_open_boundary_duplicate_degenerate_and_isolated(self):
        mesh = tetra()
        mesh.positions = np.concatenate((mesh.positions, [[0,0,0], [8,9,10]]))
        mesh.triangles = np.array([[0,2,1],[1,2,0],[0,0,1]])
        result = topology_report(mesh)
        self.assertEqual(result['duplicate_positions'], 1)
        self.assertEqual(result['duplicate_faces'], 1)
        self.assertEqual(result['degenerate_faces'], [2])
        self.assertEqual(result['isolated_vertices'], [3,4,5])
        self.assertTrue(result['nonmanifold_edges'])

    def test_components_touching_at_vertex_remain_face_separate(self):
        mesh = Mesh('two', np.array([[0,0,0],[1,0,0],[0,1,0],[-1,0,0],[0,-1,0]], float),
                    np.array([[0,1,2],[0,3,4]]))
        self.assertEqual([x.tolist() for x in face_components(mesh)], [[0],[1]])

    def test_cleanup_requires_explicit_policy_and_keeps_semantics(self):
        mesh = tetra(); mesh.positions = np.concatenate((mesh.positions, [[4,5,6]]))
        mesh.triangles = np.concatenate((mesh.triangles, [[0,1,2],[0,0,1]]))
        baseline = copy.deepcopy(mesh)
        unchanged = clean_mesh(mesh)
        self.assertEqual(len(unchanged.triangles), 6)
        clean = clean_mesh(mesh, remove_degenerate=True, remove_duplicate_faces=True, remove_unused_vertices=True)
        np.testing.assert_array_equal(clean.triangles, tetra().triangles)
        np.testing.assert_array_equal(clean.positions, tetra().positions)
        np.testing.assert_array_equal(mesh.triangles, baseline.triangles)
        self.assertEqual((clean.name, clean.region, clean.source_node), ('Authored shell','body',7))
        clean.positions[0,0] = 44
        self.assertEqual(mesh.positions[0,0], 0)

    def test_cleanup_never_deletes_entire_mesh(self):
        mesh = Mesh('line', np.array([[0,0,0],[1,0,0],[2,0,0]], float), np.array([[0,1,2]]))
        self.assertCode('MODEL3D_TOPOLOGY_EMPTY', lambda: clean_mesh(mesh, remove_degenerate=True))

    def test_exact_weld_preserves_uv_and_morph_seams(self):
        mesh = Mesh('seams', np.array([[0,0,0],[1,0,0],[0,1,0],[0,0,0],[0,0,0]], float),
                    np.array([[0,1,2],[3,1,2],[4,1,2]]),
                    attributes={'TEXCOORD_0':np.array([[0,0],[1,0],[0,1],[1,1],[0,0]],float)},
                    morphs={'smile':{'POSITION':np.zeros((5,3))}}, morph_defaults={'smile':.5})
        result = clean_mesh(mesh, weld_exact_vertices=True)
        self.assertEqual(len(result.positions), 4)
        np.testing.assert_array_equal(result.triangles, [[0,1,2],[3,1,2],[0,1,2]])
        self.assertEqual(result.morph_defaults, {'smile':.5})
        mesh.morphs['smile']['POSITION'][4] = [0,0,.1]
        self.assertEqual(len(clean_mesh(mesh,weld_exact_vertices=True).positions),5)

    def test_orient_repairs_one_flipped_face_and_outward(self):
        mesh = tetra(); mesh.triangles[2] = mesh.triangles[2,::-1]
        self.assertTrue(topology_report(mesh)['inconsistent_edges'])
        result = orient_mesh(mesh, outward=True)
        self.assertEqual(topology_report(result)['inconsistent_edges'], [])
        self.assertAlmostEqual(volume(result), 1/6)
        self.assertTrue(topology_report(mesh)['inconsistent_edges'])
        reversed_mesh = tetra(); reversed_mesh.triangles = reversed_mesh.triangles[:,::-1]
        self.assertGreater(volume(orient_mesh(reversed_mesh,outward=True)),0)

    def test_open_or_nonmanifold_orientation_rejected(self):
        mesh=tetra();mesh.triangles=mesh.triangles[:3]
        self.assertCode('MODEL3D_TOPOLOGY_ORIENTATION', lambda: orient_mesh(mesh,outward=True))
        mesh.triangles=np.concatenate((mesh.triangles,mesh.triangles[:1]))
        self.assertCode('MODEL3D_TOPOLOGY_ORIENTATION', lambda: orient_mesh(mesh))

    def test_reorientation_rejects_directional_frames(self):
        mesh=tetra();mesh.triangles[0]=mesh.triangles[0,::-1]
        mesh.attributes['TANGENT']=np.tile([1.,0,0,1],(4,1))
        self.assertCode('MODEL3D_TOPOLOGY_ATTRIBUTE', lambda: orient_mesh(mesh))

    def test_tiny_geometry_does_not_use_absolute_degeneracy(self):
        mesh=tetra();mesh.positions *= 1e-200
        self.assertEqual(topology_report(mesh)['degenerate_faces'], [])
        oriented=orient_mesh(mesh,outward=True)
        self.assertEqual(topology_report(oriented)['inconsistent_edges'], [])
        self.assertTrue(np.isfinite(shade_mesh(mesh).attributes['NORMAL']).all())

    def test_subdivision_is_actual_four_faces_and_preserves_volume(self):
        mesh=tetra();mesh.attributes['TEXCOORD_0']=np.array([[0,0],[1,0],[0,1],[1,1]],float)
        result=subdivide_mesh(mesh,levels=1.0)
        self.assertEqual(len(result.positions),10)
        self.assertEqual(len(result.triangles),16)
        self.assertAlmostEqual(volume(result),volume(mesh))
        self.assertEqual(topology_report(result)['boundary_edges'],[])
        self.assertEqual(topology_report(result)['inconsistent_edges'],[])
        self.assertEqual(len(result.attributes['TEXCOORD_0']),10)
        self.assertEqual(len(mesh.positions),4)
        self.assertEqual(result.name,mesh.name)

    def test_large_finite_midpoints_do_not_overflow(self):
        mesh=tetra();mesh.positions=(mesh.positions*.1+.5)*1e308
        result=subdivide_mesh(mesh)
        self.assertTrue(np.isfinite(result.positions).all())

    def test_subdivision_exact_boundary_budget_and_negative(self):
        self.assertEqual(len(subdivide_mesh(tetra(),limits={'vertices':10,'triangles':16}).positions),10)
        self.assertCode('MODEL3D_TOPOLOGY_LIMIT',lambda:subdivide_mesh(tetra(),limits={'vertices':9}))
        for bad in (True,1.1,float('inf'),float('nan'),-1,9,10**1000):
            self.assertCode('MODEL3D_TOPOLOGY_LIMIT',lambda:subdivide_mesh(tetra(),levels=bad))
        self.assertCode('MODEL3D_TOPOLOGY_LIMIT',lambda:topology_report(tetra(),limits={'memory_bytes':64}))
        self.assertCode('MODEL3D_TOPOLOGY_LIMIT',lambda:topology_report(tetra(),limits={'vertices':True}))
        self.assertCode('MODEL3D_TOPOLOGY_LIMIT',lambda:topology_report(tetra(),limits={'vertices':10**1000}))

    def test_subdivision_morph_tangent_not_silently_dropped(self):
        mesh=tetra();mesh.morphs={'x':{'POSITION':np.zeros((4,3))}}
        self.assertCode('MODEL3D_TOPOLOGY_ATTRIBUTE',lambda:subdivide_mesh(mesh))
        np.testing.assert_array_equal(subdivide_mesh(mesh,levels=0).morphs['x']['POSITION'],mesh.morphs['x']['POSITION'])

    def test_flat_and_smooth_normals_preserve_split_indices_and_values(self):
        mesh=tetra();mesh.attributes['TEXCOORD_0']=np.array([[0,0],[1,0],[0,1],[1,1]],float)
        flat=shade_mesh(mesh,mode='flat'); smooth=shade_mesh(mesh,mode='smooth')
        self.assertEqual(len(flat.positions),12)
        self.assertEqual(len(smooth.positions),4)
        for tri in flat.triangles:
            np.testing.assert_allclose(flat.attributes['NORMAL'][tri],np.tile(flat.attributes['NORMAL'][tri[0]],(3,1)))
        np.testing.assert_allclose(np.linalg.norm(smooth.attributes['NORMAL'],axis=1),1)
        np.testing.assert_array_equal(flat.attributes['TEXCOORD_0'],mesh.attributes['TEXCOORD_0'][mesh.triangles.reshape(-1)])
        self.assertNotIn('NORMAL',mesh.attributes)

    def test_hole_cap_is_explicit_planar_and_source_immutable(self):
        mesh=tetra();mesh.triangles=mesh.triangles[:3]
        result=fill_planar_hole(mesh,[1.,2.,3.])
        self.assertEqual(topology_report(result)['boundary_edges'],[])
        self.assertEqual(topology_report(result)['inconsistent_edges'],[])
        self.assertAlmostEqual(volume(result),1/6)
        self.assertEqual(len(mesh.triangles),3)
        self.assertEqual(len(result.positions),4)

    def test_hole_cap_preserves_other_open_component(self):
        mesh=tetra();mesh.triangles=mesh.triangles[:3]
        # An unrelated component below the cap plane stays open and untouched.
        mesh.positions=np.concatenate((mesh.positions,[[.1,.1,.1],[.2,.1,.1],[.1,.2,.1]]))
        mesh.triangles=np.concatenate((mesh.triangles,[[4,5,6]]))
        result=fill_planar_hole(mesh,[1,2,3])
        self.assertEqual(len(topology_report(result)['boundary_edges']),3)
        np.testing.assert_array_equal(result.triangles[3],[4,5,6])

    def test_hole_cap_rejects_nonboundary_nonplanar_and_crossing_plane(self):
        mesh=tetra()
        self.assertCode('MODEL3D_TOPOLOGY_HOLE',lambda:fill_planar_hole(mesh,[0,1,2]))
        mesh.triangles=mesh.triangles[:3]
        self.assertCode('MODEL3D_TOPOLOGY_HOLE',lambda:fill_planar_hole(mesh,[1,2,2]))
        mesh.positions=np.concatenate((mesh.positions,[[1,1,1]]))
        self.assertCode('MODEL3D_TOPOLOGY_HOLE',lambda:fill_planar_hole(mesh,[1,2,3]))

    def test_quad_cap_convex_planarity_and_budget_boundaries(self):
        mesh=Mesh('open-box',np.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0],
                                     [0,0,1],[1,0,1],[1,1,1],[0,1,1]],float),
                  np.array([[0,2,1],[0,3,2],[0,1,5],[0,5,4],[1,2,6],[1,6,5],
                            [2,3,7],[2,7,6],[3,0,4],[3,4,7]]))
        capped=fill_planar_hole(mesh,[4,5,6,7],limits={'triangles':12})
        self.assertAlmostEqual(volume(capped),1)
        self.assertEqual(topology_report(capped)['boundary_edges'],[])
        self.assertCode('MODEL3D_TOPOLOGY_LIMIT',lambda:fill_planar_hole(mesh,[4,5,6,7],limits={'triangles':11}))
        self.assertCode('MODEL3D_TOPOLOGY_HOLE',lambda:fill_planar_hole(mesh,[4,5,6,10**1000]))
        mesh.positions[6,2]=1.2
        self.assertCode('MODEL3D_TOPOLOGY_HOLE',lambda:fill_planar_hole(mesh,[4,5,6,7]))
        mesh.positions[6]=[.25,.25,1]
        self.assertCode('MODEL3D_TOPOLOGY_HOLE',lambda:fill_planar_hole(mesh,[4,5,6,7]))

    def test_normals_do_not_fabricate_isolated_or_zero_surface_frames(self):
        mesh=tetra();mesh.positions=np.concatenate((mesh.positions,[[4,5,6]]))
        self.assertCode('MODEL3D_TOPOLOGY_NORMAL',lambda:shade_mesh(mesh))
        mesh=Mesh('zero',np.zeros((3,3)),np.array([[0,1,2]]))
        self.assertCode('MODEL3D_TOPOLOGY_NORMAL',lambda:shade_mesh(mesh))

    def test_multiscale_disconnected_components_are_not_deleted(self):
        mesh=tetra(); tiny=tetra()
        tiny.positions=tiny.positions*1e-8+2
        mesh.positions=np.concatenate((mesh.positions,tiny.positions))
        mesh.triangles=np.concatenate((mesh.triangles,tiny.triangles+4))
        original_positions=mesh.positions.copy(); original_triangles=mesh.triangles.copy()
        self.assertEqual(topology_report(mesh)['degenerate_faces'],[])
        cleaned=clean_mesh(mesh,remove_degenerate=True,remove_unused_vertices=True)
        np.testing.assert_array_equal(cleaned.positions,original_positions)
        np.testing.assert_array_equal(cleaned.triangles,original_triangles)
        self.assertEqual(len(face_components(cleaned)),2)
        oriented=orient_mesh(mesh,outward=True)
        self.assertEqual(topology_report(oriented)['inconsistent_edges'],[])
        normals=shade_mesh(mesh).attributes['NORMAL']
        np.testing.assert_allclose(normals[:4],normals[4:],atol=1e-14)
        np.testing.assert_array_equal(mesh.positions,original_positions)
        np.testing.assert_array_equal(mesh.triangles,original_triangles)

    def test_degeneracy_does_not_delete_skinny_valid_triangle(self):
        mesh=Mesh('skinny',np.array([[0.,0,0],[1,0,0],[1,1e-300,0]]),np.array([[0,1,2]]))
        self.assertEqual(topology_report(mesh)['degenerate_faces'],[])
        self.assertEqual(len(clean_mesh(mesh,remove_degenerate=True).triangles),1)
        np.testing.assert_allclose(shade_mesh(mesh).attributes['NORMAL'],[[0,0,1]]*3)
        integer=Mesh('exact-int',np.array([[2**60,0,0],[2**60+1,0,0],[2**60+2,1,0]],np.int64),np.array([[0,1,2]]))
        self.assertEqual(topology_report(integer)['degenerate_faces'],[])
        integer.positions[2,1]=0
        self.assertEqual(topology_report(integer)['degenerate_faces'],[0])

    def test_many_components_orientation_scans_each_component_locally(self):
        import model3d_topology as topology
        base=tetra(); count=100
        mesh=Mesh('many',np.concatenate([base.positions+[i*2,0,0] for i in range(count)]),
                  np.concatenate([base.triangles+i*4 for i in range(count)]))
        original_edges=topology._edges
        visits=[]
        class CountedEdges(dict):
            def values(self):
                for refs in super().values():
                    visits.append(1)
                    yield refs
        with mock.patch.object(topology,'_edges',side_effect=lambda t:CountedEdges(original_edges(t))):
            with mock.patch.object(topology,'_scaled_positions',wraps=topology._scaled_positions) as scaling:
                result=orient_mesh(mesh,outward=True)
        self.assertEqual(len(result.triangles),count*4)
        self.assertLessEqual(len(visits),count*6*5)
        self.assertEqual(scaling.call_count,count)
        self.assertTrue(all(len(call.args[0])==4 for call in scaling.call_args_list))
        for faces in face_components(result):
            self.assertGreater(volume(Mesh('part',result.positions,result.triangles[faces])),0)

    def test_invalid_native_arrays_fail_structured(self):
        for value in (np.array([[0,1,7]]),np.array([[0,-1,2]]),np.array([[0.,1.,2.]])):
            mesh=tetra();mesh.triangles=value
            with self.assertRaises(RigError):topology_report(mesh)
        mesh=tetra();mesh.positions[0,0]=np.nan
        with self.assertRaises(RigError):topology_report(mesh)
        mesh=tetra();mesh.attributes={'NORMAL':np.full((4,3),'x')}
        self.assertCode('MODEL3D_TOPOLOGY',lambda:topology_report(mesh))
        mesh=tetra();mesh.attributes={'NORMAL':None}
        self.assertCode('MODEL3D_TOPOLOGY',lambda:topology_report(mesh))


if __name__ == '__main__':unittest.main()
