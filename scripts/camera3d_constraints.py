"""Deterministic camera constraints using native scene targets and camera math.

All distances are meters; camera optical forward is local -Z. Solving has fixed
iteration/work limits and never consumes wall-clock or previous playback state.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import product
import math

import numpy as np

import camera3d_math as cm
from camera3d_contracts import canonical_bytes, validate
from rig_math import compose, matrix_quaternion, qmatrix, quaternion, slerp, vector

VERSION = 'camera3d-constraints/1'
MAX_TARGETS = 128
MAX_CONSTRAINTS = 128


class ConstraintError(ValueError):
    def __init__(self, message, *, code='CAMERA3D_CONSTRAINT', constraint=None, details=None):
        super().__init__(message)
        self.report = dict(code=code, message=message, constraint=constraint,
                           details={} if details is None else details)


def _number(value, name, low=-1e9, high=1e9):
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value)):
        raise ConstraintError(name + ' must be a finite number in the declared bounds.')
    return float(value)


def _world(value):
    try:
        return cm._matrix(value, rigid=True).copy()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ConstraintError('Camera world transform must be finite rigid TRS.') from exc


def _points(targets):
    if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ConstraintError('Framing needs between 1 and 128 resolved targets.')
    points = []
    for target in targets:
        bounds = target.get('bounds')
        if bounds is None:
            raise ConstraintError('Framing needs actual target bounds; a target origin is insufficient.',
                                  code='CAMERA3D_MISSING_BOUNDS')
        lo, hi = vector(bounds['minimum']), vector(bounds['maximum'])
        if np.any(lo > hi):
            raise ConstraintError('Target bounds are inverted.')
        points.extend(product(*zip(lo, hi)))
    return np.asarray(points)


def look_at_world(world, target, *, up=(0, 1, 0), roll_rad=0., weight=1.):
    """Aim local -Z at target, with deterministic least-aligned pole fallback."""
    world = _world(world)
    point, up = vector(target), vector(up)
    if np.linalg.norm(up) <= 1e-12:
        raise ConstraintError('Look-at up vector must be nonzero.')
    backward = world[:3, 3] - point
    if np.linalg.norm(backward) <= 1e-10:
        raise ConstraintError('Camera and look-at target coincide.', code='CAMERA3D_TARGET_COINCIDENT')
    backward /= np.linalg.norm(backward)
    up /= np.linalg.norm(up)
    fallback = abs(float(np.dot(up, backward))) > 1 - 1e-8
    if fallback:
        # Stable X,Y,Z tie order, independent of hash iteration or prior frames.
        up = np.eye(3)[int(np.argmin(np.abs(backward)))]
    right = np.cross(up, backward);right /= np.linalg.norm(right)
    desired = matrix_quaternion(np.column_stack((right, np.cross(backward, right), backward)))
    roll = _number(roll_rad, 'Roll', -math.tau * 1e6, math.tau * 1e6)
    if roll:
        from rig_math import axis_angle, qmul
        desired = quaternion(qmul(desired, axis_angle([0, 0, 1], roll)))
    amount = _number(weight, 'Look-at weight', 0, 1)
    world[:3, :3] = qmatrix(slerp(matrix_quaternion(world[:3, :3]), desired, amount))
    return world, dict(pole_fallback=fallback, up_axis=up.tolist())


def calculate_focus_distance(world, targets, *, weights=None, limits=(1e-6, 1e9)):
    """Weighted camera-axis depth, not Euclidean distance or world Z."""
    world = _world(world)
    if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ConstraintError('Focus requires 1..128 resolved targets.')
    if weights is None:
        weights = [1.] * len(targets)
    if not isinstance(weights, (list, tuple)) or len(weights) != len(targets):
        raise ConstraintError('Focus weight count differs from target count.')
    weights = np.array([_number(w, 'Focus weight', 0, 1e9) for w in weights])
    if not np.any(weights > 0):
        raise ConstraintError('At least one focus weight must be positive.')
    depths = np.array([np.dot(vector(t['position']) - world[:3, 3], -world[:3, 2]) for t in targets])
    if np.any(depths <= 0):
        raise ConstraintError('Focus target is on or behind the camera plane.', code='CAMERA3D_FOCUS_BEHIND')
    if len(limits) != 2:
        raise ConstraintError('Focus limits need minimum and maximum.')
    lo, hi = [_number(x, 'Focus limit', 1e-9, 1e9) for x in limits]
    if lo > hi:
        raise ConstraintError('Focus limits are inverted.')
    # Normalize first to avoid overflow in weighted sums.
    weights = weights / weights.sum()
    depth = float(weights @ depths)
    if not lo <= depth <= hi:
        raise ConstraintError('Required focus is outside configured limits.',
                              code='CAMERA3D_FOCUS_LIMIT', details=dict(required_m=depth, limits_m=[lo, hi]))
    return dict(distance_m=depth, target_depths_m=depths.tolist(), weights=weights.tolist(),
                plane_point=(world[:3, 3] - world[:3, 2] * depth).tolist(),
                plane_normal=(-world[:3, 2]).tolist())


def rack_focus(start_m, end_m, progress, *, curve='smoothstep'):
    start = _number(start_m, 'Start focus', 1e-9, 1e9)
    end = _number(end_m, 'End focus', 1e-9, 1e9)
    u = _number(progress, 'Rack focus progress', 0, 1)
    if curve not in ('linear', 'smoothstep'):
        raise ConstraintError('Unsupported rack focus curve.')
    if curve == 'smoothstep':
        u = u*u*(3 - 2*u)
    return start + (end - start) * u


def inspect_framing(world, optics, render, targets, *, safe_rect=(0, 0, 1, 1)):
    """Inspect every real bounds corner; clipped projections cannot hide cutoff."""
    world = _world(world);projection = cm.projection_matrix(optics, render)
    if len(safe_rect) != 4:
        raise ConstraintError('Safe rectangle requires left, top, right, bottom.')
    left, top, right, bottom = [_number(v, 'Safe rectangle', 0, 1) for v in safe_rect]
    if left >= right or top >= bottom:
        raise ConstraintError('Safe rectangle has no positive area.')
    points = _points(targets)
    inverse = np.linalg.inv(world)
    local = np.c_[points, np.ones(len(points))] @ inverse.T
    depths = -local[:, 2]
    if np.any(depths <= 1e-12):
        return dict(satisfied=False, reason='target_on_or_behind_camera',
                    minimum_depth_m=float(depths.min()), projected_rect=None, coverage=None)
    clip = local @ projection.T
    ndc = clip[:, :3] / clip[:, 3:4]
    normalized = np.c_[(ndc[:, 0]+1)/2, (1-ndc[:, 1])/2]
    rect = [float(normalized[:, 0].min()), float(normalized[:, 1].min()),
            float(normalized[:, 0].max()), float(normalized[:, 1].max())]
    violations = []
    for name, failed in [('left', rect[0] < left-1e-9), ('top', rect[1] < top-1e-9),
                         ('right', rect[2] > right+1e-9), ('bottom', rect[3] > bottom+1e-9),
                         ('near', ndc[:, 2].min() < -1-1e-9), ('far', ndc[:, 2].max() > 1+1e-9)]:
        if failed:
            violations.append(name)
    return dict(satisfied=not violations, reason=None if not violations else 'bounds_outside_safe_frustum',
                violations=violations, projected_rect=rect,
                coverage=max(0., rect[2]-rect[0])*max(0., rect[3]-rect[1]),
                minimum_depth_m=float(depths.min()), maximum_depth_m=float(depths.max()),
                safe_rect=[left, top, right, bottom])


def frame_subject(world, optics, render, targets, *, safe_rect=(.05, .05, .95, .95),
                  distance_range_m=(.01, 1e6), up=(0, 1, 0), anchor=None, weights=None):
    """Fit bounds by bounded axial dolly or true orthographic scale adjustment.

    The optical axis aims at the group's bounds center. All target corners must
    satisfy the requested safe rectangle and clipping planes. Impossible lens
    shifts, distance bounds, and clipping return an error rather than partial fit.
    """
    world = _world(world);optics = deepcopy(validate(optics, 'optics'));validate(render, 'render')
    points = _points(targets)
    center = (points.min(axis=0)+points.max(axis=0))/2
    if weights is not None:
        if not isinstance(weights,(list,tuple)) or len(weights)!=len(targets):
            raise ConstraintError('Framing weight count differs from targets.')
        weights=np.asarray([_number(w,'Framing weight',0,1e9) for w in weights])
        if not np.any(weights>0):
            raise ConstraintError('At least one framing weight must be positive.')
        centers=np.asarray([(vector(t['bounds']['minimum'])+vector(t['bounds']['maximum']))/2 for t in targets])
        center=(weights/weights.sum())@centers
    if anchor is None:
        anchor=((safe_rect[0]+safe_rect[2])/2,(safe_rect[1]+safe_rect[3])/2)
    if not isinstance(anchor,(list,tuple)) or len(anchor)!=2:
        raise ConstraintError('Composition anchor requires normalized X/Y.')
    anchor=[_number(v,'Composition anchor',0,1) for v in anchor]
    if len(distance_range_m) != 2:
        raise ConstraintError('Distance range needs minimum and maximum.')
    low, high = [_number(v, 'Distance range', 1e-9, 1e9) for v in distance_range_m]
    if low > high:
        raise ConstraintError('Distance range is inverted.')
    requested_distance=(low,high)
    original_distance = float(np.linalg.norm(world[:3, 3]-center))
    if original_distance <= 1e-10:
        world[:3, 3] = center + world[:3, 2]
    world, aim = look_at_world(world, center, up=up)
    backward = world[:3, 2].copy()
    relative = (points-center) @ world[:3, :3]
    projection = optics['projection']
    pm=cm.projection_matrix(optics,render)
    ndc=np.array([2*anchor[0]-1,1-2*anchor[1]])
    shift=np.array([2*projection['shift_x']/render['overscan'],2*projection['shift_y']/render['overscan']])
    offset=(shift-ndc)/np.array([pm[0,0],pm[1,1]])
    lateral=world[:3,0]*offset[0]+world[:3,1]*offset[1]
    if projection['type']=='perspective':
        factor=math.sqrt(1+float(np.dot(offset,offset)))
        low/=factor;high/=factor
    near, far = projection['near_clip_m'], projection['far_clip_m']
    low = max(low, float(relative[:, 2].max()) + near + 1e-7)
    high = min(high, float(relative[:, 2].min()) + far - 1e-7)
    if low > high:
        raise ConstraintError('Bounds cannot fit camera clipping/distance interval.', code='CAMERA3D_FRAME_IMPOSSIBLE')
    if projection['type'] == 'orthographic':
        distance = min(high, max(low, original_distance))
        world[:3, 3] = center + backward*distance + lateral
        low_scale, high_scale = 1e-6, 1e9
        optics['projection']['orthographic_height_m'] = high_scale
        pm=cm.projection_matrix(optics,render)
        offset=(shift-ndc)/np.array([pm[0,0],pm[1,1]])
        world[:3,3]=center+backward*distance+world[:3,0]*offset[0]+world[:3,1]*offset[1]
        if not inspect_framing(world, optics, render, targets, safe_rect=safe_rect)['satisfied']:
            raise ConstraintError('Orthographic bounds cannot fit safe rectangle.', code='CAMERA3D_FRAME_IMPOSSIBLE')
        for _ in range(56):
            mid = (low_scale+high_scale)/2
            optics['projection']['orthographic_height_m'] = mid
            pm=cm.projection_matrix(optics,render)
            offset=(shift-ndc)/np.array([pm[0,0],pm[1,1]])
            world[:3,3]=center+backward*distance+world[:3,0]*offset[0]+world[:3,1]*offset[1]
            if inspect_framing(world, optics, render, targets, safe_rect=safe_rect)['satisfied']:
                high_scale = mid
            else:
                low_scale = mid
        optics['projection']['orthographic_height_m'] = high_scale
        pm=cm.projection_matrix(optics,render)
        offset=(shift-ndc)/np.array([pm[0,0],pm[1,1]])
        world[:3,3]=center+backward*distance+world[:3,0]*offset[0]+world[:3,1]*offset[1]
    else:
        world[:3, 3] = center+(backward+lateral)*high
        if not inspect_framing(world, optics, render, targets, safe_rect=safe_rect)['satisfied']:
            raise ConstraintError('Perspective bounds cannot fit distance/safe rectangle.', code='CAMERA3D_FRAME_IMPOSSIBLE')
        for _ in range(56):
            mid = (low+high)/2
            world[:3, 3] = center+(backward+lateral)*mid
            if inspect_framing(world, optics, render, targets, safe_rect=safe_rect)['satisfied']:
                high = mid
            else:
                low = mid
        world[:3, 3] = center+(backward+lateral)*high
    report = inspect_framing(world, optics, render, targets, safe_rect=safe_rect)
    actual_distance=float(np.linalg.norm(world[:3,3]-center))
    if not requested_distance[0]-1e-7<=actual_distance<=requested_distance[1]+1e-7:
        raise ConstraintError('Composition requires a distance outside configured bounds.',code='CAMERA3D_FRAME_IMPOSSIBLE')
    if not report['satisfied']:
        raise ConstraintError('Framing failed final verification.', code='CAMERA3D_FRAME_IMPOSSIBLE', details=report)
    report.update(aim=aim, distance_m=float(np.linalg.norm(world[:3, 3]-center)), solver_iterations=56,composition_anchor=anchor)
    return dict(world_matrix=world.tolist(), optics=optics, framing=report)


def track_subject(world, target, *, offset_m=(0, 0, 0), follow_position=True,
                  follow_rotation=False, weight=1., fixed_height_m=None,
                  distance_range_m=None):
    """Stateless follow with an explicit offset and optional bounded distance."""
    world = _world(world)
    amount = _number(weight, 'Follow weight', 0, 1)
    point = vector(target['position']);offset = vector(offset_m)
    desired = point+offset
    if distance_range_m is not None:
        if len(distance_range_m) != 2:
            raise ConstraintError('Distance range needs minimum and maximum.')
        lo, hi = [_number(v, 'Follow distance', 0, 1e9) for v in distance_range_m]
        if lo > hi:
            raise ConstraintError('Follow distance range inverted.')
        distance = float(np.linalg.norm(offset))
        if distance <= 1e-12 and lo > 0:
            raise ConstraintError('A nonzero offset is needed for minimum distance.')
        if distance > 0:
            desired = point+offset/distance*min(hi, max(lo, distance))
    if follow_position:
        world[:3, 3] = world[:3, 3]*(1-amount)+desired*amount
    if fixed_height_m is not None:
        world[1, 3] = _number(fixed_height_m, 'Fixed height')
    if follow_rotation:
        # Scaled/sheared scene nodes still have valid positions. Rotation
        # following requires an unambiguous rigid basis and validates only then.
        target_world = cm._matrix(target['world_matrix'], rigid=True)
        world[:3, :3] = qmatrix(slerp(matrix_quaternion(world[:3, :3]),
                                     matrix_quaternion(target_world[:3, :3]), amount))
    return world


def evaluate_camera_constraints(world, optics, render, constraints, resolve_target, *, collision=None):
    """Evaluate existing camera3d constraint documents in (priority,id) order.

    Final framing/focus are verified after all modifications, so a later
    collision correction cannot silently invalidate an earlier framing result.
    Warning policy keeps explicit unsatisfied diagnostics; reject policy raises.
    """
    import hashlib
    world = _world(world);optics = deepcopy(validate(optics, 'optics'));validate(render, 'render')
    if not isinstance(constraints, (list, tuple)) or len(constraints) > MAX_CONSTRAINTS:
        raise ConstraintError('Camera constraint budget exceeded.')
    constraints = [validate(deepcopy(c), 'constraint') for c in constraints]
    if len({c['id'] for c in constraints}) != len(constraints):
        raise ConstraintError('Duplicate camera constraint IDs.')
    ordered = sorted((c for c in constraints if c['enabled']), key=lambda c: (c['priority'], c['id']))
    diagnostics=[];checks=[];resolved={}
    for c in ordered:
        if c['weight'] == 0:
            diagnostics.append(dict(id=c['id'], type=c['type'], status='disabled-by-zero-weight'))
            continue
        try:
            targets=[]
            for ref in c['targets']:
                key=canonical_bytes(ref)
                if key not in resolved:
                    resolved[key]=resolve_target(ref)
                targets.append(resolved[key])
            if c['type'] == 'look-at':
                center=np.mean([vector(t['position']) for t in targets],axis=0)
                world, result=look_at_world(world,center,up=c['up'],weight=c['weight'])
            elif c['type'] == 'horizon':
                world,result=look_at_world(world,world[:3,3]-world[:3,2],up=c['up'],weight=c['weight'])
            elif c['type'] == 'focus':
                result=calculate_focus_distance(world,targets,limits=c['distance_range_m'])
                optics['depth_of_field']['focus_distance_m']=(optics['depth_of_field']['focus_distance_m']*(1-c['weight'])+
                                                            result['distance_m']*c['weight'])
                checks.append((c,targets))
            elif c['type'] == 'frame':
                framed=frame_subject(world,optics,render,targets,safe_rect=c['safe_rect'],
                                     distance_range_m=c['distance_range_m'],up=c['up'])
                # Partial influence may not satisfy strict framing: final check reports it.
                wanted=np.asarray(framed['world_matrix'])
                world[:3,3]=world[:3,3]*(1-c['weight'])+wanted[:3,3]*c['weight']
                world[:3,:3]=qmatrix(slerp(matrix_quaternion(world[:3,:3]),matrix_quaternion(wanted[:3,:3]),c['weight']))
                if optics['projection']['type']=='orthographic':
                    old=optics['projection']['orthographic_height_m']
                    optics['projection']['orthographic_height_m']=old*(1-c['weight'])+framed['optics']['projection']['orthographic_height_m']*c['weight']
                result=framed['framing'];checks.append((c,targets))
            elif c['type'] == 'collision':
                if collision is None:
                    raise ConstraintError('Native scene collision owner was not supplied.',code='CAMERA3D_COLLISION_UNAVAILABLE')
                result=collision(world.copy(),c['collision_radius_m'])
                if not isinstance(result,dict) or result.get('clear') is not True:
                    raise ConstraintError('Camera intersects scene geometry.',code='CAMERA3D_COLLISION',details=result)
            diagnostics.append(dict(id=c['id'],type=c['type'],status='satisfied',details=result))
        except (ConstraintError,ValueError) as exc:
            report=getattr(exc,'report',dict(code='CAMERA3D_TARGET',message=str(exc)))
            diagnostics.append(dict(id=c['id'],type=c['type'],status='unsatisfied',details=report))
            if c['failure']=='reject':
                raise ConstraintError(str(exc),constraint=c['id'],details=report) from exc
    for c,targets in checks:
        try:
            if c['type']=='frame':
                result=inspect_framing(world,optics,render,targets,safe_rect=c['safe_rect'])
            else:
                expected=calculate_focus_distance(world,targets,limits=c['distance_range_m'])
                result=dict(satisfied=math.isclose(expected['distance_m'],optics['depth_of_field']['focus_distance_m'],rel_tol=1e-7,abs_tol=1e-7),
                            required_m=expected['distance_m'],actual_m=optics['depth_of_field']['focus_distance_m'])
        except (ConstraintError, ValueError) as exc:
            result=dict(satisfied=False, diagnostic=getattr(exc,'report',dict(message=str(exc))))
        if not result['satisfied']:
            diagnostics.append(dict(id=c['id'],type=c['type'],status='invalidated-by-later-constraint',details=result))
            if c['failure']=='reject':
                raise ConstraintError('Later constraint invalidates '+c['id'],code='CAMERA3D_CONSTRAINT_CONFLICT',constraint=c['id'],details=result)
    value=dict(world_matrix=world.tolist(),view_matrix=np.linalg.inv(world).tolist(),
               projection_matrix=cm.projection_matrix(optics,render).tolist(),optics=optics,
               targets=[resolved[k] for k in sorted(resolved)],diagnostics=diagnostics,evaluator=VERSION)
    value['sha256']=hashlib.sha256(canonical_bytes(value)).hexdigest()
    return value


def _bounds(value):
    if value is None:
        return None
    if not isinstance(value,dict) or set(value)!={'minimum','maximum'}:
        raise ConstraintError('Camera bounds require only minimum and maximum.')
    lo,hi=vector(value['minimum']),vector(value['maximum'])
    if np.any(lo>hi):
        raise ConstraintError('Camera bounds are inverted.')
    return lo,hi


def _sweep(queries,start,end,radius):
    """Validate the native spatial owner's swept-sphere result."""
    hit=queries.sweep_sphere(start.tolist(),end.tolist(),radius)
    if hit is None:
        return None
    if not isinstance(hit,dict):
        raise ConstraintError('Spatial owner returned an invalid sweep result.')
    fraction=_number(hit.get('fraction'),'Collision fraction',0,1)
    normal=vector(hit.get('normal'))
    if not math.isclose(float(np.linalg.norm(normal)),1.,rel_tol=1e-6,abs_tol=1e-6):
        raise ConstraintError('Spatial owner returned a non-unit collision normal.')
    return dict(fraction=fraction,normal=normal.tolist(),point=(start+(end-start)*fraction).tolist(),
                node=hit.get('node'),source=deepcopy(hit))


