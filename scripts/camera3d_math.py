"""Reference camera math using authoritative rig quaternion and basis utilities.

Column vectors; parent@local; right-handed Y-up world; local -Z view, +Y up.
NDC depth is OpenGL/glTF [-1,+1]; raster coordinates use top-left pixel edges.
These functions do not perform rendering or claim browser/Blender parity.
"""
from __future__ import annotations

import math


def _numeric():
    import numpy as np
    import rig_math
    return np, rig_math


def _matrix(value, *, rigid=False):
    np, _ = _numeric()
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4,4) or not np.isfinite(matrix).all():
        raise ValueError('camera3d requires finite 4x4 matrix')
    if rigid and (not np.allclose(matrix[3],[0,0,0,1],atol=1e-9)
            or not np.allclose(matrix[:3,:3].T@matrix[:3,:3],np.eye(3),atol=1e-8)
            or not math.isclose(float(np.linalg.det(matrix[:3,:3])),1,abs_tol=1e-8)):
        raise ValueError('camera3d world matrix must be a rigid proper affine transform')
    sign, logdet = np.linalg.slogdet(matrix)
    if sign == 0 or not math.isfinite(float(logdet)):
        raise ValueError('camera3d matrix is singular')
    return matrix


def world_transform(transform, parent=None):
    np, rig = _numeric()
    if set(transform) != {'translation','rotation','scale'} or list(transform['scale']) != [1,1,1]:
        raise ValueError('camera3d camera transforms require unit scale')
    rig.quaternion(transform['rotation'],strict=True)
    local = rig.transform_matrix(transform)
    return _matrix((np.eye(4) if parent is None else _matrix(parent,rigid=True))@local,rigid=True)


def basis_transform(matrix, destination='blender'):
    """Convert world and camera-local bases by conjugation; glTF is identity."""
    np, rig = _numeric()
    matrix = _matrix(matrix,rigid=True)
    if destination == 'gltf':
        return matrix.copy()
    if destination not in ('blender','canonical-from-blender'):
        raise ValueError('camera3d unsupported basis')
    basis = rig.BLENDER_FROM_CANONICAL
    if destination == 'canonical-from-blender':
        basis = basis.T
    return basis@matrix@basis.T


def blender_camera_transform(matrix):
    """Convert world basis only: Blender camera-local -Z/+Y already matches ours."""
    _, rig = _numeric()
    return rig.BLENDER_FROM_CANONICAL@_matrix(matrix,rigid=True)


def canonical_camera_transform(matrix):
    _, rig = _numeric()
    return rig.BLENDER_FROM_CANONICAL.T@_matrix(matrix,rigid=True)


def look_at(position, target, up=(0,1,0)):
    np, rig = _numeric()
    position, target, up = rig.vector(position),rig.vector(target),rig.unit(up)
    backward = rig.unit(position-target)
    right = np.cross(up,backward)
    if np.linalg.norm(right) < 1e-8:
        raise ValueError('camera3d target direction is parallel to up')
    right = rig.unit(right)
    rotation = np.column_stack((right,np.cross(backward,right),backward))
    return rig.matrix_quaternion(rotation)


def projection_matrix(optics, render):
    """Pure pinhole/orthographic reference, excluding lens distortion and DOF.

    shift_x/y locate the principal point relative to image center, in fractions
    of full width/height (+X right,+Y up). Adapter optical sign conversion must
    be explicit. Overscan expands the frustum; nominal width/height stay fixed.
    """
    np, _ = _numeric()
    from camera3d_contracts import validate
    validate(optics,'optics');validate(render,'render')
    p = optics['projection']
    near,far = p['near_clip_m'],p['far_clip_m']
    aspect = render['width']*render['pixel_aspect']['numerator']/(render['height']*render['pixel_aspect']['denominator'])
    overscan = render['overscan']
    result = np.zeros((4,4))
    if p['type'] == 'perspective':
        width,height = p['sensor_width_mm'],p['sensor_height_mm']
        fit = p['sensor_fit']
        if fit == 'horizontal':
            height = width/aspect
        elif fit == 'vertical':
            width = height*aspect
        elif render['gate_fit'] == 'crop':
            width = min(width,height*aspect);height=width/aspect
        else:
            width = max(width,height*aspect);height=width/aspect
        result[0,0]=2*p['focal_length_mm']/(width*overscan)
        result[1,1]=2*p['focal_length_mm']/(height*overscan)
        result[0,2]=-2*p['shift_x']/overscan
        result[1,2]=-2*p['shift_y']/overscan
        result[2,2]=-(far+near)/(far-near)
        result[2,3]=-2*far*near/(far-near)
        result[3,2]=-1
    else:
        height=p['orthographic_height_m']*overscan;width=height*aspect
        result[0,0]=2/width;result[1,1]=2/height
        result[0,3]=2*p['shift_x']/overscan;result[1,3]=2*p['shift_y']/overscan
        result[2,2]=-2/(far-near);result[2,3]=-(far+near)/(far-near)
        result[3,3]=1
    return _matrix(result)


