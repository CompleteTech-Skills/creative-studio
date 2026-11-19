"""Analytic contact oracles against the real spatial owner's native triangles."""
from pathlib import Path
import math
import unittest
import numpy as np
from rig_errors import RigError
from scene3d_contracts import read_document
from scene3d_graph import SceneGraph
from scene3d_spatial import SpatialIndex

ROOT=Path(__file__).resolve().parents[1]

class SphereSweepTests(unittest.TestCase):
    def index(self,vertices=None,triangles=None):
        scene=read_document(ROOT/'examples/scene3d-contracts/example-scene.json')
        for node in scene['nodes']:node['transform']['translation']=[0,0,0]
        graph=SceneGraph(scene,external_references={})
        return SpatialIndex(graph,{'mesh':dict(vertices=vertices or [[0,0,0],[0,0,1],[1,0,0]],
                                               triangles=triangles or [[0,1,2]])})

    def test_face_contact_and_two_sided_normal(self):
        index=self.index()
        for sign in (-1,1):
            hit=index.sweep_sphere([.2,sign,.2],[.2,-sign,.2],.1)
            self.assertAlmostEqual(hit['fraction'],.45)
            np.testing.assert_allclose(hit['normal'],[0,sign,0],atol=1e-12)
            np.testing.assert_allclose(hit['point'],[.2,0,.2])

    def test_edge_and_vertex_contact_before_center_crosses_triangle(self):
        index=self.index()
        edge=index.sweep_sphere([-1,.1,.5],[1,.1,.5],.2)
        self.assertAlmostEqual(edge['fraction'],(1-math.sqrt(.03))/2)
        np.testing.assert_allclose(edge['point'],[0,0,.5],atol=1e-12)
        vertex=index.sweep_sphere([-1,.1,-.1],[1,.1,-.1],.2)
        self.assertAlmostEqual(vertex['fraction'],(1-math.sqrt(.02))/2)
        np.testing.assert_allclose(vertex['point'],[0,0,0],atol=1e-12)

    def test_fast_path_tangent_zero_motion_initial_overlap(self):
        index=self.index()
        self.assertIsNotNone(index.sweep_sphere([.2,1000,.2],[.2,-1000,.2],.001))
        self.assertAlmostEqual(index.sweep_sphere([-1,.2,0],[1,.2,0],.2)['fraction'],.5)
        self.assertEqual(index.sweep_sphere([.2,.05,.2],[.2,.05,.2],.1)['fraction'],0)
        self.assertIsNone(index.sweep_sphere([.2,1,.2],[.2,1,.2],.1))
        self.assertEqual(index.sweep_sphere([.2,.1,.2],[.2,1,.2],.1)['fraction'],0)

    def test_long_sweep_returns_entry_contact_on_vertex_and_edge(self):
        index = self.index()
        radius = .2
        for z, contact_z in ((-.1, 0), (.5, .5)):
            with self.subTest(z=z):
                start, end = [-2000, .1, z], [2000, .1, z]
                entry_x = -math.sqrt(radius**2 - .1**2 - (z - contact_z)**2)
                hit = index.sweep_sphere(start, end, radius)
                self.assertIsNotNone(hit)
                self.assertAlmostEqual(hit['fraction'], (entry_x - start[0]) / 4000, places=13)
                np.testing.assert_allclose(hit['center'], [entry_x, .1, z], atol=1e-10, rtol=0)
                np.testing.assert_allclose(hit['point'], [0, 0, contact_z], atol=1e-10, rtol=0)
                np.testing.assert_allclose(hit['normal'], np.array([entry_x, .1, z - contact_z]) / radius,
                                           atol=1e-10, rtol=0)

        # The path overlaps the triangle's padded AABB but misses its vertex and edges.
        self.assertIsNone(index.sweep_sphere([-2000, .18, -.1], [2000, .18, -.1], radius))

    def test_sweep_hits_thin_triangle_face(self):
        vertices = [[0,0,0], [1,0,0], [1,0,1e-8]]
        radius = 6.25e-10
        for face in ([0,1,2], [1,2,0], [0,2,1]):
            with self.subTest(face=face):
                index = self.index(vertices, [face])
                hit = index.sweep_sphere([.75,1,2.5e-9], [.75,-1,2.5e-9], radius)
                self.assertIsNotNone(hit)
                self.assertAlmostEqual(hit['fraction'], (1-radius)/2, places=15)
                np.testing.assert_allclose(hit['normal'], [0,1,0], rtol=0, atol=1e-12)
                np.testing.assert_allclose(hit['point'], [.75,0,2.5e-9], rtol=0, atol=1e-18)

    def test_sweep_preserves_edge_clearance_under_large_translation(self):
        z = .5009765625
        clearance = (z - .5) / math.sqrt(2)
        for base in (0, 2**39, -(2**39)):
            with self.subTest(base=base):
                index = self.index([[base,0,0], [base,0,1], [base+1,0,0]])
                start, end = [base+.5,1,z], [base+.5,-1,z]
                self.assertIsNone(index.sweep_sphere(start, end, .0001))
                self.assertIsNone(index.sweep_sphere([base+.5,2**39,z], [base+.5,-(2**39),z], .0001))
                radius = .001
                height = math.sqrt(radius**2 - clearance**2)
                hit = index.sweep_sphere(start, end, radius)
                self.assertIsNotNone(hit)
                self.assertAlmostEqual(hit['fraction'], (1-height)/2, places=14)
                np.testing.assert_allclose(hit['normal'],
                                           [(z-.5)/2/radius, height/radius, (z-.5)/2/radius],
                                           rtol=0, atol=1e-10)

    def test_long_oblique_vertex_tangent_and_near_miss(self):
        index = self.index(vertices=[[0,0,0]] * 3)
        # (-4, 3) is exactly perpendicular to the (3, 4) path and has length 5.
        travel = np.array([9000., 12000., 0.])
        tangent = np.array([-4., 3., 0.])
        hit = index.sweep_sphere(tangent - travel, tangent + travel, 5)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit['fraction'], .5, places=14)
        np.testing.assert_allclose(hit['center'], tangent, atol=1e-10, rtol=0)
        self.assertIsNone(index.sweep_sphere(tangent - travel, tangent + travel, math.nextafter(5., 0.)))

    def test_axis_aligned_vertex_tangent_rejects_the_next_smaller_radius(self):
        index = self.index(vertices=[[0, 0, 0], [0, -1, 0], [0, 0, -1]])
        start, end = [-10, 3, 4], [10, 3, 4]
        tangent = index.sweep_sphere(start, end, 5)
        self.assertIsNotNone(tangent)
        self.assertEqual(tangent['fraction'], .5)
        self.assertEqual(tangent['center'], [0, 3, 4])
        self.assertIsNone(index.sweep_sphere(start, end, math.nextafter(5, 0)))

    def test_long_sweep_preserves_small_offsets_on_both_sides_of_contact(self):
        radius = 2**-20
        for x, y, expected in (
                (2**-14 + 2**-30, 2**-14 - 2**-30, True),
                (2**-16, 2**-18, False)):
            distance = (x - y) / math.sqrt(2)
            self.assertEqual(distance < radius, expected)
            index = self.index(vertices=[[x, y, 0], [x+1, y, 0], [x, y-1, 0]])
            for length in (1, 2**39):
                with self.subTest(expected=expected, length=length):
                    hit = index.sweep_sphere([-length, -length, 0], [length, length, 0], radius)
                    self.assertEqual(hit is not None, expected)
                    if hit is not None:
                        # The approach reaches the vertical edge at x before
                        # it reaches the corner's contact circle.
                        entry = x - radius
                        np.testing.assert_allclose(hit['center'], [entry, entry, 0], atol=1e-17, rtol=0)
                        np.testing.assert_allclose(hit['point'], [x, entry, 0], atol=1e-17, rtol=0)
                        self.assertAlmostEqual(math.dist(hit['point'], hit['center']), radius, places=16)

    def test_first_contact_keeps_order_when_public_fractions_round_equal(self):
        from copy import deepcopy
        from fractions import Fraction

        base = [[0, 0, 0], [0, 1, 0], [0, 0, 1]]
        index = self.index(vertices=base)
        added = deepcopy(index.graph.document()['nodes'][1])
        added.update(id='a-later', name='Later triangle')
        index.graph.create(added)
        later = [[2**-20, y, z] for _, y, z in base]
        index = SpatialIndex(index.graph, {
            'mesh': dict(vertices=base, triangles=[[0, 1, 2]]),
            'a-later': dict(vertices=later, triangles=[[0, 1, 2]])})
        length = 2**39
        early = (length - Fraction(1, 8)) / (2 * length)
        late = early + Fraction(1, 2**20) / (2 * length)
        self.assertLess(early, late)
        self.assertEqual(float(early), float(late))
        hit = index.sweep_sphere([-length, .25, .25], [length, .25, .25], .125)
        self.assertEqual(hit['node'], 'mesh')
        self.assertEqual(hit['point'], [0, .25, .25])

    def test_disconnected_closed_sweep_does_not_rescan_unvisited_faces(self):
        from unittest.mock import patch
        vertices, triangles = [], []
        faces = [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]]
        for number in range(6):
            x, start = number * 3, len(vertices)
            vertices.extend([[x, 0, 0], [x+1, 0, 0], [x, 1, 0], [x, 0, 1]])
            triangles.extend([[start+i for i in face] for face in faces])
        index = self.index(vertices=vertices, triangles=triangles)
        with patch('scene3d_sweep.min', wraps=min, create=True) as minimum:
            hit = index.sweep_sphere([-1, 0, 0], [17, 0, 0], .125)
        self.assertIsNotNone(hit)
        self.assertFalse(any(call.args and isinstance(call.args[0], set)
                             for call in minimum.call_args_list))

    def test_bounds_only_and_false_positive_triangle_region_do_not_hit(self):
        index=self.index()
        self.assertIsNone(index.sweep_sphere([.9,1,.9],[.9,-1,.9],.05))
        self.assertIsNone(index.sweep_sphere([.2,1,.2],[.2,-1,.2],.1,exclude=['mesh']))
        self.assertIsNone(SpatialIndex(index.graph).sweep_sphere([.2,1,.2],[.2,-1,.2],.1))

    def test_revision_transform_and_degenerate_edges(self):
        index=self.index();index.graph.set_transform('root',dict(translation=[0,3,0],rotation=[0,0,0,1],scale=[2,2,2]))
        hit=index.sweep_sphere([.2,5,.2],[.2,1,.2],.1)
        self.assertAlmostEqual(hit['fraction'],.475);self.assertEqual(hit['revision'],1)
        edge=self.index([[0,0,0],[1,0,0],[1,0,0]])
        self.assertAlmostEqual(edge.sweep_sphere([.5,1,0],[.5,-1,0],.1)['fraction'],.45)

    def test_closed_solid_initial_containment(self):
        index=self.index([[0,0,0],[1,0,0],[0,1,0],[0,0,1]],
                         [[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
        hit=index.sweep_sphere([.1,.1,.1],[2,2,2],.01)
        self.assertEqual(hit['fraction'],0);self.assertTrue(hit['initial_containment'])

    def test_tiny_radius_does_not_invent_initial_overlap(self):
        index=self.index()
        hit=index.sweep_sphere([.2,5e-13,.2],[.2,-5e-13,.2],1e-14)
        self.assertAlmostEqual(hit['fraction'],.49)
        self.assertIsNone(index.sweep_sphere([.2,2e-14,.2],[.2,3e-14,.2],1e-14))
        self.assertIsNone(index.sweep_sphere([.2,.10000000000001,.2],[.2,1,.2],.1))

    def test_closed_prism_initial_clearance_survives_translation(self):
        vertices = [[0,0,0],[1,0,0],[0,0,1],[0,1,0],[1,1,0],[0,1,1]]
        faces = [[0,1,2],[3,5,4],[0,3,4],[0,4,1],[1,4,5],[1,5,2],[2,5,3],[2,3,0]]
        for base in (0, 2**39, -(2**39)):
            with self.subTest(base=base):
                index = self.index([[base+x,y,z] for x,y,z in vertices], faces)
                start = [base+.5,.5,.5001]
                # The diagonal face is .0001/sqrt(2) from the center.
                self.assertIsNone(index.sweep_sphere(start, start, .00006))
                hit = index.sweep_sphere(start, start, .00008)
                self.assertEqual(hit['fraction'], 0)
                self.assertFalse(hit['initial_containment'])
                np.testing.assert_allclose(hit['normal'], [2**-.5,0,2**-.5], rtol=0, atol=1e-10)

    def test_closed_box_initial_containment_uses_nearest_outward_escape(self):
        vertices=[[x,y,z] for x in (-1,1) for y in (-1,1) for z in (-1,1)]
        faces=[[0,1,3],[0,3,2],[4,6,7],[4,7,5],[0,4,5],[0,5,1],
               [2,3,7],[2,7,6],[0,2,6],[0,6,4],[1,5,7],[1,7,3]]
        for triangles in (faces,[face[::-1] for face in faces]):
            index=self.index(vertices,triangles)
            for start,expected in (([.9,0,0],[1,0,0]),([0,.9,0],[0,1,0]),([1,0,0],[1,0,0])):
                hit=index.sweep_sphere(start,[2,0,0],.01)
                self.assertEqual(hit['fraction'],0)
                np.testing.assert_allclose(hit['normal'],expected,atol=1e-12)
            center=index.sweep_sphere([0,0,0],[2,0,0],.01)
            np.testing.assert_allclose(center['normal'],[-1,0,0],atol=1e-12)
            self.assertEqual(center,index.sweep_sphere([0,0,0],[2,0,0],.01))

    def test_generated_box_boundary_and_nested_shell_domain(self):
        vertices=[[x,y,z] for x in (-.5,.5) for y in (-.5,.5) for z in (-.5,.5)]
        faces=[[0,1,3],[0,3,2],[4,6,7],[4,7,5],[0,4,5],[0,5,1],
               [2,3,7],[2,7,6],[0,2,6],[0,6,4],[1,5,7],[1,7,3]]
        index=self.index(vertices,faces)
        hit=index.sweep_sphere([.5,0,0],[1,0,0],.01)
        np.testing.assert_allclose(hit['normal'],[1,0,0],atol=1e-12)
        for start in ([.5,.1,.1],[.5,.123,.234]):
            boundary=index.sweep_sphere(start,[1,.1,.1],.01)
            np.testing.assert_allclose(boundary['normal'],[1,0,0],atol=1e-12)
        for reversed_inner in (False,True):
            outer=(np.asarray(vertices)*4).tolist();inner=(np.asarray(vertices)*2).tolist()
            inside_faces=[face[::-1] for face in faces] if reversed_inner else faces
            nested=self.index(outer+inner,faces+[[i+8 for i in face] for face in inside_faces])
            with self.assertRaisesRegex(RigError,'nested or interior-overlapping'):
                nested.sweep_sphere([1,0,0],[0,0,0],.01)

    def test_touching_solid_cells_ignore_internal_shared_face(self):
        vertices=np.asarray([[x,y,z] for x in (-.5,.5) for y in (-.5,.5) for z in (-.5,.5)])
        faces=[[0,1,3],[0,3,2],[4,6,7],[4,7,5],[0,4,5],[0,5,1],
               [2,3,7],[2,7,6],[0,2,6],[0,6,4],[1,5,7],[1,7,3]]
        index=self.index(vertices.tolist()+(vertices+[1,0,0]).tolist(),faces+[[i+8 for i in f] for f in faces])
        hit=index.sweep_sphere([.49,0,0],[2,0,0],.01)
        self.assertTrue(hit['initial_containment'])
        self.assertAlmostEqual(np.linalg.norm(np.asarray(hit['point'])-[.49,0,0]),.5)
        self.assertNotAlmostEqual(hit['point'][0],.5)
        boundary=index.sweep_sphere([1.5,0,0],[2,0,0],.01)
        np.testing.assert_allclose(boundary['normal'],[1,0,0],atol=1e-12)

    def test_component_pairs_consume_shared_query_budget_across_nodes(self):
        from copy import deepcopy
        from unittest.mock import patch
        tetra=np.asarray([[0,0,0],[.25,0,0],[0,.25,0],[0,0,.25]])
        vertices=(tetra+[-2,0,0]).tolist()+(tetra+[2,0,0]).tolist()
        faces=[[0,2,1],[0,1,3],[0,3,2],[1,2,3]]
        triangles=faces+[[i+4 for i in f] for f in faces]
        index=self.index(vertices,triangles)
        other=deepcopy(next(n for n in index.graph.document()['nodes'] if n['id']=='mesh'))
        other.update(id='other',name='other');index.graph.create(other)
        mesh=dict(vertices=vertices,triangles=triangles)
        index=SpatialIndex(index.graph,{'mesh':mesh,'other':mesh})
        with patch('scene3d_spatial.MAX_QUERY_TESTS',49):
            with self.assertRaisesRegex(RigError,'budget exceeded'):
                index.sweep_sphere([0,.1,-10],[0,.1,10],.01)
        with patch('scene3d_spatial.MAX_QUERY_TESTS',50):
            self.assertIsNone(index.sweep_sphere([0,.1,-10],[0,.1,10],.01))

    def test_bad_values_fail_explicitly(self):
        index=self.index()
        for radius in (0,-1,True,float('inf')):
            with self.assertRaises(RigError):index.sweep_sphere([0,1,0],[0,-1,0],radius)
        with self.assertRaises(RigError):index.sweep_sphere([float('nan'),0,0],[0,1,0],.1)
        with self.assertRaises(RigError):index.sweep_sphere([1e13,0,0],[0,1,0],.1)

if __name__=='__main__':unittest.main()
