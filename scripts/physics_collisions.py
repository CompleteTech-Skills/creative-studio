"""Geometric admission for explicit Bullet collision proxies.

Visual geometry and collision geometry are independent. This module performs no
solver calls and imports optional numerical libraries only at mesh admission.
"""
from __future__ import annotations

import math


class CollisionError(ValueError):
    pass


def _require(value, message):
    if not value:
        raise CollisionError(message)


def validate_shape(shape, *, dynamic, source_ids=(), depth=0):
    """Return resource counts after bounded geometric validation of one proxy."""
    _require(depth <= 8, 'Collision proxy nesting exceeds eight levels.')
    kind = shape['type']
    if kind == 'compound':
        children = shape['children']
        _require(1 <= len(children) <= 64, 'Compound needs one to 64 children.')
        counts = [validate_shape(child['shape'], dynamic=dynamic, source_ids=source_ids,
                                 depth=depth+1) for child in children]
        result = tuple(sum(value[i] for value in counts) for i in range(3))
        _require(result[2] <= 64, 'Compound exceeds 64 primitive leaves.')
        return result
    if kind in ('sphere', 'box', 'capsule', 'cylinder'):
        dimensions = shape['size'] if kind == 'box' else [shape['radius']]
        if kind in ('capsule', 'cylinder'):
            dimensions = [*dimensions, shape['height']]
        _require(all(type(x) in (float, int) and math.isfinite(x) and .001 <= x <= 1000
                     for x in dimensions), 'Proxy dimensions must be finite within .001..1000 meters.')
        return 0, 0, 1
    _require(kind in ('convex-hull', 'triangle-mesh'), 'Unsupported collision proxy.')
    _require(not dynamic or kind != 'triangle-mesh', 'Concave triangle-mesh colliders must be static; use explicit convex decomposition for dynamics.')
    _require(shape['source'] in source_ids, 'Proxy source must name an admitted geometry asset.')
    import numpy as np
    points = np.asarray(shape['vertices'], dtype=float)
    faces = np.asarray(shape['triangles'], dtype=int)
    _require(points.ndim == 2 and points.shape[1:] == (3,) and 3 <= len(points) <= 4096,
             'Proxy mesh needs 3..4096 vertices.')
    _require(faces.ndim == 2 and faces.shape[1:] == (3,) and 1 <= len(faces) <= 8192,
             'Proxy mesh needs 1..8192 triangles.')
    _require(np.all(np.isfinite(points)) and np.max(np.abs(points)) <= 1000,
             'Proxy vertices must be finite within 1000 meters.')
    _require(np.all(faces >= 0) and np.max(faces) < len(points), 'Proxy triangle index is out of range.')
    _require(len({tuple(sorted(face)) for face in faces}) == len(faces), 'Duplicate proxy triangles.')
    triangles = points[faces]
    normals = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])
    scale = max(float(np.ptp(points, axis=0).max()), 1e-12)
    lengths = np.linalg.norm(normals, axis=1)
    _require(np.all(lengths > scale*scale*1e-12), 'Degenerate proxy triangle.')
    if kind == 'convex-hull':
        _require(len(points) >= 4 and len(faces) >= 4 and np.linalg.matrix_rank(points-points.mean(axis=0), tol=scale*1e-10) == 3,
                 'Convex proxy must span three dimensions.')
        edges = {}
        for face in faces:
            for a, b in zip(face, np.roll(face, -1)):
                edges.setdefault(tuple(sorted((int(a), int(b)))), []).append((int(a), int(b)))
        _require(all(len(pair) == 2 and pair[0] == pair[1][::-1] for pair in edges.values()),
                 'Convex proxy must be a closed consistently oriented manifold.')
        _require(len(set(faces.flat)) == len(points), 'Convex proxy contains unused vertices.')
        center = points.mean(axis=0)
        # Each face must point outward and bound every vertex on its inner side.
        # Batch to avoid allocating an unbounded vertices-by-faces matrix.
        unit = normals/lengths[:, None]
        _require(np.all(np.sum(unit*(triangles[:, 0]-center), axis=1) > scale*1e-10),
                 'Convex proxy faces must be outward with positive enclosed volume.')
        for start in range(0, len(faces), 64):
            axes = unit[start:start+64]
            offsets = np.sum(axes*triangles[start:start+64, 0], axis=1)
            _require(np.all(points @ axes.T <= offsets+scale*1e-8),
                     'Convex proxy has a concave face; supply an explicit convex decomposition.')
    return len(points), len(faces), 1


def validate_colliders(body, *, source_ids):
    """Admit one proxy tree without silently enabling unavailable sensors."""
    _require(len(body['colliders']) == 1, 'Use one explicit compound proxy for multi-part collision.')
    collider = body['colliders'][0]
    _require(not collider['sensor'], 'Bullet sensor response is not implemented by this adapter.')
    _require(1 <= collider['group'] <= 0xffffffff and 0 <= collider['mask'] <= 0xffffffff,
             'Collision filters must be 32-bit masks.')
    return validate_shape(collider['shape'], dynamic=body['mode'] == 'dynamic', source_ids=source_ids)


def collision_capabilities():
    return dict(backend='pybullet', shapes=['sphere', 'box', 'capsule', 'cylinder',
        'convex-hull', 'compound', 'static-triangle-mesh'], convex_decomposition='explicit convex compound children',
        filtering='bilateral group/mask; explicit pair exclusions', contacts='native Bullet contact manifolds',
        raycast='native Bullet closest ray test', sweep='unsupported', sensors='unsupported',
        continuous_collision='not guaranteed; high-speed tunneling must be measured')


def verify_native_children(body, controls):
    """Compare native Bullet children with the independently admitted proxy tree."""
    import numpy as np
    from rig_math import qmatrix, qmul
    from physics_collision_native import collision_leaves
    expected = collision_leaves(body['colliders'][0])
    actual = controls.get('collision_children')
    _require(type(actual) is list and len(actual) == len(expected), 'Native collision child count differs from proxy.')
    axes = qmatrix(controls['inertial_rotation'])
    center = np.asarray(body['mass']['center_of_mass'])
    kinds = {'sphere': 2, 'box': 3, 'cylinder': 4, 'convex-hull': 5, 'triangle-mesh': 5, 'capsule': 7}
    for child, leaf in zip(actual, expected):
        shape=leaf['shape'];kind=shape['type']
        dimensions = ([shape['radius']]*3 if kind=='sphere' else shape['size'] if kind=='box'
            else [shape['height'],shape['radius'],0.] if kind in ('capsule','cylinder') else [1.,1.,1.])
        _require(child.get('type') == kinds[kind], 'Native collision type differs from proxy.')
        _require(np.allclose(child['dimensions'], dimensions, atol=1e-7, rtol=1e-6), 'Native collision dimensions differ from proxy.')
        _require(np.allclose(axes @ np.asarray(child['translation'])+center, leaf['translation'], atol=1e-6, rtol=0),
                 'Native collision child offset differs from proxy.')
        rotation=qmatrix(qmul(controls['inertial_rotation'], child['rotation']))
        _require(np.allclose(rotation,qmatrix(leaf['rotation']),atol=1e-6,rtol=0),
                 'Native collision child rotation differs from proxy.')
