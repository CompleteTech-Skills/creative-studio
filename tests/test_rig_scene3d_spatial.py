"""Numeric and authored-scene spatial integration checks."""
from copy import deepcopy
from pathlib import Path
import math
import unittest

import numpy as np
from rig_errors import RigError
from scene3d_contracts import read_document
from scene3d_graph import SceneGraph
from scene3d_spatial import SpatialIndex, snap_grid, snap_scale
from scene3d_constraints import (attach, detach, place, sample_path, set_constraint,
                                 snap_rotation, snap_surface, snap_transform, snap_bounds, follow_path)

ROOT = Path(__file__).resolve().parents[1]

class SpatialTests(unittest.TestCase):
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
        for origin,direction,limit in [([0,0,0],[0,0,0],1),([0,float('inf'),0],[1,0,0],1),([0,0,0],[1,0,0],-1)]:
            with self.assertRaises(RigError): index.raycast(origin,direction,max_distance=limit)
        with self.assertRaises(RigError): index.overlap(dict(minimum=[1,1,1],maximum=[0,0,0]))
        with self.assertRaises(RigError): index.frustum([[0,0,0,0]]*6)

    def test_snap_explicit_space_and_no_invalid_mutation(self):
        graph = self.graph()
        graph.set_transform('root',dict(translation=[.2,0,0],rotation=[0,0,0,1],scale=[1,1,1]))
        snap_transform(graph,'mesh',space='world',grid=1)
        np.testing.assert_allclose(np.asarray(graph.transform('mesh',space='world'))[:3,3],[2,0,0])
        self.assertEqual(snap_grid([-.5,.5,1.5],1),[-1,1,2])
        self.assertEqual(snap_scale([.9,1.1,2.1],.5),[1,1,2])
        np.testing.assert_allclose(snap_rotation([0,math.sin(.3),0,math.cos(.3)],math.pi/2),[0,0,0,1])
        q = np.array([0,math.sin(.3),0,math.cos(.3)])
        np.testing.assert_allclose(snap_rotation(q,1),snap_rotation(-q,1))
        before = graph.document()
        with self.assertRaises(RigError): snap_transform(graph,'mesh',space='local',scale_step=100)
        self.assertEqual(before,graph.document())

    def test_named_socket_attach_detach_preserves_world_pose(self):
        graph = self.graph()
        socket = deepcopy(graph.document()['nodes'][0])
        socket.update(id='hand',name='hand',type='socket',parent='root')
        socket['transform']['translation'] = [0,2,0]
        graph.create(socket)
        before = graph.transform('mesh',space='world')
        attach(graph,'mesh','root','hand')
        np.testing.assert_allclose(graph.transform('mesh',space='world'),before)
        graph.set_transform('hand',dict(translation=[0,3,0],rotation=[0,0,0,1],scale=[1,1,1]))
        moved = graph.transform('mesh',space='world')
        self.assertEqual(moved[1][3],1)
        detach(graph,'mesh')
        np.testing.assert_allclose(graph.transform('mesh',space='world'),moved)
        with self.assertRaises(RigError): attach(graph,'mesh','root','camera')

    def test_constraint_uses_existing_graph_cycle_and_lock_checks(self):
        graph = self.graph()
        set_constraint(graph,'mesh',dict(id='follow',type='copy-transform',target='camera',influence=1))
        np.testing.assert_allclose(graph.transform('mesh',space='world'),graph.transform('camera',space='world'))
        before = graph.document()
        with self.assertRaises(RigError): set_constraint(graph,'camera',dict(id='cycle',type='copy-transform',target='mesh',influence=1))
        self.assertEqual(graph.document(),before)

    def test_bounded_placement_collision_and_atomic_failure(self):
        graph = self.graph()
        index = self.index(graph)
        other = deepcopy(graph.document()['nodes'][1])
        other.update(id='obstacle',name='obstacle')
        graph.create(other)
        before = graph.document()
        with self.assertRaises(RigError): place(graph,index,'mesh',[[2,0,0]])
        self.assertEqual(before,graph.document())
        receipt = place(graph,index,'mesh',[[2,0,0],[10,0,0]])
        self.assertEqual(receipt['position'],[10,0,0])

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

    def test_real_saved_room_geometry_door_opening_and_ground(self):
        import tempfile
        from scene3d_environment import create_environment, load_meshes
        from scene3d_contracts import load_project
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'rooms'
            create_environment(root,dict(id='rooms',version=1,seed=1,generator='rooms',parameters=dict(rooms=2),budget=30))
            bundle = load_project(root)
            path = bundle['project']['scenes'][-1]['path']
            scene = bundle['documents'][path]
            graph = SceneGraph.from_bundle(bundle,path)
            index = SpatialIndex(graph,load_meshes(root,path))
            through_door = index.raycast([0,1,0],[1,0,0])
            wall = index.raycast([0,2.6,0],[1,0,0])
            self.assertEqual(through_door['node'],'rooms-partition-2')
            self.assertEqual(wall['node'],'rooms-partition-1')
            self.assertAlmostEqual(through_door['distance'],7.4)
            self.assertAlmostEqual(wall['distance'],2.4)
            floor = index.ground([0,2.8,0])
            self.assertEqual(floor['node'],'rooms-floor-0')
            self.assertAlmostEqual(floor['point'][1],0)
            self.assertEqual(graph.document(),scene)

    def test_placement_rejects_driven_descendant_without_mutation(self):
        graph = self.graph()
        set_constraint(graph,'mesh',dict(id='follow',type='copy-transform',target='camera',influence=1))
        index = self.index(graph)
        before = graph.document()
        with self.assertRaisesRegex(RigError,'driven descendants'):
            place(graph,index,'root',[[10,0,0]])
        self.assertEqual(graph.document(),before)

    def test_bounds_snap_and_explicit_follow_preserve_orientation(self):
        graph = self.graph()
        other = deepcopy(graph.document()['nodes'][1])
        other.update(id='other',name='other')
        other['transform']['translation'] = [5,0,0]
        graph.create(other)
        index = self.index(graph)
        snap_bounds(graph,index,'mesh','other',axis=0,gap=.5)
        self.assertAlmostEqual(index.bounds('mesh')['minimum'][0],6.5)
        follow_path(graph,'camera',[[0,0,0],[4,0,0]],.25)
        np.testing.assert_allclose(np.asarray(graph.transform('camera',space='world'))[:3,3],[1,0,0])
        before = graph.document()
        with self.assertRaises(RigError): follow_path(graph,'camera',[[0,0,0],[1,0,0]],.5,expected_revision=-1)
        self.assertEqual(before,graph.document())

    def test_surface_snap_and_arc_length_path(self):
        graph = self.graph()
        index = self.index(graph)
        hit = snap_surface(graph,index,'camera',[2.2,4,.2],offset=.5)
        self.assertEqual(hit['node'],'mesh')
        np.testing.assert_allclose(np.asarray(graph.transform('camera',space='world'))[:3,3],[2.2,.5,.2])
        self.assertEqual(sample_path([[0,0,0],[1,0,0],[1,0,3]],.5),[1,0,1])
        with self.assertRaises(RigError): sample_path([[0,0,0],[0,0,0]],.5)

if __name__ == '__main__': unittest.main()
