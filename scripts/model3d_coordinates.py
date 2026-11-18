"""Explicit, nondestructive coordinate conversion for unskinned native geometry.

Skins and animation belong to graph-aware acquisition; this API accepts Geometry,
not ImportedModel/RigAsset, and never implies their transforms were converted.
"""
from __future__ import annotations
import copy
import numbers
import numpy as np
from rig_errors import require
from rig_models import Geometry, validate_geometry

BASES = {
    'canonical': np.eye(3),
    'right-handed-z-up': np.array([[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]]),
    'left-handed-y-up': np.diag([1., 1., -1.]),
}


def _numeric(value, shape):
    require(isinstance(value, (list, tuple, np.ndarray)) and (not isinstance(value, np.ndarray) or value.ndim > 0)
            and len(value) == shape[0], 'MODEL3D_TRANSFORM', 'Coordinate array has an invalid shape.')
    if len(shape) > 1:
        return np.asarray([_numeric(row, shape[1:]) for row in value])
    require(all(isinstance(v, numbers.Real) and not isinstance(v, (bool, np.bool_))
                and -1e100 <= v <= 1e100 and np.isfinite(v) for v in value),
            'MODEL3D_TRANSFORM', 'Coordinates must be bounded finite numbers, not booleans or strings.')
    return np.asarray(value, dtype=float)


def _positions(mesh):
    values = np.asarray(mesh.positions)
    require(values.dtype.kind in 'fiu', 'MODEL3D_TRANSFORM', 'Positions must be numeric arrays.')
    if values.dtype.kind in 'iu':
        require(np.all(values <= 2**53) and (values.dtype.kind == 'u' or np.all(values >= -2**53)),
                'MODEL3D_TRANSFORM', 'Integer coordinates exceed exact float64 precision.')
    result = values.astype(np.float64)
    require(np.isfinite(result).all(), 'MODEL3D_TRANSFORM', 'Coordinates exceed numerical range.')
    return result


def conversion_matrix(*, source_basis='canonical', target_basis='canonical',
                      unit_scale=1., pivot=(0., 0., 0.), translation=(0., 0., 0.)):
    """Map source coordinates with target = basis*(source-pivot)*scale+translation.

    unit_scale is target units per source unit. Pivot is source-space; translation
    is target-space. No automatic origin/ground heuristic or physical-scale claim.
    """
    require(isinstance(source_basis, str) and isinstance(target_basis, str)
            and source_basis in BASES and target_basis in BASES, 'MODEL3D_BASIS', 'Unknown coordinate basis.')
    require(isinstance(unit_scale, (int, float)) and not isinstance(unit_scale, bool)
            and 1e-6 <= unit_scale <= 1e6 and np.isfinite(unit_scale),
            'MODEL3D_SCALE', 'Explicit unit scale must be finite within [1e-6, 1e6].')
    p, t = _numeric(pivot, (3,)), _numeric(translation, (3,))
    linear = BASES[target_basis].T @ BASES[source_basis] * float(unit_scale)
    result = np.eye(4)
    result[:3, :3] = linear
    result[:3, 3] = t-linear@p
    require(np.isfinite(result).all(), 'MODEL3D_TRANSFORM', 'Computed transform must be finite.')
    return result


def transform_geometry(geometry: Geometry, matrix, *, limits=None) -> Geometry:
    """Return a transformed copy, preserving IDs/materials/UVs and morph semantics.

    Invertible affine transforms only. Reflection reverses face winding and tangent
    handedness. Direction morph deltas share the base normalization denominator,
    preserving normalized(base + weighted delta) under arbitrary affine scales.
    """
    from model3d_primitives import _limits
    limits = _limits(limits)
    validate_geometry(geometry, limits)
    m = _numeric(matrix, (4, 4))
    require(np.array_equal(m[3], [0, 0, 0, 1]), 'MODEL3D_TRANSFORM', 'Expected affine 4x4 matrix.')
    a = m[:3, :3]
    scales = np.max(np.abs(a), axis=0)
    require(np.all(scales > 0), 'MODEL3D_TRANSFORM', 'Transform must be invertible.')
    det = float(np.linalg.det(a/scales))
    require(np.isfinite(det) and abs(det) > 1e-12, 'MODEL3D_TRANSFORM', 'Transform is singular or numerically ill-conditioned.')
    try:
        inverse = np.linalg.inv(a)
    except np.linalg.LinAlgError:
        require(False, 'MODEL3D_TRANSFORM', 'Transform must have a finite inverse.')
    require(np.isfinite(inverse).all(), 'MODEL3D_TRANSFORM', 'Transform inverse must be finite.')
    result = copy.deepcopy(geometry)
    for mesh in result.meshes:
        mesh.positions = _positions(mesh) @ a.T + m[:3, 3]
        if det < 0:
            mesh.triangles = mesh.triangles[:, [0, 2, 1]].copy()
        for targets in mesh.morphs.values():
            targets['POSITION'] = targets['POSITION'] @ a.T
        for semantic, operator in [('NORMAL', inverse), ('TANGENT', a.T)]:
            if any(semantic in target for target in mesh.morphs.values()):
                require(semantic in mesh.attributes, 'MODEL3D_TRANSFORM', 'Direction morph requires its base attribute.')
            if semantic not in mesh.attributes:
                continue
            original = np.asarray(mesh.attributes[semantic], dtype=np.float64).copy()
            mesh.attributes[semantic] = original
            direction = original[:, :3] @ operator
            length = np.linalg.norm(direction, axis=1)
            require(np.isfinite(length).all() and np.all(length > 0), 'MODEL3D_TRANSFORM', 'Cannot transform zero/nonfinite directions.')
            original[:, :3] = direction/length[:, None]
            if semantic == 'TANGENT' and det < 0:
                original[:, 3] *= -1
            for targets in mesh.morphs.values():
                if semantic in targets:
                    targets[semantic] = (targets[semantic] @ operator)/length[:, None]
    validate_geometry(result, limits)
    return result


