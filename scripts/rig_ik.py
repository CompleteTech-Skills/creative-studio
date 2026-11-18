"""Analytical two-bone IK and bounded FABRIK, evaluated on actual FK chains."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import numpy as np
from rig_contracts import validate_contract
from rig_constraints import apply_constraints, constraint_order
from rig_errors import require
from rig_math import (decompose, qangle, qbetween, qinverse, qmatrix, qmul, quaternion, unit, vector)
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
    lengths = np.array([math.dist(a, b) for a, b in zip(points[:-1], points[1:])])
    with np.errstate(over='ignore', invalid='ignore'):
        finite = np.isfinite(lengths).all() and math.isfinite(float(lengths.sum()))
    require(finite and np.all(lengths > 1e-8), 'RIG_IK_LENGTH',
            'IK segments must have finite measurable lengths and a finite total.')
    return points, lengths


def _reachability(points, lengths, target):
    distance = math.dist(target, points[0])
    require(math.isfinite(distance), 'RIG_NONFINITE', 'IK target distance exceeds finite representation.')
    lower = max(0., float(lengths.max()-(lengths.sum()-lengths.max())))
    return distance, lower, bool(lower-1e-7 <= distance <= lengths.sum()+1e-7)


def chain_reachable(skeleton: dict, chain: dict, pose: dict, target) -> bool:
    """Measure the native chain range on the supplied, possibly final, pose."""
    points, lengths = _chain_points_and_lengths(skeleton, chain['bones'], pose)
    return _reachability(points, lengths, vector(target))[2]


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


def _orientation_barrier(skeleton: dict, constraints: list[dict], endpoint: str) -> int:
    """Split the validated order before any consumer of the endpoint pose."""
    # Follow the same parent/target edges validated by constraint_order().
    # Keep a contiguous suffix so every constraint still runs once, in order.
    dependents = {b['id']: [] for b in skeleton['bones']}
    for bone in skeleton['bones']:
        if bone['parent']:
            dependents[bone['parent']].append(bone['id'])
    for c in constraints:
        if c['type'] in ('copy-position', 'copy-rotation', 'aim') and 'target' in c:
            dependents[c['target']].append(c['bone'])
    affected, pending = set(), [endpoint]
    while pending:
        name = pending.pop()
        if name in affected:continue
        affected.add(name)
        pending.extend(dependents[name])
    return next((i for i, c in enumerate(constraints) if c['bone'] in affected),
                len(constraints))


def solve_ik(skeleton: dict, pose: dict, chain: dict, target, *, pole=None,
             constraints=(), orientation=None) -> IKResult:
    validate_chain(skeleton, chain)
    target = vector(target);pole = vector(pole if pole is not None else chain['pole'])
    result = deepcopy(pose)
    names = chain['bones']
    points, lengths = _chain_points_and_lengths(skeleton, names, result)
    origin = points[0].copy()
    distance, lower, reachable = _reachability(points, lengths, target)
    direction = (target-origin)/distance if distance > 1e-9 else unit(points[-1]-origin) if np.linalg.norm(points[-1]-origin) > 1e-9 else np.array([0., 1., 0.])
    constrained = constraint_order(skeleton, list(constraints))
    orientation = orientation if orientation is not None else chain.get('orientation')
    barrier = (_orientation_barrier(skeleton, constrained, names[-1])
               if orientation is not None else len(constrained))
    prefix, suffix = constrained[:barrier], constrained[barrier:]

    def apply_pass(current):
        applied = []
        if prefix:
            current, applied = apply_constraints(skeleton, current, prefix, ordered=True)
        if orientation is not None:
            # All endpoint ancestors/targets precede the barrier. Convert the
            # world goal using their finalized FK before limits and followers.
            world = evaluate_fk(skeleton, current)
            parent = next(b['parent'] for b in skeleton['bones'] if b['id'] == names[-1])
            parent_q = decompose(world[parent])['rotation'] if parent else [0, 0, 0, 1]
            current['bones'].setdefault(names[-1], {})['rotation'] = quaternion(
                qmul(qinverse(parent_q), quaternion(orientation))).tolist()
        if suffix:
            current, following = apply_constraints(skeleton, current, suffix, ordered=True)
            applied.extend(following)
        return current, applied

    changes, iterations = [], 0
    tolerance = chain['tolerance']
    limit = chain['iterations']
    pole_delta = pole-origin
    matched_fold = (distance <= 1e-9 and np.linalg.norm(points[-1]-origin) <= 1e-9
                    and np.linalg.norm(points[-1]-target) <= tolerance
                    and np.linalg.norm(pole_delta) > 1e-9
                    and np.linalg.norm(unit(pole_delta)-unit(points[1]-origin)) <= 8*np.finfo(float).eps)
    if matched_fold:
        # A closed chain has no endpoint axis from which to reconstruct its
        # interior. Keep its authoritative matched seed, then run the same
        # constraint/orientation pass and residual checks as every other solve.
        result, changes = apply_pass(result)
    elif (distance <= 1e-9 and np.linalg.norm(points[-1]-origin) <= 1e-9
          and np.linalg.norm(points[-1]-target) <= tolerance
          and np.linalg.norm(pole_delta) > 1e-9):
        # A closed seed can have no usable neighbouring-joint axes at all.
        # Reorient its existing shape toward the explicit pole before the
        # ordinary native orientation/constraint pass, retaining its lengths.
        rotation = qmatrix(qbetween(points[1]-origin, pole_delta))
        points = origin + (points-origin) @ rotation.T
        result = _orient_chain(skeleton, result, names, points)
        result, changes = apply_pass(result)
        iterations = 1
    elif (len(names) == 3 and distance <= 1e-9
          and abs(lengths[0]-lengths[1]) <= 1e-9):
        # The equal-length folded limit applies to either solver. The pole
        # names the elbow direction directly, without an invented world axis.
        bend = pole_delta if np.linalg.norm(pole_delta) > 1e-9 else points[1]-origin
        points[1] = origin + unit(bend)*lengths[0]
        points[2] = target
        result = _orient_chain(skeleton, result, names, points)
        result, changes = apply_pass(result)
        iterations = 1
    elif chain['solver'] == 'two-bone':
        d = float(np.clip(distance, max(lower, 1e-9), lengths.sum()))
        a, b = lengths
        # Factor the difference of squares and halve before adding. Every
        # intermediate stays bounded by a side on a feasible triangle, unlike
        # squaring large but representable segment lengths.
        x = d/2 + ((a-b)/d)*(a/2+b/2)
        ratio = x/a
        y = a*math.sqrt(max(0., (1-ratio)*(1+ratio)))
        points[1] = origin + direction*x + _pole_perpendicular(direction, origin, pole)*y
        points[2] = origin + direction*d
        result = _orient_chain(skeleton, result, names, points)
        result, changes = apply_pass(result)
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
                    radius = math.dist(delta, projection)
                    points[i] = points[i-1]+projection+_pole_perpendicular(axis, points[i-1], pole)*radius
            result = _orient_chain(skeleton, result, names, points)
            result, current = apply_pass(result)
            changes.extend(current)
            world = evaluate_fk(skeleton, result)
            points = np.array([world[name][:3, 3] for name in names])
            if math.dist(points[-1], target) <= tolerance or not reachable:break
    final = evaluate_fk(skeleton, result)[names[-1]]
    error = math.dist(final[:3, 3], target)
    orientation_error = (qangle(decompose(final)['rotation'], orientation)
                         if orientation is not None else None)
    require(math.isfinite(error) and (orientation_error is None or math.isfinite(orientation_error)),
            'RIG_NONFINITE', 'IK residual exceeds finite representation.')
    converged = reachable and error <= tolerance and (orientation_error is None or orientation_error <= 1e-5)
    return IKResult(result, error, bool(reachable), bool(converged), iterations, changes, orientation_error)


def match_fk_to_ik(skeleton: dict, pose: dict, chain: dict) -> dict:
    validate_chain(skeleton, chain)
    points, lengths = _chain_points_and_lengths(skeleton, chain['bones'], pose)
    world = evaluate_fk(skeleton, pose)
    reach = math.dist(points[-1], points[0])
    if reach <= 1e-9:
        # A valid fully folded chain has a measurable first segment even
        # though its endpoint coincides with its root.
        pole = points[1]
    else:
        bend = _pole_perpendicular(unit(points[-1]-points[0]), points[0], points[1])
        pole = points[0]+bend*reach
    return dict(target=points[-1].tolist(), pole=pole.tolist(),
                orientation=decompose(world[chain['bones'][-1]])['rotation'])
