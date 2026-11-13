"""Capability-checked morph mixing, gaze and articulated appendage controls."""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np
from rig_constraints import apply_constraints
from rig_errors import require
from rig_math import axis_angle, qmul, unit
from rig_skeleton import evaluate_fk, hierarchy_order, rotate_bone


def validate_eyelid_bindings(rig):
    """Validate only explicit authored gaze/lid relationships; infer no anatomy."""
    facial = rig.manifest['facial']
    if 'eyelids' not in facial:
        return []
    from rig_contracts import validate_contract
    validate_contract(facial, 'FacialController')
    bindings = facial['eyelids']
    eyes = set(rig.manifest['facial']['eyes'])
    bones = {bone['id'] for bone in rig.skeleton['bones']}
    meshes = rig.geometry.by_id();targets = set()
    for binding in bindings:
        eye, mesh_id, morph = binding['eye'], binding['mesh'], binding['morph']
        require(eye in eyes and eye in bones, 'RIG_EYELID_EYE',
                'An eyelid binding must reference a configured eye joint.', asset=eye)
        require(mesh_id in meshes and morph in meshes[mesh_id].morphs,
                'RIG_EYELID_MORPH', 'An eyelid binding requires its exact authored mesh and morph.', asset=mesh_id)
        require((mesh_id, morph) not in targets, 'RIG_EYELID_CONFLICT',
                'Only one eye may own a gaze-driven morph channel.', asset=mesh_id+'::'+morph)
        targets.add((mesh_id, morph))
        mesh = meshes[mesh_id];delta = mesh.morphs[morph].get('POSITION')
        require(isinstance(delta, np.ndarray) and delta.shape == mesh.positions.shape
                and np.isfinite(delta).all() and np.any(delta != 0),
                'RIG_EYELID_MORPH', 'Gaze-driven eyelids require actual nonzero morph geometry.', asset=mesh_id+'::'+morph)
        default = mesh.morph_defaults.get(morph, 0.)
        require(type(default) in (int, float) and math.isfinite(default) and -2 <= default <= 2,
                'RIG_EYELID_MORPH', 'Authored eyelid default must be finite and within [-2,2].')
        require(binding['up_weight'] != default or binding['down_weight'] != default,
                'RIG_EYELID_MORPH', 'An eyelid binding must change its authored neutral weight in at least one direction.')
    return bindings


def _gaze_eyelids(rig, pose, bindings, rest):
    """Set owned channels from final constrained eye pitch relative to its parent."""
    world = evaluate_fk(rig.skeleton, pose)
    bones = {bone['id']:bone for bone in rig.skeleton['bones']}
    meshes = rig.geometry.by_id()
    for binding in bindings:
        eye = binding['eye'];parent = bones[eye]['parent']
        parent_rest = rest[parent][:3,:3] if parent else np.eye(3)
        parent_world = world[parent][:3,:3] if parent else np.eye(3)
        forward = unit(np.linalg.solve(parent_rest, [0.,0.,1.]))
        up = np.linalg.solve(parent_rest, [0.,1.,0.])
        up = unit(up-forward*np.dot(up,forward))
        eye_forward = np.linalg.solve(rest[eye][:3,:3], [0.,0.,1.])
        direction = unit(np.linalg.solve(parent_world, world[eye][:3,:3] @ eye_forward))
        pitch = math.asin(float(np.clip(np.dot(direction, up), -1., 1.)))
        mesh = meshes[binding['mesh']]
        default = mesh.morph_defaults.get(binding['morph'], 0.)
        end = binding['up_weight'] if pitch >= 0 else binding['down_weight']
        limit = binding['up_radians'] if pitch >= 0 else binding['down_radians']
        amount = min(abs(pitch)/limit, 1.)
        pose['morphs'].setdefault(mesh.id, {})[binding['morph']] = default+(end-default)*amount
    return pose


