"""Independent public sweep regressions for review 7007150381.

Frozen against 0b11426b0616e8b7908b68f76a4e3d111325d166 before repair.
The oracle minimizes exact rational distances to a triangle; it neither calls
private collision helpers nor duplicates their floating-point root solver.
Synthetic OBJ bytes are persisted, reopened and hash-checked for every query.
This is offline geometry evidence, not renderer or production acceptance.
"""
from fractions import Fraction as Q
import hashlib
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np
from rig_errors import RigError
from rig_obj import import_obj
from scene3d_contracts import read_document
from scene3d_graph import SceneGraph
from scene3d_spatial import SpatialIndex

ROOT = Path(__file__).resolve().parents[1]
FACE = [[3.0934559956558374, -1.856539837238375, 4.357039113398898],
        [-0.44070639153571584, 6.846918612131243, -4.114059235756133],
        [-5.746205599775959, -3.133838937654493, -4.600018991041663]]
FACE_START = [-0.6935742917304689, 0.33770269449647133, 0.6363266393766661]
FACE_END = [1.3871485834609372, -0.6754053889929426, -1.272653278753332]
EDGE = [[-1.1784965089626311, -3.027194203551437, 2.3339325680806353],
        [1.1784965089626311, 3.027194203551437, -2.3339325680806353],
        [3.8196215296053686, -0.8386331884926393, 0.840943366543675]]
EDGE_START = [-0.9549053824013426, 0.20965829712315992, -0.21023584163591882]
EDGE_END = [1.9098107648026836, -0.41931659424631956, 0.4204716832718375]
LARGE = [[67782580.89539865, -35695720.70180144, -5063378.807393603],
         [-99856530.37003899, -19084109.454558585, -38119168.24677775],
         [-35708629.30314384, 90475551.52552626, 48245925.68755242]]
LARGE_START = [232845.53794648356, 569084.725946681, -894866.550512219]
LARGE_END = [-134681.4978130939, -329169.27749305626, 517607.939620718]


def sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def at(start, end, t):
    return tuple(Q(x) + t*(Q(y)-Q(x)) for x, y in zip(start, end))


def exact_distance_squared(point, triangle):
    """Minimum over three closed segments and the interior plane projection.

    All inputs are the exact values of their binary floats. The 2x2 Gram
    system supplies barycentric coordinates without normalizing a normal.
    """
    p = tuple(map(Q, point))
    a, b, c = [tuple(map(Q, vertex)) for vertex in triangle]
    candidates = []
    for left, right in ((a, b), (b, c), (c, a)):
        direction, offset = sub(right, left), sub(p, left)
        length2 = dot(direction, direction)
        t = max(Q(0), min(Q(1), dot(offset, direction)/length2)) if length2 else Q(0)
        residual = sub(offset, tuple(t*x for x in direction))
        candidates.append(dot(residual, residual))
    u, v, w = sub(b, a), sub(c, a), sub(p, a)
    uu, uv, vv = dot(u, u), dot(u, v), dot(v, v)
    determinant = uu*vv-uv*uv
    if determinant:
        s = (dot(w, u)*vv-dot(w, v)*uv)/determinant
        t = (dot(w, v)*uu-dot(w, u)*uv)/determinant
        if s >= 0 and t >= 0 and s+t <= 1:
            residual = tuple(wi-s*ui-t*vi for wi, ui, vi in zip(w, u, v))
            candidates.append(dot(residual, residual))
    return min(candidates)


def entry_bracket(triangle, start, end, radius, inside=Q(1, 4)):
    """Bracket first contact using convexity of distance to a convex triangle.

    Start is strictly outside and the upper endpoint is strictly inside.
    Hence the sublevel set is an interval, and bisection finds its left end.
    180 steps resolve the tiny positive entries without a quadratic formula.
    """
    r2 = Q(radius)**2
    assert exact_distance_squared(start, triangle) > r2
    assert exact_distance_squared(at(start, end, inside), triangle) < r2
    low, high = Q(0), inside
    for _ in range(180):
        middle = (low+high)/2
        if exact_distance_squared(at(start, end, middle), triangle) <= r2:
            high = middle
        else:
            low = middle
    return low, high


