"""Real normalized skin weights, graph diffusion and chunked linear blend skinning."""
from __future__ import annotations

from copy import deepcopy
from numbers import Integral, Real
import numpy as np
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require
from rig_geometry import mesh_edges, vertex_normals
from rig_math import transform_points
from rig_models import Geometry, Mesh, RigAsset, SkinWeights, validate_geometry
from rig_skeleton import evaluate_fk, validate_skeleton


def _distances(vertices: np.ndarray, heads: np.ndarray, tails: np.ndarray) -> np.ndarray:
    segments = tails-heads
    square = np.sum(segments*segments, axis=1)
    require(np.all(square > 1e-16), 'RIG_BONE_LENGTH', 'Cannot weight a zero-length deform segment.')
    relative = vertices[:, None, :]-heads[None, :, :]
    t = np.clip(np.sum(relative*segments[None, :, :], axis=2)/square, 0, 1)
    return np.linalg.norm(relative-t[:, :, None]*segments[None, :, :], axis=2)


def _prune(dense: np.ndarray, count: int) -> tuple[np.ndarray, np.ndarray]:
    require(np.isfinite(dense).all() and np.all(dense >= 0), 'RIG_WEIGHTS', 'Weights must be finite and nonnegative.')
    selected = np.argsort(-dense, axis=1, kind='stable')[:, :min(count, dense.shape[1])]
    values = np.take_along_axis(dense, selected, axis=1)
    total = values.sum(axis=1, keepdims=True)
    require(np.all(total > 1e-15), 'RIG_UNWEIGHTED', 'Some vertices have no effective bone influence.')
    values = values/total
    if selected.shape[1] < count:
        selected = np.pad(selected, ((0, 0), (0, count-selected.shape[1])))
        values = np.pad(values, ((0, 0), (0, count-values.shape[1])))
    values[values < 1e-10] = 0
    values /= values.sum(axis=1, keepdims=True)
    return selected.astype(np.uint16), values


