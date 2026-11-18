"""Semantic, calibrated retargeting with scale adaptation and contact re-solving."""
from __future__ import annotations

from copy import deepcopy
import numpy as np
from rig_animation import clip_from_poses, evaluate_animation, validate_animation
from rig_constraints import apply_constraints
from rig_contracts import validate_contract
from rig_errors import require
from rig_math import decompose, qinverse, qmul, quaternion
from rig_skeleton import evaluate_fk, hierarchy_order, neutral_pose


def _roles(rig):
    result = {}
    for bone in rig.skeleton['bones']:
        require(bone['role'] not in result, 'RIG_RETARGET_SEMANTICS', 'Ambiguous anatomical role: '+bone['role'])
        result[bone['role']] = bone['id']
    return result


def build_retarget_profile(source, target, *, partial=False, morph_mapping=None) -> dict:
    require(source.manifest['archetype'] == target.manifest['archetype'] or partial,
            'RIG_RETARGET_ARCHETYPE', 'Different archetypes need an explicit partial semantic mapping; no automatic cross-species retarget.')
    src, dst = _roles(source), _roles(target)
    omissions = sorted(set(src)-set(dst))
    require(not omissions or partial, 'RIG_RETARGET_MISSING_ROLE', 'Target lacks required anatomical roles.', observed=omissions)
    mapping = {name: dst[role] for role, name in src.items() if role in dst}
    require(source.skeleton['root'] in mapping and mapping[source.skeleton['root']] == target.skeleton['root'],
            'RIG_RETARGET_ROOT', 'Retarget requires a semantic root correspondence.')
    sw, tw = evaluate_fk(source.skeleton), evaluate_fk(target.skeleton)
    offsets = {a: quaternion(qmul(qinverse(decompose(sw[a])['rotation']), decompose(tw[b])['rotation'])).tolist() for a, b in mapping.items()}
    source_height = max(m.positions[:, 1].max() for m in source.geometry.meshes)-min(m.positions[:, 1].min() for m in source.geometry.meshes)
    target_height = max(m.positions[:, 1].max() for m in target.geometry.meshes)-min(m.positions[:, 1].min() for m in target.geometry.meshes)
    require(source_height > 1e-6 and target_height > 1e-6, 'RIG_RETARGET_SCALE', 'Retarget requires measurable character height.')
    result = dict(schema_version=1, kind='hatch-3d-retarget-profile', id=source.manifest['id']+'-to-'+target.manifest['id'],
        source_rig=source.manifest['id'], target_rig=target.manifest['id'],
        source_skeleton_version=source.skeleton['version'], target_skeleton_version=target.skeleton['version'],
        mapping=mapping, root_scale=float(target_height/source_height), orientation_offsets=offsets,
        omissions=omissions, morph_mapping=deepcopy(morph_mapping or {}))
    validate_contract(result, 'RetargetProfile');return result


