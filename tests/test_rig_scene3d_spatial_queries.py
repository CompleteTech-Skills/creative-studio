"""Canonical spatial query regressions ported from source8238c3e.

Only query methods are selected; the unreleased placement/environment/temporal
implementation is deliberately outside this narrow dependency transport.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import math
import unittest
import numpy as np
from rig_errors import RigError
from scene3d_contracts import read_document
from scene3d_graph import SceneGraph
from scene3d_spatial import SpatialIndex, _QueryBudget
ROOT = Path(__file__).resolve().parents[1]

def box(width=1, height=1, depth=1):
    # Analytic test fixture, same vertices/faces as canonical source box fixture.
    w,h,d = width/2, height/2, depth/2
    vertices = [[-w,-h,-d],[w,-h,-d],[w,h,-d],[-w,h,-d],[-w,-h,d],[w,-h,d],[w,h,d],[-w,h,d]]
    quads = [(0,3,2,1),(4,5,6,7),(0,4,7,3),(1,2,6,5),(0,1,5,4),(3,7,6,2)]
    return SimpleNamespace(vertices=vertices, triangles=[[q[0],q[1],q[2]] for q in quads]+[[q[0],q[2],q[3]] for q in quads])

class SpatialQueryTests(unittest.TestCase):
    def graph(self):
        scene = read_document(ROOT/'examples/scene3d-contracts/example-scene.json')
        scene['nodes'][1]['bounds'] = dict(minimum=[0,0,0],maximum=[1,0,1])
        return SceneGraph(scene,external_references={})


    def index(self,graph):
        # Upward-facing triangle: local plane y=0, x+z<=1.
        return SpatialIndex(graph,{'mesh':dict(vertices=[[0,0,0],[0,0,1],[1,0,0]],triangles=[[0,1,2]])})


    def test_raycast_actual_triangle_rejects_box_only_region(self):
        index = self.index(self.graph())
        self.assertIsNone(index.raycast([2.9,1,.9],[0,-1,0]))
        hit = index.raycast([2.2,2,.2],[0,-9,0])
        self.assertAlmostEqual(hit['distance'],2)
        np.testing.assert_allclose(hit['point'],[2.2,0,.2])
        np.testing.assert_allclose(hit['normal'],[0,1,0])
        self.assertEqual(hit['triangle'],0)
        self.assertEqual(index.raycast([2.2,2,.2],[0,-1e308,0]),hit)


    def test_ray_tiny_triangle_preserves_interior(self):
        index = SpatialIndex(self.graph(),{'mesh':dict(vertices=[[0,0,0],[0,0,1e-6],[1e-6,0,0]],triangles=[[0,1,2]])})
        hit = index.raycast([2.00000025,1,2.5e-7],[0,-1,0])
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit['distance'],1)
        np.testing.assert_allclose(hit['normal'],[0,1,0])


    def test_shallow_nonzero_ray_and_small_oriented_bounds(self):
        graph = self.graph()
        mesh = dict(vertices=[[1,0,0],[1,1,0],[1,0,2e12]],triangles=[[0,1,2]])
        hit = SpatialIndex(graph,{'mesh':mesh}).raycast([2,.25,0],[1e-12,0,1],max_distance=1.1e12)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit['distance']/1e12,1)
        scene = graph.document()
        node = scene['nodes'][1]
        node['bounds'] = dict(minimum=[-2e-6,-.1e-6,-.1e-6],maximum=[2e-6,.1e-6,.1e-6])
        node['transform'] = dict(translation=[0,0,0],rotation=[0,math.sin(math.pi/8),0,math.cos(math.pi/8)],scale=[1,1,1])
        other = deepcopy(node);other.update(id='other',name='other')
        other['transform']['translation'] = [.5e-6,0,.5e-6]
        scene['nodes'].append(other)
        index = SpatialIndex(SceneGraph(scene,external_references={}))
        self.assertIn('other',index.overlap(index.bounds('mesh')))
        self.assertNotIn('other',index.oriented_overlap('mesh'))


    def test_bounds_only_not_misrepresented_as_triangle(self):
        index = SpatialIndex(self.graph())
        self.assertEqual(index.overlap(dict(minimum=[2,0,0],maximum=[3,1,1])),['mesh'])
        self.assertIsNone(index.raycast([2.1,1,.1],[0,-1,0]))
        self.assertIsNone(index.nearest([2.1,1,.1]))


    def test_transformed_mesh_and_index_refresh(self):
        graph = self.graph()
        index = self.index(graph)
        graph.set_transform('root',dict(translation=[0,3,0],rotation=[0,0,0,1],scale=[2,2,2]))
        hit = index.ground([4.2,8,.2])
        np.testing.assert_allclose(hit['point'],[4.2,3,.2])
        self.assertEqual(hit['revision'],1)
        graph.delete('mesh')
        self.assertIsNone(index.ground([4.2,8,.2]))


    def test_exact_mesh_overlap_rejects_aabb_false_positive_and_hits_crossing(self):
        graph = self.graph()
        other = deepcopy(graph.document()['nodes'][1]);other.update(id='other',name='other')
        graph.create(other)
        meshes = {
            'mesh':dict(vertices=[[0,0,0],[0,0,1],[1,0,0]],triangles=[[0,1,2]]),
            'other':dict(vertices=[[.8,0,.8],[.8,0,1],[1,0,.8]],triangles=[[0,1,2]])}
        index = SpatialIndex(graph,meshes)
        self.assertIn('other',index.overlap(index.bounds('mesh')))
        self.assertEqual(index.mesh_overlap('mesh'),[])
        meshes['other']['vertices'] = [[.1,0,.1],[.1,0,.8],[.8,0,.1]]
        hit = SpatialIndex(graph,meshes).mesh_overlap('mesh')
        self.assertEqual(hit[0]['relation'],'surface')
        self.assertEqual(hit[0]['node'],'other')
        meshes['other']['vertices'] = [[.25,-1,.25],[.25,1,.25],[.25,0,.8]]
        self.assertEqual(SpatialIndex(graph,meshes).mesh_overlap('mesh')[0]['relation'],'surface')


    def test_exact_closed_mesh_overlap_detects_containment_and_touch(self):
        graph = self.graph()
        graph.set_transform('mesh',dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1]))
        other = deepcopy(graph.document()['nodes'][1]);other.update(id='other',name='other')
        graph.create(other)
        outer,inner = box(4,4,4),box(1,1,1)
        meshes = {'mesh':dict(vertices=outer.vertices,triangles=outer.triangles),
                  'other':dict(vertices=inner.vertices,triangles=inner.triangles)}
        index = SpatialIndex(graph,meshes)
        self.assertEqual(index.mesh_overlap('mesh'),[])
        self.assertEqual(index.mesh_overlap('mesh',solid=True)[0]['relation'],'containment')
        self.assertEqual(index.mesh_overlap('other',solid=True)[0]['relation'],'containment')
        graph.set_transform('other',dict(translation=[2.5,0,0],rotation=[0,0,0,1],scale=[1,1,1]))
        self.assertEqual(index.mesh_overlap('mesh',solid=True)[0]['relation'],'surface')
        graph.set_transform('other',dict(translation=[3,0,0],rotation=[0,0,0,1],scale=[1,1,1]))
        self.assertEqual(index.mesh_overlap('mesh',solid=True),[])


    def test_exact_overlap_rejects_open_solid_and_pair_budget(self):
        from unittest.mock import patch
        graph = self.graph()
        other = deepcopy(graph.document()['nodes'][1]);other.update(id='other',name='other')
        graph.create(other)
        mesh = dict(vertices=[[0,0,0],[0,0,1],[1,0,0]],triangles=[[0,1,2]])
        index = SpatialIndex(graph,{'mesh':mesh,'other':mesh})
        with self.assertRaisesRegex(RigError,'closed'):
            index.mesh_overlap('mesh',solid=True)
        with patch('scene3d_spatial.MAX_QUERY_TESTS',0):
            with self.assertRaisesRegex(RigError,'budget'):
                index.mesh_overlap('mesh')


    def test_nearest_tiny_triangle_preserves_interior(self):
        graph = self.graph()
        index = SpatialIndex(graph,{'mesh':dict(vertices=[[0,0,0],[0,0,.001],[.001,0,0]],triangles=[[0,1,2]])})
        hit = index.nearest([2.00025,.001,.00025])
        self.assertAlmostEqual(hit['distance'],.001,places=12)
        np.testing.assert_allclose(hit['point'],[2.00025,0,.00025],atol=1e-14)


    def test_nearest_measures_triangle_not_origin(self):
        index = self.index(self.graph())
        hit = index.nearest([2.25,3,.25])
        self.assertAlmostEqual(hit['distance'],3)
        np.testing.assert_allclose(hit['point'],[2.25,0,.25])
        self.assertIsNone(index.nearest([2.25,3,.25],max_distance=2.9))


    def test_visibility_and_exclusion_apply_without_revision(self):
        graph = self.graph()
        index = self.index(graph)
        self.assertIsNone(index.ground([2.1,2,.1],exclude=['mesh']))
        graph.isolate(['camera'])
        self.assertIsNone(index.ground([2.1,2,.1],visibility='editor'))
        self.assertIsNotNone(index.ground([2.1,2,.1],visibility='render'))


    def test_frustum_inward_planes_and_transformed_bounds(self):
        index = self.index(self.graph())
        planes = [[1,0,0,-1],[0,1,0,1],[0,0,1,1],[-1,0,0,4],[0,-1,0,1],[0,0,-1,2]]
        self.assertEqual(index.frustum(planes),['mesh'])
        planes[0][3] = -5
        self.assertEqual(index.frustum(planes),[])
        self.assertEqual(index.obb('mesh')['center'],[2.5,0,.5])


    def test_ground_backface_and_slope_policy(self):
        graph = self.graph()
        index = SpatialIndex(graph,{'mesh':dict(vertices=[[0,0,0],[1,0,0],[0,0,1]],triangles=[[0,1,2]])})
        self.assertIsNotNone(index.raycast([2.1,1,.1],[0,-1,0]))
        self.assertIsNone(index.ground([2.1,1,.1]))


    def test_invalid_geometry_and_query_boundaries(self):
        graph = self.graph()
        for mesh in [dict(vertices=[[0,0,0]],triangles=[[0,0,2]]),
                     dict(vertices=[[0,0,float('nan')]],triangles=[[0,0,0]]),
                     dict(vertices=[[0,0,0]],triangles=[[0.,0.,0.]])]:
            with self.assertRaises(RigError): SpatialIndex(graph,{'mesh':mesh})
        index = self.index(graph)
        for origin,direction,limit in [([0,0,0],[0,0,0],1),([0,float('inf'),0],[1,0,0],1),([0,0,0],[1,0,0],-1),([0,0,0],[1,0,0],10**1000)]:
            with self.assertRaises(RigError): index.raycast(origin,direction,max_distance=limit)
        with self.assertRaises(RigError): index.overlap(dict(minimum=[1,1,1],maximum=[0,0,0]))
        for invalid in ([[0,0,0,0]]*6, np.array(0), [['bad',0,0,0]]*6):
            with self.assertRaises(RigError): index.frustum(invalid)
        with self.assertRaises(RigError):
            SpatialIndex(graph,{'mesh':dict(vertices=np.array(0),triangles=[[0,0,0]])})


    def test_degenerate_triangles_and_oriented_box_separation(self):
        graph = self.graph()
        index = SpatialIndex(graph,{'mesh':dict(vertices=[[0,0,0],[0,0,0],[1,0,0]],triangles=[[0,1,2]])})
        hit = index.nearest([2.5,2,0])
        self.assertAlmostEqual(hit['distance'],2)
        np.testing.assert_allclose(hit['point'],[2.5,0,0])
        self.assertIsNone(index.raycast([2.5,2,0],[0,-1,0]))
        # Two long, parallel narrow boxes whose world AABBs overlap.
        node = graph.document()['nodes'][1]
        node['bounds'] = dict(minimum=[-2,-.1,-.1],maximum=[2,.1,.1])
        node['transform'] = dict(translation=[0,0,0],rotation=[0,math.sin(math.pi/8),0,math.cos(math.pi/8)],scale=[1,1,1])
        scene = graph.document();scene['nodes'][1] = node
        other = deepcopy(node);other.update(id='other',name='other')
        other['transform']['translation'] = [.5,0,.5]
        scene['nodes'].append(other)
        boxes = SpatialIndex(SceneGraph(scene,external_references={}))
        self.assertIn('other',boxes.overlap(boxes.bounds('mesh')))
        self.assertNotIn('other',boxes.oriented_overlap('mesh'))


    def test_shared_budget_applies_across_multiple_pair_queries(self):
        from unittest.mock import patch
        graph = self.graph()
        other = deepcopy(graph.document()['nodes'][1]); other.update(id='other', name='other')
        graph.create(other)
        triangle = dict(vertices=[[0,0,0],[0,0,1],[1,0,0]], triangles=[[0,1,2]])
        index = SpatialIndex(graph, {'mesh':triangle, 'other':triangle})
        with patch('scene3d_spatial.MAX_QUERY_TESTS', 1):
            budget = _QueryBudget()
            self.assertTrue(index.mesh_overlap('mesh', _budget=budget))
            with self.assertRaisesRegex(RigError, 'budget'):
                index.mesh_overlap('mesh', _budget=budget)

if __name__ == '__main__': unittest.main()
