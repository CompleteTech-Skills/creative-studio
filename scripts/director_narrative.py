"""Author the five-shot laboratory performance through native motion owners.

This is a fixed procedural engineering narrative, not a planning provider or a
new scene/animation evaluator. Source assets and earlier films remain immutable.
"""
from __future__ import annotations

from copy import deepcopy
import math

import numpy as np

from rig_animation import blend_poses, clip_from_poses
from rig_attachments import solve_grasp, socket_world
from rig_errors import require
from rig_facial import finger_controls, gaze_at
from rig_ik import solve_ik
from rig_math import axis_angle, decompose, transform_matrix
from rig_skeleton import evaluate_fk, neutral_pose

ACTORS = ('aster', 'moss')
FPS = 24
FRAMES = 720
SHOTS = (('entry', 0, 120), ('exploration', 120, 264),
         ('discovery', 264, 432), ('cooperation', 432, 576),
         ('resolution', 576, 720))
PICKUP = 360
HANDOFF = 528
CONTACT_START, CONTACT_END = 516, 540
STRIDE = .38
DISTANCE = 8 * STRIDE
SIDES = {'aster': 'left', 'moss': 'right'}
FINAL_ROOTS = {'aster': [-.65, -.005, -1.15], 'moss': [.60, -.00435, -1.05]}
GRIPS = {
    'aster': dict(translation=[-.09, 0., 0.],
                  rotation=axis_angle([0, 0, 1], -math.pi/2).tolist(), scale=[1., 1., 1.]),
    'moss': dict(translation=[.09, 0., 0.],
                 rotation=axis_angle([0, 0, 1], math.pi/2).tolist(), scale=[1., 1., 1.]),
}


def ease(value):
    value = min(1., max(0., value))
    return value * value * (3. - 2. * value)


def _linear_keys(keys, frame):
    """Authoring trajectory only; published channels use native interpolation."""
    for (a, first), (b, second) in zip(keys, keys[1:]):
        if frame <= b:
            return np.asarray(first) + (np.asarray(second)-first) * ease((frame-a)/(b-a))
    return np.asarray(keys[-1][1], dtype=float)


def root_transform(actor, frame):
    require(actor in ACTORS and type(frame) is int and 0 <= frame <= FRAMES,
            'DIRECTOR_NARRATIVE_FRAME', 'Select a known actor and exact boundary 0..720.')
    position = list(FINAL_ROOTS[actor])
    position[2] += DISTANCE * (1. - ease(frame/312))
    return dict(translation=position, rotation=axis_angle([0, 1, 0], math.pi).tolist(),
                scale=[1., 1., 1.])


def key_transform(frame):
    require(type(frame) is int and 0 <= frame <= FRAMES,
            'DIRECTOR_NARRATIVE_FRAME', 'Select an exact boundary 0..720.')
    position = _linear_keys([(0, [-.50, 1.12, -1.64]),
                            (PICKUP, [-.50, 1.12, -1.64]),
                            (396, [-.35, 1.24, -1.50]),
                            (456, [-.20, 1.15, -1.35]),
                            (CONTACT_START, [0., 1.15, -1.35]),
                            (CONTACT_END, [0., 1.15, -1.35]),
                            (576, [.32, 1.20, -1.40]),
                            (600, [.32, 1.20, -1.40]),
                            (648, [.32, 1.40, -1.40]),
                            (FRAMES, [.32, 1.40, -1.40])], frame)
    return dict(translation=position.tolist(), rotation=[0., 0., 0., 1.], scale=[1., 1., 1.])


def world_pose(rig, pose, actor_matrix):
    """Explicit temporary adapter for native socket/contact validation.

    Stored clips remain in rig-local space. Scene3D owns actor placement.
    """
    result = deepcopy(pose)
    root = rig.skeleton['root']
    result['bones'][root] = decompose(actor_matrix @ evaluate_fk(rig.skeleton, pose)[root])
    return result


def _foot_offset(progress, side, frame):
    phase = (progress / STRIDE + (.5 if side == 'right' else 0.)) % 1.
    z = STRIDE*.25 - STRIDE*phase
    lift = 0.
    if phase >= .5:
        swing = 2*(phase-.5)
        z += STRIDE*ease(swing)
        lift = .055*math.sin(math.pi*swing)**2
    if frame >= 312:
        # Root stops at an exact complete stride. Settle each foot through a
        # lifted step; never slide a declared planted foot into the neutral pose.
        if side == 'right':
            swing = min(1., max(0., (frame-312)/12))
            z = -.25*STRIDE*(1.-ease(swing))
        else:
            swing = min(1., max(0., (frame-324)/12))
            z = .25*STRIDE*(1.-ease(swing))
        lift = .055*math.sin(math.pi*swing)**2
    return z, lift