def retarget_pose(source, target, pose: dict, profile: dict) -> dict:
    validate_contract(profile, 'RetargetProfile')
    require(profile['source_rig']==source.manifest['id'] and profile['target_rig']==target.manifest['id'],
            'RIG_RETARGET_ID','Retarget calibration targets different character identities.')
    require(profile['source_skeleton_version'] == source.skeleton['version'] and profile['target_skeleton_version'] == target.skeleton['version'],
            'RIG_RETARGET_VERSION', 'Retarget calibration is stale for these skeleton versions.')
    mapping = profile['mapping'];reverse = {v: k for k, v in mapping.items()}
    require(len(reverse) == len(mapping), 'RIG_RETARGET_MAPPING', 'Multiple source joints cannot silently replace one target joint.')
    src_bones = {b['id']: b for b in source.skeleton['bones']};dst_bones = {b['id']: b for b in target.skeleton['bones']}
    require(set(mapping) <= set(src_bones) and set(reverse) <= set(dst_bones), 'RIG_RETARGET_MAPPING', 'Retarget mapping references missing joints.')
    source_world = evaluate_fk(source.skeleton, pose);result = neutral_pose(target.skeleton, pose['id']+'-retargeted')
    for name in hierarchy_order(target.skeleton):
        if name not in reverse:continue
        original = reverse[name];bone = dst_bones[name]
        world_q = qmul(decompose(source_world[original])['rotation'], profile['orientation_offsets'][original])
        parent = bone['parent'];parent_q = decompose(evaluate_fk(target.skeleton, result)[parent])['rotation'] if parent else [0, 0, 0, 1]
        values = result['bones'].setdefault(name, {})
        values['rotation'] = quaternion(qmul(qinverse(parent_q), world_q)).tolist()
        incoming = pose['bones'].get(original, {})
        if 'translation' in incoming:
            scale = profile['root_scale'] if name == target.skeleton['root'] else bone['length']/src_bones[original]['length']
            delta = np.asarray(incoming['translation'])-src_bones[original]['rest']['translation']
            values['translation'] = (np.asarray(bone['rest']['translation'])+delta*scale).tolist()
        if 'scale' in incoming:
            values['scale'] = (np.asarray(bone['rest']['scale'])*np.asarray(incoming['scale'])/src_bones[original]['rest']['scale']).tolist()
    assigned_morphs = set()
    for source_mesh, weights in pose.get('morphs', {}).items():
        for name, amount in weights.items():
            qualified = source_mesh+'::'+name
            mapped = profile['morph_mapping'].get(qualified, profile['morph_mapping'].get(name))
            if mapped is not None:
                qualifier, morph = mapped.split('::', 1) if '::' in mapped else (None, mapped)
                matches = [mesh for mesh in target.geometry.meshes if morph in mesh.morphs
                           and (qualifier is None or mesh.id == qualifier)]
                require(matches, 'RIG_RETARGET_MORPH', 'Explicit morph mapping has no target.', asset=mapped)
            else:
                morph = name
                matches = [mesh for mesh in target.geometry.meshes if morph in mesh.morphs]
                same_mesh = [mesh for mesh in matches if mesh.id == source_mesh]
                if same_mesh:matches = same_mesh
            require(len(matches) <= 1, 'RIG_RETARGET_MORPH',
                    'Morph target is ambiguous; use a mesh::morph mapping.', asset=qualified)
            for mesh in matches:
                key = (mesh.id, morph)
                require(key not in assigned_morphs, 'RIG_RETARGET_MORPH',
                        'Multiple source morphs cannot overwrite one target control.', asset=mesh.id+'::'+morph)
                assigned_morphs.add(key)
                result['morphs'].setdefault(mesh.id, {})[morph] = amount
    return apply_constraints(target.skeleton, result, target.manifest['controllers']['constraints'])[0]


def retarget_animation(source, target, clip: dict | str, *, profile=None, partial=False) -> dict:
    clip = source.clips[clip] if isinstance(clip, str) else clip
    validate_animation(source, clip)
    profile = profile or build_retarget_profile(source, target, partial=partial)
    times = np.linspace(0, clip['duration'], max(2, int(np.ceil(clip['duration']*clip['fps']))+1)).tolist()
    poses = [];source_error = 0.
    for t in times:
        evaluated = evaluate_animation(source, clip, t, _validated=True)
        source_error = max(source_error, max((r['residual_m'] for r in evaluated['contacts']), default=0.))
        poses.append(retarget_pose(source, target, evaluated['pose'], profile))
    source_chains = {c['id']: c for c in source.manifest['controllers']['ik']}
    target_chains = target.manifest['controllers']['ik'];sr, tr = evaluate_fk(source.skeleton), evaluate_fk(target.skeleton)
    contacts, omissions = [], profile['omissions'].copy()
    for contact in clip['contacts']:
        source_chain = source_chains[contact['chain']]
        mapped = [profile['mapping'].get(n) for n in source_chain['bones']]
        chain = next((c for c in target_chains if c['bones'] == mapped), None)
        if chain is None:
            require(partial, 'RIG_RETARGET_CONTACT', 'Target has no compatible contact chain.');omissions.append(contact['id']);continue
        before, after = sr[source_chain['bones'][-1]][:3, 3], tr[chain['bones'][-1]][:3, 3]
        value = deepcopy(contact);value['chain'] = chain['id']
        value['target'] = (after+(np.asarray(contact['target'])-before)*profile['root_scale']).tolist()
        if 'orientation' in contact:
            # Contact orientations are world-space goals. Calibrate the
            # authored goal like the endpoint rotation instead of replacing
            # it with the target's rest orientation. Positional-only contacts
            # must remain free to inherit the animated endpoint orientation.
            endpoint = source_chain['bones'][-1]
            value['orientation'] = quaternion(qmul(contact['orientation'],
                profile['orientation_offsets'][endpoint])).tolist()
        contacts.append(value)
    result = clip_from_poses(target, clip['id']+'-retargeted', times, poses, fps=clip['fps'], loop=clip['loop'],
        contacts=contacts, events=clip['events'], root_motion=clip['root_motion'], provenance=clip['provenance'])
    target_error = 0.
    for t in times:
        evaluated = evaluate_animation(target, result, t, _validated=True)
        target_error = max(target_error, max((r['residual_m'] for r in evaluated['contacts']), default=0.))
    return dict(clip=result, profile=profile, report=dict(source_contact_max_error_m=source_error,
                target_contact_max_error_m=target_error, root_scale=profile['root_scale'], omissions=omissions,
                visual_review_required=True))
