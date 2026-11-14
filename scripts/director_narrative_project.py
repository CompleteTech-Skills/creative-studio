"""Construct native narrative documents inside an already owned build tree.

Final capture, exclusive publication and snapshot verification belong to the
canonical Scene3D snapshot service. This module supplies no second publisher.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import numpy as np

from asset_library import read_source_bytes
from camera3d_math import look_at
from director_narrative import (ACTORS, CONTACT_END, CONTACT_START, FPS, FRAMES,
    GRIPS, HANDOFF, PICKUP, SHOTS, author_motion, ease, key_transform,
    prepare_rigs, root_transform)
from model3d_geometry import box
from rig_animation import evaluate_animation, validate_animation
from rig_attachments import socket_world
from rig_errors import atomic_json, decode_json, object_hash, require, sha256
from rig_geometry import vertex_normals
from rig_gltf import export_geometry, export_gltf
from rig_jobs import save_rig
from rig_math import axis_angle, decompose, transform_matrix, transform_points
from rig_models import Geometry, load_rig
from scene3d_contracts import (all_refs, canonical_bytes, load_project,
                                portable_path, validate_document)
from scene3d_example import node
from animation3d_contracts import validate_contract as validate_animation_document

MAX_BYTES = 256*1024**2
MAX_FILES = 1024
PROMPT = 'Create a 30-second silent 3D animation of two robots exploring an abandoned laboratory.'
SETTINGS = dict(backend='blender-cycles', width=1280, height=720, samples=16,
                transparent=False, color_space='srgb', output='png', device='cpu')
PROVENANCE = dict(origin='procedural-fixture', fixture=True,
    tool='director-five-shot-narrative-v1', inputs=[],
    attribution='Hatch Pet native procedural engineering demonstration', license='CC0-1.0')
CAMERAS = {
    'entry': ([0., 3., 5.5], [0., 1., .8], [.3, 2.8, 4.9], [0., 1., .4], 1.0),
    'exploration': ([2.8, 2.6, 3.], [0., 1., .5], [2.6, 2.3, 1.9], [0., 1.1, -.1], 1.1),
    'discovery': ([-2.1, 2.2, .8], [-.15, 1.05, -1.45], [-1.85, 2.1, .55], [-.15, 1.05, -1.45], 1.2),
    'cooperation': ([0., 2.1, -2.8], [0., 1.15, -1.15], [0., 2.1, -2.8], [0., 1.15, -1.15], 1.25),
    'resolution': ([-1., 2.8, -2.75], [.2, 1.1, -1.3], [-.4, 2.9, -2.7], [.2, 1.1, -1.3], 1.2),
    'proof-left': ([-1.8, 2.1, -2.65], [0., 1.15, -1.35], [-1.8, 2.1, -2.65], [0., 1.15, -1.35], 1.05),
    'proof-right': ([1.8, 2.3, -2.65], [0., 1.15, -1.35], [1.8, 2.3, -2.65], [0., 1.15, -1.35], 1.05),
    'proof-light-off': ([-.7, 2.85, -2.72], [.2, 1.1, -1.3], [-.7, 2.85, -2.72], [.2, 1.1, -1.3], 1.2),
    'proof-light-on': ([-.7, 2.85, -2.72], [.2, 1.1, -1.3], [-.7, 2.85, -2.72], [.2, 1.1, -1.3], 1.2),
}
DRESSING = (
    ('key-stand', [-.50, 1.0575, -1.64], [.04, .065, .06], 0.),
    ('fallen-panel', [-2.35, .035, .45], [.55, .07, .42], .3),
    ('broken-tile-a', [2.25, .025, .7], [.32, .05, .28], -.35),
    ('broken-tile-b', [2.6, .018, .25], [.24, .036, .22], .5),
    ('discarded-cover', [-2.15, .025, -.85], [.42, .05, .24], -.25),
    ('discarded-strip-a', [-2.55, .02, -1.15], [.055, .04, .52], .35),
    ('discarded-strip-b', [-2.30, .02, -1.45], [.055, .04, .38], -.6),
)


def camera_id(shot):
    # A photometric control changes intensity only, including camera identity.
    return 'camera-'+('proof-light-off' if shot == 'proof-light-on' else shot)


def _file(root, path):
    return dict(path=path, sha256=sha256(root/path))


def _ref(root, path, kind='evidence', identifier=None, version='1.0.0'):
    return dict(kind=kind, id=identifier or 'file-'+hashlib.sha256(path.encode()).hexdigest()[:48],
                version=version, **_file(root, path))


def _document(root, path, value):
    (root/path).parent.mkdir(parents=True, exist_ok=True)
    (root/path).write_bytes(canonical_bytes(value))
    return _ref(root, path, value['kind'], value['id'], value['version'])


def _track(target, prop, values, *, interpolation='linear'):
    return dict(target=target, property=prop, interpolation=interpolation,
                keys=[dict(frame=frame, value=value) for frame, value in enumerate(values)])


def _dressing(root, lab_document, lab_geometry):
    meshes = []
    for name, center, dimensions, angle in DRESSING:
        mesh = box(*dimensions, name=name)
        matrix = transform_matrix(dict(translation=center,
            rotation=axis_angle([0, 1, 0], angle).tolist(), scale=[1., 1., 1.]))
        mesh.positions = transform_points(matrix, mesh.positions)
        mesh.attributes['NORMAL'] = vertex_normals(mesh.positions, mesh.triangles)
        mesh.material = 1
        meshes.append(mesh)
    geometry = Geometry(meshes, deepcopy(lab_geometry.materials))
    export_geometry(geometry, root/'laboratory-dressing.glb',
                    provenance=dict(fixture=True, operation='Native box dressing and key pedestal'))
    points = np.concatenate([mesh.positions for mesh in meshes])
    asset = dict(schema_version=1, kind='scene3d-asset', id='laboratory-dressing', version='1.0.0',
        name='Abandoned laboratory dressing and narrow key pedestal', asset_type='environment',
        content=_file(root, 'laboratory-dressing.glb'), source=_file(root, 'laboratory-dressing.glb'),
        provenance=deepcopy(PROVENANCE), coordinate_system=deepcopy(lab_document['coordinate_system']),
        original_coordinate_system='right-handed Y-up meters', pivot=[0, 0, 0],
        meshes=[_file(root, 'laboratory-dressing.glb')], materials=[],
        bounds=dict(minimum=points.min(axis=0).tolist(), maximum=points.max(axis=0).tolist()),
        collision=None, sockets=[], components={}, skeleton=None, animations=[], lods=[], dependencies=[],
        resources=dict(vertices=geometry.vertex_count, triangles=geometry.triangle_count,
                       bytes=(root/'laboratory-dressing.glb').stat().st_size),
        validation=[], review=dict(status='unreviewed', receipts=[]))
    asset['provenance']['inputs'] = [_file(root, 'recipes/narrative.json')]
    return _document(root, 'laboratory-dressing-asset.json', asset), asset


def populate_narrative(source_root, stage, *, motions=None):
    """Write a complete draft closure into an empty, application-owned tree.

    Caller owns this private build tree and its lifetime. This does not publish
    a delivery or confer acceptance. Public orchestration must use the native
    Scene3D capture/snapshot owner and bind the expected completed source hash.
    """
    source_root, stage = Path(source_root), Path(stage)
    require(stage.is_dir() and not stage.is_symlink() and not any(stage.iterdir()),
            'DIRECTOR_NARRATIVE_STAGE', 'Use an empty application-owned build directory.')
    bundle = load_project(source_root)
    require({r['id'] for r in bundle['project']['characters']} == set(ACTORS),
            'DIRECTOR_NARRATIVE_IDENTITY', 'Source must contain exactly Aster and Moss.')
    require(bundle['bytes']*2 <= MAX_BYTES//2 and len(bundle['inventory'])*2 < MAX_FILES//2,
            'DIRECTOR_NARRATIVE_LIMIT', 'Source leaves insufficient bounded authoring capacity.')
    original = []
    for path, record in sorted(bundle['inventory'].items()):
        payload = read_source_bytes(source_root/portable_path(path), min(record['bytes'], 64*1024**2))
        require(len(payload) == record['bytes'] and hashlib.sha256(payload).hexdigest() == record['sha256'],
                'DIRECTOR_NARRATIVE_SOURCE_CHANGED', 'Source changed during bounded copying.', asset=path)
        for relative in ['source-original/'+path, *([path] if path != 'project.json' else [])]:
            target = stage/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        original.append(_ref(stage, 'source-original/'+path))
    require(load_project(stage/'source-original')['inventory'] == bundle['inventory'],
            'DIRECTOR_NARRATIVE_SOURCE_CHANGED', 'Original project copy is not exact.')
    recipe = dict(schema_version=1, kind='director-narrative-recipe', original_prompt=PROMPT,
        interpretation='deterministic fixed procedural narrative; no provider execution',
        actor_ids=list(ACTORS), fps=dict(numerator=FPS, denominator=1), frames=FRAMES,
        shots=[dict(id=name, start_frame=start, end_frame=end) for name, start, end in SHOTS],
        pickup_frame=PICKUP, handoff_frame=HANDOFF,
        dual_contact_interval=dict(start=CONTACT_START, end=CONTACT_END+1),
        paired_object_grips=deepcopy(GRIPS), dressing=[dict(id=n, position=p, dimensions=d, yaw=a)
                                                   for n, p, d, a in DRESSING],
        semantic_scene_approval=False, production_approved=False)
    (stage/'recipes').mkdir(exist_ok=True)
    atomic_json(stage/'recipes/narrative.json', recipe)
    rigs = {a: load_rig(stage/'native'/a) for a in ACTORS}
    prepare_rigs(rigs)
    motions = author_motion(rigs) if motions is None else motions
    assets, documents, native_refs, hand_frames = [], {}, {}, {}
    for actor, rig in rigs.items():
        original_geometry = deepcopy(rig.manifest['geometry'])
        original_skeleton = object_hash(rig.skeleton)
        original_materials = object_hash(rig.geometry.materials)
        require(set(motions[actor]['clips']) == {name for name, _, _ in SHOTS} | {'full-narrative'},
                'DIRECTOR_NARRATIVE_CLIPS', 'All five exact native shot clips and full motion are required.')
        for clip in motions[actor]['clips'].values():
            validate_animation(rig, clip)
            rig.clips[clip['id']] = deepcopy(clip)
        save_rig(rig, operation='Author five-shot Director narrative; preserve original geometry and skeleton')
        require(rig.manifest['geometry']['data']['sha256'] == original_geometry['data']['sha256']
                and object_hash(rig.skeleton) == original_skeleton
                and object_hash(rig.geometry.materials) == original_materials,
                'DIRECTOR_NARRATIVE_IDENTITY', 'Native authoring changed geometry, skeleton or materials.')
        content = actor+'-narrative.glb'
        export_gltf(rig, stage/content, bake=False)
        asset = deepcopy(bundle['documents'][actor+'-asset.json'])
        asset.update(version='1.1.0', name=actor+' persistent narrative identity', content=_file(stage, content))
        asset['meshes'] = [_file(stage, content)]
        asset['skeleton'] = _ref(stage, 'native/'+actor+'/rig.json', 'hatch-3d-rig',
                                  rig.manifest['id'], rig.manifest['version'])
        asset['animations'] = [_ref(stage, 'native/'+actor+'/'+c['path'], 'hatch-3d-animation',
                                    c['id'], c['version']) for c in rig.manifest['animations']]
        asset['dependencies'] = [_ref(stage, p.relative_to(stage).as_posix())
            for p in sorted((stage/'native'/actor).rglob('*')) if p.is_file()]
        asset['resources']['bytes'] = (stage/content).stat().st_size
        asset['review'] = dict(status='unreviewed', receipts=[])
        asset['provenance'] = dict(deepcopy(PROVENANCE), origin='derived',
            inputs=[_file(stage, 'source-original/'+actor+'-asset.json')])
        assets.append(_document(stage, actor+'-narrative-asset.json', asset))
        documents[assets[-1]['path']] = asset
        native_refs[actor] = {name: next(c for c in asset['animations'] if c['id'] == actor+'-'+name)
                              for name, _, _ in SHOTS}
        full_clip = rig.clips[actor+'-full-narrative']
        side = 'left' if actor == 'aster' else 'right'
        hand_frames[actor] = [decompose(socket_world(rig,
            evaluate_animation(rig, full_clip, frame/FPS, loop=False, _validated=True)['pose'],
            'grip.'+side)) for frame in range(FRAMES+1)]
    for identifier in ('laboratory', 'access-key'):
        reference = next(r for r in bundle['project']['assets'] if r['id'] == identifier)
        assets.append(deepcopy(reference))
        documents[reference['path']] = deepcopy(bundle['documents'][reference['path']])
    from rig_import import load_character_model
    laboratory = documents[next(r['path'] for r in assets if r['id'] == 'laboratory')]
    laboratory_geometry = load_character_model(stage/'laboratory.glb').geometry
    dressing_ref, dressing = _dressing(stage, laboratory, laboratory_geometry)
    assets.append(dressing_ref)
    documents[dressing_ref['path']] = dressing
    scene = deepcopy(bundle['documents']['scene.json'])
    scene.update(id='director-laboratory-narrative', version='1.0.0', revision=0,
                 name='Two robots explore the abandoned laboratory', shots=[*CAMERAS],
                 dependencies=assets, provenance=deepcopy(PROVENANCE))
    scene['nodes'] = [n for n in scene['nodes'] if n['type'] != 'camera']
    scene['cameras'] = []
    by_id = {n['id']: n for n in scene['nodes']}
    for actor in ACTORS:
        by_id[actor]['asset'] = next(r for r in assets if r['id'] == actor)
        by_id[actor]['transform'] = root_transform(actor, 0)
    by_id['access-key']['transform'] = key_transform(0)
    by_id['access-key']['metadata'] = dict(ownership='canonical native paired-grip transfer, baked world channels',
        pickup_frame=PICKUP, handoff_frame=HANDOFF, original_owner='unowned', final_owner='moss')
    for actor in ACTORS:
        anchor = node(actor+'-hand-grip', 'socket', actor)
        anchor['transform'] = hand_frames[actor][0]
        anchor['metadata'] = dict(native_actor=actor, native_socket='grip.'+('left' if actor == 'aster' else 'right'),
                                  authority='baked from canonical native FK, not a second rig')
        scene['nodes'].append(anchor)
        prop_anchor = node('key-'+actor+'-grip', 'socket', 'access-key')
        prop_anchor['transform'] = deepcopy(GRIPS[actor])
        scene['nodes'].append(prop_anchor)
    dressing_node = node('laboratory-dressing', 'mesh_instance', 'root')
    dressing_node.update(asset=dressing_ref, bounds=dressing['bounds'])
    scene['nodes'].append(dressing_node)
    scene['world']['exposure'] = -.5
    for light_id, intensity in [('key-light', 180.), ('fill-light', 90.)]:
        by_id[light_id]['light']['intensity'] = intensity
    for identifier, parent, position, color in (
        ('activation-glow', 'root', [.32, 1.52, -1.40], [.2, .7, 1.]),
        ('laboratory-reveal', 'root', [0., 2.65, -2.6], [.35, .6, 1.])):
        light = node(identifier, 'light', parent)
        light['transform']['translation'] = position
        light['light'] = dict(type='point' if identifier == 'activation-glow' else 'area', color=color, intensity=0.)
        scene['nodes'].append(light)
        scene['lights'].append(identifier)
    for identifier, (position, target, _, _, fov) in CAMERAS.items():
        if identifier == 'proof-light-on':
            continue
        camera = node(camera_id(identifier), 'camera', 'root')
        camera['transform'].update(translation=position, rotation=look_at(position, target).tolist())
        camera['camera'] = dict(projection='perspective', near=.05, far=100.,
                                vertical_fov=fov, orthographic_height=5.)
        scene['nodes'].append(camera)
        scene['cameras'].append(camera['id'])
    scene_ref = _document(stage, 'narrative-scene.json', scene)
    documents[scene_ref['path']] = scene
    shots, production = [], []
    for name, start, end in [*SHOTS, ('proof-left', 432, 576), ('proof-right', 432, 576),
                             ('proof-light-off', 576, 720), ('proof-light-on', 576, 720)]:
        frames = list(range(start, end+1))
        tracks = []
        for actor in ACTORS:
            tracks.append(_track(actor, 'translation', [root_transform(actor, f)['translation'] for f in frames]))
            for prop, interpolation in [('translation', 'linear'), ('rotation', 'slerp'), ('scale', 'linear')]:
                tracks.append(_track(actor+'-hand-grip', prop,
                    [hand_frames[actor][f][prop] for f in frames], interpolation=interpolation))
        for prop, interpolation in [('translation', 'linear'), ('rotation', 'slerp'), ('scale', 'linear')]:
            tracks.append(_track('access-key', prop, [key_transform(f)[prop] for f in frames], interpolation=interpolation))
        for target, multiplier in [('activation-glow', 160.), ('laboratory-reveal', 280.)]:
            values = ([0.] * len(frames) if name == 'proof-light-off' else
                      [multiplier] * len(frames) if name == 'proof-light-on' else
                      [multiplier*ease((f-624)/36) for f in frames])
            tracks.append(_track(target, 'light_intensity', values))
        tracks.append(_track('world', 'world_exposure',
            [-.5] * len(frames) if name.startswith('proof-light-') else
            [-.5+.75*ease((f-624)/36) for f in frames]))
        position, target, final_position, final_target, _ = CAMERAS[name]
        camera_positions, camera_rotations = [], []
        for f in frames:
            amount = ease((f-start)/(end-start))
            p = np.asarray(position)+(np.asarray(final_position)-position)*amount
            t = np.asarray(target)+(np.asarray(final_target)-target)*amount
            camera_positions.append(p.tolist())
            camera_rotations.append(look_at(p, t).tolist())
        tracks.extend([_track(camera_id(name), 'translation', camera_positions),
                       _track(camera_id(name), 'rotation', camera_rotations, interpolation='slerp')])
        shot = dict(schema_version=1, kind='scene3d-shot', id=name, version='1.0.0', name=name,
            scene=scene_ref, duration_frames=end-start, fps=dict(numerator=FPS, denominator=1),
            camera=camera_id(name), camera_overrides={}, tracks=tracks,
            bindings=[dict(target=a, clip=native_refs[a]['resolution' if name.startswith('proof-light-')
                                                        else 'cooperation' if name.startswith('proof-') else name],
                           start_frame=0, end_frame=end-start, speed=1, loop=False) for a in ACTORS],
            material_overrides=[], temporal_constraints=[], render=deepcopy(SETTINGS), continuity=[],
            dependencies=[], metadata=dict(sequence_start=start, sequence_end=end,
                purpose='engineering-render-effect-control' if name.startswith('proof-light-')
                        else 'engineering-alternate-view' if name.startswith('proof-') else 'production-sequence'))
        reference = _document(stage, name+'-narrative-shot.json', shot)
        documents[reference['path']] = shot
        shots.append(reference)
        if not name.startswith('proof-'):
            production.append(dict(id=name, start_frame=start, end_frame=end, scene=scene_ref, shot=reference))
    project = deepcopy(bundle['project'])
    project.update(id='director-laboratory-narrative-project', version='1.0.0',
        name='Thirty-second native two-robot narrative', assets=assets,
        characters=[r for r in assets if r['id'] in ACTORS], scenes=[scene_ref], shots=shots,
        animations=[], prefabs=[], render_defaults=deepcopy(SETTINGS),
        budgets=dict(max_nodes=256, max_files=MAX_FILES, max_bytes=MAX_BYTES, max_frames=FRAMES),
        created_at='2026-11-14T00:00:00Z', modified_at='2026-11-14T00:00:00Z',
        provenance=deepcopy(PROVENANCE), validation=[],
        dependency_lock=[],
        metadata=dict(sequence=','.join(name for name, _, _ in SHOTS),
                      proof_shots=','.join(name for name in CAMERAS if name.startswith('proof-')),
                      artistic_approval=False))
    project['provenance']['inputs'] = [_file(stage, 'recipes/narrative.json'),
                                       _file(stage, 'source-original/project.json')]
    interaction = dict(schema_version=1, kind='animation_interaction_3d', id='access-key-handoff',
        participants=[dict(actor_id=a, role='giver' if a == 'aster' else 'receiver',
                           hand_bone='hand.'+('left' if a == 'aster' else 'right'),
                           grip_anchor=a+'-hand-grip', interaction_marker='paired-grip-contact-start') for a in ACTORS],
        prop_id='access-key', start_frame=CONTACT_START, end_frame=CONTACT_END+1,
        synchronization_markers=['paired-grip-contact-start', 'ownership-transfer', 'giver-contact-end'],
        ownership_changes=[dict(frame=HANDOFF, previous_owner='aster', next_owner='moss',
            previous_anchor='aster-hand-grip', next_anchor='moss-hand-grip', world_pose_policy='preserve',
            transfer_duration_frames=0)], constraints=['aster-paired-grip', 'moss-paired-grip'],
        position_tolerance_m=.01, orientation_tolerance_rad=.01, failure_policy='error')
    validate_animation_document(interaction)
    atomic_json(stage/'recipes/interaction.json', interaction)
    project['provenance']['inputs'].append(_file(stage, 'recipes/interaction.json'))
    refs = {tuple(r[k] for k in ('kind', 'id', 'version')): r for r in original}
    for reference in [*assets, scene_ref, *shots]:
        refs[tuple(reference[k] for k in ('kind', 'id', 'version'))] = reference
    for document in [project, *documents.values()]:
        for reference in all_refs(document):
            if 'kind' in reference:
                identity = tuple(reference[k] for k in ('kind', 'id', 'version'))
                require(identity not in refs or refs[identity] == reference,
                        'DIRECTOR_NARRATIVE_REFERENCE', 'Conflicting exact native dependency reference.')
                refs[identity] = reference
    project['dependency_lock'] = [refs[k] for k in sorted(refs)]
    _document(stage, 'project.json', project)
    verified = load_project(stage)
    require(verified['bytes'] <= MAX_BYTES and len(verified['inventory']) <= MAX_FILES,
            'DIRECTOR_NARRATIVE_LIMIT', 'Authored closure exceeds the bounded profile.')
    return dict(kind='director-narrative-build', schema_version=1,
        project_sha256=sha256(stage/'project.json'), frames=FRAMES, fps=dict(numerator=FPS, denominator=1),
        resolution=[1280, 720], production_shots=production,
        proof_shots=[r for r in shots if r['id'].startswith('proof-')],
        original_project_sha256=bundle['inventory']['project.json']['sha256'],
        source_original_inventory=load_project(stage/'source-original')['inventory'],
        semantic_scene_approval=False, production_approved=False,
        state='authored-and-closure-verified; physical acceptance and publication pending')