class ContactPrecisionTests(unittest.TestCase):
    def index_from_obj(self, vertices, faces=None):
        faces = faces or [[0, 1, 2]]
        temporary = tempfile.TemporaryDirectory(prefix='scene3d-contact-')
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)/'synthetic.obj'
        payload = ('# Synthetic precision fixture; no external assets\n' +
                   ''.join('v '+' '.join(repr(float(x)) for x in row)+'\n' for row in vertices) +
                   ''.join('f '+' '.join(str(x+1) for x in row)+'\n' for row in faces)).encode('ascii')
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        # Cleanup assertions run even when a geometry assertion fails.
        self.addCleanup(lambda: self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest))
        imported = import_obj(path).geometry.meshes[0]
        # Ensure persistence did not quantize the reproduced coordinates.
        actual_triangles = np.asarray(imported.positions)[np.asarray(imported.triangles)]
        np.testing.assert_array_equal(actual_triangles, np.asarray(vertices)[np.asarray(faces)])
        scene = read_document(ROOT/'examples/scene3d-contracts/example-scene.json')
        for node in scene['nodes']:
            node['transform']['translation'] = [0, 0, 0]
        return SpatialIndex(SceneGraph(scene, external_references={}),
                            {'mesh': {'vertices': imported.positions, 'triangles': imported.triangles}})

    def assert_valid_hit(self, hit, start, end, radius):
        self.assertIsNotNone(hit)
        self.assertTrue(math.isfinite(hit['fraction']))
        self.assertGreaterEqual(hit['fraction'], 0)
        self.assertLessEqual(hit['fraction'], 1)
        for key in ('center', 'point', 'normal'):
            self.assertEqual(len(hit[key]), 3)
            self.assertTrue(all(math.isfinite(x) for x in hit[key]))
        self.assertEqual(hit['triangle'], 0)
        np.testing.assert_allclose(hit['center'], np.asarray(start)+hit['fraction']*(np.asarray(end)-start),
                                   rtol=2e-14, atol=1e-14)
        self.assertAlmostEqual(math.dist(hit['center'], hit['point'])/radius, 1, places=7)
        self.assertAlmostEqual(math.hypot(*hit['normal']), 1, places=7)

    def assert_first_entry(self, triangle, start, end, radius, inside=Q(1, 4)):
        low, high = entry_bracket(triangle, start, end, radius, inside)
        self.assertGreater(low, 0)
        self.assertLess(high-low, Q(1, 10**50))
        hit = self.index_from_obj(triangle).sweep_sphere(start, end, radius)
        self.assertIsNotNone(hit)
        # Absolute decimal-place comparisons would accept zero for ~1e-16.
        # Relative error here must be small compared with the exact bracket.
        expected = float((low+high)/2)
        self.assertGreater(hit['fraction'], 0)
        self.assertLessEqual(abs(hit['fraction']/expected-1), 1e-6,
                             f'first entry bracket [{float(low)!r}, {float(high)!r}], hit={hit!r}')
        self.assert_valid_hit(hit, start, end, radius)
        return low, high

    def test_face_entry_keeps_positive_ulp_clearance(self):
        low, high = self.assert_first_entry(FACE, FACE_START, FACE_END, 1.)
        self.assertLess(high, Q(1, 10**14))

    def test_edge_entry_keeps_positive_ulp_clearance(self):
        low, high = self.assert_first_entry(EDGE, EDGE_START, EDGE_END, 1.)
        self.assertLess(high, Q(1, 10**14))

    def test_large_face_midpoint_witness_requires_first_contact(self):
        radius = 1085753.2402817172
        midpoint_d2 = exact_distance_squared(at(LARGE_START, LARGE_END, Q(1, 2)), LARGE)
        self.assertGreater(midpoint_d2, Q(228000)**2)
        self.assertLess(midpoint_d2, Q(229000)**2)
        self.assertLess(midpoint_d2, Q(radius)**2)
        self.assert_first_entry(LARGE, LARGE_START, LARGE_END, radius, Q(1, 2))

    def test_subnormal_motion_returns_no_contact_or_explicit_domain_refusal(self):
        big = 1e6
        radius = big-2*math.ulp(big)
        vertices = [[0, -big, 0], [1, -big-1, 0], [0, -big-1, 1],
                    [1e9, -1e9, 0], [1e9, 1e9, 0], [1e9, 0, 1]]
        index = self.index_from_obj(vertices, [[0, 1, 2], [3, 4, 5]])
        end_y = -math.ulp(0.)
        # First triangle remains at least R-|dy| away in y; the second is
        # 1e9 away in x. These exact lower bounds cover the whole segment.
        self.assertGreater(Q(big)+Q(end_y), Q(radius))
        self.assertGreater(Q(1e9), Q(radius))
        try:
            result = index.sweep_sphere([0, 0, 0], [0, end_y, 0], radius)
        except RigError as error:
            self.assertTrue(str(error).strip(), 'A domain refusal must explain itself')
        else:
            self.assertIsNone(result)

    def test_control_face_entry_from_either_side(self):
        triangle = [[-4, 0, -4], [4, 0, -4], [0, 0, 4]]
        for sign in (-1, 1):
            with self.subTest(sign=sign):
                start, end = [0, 2*sign, 0], [0, -2*sign, 0]
                hit = self.index_from_obj(triangle).sweep_sphere(start, end, 1)
                self.assert_valid_hit(hit, start, end, 1)
                self.assertEqual(hit['fraction'], .25)
                np.testing.assert_allclose(hit['normal'], [0, sign, 0], rtol=0, atol=1e-14)

    def test_control_edge_entry(self):
        triangle = [[0, 0, -4], [0, 0, 4], [4, 0, 0]]
        start, end = [-2, 0, 0], [2, 0, 0]
        hit = self.index_from_obj(triangle).sweep_sphere(start, end, 1)
        self.assert_valid_hit(hit, start, end, 1)
        self.assertEqual(hit['fraction'], .25)
        np.testing.assert_array_equal(hit['point'], [0, 0, 0])

    def test_control_vertex_positive_ulp_entry(self):
        triangle = [[0, 0, 0], [0, -1, 0], [0, 0, -1]]
        self.assert_first_entry(triangle, [0, math.nextafter(3., math.inf), 4], [0, 0, 4], 5)

    def test_control_exact_vertex_tangency(self):
        triangle = [[0, 0, 0], [0, -1, 0], [0, 0, -1]]
        start, end = [-10, 3, 4], [10, 3, 4]
        self.assertEqual(exact_distance_squared(at(start, end, Q(1, 2)), triangle), 25)
        hit = self.index_from_obj(triangle).sweep_sphere(start, end, 5)
        self.assert_valid_hit(hit, start, end, 5)
        self.assertEqual(hit['fraction'], .5)

    def test_control_one_ulp_smaller_radius_misses_tangent_path(self):
        triangle = [[0, 0, 0], [0, -1, 0], [0, 0, -1]]
        radius = math.nextafter(5., 0.)
        self.assertLess(Q(radius)**2, 25)
        self.assertIsNone(self.index_from_obj(triangle).sweep_sphere([-10, 3, 4], [10, 3, 4], radius))

    def test_control_motion_away_from_face_misses(self):
        triangle = [[-4, 0, -4], [4, 0, -4], [0, 0, 4]]
        self.assertIsNone(self.index_from_obj(triangle).sweep_sphere([0, 2, 0], [0, 3, 0], 1))


if __name__ == '__main__':
    unittest.main()
