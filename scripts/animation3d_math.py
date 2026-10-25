"""Animation mathematics built on the canonical rig_math authority.

No second coordinate/pose evaluator: matrices are row-major arrays acting on
column vectors, T @ R @ S, XYZW unit quaternions, meters, right-handed Y-up.
"""
import math
import numpy as np
from rig_errors import require
from rig_math import (compose, decompose, vector, quaternion, qmul, qinverse,
                      slerp, qmatrix, matrix_quaternion, BLENDER_FROM_CANONICAL, unit)


def _bounded(value):
    array=np.asarray(value,dtype=float)
    require(np.isfinite(array).all() and np.max(np.abs(array),initial=0)<=1e9,
            'ANIMATION_NUMERICAL','Value exceeds the finite numerical bound of 1e9.')
    return array


def inverse(matrix):
    m = _bounded(matrix)
    require(m.shape == (4, 4) and np.isfinite(m).all(), 'ANIMATION_MATRIX', 'Expected finite 4x4 matrix.')
    require(abs(np.linalg.det(m)) > 1e-12, 'ANIMATION_MATRIX', 'Singular matrix cannot be inverted.')
    result = np.linalg.inv(m)
    require(np.isfinite(result).all(), 'ANIMATION_MATRIX', 'Inverse is nonfinite.')
    return result


def local_to_world(local, parent):
    # Validate as TRS, including shear/reflection rejection, before composing.
    local,parent=_bounded(local),_bounded(parent)
    decompose(local); decompose(parent)
    result=_bounded(parent @ local)
    decompose(result)
    return result


def quaternion_log(value):
    q = quaternion(_bounded(value))
    # q and -q denote the same orientation; choose the short hemisphere.
    if q[3] < 0: q = -q
    length = np.linalg.norm(q[:3])
    return np.zeros(3) if length < 1e-12 else q[:3] * math.atan2(length, q[3]) / length


def quaternion_exp(value):
    v = vector(value)
    require(np.max(np.abs(v)) <= 1e9, 'ANIMATION_QUATERNION', 'Rotation vector exceeds numeric bounds.')
    angle = np.linalg.norm(v)
    return quaternion(np.r_[v * (math.sin(angle) / angle if angle > 1e-12 else 1), math.cos(angle)])


def euler_xyz(value):
    """Intrinsic XYZ rotation: Rz @ Ry @ Rx on column vectors."""
    from rig_math import axis_angle
    x, y, z = vector(_bounded(value))
    return quaternion(qmul(qmul(axis_angle([0, 0, 1], z), axis_angle([0, 1, 0], y)), axis_angle([1, 0, 0], x)))


def smoothstep(t):
    require(math.isfinite(t) and 0 <= t <= 1, 'ANIMATION_CURVE', 'Curve fraction must be in [0,1].')
    return t*t*(3-2*t)


def hermite(a, b, out_tangent, in_tangent, t, duration=1):
    smoothstep(t)
    require(math.isfinite(duration) and duration > 0, 'ANIMATION_CURVE', 'Tangent duration must be positive.')
    a,b,o,i = (_bounded(x) for x in (a,b,out_tangent,in_tangent))
    require(a.shape == b.shape == o.shape == i.shape and all(np.isfinite(x).all() for x in (a,b,o,i)),
            'ANIMATION_CURVE', 'Finite curve endpoints/tangents must have equal shapes.')
    require(duration <= 1e9 and all(np.max(np.abs(x), initial=0) <= 1e9 for x in (a,b,o,i)),
            'ANIMATION_CURVE', 'Curve exceeds numeric bounds.')
    result = (2*t**3-3*t*t+1)*a + (t**3-2*t*t+t)*duration*o + (-2*t**3+3*t*t)*b + (t**3-t*t)*duration*i
    require(np.isfinite(result).all(), 'ANIMATION_CURVE', 'Curve result is nonfinite.')
    return result


def bezier(a, b, c, d, t):
    smoothstep(t)
    values = [_bounded(v) for v in (a,b,c,d)]
    require(all(v.shape == values[0].shape and np.isfinite(v).all() for v in values),
            'ANIMATION_CURVE', 'Finite Bezier points must have equal shapes.')
    a,b,c,d = values
    return (1-t)**3*a + 3*(1-t)**2*t*b + 3*(1-t)*t*t*c + t**3*d


def catmull_rom(a, b, c, d, t):
    a,b,c,d = (_bounded(v) for v in (a,b,c,d))
    return hermite(b,c,(c-a)/2,(d-b)/2,t)


def arc_length(points):
    values = _bounded(points)
    require(values.ndim == 2 and values.shape[1] == 3 and len(values) >= 2 and np.isfinite(values).all(),
            'ANIMATION_CURVE', 'Arc length needs at least two finite 3D samples.')
    result = float(np.linalg.norm(np.diff(values, axis=0), axis=1).sum())
    require(math.isfinite(result), 'ANIMATION_CURVE', 'Arc length is nonfinite.')
    return result


def world_to_local(world, parent):
    world,parent=_bounded(world),_bounded(parent)
    decompose(world); decompose(parent)
    result=_bounded(inverse(parent) @ world)
    decompose(result)
    return result


def vector_add(a, b):
    return _bounded(vector(_bounded(a)) + vector(_bounded(b)))


def vector_subtract(a, b):
    return _bounded(vector(_bounded(a)) - vector(_bounded(b)))


def vector_lerp(a, b, t):
    require(math.isfinite(t) and 0 <= t <= 1, 'ANIMATION_CURVE', 'Blend fraction must be in [0,1].')
    return _bounded(vector(_bounded(a))*(1-t)+vector(_bounded(b))*t)
