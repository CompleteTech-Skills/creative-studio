"""Collision geometry admission and real-solver regression tests."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_collisions import CollisionError, validate_shape, validate_colliders
from physics_backend_example import _box


class CollisionProxyTests(unittest.TestCase):
    def cube(self):
        points, faces = _box([1, 1, 1])
        return dict(type='convex-hull', source='geometry', vertices=points, triangles=faces)

    def test_convex_proxy_proves_closed_oriented_volume(self):
        self.assertEqual(validate_shape(self.cube(), dynamic=True, source_ids=['geometry']), (8, 12, 1))
        for change in ('open', 'reverse-face', 'concave', 'unused', 'degenerate', 'flat'):
            shape = self.cube()
            shape['vertices'] = [list(x) for x in shape['vertices']]
            shape['triangles'] = [list(x) for x in shape['triangles']]
            if change == 'open': shape['triangles'].pop()
            if change == 'reverse-face': shape['triangles'][0].reverse()
            if change == 'concave': shape['vertices'][0] = [0, 0, 0]
            if change == 'unused': shape['vertices'].append([0, 0, 0])
            if change == 'degenerate': shape['triangles'][0] = [0, 0, 1]
            if change == 'flat':
                for point in shape['vertices']: point[2] = 0
            with self.subTest(change=change), self.assertRaises(CollisionError):
                validate_shape(shape, dynamic=True, source_ids=['geometry'])

    def test_static_triangle_mesh_may_be_open_but_not_dynamic_or_degenerate(self):
        shape=dict(type='triangle-mesh', source='geometry', vertices=[[0,0,0],[1,0,0],[0,0,1]], triangles=[[0,1,2]])
        self.assertEqual(validate_shape(shape,dynamic=False,source_ids=['geometry']), (3,1,1))
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True,source_ids=['geometry'])
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=False,source_ids=[])

    def test_compound_depth_and_leaf_bounds(self):
        shape=dict(type='sphere',radius=.5)
        for _ in range(9):shape=dict(type='compound',children=[dict(shape=shape)])
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True)
        shape=dict(type='compound',children=[dict(shape=dict(type='sphere',radius=.5)) for _ in range(65)])
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True)

    def test_supported_primitive_dimensions_and_filter_limits(self):
        for shape in [dict(type='capsule',radius=.2,height=.8,axis='Y'),dict(type='cylinder',radius=.2,height=.8,axis='Z')]:
            self.assertEqual(validate_shape(shape,dynamic=True),(0,0,1))
            shape['radius']=float('inf')
            with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True)