def apply_expressions(rig, pose: dict, expressions: dict[str, float], *, reset=True) -> dict:
    available = rig.manifest['facial']['expressions']
    require(set(expressions) <= set(available), 'RIG_EXPRESSION_UNAVAILABLE', 'An expression has no authored facial controls.',
            observed=sorted(set(expressions)-set(available)))
    require(all(math.isfinite(v) and 0 <= v <= 1 for v in expressions.values()), 'RIG_EXPRESSION_INTENSITY', 'Expression intensity must be within [0,1].')
    active = {name for name, amount in expressions.items() if amount > 0}
    for name in active:
        require(not set(available[name]['incompatible']) & active, 'RIG_EXPRESSION_CONFLICT', 'Incompatible facial expressions were combined.', asset=name)
    result = deepcopy(pose)
    if reset:
        for target in {m for e in available.values() for m in e['morphs']}:
            qualifier,morph=(target.split('::',1) if '::' in target else (None,target))
            for mesh,weights in result['morphs'].items():
                if qualifier is None or mesh==qualifier:weights.pop(morph,None)
        result['morphs']={mesh:weights for mesh,weights in result['morphs'].items() if weights}
        for bone in {b for e in available.values() for b in e['bones']}:result['bones'].pop(bone,None)
    from rig_animation import blend_poses
    for name, amount in expressions.items():
        expression = available[name]
        for target, weight in expression['morphs'].items():
            qualifier,morph=(target.split('::',1) if '::' in target else (None,target))
            matches = [m for m in rig.geometry.meshes if morph in m.morphs and (qualifier is None or m.id==qualifier)]
            require(matches, 'RIG_EXPRESSION_UNAVAILABLE', 'Expression morph is missing: '+target)
            for mesh in matches:
                weights = result['morphs'].setdefault(mesh.id, {})
                weights[morph] = weights.get(morph, 0.)+weight*amount
                require(abs(weights[morph]) <= 1.5, 'RIG_EXPRESSION_EXCESS', 'Combined expression exceeds its bounded intensity.')
        if expression['bones']:
            overlay = deepcopy(pose);overlay['bones'] = deepcopy(expression['bones']);overlay['morphs'] = {}
            result = blend_poses(rig, result, overlay, amount, additive=True, include_morphs=False)
    return result


def gaze_at(rig, pose: dict, target, *, head_follow=0., vergence=True) -> dict:
    require(0 <= head_follow <= 1, 'RIG_GAZE', 'Head follow must be within [0,1].')
    eyes = rig.manifest['facial']['eyes']
    require(eyes, 'RIG_GAZE_UNAVAILABLE', 'This rig has no verified eye joints.')
    eyelids = validate_eyelid_bindings(rig)
    if target is None:
        result=deepcopy(pose)
        for name in eyes:result['bones'].pop(name,None)
        for binding in eyelids:
            weights = result['morphs'].get(binding['mesh'], {})
            weights.pop(binding['morph'], None)
            if not weights:result['morphs'].pop(binding['mesh'], None)
        if head_follow:
            for bone in rig.skeleton['bones']:
                if bone['role']=='head':result['bones'].pop(bone['id'],None)
        return result
    target = np.asarray(target, dtype=float)
    require(target.shape == (3,) and np.isfinite(target).all(), 'RIG_GAZE', 'Gaze requires a finite spatial target.')
    rest = evaluate_fk(rig.skeleton);world = evaluate_fk(rig.skeleton, pose)
    center = np.mean([world[eye][:3, 3] for eye in eyes], axis=0)
    constraints = []
    if head_follow:
        heads = [b for b in rig.skeleton['bones'] if b['role'] == 'head']
        require(len(heads) == 1, 'RIG_HEAD_UNAVAILABLE', 'Head-follow requires an unambiguous head joint.')
        head = heads[0]['id'];local_forward = np.linalg.inv(rest[head][:3, :3]) @ [0, 0, 1]
        constraints.append(dict(schema_version=1, id='gaze-head', bone=head, type='aim', axis=local_forward.tolist(),
                                point=target.tolist(), influence=head_follow))
    for eye in eyes:
        local_forward = np.linalg.inv(rest[eye][:3, :3]) @ [0, 0, 1]
        point = target if vergence else world[eye][:3, 3]+target-center
        constraints.append(dict(schema_version=1, id='gaze-'+eye, bone=eye, type='aim',
                                axis=local_forward.tolist(), point=point.tolist(), influence=1.))
    constraints.extend(deepcopy(rig.manifest['controllers']['constraints']))
    result, _ = apply_constraints(rig.skeleton, pose, constraints)
    return _gaze_eyelids(rig, result, eyelids, rest) if eyelids else result


