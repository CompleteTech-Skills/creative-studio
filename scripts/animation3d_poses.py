"""Detached native pose editing delegated to the canonical rig authorities.

Locks refer to bone-local channels. World-space locking requires an IK owner.
Named immutable storage remains animation3d_evaluator.PoseLibrary's authority.
"""
from copy import deepcopy
import math
from pathlib import Path

import numpy as np

from rig_contracts import validate_contract
from rig_errors import MAX_JSON_BYTES, RigError, decode_json, open_payload, require, safe_path, job_lock
from rig_skeleton import (neutral_pose, evaluate_fk, inspect_skeleton,
                          mirror_pose as native_mirror, validate_skeleton)
from rig_skinning import evaluate_deformation
from rig_animation import blend_poses as native_blend, clip_from_poses, save_pose as native_save
from rig_constraints import apply_constraints


def validate_pose(rig, pose):
    """Return a detached native Pose after FK, limits and actual skin evaluation."""
    validate_contract(pose, 'Pose')
    value = deepcopy(pose)
    require(value['rig_version'] == rig.skeleton['version'], 'ANIMATION_IDENTITY', 'Pose rig version differs.')
    validate_skeleton(rig.skeleton, rig.manifest['limits'])
    require(rig.geometry.vertex_count*256 + len(rig.skeleton['bones'])*2048 <= rig.manifest['limits']['memory_bytes'],
            'ANIMATION_LIMIT', 'Pose geometry working estimate exceeds the native memory budget.')
    try:
        world = evaluate_fk(rig.skeleton, value)
        constraints = rig.manifest.get('controllers', {}).get('constraints', [])
        if constraints:
            limited, _ = apply_constraints(rig.skeleton, value, constraints)
            corrected = evaluate_fk(rig.skeleton, limited)
            require(all(np.allclose(world[name], corrected[name], rtol=0, atol=1e-8) for name in world),
                    'ANIMATION_POSE_CONSTRAINT', 'Pose violates an authored native rig constraint.')
        evaluate_deformation(rig, value)
    except RigError:
        raise
    except (OverflowError, ValueError, TypeError) as exc:
        raise RigError('ANIMATION_POSE_NUMERIC', 'Native pose values cannot be evaluated as finite canonical transforms.') from exc
    return value


def _locks(rig, locked_bones):
    require(isinstance(locked_bones, (list, tuple, set, frozenset)), 'ANIMATION_POSE_LOCK', 'Use a finite bone ID collection.')
    require(len(locked_bones) <= len(rig.skeleton['bones']), 'ANIMATION_POSE_LOCK', 'Bone lock collection exceeds the rig size.')
    require(all(isinstance(name, str) for name in locked_bones), 'ANIMATION_POSE_LOCK', 'Bone locks require IDs.')
    names = set(locked_bones)
    require(names <= {b['id'] for b in rig.skeleton['bones']}, 'ANIMATION_POSE_LOCK', 'Locked bone is absent.')
    return names


def _preserve_locks(rig, original, result, locked_bones):
    for name in _locks(rig, locked_bones):
        if name in original['bones']:
            result['bones'][name] = deepcopy(original['bones'][name])
        else:
            result['bones'].pop(name, None)
    return result


def duplicate_pose(rig, pose, identifier):
    result = validate_pose(rig, pose)
    result['id'] = identifier
    return validate_pose(rig, result)


def reset_pose(rig, pose=None, *, identifier='neutral', locked_bones=()):
    original = validate_pose(rig, pose) if pose is not None else neutral_pose(rig.skeleton)
    result = neutral_pose(rig.skeleton, identifier)
    return validate_pose(rig, _preserve_locks(rig, original, result, locked_bones))