def _raster_size(width, height):
    values=[]
    for value in (width,height):
        if (isinstance(value,bool) or not isinstance(value,(int,float))
                or not 1<=value<=16384 or int(value)!=value):
            raise ValueError('camera3d raster dimensions require exact integers in 1..16384')
        values.append(int(value))
    return values


def project(point, world, projection, width, height):
    np, rig = _numeric()
    width,height=_raster_size(width,height)
    camera=np.linalg.inv(_matrix(world,rigid=True))@np.r_[rig.vector(point),1.]
    clip=_matrix(projection)@camera
    if abs(float(clip[3])) < 1e-12:
        raise ValueError('camera3d projective division is undefined')
    ndc=clip[:3]/clip[3]
    return {'pixel':[float((ndc[0]+1)*width/2),float((1-ndc[1])*height/2)],
            'ndc':ndc.tolist(),'depth_m':float(-camera[2]),
            'inside':bool(camera[2]<0 and np.all(ndc>=-1) and np.all(ndc<=1))}


def unproject(pixel, depth_ndc, world, projection, width, height):
    np, _ = _numeric()
    width,height=_raster_size(width,height)
    if (len(pixel)!=2 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in (*pixel,depth_ndc))):
        raise ValueError('camera3d requires finite raster point and positive dimensions')
    clip=np.array([2*pixel[0]/width-1,1-2*pixel[1]/height,depth_ndc,1.])
    point=_matrix(world,rigid=True)@np.linalg.inv(_matrix(projection))@clip
    if abs(float(point[3]))<1e-12:
        raise ValueError('camera3d unprojection at infinity')
    return point[:3]/point[3]


def screen_ray(pixel, world, projection, width, height):
    _,rig=_numeric()
    near=unproject(pixel,-1,world,projection,width,height)
    far=unproject(pixel,1,world,projection,width,height)
    return near,rig.unit(far-near)


def validate_evaluated_matrices(value):
    np,_=_numeric()
    world=_matrix(value['world_matrix'],rigid=True)
    view=_matrix(value['view_matrix'],rigid=True)
    _matrix(value['projection_matrix'])
    if not np.allclose(view@world,np.eye(4),atol=1e-8):
        raise ValueError('camera3d view matrix must invert world matrix')


def add(left, right):
    _,rig=_numeric()
    return rig.vector(left)+rig.vector(right)


def subtract(left, right):
    _,rig=_numeric()
    return rig.vector(left)-rig.vector(right)


def dot(left, right):
    np,rig=_numeric()
    return float(np.dot(rig.vector(left),rig.vector(right)))


def cross(left, right):
    np,rig=_numeric()
    return np.cross(rig.vector(left),rig.vector(right))


def distance(left, right):
    np,_=_numeric()
    return float(np.linalg.norm(subtract(left,right)))


def angle(left, right):
    np,rig=_numeric()
    return math.acos(float(np.clip(np.dot(rig.unit(left),rig.unit(right)),-1,1)))


def normalize(value):
    _,rig=_numeric()
    return rig.unit(value)


def quaternion_normalize(value):
    _,rig=_numeric()
    return rig.quaternion(value)


def quaternion_multiply(left, right):
    _,rig=_numeric()
    return rig.quaternion(rig.qmul(rig.quaternion(left),rig.quaternion(right)))


def quaternion_inverse(value):
    _,rig=_numeric()
    return rig.qinverse(value)


def quaternion_slerp(left, right, weight):
    _,rig=_numeric()
    if isinstance(weight,bool) or not isinstance(weight,(int,float)) or not math.isfinite(weight) or not 0<=weight<=1:
        raise ValueError('camera3d SLERP weight must be in [0,1]')
    return rig.slerp(left,right,weight)


def quaternion_matrix(value):
    _,rig=_numeric()
    return rig.qmatrix(value)


