"""Right-handed meters; XYZW quaternions; matrices act on column vectors.

World = parent @ local. Array storage in Python is row-major; glTF serialization
explicitly transposes matrices to column-major. No incremental pose integration.
"""
from __future__ import annotations

import math
from typing import Sequence
import numpy as np

from rig_errors import require

EPS = 1e-10
IDENTITY_Q = [0.0, 0.0, 0.0, 1.0]
BLENDER_FROM_CANONICAL = np.array([[1., 0., 0., 0.], [0., 0., -1., 0.],
                                  [0., 1., 0., 0.], [0., 0., 0., 1.]])


def vector(value: Sequence[float], size: int = 3) -> np.ndarray:
    a = np.asarray(value, dtype=np.float64)
    require(a.shape == (size,) and np.isfinite(a).all(), 'RIG_VECTOR', f'Expected a finite {size}-vector.')
    return a


def unit(value: Sequence[float]) -> np.ndarray:
    a = vector(value)
    length = np.linalg.norm(a)
    require(length > EPS, 'RIG_ZERO_VECTOR', 'A direction vector cannot be zero.')
    return a / length


def quaternion(value: Sequence[float], *, strict: bool = False) -> np.ndarray:
    q = vector(value, 4)
    length = float(np.linalg.norm(q))
    require(length > EPS, 'RIG_QUATERNION', 'A quaternion cannot have zero length.')
    require(not strict or abs(length - 1) < 1e-5, 'RIG_QUATERNION', 'Quaternion must be normalized.', observed=length)
    return q / length


def qmul(left: Sequence[float], right: Sequence[float]) -> np.ndarray:
    x, y, z, w = left
    a, b, c, d = right
    return np.array([w*a + x*d + y*c - z*b, w*b - x*c + y*d + z*a,
                     w*c + x*b - y*a + z*d, w*d - x*a - y*b - z*c])


def qinverse(value: Sequence[float]) -> np.ndarray:
    q = quaternion(value)
    return q * [-1, -1, -1, 1]


def axis_angle(axis: Sequence[float], angle: float) -> np.ndarray:
    require(math.isfinite(angle), 'RIG_NONFINITE', 'Rotation angle must be finite.')
    return np.r_[unit(axis) * math.sin(angle / 2), math.cos(angle / 2)]


def qmatrix(value: Sequence[float]) -> np.ndarray:
    x, y, z, w = quaternion(value)
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])


def matrix_quaternion(matrix: np.ndarray) -> np.ndarray:
    m = np.asarray(matrix, dtype=np.float64)[:3, :3]
    require(np.isfinite(m).all() and np.allclose(m.T @ m, np.eye(3), atol=1e-6)
            and np.linalg.det(m) > 0, 'RIG_ROTATION_MATRIX', 'Rotation matrix must be orthonormal and proper.')
    trace = float(np.trace(m))
    if trace > 0:
        s = math.sqrt(trace + 1) * 2
        q = [(m[2, 1]-m[1, 2])/s, (m[0, 2]-m[2, 0])/s, (m[1, 0]-m[0, 1])/s, s/4]
    else:
        i = int(np.argmax(np.diag(m)))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = math.sqrt(max(EPS, 1 + m[i, i] - m[j, j] - m[k, k])) * 2
        q = [0., 0., 0., (m[k, j] - m[j, k])/s]
        q[i] = s / 4; q[j] = (m[j, i] + m[i, j])/s; q[k] = (m[k, i] + m[i, k])/s
    return quaternion(q)


def qbetween(source: Sequence[float], target: Sequence[float]) -> np.ndarray:
    a, b = unit(source), unit(target)
    dot = float(np.clip(np.dot(a, b), -1, 1))
    cross = np.cross(a, b)
    if dot >= 0:
        # Retain small but measurable rotations rather than snapping their
        # cosine to identity. This form is stable throughout this hemisphere.
        return quaternion(np.r_[cross, 1 + dot])
    # Near the opposite direction, multiply by the small sum rather than
    # subtracting two nearly equal products to construct the rotation axis.
    cross = np.cross(a, a + b)
    sine = math.hypot(*cross)
    if sine <= 4 * np.finfo(float).eps:
        # Independently normalized antiparallel vectors can leave a cross
        # product at roundoff scale whose direction is numerical noise.
        basis = np.eye(3)[int(np.argmin(np.abs(a)))]
        return axis_angle(np.cross(a, basis), math.pi)
    # Near the opposite direction, 1+dot loses the small angular difference.
    # atan2 retains it, including when the rounded dot is exactly -1.
    half = math.atan2(sine, dot) / 2
    return np.r_[cross / sine * math.sin(half), math.cos(half)]


