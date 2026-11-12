"""Scene transform evaluation and explicit adapters over the existing rig math.

Arrays are row-major in Python, column-vector mathematics, local T @ R @ S,
world parent @ local. Blender conversion is applied by conjugation exactly once.
This module imports optional NumPy; scene schema generation does not import it.
"""
from __future__ import annotations

import math
import numpy as np

from rig_errors import RigError
from rig_math import (BLENDER_FROM_CANONICAL, compose, decompose, qmatrix,
                      quaternion, slerp, vector)
from scene3d_contracts import require, validate_document


BLENDER_BASIS = BLENDER_FROM_CANONICAL


def local_matrix(transform):
    """TRS with an explicit optional pivot-about-point adapter.

    Persisted translation retains the rig TRS meaning. With pivot p, the effective
    translation is t + p - R*S*p, i.e. T(t) T(p) R S T(-p). The pivot is not a second
    world position. Exporters must use this evaluated matrix/baked translation.
    """
    q = quaternion(transform['rotation'], strict=True)
    result = compose(transform['translation'], q, transform['scale'])
    if 'pivot' in transform:
        pivot = vector(transform['pivot'])
        result[:3, 3] += pivot - result[:3, :3] @ pivot
    return result


def _matrix(matrix):
    a = np.asarray(matrix, dtype=np.float64)
    require(a.shape == (4, 4) and np.isfinite(a).all() and
            np.allclose(a[3], [0, 0, 0, 1], atol=1e-10, rtol=0),
            'Expected a finite affine 4 by 4 matrix.')
    return a


def to_blender(matrix):
    """Canonical Y-up to Blender Z-up; (x,y,z) maps to (x,-z,y)."""
    return BLENDER_BASIS @ _matrix(matrix) @ BLENDER_BASIS.T


def from_blender(matrix):
    return BLENDER_BASIS.T @ _matrix(matrix) @ BLENDER_BASIS


def to_gltf(matrix):
    """Basis is already glTF. Serialize only to column-major flat storage."""
    return _matrix(matrix).T.reshape(16).tolist()


def from_gltf(values):
    a = np.asarray(values, dtype=np.float64)
    require(a.shape == (16,), 'Expected 16 glTF matrix components.')
    return _matrix(a.reshape(4, 4).T)


def evaluate_transforms(scene):
    """Evaluate the validated static TRS hierarchy, without renderer state.

    Constraint, attachment, animation and prefab expansion require their owning
    evaluators; unsupported dynamic declarations fail rather than being ignored.
    World orientation/scale are None for shear, while the matrix stays exact.
    """
    scene = validate_document(scene, 'scene')
    require(all(not n['constraints'] and not n['animation_bindings'] and
                'attachment' not in n and 'prefab' not in n for n in scene['nodes']),
            'Static transform evaluation requires expanded nodes without dynamic constraints/bindings.')
    nodes = {n['id']: n for n in scene['nodes']}
    result = {}
    def visit(identifier):
        if identifier in result:
            return result[identifier]
        node = nodes[identifier]
        local = local_matrix(node['transform'])
        parent = visit(node['parent']) if node['parent'] is not None else None
        world = (np.array(parent['world_matrix']) @ local) if parent else local
        require(np.isfinite(world).all(), 'Hierarchy produced a nonfinite transform.')
        decomposed = None
        try:
            decomposed = decompose(world)
        except RigError as exc:
            if exc.report['code'] != 'RIG_SHEAR':
                raise
        result[identifier] = dict(local_matrix=local.tolist(), world_matrix=world.tolist(),
            world_position=world[:3, 3].tolist(),
            world_orientation=decomposed['rotation'] if decomposed else None,
            world_scale=decomposed['scale'] if decomposed else None,
            decomposable=decomposed is not None,
            visible=node['visible'] and (parent['visible'] if parent else True))
        return result[identifier]
    for identifier in nodes:
        visit(identifier)
    return result


def overlay_point(pixel, *, pixels_per_meter, origin, depth):
    """Explicit pixel/Y-down to canonical XY-plane mapping; never implicit."""
    require(type(pixels_per_meter) in (int, float) and math.isfinite(pixels_per_meter)
            and pixels_per_meter > 0, 'pixels_per_meter must be finite and positive.')
    require(type(depth) in (int, float) and math.isfinite(depth), 'Overlay depth must be finite.')
    p, o = vector(pixel, 2), vector(origin, 2)
    return [(p[0] - o[0]) / pixels_per_meter, (o[1] - p[1]) / pixels_per_meter, float(depth)]


def overlay_rotation(clockwise_radians):
    require(type(clockwise_radians) in (int, float) and math.isfinite(clockwise_radians),
            'Overlay rotation must be finite radians.')
    return [0., 0., -math.sin(clockwise_radians / 2), math.cos(clockwise_radians / 2)]
