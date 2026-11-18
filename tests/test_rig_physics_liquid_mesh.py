"""Actual native-format decode and resource-bound tests; no fake solver positives."""
import gzip
import math
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_liquid_mesh import decode_bobj,canonical_vertices,measurements,LiquidMeshError


def encoded(vertices,triangles):
    return gzip.compress(struct.pack('<i',len(vertices))+b''.join(struct.pack('<3f',*v) for v in vertices)+
        struct.pack('<i',len(vertices))+b''.join(struct.pack('<3f',0,0,1) for _ in vertices)+
        struct.pack('<i',len(triangles))+b''.join(struct.pack('<3i',*v) for v in triangles))


class NativeLiquidMeshTests(unittest.TestCase):
    def test_valid_triangle_is_decoded_with_explicit_inverse_basis(self):
        mesh=decode_bobj(encoded([[0,0,0],[1,0,0],[0,1,0]],[[0,1,2]]))
        positions=canonical_vertices(mesh,dict(size=[3,3,3],transform=dict(translation=[1,2,3])))
        self.assertEqual(positions,[[1,2,3],[4,2,3],[1,2,0]])
        self.assertEqual(measurements(positions,mesh['triangles'])['bounds'],[[1,2,0],[4,2,3]])

    def test_empty_cache_mesh_is_valid_but_not_a_liquid_readiness_claim(self):
        mesh=decode_bobj(encoded([],[]));self.assertEqual(measurements(**mesh)['vertex_count'],0)

    def test_corrupt_nonfinite_or_invalid_indices_fail(self):
        good=encoded([[0,0,0],[1,0,0],[0,1,0]],[[0,1,2]])
        for payload in (good[:-1],good+b'junk',good+good,encoded([[math.inf,0,0],[1,0,0],[0,1,0]],[[0,1,2]]),
            encoded([[0,0,0],[1,0,0],[0,1,0]],[[0,1,3]]),encoded([[0,0,0],[1,0,0],[0,1,0]],[[0,0,2]])):
            with self.subTest(size=len(payload)),self.assertRaises(LiquidMeshError):decode_bobj(payload)

    def test_header_and_decompression_counts_are_bounded(self):
        good=encoded([[0,0,0],[1,0,0],[0,1,0]],[[0,1,2]])
        for limits in [dict(max_vertices=2),dict(max_triangles=0),dict(max_expanded_bytes=16)]:
            with self.assertRaises(LiquidMeshError):decode_bobj(good,**limits)
        with self.assertRaises(LiquidMeshError):decode_bobj(gzip.compress(struct.pack('<i',2**31-1)))
        with self.assertRaises(LiquidMeshError):decode_bobj(gzip.compress(b'x'*100000),max_expanded_bytes=1000)
