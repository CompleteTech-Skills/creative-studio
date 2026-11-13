"""Versioned, hash-bound two-hand grip recipes using the native grasp solver.

Recipe transforms are rig-root/world meters. Object-relative grip transforms are
rigid. Solving is transient and never activates or transfers prop ownership.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import numpy as np

from rig_attachments import prop_world, socket_world, solve_grasp
from rig_contracts import definitions, obj, arr, validate_contract
from rig_errors import finite_json, object_hash, require
from rig_math import decompose, qangle, quaternion, transform_matrix
from rig_models import rig_signature
from rig_skeleton import evaluate_fk
from rig_ik import validate_controllers


@lru_cache(maxsize=1)
def grip_pose_schema():
    """Return the standalone strict JSON schema for reusable grip-pose v1."""
    fields = definitions()['Rig']['properties']
    bone = definitions()['Bone']['properties']
    identifier = bone['id'];version = fields['version'];transform = bone['rest']
    digest = dict(type='string', pattern='^[a-f0-9]{64}$')
    return {'$schema':'https://json-schema.org/draft/2020-12/schema',
        '$id':'https://gethatch.pet/schemas/rig-grip-pose-v1', 'title':'GripPose v1',
        **obj(dict(schema_version={'const':1}, kind={'const':'hatch-3d-grip-pose'},
            id=identifier, version=version, rig_id=identifier, rig_version=version,
            rig_sha256=digest, object=obj(dict(id=identifier,
                expected_owner={'anyOf':[identifier,{'type':'null'}]}, transform=transform)),
            grips=arr(obj(dict(id=identifier, chain=identifier, socket=identifier,
                               transform=transform)),2,2),
            position_tolerance_m=dict(type='number',exclusiveMinimum=0,maximum=.1),
            orientation_tolerance_rad=dict(type='number',exclusiveMinimum=0,maximum=.1)))}


def validate_grip_pose(rig, recipe):
    """Admit exact rig revision, rigid authored grips, and independent limb chains."""
    from jsonschema import Draft202012Validator
    finite_json(recipe)
    error=next(Draft202012Validator(grip_pose_schema()).iter_errors(recipe),None)
    require(error is None,'RIG_GRIP_SCHEMA',error.message if error else '',
            asset='.'.join(map(str,error.absolute_path)) if error else None)
    require(recipe['rig_id']==rig.manifest['id'] and recipe['rig_version']==rig.manifest['version']
            and recipe['rig_sha256']==rig_signature(rig.manifest),
            'RIG_STALE_GRIP','Grip pose targets a different rig revision; explicitly reauthor it.')
    require(recipe['object']['expected_owner'] in (None,rig.manifest['character']['id']),
            'RIG_GRIP_OWNER','A grip recipe can expect an unowned prop or this character, not another owner.')
    evaluate_fk(rig.skeleton)
    validate_controllers(rig.skeleton,rig.manifest['controllers'])
    sockets={s['id']:s for s in rig.manifest['attachments']}
    chains={c['id']:c for c in rig.manifest['controllers']['ik']}
    grips=recipe['grips']
    for field in ('id','chain','socket'):
        require(len({grip[field] for grip in grips})==2,'RIG_GRIP_DUPLICATE',
                'Two-hand grips require distinct grip IDs, chains and sockets.',asset=field)
    for transform in [recipe['object']['transform']]+[grip['transform'] for grip in grips]:
        quaternion(transform['rotation'],strict=True)
        require(np.allclose(transform['scale'],[1,1,1],rtol=0,atol=1e-12),
                'RIG_GRIP_SCALE','Two-hand object and grip transforms must be rigid unit-scale transforms.')
    for grip in grips:
        require(grip['socket'] in sockets and grip['chain'] in chains,'RIG_GRIP_REFERENCE',
                'Each authored grip needs an existing socket and IK chain.',asset=grip['id'])
        socket=sockets[grip['socket']];chain=chains[grip['chain']]
        require(chain['bones'][-1]==socket['bone'],'RIG_GRIP_REFERENCE',
                'Each hand socket must belong to its IK chain endpoint.',asset=grip['id'])
        require(np.allclose(socket['transform']['scale'],[1,1,1],rtol=0,atol=1e-12),
                'RIG_GRIP_SCALE','Two-hand sockets must use rigid unit-scale transforms.',asset=grip['socket'])
    bones={b['id']:b for b in rig.skeleton['bones']}
    first,second=(set(chains[g['chain']]['bones']) for g in grips)
    require(not first & second,'RIG_GRIP_COUPLED','Two-hand grip solving requires disjoint limb chains.')
    for grip,other in zip(grips,(second,first)):
        ancestor=bones[chains[grip['chain']]['bones'][0]]['parent']
        while ancestor is not None:
            require(ancestor not in other,'RIG_GRIP_COUPLED',
                    'One hand chain cannot descend from the other; coupled limbs need a separate authored workflow.')
            ancestor=bones[ancestor]['parent']
    return recipe


def create_grip_pose(rig, identifier, version, prop, grips, *, expected_owner=None,
                     object_transform=None, position_tolerance_m=.001,
                     orientation_tolerance_rad=1e-5):
    """Build a reusable versioned recipe; caller supplies the intended owner explicitly."""
    require(isinstance(prop,dict) and {'id','owner','transform'}<=set(prop),
            'RIG_GRIP_OBJECT','Provide explicit prop identity, owner and transform metadata.')
    require(prop.get('owner')==expected_owner,'RIG_GRIP_OWNER',
            'Prop ownership must match the explicitly expected owner before authoring.')
    transform=object_transform if object_transform is not None else prop.get('transform')
    require(expected_owner is None or object_transform is not None,'RIG_GRIP_OBJECT',
            'An already attached prop needs its explicit current world transform.')
    recipe=dict(schema_version=1,kind='hatch-3d-grip-pose',id=identifier,version=version,
        rig_id=rig.manifest['id'],rig_version=rig.manifest['version'],rig_sha256=rig_signature(rig.manifest),
        object=dict(id=prop.get('id'),expected_owner=expected_owner,transform=deepcopy(transform)),
        grips=deepcopy(grips),position_tolerance_m=position_tolerance_m,
        orientation_tolerance_rad=orientation_tolerance_rad)
    return validate_grip_pose(rig,recipe)


def solve_grip_pose(rig, pose, prop, recipe, *, require_success=True):
    """Solve both hands, then measure both final sockets together; never publish changes."""
    validate_grip_pose(rig,recipe);validate_contract(pose,'Pose')
    require(pose['rig_version']==rig.skeleton['version'],'RIG_POSE_VERSION',
            'Grip input pose targets a different skeleton version.')
    require(type(require_success) is bool,'RIG_GRIP_SCHEMA','require_success must be a boolean.')
    require(isinstance(prop,dict) and {'id','owner','transform'}<=set(prop),
            'RIG_GRIP_OBJECT','Provide explicit prop identity, owner and transform metadata.')
    require(prop.get('owner') is None or isinstance(prop.get('socket'),str),
            'RIG_GRIP_OBJECT','An owned prop requires its authored attachment socket.')
    require(prop.get('id')==recipe['object']['id'] and prop.get('owner')==recipe['object']['expected_owner'],
            'RIG_GRIP_OWNER','Prop identity or ownership changed since this grip recipe was authored.')
    object_transform=recipe['object']['transform'];object_matrix=transform_matrix(object_transform)
    require(np.allclose(prop_world(rig,pose,prop),object_matrix,rtol=0,atol=1e-8),
            'RIG_STALE_GRIP_OBJECT','Prop world transform changed; explicitly reauthor the grip recipe.')
    evaluate_fk(rig.skeleton,pose)
    result=deepcopy(pose);attempts={}
    ordered=sorted(recipe['grips'],key=lambda grip:(grip['chain'],grip['socket'],grip['id']))
    for grip in ordered:
        attempt=solve_grasp(rig,result,grip['chain'],grip['socket'],object_transform,grip['transform'])
        result=attempt['pose'];attempts[grip['id']]=attempt
    contacts=[]
    for grip in ordered:
        desired=object_matrix @ transform_matrix(grip['transform'])
        actual=socket_world(rig,result,grip['socket'])
        position=float(np.linalg.norm(actual[:3,3]-desired[:3,3]))
        angular=qangle(decompose(actual)['rotation'],decompose(desired)['rotation'])
        reachable=attempts[grip['id']]['reachable']
        contacts.append(dict(id=grip['id'],chain=grip['chain'],socket=grip['socket'],
            residual_m=position,orientation_residual_rad=angular,reachable=reachable,
            converged=bool(reachable and position<=recipe['position_tolerance_m']
                           and angular<=recipe['orientation_tolerance_rad']),
            grip_world=desired.tolist(),hand_world=actual.tolist()))
    report=dict(schema_version=1,recipe_id=recipe['id'],recipe_version=recipe['version'],
        recipe_sha256=object_hash(recipe),rig_sha256=recipe['rig_sha256'],object_id=prop['id'],owner=prop.get('owner'),
        ownership_changed=False,contacts=contacts,converged=all(c['converged'] for c in contacts),
        max_residual_m=max(c['residual_m'] for c in contacts),
        max_orientation_residual_rad=max(c['orientation_residual_rad'] for c in contacts),
        visual_review_required=True)
    require(not require_success or report['converged'],'RIG_TWO_HAND_GRIP',
            'Both hand sockets must simultaneously reach their authored object grips.',observed=report)
    return dict(pose=result,prop=deepcopy(prop),report=report)