def prepare_rigs(rigs):
    """Use existing unambiguous eye bones; do not invent facial geometry."""
    require(set(rigs) == set(ACTORS), 'DIRECTOR_NARRATIVE_ACTORS',
            'Exactly the two persistent native robot identities are required.')
    for actor, rig in rigs.items():
        require(rig.manifest['character']['id'] == actor, 'DIRECTOR_NARRATIVE_IDENTITY',
                'Native character identity differs from the selected actor.')
        eyes = [bone['id'] for bone in rig.skeleton['bones']
                if bone['role'] in ('eye.left', 'eye.right')]
        require(len(eyes) == 2, 'DIRECTOR_NARRATIVE_GAZE',
                'Independent gaze needs both actual native eye joints.')
        rig.manifest['facial']['eyes'] = eyes


def foot_planted(frame, side):
    require(type(frame) is int and 0 <= frame <= FRAMES and side in ('left', 'right'),
            'DIRECTOR_NARRATIVE_FOOT', 'Select an exact foot/frame.')
    if frame < 312:
        phase = (DISTANCE*ease(frame/312)/STRIDE + (.5 if side == 'right' else 0.)) % 1.
        return phase < .5
    return not ((312 <= frame < 324) if side == 'right' else (324 <= frame < 336))


def native_events(actor):
    """Record the actual baked foot/contact and single-owner transfer markers."""
    require(actor in ACTORS, 'DIRECTOR_NARRATIVE_ACTORS', 'Unknown narrative actor.')
    events = []
    for side in ('left', 'right'):
        previous = None
        for frame in range(FRAMES+1):
            planted = foot_planted(frame, side)
            if planted != previous:
                events.append(dict(schema_version=1, time=frame/FPS,
                    type='foot_contact' if planted else 'foot_release',
                    target='foot.'+side, value='world-floor-baked-native-IK'))
            previous = planted
    for frame, event_type, label in (
        [(PICKUP, 'grasp', 'pickup-single-owner'),
         (CONTACT_START, 'hand_contact', 'paired-grip-contact-start'),
         (HANDOFF, 'release', 'ownership-transfer-to-moss'),
         (CONTACT_END+1, 'release', 'giver-contact-end')]
        if actor == 'aster' else
        [(CONTACT_START, 'hand_contact', 'paired-grip-contact-start'),
         (HANDOFF, 'attach', 'ownership-transfer-from-aster'),
         (624, 'marker', 'activate-laboratory')]):
        events.append(dict(schema_version=1, time=frame/FPS, type=event_type,
                           target='access-key', value=label))
    return sorted(events, key=lambda e: (e['time'], e['target'], e['type']))


