"""Analytical two-bone IK and bounded FABRIK, evaluated on actual FK chains."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from itertools import permutations, product
import math
import numpy as np
from rig_contracts import validate_contract
from rig_constraints import apply_constraints, constraint_order
from rig_errors import RigError, require
from rig_math import (compose, decompose, matrix_quaternion, qangle, qbetween, qinverse, qmatrix,
                      qmul, quaternion, unit, vector)
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


def _fold_ray_angles(constant, cosine, sine, bounds):
    """Finite circle/ray-box boundary intersections and their intervening arcs."""
    angles = {0.}
    lower, upper = bounds
    values = (lower, (lower + upper) / 2, upper)
    parts = np.array([constant, cosine, sine])
    for i, j in ((0, 1), (0, 2), (1, 2)):
        for first, second in product(values, repeat=2):
            a, b, c = parts[:, i] * second[j] - parts[:, j] * first[i]
            radius = math.hypot(b, c)
            if radius and abs(a) <= radius * (1 + 32 * np.finfo(float).eps):
                center = math.atan2(c, b)
                offset = math.acos(float(np.clip(-a / radius, -1, 1)))
                angles.update(((center - offset) % math.tau, (center + offset) % math.tau))
    ordered = sorted(angles)
    yield from ordered
    for first, second in zip(ordered, [*ordered[1:], ordered[0] + math.tau]):
        yield (first + second) / 2


def _fold_parent_axis_turns(parent_axes, seed, source, target, direction_tolerance, bounds):
    """Lift rotations that commute with the parent's stretch to the seed TRS."""
    left, scales, right_t = parent_axes
    # The orthogonal complement of a repeated pair is a distinguished axis.
    # A proper rotation can preserve or reverse that axis. Both map the
    # transverse part of the seed direction onto that of the pole.
    axis_index = 0 if scales[0] - scales[1] > scales[1] - scales[2] else 2
    transverse = np.delete(scales, axis_index)
    axis = np.eye(3)[axis_index]

    def frame(direction):
        radial = left.T @ direction
        radial[axis_index] = 0.
        length = math.hypot(*radial)
        radial = radial / length if length else np.eye(3)[(axis_index + 1) % 3]
        return np.column_stack((axis, radial, np.cross(axis, radial)))

    if abs(transverse[0] - transverse[1]) <= 32 * np.finfo(float).eps * scales[0]:
        before, after = frame(source), frame(target)
        def axial_symmetries():
            for sign in (1., -1.):
                yield (after * [sign, 1., sign]) @ before.T
                axial = float(axis @ (left.T @ source))
                radial = math.hypot(*(source - left @ axis * axial))
                constant = left @ axis * (sign * axial)
                cosine, sine = (left @ before[:, i] * radial for i in (1, 2))
                # Coordinate intervals can have very different precision.
                # Solve their ray boundaries instead of choosing only the
                # azimuth of the rounded central pole position.
                for angle in _fold_ray_angles(constant, cosine, sine, bounds):
                    c, s = math.cos(angle), math.sin(angle)
                    frame_turn = np.array([[1., 0., 0.], [0., c, -s], [0., s, c]])
                    yield ((before @ frame_turn) * [sign, 1., sign]) @ before.T
        symmetries = axial_symmetries()
    else:
        # Distinct singular values permit half turns about each principal axis.
        symmetries = [np.diag(2 * basis - 1) for basis in np.eye(3)]
    for symmetry in symmetries:
        # Conjugating by the right singular basis gives a local rotation
        # without an inverse scale magnifying errors in the world matrices.
        world_turn = left @ symmetry @ left.T
        if np.max(np.abs(world_turn @ source - target)) <= direction_tolerance:
            turn = right_t.T @ symmetry @ right_t
            rotation = quaternion(qmul(matrix_quaternion(turn), seed['rotation']))
            yield dict(rotation=rotation.tolist(), scale=seed['scale'])