def finger_controls(rig, pose: dict, *, side: str, curl=0., spread=0., fingers=None) -> dict:
    require(side in ('left', 'right') and 0 <= curl <= 1 and -1 <= spread <= 1,
            'RIG_FINGER_CONTROL', 'Finger controls exceed their bounded ranges.')
    fingers = fingers or ['thumb', 'index', 'middle', 'ring', 'pinky']
    result = deepcopy(pose)
    found = 0
    order = {name:i for i,name in enumerate(hierarchy_order(rig.skeleton))}
    for i, finger in enumerate(fingers):
        bones = [b for b in rig.skeleton['bones'] if b['role'].startswith(finger+'.') and b['role'].endswith('.'+side)]
        bones.sort(key=lambda bone:order[bone['id']])
        require(bones, 'RIG_FINGER_UNAVAILABLE', 'This rig has no articulated '+finger+' on '+side+'.')
        for j, bone in enumerate(bones):
            rotation = axis_angle([1, 0, 0], curl*1.35)
            if j == 0:rotation = qmul(rotation, axis_angle([0, 0, 1], spread*(i-(len(fingers)-1)/2)*.10))
            result = rotate_bone(rig.skeleton, result, bone['id'], rotation);found += 1
    return apply_constraints(rig.skeleton, result, rig.manifest['controllers']['constraints'])[0]


EXPRESSION_ALIASES={
    'smile':['smile','mouthSmile'],'sadness':['sad','frown','mouthFrown'],'anger':['angry','anger'],
    'surprise':['surprise','surprised'],'fear':['fear','afraid'],'squint':['squint','eyeSquint'],
    'blink':['blink','eyeBlink'],'wink-left':['winkLeft','blinkLeft','eyeBlinkLeft'],
    'wink-right':['winkRight','blinkRight','eyeBlinkRight'],'brow-raise':['browRaise','browInnerUp'],
    'brow-lower':['browLower','browDown'],'jaw-open':['jawOpen'],'jaw-close':['jawClose'],
    'mouth-corner-left':['mouthSmileLeft','mouthCornerLeft'],'mouth-corner-right':['mouthSmileRight','mouthCornerRight']}
VISEMES=('sil','PP','FF','TH','DD','kk','CH','SS','nn','RR','aa','E','ih','oh','ou')


def expression_catalog(rig):
    """Map existing labelled geometry only; unavailable semantic controls stay explicit."""
    import re
    normalize=lambda name:re.sub('[^a-z0-9]','',name.lower())
    morphs={normalize(name):name for mesh in rig.geometry.meshes for name in mesh.morphs}
    controls={'neutral':dict(schema_version=1,morphs={},bones={},incompatible=[])};availability={}
    aliases={**EXPRESSION_ALIASES,**{'viseme-'+v:[v,'viseme_'+v,'vrc.v_'+v] for v in VISEMES}}
    for semantic,names in aliases.items():
        target=next((morphs[normalize(n)] for n in names if normalize(n) in morphs),None)
        if target:
            controls[semantic]=dict(schema_version=1,morphs={target:1.},bones={},incompatible=[])
            availability[semantic]=dict(status='available',control=target,reason='Existing authored morph label.')
        else:availability[semantic]=dict(status='unavailable',control=None,reason='No authored matching morph; no geometry or expression is invented.')
    return dict(schema_version=1,controls=controls,availability=availability,visemes=list(VISEMES),automatic_geometry_generation=False)