def generate_skin_weights(geometry: Geometry, skeleton: dict, *, method='auto',
                          regions: dict[str, list[str]] | None = None, previous=None,
                          iterations=16, limits=None) -> dict[str, SkinWeights]:
    limits = limits or DEFAULT_LIMITS
    validate_geometry(geometry, limits);validate_skeleton(skeleton, limits)
    require(method in ('auto', 'distance', 'diffusion', 'segmented', 'rigid'), 'RIG_WEIGHT_METHOD', 'Unknown skinning method.')
    require(0 <= iterations <= 64, 'RIG_LIMIT_ITERATIONS', 'Weight smoothing needs 0–64 iterations.')
    all_bones = [b for b in skeleton['bones'] if b['deform']]
    require(bool(all_bones), 'RIG_NO_DEFORM_BONES', 'A skin needs at least one deform bone.')
    joint_ids = [b['id'] for b in all_bones]
    world = evaluate_fk(skeleton)
    inverse = np.array([np.linalg.inv(world[name]) for name in joint_ids])
    result = {}
    for mesh in geometry.meshes:
        allowed = (regions or {}).get(mesh.id)
        if allowed is None and mesh.region in joint_ids:allowed = [mesh.region]
        require(allowed is None or (bool(allowed) and set(allowed) <= set(joint_ids)),
                'RIG_WEIGHT_REGION', 'A semantic skin region references missing deform bones.', asset=mesh.id)
        indices = [i for i, b in enumerate(all_bones) if allowed is None or b['id'] in allowed]
        bones = [all_bones[i] for i in indices]
        count = limits['influences']
        require(count in (4, 8), 'RIG_INFLUENCE_PROFILE', 'Portable profiles explicitly support four or eight influences.')
        estimated = len(mesh.positions)*len(bones)*8*5
        use_diffusion = method in ('auto', 'diffusion', 'segmented') and len(bones) > 1
        fallback = None
        if use_diffusion and (estimated > limits['memory_bytes']//2 or len(bones) > 64):
            use_diffusion = False
            fallback = 'Graph diffusion working set exceeded its bound; used chunked segment-distance weighting.'
        edges, _ = mesh_edges(mesh.triangles)
        if use_diffusion and len(edges) == 0:
            use_diffusion = False;fallback = 'No usable topology edges; used segment-distance weighting.'
        selected, weights = [], []
        dense_parts = []
        heads, tails = np.array([b['head'] for b in bones]), np.array([b['tail'] for b in bones])
        lengths = np.linalg.norm(tails-heads, axis=1)
        for start in range(0, len(mesh.positions), 4096):
            distances = _distances(mesh.positions[start:start+4096], heads, tails)
            if method == 'rigid' or len(bones) == 1:
                dense = np.zeros_like(distances);dense[np.arange(len(dense)), np.argmin(distances, axis=1)] = 1
            else:
                # Relative distances avoid underflow for vertices far from a thin segment.
                sigma = max(.015, float(np.median(lengths))*.3)
                squared = distances**2
                dense = np.exp(-np.clip((squared-squared.min(axis=1, keepdims=True))/(2*sigma*sigma), 0, 80))
                dense /= dense.sum(axis=1, keepdims=True)
            if use_diffusion:dense_parts.append(dense)
            else:
                a, b = _prune(dense, count);selected.append(a);weights.append(b)
        if use_diffusion:
            dense = np.concatenate(dense_parts)
            seed = dense.copy()
            locked_seed = np.max(seed, axis=1) > .94
            degree = np.bincount(edges.ravel(), minlength=len(dense)).clip(min=1)[:, None]
            for _ in range(iterations):
                neighbours = np.zeros_like(dense)
                np.add.at(neighbours, edges[:, 0], dense[edges[:, 1]])
                np.add.at(neighbours, edges[:, 1], dense[edges[:, 0]])
                dense = .35*seed + .65*neighbours/degree
                dense[locked_seed] = seed[locked_seed]
            local_indices, values = _prune(dense, count)
        else:
            local_indices, values = np.concatenate(selected), np.concatenate(weights)
        joint_indices = np.asarray(indices, dtype=np.uint16)[local_indices]
        chosen_method = 'rigid' if method == 'rigid' or len(bones) == 1 else 'diffusion' if use_diffusion else 'distance'
        weight = SkinWeights(mesh.id, joint_ids.copy(), joint_indices, values, inverse.copy(),
                             method=chosen_method, fallback_reason=fallback)
        old = (previous or {}).get(mesh.id)
        if old is not None and len(old.locked):
            require(len(old.weights) == len(values), 'RIG_LOCKED_TOPOLOGY', 'Manual weight locks require unchanged topology.')
            for vertex in old.locked:
                row = {old.joints[int(j)]: float(w) for j, w in zip(old.indices[vertex], old.weights[vertex]) if w > 0}
                weight = edit_weights(weight, {int(vertex): row}, max_influences=count, lock=True)
        validate_skin(weight, mesh, skeleton, limits)
        result[mesh.id] = weight
    return result


def validate_skin(skin: SkinWeights, mesh: Mesh, skeleton: dict, limits=None) -> dict:
    limits = limits or DEFAULT_LIMITS
    indices, weights = skin.indices, skin.weights
    bone_ids = {b['id'] for b in skeleton['bones']}
    require(len(set(skin.joints)) == len(skin.joints) and set(skin.joints) <= bone_ids
            and bool(skin.joints), 'RIG_SKIN_JOINT', 'Skin references missing or duplicate joints.')
    require(indices.ndim == 2 and indices.shape == weights.shape and len(indices) == len(mesh.positions)
            and indices.shape[1] <= limits['influences'] and indices.shape[1] > 0,
            'RIG_SKIN_SHAPE', 'Every vertex needs bounded, matching joint and weight attributes.')
    require(indices.dtype.kind in 'iu' and indices.min() >= 0 and indices.max() < len(skin.joints),
            'RIG_SKIN_INDEX', 'Joint indices must reference the skin joint array.')
    require(np.isfinite(weights).all() and np.all(weights >= 0), 'RIG_WEIGHTS', 'Weights must be finite and nonnegative.')
    error = float(np.max(np.abs(weights.sum(axis=1)-1)))
    require(error <= 1e-4, 'RIG_WEIGHT_NORMALIZATION', 'Vertex weights must sum to one.', observed=error)
    for i in range(indices.shape[1]):
        for j in range(i):
            require(not np.any((indices[:, i] == indices[:, j]) & (weights[:, i] > 0) & (weights[:, j] > 0)),
                    'RIG_DUPLICATE_INFLUENCE', 'A vertex cannot repeat a nonzero joint influence.')
    require(skin.inverse_bind.shape == (len(skin.joints), 4, 4) and np.isfinite(skin.inverse_bind).all(),
            'RIG_BIND_MATRIX', 'Inverse bind matrix data are incomplete or nonfinite.')
    rest = evaluate_fk(skeleton)
    bind_error = max(float(np.abs(rest[j] @ matrix-np.eye(4)).max()) for j, matrix in zip(skin.joints, skin.inverse_bind))
    require(bind_error <= 1e-5, 'RIG_BIND_MATRIX', 'Inverse binds do not match the canonical rest joints.', observed=bind_error)
    require(skin.locked.ndim == 1 and skin.locked.dtype.kind in 'iu'
            and (not len(skin.locked) or (skin.locked.min() >= 0 and skin.locked.max() < len(weights))),
            'RIG_LOCKED_VERTEX', 'Manual lock references a missing vertex.')
    return dict(weight_sum_max_error=error, inverse_bind_max_error=bind_error,
                vertices=len(weights), influences=weights.shape[1], locked_vertices=len(skin.locked))


def deform_mesh(mesh: Mesh, skin: SkinWeights, world: dict, morph_weights=None) -> np.ndarray:
    vertices = np.array(mesh.positions, dtype=np.float64, copy=True)
    for name, amount in {**mesh.morph_defaults, **(morph_weights or {})}.items():
        require(name in mesh.morphs, 'RIG_MORPH_REFERENCE', 'Morph target is unavailable: ' + name, asset=mesh.id)
        require(np.isfinite(amount) and -2 <= amount <= 2, 'RIG_MORPH_RANGE', 'Morph weights must be finite in [-2, 2].')
        vertices += mesh.morphs[name]['POSITION']*amount
    matrices = np.array([world[name] @ skin.inverse_bind[i] for i, name in enumerate(skin.joints)])
    output = np.zeros_like(vertices)
    for start in range(0, len(vertices), 8192):
        stop = min(start+8192, len(vertices));v = vertices[start:stop]
        for k in range(skin.indices.shape[1]):
            palette = matrices[skin.indices[start:stop, k]]
            transformed = np.einsum('nij,nj->ni', palette[:, :3, :3], v)+palette[:, :3, 3]
            output[start:stop] += transformed*skin.weights[start:stop, k, None]
    require(np.isfinite(output).all(), 'RIG_NONFINITE_DEFORMATION', 'Skinning produced nonfinite vertex positions.')
    return output


def evaluate_deformation(rig: RigAsset, pose: dict | None = None, *, normals=False) -> dict:
    require(set(rig.weights) == set(rig.geometry.by_id()), 'RIG_MISSING_SKIN', 'Every mesh must have a real skin binding.')
    world = evaluate_fk(rig.skeleton, pose)
    morphs = (pose or {}).get('morphs', {})
    require(set(morphs) <= set(rig.geometry.by_id()), 'RIG_MORPH_REFERENCE', 'Morph pose references missing geometry.')
    output = {}
    for mesh in rig.geometry.meshes:
        positions = deform_mesh(mesh, rig.weights[mesh.id], world, morphs.get(mesh.id))
        output[mesh.id] = dict(positions=positions)
        if normals:output[mesh.id]['normals'] = vertex_normals(positions, mesh.triangles)
    return output


def edit_weights(skin: SkinWeights, assignments: dict[int, dict[str, float]], *, max_influences=4, lock=True) -> SkinWeights:
    require(isinstance(max_influences, Integral) and not isinstance(max_influences, (bool, np.bool_)) and 1 <= max_influences <= skin.indices.shape[1],
            'RIG_WEIGHT_EDIT', 'Influence limit must be an integer within the stored skin width.')
    result = deepcopy(skin)
    lookup = {name: i for i, name in enumerate(skin.joints)}
    for vertex, row in assignments.items():
        require(type(vertex) is int and 0 <= vertex < len(skin.weights), 'RIG_VERTEX_REFERENCE', 'Weight correction vertex is invalid.')
        require(bool(row) and set(row) <= set(lookup) and len(row) <= max_influences,
                'RIG_WEIGHT_EDIT', 'Weight correction needs existing joints within the influence limit.')
        dense = np.zeros((1, len(lookup)))
        for joint, weight in row.items():
            require(isinstance(weight, (float, int)) and not isinstance(weight, bool) and np.isfinite(weight) and weight >= 0,
                    'RIG_WEIGHT_EDIT', 'Edited weights must be finite and nonnegative.')
            dense[0, lookup[joint]] = weight
        indices, weights = _prune(dense, result.indices.shape[1])
        result.indices[vertex], result.weights[vertex] = indices[0], weights[0]
    locked = set(map(int, result.locked))
    if lock:locked.update(assignments)
    else:locked.difference_update(assignments)
    result.locked = np.array(sorted(locked), dtype=np.int64)
    result.method = 'manual'
    return result


def copy_weights(skin: SkinWeights, pairs: dict[int, int], *, lock=True) -> SkinWeights:
    assignments = {}
    for target, source in pairs.items():
        require(isinstance(source, Integral) and not isinstance(source, (bool, np.bool_)) and 0 <= source < len(skin.weights), 'RIG_VERTEX_REFERENCE', 'Source vertex is missing.')
        assignments[target] = {skin.joints[int(j)]: float(w) for j, w in zip(skin.indices[source], skin.weights[source]) if w > 0}
    return edit_weights(skin, assignments, max_influences=skin.weights.shape[1], lock=lock)


def smooth_weights(mesh: Mesh, skin: SkinWeights, *, iterations=4, strength=.3) -> SkinWeights:
    require(isinstance(iterations, Integral) and not isinstance(iterations, (bool, np.bool_)) and 0 <= iterations <= 64
            and isinstance(strength, Real) and not isinstance(strength, (bool, np.bool_))
            and np.isfinite(strength) and 0 <= strength <= 1, 'RIG_WEIGHT_SMOOTH', 'Smoothing parameters exceed their bounds.')
    require(len(mesh.positions)*len(skin.joints)*8*3 < DEFAULT_LIMITS['memory_bytes']//2,
            'RIG_LIMIT_MEMORY', 'Dense manual smoothing exceeds the bounded working set.')
    dense = np.zeros((len(mesh.positions), len(skin.joints)))
    for k in range(skin.indices.shape[1]):dense[np.arange(len(dense)), skin.indices[:, k]] += skin.weights[:, k]
    edges, _ = mesh_edges(mesh.triangles)
    degree = np.bincount(edges.ravel(), minlength=len(dense)).clip(min=1)[:, None]
    original = dense.copy()
    for _ in range(iterations):
        neighbours = np.zeros_like(dense)
        np.add.at(neighbours, edges[:, 0], dense[edges[:, 1]]);np.add.at(neighbours, edges[:, 1], dense[edges[:, 0]])
        dense = (1-strength)*dense+strength*neighbours/degree
        dense[skin.locked] = original[skin.locked]
    result = deepcopy(skin);result.indices, result.weights = _prune(dense, skin.indices.shape[1])
    result.method = 'manual'
    return result