def _fold_axis_permutations(root, seed, source):
    """Permute local axes when their world directions retain all pairwise angles."""
    lengths = np.linalg.norm(root, axis=0)
    axes = root / lengths
    gram = axes.T @ axes
    coordinates = np.linalg.solve(axes, source)
    scales = np.asarray(seed['scale'])
    for order in permutations(range(3)):
        indices = list(order)
        for signs in product((1., -1.), repeat=3):
            permutation = np.eye(3)[:, indices] * signs
            if np.array_equal(permutation, np.eye(3)) or np.linalg.det(permutation) < 0:
                continue
            if np.max(np.abs(permutation.T @ gram @ permutation - gram)) > 32 * np.finfo(float).eps:
                continue
            # Match the original world column lengths with local scales. The
            # resulting rigid turn can still be followed by a parent symmetry.
            rotation = quaternion(qmul(seed['rotation'], matrix_quaternion(permutation)))
            reoriented = dict(rotation=rotation.tolist(),
                              scale=(scales[indices] * (lengths / lengths[indices])).tolist())
            yield reoriented, axes @ (permutation @ coordinates)


def _fold_symmetry_candidates(parent_axes, root, seed, source, direction, direction_tolerance, bounds):
    yield from _fold_parent_axis_turns(parent_axes, seed, source, direction, direction_tolerance, bounds)
    for permuted, axis in _fold_axis_permutations(root, seed, source):
        if np.max(np.abs(axis - direction)) <= direction_tolerance:
            yield permuted
        yield from _fold_parent_axis_turns(parent_axes, permuted, axis, direction, direction_tolerance, bounds)


