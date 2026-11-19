"""Capability-checked morph mixing, gaze and articulated appendage controls."""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np
from rig_constraints import apply_constraints
from rig_errors import require
from rig_math import axis_angle, qmatrix, qmul, quaternion, unit
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


def _local_eye_pitch(bone, override, parent_rest):
    """Measure the final rotation of the rest gaze direction in parent space."""
    rest = bone['rest']['rotation']
    posed = override.get('rotation', rest)
    canonical_rest = quaternion(rest)
    # A constraint can normalize a rest quaternion without changing the FK
    # rotation. Exact matrix equality keeps even the smallest distinct motion.
    if (np.array_equal(posed, canonical_rest) or np.array_equal(posed, -canonical_rest)
            or np.array_equal(qmatrix(posed), qmatrix(rest))):
        return 0.

    # Work with directions, whose arbitrary lengths must not trigger the
    # geometry helper's absolute zero-length threshold. Column balancing and
    # exponent-relative reconstruction avoid overflow in inverse scales.
    columns = np.max(np.abs(parent_rest), axis=0)
    require(np.all(columns > 0), 'RIG_SINGULAR_TRANSFORM', 'Eye parent frame must be invertible.')
    balanced = parent_rest / columns
    inverse_forward = np.linalg.solve(balanced, [0., 0., 1.])
    mantissa, exponent = np.frexp(inverse_forward)
    column_mantissa, column_exponent = np.frexp(columns)
    exponent = exponent - column_exponent
    scale = np.max(exponent[inverse_forward != 0])
    forward = unit(np.ldexp(mantissa / column_mantissa, exponent - scale))
    # The inverse Y/Z plane has normal proportional to the first row of the
    # forward matrix. This avoids subtracting nearly parallel inverse rays.
    right = unit(parent_rest[0] / np.max(np.abs(parent_rest[0])))
    sign = np.linalg.slogdet(balanced)[0]
    require(sign != 0, 'RIG_SINGULAR_TRANSFORM', 'Eye parent frame must be invertible.')
    up = unit(np.cross(forward, right)) * sign

    rest, posed = canonical_rest, quaternion(posed)
    scalar = np.dot(posed, rest)
    if scalar < 0:
        posed, scalar = -posed, -scalar
    # The vector part of rest * conjugate(rest) is zero. Subtract rest
    # before multiplying so cancellation cannot erase a small eye rotation.
    relative = qmul(posed - rest, rest * [-1., -1., -1., 1.])
    relative[3] = scalar
    relative = quaternion(relative)
    vector = relative[:3]
    cross = np.cross(vector, forward)
    change = 2 * (relative[3] * cross + np.cross(vector, cross))
    # Project the change: the rest direction has exactly zero elevation.
    vertical = np.dot(change, up)
    horizontal = math.hypot(1 + np.dot(change, forward), np.dot(change, np.cross(up, forward)))
    return math.atan2(vertical, horizontal)


def _gaze_eyelids(rig, pose, bindings, rest):
    """Set owned channels from final constrained eye pitch relative to its parent."""
    # Retain FK validation of all authored pose overrides. Only the local
    # quaternion below contributes to the eye rotation measurement.
    evaluate_fk(rig.skeleton, pose)
    bones = {bone['id']:bone for bone in rig.skeleton['bones']}
    meshes = rig.geometry.by_id()
    for binding in bindings:
        eye = bones[binding['eye']]
        parent = eye['parent']
        parent_rest = rest[parent][:3, :3] if parent else np.eye(3)
        pitch = _local_eye_pitch(eye, pose['bones'].get(eye['id'], {}), parent_rest)
        mesh = meshes[binding['mesh']]
        default = mesh.morph_defaults.get(binding['morph'], 0.)
        end = binding['up_weight'] if pitch >= 0 else binding['down_weight']
        limit = binding['up_radians'] if pitch >= 0 else binding['down_radians']
        amount = min(abs(pitch)/limit, 1.)
        pose['morphs'].setdefault(mesh.id, {})[binding['morph']] = default+(end-default)*amount
    return pose