def create_morph_target(mesh,name,*,deltas=None,positions=None,replace=False):
    from rig_geometry import vertex_normals
    require(isinstance(name,str) and name and len(name)<=128,'RIG_MORPH_NAME','Morph target needs a bounded name.')
    require((deltas is None)!=(positions is None),'RIG_MORPH_INPUT','Provide either vertex deltas or absolute target positions.')
    require(replace or name not in mesh.morphs,'RIG_MORPH_EXISTS','Use an explicit replacement to edit an existing target.')
    value=np.asarray(deltas if deltas is not None else np.asarray(positions)-mesh.positions,dtype=float)
    require(value.shape==mesh.positions.shape and np.isfinite(value).all(),'RIG_MORPH_TOPOLOGY','Morph coordinates must match the exact base topology.')
    result=deepcopy(mesh);normal=vertex_normals(mesh.positions,mesh.triangles);changed=vertex_normals(mesh.positions+value,mesh.triangles)
    result.morphs[name]={'POSITION':value.copy(),'NORMAL':changed-normal};return result


def edit_morph_target(mesh,name,assignments):
    require(name in mesh.morphs,'RIG_MORPH_MISSING','Morph target does not exist.')
    values=mesh.morphs[name]['POSITION'].copy()
    for index,delta in assignments.items():
        require(type(index) is int and 0<=index<len(values),'RIG_VERTEX_INDEX','Morph vertex is out of range.')
        vector=np.asarray(delta,dtype=float);require(vector.shape==(3,) and np.isfinite(vector).all(),'RIG_MORPH_INPUT','Morph edit needs a finite XYZ delta.')
        values[index]=vector
    return create_morph_target(mesh,name,deltas=values,replace=True)


def appendage_controls(rig, pose: dict, *, time=0., tail_curl=0., tail_wag=0.,
                        wing_flap=0., wing_fold=0., wing_twist=0., wing_side='both',
                        seed=0, secondary=0.) -> dict:
    require(math.isfinite(time) and 0 <= time <= 120 and all(math.isfinite(v) and abs(v) <= 1 for v in
            (tail_curl, tail_wag, wing_flap, wing_fold, wing_twist, secondary)), 'RIG_APPENDAGE_RANGE', 'Appendage control is outside its bounded range.')
    require(wing_side in ('both','left','right'), 'RIG_WING_SIDE', 'Wing side must be both, left or right.')
    result = deepcopy(pose)
    tails = [b for b in rig.skeleton['bones'] if b['role'].startswith('tail.')]
    order = {name:i for i,name in enumerate(hierarchy_order(rig.skeleton))}
    tails.sort(key=lambda bone:order[bone['id']])
    wings = [b for b in rig.skeleton['bones'] if b['role'].startswith('wing-')
             and (wing_side == 'both' or b['role'].endswith('.'+wing_side))]
    require(not (tail_curl or tail_wag or secondary) or tails, 'RIG_TAIL_UNAVAILABLE', 'Character has no tail chain.')
    require(not (wing_flap or wing_fold or wing_twist) or wings, 'RIG_WING_UNAVAILABLE', 'Character has no requested wing joints.')
    phase = (int(seed) % 65536)*.013
    rest_world = evaluate_fk(rig.skeleton) if wing_twist else {}
    for i, bone in enumerate(tails):
        angle = tail_curl*.65+tail_wag*.38*math.sin(2*math.pi*time-i*.45)
        angle += secondary*.15*math.sin(time*7-i*.6+phase)*math.exp(-.35*time)
        result = rotate_bone(rig.skeleton, result, bone['id'], qmul(axis_angle([1, 0, 0], tail_curl*.4), axis_angle([0, 0, 1], angle)))
    for bone in wings:
        sign = -1 if bone['id'].endswith('.left') else 1
        angle = sign*(wing_flap*.7*math.sin(2*math.pi*time)+wing_fold*.7)
        # Imported joint frames need not point local +Y along the authored
        # bone. Derive the length axis from rest geometry in that joint frame.
        length_axis = (unit(np.linalg.solve(rest_world[bone['id']][:3,:3],
                            np.asarray(bone['tail'])-np.asarray(bone['head'])))
                       if wing_twist else [0,1,0])
        rotation = qmul(axis_angle([0, 0, 1], angle), axis_angle(length_axis, sign*wing_twist*.7))
        result = rotate_bone(rig.skeleton, result, bone['id'], rotation)
    return apply_constraints(rig.skeleton, result, rig.manifest['controllers']['constraints'])[0]
