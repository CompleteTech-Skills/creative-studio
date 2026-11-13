"""Analytical two-bone IK and bounded FABRIK, evaluated on actual FK chains."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import numpy as np
from rig_contracts import validate_contract
from rig_constraints import apply_constraints, constraint_order
from rig_errors import require
from rig_math import (decompose, qangle, qbetween, qinverse, qmul, quaternion, unit, vector)
from rig_skeleton import evaluate_fk


@dataclass
class IKResult:
    pose: dict
    residual_m: float
    reachable: bool
    converged: bool
    iterations: int
    constraints_applied: list[dict]
    orientation_residual_rad: float | None = None

    def report(self) -> dict:
        return dict(pose=self.pose, residual_m=self.residual_m, reachable=self.reachable,
                    converged=self.converged, iterations=self.iterations,
                    constraints_applied=self.constraints_applied,
                    orientation_residual_rad=self.orientation_residual_rad)


def _chain_points_and_lengths(skeleton: dict, names: list[str], pose=None):
    """Use the same evaluated segment boundary for rest and runtime poses."""
    world = evaluate_fk(skeleton, pose)
    points = np.array([world[name][:3, 3] for name in names])
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    require(np.all(lengths > 1e-8), 'RIG_IK_LENGTH', 'IK segments must have measurable length.')
    return points, lengths


def validate_chain(skeleton: dict, chain: dict) -> None:
    validate_contract(chain, 'IKChain')
    bones = {b['id']: b for b in skeleton['bones']}
    names = chain['bones']
    require(len(set(names)) == len(names) and all(n in bones for n in names), 'RIG_IK_CHAIN', 'IK references invalid bones.')
    require(all(bones[b]['parent'] == a for a, b in zip(names, names[1:])),
            'RIG_IK_CHAIN', 'IK chains must follow direct parent relationships.')
    require(chain['solver'] != 'two-bone' or len(names) == 3, 'RIG_IK_CHAIN', 'Two-bone IK needs two segments and an endpoint.')
    if 'orientation' in chain:
        quaternion(chain['orientation'], strict=True)
    _chain_points_and_lengths(skeleton, names)


def validate_controllers(skeleton: dict, controllers: dict) -> None:
    """Validate every declared controller, including chains unused by clips."""
    validate_contract(controllers, 'RigController')
    ids = set()
    for chain in controllers['ik']:
        require(chain['id'] not in ids, 'RIG_IK_ID', 'IK chain IDs must be unique.', asset=chain['id'])
        ids.add(chain['id'])
        validate_chain(skeleton, chain)
    constraint_order(skeleton, controllers['constraints'])


def _orient_chain(skeleton: dict, pose: dict, names: list[str], targets: np.ndarray) -> dict:
    result = deepcopy(pose)
    bones = {b['id']: b for b in skeleton['bones']}
    for i, name in enumerate(names[:-1]):
        world = evaluate_fk(skeleton, result)
        current = world[names[i+1]][:3, 3]-world[name][:3, 3]
        desired = targets[i+1]-targets[i]
        if np.linalg.norm(current) < 1e-9 or np.linalg.norm(desired) < 1e-9:continue
        world_q = qmul(qbetween(current, desired), decompose(world[name])['rotation'])
        parent = bones[name]['parent']
        parent_q = decompose(world[parent])['rotation'] if parent else [0, 0, 0, 1]
        result['bones'].setdefault(name, {})['rotation'] = quaternion(qmul(qinverse(parent_q), world_q)).tolist()
    return result


def _pole_perpendicular(direction, origin, pole):
    delta = pole-origin
    perpendicular = delta-direction*np.dot(delta, direction)
    if np.linalg.norm(perpendicular) < 1e-8:
        perpendicular = np.cross(direction, np.eye(3)[np.argmin(np.abs(direction))])
    return unit(perpendicular)


def solve_ik(skeleton: dict, pose: dict, chain: dict, target, *, pole=None,
             constraints=(), orientation=None) -> IKResult:
    validate_chain(skeleton, chain)
    target = vector(target);pole = vector(pole if pole is not None else chain['pole'])
    result = deepcopy(pose)
    names = chain['bones']
    points, lengths = _chain_points_and_lengths(skeleton, names, result)
    origin = points[0].copy()
    distance = float(np.linalg.norm(target-origin))
    lower = max(0., float(2*lengths.max()-lengths.sum()))
    reachable = lower-1e-7 <= distance <= lengths.sum()+1e-7
    direction = unit(target-origin) if distance > 1e-9 else unit(points[-1]-origin) if np.linalg.norm(points[-1]-origin) > 1e-9 else np.array([0., 1., 0.])
    constrained = constraint_order(skeleton, list(constraints))
    orientation = orientation if orientation is not None else chain.get('orientation')
    rotational = ('hinge', 'ball', 'swing-twist', 'locked', 'aim', 'copy-rotation')
    endpoint_constraints = ([c for c in constrained if c['bone'] == names[-1] and c['type'] in rotational]
                            if orientation is not None else [])
    # These constraints affect the endpoint's orientation, not the chain's
    # positional solve. Evaluate them once against its final requested pose.
    constrained = [c for c in constrained if c not in endpoint_constraints]
    changes, iterations = [], 0
    tolerance = chain['tolerance']
    limit = chain['iterations']
    pole_delta = pole-origin
    matched_fold = (distance <= 1e-9 and np.linalg.norm(points[-1]-target) <= tolerance
                    and np.linalg.norm(pole_delta) > 1e-9
                    and np.dot(unit(pole_delta), unit(points[1]-origin)) >= 1.-1e-12)
    if matched_fold:
        # A closed multisegment chain has no endpoint axis from which to
        # reconstruct its interior. Keep the authoritative matched seed when
        # its pole agrees; constraints and final orientation still run below.
        if constrained:result, changes = apply_constraints(skeleton, result, constrained, ordered=True)
    elif chain['solver'] == 'two-bone':
        a, b = lengths
        if distance <= 1e-9 and abs(a-b) <= 1e-9:
            # At a complete equal-length fold there is no root-to-end axis.
            # The pole directly names the elbow direction rather than a plane
            # perpendicular to an invented world axis.
            bend = pole-origin
            if np.linalg.norm(bend) <= 1e-9:bend = points[1]-origin
            points[1] = origin + unit(bend)*a
            points[2] = target
        else:
            d = float(np.clip(distance, max(lower, 1e-9), lengths.sum()))
            x = (d*d+a*a-b*b)/(2*d)
            y = math.sqrt(max(0., a*a-x*x))
            points[1] = origin + direction*x + _pole_perpendicular(direction, origin, pole)*y
            points[2] = origin + direction*d
        result = _orient_chain(skeleton, result, names, points)
        if constrained:result, changes = apply_constraints(skeleton, result, constrained, ordered=True)
        iterations = 1
    else:
        for iterations in range(1, limit+1):
            if distance >= lengths.sum():
                for i, length in enumerate(lengths):points[i+1] = points[i]+direction*length
            else:
                points[-1] = target
                for i in range(len(points)-2, -1, -1):
                    d = points[i]-points[i+1]
                    points[i] = points[i+1]+unit(d if np.linalg.norm(d) > 1e-10 else [1, 0, 0])*lengths[i]
                points[0] = origin
                for i, length in enumerate(lengths):
                    d = points[i+1]-points[i]
                    points[i+1] = points[i]+unit(d if np.linalg.norm(d) > 1e-10 else direction)*length
                # Pole rotates each intermediate point around its neighbour axis.
                for i in range(1, len(points)-1):
                    axis = points[i+1]-points[i-1]
                    if np.linalg.norm(axis) < 1e-8:continue
                    axis = unit(axis);delta = points[i]-points[i-1]
                    projection = axis*np.dot(delta, axis)
                    radius = np.linalg.norm(delta-projection)
                    points[i] = points[i-1]+projection+_pole_perpendicular(axis, points[i-1], pole)*radius
            result = _orient_chain(skeleton, result, names, points)
            if constrained:
                result, current = apply_constraints(skeleton, result, constrained, ordered=True);changes.extend(current)
            world = evaluate_fk(skeleton, result)
            points = np.array([world[name][:3, 3] for name in names])
            if np.linalg.norm(points[-1]-target) <= tolerance or not reachable:break
    if orientation is not None:
        world = evaluate_fk(skeleton, result)
        parent = next(b['parent'] for b in skeleton['bones'] if b['id'] == names[-1])
        parent_q = decompose(world[parent])['rotation'] if parent else [0, 0, 0, 1]
        result['bones'].setdefault(names[-1], {})['rotation'] = quaternion(qmul(qinverse(parent_q), quaternion(orientation))).tolist()
        # The endpoint orientation is a requested pose, not permission to bypass
        # its joint limits. Do not replay unrelated fractional constraints:
        # a second pass would change their authored influence.
        if endpoint_constraints:
            result, current = apply_constraints(skeleton, result, endpoint_constraints, ordered=True)
            changes.extend(current)
    final = evaluate_fk(skeleton, result)[names[-1]]
    error = float(np.linalg.norm(final[:3, 3]-target))
    orientation_error = (qangle(decompose(final)['rotation'], orientation)
                         if orientation is not None else None)
    converged = reachable and error <= tolerance and (orientation_error is None or orientation_error <= 1e-5)
    return IKResult(result, error, bool(reachable), bool(converged), iterations, changes, orientation_error)


def match_fk_to_ik(skeleton: dict, pose: dict, chain: dict) -> dict:
    validate_chain(skeleton, chain)
    world = evaluate_fk(skeleton, pose)
    points = [world[name][:3, 3] for name in chain['bones']]
    reach = float(np.linalg.norm(points[-1]-points[0]))
    if reach <= 1e-9:
        # A valid fully folded chain has a measurable first segment even
        # though its endpoint coincides with its root.
        bend = unit(points[1]-points[0])
    else:
        bend = _pole_perpendicular(unit(points[-1]-points[0]), points[0], points[1])
    pole_distance = max(reach, sum(float(np.linalg.norm(b-a)) for a,b in zip(points,points[1:])))
    return dict(target=points[-1].tolist(), pole=(points[0]+bend*pole_distance).tolist(),
                orientation=decompose(world[chain['bones'][-1]])['rotation'])