def edit_pose(rig, pose, *, bones=None, morphs=None, locked_bones=(), symmetric=False, approved=False):
    """Replace specified local channels; optionally copy their reflected edits.

    Symmetric bone editing requires reciprocal mappings declared on the native
    skeleton. Existing unmapped channels are retained; no anatomy is inferred.
    """
    require(type(symmetric) is bool, 'ANIMATION_POSE_EDIT', 'Symmetric editing is a boolean option.')
    result = validate_pose(rig, pose)
    bones = {} if bones is None else bones
    morphs = {} if morphs is None else morphs
    require(isinstance(bones, dict) and isinstance(morphs, dict), 'ANIMATION_POSE_EDIT', 'Edits must be channel maps.')
    # Reject oversized/malformed patches before detached-copy allocation.
    validate_contract(dict(schema_version=1, kind='hatch-3d-pose', id=result['id'], rig_version=result['rig_version'], bones=bones, morphs=morphs), 'Pose')
    require(set(bones) <= {b['id'] for b in rig.skeleton['bones']} and set(morphs) <= set(rig.geometry.by_id()),
            'ANIMATION_POSE_EDIT', 'Edited native bone or mesh is absent.')
    bones, morphs = deepcopy(bones), deepcopy(morphs)
    locks = _locks(rig, locked_bones)
    require(not set(bones) & locks, 'ANIMATION_POSE_LOCK', 'An explicit edit targets a locked bone.')
    for name, channels in bones.items():
        require(isinstance(channels, dict), 'ANIMATION_POSE_EDIT', 'Bone edits must be local channel maps.')
        result['bones'].setdefault(name, {}).update(channels)
    for mesh, channels in morphs.items():
        require(isinstance(channels, dict), 'ANIMATION_POSE_EDIT', 'Morph edits must be channel maps.')
        result['morphs'].setdefault(mesh, {}).update(channels)
    result = validate_pose(rig, result)
    if symmetric:
        require(approved is True, 'RIG_MIRROR_APPROVAL', 'Symmetry editing requires explicit approval.')
        index = {b['id']: b for b in rig.skeleton['bones']}
        pairs = {name: index[name]['mirror'] for name in bones}
        require(all(partner is not None for partner in pairs.values()), 'ANIMATION_POSE_MAPPING', 'Edited bones need explicit reciprocal mirror partners.')
        require(not set(pairs.values()) & set(bones), 'ANIMATION_POSE_MAPPING', 'Edit one side of each pair; conflicting bilateral edits are ambiguous.')
        require(not set(pairs.values()) & locks, 'ANIMATION_POSE_LOCK', 'The symmetric partner is locked.')
        reflected = native_mirror(rig.skeleton, result, approved=True)
        for partner in pairs.values():
            result['bones'][partner] = deepcopy(reflected['bones'][partner])
    return validate_pose(rig, result)


def blend_pose(rig, source, target, weight, *, locked_bones=()):
    require(type(weight) in (int, float) and 0 <= weight <= 1 and math.isfinite(weight),
            'ANIMATION_POSE_WEIGHT', 'Blend weight must be finite and within [0,1].')
    source, target = validate_pose(rig, source), validate_pose(rig, target)
    # Materialize the target's rest/default channels so sparse target poses can
    # restore neutral channels instead of leaving source overrides in place.
    complete = deepcopy(target)
    for bone in rig.skeleton['bones']:
        complete['bones'][bone['id']] = {**deepcopy(bone['rest']), **target['bones'].get(bone['id'], {})}
    for mesh in rig.geometry.meshes:
        complete['morphs'][mesh.id] = {name: target['morphs'].get(mesh.id, {}).get(name, mesh.morph_defaults.get(name, 0.)) for name in mesh.morphs}
    result = native_blend(rig, source, complete, float(weight))
    return validate_pose(rig, _preserve_locks(rig, source, result, locked_bones))


