"""Discover represented hand anatomy and reuse explicit native pose actuators."""
from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real
import numpy as np
from rig_contracts import validate_contract
from rig_constraints import apply_constraints
from rig_errors import require
from rig_facial import finger_controls
from rig_ik import validate_controllers
from rig_skeleton import hierarchy_order, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin

FINGERS = ('thumb', 'index', 'middle', 'ring', 'pinky')
SUBSTITUTES = ('paw', 'gripper', 'mitten', 'claw')


def discover_hand_controls(rig) -> dict:
    """Report existing weighted semantic anatomy; never infer it from bone IDs.

    A represented substitute is not an aperture/grasp actuator. Only existing
    single-axis prismatic gripper constraints and articulated finger controls
    expose parameterized operations here. Sockets/IK remain native references.
    """
    validate_skeleton(rig.skeleton, rig.manifest['limits'])
    validate_controllers(rig.skeleton, rig.manifest['controllers'])
    bones = {b['id']: b for b in rig.skeleton['bones']}
    geometry = rig.geometry.by_id()
    require(set(rig.weights) <= set(geometry), 'RIG_HAND_SKIN', 'Hand discovery skin references missing geometry.')
    influential = set()
    for mesh_id, skin in rig.weights.items():
        validate_skin(skin, geometry[mesh_id], rig.skeleton, rig.manifest['limits'])
        influential.update(skin.joints[int(j)] for j in skin.indices[skin.weights > 0])
    for name in list(influential):
        parent = bones[name]['parent']
        while parent is not None and parent not in influential:
            influential.add(parent);parent = bones[parent]['parent']
    order = {name:i for i,name in enumerate(hierarchy_order(rig.skeleton))}
    controls = [];finger_anatomy = {}
    for side in ('left', 'right'):
        chains = {}
        for finger in FINGERS:
            chain = sorted((b for b in bones.values() if b['role'].startswith(finger+'.')
                            and b['role'].endswith('.'+side)), key=lambda b:order[b['id']])
            if (len(chain) >= 3 and all(b['id'] in influential for b in chain)
                    and all(b['parent'] == a['id'] for a,b in zip(chain,chain[1:]))):
                chains[finger] = [b['id'] for b in chain]
        finger_anatomy[side] = dict(status='represented' if chains else 'unavailable', chains=chains,
            missing_fingers=[name for name in FINGERS if name not in chains])
        if chains:
            controls.append(dict(id='fingers:'+side, kind='articulated-fingers', side=side,
                fingers=list(chains), joints=[name for chain in chains.values() for name in chain],
                parameters=dict(curl=[0.,1.], spread=[-1.,1.])))
    substitutes = {}
    for kind in SUBSTITUTES:
        joints = [b['id'] for b in bones.values() if b['id'] in influential
                  and (b['role'] == kind or b['role'].startswith(kind+'.'))]
        substitutes[kind] = dict(status='represented' if joints else 'unavailable', joints=joints,
            sockets=[s['id'] for s in rig.manifest['attachments'] if s['bone'] in joints],
            ik_chains=[c['id'] for c in rig.manifest['controllers']['ik'] if c['bones'][-1] in joints],
            reason='Existing weighted semantic anatomy; no finger or grip aperture is inferred.' if joints else
                   'No weighted joint with this explicit semantic role is represented.')
    grippers = set(substitutes['gripper']['joints'])
    for constraint in rig.manifest['controllers']['constraints']:
        axes = constraint.get('axes', [0,1,2])
        if (constraint['bone'] in grippers and constraint['type'] == 'translation'
                and len(axes) == 1 and constraint['minimum'] < constraint['maximum']):
            controls.append(dict(id='slide:'+constraint['id'], kind='prismatic-gripper-travel',
                joint=constraint['bone'], constraint=constraint['id'], axis=axes[0],
                minimum=constraint['minimum'], maximum=constraint['maximum'], parameters=dict(amount=[0.,1.]),
                meaning='Local gripper travel; does not imply jaw closure or object grasp.'))
    return dict(schema_version=1, rig_id=rig.manifest['id'], rig_version=rig.manifest['version'],
        fingers=finger_anatomy, substitutes=substitutes, controls=controls,
        skin_complete=set(rig.weights) == set(geometry),
        grip_aperture=dict(status='unavailable', reason='No dedicated authored aperture actuator is exposed by this API.'),
        limitations=['Discovery proves weighted semantic representation, not useful motion amplitude or visual approval.',
                     'Paw, mitten and claw posing uses ordinary native Pose/IK/socket APIs; no synthetic finger controller is created.'])


def apply_hand_control(rig, pose: dict, control_id: str, **parameters) -> dict:
    """Evaluate an explicitly discovered actuator, returning an ordinary Pose."""
    validate_contract(pose, 'Pose')
    require(pose['rig_version'] == rig.skeleton['version'], 'RIG_POSE_VERSION', 'Hand pose targets a different skeleton version.')
    report = discover_hand_controls(rig)
    control = next((entry for entry in report['controls'] if entry['id'] == control_id), None)
    require(control is not None, 'RIG_HAND_CONTROL', 'No represented native hand actuator has this ID.', asset=control_id)
    require(set(parameters) <= set(control['parameters']), 'RIG_HAND_PARAMETERS', 'Unexpected hand actuator parameter.')
    values = {name:parameters.get(name, 0.) for name in control['parameters']}
    for name,value in values.items():
        low,high = control['parameters'][name]
        require(isinstance(value, Real) and not isinstance(value, (bool,np.bool_)) and low <= value <= high
                and math.isfinite(value), 'RIG_HAND_PARAMETERS', 'Hand actuator parameters must be finite and within their declared ranges.', asset=name)
    if control['kind'] == 'articulated-fingers':
        result = finger_controls(rig, pose, side=control['side'], fingers=control['fingers'], **values)
    else:
        result = deepcopy(pose);bone = next(b for b in rig.skeleton['bones'] if b['id'] == control['joint'])
        transform = result['bones'].setdefault(bone['id'], {})
        translation = list(transform.get('translation', bone['rest']['translation']))
        translation[control['axis']] = bone['rest']['translation'][control['axis']]+control['minimum']+values['amount']*(control['maximum']-control['minimum'])
        transform['translation'] = translation
        result = apply_constraints(rig.skeleton, result, rig.manifest['controllers']['constraints'])[0]
    validate_contract(result, 'Pose')
    evaluate_deformation(rig, result)
    return result