def euler_xyz(angles_rad):
    """Fixed-axis/extrinsic XYZ radians, R = Rz @ Ry @ Rx (not intrinsic XYZ)."""
    _,rig=_numeric()
    x,y,z=rig.vector(angles_rad)
    return quaternion_multiply(rig.axis_angle([0,0,1],z),
        quaternion_multiply(rig.axis_angle([0,1,0],y),rig.axis_angle([1,0,0],x)))


def local_to_world(point, world):
    _,rig=_numeric()
    return rig.transform_points(_matrix(world,rigid=True),rig.vector(point))


def world_to_camera(point, world):
    np,_=_numeric()
    return local_to_world(point,np.linalg.inv(_matrix(world,rigid=True)))


def camera_to_world(point, world):
    return local_to_world(point,world)


def target_offset(point, offset, transform=None):
    """World offset by default; an explicit rigid node transform rotates local offsets."""
    _,rig=_numeric()
    offset=rig.vector(offset)
    if transform is not None:
        offset=_matrix(transform,rigid=True)[:3,:3]@offset
    return rig.vector(point)+offset


def ray_plane_intersection(origin, direction, plane_point, plane_normal, *, tolerance=1e-9):
    """Return a forward-ray intersection or None for parallel/behind planes."""
    np,rig=_numeric()
    if not math.isfinite(tolerance) or tolerance<=0:
        raise ValueError('camera3d tolerance must be finite and positive')
    origin,direction=rig.vector(origin),rig.unit(direction)
    point,normal=rig.vector(plane_point),rig.unit(plane_normal)
    denominator=float(np.dot(normal,direction))
    if abs(denominator)<=tolerance:
        return None
    travel=float(np.dot(point-origin,normal))/denominator
    if travel < -tolerance:
        return None
    return origin+max(0.,travel)*direction


def frustum_corners(world, projection):
    """Near TL/TR/BR/BL then far TL/TR/BR/BL, in world meters."""
    np,_=_numeric()
    return np.asarray([unproject(pixel,depth,world,projection,2,2)
        for depth in (-1,1) for pixel in ([0,0],[2,0],[2,2],[0,2])])


def frustum_planes(world, projection):
    """Unit inward world planes [nx,ny,nz,d]: left/right/bottom/top/near/far."""
    np,_=_numeric()
    combined=_matrix(projection)@np.linalg.inv(_matrix(world,rigid=True))
    planes=np.asarray([combined[3]+sign*combined[axis]
                       for axis in (0,1,2) for sign in (1,-1)])
    lengths=np.linalg.norm(planes[:,:3],axis=1)
    if np.any(lengths<1e-12):
        raise ValueError('camera3d degenerate frustum plane')
    return planes/lengths[:,None]


def frustum_contains(point, world, projection, *, tolerance=1e-8):
    np,rig=_numeric()
    if not math.isfinite(tolerance) or tolerance<0:
        raise ValueError('camera3d tolerance must be finite and nonnegative')
    return bool(np.all(frustum_planes(world,projection)@np.r_[rig.vector(point),1.]>=-tolerance))


def project_bounds(minimum, maximum, world, projection, width, height):
    """Screen rectangle of the actual AABB/frustum intersection, or None.

    Intersect the twelve convex planes. Every intersection-polyhedron vertex is
    the intersection of three boundary planes; fixed 220 combinations keep the
    reference bounded and correctly handle near-plane crossings/camera enclosure.
    This is mathematical bounding geometry, not rendered occlusion visibility.
    """
    from itertools import combinations
    np,rig=_numeric()
    width,height=_raster_size(width,height)
    minimum,maximum=rig.vector(minimum),rig.vector(maximum)
    if np.any(minimum>maximum):
        raise ValueError('camera3d bounds are inverted')
    box=[]
    for axis in range(3):
        normal=np.zeros(3);normal[axis]=1
        box.extend([np.r_[normal,-minimum[axis]],np.r_[-normal,maximum[axis]]])
    planes=np.vstack([frustum_planes(world,projection),box])
    candidates=[]
    for indices in combinations(range(12),3):
        subset=planes[list(indices)]
        try:
            point=np.linalg.solve(subset[:,:3],-subset[:,3])
        except np.linalg.LinAlgError:
            continue
        if np.isfinite(point).all() and np.all(planes@np.r_[point,1.]>=-1e-8):
            candidates.append(project(point,world,projection,width,height)['pixel'])
    if not candidates:
        return None
    points=np.asarray(candidates)
    return np.r_[points.min(axis=0),points.max(axis=0)].tolist()