def author_pose(rig, actor, frame):
    require(actor in ACTORS and type(frame) is int and 0 <= frame <= FRAMES,
            'DIRECTOR_NARRATIVE_FRAME', 'Select an exact native actor/frame.')
    root = transform_matrix(root_transform(actor, frame))
    inverse = np.linalg.inv(root)
    progress = DISTANCE*ease(frame/312)
    pose = neutral_pose(rig.skeleton)
    pelvis = next(b for b in rig.skeleton['bones'] if b['role'] == 'pelvis')
    pose['bones'][pelvis['id']] = dict(translation=list(pelvis['rest']['translation']))
    pose['bones'][pelvis['id']]['translation'][1] -= .03*(1.-ease((frame-336)/12))
    phase = 2*math.pi*progress/STRIDE
    chains = {chain['id']: chain for chain in rig.manifest['controllers']['ik']}
    rest = evaluate_fk(rig.skeleton)
    # Lower both arms for relaxed traversal. Broad bind-pose bounds alone do not
    # establish a collision; exact deformed-geometry checks remain separate.
    for arm, phase_offset in [('left', 0.), ('right', math.pi)]:
        chain = chains['arm.'+arm]
        shoulder = rest[chain['bones'][0]][:3, 3]
        reach = sum(next(b['length'] for b in rig.skeleton['bones'] if b['id'] == name)
                    for name in chain['bones'][:-1])
        swing = .06*math.sin(phase+phase_offset) if frame < 312 else 0.
        target = shoulder + [math.copysign(.06, shoulder[0]), -.72*reach, .08+swing]
        solved = solve_ik(rig.skeleton, pose, chain, target,
                          constraints=rig.manifest['controllers']['constraints'])
        require(solved.reachable and solved.residual_m <= .005,
                'DIRECTOR_NARRATIVE_ARM', 'Native resting arm target is unreachable.',
                asset=actor, observed=dict(frame=frame, side=arm, residual_m=solved.residual_m))
        pose = solved.pose
    # Gaze targets and phases differ between the two real rigs.
    scan = np.asarray([-2.8, 1.25, -2.2] if actor == 'aster' else [2.7, 1.35, -1.85])
    scan[0] += .35*math.sin(min(frame, 264)/36 + (0 if actor == 'aster' else 1.7))
    # Blend the actual native gaze rotations. Interpolating target positions can
    # carry the target through an eye and create an abrupt aim-axis flip.
    scanned = gaze_at(rig, pose, (inverse @ np.r_[scan, 1.])[:3], head_follow=.18)
    discovered = gaze_at(rig, pose,
        (inverse @ np.r_[key_transform(frame)['translation'], 1.])[:3], head_follow=.18)
    pose = blend_poses(rig, scanned, discovered, ease((frame-264)/48),
                       mask=['head', 'eye.left', 'eye.right'])
    # Actual existing morphs blink independently; no semantic-only face control.
    blink_phase = (frame + (37 if actor == 'moss' else 0)) % 137
    blink = math.sin(math.pi*blink_phase/8)**2 if blink_phase < 8 else 0.
    for mesh in rig.geometry.meshes:
        if 'blink' in mesh.morphs:
            pose['morphs'].setdefault(mesh.id, {})['blink'] = blink
    side = SIDES[actor]
    approach = ease((frame-336)/24) if actor == 'aster' else ease((frame-480)/36)
    hold = 1.-ease((frame-CONTACT_END)/24) if actor == 'aster' else 1.
    amount = approach*hold
    if amount:
        pose = finger_controls(rig, pose, side=side, curl=.8*amount)
    if frame < 348:
        for foot in ('left', 'right'):
            offset, lift = _foot_offset(progress, foot, frame)
            target = rest['foot.'+foot][:3, 3] + [0., lift, offset]
            orientation = decompose(rest['foot.'+foot])['rotation']
            solved = solve_ik(rig.skeleton, pose, chains['leg.'+foot], target,
                              orientation=orientation, constraints=rig.manifest['controllers']['constraints'])
            require(solved.reachable and solved.residual_m <= .005,
                    'DIRECTOR_NARRATIVE_FOOT', 'Native walking foot target is unreachable.',
                    asset=actor, observed=dict(frame=frame, side=foot, residual_m=solved.residual_m))
            pose = solved.pose
    grasp_residual = None
    if amount:
        local_key = decompose(inverse @ transform_matrix(key_transform(frame)))
        chain = chains['arm.'+side]
        seed = deepcopy(pose)
        for bone in chain['bones']:
            seed['bones'].pop(bone, None)
        solved = solve_grasp(rig, seed, 'arm.'+side, 'grip.'+side, local_key, GRIPS[actor])
        require(solved['reachable'] and solved['residual_m'] <= .005,
                'DIRECTOR_NARRATIVE_GRIP', 'Native object grip is unreachable.',
                asset=actor, observed=dict(frame=frame, residual_m=solved['residual_m'],
                                          reachable=solved['reachable'], converged=solved['converged']))
        pose = blend_poses(rig, pose, solved['pose'], amount, mask=chain['bones'])
        if amount == 1.:
            actual = root @ socket_world(rig, pose, 'grip.'+side)
            expected = transform_matrix(key_transform(frame)) @ transform_matrix(GRIPS[actor])
            grasp_residual = float(np.linalg.norm(actual[:3, 3]-expected[:3, 3]))
            require(grasp_residual <= .005, 'DIRECTOR_NARRATIVE_GRIP',
                    'Authored grip changed after native blending.', observed=grasp_residual)
    return pose, dict(actor=actor, frame=frame, grasp_residual_m=grasp_residual,
                      grasp_active=amount == 1., foot_settled=frame >= 348)


def author_motion(rigs):
    """Build actual native local clips; no filesystem publication or rendering."""
    prepare_rigs(rigs)
    result = {}
    for actor, rig in rigs.items():
        samples = [author_pose(rig, actor, frame) for frame in range(FRAMES+1)]
        poses = [sample[0] for sample in samples]
        provenance = dict(schema_version=1, tool_version='1.0.0',
            operation='Director five-shot native exploration and paired-grip handoff',
            created_at='2026-11-14T00:00:00Z',
            input_hashes={'source': rig.manifest['source']['sha256']}, ancestry=[])
        clips = {}
        for name, start, end in [*SHOTS, ('full-narrative', 0, FRAMES)]:
            events = [dict(event, time=(int(round(event['time']*FPS))-start)/FPS)
                      for event in native_events(actor)
                      if start <= int(round(event['time']*FPS)) <= end]
            clip = clip_from_poses(rig, actor+'-'+name, [f/FPS for f in range(end-start+1)],
                poses[start:end+1], fps=FPS, loop=False, root_motion='in-place',
                events=events, provenance=provenance)
            clips[name] = clip
        result[actor] = dict(clips=clips, samples=[sample[1] for sample in samples])
    return result