def refresh_gaze_eyelids(rig, pose: dict) -> dict:
    """Resample gaze-driven morphs after the caller finishes all bone solving."""
    bindings = validate_eyelid_bindings(rig)
    if not bindings:
        return pose
    result = _gaze_eyelids(rig, deepcopy(pose), bindings, evaluate_fk(rig.skeleton))
    for mesh, entries in result.get('gaze_overrides', {}).get('morphs', {}).items():
        for morph, change in entries.items():
            current = pose['morphs'].get(mesh, {}).get(morph)
            if current != change[1]:
                change[0] = current
            change[1] = result['morphs'][mesh][morph]
    return result


def validate_gaze_overrides(rig, pose, bindings=None):
    overrides = pose.get('gaze_overrides')
    if overrides is None:
        return
    eyes = set(rig.manifest['facial']['eyes'])
    heads = [bone['id'] for bone in rig.skeleton['bones'] if bone['role'] == 'head']
    bones = {bone['id'] for bone in rig.skeleton['bones']}
    eligible = (eyes | set(heads if len(heads) == 1 else ())) & bones
    require(set(overrides['rotations']) <= eligible, 'RIG_GAZE_OVERRIDE',
            'Gaze rotation overrides require configured eyes or a unique head joint.')
    for before, applied in overrides['rotations'].values():
        quaternion(applied, strict=True)
        if before is not None:
            quaternion(before, strict=True)
    if overrides['morphs']:
        bindings = rig.manifest['facial'].get('eyelids', ()) if bindings is None else bindings
        owned = {(binding['mesh'], binding['morph']) for binding in bindings}
        require(all((mesh, morph) in owned for mesh, entries in overrides['morphs'].items() for morph in entries),
                'RIG_GAZE_OVERRIDE', 'Gaze morph overrides require an exact eyelid binding.')


def record_constrained_gaze_rotations(before, after):
    """Carry gaze ownership through the final pose constraint pass."""
    rotations = after['gaze_overrides']['rotations']
    for name, change in list(rotations.items()):
        previous = before['bones'].get(name, {}).get('rotation')
        current = after['bones'].get(name, {}).get('rotation')
        if previous == change[1] and current is not None:
            change[1] = deepcopy(current)
        else:
            del rotations[name]


def _before_gaze(current, change):
    return deepcopy(change[0] if change and current == change[1] else current)


def _restore_gaze_value(values, name, change):
    before, applied = change
    if values.get(name) == applied:
        if before is None:
            del values[name]
        else:
            values[name] = before


def _release_gaze(pose):
    result = deepcopy(pose)
    overrides = result.pop('gaze_overrides', None)
    if overrides is None:
        return result
    for bone, change in overrides['rotations'].items():
        values = result['bones'].get(bone)
        if values is not None:
            _restore_gaze_value(values, 'rotation', change)
            if not values:
                del result['bones'][bone]
    for mesh, entries in overrides['morphs'].items():
        values = result['morphs'].get(mesh)
        if values is not None:
            for morph, change in entries.items():
                _restore_gaze_value(values, morph, change)
            if not values:
                del result['morphs'][mesh]
    return result


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
    if 'gaze_overrides' in result:
        # An explicit expression can take over a channel with the same value.
        overrides = result['gaze_overrides']
        affected = list(available.values()) if reset else [available[name] for name in expressions]
        targets = {target for expression in affected for target in expression['morphs']}
        for mesh, entries in list(overrides['morphs'].items()):
            for morph in list(entries):
                if morph in targets or mesh+'::'+morph in targets:
                    del entries[morph]
            if not entries:
                del overrides['morphs'][mesh]
        for expression in affected:
            for bone, values in expression['bones'].items():
                if reset or 'rotation' in values:
                    overrides['rotations'].pop(bone, None)
        if not any(overrides.values()):
            del result['gaze_overrides']
    return result