def _fold_root_transform(parent, root, seed, source, target, target_resolution):
    """Choose a rigid pole turn that is representable as local TRS.

    Pole direction fixes the swing, but leaves a twist about that direction.
    Under a scaled parent the shortest swing can require local shear even when
    another twist preserves the entire closed seed with an ordinary local TRS.
    Orthogonality of each pair of local columns is a degree-two trigonometric
    polynomial in that twist. Its half-angle substitution is a quartic, so
    checking the three finite root sets is bounded and needs no pose iteration.
    """
    # Normalize bounded vectors before the native quaternion helper squares
    # their components. Pole distance must not change a representable ray.
    target_scale = float(np.max(np.abs(target)))
    target, target_resolution = target / target_scale, target_resolution / target_scale
    source, direction = unit(source / np.max(np.abs(source))), unit(target)
    swing = qmatrix(qbetween(source, direction))
    target_length = math.hypot(*target)
    identity = np.eye(3)
    # Cancel root column magnitudes before solving for the world turn. Local
    # bone scales can span many orders even when the parent is well-conditioned.
    column_scales = np.max(np.abs(root), axis=0)
    require(np.all(column_scales > 0), 'RIG_SINGULAR_TRANSFORM',
            'Closed-chain pole steering needs an invertible root transform.')
    scaled_root = root / column_scales
    # The measured turn uses a linear solve. Allow its conditioning to amplify
    # arithmetic roundoff, but cap the error even for poorly resolved frames.
    eps = np.finfo(float).eps
    parent_axes = np.linalg.svd(parent)
    stretches = parent_axes[1]
    require(stretches[-1] > 0, 'RIG_SINGULAR_TRANSFORM',
            'Closed-chain pole steering needs an invertible parent transform.')
    root_condition = np.linalg.cond(scaled_root)
    conditioning = max(stretches[0] / stretches[-1], root_condition)
    roundoff = min(16 * conditioning, 65536) * eps
    # This scalar only filters candidate generators. Final pole admission uses
    # the independently resolved coordinate intervals below, so a large X
    # origin cannot conceal a well-resolved mismatch in Y or Z.
    direction_tolerance = roundoff + 2 * math.hypot(*target_resolution) / target_length
    extent = target_resolution / target_length + roundoff
    bounds = (direction - extent, direction + extent)

    def pole_matches(axis):
        # A pole specifies a ray, not an elbow distance. Intersect the positive
        # ray with each coordinate's rounding interval without letting a far
        # pole's spacing inherit the first segment's much shorter length.
        lower, upper = 0., math.inf
        for component, observed, spacing in zip(axis, target, target_resolution):
            error = spacing + roundoff * target_length
            if component == 0:
                if abs(observed) > error:
                    return False
                continue
            endpoints = ((observed - error) / component, (observed + error) / component)
            lower, upper = max(lower, min(endpoints)), min(upper, max(endpoints))
            if lower > upper:
                return False
        return upper > 0

    def pole_error(oriented):
        try:
            local_frame = compose(rotation=oriented['rotation'], scale=oriented['scale'])[:3, :3]
        except RigError as exc:
            if exc.report['code'] == 'RIG_SINGULAR_TRANSFORM':
                return math.inf
            raise
        world = parent @ local_frame
        turn = np.linalg.solve(scaled_root.T, (world / column_scales).T).T
        if np.linalg.det(turn) > 0 and np.max(np.abs(turn.T @ turn - identity)) <= roundoff:
            axis = turn @ source
            if pole_matches(axis):
                return np.max(np.abs(axis - direction))
        return math.inf

    def try_world_turn(rotation):
        value = np.eye(4)
        try:
            value[:3, :3] = np.linalg.solve(parent, rotation @ root)
            oriented = decompose(value)
        except RigError as exc:
            if exc.report['code'] in ('RIG_SHEAR', 'RIG_SINGULAR_TRANSFORM'):
                return None
            raise
        columns = value[:3, :3] / oriented['scale']
        if np.max(np.abs(columns.T @ columns - identity)) > 128 * eps:
            return None
        return oriented if math.isfinite(pole_error(oriented)) else None

    oriented = try_world_turn(swing)
    if oriented is not None:
        return oriented
    # Different sign choices can represent the same pole with different
    # rounding after FK. Prefer a well-resolved fit whenever one is available.
    closest, best_error = None, math.inf
    for oriented in _fold_symmetry_candidates(parent_axes, root, seed, source, direction, direction_tolerance, bounds):
        error = pole_error(oriented)
        if error <= 16 * eps:
            return oriented
        if math.isfinite(error) and error <= best_error:
            closest, best_error = oriented, error
    if closest is not None:
        return closest

    parallel = np.outer(direction, direction)
    perpendicular = np.eye(3) - parallel
    x, y, z = direction
    cross = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    constant, cosine, sine = (np.linalg.solve(parent, part @ swing @ root)
                              for part in (parallel, perpendicular, cross))
    # Uniform rescaling leaves all zero dot-product conditions unchanged and
    # avoids overflow while forming polynomial coefficients.
    bound = max(float(np.max(np.abs(part))) for part in (constant, cosine, sine))
    require(math.isfinite(bound) and bound > 0, 'RIG_SHEAR',
            'Cannot resolve a rigid closed-chain pole turn.')
    constant, cosine, sine = (part / bound for part in (constant, cosine, sine))
    candidates = [math.pi]  # The half-angle substitution omits this point.
    for i, j in ((0, 1), (0, 2), (1, 2)):
        c0 = constant[:, i] @ constant[:, j] + .5 * (
            cosine[:, i] @ cosine[:, j] + sine[:, i] @ sine[:, j])
        c1 = constant[:, i] @ cosine[:, j] + cosine[:, i] @ constant[:, j]
        s1 = constant[:, i] @ sine[:, j] + sine[:, i] @ constant[:, j]
        c2 = .5 * (cosine[:, i] @ cosine[:, j] - sine[:, i] @ sine[:, j])
        s2 = .5 * (cosine[:, i] @ sine[:, j] + sine[:, i] @ cosine[:, j])
        coefficients = np.array([c0-c1+c2, 2*s1-4*s2, 2*c0-6*c2, 2*s1+4*s2, c0+c1+c2])
        magnitude = float(np.max(np.abs(coefficients)))
        if magnitude:
            # Multiple real roots can acquire small imaginary parts in a
            # polynomial solve. Every candidate is checked in the original
            # matrices, so retaining its real part cannot admit local shear.
            for value in np.roots(coefficients / magnitude):
                if math.isfinite(float(value.real)):
                    candidates.append(2 * math.atan(float(value.real)))
    for angle in sorted(candidates, key=abs):
        rotation = (parallel + math.cos(angle) * perpendicular + math.sin(angle) * cross) @ swing
        oriented = try_world_turn(rotation)
        if oriented is not None:
            return oriented
    raise RigError('RIG_SHEAR', 'Closed-chain pole turn is not representable as local TRS.')


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


