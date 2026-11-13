"""Semantic, calibrated retargeting with scale adaptation and contact re-solving."""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np
from rig_animation import clip_from_poses, evaluate_animation, validate_animation
from rig_constraints import apply_constraints
from rig_contracts import validate_contract
from rig_errors import require
from rig_math import (axis_angle, decompose, qangle, qinverse, qmatrix, qmul, quaternion,
                      signed_twist, swing_twist, unit)
from rig_skeleton import evaluate_fk, hierarchy_order, neutral_pose


def _roles(rig):
    result = {}
    for bone in rig.skeleton['bones']:
        require(bone['role'] not in result, 'RIG_RETARGET_SEMANTICS', 'Ambiguous anatomical role: '+bone['role'])
        result[bone['role']] = bone['id']
    return result


def build_retarget_profile(source, target, *, partial=False, morph_mapping=None,
                           supported_dofs=None, twist_distribution=None) -> dict:
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
    if supported_dofs is not None:result['supported_dofs'] = deepcopy(supported_dofs)
    if twist_distribution is not None:result['twist_distribution'] = deepcopy(twist_distribution)
    validate_contract(result, 'RetargetProfile')
    _profile_controls(target, result)
    return result


def _profile_controls(target, profile):
    """Admit explicit target-local controls; never infer twist recipients or DOFs."""
    bones = {b['id']: b for b in target.skeleton['bones']}
    mapped = set(profile['mapping'].values())
    dofs = profile.get('supported_dofs', {})
    require(set(dofs) <= set(bones), 'RIG_RETARGET_DOF', 'A supported-DOF entry references a missing target joint.')
    for name, dof in dofs.items():
        require(('axis' in dof) == (dof['rotation'] == 'twist'), 'RIG_RETARGET_DOF',
                'Only a twist rotational DOF requires an explicit rest-local axis.', asset=name)
        if 'axis' in dof:unit(dof['axis'])
    distributions = profile.get('twist_distribution', {})
    require(set(distributions) <= mapped, 'RIG_RETARGET_TWIST',
            'A twist driver must be an explicitly mapped target joint.')
    claimed = set(distributions);axes = {};rest = evaluate_fk(target.skeleton)
    for driver, distribution in distributions.items():
        axis = unit(distribution['axis']);previous = driver;total = 0.
        require(dofs.get(driver, {}).get('rotation', 'free') == 'free', 'RIG_RETARGET_TWIST',
                'A twist driver cannot also restrict its rotational DOF.', asset=driver)
        world_axis = qmatrix(decompose(rest[driver])['rotation']) @ axis
        for recipient in distribution['recipients']:
            name = recipient['bone'];total += recipient['weight']
            require(name in bones and name not in mapped and name not in claimed,
                    'RIG_RETARGET_TWIST', 'Twist recipients must be existing, unmapped, uniquely owned target joints.', asset=name)
            require(bones[name]['parent'] == previous, 'RIG_RETARGET_TWIST',
                    'Twist recipients must form a direct descendant chain in parent order.', asset=name)
            require(dofs.get(name, {}).get('rotation', 'free') == 'free', 'RIG_RETARGET_TWIST',
                    'A twist recipient cannot also restrict its rotational DOF.', asset=name)
            # Collinear intermediate joints preserve the calibrated chain heads.
            separation = rest[name][:3, 3]-rest[driver][:3, 3]
            require(np.linalg.norm(np.cross(separation, world_axis)) <= 1e-6,
                    'RIG_RETARGET_TWIST', 'Twist recipient heads must lie on the authored driver twist axis.', asset=name)
            axes[name] = unit(qmatrix(decompose(rest[name])['rotation']).T @ world_axis)
            claimed.add(name);previous = name
        require(total <= 1.+1e-12, 'RIG_RETARGET_TWIST', 'Twist recipient weights cannot exceed one.', asset=driver)
    return dofs, distributions, axes