def resolve_camera_collision(start, end, radius, queries, *, policy='reject',
                             bounds=None, margin_m=1e-4, max_slide_steps=4):
    """Respond to native swept-sphere collisions, with bounded deterministic work.

    ``queries.sweep_sphere(start,end,radius)`` is the owning geometry query. It
    must detect initial overlap and return first time of impact or None. No
    sampling-only clearance test is substituted for a continuous sweep.
    """
    start,end=vector(start),vector(end)
    requested=end.copy()
    radius=_number(radius,'Camera collision radius',1e-9,1e6)
    margin=_number(margin_m,'Collision margin',1e-9,1.)
    if policy not in ('reject','warn','stop','slide','adjust-distance','nearby'):
        raise ConstraintError('Unsupported camera collision response policy.')
    if type(max_slide_steps) is not int or not 1<=max_slide_steps<=8:
        raise ConstraintError('Slide work limit must be an integer in 1..8.')
    bound=_bounds(bounds)
    if bound is not None:
        lo,hi=bound
        if np.any(lo+radius>hi-radius):
            raise ConstraintError('Camera radius cannot fit within scene bounds.')
        if np.any(start<lo+radius) or np.any(start>hi-radius):
            raise ConstraintError('Camera starts outside mandatory scene bounds.')
        if np.any(end<lo+radius) or np.any(end>hi-radius):
            # Mandatory bounds cannot be downgraded by warning policy.
            if policy not in ('stop','slide','adjust-distance','nearby'):
                raise ConstraintError('Camera path leaves mandatory scene bounds.',code='CAMERA3D_BOUNDS')
            delta=end-start;t=1.
            for axis in range(3):
                if delta[axis]>0:t=min(t,(hi[axis]-radius-start[axis])/delta[axis])
                if delta[axis]<0:t=min(t,(lo[axis]+radius-start[axis])/delta[axis])
            end=start+delta*max(0.,t)
    hit=_sweep(queries,start,end,radius)
    if hit is None:
        return dict(position=end.tolist(),clear=True,changed=not np.array_equal(end,requested),hits=[],policy=policy)
    if policy=='reject':
        raise ConstraintError('Camera swept body intersects native scene geometry.',code='CAMERA3D_COLLISION',details=hit)
    if policy=='warn':
        return dict(position=end.tolist(),clear=False,changed=False,hits=[hit],policy=policy)
    delta=end-start;length=float(np.linalg.norm(delta))
    if hit['fraction']<=0:
        # Initial overlap cannot safely be repaired with a discontinuous jump.
        raise ConstraintError('Camera starts in native scene geometry.',code='CAMERA3D_INITIAL_OVERLAP',details=hit)
    fraction=max(0.,hit['fraction']-margin/max(length,margin))
    stopped=start+delta*fraction
    if policy in ('stop','adjust-distance'):
        if _sweep(queries,start,stopped,radius) is not None:
            raise ConstraintError('Collision stop failed independent swept verification.')
        return dict(position=stopped.tolist(),clear=True,changed=True,hits=[hit],policy=policy)
    hits=[hit]
    if policy=='nearby':
        # Fixed six-axis candidates; never teleport across an obstacle.
        candidates=[]
        for axis in range(3):
            for sign in (-1,1):
                candidate=stopped+np.eye(3)[axis]*sign*(radius*2+margin)
                if bound is not None and (np.any(candidate<bound[0]+radius) or np.any(candidate>bound[1]-radius)):
                    continue
                if _sweep(queries,start,candidate,radius) is None:
                    candidates.append((float(np.linalg.norm(candidate-end)),axis,sign,candidate))
        if not candidates:
            return dict(position=stopped.tolist(),clear=True,changed=True,hits=hits,policy=policy,avoidance='stopped')
        chosen=min(candidates,key=lambda c:(c[0],c[1],c[2]))[3]
        return dict(position=chosen.tolist(),clear=True,changed=True,hits=hits,policy=policy,avoidance='nearby-swept')
    current=stopped
    remaining=end-current
    normal=np.asarray(hit['normal'])
    for _ in range(max_slide_steps):
        # Remove only the inward component, retaining tangential motion.
        remaining=remaining-min(0.,float(np.dot(remaining,normal)))*normal
        if np.linalg.norm(remaining)<=margin:
            break
        desired=current+remaining
        if bound is not None:
            desired=np.minimum(bound[1]-radius,np.maximum(bound[0]+radius,desired))
        next_hit=_sweep(queries,current,desired,radius)
        if next_hit is None:
            current=desired;break
        hits.append(next_hit)
        if next_hit['fraction']<=0:
            break
        step=desired-current;distance=float(np.linalg.norm(step))
        current=current+step*max(0.,next_hit['fraction']-margin/max(distance,margin))
        remaining=desired-current;normal=np.asarray(next_hit['normal'])
    # Verify all traversed segments during solving; final zero sweep verifies resting clearance.
    if _sweep(queries,current,current,radius) is not None:
        raise ConstraintError('Slide result remains in collision.')
    return dict(position=current.tolist(),clear=True,changed=True,hits=hits,policy=policy)


def preflight_camera_path(positions, radius, queries, *, bounds=None):
    """Continuous swept checks on each declared linear path segment."""
    if not isinstance(positions,(list,tuple)) or not 2<=len(positions)<=4096:
        raise ConstraintError('Preflight requires 2..4096 positions.')
    points=[vector(p) for p in positions]
    results=[]
    for index,(start,end) in enumerate(zip(points,points[1:])):
        try:
            resolve_camera_collision(start,end,radius,queries,bounds=bounds,policy='reject')
        except ConstraintError as exc:
            results.append(dict(segment=index,diagnostic=exc.report))
    return dict(clear=not results,segments=len(points)-1,collisions=results,
                interpolation='piecewise-linear',curved_path_claim=False)