def gaze_at(rig, pose: dict, target, *, head_follow=0., vergence=True) -> dict:
    require(0 <= head_follow <= 1, 'RIG_GAZE', 'Head follow must be within [0,1].')
    eyes = rig.manifest['facial']['eyes']
    require(eyes, 'RIG_GAZE_UNAVAILABLE', 'This rig has no verified eye joints.')
    eyelids = validate_eyelid_bindings(rig)
    validate_gaze_overrides(rig, pose, eyelids)
    if target is None:
        return _release_gaze(pose)
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
    gaze_bones = [constraint['bone'] for constraint in constraints]
    constraints.extend(deepcopy(rig.manifest['controllers']['constraints']))
    result, _ = apply_constraints(rig.skeleton, pose, constraints)
    if eyelids:
        result = _gaze_eyelids(rig, result, eyelids, rest)
    previous = pose.get('gaze_overrides', {})
    previous_rotations = previous.get('rotations', {})
    rotations = {}
    for name in dict.fromkeys(gaze_bones + list(previous_rotations)):
        current = pose['bones'].get(name, {}).get('rotation')
        change = previous_rotations.get(name)
        if name in gaze_bones or (change and current == change[1]):
            rotations[name] = [_before_gaze(current, change), deepcopy(result['bones'][name]['rotation'])]
    morphs = {}
    for binding in eyelids:
        mesh, morph = binding['mesh'], binding['morph']
        current = pose['morphs'].get(mesh, {}).get(morph)
        change = previous.get('morphs', {}).get(mesh, {}).get(morph)
        morphs.setdefault(mesh, {})[morph] = [_before_gaze(current, change), result['morphs'][mesh][morph]]
    result['gaze_overrides'] = dict(rotations=rotations, morphs=morphs)
    return result


_FINGER_NAMES = ('thumb', 'index', 'middle', 'ring', 'pinky')


def validate_finger_names(fingers):
    require(all(name in _FINGER_NAMES for name in fingers), 'RIG_FINGER_UNAVAILABLE',
            'Choose a thumb, index, middle, ring or pinky digit.')


def finger_bindings(rig, *, side, fingers=None):
    fingers = fingers or _FINGER_NAMES
    validate_finger_names(fingers)
    bindings = {}
    for finger in fingers:
        bones = [bone for bone in rig.skeleton['bones']
                 if bone['role'].startswith(finger+'.') and bone['role'].endswith('.'+side)]
        require(bones, 'RIG_FINGER_UNAVAILABLE', 'This rig has no articulated '+finger+' on '+side+'.')
        bindings[finger] = bones
    return bindings


def finger_controls(rig, pose: dict, *, side: str, curl=0., spread=0., fingers=None) -> dict:
    require(side in ('left', 'right') and 0 <= curl <= 1 and -1 <= spread <= 1,
            'RIG_FINGER_CONTROL', 'Finger controls exceed their bounded ranges.')
    bindings = finger_bindings(rig, side=side, fingers=fingers)
    result = deepcopy(pose)
    order = {name:i for i,name in enumerate(hierarchy_order(rig.skeleton))}
    for finger, bones in bindings.items():
        bones.sort(key=lambda bone:order[bone['id']])
        digit_offset = _FINGER_NAMES.index(finger) - (len(_FINGER_NAMES)-1) / 2
        for j, bone in enumerate(bones):
            rotation = axis_angle([1, 0, 0], curl*1.35)
            if j == 0:rotation = qmul(rotation, axis_angle([0, 0, 1], spread*digit_offset*.10))
            result = rotate_bone(rig.skeleton, result, bone['id'], rotation)
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
        sign = -1 if bone['role'].endswith('.left') else 1
        angle = sign*(wing_flap*.7*math.sin(2*math.pi*time)+wing_fold*.7)
        # rotate_bone composes Rrest * Rdelta * S, so the twist acts after
        # local rest scale. Undo the full rest world (including the parent),
        # then restore local scale to express the authored axis in that space.
        length_axis = (unit(np.asarray(bone['rest']['scale']) *
                           np.linalg.solve(rest_world[bone['id']][:3,:3],
                            np.asarray(bone['tail'])-np.asarray(bone['head'])))
                       if wing_twist else [0,1,0])
        rotation = qmul(axis_angle([0, 0, 1], angle), axis_angle(length_axis, sign*wing_twist*.7))
        result = rotate_bone(rig.skeleton, result, bone['id'], rotation)
    return apply_constraints(rig.skeleton, result, rig.manifest['controllers']['constraints'])[0]