def mirror_pose(rig, pose, *, approved=False, mapping=None, morph_mapping=None, locked_bones=()):
    value = validate_pose(rig, pose)
    require(approved is True, 'RIG_MIRROR_APPROVAL', 'Mirroring requires explicit symmetry approval.')
    skeleton = deepcopy(rig.skeleton)
    bones = {b['id']: b for b in skeleton['bones']}
    if mapping is not None:
        require(isinstance(mapping, dict) and len(mapping) <= len(bones) and set(mapping) <= set(bones), 'ANIMATION_POSE_MAPPING', 'Mirror mapping names missing bones.')
        for name, partner in mapping.items():
            require(partner is None or isinstance(partner, str) and partner in bones and mapping.get(partner) == name,
                    'ANIMATION_POSE_MAPPING', 'Explicit mirror mappings must be reciprocal.')
            bones[name]['mirror'] = partner
        validate_skeleton(skeleton, rig.manifest['limits'])
    result = native_mirror(skeleton, value, approved=True)
    if morph_mapping is not None:
        require(isinstance(morph_mapping, dict), 'ANIMATION_POSE_MAPPING', 'Morph mapping must be per-mesh channel maps.')
        meshes = rig.geometry.by_id()
        require(len(morph_mapping) <= len(meshes) and set(morph_mapping) <= set(meshes), 'ANIMATION_POSE_MAPPING', 'Mapped mesh is absent.')
        for mesh, pairs in morph_mapping.items():
            require(isinstance(pairs, dict) and set(pairs) <= set(meshes[mesh].morphs), 'ANIMATION_POSE_MAPPING', 'Mapped morph is absent.')
            for name, partner in pairs.items():
                require(isinstance(partner, str) and partner in meshes[mesh].morphs and pairs.get(partner) == name,
                        'ANIMATION_POSE_MAPPING', 'Morph mirror mappings must be reciprocal.')
                result['morphs'].setdefault(mesh, {})[partner] = value['morphs'].get(mesh, {}).get(name, meshes[mesh].morph_defaults.get(name, 0.))
    return validate_pose(rig, _preserve_locks(rig, value, result, locked_bones))


def compare_poses(rig, source, target):
    source, target = validate_pose(rig, source), validate_pose(rig, target)
    a, b = evaluate_fk(rig.skeleton, source), evaluate_fk(rig.skeleton, target)
    ga, gb = evaluate_deformation(rig, source), evaluate_deformation(rig, target)
    return dict(world_matrix_max_error={name: float(np.max(np.abs(a[name]-b[name]))) for name in a},
                vertex_max_displacement={mesh: float(np.linalg.norm(ga[mesh]['positions']-gb[mesh]['positions'], axis=1).max(initial=0)) for mesh in ga},
                scope='actual native FK and skinned/morphed geometry; no visual approval')


def inspect_rest_pose(rig):
    pose = validate_pose(rig, neutral_pose(rig.skeleton))
    return dict(pose=pose, bones=inspect_skeleton(rig.skeleton, pose),
                scope='native rest transforms and hierarchy; no anatomy inference')


def transition_samples(rig, source, target, *, samples=2, locked_bones=()):
    """Return uniformly spaced native poses; the caller owns its exact clock."""
    require(type(samples) is int and 2 <= samples <= 256,
            'ANIMATION_POSE_TIME', 'Use 2..256 transition samples.')
    channels = 3*len(rig.skeleton['bones']) + sum(len(m.morphs) for m in rig.geometry.meshes)
    require(samples*channels <= rig.manifest['limits']['keyframes'], 'ANIMATION_LIMIT', 'Pose transition exceeds native key budget.')
    require(samples*channels*512 + rig.geometry.vertex_count*256 <= rig.manifest['limits']['memory_bytes'],
            'ANIMATION_LIMIT', 'Pose transition exceeds conservative working-memory admission.')
    source, target = validate_pose(rig, source), validate_pose(rig, target)
    return [blend_pose(rig, source, target, index/(samples-1), locked_bones=locked_bones) for index in range(samples)]


def pose_transition(rig, source, target, *, identifier='pose-transition', duration_seconds=1., samples=2, fps=24, locked_bones=()):
    """Generate a native seconds-based RigAnimationClip through its existing owner.

    Frame-based Animation3D consumers use transition_samples and their own exact
    rational clock; this explicit compatibility adapter retains native fps limits.
    """
    require(type(duration_seconds) in (int, float) and 0 < duration_seconds <= rig.manifest['limits']['duration'] and math.isfinite(duration_seconds),
            'ANIMATION_POSE_TIME', 'Duration must fit the native seconds budget.')
    require(type(fps) is int and 1 <= fps <= 60, 'ANIMATION_POSE_TIME', 'Native integer fps must be within 1..60.')
    poses = transition_samples(rig, source, target, samples=samples, locked_bones=locked_bones)
    times = [index*duration_seconds/(samples-1) for index in range(samples)]
    return clip_from_poses(rig, identifier, times, poses, fps=fps)