def _project_dofs(skeleton, pose, dofs):
    result = deepcopy(pose);bones = {b['id']: b for b in skeleton['bones']}
    for name, dof in dofs.items():
        rest = bones[name]['rest'];values = result['bones'].setdefault(name, {})
        for channel in ('translation', 'scale'):
            incoming = np.asarray(values.get(channel, rest[channel]))
            values[channel] = np.where(dof[channel], incoming, rest[channel]).tolist()
        if dof['rotation'] == 'locked':values['rotation'] = list(rest['rotation'])
        elif dof['rotation'] == 'twist':
            delta = qmul(qinverse(rest['rotation']), values.get('rotation', rest['rotation']))
            values['rotation'] = quaternion(qmul(rest['rotation'], swing_twist(delta, dof['axis'])[1])).tolist()
    return result


def _constrain_dofs(target, pose, dofs):
    projected = _project_dofs(target.skeleton, pose, dofs)
    constrained = apply_constraints(target.skeleton, projected, target.manifest['controllers']['constraints'])[0]
    if not dofs:return constrained
    checked = _project_dofs(target.skeleton, constrained, dofs)
    # Evaluate existing controllers once. Reject any unsupported channel they
    # reintroduce instead of silently overriding either authored contract.
    bones = {b['id']: b for b in target.skeleton['bones']}
    for name in dofs:
        a = checked['bones'][name];b = constrained['bones'].get(name, {})
        require(qangle(a.get('rotation', bones[name]['rest']['rotation']),
                       b.get('rotation', bones[name]['rest']['rotation'])) <= 1e-7
                and all(np.allclose(a[channel], b.get(channel, bones[name]['rest'][channel]), rtol=0, atol=1e-8)
                        for channel in ('translation', 'scale')),
                'RIG_RETARGET_DOF_CONFLICT', 'Target constraints require a channel excluded by supported DOFs.', asset=name)
    return constrained


def retarget_pose(source, target, pose: dict, profile: dict, *, _twist_angles=None) -> dict:
    validate_contract(profile, 'RetargetProfile')
    require(profile['source_rig']==source.manifest['id'] and profile['target_rig']==target.manifest['id'],
            'RIG_RETARGET_ID','Retarget calibration targets different character identities.')
    require(profile['source_skeleton_version'] == source.skeleton['version'] and profile['target_skeleton_version'] == target.skeleton['version'],
            'RIG_RETARGET_VERSION', 'Retarget calibration is stale for these skeleton versions.')
    mapping = profile['mapping'];reverse = {v: k for k, v in mapping.items()}
    require(len(reverse) == len(mapping), 'RIG_RETARGET_MAPPING', 'Multiple source joints cannot silently replace one target joint.')
    src_bones = {b['id']: b for b in source.skeleton['bones']};dst_bones = {b['id']: b for b in target.skeleton['bones']}
    require(set(mapping) <= set(src_bones) and set(reverse) <= set(dst_bones), 'RIG_RETARGET_MAPPING', 'Retarget mapping references missing joints.')
    require(set(profile['orientation_offsets']) == set(mapping), 'RIG_RETARGET_MAPPING',
            'Orientation calibration must cover exactly the mapped source joints.')
    for offset in profile['orientation_offsets'].values():quaternion(offset, strict=True)
    dofs, distributions, axes = _profile_controls(target, profile)
    source_world = evaluate_fk(source.skeleton, pose);result = neutral_pose(target.skeleton, pose['id']+'-retargeted')
    distributed = {}
    for name in hierarchy_order(target.skeleton):
        if name in distributed:
            rest = dst_bones[name]['rest']['rotation']
            result['bones'].setdefault(name, {})['rotation'] = quaternion(qmul(rest, distributed[name])).tolist()
        if name not in reverse:continue
        original = reverse[name];bone = dst_bones[name]
        world_q = qmul(decompose(source_world[original])['rotation'], profile['orientation_offsets'][original])
        parent = bone['parent'];parent_q = decompose(evaluate_fk(target.skeleton, result)[parent])['rotation'] if parent else [0, 0, 0, 1]
        values = result['bones'].setdefault(name, {})
        values['rotation'] = quaternion(qmul(qinverse(parent_q), world_q)).tolist()
        if name in distributions:
            distribution = distributions[name];axis = distribution['axis']
            delta = qmul(qinverse(bone['rest']['rotation']), values['rotation'])
            swing, twist = swing_twist(delta, axis);angle = signed_twist(twist, axis)
            if _twist_angles is not None:
                if name in _twist_angles:
                    angle += round((_twist_angles[name]-angle)/(2*math.pi))*2*math.pi
                _twist_angles[name] = angle
            total = sum(recipient['weight'] for recipient in distribution['recipients'])
            values['rotation'] = quaternion(qmul(bone['rest']['rotation'],
                qmul(swing, axis_angle(axis, angle*max(0., 1.-total))))).tolist()
            for recipient in distribution['recipients']:
                distributed[recipient['bone']] = axis_angle(axes[recipient['bone']], angle*recipient['weight'])
        incoming = pose['bones'].get(original, {})
        if 'translation' in incoming:
            scale = profile['root_scale'] if name == target.skeleton['root'] else bone['length']/src_bones[original]['length']
            delta = np.asarray(incoming['translation'])-src_bones[original]['rest']['translation']
            values['translation'] = (np.asarray(bone['rest']['translation'])+delta*scale).tolist()
        if 'scale' in incoming:
            values['scale'] = (np.asarray(bone['rest']['scale'])*np.asarray(incoming['scale'])/src_bones[original]['rest']['scale']).tolist()
        if name in dofs:
            result = _project_dofs(target.skeleton, result, {name: dofs[name]})
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
    return _constrain_dofs(target, result, dofs)


