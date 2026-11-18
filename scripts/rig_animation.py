"""Continuous-time clips, SLERP/Hermite interpolation, contacts, layers and baking."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import numpy as np
from rig_contracts import DEFAULT_LIMITS, validate_contract
from rig_constraints import apply_constraints, constraint_order
from rig_errors import atomic_json, read_json, require
from rig_ik import solve_ik, validate_chain
from rig_math import IDENTITY_Q, qangle, qinverse, qmul, quaternion, slerp
from rig_models import RigAsset
from rig_skeleton import evaluate_fk, neutral_pose


def validate_animation(rig: RigAsset, clip: dict) -> dict:
    validate_contract(clip, 'AnimationClip')
    skeleton = rig.skeleton;limits = rig.manifest.get('limits', DEFAULT_LIMITS)
    require(clip['rig_id'] in (rig.manifest.get('id'), 'imported'), 'RIG_CLIP_COMPATIBILITY', 'Clip targets a different rig.')
    require(clip['skeleton_version'] == skeleton['version'], 'RIG_CLIP_VERSION', 'Clip skeleton version is stale.')
    require(clip['duration'] <= limits['duration'] and clip['fps'] <= limits['fps'], 'RIG_LIMIT_ANIMATION', 'Clip duration/frame rate exceeds its budget.')
    bones = {b['id'] for b in skeleton['bones']};meshes = rig.geometry.by_id();seen = set();keys = 0
    for track in clip['tracks']:
        key = (track['target'], track['path'])
        require(key not in seen, 'RIG_ANIMATION_DUPLICATE', 'A clip cannot repeat a target/path channel.');seen.add(key)
        times = np.array(track['times']);values = np.array(track['values'], dtype=float)
        require(times.ndim == 1 and len(times) == len(values) and np.all(np.diff(times) > 0)
                and times[0] >= 0 and times[-1] <= clip['duration']+1e-7,
                'RIG_ANIMATION_TIME', 'Clip keys must have ordered times within its duration.')
        keys += len(times)
        if track['path'] == 'weights':
            require(track['target'] in meshes and bool(meshes[track['target']].morphs), 'RIG_MORPH_REFERENCE', 'Weight animation targets missing morphs.')
            width = len(meshes[track['target']].morphs)
            require(np.all(np.abs(values) <= 2), 'RIG_MORPH_RANGE', 'Morph keys must stay in [-2,2].')
        else:
            require(track['target'] in bones, 'RIG_ANIMATION_REFERENCE', 'Bone animation target is missing.')
            width = 4 if track['path'] == 'rotation' else 3
            if track['path'] == 'rotation':
                require(np.all(np.abs(np.linalg.norm(values, axis=1)-1) < 1e-5), 'RIG_QUATERNION', 'Rotation keyframes must be unit quaternions.')
            if track['path'] == 'scale':require(np.all(values > 1e-8), 'RIG_SINGULAR_TRANSFORM', 'Animated scale must remain positive.')
        require(values.shape == (len(times), width), 'RIG_ANIMATION_VALUES', 'Animation channel dimensions are invalid.')
        if track['interpolation'] == 'CUBICSPLINE':
            require(len(times) >= 2 and np.asarray(track.get('in_tangents')).shape == values.shape
                    and np.asarray(track.get('out_tangents')).shape == values.shape,
                    'RIG_CUBIC_TANGENTS', 'Cubic channels require finite in/out tangents for every key.')
        else:
            require('in_tangents' not in track and 'out_tangents' not in track, 'RIG_CUBIC_TANGENTS', 'Non-cubic tracks cannot declare unused tangents.')
    require(keys <= limits['keyframes'], 'RIG_LIMIT_KEYFRAMES', 'Clip key budget exceeded.')
    chains = {c['id']: c for c in rig.manifest.get('controllers', {}).get('ik', [])}
    intervals = {}
    for contact in clip['contacts']:
        require(contact['chain'] in chains, 'RIG_CONTACT_CHAIN', 'Contact references a missing IK chain.')
        validate_chain(skeleton, chains[contact['chain']])
        require(0 <= contact['start'] < contact['end'] <= clip['duration'], 'RIG_CONTACT_INTERVAL', 'Contact interval is invalid.')
        for start, end in intervals.setdefault(contact['chain'], []):
            require(max(start, contact['start']) >= min(end, contact['end']), 'RIG_CONTACT_CONFLICT', 'One limb cannot have overlapping contact targets.')
        intervals[contact['chain']].append((contact['start'], contact['end']))
    require(all(e['time'] <= clip['duration'] for e in clip['events']), 'RIG_EVENT_TIME', 'Animation event exceeds its clip duration.')
    return dict(tracks=len(seen), keyframes=keys, contacts=len(clip['contacts']))


def evaluate_track(track: dict, time: float) -> np.ndarray:
    times = track['times'];values = np.asarray(track['values'], dtype=float)
    require(math.isfinite(time), 'RIG_NONFINITE', 'Animation time must be finite.')
    if time <= times[0]:return values[0].copy()
    if time >= times[-1]:return values[-1].copy()
    right = int(np.searchsorted(times, time, side='right'));left = right-1
    dt = times[right]-times[left];u = (time-times[left])/dt
    if track['interpolation'] == 'STEP':return values[left].copy()
    if track['interpolation'] == 'CUBICSPLINE':
        a, b = values[left], values[right]
        out_tangent = np.asarray(track['out_tangents'][left]);in_tangent = np.asarray(track['in_tangents'][right])
        result = (2*u**3-3*u**2+1)*a+(u**3-2*u**2+u)*dt*out_tangent+(-2*u**3+3*u**2)*b+(u**3-u**2)*dt*in_tangent
        return quaternion(result) if track['path'] == 'rotation' else result
    if track['path'] == 'rotation':return slerp(values[left], values[right], u)
    return values[left]*(1-u)+values[right]*u


def evaluate_animation(rig: RigAsset, clip: dict | str, time: float, *, loop=False,
                       contacts=True, constraints=True, _validated=False) -> dict:
    clip = rig.clips[clip] if isinstance(clip, str) else clip
    if not _validated:validate_animation(rig, clip)
    require(math.isfinite(time) and time >= 0, 'RIG_ANIMATION_TIME', 'Evaluation time must be finite and nonnegative.')
    duration = clip['duration'];cycles = int(time//duration) if loop and clip['loop'] else 0
    local_time = time % duration if loop and clip['loop'] else min(duration, time)
    pose = neutral_pose(rig.skeleton, clip['id']+'-pose')
    meshes = rig.geometry.by_id();root_id = rig.skeleton['root']
    root_rest = next(b['rest']['translation'] for b in rig.skeleton['bones'] if b['id'] == root_id)
    root_cycle = np.zeros(3)
    for track in clip['tracks']:
        values = evaluate_track(track, local_time)
        if track['path'] == 'weights':pose['morphs'][track['target']] = dict(zip(meshes[track['target']].morphs, map(float, values)))
        else:pose['bones'].setdefault(track['target'], {})[track['path']] = values.tolist()
        if track['target'] == root_id and track['path'] == 'translation':
            root_cycle = evaluate_track(track, duration)-evaluate_track(track, 0)
    if cycles and clip['root_motion'] == 'embedded':
        root = pose['bones'].setdefault(root_id, {})
        root['translation'] = (np.array(root.get('translation', root_rest))+cycles*root_cycle).tolist()
    controller_data = rig.manifest.get('controllers', dict(ik=[], constraints=[]))
    ordered = constraint_order(rig.skeleton, controller_data['constraints']) if constraints else []
    changes = []
    if ordered:pose, changes = apply_constraints(rig.skeleton, pose, ordered, ordered=True)
    reports = []
    if contacts:
        chains = {c['id']: c for c in controller_data['ik']}
        for contact in clip['contacts']:
            active = contact['start'] <= local_time < contact['end'] or (local_time == duration and contact['end'] == duration)
            if not active:continue
            target = np.array(contact['target'])+(cycles*root_cycle if clip['root_motion'] == 'embedded' else 0)
            result = solve_ik(rig.skeleton, pose, chains[contact['chain']], target,
                              constraints=ordered, orientation=contact.get('orientation'))
            pose = result.pose
            reports.append(dict(id=contact['id'], chain=contact['chain'], target=target.tolist(),
                                residual_m=result.residual_m, orientation_residual_rad=result.orientation_residual_rad,
                                reachable=result.reachable, converged=result.converged))
    root_delta = np.array(pose['bones'].get(root_id, {}).get('translation', root_rest))-root_rest
    return dict(pose=pose, time=local_time, cycle=cycles, root_motion=root_delta.tolist(), contacts=reports,
                constraints_applied=changes, events=[deepcopy(e) for e in clip['events'] if abs(e['time']-local_time) < 1e-7])


def blend_poses(rig: RigAsset, base: dict, overlay: dict, weight: float, *, mask=None,
                additive=False, include_morphs=True) -> dict:
    require(math.isfinite(weight) and 0 <= weight <= 1, 'RIG_BLEND_WEIGHT', 'Blend weight must be within [0,1].')
    result = deepcopy(base);bone_index = {b['id']: b for b in rig.skeleton['bones']}
    require(set(overlay['bones']) <= set(bone_index), 'RIG_POSE_REFERENCE', 'Overlay bone is missing.')
    def selected(name):
        return mask is None or any(name == entry or bone_index[name]['role'] == entry or
                                  bone_index[name]['role'].startswith(entry+'.') for entry in mask)
    for name, values in overlay['bones'].items():
        if not selected(name):continue
        rest = bone_index[name]['rest'];output = result['bones'].setdefault(name, {})
        for path, incoming in values.items():
            current = output.get(path, rest[path])
            if path == 'rotation':
                if additive:
                    delta = qmul(qinverse(rest[path]), incoming)
                    output[path] = quaternion(qmul(current, slerp(IDENTITY_Q, delta, weight))).tolist()
                else:output[path] = slerp(current, incoming, weight).tolist()
            elif additive and path == 'scale':
                output[path] = (np.asarray(current)*(1+weight*(np.asarray(incoming)/rest[path]-1))).tolist()
            elif additive:output[path] = (np.asarray(current)+weight*(np.asarray(incoming)-rest[path])).tolist()
            else:output[path] = (np.asarray(current)*(1-weight)+np.asarray(incoming)*weight).tolist()
    if include_morphs:
        for mesh, weights in overlay.get('morphs', {}).items():
            target = result['morphs'].setdefault(mesh, {})
            for name, amount in weights.items():
                old = target.get(name, rig.geometry.by_id()[mesh].morph_defaults.get(name, 0.))
                target[name] = old+amount*weight if additive else old*(1-weight)+amount*weight
                require(-2 <= target[name] <= 2, 'RIG_MORPH_RANGE', 'Layered morph weights exceed the supported range.')
    return result


def crossfade(rig: RigAsset, source: dict | str, target: dict | str, source_time: float,
              target_time: float, weight: float, *, mask=None, align_root=True) -> dict:
    a = evaluate_animation(rig, source, source_time)['pose'];b = evaluate_animation(rig, target, target_time)['pose']
    if align_root:
        root = rig.skeleton['root'];rest = next(x['rest']['translation'] for x in rig.skeleton['bones'] if x['id'] == root)
        start = evaluate_animation(rig, target, 0)['pose']
        anchor = evaluate_animation(rig, source, max(0., source_time-target_time))['pose']
        offset = np.asarray(anchor['bones'].get(root, {}).get('translation', rest))-start['bones'].get(root, {}).get('translation', rest)
        values = b['bones'].setdefault(root, {});values['translation'] = (np.asarray(values.get('translation', rest))+offset).tolist()
    return blend_poses(rig, a, b, weight, mask=mask)


def clip_from_poses(rig: RigAsset, identifier: str, times: list[float], poses: list[dict], *, fps=24,
                    loop=False, contacts=None, events=None, root_motion='embedded', provenance=None) -> dict:
    require(len(times) == len(poses) and len(times) >= 2 and times[0] == 0 and np.all(np.diff(times) > 0),
            'RIG_POSE_KEYS', 'Pose authoring requires at least two ordered samples beginning at zero.')
    tracks = []
    for bone in rig.skeleton['bones']:
        for path in ('translation', 'rotation', 'scale'):
            values = np.array([pose['bones'].get(bone['id'], {}).get(path, bone['rest'][path]) for pose in poses], dtype=float)
            if path == 'rotation':
                for i in range(1, len(values)):
                    if np.dot(values[i], values[i-1]) < 0:values[i] *= -1
            if np.max(np.abs(values-np.asarray(bone['rest'][path]))) < 1e-9:continue
            tracks.append(dict(schema_version=1, target=bone['id'], path=path, interpolation='LINEAR', times=list(times), values=values.tolist()))
    for mesh in rig.geometry.meshes:
        if not mesh.morphs:continue
        values = [[pose.get('morphs', {}).get(mesh.id, {}).get(name, mesh.morph_defaults.get(name, 0.)) for name in mesh.morphs] for pose in poses]
        if not np.any(values):continue
        tracks.append(dict(schema_version=1, target=mesh.id, path='weights', interpolation='LINEAR', times=list(times), values=values))
    if not tracks:
        bone = rig.skeleton['bones'][0]
        tracks = [dict(schema_version=1, target=bone['id'], path='rotation', interpolation='LINEAR',
                       times=[times[0], times[-1]], values=[bone['rest']['rotation']]*2)]
    if provenance is None:
        from rig_errors import timestamp
        provenance=dict(schema_version=1, tool_version='1.0.0', operation='Authored pose keyframes', created_at=timestamp(), input_hashes={}, ancestry=[])
    result = dict(schema_version=1, kind='hatch-3d-animation', id=identifier, version='1.0.0',
                  rig_id=rig.manifest['id'], skeleton_version=rig.skeleton['version'], duration=float(times[-1]), fps=fps,
                  loop=loop, tracks=tracks, events=deepcopy(events or []), contacts=deepcopy(contacts or []),
                  root_motion=root_motion, interruptible=True, reduced_motion_pose='neutral', provenance=provenance)
    validate_animation(rig, result)
    return result


def bake_clip(rig: RigAsset, clip: dict, *, fps=None) -> dict:
    validate_animation(rig, clip);fps = fps or clip['fps']
    if not clip['contacts'] and not rig.manifest.get('controllers', {}).get('constraints'):
        return deepcopy(clip)  # Preserve imported STEP and cubic tangents exactly.
    require(all(t['interpolation'] != 'STEP' for t in clip['tracks']), 'RIG_BAKE_STEP',
            'A constrained STEP clip requires explicit stepped pose keys; export without baking only after removing live controls.')
    require(type(fps) is int and 1 <= fps <= rig.manifest.get('limits', DEFAULT_LIMITS)['fps'], 'RIG_FRAME_RATE', 'Bake frame rate exceeds the allowed range.')
    samples = max(2, int(math.ceil(clip['duration']*fps))+1)
    require(samples*len(rig.skeleton['bones'])*3 <= rig.manifest.get('limits', DEFAULT_LIMITS)['keyframes'],
            'RIG_LIMIT_KEYFRAMES', 'Estimated bake exceeds its keyframe budget.')
    times = np.linspace(0, clip['duration'], samples).tolist()
    # Contact boundaries and original step transitions are explicit sample points.
    times = sorted(set(times+[x[k] for x in clip['contacts'] for k in ('start', 'end')]))
    poses = [evaluate_animation(rig, clip, t, _validated=True)['pose'] for t in times]
    result = clip_from_poses(rig, clip['id'], times, poses, fps=fps, loop=clip['loop'],
                             events=clip['events'], root_motion=clip['root_motion'], provenance=clip['provenance'])
    result['version'] = clip['version'];result['interruptible'] = clip['interruptible']
    # Contacts are baked to channels. Keeping active contacts here would solve twice.
    return result


def reduce_keys(clip: dict, *, tolerance=1e-4) -> dict:
    require(math.isfinite(tolerance) and tolerance > 0, 'RIG_KEY_TOLERANCE', 'Reduction tolerance must be positive.')
    result = deepcopy(clip)
    for track in result['tracks']:
        if track['interpolation'] != 'LINEAR' or len(track['times']) <= 2:continue
        times = track['times'];values = np.array(track['values']);keep = {0, len(times)-1}
        pending = [(0, len(times)-1)]
        while pending:
            a, b = pending.pop()
            if b-a <= 1:continue
            errors = []
            for i in range(a+1, b):
                t = (times[i]-times[a])/(times[b]-times[a])
                predicted = slerp(values[a], values[b], t) if track['path'] == 'rotation' else values[a]*(1-t)+values[b]*t
                errors.append(qangle(values[i], predicted) if track['path'] == 'rotation' else float(np.max(np.abs(values[i]-predicted))))
            if max(errors) > tolerance:
                i = a+1+int(np.argmax(errors));keep.add(i);pending.extend([(a, i), (i, b)])
        keep = sorted(keep);track['times'] = [times[i] for i in keep];track['values'] = values[keep].tolist()
    return result


def save_pose(path: str | Path, pose: dict) -> None:
    validate_contract(pose, 'Pose');atomic_json(path, pose)


def load_pose(path: str | Path) -> dict:
    return validate_contract(read_json(path), 'Pose')