def apply_expressions(rig, pose, expressions, *, reset=True, locked_bones=()):
    from rig_facial import apply_expressions as native_expressions
    require(isinstance(expressions, dict) and all(type(v) in (int, float) and 0 <= v <= 1 and math.isfinite(v) for v in expressions.values()),
            'RIG_EXPRESSION_INTENSITY', 'Expression intensities must be finite numbers in [0,1].')
    require(type(reset) is bool, 'ANIMATION_POSE_EDIT', 'Facial reset is a boolean option.')
    source = validate_pose(rig, pose)
    result = native_expressions(rig, source, expressions, reset=reset)
    return validate_pose(rig, _preserve_locks(rig, source, result, locked_bones))


def save_pose(rig, path, pose):
    """Save an editable snapshot while preserving the native source inventory.

    A detached evaluator's expired temporary root is not a source binding.
    Callers saving snapshots must provide an independently admitted live rig.
    """
    value = validate_pose(rig, pose)
    require(rig.root is not None, 'ANIMATION_POSE_PATH', 'Snapshot saving needs a live native source root.')
    try:
        root = Path(rig.root).resolve(strict=True)
        destination = Path(path).absolute()
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise RigError('ANIMATION_POSE_PATH', 'Snapshot saving needs a live source root and resolvable destination.') from exc
    require(root.is_dir(), 'ANIMATION_POSE_PATH', 'Native source root is unavailable.')
    # Reuse the native writer lock: an updated live manifest may protect more
    # paths than an older detached in-memory context. Geometry is not reloaded.
    with job_lock(root):
        try:
            resolved = destination.resolve(strict=False)
            canonical_destination = destination.parent.resolve(strict=False) / destination.name
        except (OSError, RuntimeError, ValueError) as exc:
            raise RigError('ANIMATION_POSE_PATH', 'Pose destination cannot be resolved safely.') from exc
        current = _read_json_for_rig(rig, root/'rig.json')
        validate_contract(current, 'Rig')
        require((current['id'], current['version']) == (rig.manifest['id'], rig.manifest['version']),
                'ANIMATION_IDENTITY', 'Live source root does not match the admitted rig identity.')
        from animation3d_contracts import _walk
        names = {'rig.json', 'identity.json'}
        for manifest in (rig.manifest, current):
            names.update(node['path'] for node in _walk(manifest)
                         if {'path', 'sha256'} <= node.keys() and isinstance(node['path'], str))
        protected = {safe_path(root, name).resolve(strict=False) for name in names}
        protected.add(root.parent / ('.' + root.name + '.rig.lock'))
        require(resolved not in protected, 'ANIMATION_POSE_SOURCE', 'Editable pose snapshots cannot replace native source or inventory files.')
        try:
            relative = resolved.relative_to(root).as_posix().casefold()
        except ValueError:
            relative = None
        require(relative is None or relative not in {name.casefold() for name in names},
                'ANIMATION_POSE_SOURCE', 'Pose destination collides with a portable native source name.')
        require(not destination.is_symlink(), 'RIG_SYMLINK', 'Pose snapshot destinations cannot be symbolic links.')
        native_save(canonical_destination, value)


def _read_json_for_rig(rig, path):
    from rig_load_limits import _JSON_FACTOR
    path = Path(path).absolute()
    retained = max((rig.resource_usage or {}).get('estimated_memory_bytes', 0), rig.geometry.vertex_count*256 + len(rig.skeleton['bones'])*2048)
    remaining = rig.manifest['limits']['memory_bytes'] - retained
    require(remaining > 0, 'ANIMATION_LIMIT', 'No pose JSON memory remains in the native reservation.')
    byte_limit = min(MAX_JSON_BYTES, int(remaining // _JSON_FACTOR))
    with open_payload(path.parent.resolve(strict=True), path.name) as (stream, metadata):
        require(metadata.st_size <= byte_limit, 'ANIMATION_LIMIT', 'JSON object estimate exceeds the remaining native memory budget.')
        payload = stream.read(byte_limit+1)
        require(len(payload) == metadata.st_size, 'ANIMATION_HASH', 'Snapshot changed while reading.')
    return decode_json(payload)


def load_pose(rig, path):
    return validate_pose(rig, _read_json_for_rig(rig, path))