def retarget_animation(source, target, clip: dict | str, *, profile=None, partial=False) -> dict:
    clip = source.clips[clip] if isinstance(clip, str) else clip
    validate_animation(source, clip)
    profile = profile or build_retarget_profile(source, target, partial=partial)
    times = np.linspace(0, clip['duration'], max(2, int(np.ceil(clip['duration']*clip['fps']))+1)).tolist()
    poses = [];source_error = 0.;twist_angles = {}
    for t in times:
        evaluated = evaluate_animation(source, clip, t, _validated=True)
        source_error = max(source_error, max((r['residual_m'] for r in evaluated['contacts']), default=0.))
        poses.append(retarget_pose(source, target, evaluated['pose'], profile, _twist_angles=twist_angles))
    if clip['loop'] and profile.get('twist_distribution'):
        joints = set(profile['twist_distribution'])
        joints.update(recipient['bone'] for distribution in profile['twist_distribution'].values()
                      for recipient in distribution['recipients'])
        bones = {b['id']: b for b in target.skeleton['bones']}
        require(all(qangle(poses[0]['bones'].get(name, {}).get('rotation', bones[name]['rest']['rotation']),
                           poses[-1]['bones'].get(name, {}).get('rotation', bones[name]['rest']['rotation'])) <= 1e-6
                    for name in joints), 'RIG_RETARGET_TWIST_LOOP',
                'Distributed twist does not close at the loop seam; use a nonlooping clip or closing twist shares.')
    source_chains = {c['id']: c for c in source.manifest['controllers']['ik']}
    target_chains = target.manifest['controllers']['ik'];sr, tr = evaluate_fk(source.skeleton), evaluate_fk(target.skeleton)
    contacts, omissions = [], profile['omissions'].copy()
    for contact in clip['contacts']:
        source_chain = source_chains[contact['chain']]
        mapped = [profile['mapping'].get(n) for n in source_chain['bones']]
        chain = next((c for c in target_chains if c['bones'] == mapped), None)
        if chain is None:
            require(partial, 'RIG_RETARGET_CONTACT', 'Target has no compatible contact chain.');omissions.append(contact['id']);continue
        restricted = {name for name, dof in profile.get('supported_dofs', {}).items()
                      if dof['rotation'] != 'free' or not all(dof['translation']) or not all(dof['scale'])}
        require(not restricted.intersection(chain['bones']), 'RIG_RETARGET_DOF_CONTACT',
                'Contact re-solving cannot silently override a restricted target chain; omit this contact explicitly.')
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
                supported_dofs=deepcopy(profile.get('supported_dofs', {})),
                twist_distribution=deepcopy(profile.get('twist_distribution', {})),
                visual_review_required=True))