# Minimum FK-to-IK matching pole distance, far above the 1e-8 m pole-degeneracy
# cutoff in _pole_perpendicular. Ordinary matches keep their reach distance.
MIN_MATCHING_POLE_DISTANCE = 1e-6


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
    distance = float(np.linalg.norm(target-origin))
    lower = max(0., float(2*lengths.max()-lengths.sum()))
    reachable = lower-1e-7 <= distance <= lengths.sum()+1e-7
    direction = unit(target-origin) if distance > 1e-9 else unit(points[-1]-origin) if np.linalg.norm(points[-1]-origin) > 1e-9 else np.array([0., 1., 0.])
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
    with np.errstate(over='ignore', invalid='ignore'):
        pole_delta = pole-origin
    require(np.isfinite(pole_delta).all(), 'RIG_NONFINITE', 'IK pole offset exceeds finite representation.')
    pole_length = math.hypot(*pole_delta)
    pole_direction = unit(pole_delta / np.max(np.abs(pole_delta))) if pole_length > 1e-9 else None
    matched_fold = (distance <= 1e-9 and np.linalg.norm(points[-1]-origin) <= 1e-9
                    and np.linalg.norm(points[-1]-target) <= tolerance
                    and pole_length > 1e-9
                    and np.linalg.norm(pole_direction-unit(points[1]-origin)) <= 8*np.finfo(float).eps)
    if matched_fold:
        # A closed chain has no endpoint axis from which to reconstruct its
        # interior. Keep its authoritative matched seed, then run the same
        # constraint/orientation pass and residual checks as every other solve.
        result, changes = apply_pass(result)
    elif (distance <= 1e-9 and np.linalg.norm(points[-1]-origin) <= 1e-9
          and np.linalg.norm(points[-1]-target) <= tolerance
          and pole_length > 1e-9):
        # Rotate the closed seed coherently at its root. Reconstructing every
        # joint from translated world points loses small segments at large
        # origins and changes the seed's internal frames and blended geometry.
        world = evaluate_fk(skeleton, result)
        root = names[0]
        bone = next(bone for bone in skeleton['bones'] if bone['id'] == root)
        parent = bone['parent']
        seed = {**bone['rest'], **result['bones'].get(root, {})}
        # Form the first segment before adding the translated world origin.
        # Its native local offset is available exactly; subtracting rounded
        # world points would discard that information at distant origins.
        first = next(bone for bone in skeleton['bones'] if bone['id'] == names[1])
        first_seed = {**first['rest'], **result['bones'].get(names[1], {})}
        source = world[root][:3, :3] @ vector(first_seed['translation'])
        target_resolution = np.array([math.ulp(float(origin[i])) + math.ulp(float(pole[i]))
                                      for i in range(3)])
        try:
            oriented = _fold_root_transform(world[parent][:3, :3] if parent else np.eye(3),
                                            world[root][:3, :3], seed, source, pole_delta,
                                            target_resolution)
        except np.linalg.LinAlgError as exc:
            raise RigError('RIG_SINGULAR_TRANSFORM',
                           'Closed-chain pole steering needs a numerically invertible transform.') from exc
        result['bones'].setdefault(root, {}).update(
            rotation=oriented['rotation'], scale=oriented['scale'])
        result, changes = apply_pass(result)
        iterations = 1
    elif (len(names) == 3 and distance <= 1e-9
          and abs(lengths[0]-lengths[1]) <= 1e-9):
        # The equal-length folded limit applies to either solver. The pole
        # names the elbow direction directly, without an invented world axis.
        bend = pole_delta if pole_length > 1e-9 else points[1]-origin
        points[1] = origin + unit(bend)*lengths[0]
        points[2] = target
        result = _orient_chain(skeleton, result, names, points)
        result, changes = apply_pass(result)
        iterations = 1
    elif chain['solver'] == 'two-bone':
        d = float(np.clip(distance, max(lower, 1e-9), lengths.sum()))
        a, b = lengths
        x = (d*d+a*a-b*b)/(2*d)
        y = math.sqrt(max(0., a*a-x*x))
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
                    radius = np.linalg.norm(delta-projection)
                    points[i] = points[i-1]+projection+_pole_perpendicular(axis, points[i-1], pole)*radius
            result = _orient_chain(skeleton, result, names, points)
            result, current = apply_pass(result)
            changes.extend(current)
            world = evaluate_fk(skeleton, result)
            points = np.array([world[name][:3, 3] for name in names])
            if np.linalg.norm(points[-1]-target) <= tolerance or not reachable:break
    final = evaluate_fk(skeleton, result)[names[-1]]
    error = float(np.linalg.norm(final[:3, 3]-target))
    orientation_error = (qangle(decompose(final)['rotation'], orientation)
                         if orientation is not None else None)
    converged = reachable and error <= tolerance and (orientation_error is None or orientation_error <= 1e-5)
    return IKResult(result, error, bool(reachable), bool(converged), iterations, changes, orientation_error)


