"""Bounded procedural fixtures using the shared indexed mesh authority.

Centered right-handed geometry, +Y up, +Z forward, dimensions in meters.
"""
from __future__ import annotations

import math
import numpy as np
from rig_geometry import vertex_normals
from rig_models import Mesh, Geometry, validate_geometry


def _size(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1e-4 <= value <= 1e4 or not math.isfinite(value):
        raise ValueError('Dimensions must be finite numbers in [0.0001, 10000] meters.')
    return float(value)


def _count(value, minimum):
    if type(value) is not int or not minimum <= value <= 256:
        raise ValueError(f'Segments must be integers in [{minimum}, 256].')
    return value


def _mesh(name, vertices, faces):
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError('A nonempty name of at most 128 characters is required.')
    positions = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(faces, dtype=np.int64)
    # All primitives are convex and centered: orient every face outward.
    points = positions[triangles]
    inward = np.einsum('ij,ij->i', np.cross(points[:, 1]-points[:, 0], points[:, 2]-points[:, 0]), points.mean(axis=1)) < 0
    triangles[inward] = triangles[inward][:, [0, 2, 1]]
    face_cross = np.cross(positions[triangles[:, 1]] - positions[triangles[:, 0]],
                          positions[triangles[:, 2]] - positions[triangles[:, 0]])
    normal_scale = math.sqrt(float(np.linalg.norm(face_cross, axis=1).min()))
    mesh = Mesh(name, positions, triangles, name=name,
                attributes={'NORMAL': vertex_normals(positions / normal_scale, triangles)})
    validate_geometry(Geometry([mesh], materials=[{}]))
    return mesh


def box(width=1., height=1., depth=1., *, name='box'):
    """Return a closed eight-vertex box with shared, smoothed corner normals."""
    x, y, z = (_size(v)/2 for v in (width, height, depth))
    vertices = [(a*x, b*y, c*z) for a, b, c in
                [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]]
    faces = [(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),
             (3,2,6),(3,6,7),(0,3,7),(0,7,4),(1,5,6),(1,6,2)]
    return _mesh(name, vertices, faces)


def sphere(radius=0.5, *, segments=32, rings=16, name='sphere'):
    """Return a closed UV sphere with unique poles (no degenerate caps)."""
    r = _size(radius); n = _count(segments, 3); k = _count(rings, 2)
    vertices = [(0, r, 0)]
    for j in range(1, k):
        phi = math.pi*j/k
        vertices.extend((r*math.sin(phi)*math.cos(2*math.pi*i/n), r*math.cos(phi),
                         r*math.sin(phi)*math.sin(2*math.pi*i/n)) for i in range(n))
    bottom = len(vertices); vertices.append((0, -r, 0))
    faces = []
    for i in range(n):
        q = (i+1)%n
        faces.extend([(0,1+i,1+q),(bottom,1+(k-2)*n+q,1+(k-2)*n+i)])
        for j in range(k-2):
            a=1+j*n+i; b=1+j*n+q; c=b+n; d=a+n
            faces.extend([(a,b,c),(a,c,d)])
    return _mesh(name, vertices, faces)


def cylinder(radius=0.5, height=1., *, segments=32, name='cylinder'):
    """Return a closed Y-axis cylinder with shared rim vertices."""
    r = _size(radius); h = _size(height)/2; n = _count(segments, 3)
    vertices = [(r*math.cos(2*math.pi*i/n), y, r*math.sin(2*math.pi*i/n))
                for y in (-h,h) for i in range(n)] + [(0,-h,0),(0,h,0)]
    faces=[]
    for i in range(n):
        q=(i+1)%n
        faces.extend([(i,q,q+n),(i,q+n,i+n),(2*n,q,i),(2*n+1,i+n,q+n)])
    return _mesh(name, vertices, faces)