def slerp(a: Sequence[float], b: Sequence[float], weight: float) -> np.ndarray:
    require(math.isfinite(weight), 'RIG_NONFINITE', 'Blend weight must be finite.')
    a, b = quaternion(a), quaternion(b)
    dot = float(np.dot(a, b))
    if dot < 0:
        b, dot = -b, -dot
    dot = float(np.clip(dot, -1, 1))
    if dot > .9995:
        return quaternion(a + weight * (b - a))
    angle = math.acos(dot)
    return quaternion((math.sin((1-weight)*angle)*a + math.sin(weight*angle)*b) / math.sin(angle))


def qangle(a: Sequence[float], b: Sequence[float] = IDENTITY_Q) -> float:
    return 2 * math.acos(float(np.clip(abs(np.dot(quaternion(a), quaternion(b))), 0, 1)))


def compose(translation=(0., 0., 0.), rotation=IDENTITY_Q, scale=(1., 1., 1.)) -> np.ndarray:
    t, s = vector(translation), vector(scale)
    require(np.all(s > EPS), 'RIG_SINGULAR_TRANSFORM', 'Scale must be positive and nonsingular.')
    result = np.eye(4)
    result[:3, :3] = qmatrix(rotation) @ np.diag(s)
    result[:3, 3] = t
    return result


def decompose(matrix: np.ndarray) -> dict:
    m = np.asarray(matrix, dtype=np.float64)
    require(m.shape == (4, 4) and np.isfinite(m).all() and np.allclose(m[3], [0, 0, 0, 1]),
            'RIG_MATRIX', 'Expected finite affine 4×4 transform.')
    scale = np.linalg.norm(m[:3, :3], axis=0)
    require(np.all(scale > EPS), 'RIG_SINGULAR_TRANSFORM', 'Singular transforms are unsupported.')
    rot = m[:3, :3] / scale
    require(np.linalg.det(rot) > 0 and np.allclose(rot.T @ rot, np.eye(3), atol=1e-6),
            'RIG_SHEAR', 'Reflections or shear require an explicit mesh repair before rigging.')
    return dict(translation=m[:3, 3].tolist(), rotation=matrix_quaternion(rot).tolist(), scale=scale.tolist())


def transform_matrix(transform: dict) -> np.ndarray:
    return compose(transform.get('translation', [0, 0, 0]), transform.get('rotation', IDENTITY_Q),
                   transform.get('scale', [1, 1, 1]))


def transform_points(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def transform_normals(matrix: np.ndarray, normals: np.ndarray) -> np.ndarray:
    result = np.asarray(normals) @ np.linalg.inv(matrix[:3, :3])
    lengths = np.linalg.norm(result, axis=-1, keepdims=True)
    return result / np.maximum(lengths, EPS)


def bone_matrix(head: Sequence[float], tail: Sequence[float], roll: float = 0) -> np.ndarray:
    q = qmul(qbetween([0, 1, 0], vector(tail) - vector(head)), axis_angle([0, 1, 0], roll))
    return compose(head, q)


def swing_twist(q: Sequence[float], axis=(0., 1., 0.)) -> tuple[np.ndarray, np.ndarray]:
    q, axis = quaternion(q), unit(axis)
    raw = np.r_[axis * np.dot(q[:3], axis), q[3]]
    twist = quaternion(raw) if np.linalg.norm(raw) > EPS else np.array(IDENTITY_Q)
    return quaternion(qmul(q, qinverse(twist))), twist


def signed_twist(q: Sequence[float], axis: Sequence[float]) -> float:
    q = quaternion(q)
    result = 2 * math.atan2(float(np.dot(q[:3], unit(axis))), q[3])
    return (result + math.pi) % (2 * math.pi) - math.pi