def _matching_bend(root, first, end, reach):
    """Bend direction of a matched seed, resolved relative to its own scale.

    The solver's absolute 1e-8 m perpendicular cutoff would discard the
    measured bend of a valid tiny chain and substitute an arbitrary axis.
    Use the measured perpendicular whenever it exceeds its rounding
    uncertainty; otherwise keep the existing degenerate fallback.
    """
    direction = unit(end-root)
    delta = first-root
    raw = delta-direction*np.dot(delta, direction)
    rounding = 64*np.finfo(float).eps*max(float(np.abs(root).max()), float(np.abs(first).max()),
                                           float(np.abs(end).max()))
    if np.linalg.norm(raw) > rounding*(1.+np.linalg.norm(delta)/reach):
        return unit(raw)
    return _pole_perpendicular(direction, root, first)


def match_fk_to_ik(skeleton: dict, pose: dict, chain: dict) -> dict:
    validate_chain(skeleton, chain)
    points, lengths = _chain_points_and_lengths(skeleton, chain['bones'], pose)
    world = evaluate_fk(skeleton, pose)
    reach = float(np.linalg.norm(points[-1]-points[0]))
    if reach <= 1e-9:
        # A valid fully folded chain has a measurable first segment even
        # though its endpoint coincides with its root.
        pole = points[1]
    else:
        bend = _matching_bend(points[0], points[1], points[-1], reach)
        # Below the 1e-8 m pole-degeneracy cutoff a reach-distance pole would
        # be replaced by an arbitrary axis, moving the elbow of a valid
        # near-closed seed. Keep the same bend direction at a safe distance.
        pole = points[0]+bend*max(reach, MIN_MATCHING_POLE_DISTANCE)
    return dict(target=points[-1].tolist(), pole=pole.tolist(),
                orientation=decompose(world[chain['bones'][-1]])['rotation'])