def transform_editable(editable, matrix, *, limits=None):
    """Transform both retained polygon source and native render mesh together."""
    from model3d_primitives import EditableMesh
    require(isinstance(editable, EditableMesh), 'MODEL3D_TRANSFORM', 'Expected EditableMesh.')
    geometry = transform_geometry(editable.geometry(), matrix, limits=limits)
    m = np.asarray(matrix, dtype=float)
    source = editable.source_positions @ m[:3, :3].T + m[:3, 3]
    require(np.isfinite(source).all(), 'MODEL3D_TRANSFORM', 'Source positions overflowed.')
    source.setflags(write=False)
    faces = editable.faces
    if np.linalg.det(m[:3, :3]/np.max(np.abs(m[:3, :3]),axis=0)) < 0:
        faces = tuple(tuple(reversed(face)) for face in faces)
    owners = editable.triangle_faces.copy(); owners.setflags(write=False)
    return EditableMesh(geometry.meshes[0], source, faces, owners, editable.normal_mode)


def placement(geometry, *, purpose='prop', pivot=None, anchor=None, ground=False,
              minimum_dimensions=None, maximum_dimensions=None, limits=None):
    """Measure bounds and select an explicit, overridable source-space pivot.

    The result is a proposal with a reason, not inferred anatomy/physical scale.
    Component pivots require an authored anchor unless explicitly overridden.
    Architecture retains its authored scene origin; props use their bounds center;
    characters use the center of the measured lower Y support plane.
    """
    require(type(ground) is bool, 'MODEL3D_PIVOT', 'ground must be an explicit boolean.')
    from model3d_primitives import _limits
    limits = _limits(limits); validate_geometry(geometry, limits)
    require(purpose in ('character', 'prop', 'architecture', 'component'), 'MODEL3D_PIVOT', 'Unknown placement purpose.')
    lower = np.min([_positions(m).min(axis=0) for m in geometry.meshes], axis=0)
    upper = np.max([_positions(m).max(axis=0) for m in geometry.meshes], axis=0)
    dimensions = upper-lower
    require(np.isfinite(dimensions).all(), 'MODEL3D_DIMENSIONS', 'Bounds must be finite.')
    for values, compare, label in [(minimum_dimensions, np.greater_equal, 'minimum'),
                                    (maximum_dimensions, np.less_equal, 'maximum')]:
        if values is not None:
            value = _numeric(values, (3,))
            require(np.all(value >= 0) and np.all(compare(dimensions, value)), 'MODEL3D_DIMENSIONS', 'Dimensional constraint failed.', asset=label)
    if pivot is not None:
        chosen, reason = _numeric(pivot, (3,)), 'explicit user pivot'
    elif anchor is not None:
        chosen, reason = _numeric(anchor, (3,)), 'authored attachment/root anchor'
    elif purpose == 'component':
        require(False, 'MODEL3D_PIVOT', 'Components need an explicit joint/hinge/attachment pivot.')
    elif purpose == 'architecture':
        chosen, reason = np.zeros(3), 'preserved authored scene origin'
    else:
        chosen = lower/2+upper/2
        if purpose == 'character':chosen[1] = lower[1]
        reason = 'measured support-plane center' if purpose == 'character' else 'measured manipulation center'
    translation = np.zeros(3)
    if ground:translation[1] = chosen[1]-lower[1]
    matrix = conversion_matrix(pivot=chosen, translation=translation)
    return dict(bounds=[lower.tolist(), upper.tolist()], dimensions=dimensions.tolist(),
                pivot=chosen.tolist(), reason=reason, matrix=matrix.tolist(),
                scale_status='canonical coordinates; physical source calibration remains an external provenance claim')


def transform_frames(frames, matrix):
    """Convert generic named affine anchors without renaming them.

    Frames are affine world-from-local matrices, not typed camera records.
    Actual camera optical-local -Z, rigid pose, units and lens semantics belong
    to camera3d_math/contracts; arbitrary affine scale is not a valid camera pose.
    For unit rigid poses our world-only C@M agrees with the authoritative camera
    basis helper; it never conjugates or substitutes actor-forward +Z for -Z. Local child transforms remain unchanged when their
    common root alone is converted; callers must not convert both twice.
    """
    require(isinstance(frames, dict) and len(frames) <= 4096, 'MODEL3D_FRAMES', 'Expected bounded named frame mapping.')
    m = _numeric(matrix, (4, 4))
    require(np.array_equal(m[3], [0,0,0,1]), 'MODEL3D_FRAMES', 'Frame conversion must be affine.')
    result = {}
    for name, value in frames.items():
        require(isinstance(name, str) and 0 < len(name) <= 1024, 'MODEL3D_FRAMES', 'Frame names must be nonempty bounded strings.')
        frame = _numeric(value, (4, 4))
        require(np.array_equal(frame[3], [0,0,0,1]), 'MODEL3D_FRAMES', 'Source frame must be affine.')
        transformed = m@frame
        require(np.isfinite(transformed).all(), 'MODEL3D_FRAMES', 'Frame transform overflowed.')
        try:inverse = np.linalg.inv(transformed)
        except np.linalg.LinAlgError:require(False, 'MODEL3D_FRAMES', 'Frame must be invertible.')
        require(np.isfinite(inverse).all(), 'MODEL3D_FRAMES', 'Frame inverse must be finite.')
        result[name] = transformed.tolist()
    return result
