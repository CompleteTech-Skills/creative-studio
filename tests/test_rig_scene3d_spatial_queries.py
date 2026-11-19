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
from scene3d_spatial import SpatialIndex, _QueryBudget, snap_grid, snap_scale
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

    def test_oriented_overlap_uses_rotated_local_box_for_both_nodes(self):
        graph = self.graph()
        node = graph.document()['nodes'][1]
        rotation = [0, math.sin(math.pi/8), 0, math.cos(math.pi/8)]
        graph.set_transform('mesh', dict(translation=[0,0,0], rotation=rotation, scale=[1,1,1]))
        probe = deepcopy(node)
        probe.update(id='probe', name='probe', bounds=dict(minimum=[-.01]*3, maximum=[.01]*3))
        probe['transform']['translation'] = [1,.5,0]
        graph.create(probe)
        # The (1,1,1) corner of this tetrahedron's local box is empty.
        index = SpatialIndex(graph, {'mesh': dict(vertices=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]],
                                                  triangles=[[0,1,2],[0,3,1],[0,2,3],[1,3,2]])})
        self.assertNotIn('probe', index.overlap(index.bounds('mesh')))
        self.assertIn('probe', index.oriented_overlap('mesh'))
        self.assertIn('mesh', index.oriented_overlap('probe'))

    def test_oriented_overlap_keeps_touching_boxes_far_from_origin(self):
        graph = self.graph()
        node = graph.document()['nodes'][1]
        width, offset = 2.0**-23, 2.0**29
        # Conservative: separation is certified only beyond the rounding scale
        # of the coordinates involved (about 1e-5 m at 2**29 m).
        for name, x in (('left', offset), ('right', offset+width), ('apart', offset+width+1e-3)):
            item = deepcopy(node)
            item.update(id=name, name=name, bounds=dict(minimum=[0,0,0], maximum=[width,1,1]))
            item['transform'] = dict(translation=[x,0,0], rotation=[0,0,0,1], scale=[1,1,1])
            graph.create(item)
        index = SpatialIndex(graph)
        self.assertIn('right', index.overlap(index.bounds('left')))
        self.assertIn('right', index.oriented_overlap('left'))
        self.assertIn('left', index.oriented_overlap('right'))
        self.assertNotIn('apart', index.oriented_overlap('left'))

    def test_oriented_overlap_keeps_rotated_far_local_boxes_touching(self):
        graph = self.graph()
        node = graph.document()['nodes'][1]
        angle = math.radians(30)
        rotation = [0, 0, math.sin(angle/2), math.cos(angle/2)]
        width, offset = 1e-3, 2.0**29
        for name, low in (('near', offset), ('far', offset+width)):
            item = deepcopy(node)
            item.update(id=name, name=name, bounds=dict(minimum=[low,0,0], maximum=[low+width,1,1]))
            item['transform'] = dict(translation=[0,0,0], rotation=rotation, scale=[1,1,1])
            graph.create(item)
        index = SpatialIndex(graph)
        self.assertIn('far', index.oriented_overlap('near'))
        self.assertIn('near', index.oriented_overlap('far'))

    def test_triangle_contact_is_exact_for_nearly_parallel_skew_edges(self):
        from scene3d_spatial import _triangle_intersects
        left = np.array([[1678.9275453845798,552.2476837411891,-17.483032618084046],
                         [-1678.9275453845798,-552.2476837411891,17.483032618084046],
                         [-150.01064768274784,613.1908458429399,498.13423978480466]])
        right = np.array([[1678.9275453440628,552.247683846023,-17.483033197574446],
                          [-1678.9275453402383,-552.2476838577292,17.483033195053007],
                          [288.7652557845327,-727.162056553735,-120.0931074335002]])
        self.assertFalse(_triangle_intersects(left, right))
        self.assertFalse(_triangle_intersects(right, left))
        shared = np.array([left[0], left[1], right[2]])
        self.assertTrue(_triangle_intersects(left, shared))

    def test_mesh_overlap_decides_rounding_band_exactly(self):
        graph = self.graph()
        node = graph.document()['nodes'][1]
        for name in ('large', 'small'):
            item = deepcopy(node); item.update(id=name, name=name)
            item['transform'] = dict(translation=[0,0,0], rotation=[0,0,0,1], scale=[1,1,1])
            graph.create(item)
        size = 1e6
        # Plane z = y*1e-6; the small triangle's vertices satisfy y = 0 or 1e-6.
        large = dict(vertices=[[-size,-size,-1.],[size,-size,-1.],[0.,size,1.]], triangles=[[0,1,2]])
        def small(z):
            return dict(vertices=[[0,0,z],[1e-6,0,z],[0,1e-6,z]], triangles=[[0,1,2]])
        for gap in (1e-9, 5e-9):
            with self.subTest(gap=gap):
                index = SpatialIndex(graph, {'large': large, 'small': small(gap)})
                self.assertEqual(index.mesh_overlap('small'), [])
        crossing = dict(vertices=[[0,0,-1e-9],[1e-6,0,-1e-9],[0,1e-6,1e-9]], triangles=[[0,1,2]])
        for name, mesh in (('touching', small(0.)), ('crossing', crossing)):
            with self.subTest(case=name):
                index = SpatialIndex(graph, {'large': large, 'small': mesh})
                self.assertEqual([hit['node'] for hit in index.mesh_overlap('small')], ['large'])

    def test_raycast_distinguishes_large_triangle_edge_and_adjacent_floats(self):
        side = 1e9
        graph = self.graph()
        graph.set_transform('mesh', dict(translation=[0,0,0], rotation=[0,0,0,1], scale=[1,1,1]))
        for face in ([0,1,2], [1,2,0], [0,2,1]):
            with self.subTest(face=face):
                index = SpatialIndex(graph, {'mesh': dict(vertices=[[0,0,0],[0,0,side],[side,0,0]],
                                                          triangles=[face])})
                for z in (math.nextafter(side/2, 0), side/2):
                    hit = index.raycast([side/2, 1, z], [0,-1,0], max_distance=1)
                    self.assertEqual(hit['distance'], 1)
                    self.assertGreaterEqual(min(hit['barycentric']), 0)
                for z in (math.nextafter(side/2, math.inf), side/2+.05):
                    self.assertIsNone(index.raycast([side/2, 0, z], [0,-1,0]))

                # The 7-24-25 ray reaches the vertex at exactly 25 metres.
                hit = index.raycast([-7,24,0], [7,-24,0], max_distance=25)
                self.assertEqual(hit['distance'], 25)
                self.assertEqual(hit['point'], [0,0,0])
                self.assertIsNone(index.raycast([-7,24,0], [7,-24,0], max_distance=math.nextafter(25.,0.)))


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


    def test_mesh_overlap_distinguishes_tiny_gap_at_large_coordinates(self):
        graph = self.graph()
        graph.set_transform('mesh', dict(translation=[0, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1]))
        other = deepcopy(graph.document()['nodes'][1])
        other.update(id='other', name='other')
        graph.create(other)
        large = float(10**12)
        middle = large / 2
        meshes = {'mesh': dict(vertices=[[0, 0, 0], [large, 0, 0], [0, large, 0]], triangles=[[0, 1, 2]])}
        gap = 2 ** -10
        for shift, expected in ((gap, False), (0, True), (-gap, True)):
            with self.subTest(shift=shift):
                y = middle + shift
                meshes['other'] = dict(vertices=[[middle, y, 0], [middle + 1, y, 0], [middle, y + 1, 0]],
                                       triangles=[[0, 1, 2]])
                index = SpatialIndex(graph, meshes)
                self.assertIn('other', index.overlap(index.bounds('mesh')))
                for identifier in ('mesh', 'other'):
                    self.assertEqual(bool(index.mesh_overlap(identifier)), expected)

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

    def test_nearest_preserves_thin_triangle_interior(self):
        graph = self.graph()
        graph.set_transform('mesh', dict(translation=[0,0,0], rotation=[0,0,0,1], scale=[1,1,1]))
        vertices = [[0,0,0], [1,0,0], [1,0,1e-8]]
        point = [.75, 0, 2.5e-9]
        for face in ([0,1,2], [1,2,0], [0,2,1]):
            with self.subTest(face=face):
                index = SpatialIndex(graph, {'mesh': dict(vertices=vertices, triangles=[face])})
                hit = index.nearest(point, max_distance=0)
                self.assertIsNotNone(hit)
                self.assertEqual(hit['point'], point)
                self.assertEqual(hit['distance'], 0)
                above = index.nearest([point[0], .05, point[2]])
                self.assertAlmostEqual(above['distance'], .05, places=15)
                np.testing.assert_allclose(above['point'], point, rtol=0, atol=1e-20)

    def test_grid_snapping_preserves_full_precision_at_ties_and_existing_grid_points(self):
        below = math.nextafter(.5, 0)
        above = math.nextafter(.5, 1)
        large = float(2**52 + 1)
        self.assertEqual(snap_grid([below, .5, above], 1), [0, 1, 1])
        self.assertEqual(snap_grid([-below, -.5, -above], 1), [0, -1, -1])
        self.assertEqual(snap_grid([large, -large, 0], 1), [large, -large, 0])
        self.assertEqual(snap_grid([math.nextafter(1.5, 1), 1.5, 1.5], 1, origin=[1, 1, 2]), [1, 2, 1])

    def test_grid_and_scale_snapping_refuse_unrepresentable_result(self):
        for snap in (snap_grid, snap_scale):
            with self.subTest(snap=snap.__name__):
                with self.assertRaisesRegex(RigError, 'overflow'):
                    snap([1.7e308] * 3, 1e308)
                self.assertEqual(snap([1.4e308] * 3, 1e308), [1e308] * 3)


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

    def test_oriented_overlap_reads_one_scene_evaluation_per_query(self):
        from unittest.mock import patch

        scene = self.graph().document()
        source = scene['nodes'][1]
        source['bounds'] = dict(minimum=[0,0,0], maximum=[1,1,1])
        candidates = [f'candidate-{i}' for i in range(8)]
        for name in reversed(candidates):
            other = deepcopy(source)
            other.update(id=name, name=name)
            scene['nodes'].append(other)
        graph = SceneGraph(scene, external_references={})
        index = SpatialIndex(graph)
        with patch.object(graph, 'evaluate_base', wraps=graph.evaluate_base) as evaluate:
            self.assertEqual(index.oriented_overlap('mesh', exclude=[candidates[-1]]), candidates[:-1])
            evaluate.assert_called_once_with()
            evaluate.reset_mock()
            graph.isolate([candidates[0]])
            self.assertEqual(index.oriented_overlap('mesh', visibility='editor'), [candidates[0]])
            evaluate.assert_called_once_with()

        moved = deepcopy(source['transform'])
        moved['translation'][0] += 10
        graph.set_transform(candidates[0], moved)
        self.assertEqual(index.oriented_overlap('mesh', visibility='render'), candidates[1:])

if __name__ == '__main__': unittest.main()
