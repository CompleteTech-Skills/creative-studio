"""Native scene/rig adapter for the bounded Director two-robot performance."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import numpy as np

from atomic_publish import _rename_no_replace
from camera3d_math import look_at, blender_camera_transform
from director_scene_binding import PICKUP_FRAME, bind_scene_project, key_transform
from rig_animation import clip_from_poses, evaluate_animation
from rig_blender import find_blender
from rig_contracts import COORDINATES
from rig_errors import atomic_json, decode_json, require, sha256, object_hash
from rig_gltf import export_gltf
from rig_ik import solve_ik
from rig_jobs import save_rig
from rig_math import axis_angle, compose, transform_points
from rig_models import load_rig
from rig_skeleton import neutral_pose, rotate_bone, evaluate_fk
from rig_skinning import evaluate_deformation
from scene3d_example import node
from scene3d_math import evaluate_transforms, to_blender
from scene3d_contracts import load_project, validate_document
from scene3d_evaluate import camera_matrices, evaluate_scene

ACTORS = {'aster': [-.5, 0., -1.10], 'moss': [1.2, 0., -.3]}
SAMPLES = [0, 36, 59, 60, 71, PICKUP_FRAME, 73, 96, 119]


def author_clip(rig, actor):
    times = [frame/24 for frame in range(121)]
    poses = []
    chains = {chain['id']: chain for chain in rig.manifest['controllers']['ik']}
    for time in times:
        pose = neutral_pose(rig.skeleton)
        pose = rotate_bone(rig.skeleton, pose, 'head', axis_angle([0,1,0], .22*math.sin(time*1.5+(1 if actor=='moss' else 0))))
        if actor == 'aster':
            # Reach to the tabletop, then lift. Existing IK owns the actual pose.
            amount = min(1., max(0., (time-1.0)/2.0))
            lift = min(1., max(0., (time-3.0)/1.5))
            rest = evaluate_fk(rig.skeleton)['hand.right'][:3,3]
            target = rest*(1-amount) + np.array([.30, 1.20+.25*lift, .46-.15*lift])*amount
            target[1] += .14*math.sin(math.pi*amount)
            solved = solve_ik(rig.skeleton, pose, chains['arm.right'], target)
            require(solved.residual_m < .01, 'DIRECTOR_CONTACT', 'Authored reach exceeds one-centimeter residual.', observed=solved.residual_m)
            pose = solved.pose
        else:
            angle = .25 + .65*min(1., max(0., (time-2.0)/2.0))
            pose = rotate_bone(rig.skeleton, pose, 'upper-arm.left', axis_angle([1,0,0], -angle))
            pose = rotate_bone(rig.skeleton, pose, 'forearm.left', axis_angle([0,0,1], -.25))
        poses.append(pose)
    clip = clip_from_poses(rig, actor+'-investigation', times, poses, fps=24,
                          provenance=dict(schema_version=1, tool_version='1.0.0', operation='Director authored laboratory investigation',
                                          created_at='2026-11-12T00:00:00Z', input_hashes={'source':rig.manifest['source']['sha256']}, ancestry=[]))
    segments = {}
    for name, offset in [('establish', 0), ('discovery', 60)]:
        segment = clip_from_poses(rig, actor+'-'+name, [frame/24 for frame in range(61)],
                                  poses[offset:offset+61], fps=24, provenance=deepcopy(clip['provenance']))
        segments[segment['id']] = segment
    rig.clips = {clip['id']: clip, **segments}
    return clip


def scene_document():
    nodes = [node('root')]
    for actor, position in ACTORS.items():
        item = node(actor, parent='root')
        item['transform']['translation'] = position
        item['transform']['rotation'] = axis_angle([0,1,0], math.pi if actor=='aster' else .4).tolist()
        nodes.append(item)
    cameras=[]
    for name, position, target in [('wide',[0.,2.3,2.6],[.2,1.,-1.2]),('investigation',[-2.3,2.1,1.7],[.1,1.15,-1.4])]:
        item=node(name,'camera','root');item['transform'].update(translation=position,rotation=look_at(position,target).tolist())
        item['camera']=dict(projection='perspective',near=.05,far=100,vertical_fov=.9,orthographic_height=5.)
        nodes.append(item);cameras.append(name)
    for name, position, energy in [('key-light',[-2.,4.,1.],650.),('fill-light',[3.,3.,-1.],450.)]:
        item=node(name,'light','root');item['transform']['translation']=position
        item['light']=dict(type='area',color=[1.,.92,.8],intensity=energy)
        nodes.append(item)
    scene=dict(schema_version=1,kind='scene3d-scene',id='director-laboratory',version='1.0.0',revision=0,
        name='Director laboratory performance',description='Native rig performance adapter, procedural engineering fixture.',
        coordinate_system=COORDINATES,roots=['root'],nodes=nodes,world=dict(coordinate_system=COORDINATES,
        background=[.08,.1,.13],exposure=0,environment=None,metadata={}),collections=[],layers=[],cameras=cameras,
        lights=['key-light','fill-light'],recipes=[],dependencies=[],bounds=None,shots=['establish','discovery'],continuity=[],
        metadata={},provenance=dict(origin='procedural-fixture',fixture=True,tool='director_scene',inputs=[],attribution='Hatch Pet',license='CC0-1.0'))
    return validate_document(scene,'scene')


def evaluate_frame(rigs, scene, frame, *, include_vertices=True):
    require(type(frame) is int and 0 <= frame <= 120,'DIRECTOR_FRAME','Expected frame boundary 0 to 120.')
    transforms=evaluate_transforms(scene)
    actors={}
    for actor,rig in rigs.items():
        sampled=evaluate_animation(rig,actor+'-investigation',frame/24,contacts=False,constraints=False)
        world=np.asarray(transforms[actor]['world_matrix'])
        fk=evaluate_fk(rig.skeleton,sampled['pose'])
        deformed=evaluate_deformation(rig,sampled['pose']) if include_vertices else {}
        actors[actor]=dict(bones={name:(world@matrix).tolist() for name,matrix in fk.items()},
            vertices={name:transform_points(world,value['positions']).tolist() for name,value in deformed.items()})
    pickup=evaluate_animation(rigs['aster'],'aster-investigation',PICKUP_FRAME/24,contacts=False,constraints=False)['pose']
    pickup_hand=np.asarray(transforms['aster']['world_matrix'])@evaluate_fk(rigs['aster'].skeleton,pickup)['hand.right']
    hand=pickup_hand if frame<=PICKUP_FRAME else np.asarray(actors['aster']['bones']['hand.right'])
    key=key_transform(hand, pickup_hand)
    return dict(frame=frame,time=dict(numerator=frame,denominator=24),nodes=transforms,actors=actors,
                key_matrix=key.tolist(),key_owner='aster' if frame>=PICKUP_FRAME else None,camera='wide' if frame<60 else 'investigation')



def _copy_assets(source, destination):
    from asset_library import AssetLibrary, read_source_bytes
    from asset_library_contracts import MAX_FILE_BYTES
    from director_assets import MAX_BUNDLE_BYTES, MAX_BUNDLE_FILES

    require(source.is_dir() and not source.is_symlink(), 'DIRECTOR_SOURCE', 'Asset source must be a regular directory.')
    destination.mkdir(mode=0o700)
    locks = {f'.rig-{actor}.rig.lock' for actor in ACTORS}
    inventory = []
    remaining = MAX_BUNDLE_BYTES
    for path in sorted(source.rglob('*')):
        info = path.lstat()
        relative = path.relative_to(source)
        target = destination / relative
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(mode=0o700)
            continue
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                'DIRECTOR_SOURCE', 'Asset source requires ordinary unlinked files.')
        require(len(inventory) < MAX_BUNDLE_FILES and info.st_size <= remaining,
                'DIRECTOR_LIMIT', 'Asset snapshot exceeds bounds.')
        # The producer records both empty coordination files in its inventory.
        if relative.as_posix() in locks:
            require(info.st_size == 0, 'DIRECTOR_SOURCE', 'Unexpected native rig lock content.')
            payload = b''
        else:
            payload = read_source_bytes(path, min(MAX_FILE_BYTES, remaining))
        remaining -= len(payload)
        target.write_bytes(payload)
        target.chmod(0o600)
        inventory.append(dict(path=relative.as_posix(), bytes=len(payload),
                              sha256=hashlib.sha256(payload).hexdigest()))

    files = {entry['path']: entry for entry in inventory}
    report = decode_json((destination/'assets.json').read_bytes())
    expected = [entry for entry in inventory
                if entry['path'] != 'assets.json' and not entry['path'].startswith('library/')]
    require(isinstance(report, dict) and report.get('kind') == 'director-procedural-assets' and
            report.get('inventory') == expected, 'DIRECTOR_SOURCE', 'Producer inventory differs from staged files.')
    cast = {*ACTORS, 'laboratory', 'access-key'}
    records = report.get('assets')
    require(isinstance(records, dict) and set(records) == cast and
            all(isinstance(record, dict) for record in records.values()),
            'DIRECTOR_SOURCE', 'Unexpected producer asset identities.')
    library = AssetLibrary(destination/'library')
    references = library.list_references(limit=len(cast)+1)
    require(len(references) == len(cast) and {ref['id'] for ref in references} == cast and
            all(records[ref['id']].get('reference') == ref for ref in references),
            'DIRECTOR_SOURCE', 'Producer registry references differ from staged records.')
    for name, record in records.items():
        source_name = name + '.glb'
        identity_name = name + '-identity.json'
        reference = record['reference']
        expected_roles = {source_name: 'geometry', identity_name: 'identity',
                          name + '-recipe.json': 'source', 'license.txt': 'license'}
        if name in ACTORS:
            expected_roles[name + '-rigged.glb'] = 'rig'
        manifest = library.resolve(reference)
        stored_manifest = decode_json((destination/(name+'-asset.json')).read_bytes())
        require(manifest == stored_manifest and
                {item['path']: item['role'] for item in manifest['files']} == expected_roles,
                'DIRECTOR_SOURCE', 'Staged asset manifest differs from the exact registry version.')
        for registered_name in expected_roles:
            require(library.read_file(reference, registered_name) == (destination/registered_name).read_bytes(),
                    'DIRECTOR_SOURCE', 'Staged asset bytes differ from the exact registry version.')
        identity = decode_json((destination/identity_name).read_bytes())
        require(isinstance(identity, dict) and record.get('source') == source_name and identity.get('id') == name and
                record.get('identity') == identity and record.get('identity_sha256') == object_hash(identity) and
                identity.get('source_sha256') == files[source_name]['sha256'],
                'DIRECTOR_SOURCE', 'Staged producer identity differs from its sealed source.')
        if name in ACTORS:
            rig = load_rig(destination/('rig-'+name))
            require(rig.manifest['id'] == name and rig.manifest['source']['sha256'] == identity['source_sha256'],
                    'DIRECTOR_SOURCE', 'Staged native rig does not match its sealed original.')
    return files['assets.json']['sha256']


def validate_lab_clearance(sample, triangles, lab_bounds, key_vertices):
    """Conservatively reject overlap with the interiors of the fixture boxes."""
    epsilon = 1e-6
    frame = sample['frame']

    def clear(lower, upper, actor):
        for part, (part_lower, part_upper) in lab_bounds.items():
            intersects = np.all((lower < part_upper-epsilon) & (upper > part_lower+epsilon), axis=-1)
            require(not np.any(intersects), 'DIRECTOR_CLEARANCE', 'Authored geometry overlaps a laboratory component.',
                    observed=dict(frame=frame, actor=actor, component=part))

    for actor, data in sample['actors'].items():
        for name, points in data['vertices'].items():
            faces = np.asarray(points)[triangles[actor][name]]
            clear(faces.min(axis=1), faces.max(axis=1), actor)

    key_matrix = np.asarray(sample['key_matrix'])
    key = transform_points(key_matrix, key_vertices)
    lower, upper = key.min(axis=0), key.max(axis=0)
    clear(lower, upper, 'access-key')
    bench_lower, bench_upper = lab_bounds['workbench-top']
    if frame <= PICKUP_FRAME:
        require(abs(lower[1]-bench_upper[1]) <= epsilon and
                np.all(lower[[0,2]] >= bench_lower[[0,2]]) and
                np.all(upper[[0,2]] <= bench_upper[[0,2]]),
                'DIRECTOR_SUPPORT', 'The resting key must sit within the tabletop footprint.')
    right_hand = np.concatenate([points for name, points in sample['actors']['aster']['vertices'].items()
                                 if name == 'hand-right' or (name.startswith('finger-') and name.endswith('-right'))])
    local_hand = transform_points(np.linalg.inv(key_matrix),right_hand)
    gap = np.maximum(key_vertices.min(axis=0)-local_hand, local_hand-key_vertices.max(axis=0))
    depth = max(0., -float(np.max(gap, axis=1).min()))
    require(depth <= .001, 'DIRECTOR_CONTACT', 'The right hand penetrates the key beyond the authored fixture tolerance.',
            observed=dict(frame=frame, depth_m=depth))
    distance = float(np.linalg.norm(np.maximum(gap,0), axis=1).min())
    if frame == PICKUP_FRAME:
        require(distance <= .003, 'DIRECTOR_CONTACT', 'The right hand misses the key at attachment.', observed=distance)
    return dict(key_tabletop_gap_m=float(lower[1]-bench_upper[1]), hand_gap_m=distance, hand_vertex_depth_m=depth)


def _evaluate_playback(project, project_sha256, authored_samples, key_frames):
    bundle = load_project(project)
    require(bundle['inventory']['project.json']['sha256'] == project_sha256,
            'DIRECTOR_PLAYBACK', 'Portable project differs from the verified binding.')
    shots = [bundle['documents'][ref['path']] for ref in bundle['project']['shots']]
    require([(shot['id'], shot['duration_frames'], shot['metadata'].get('sequence_start'),
              shot['metadata'].get('sequence_end')) for shot in shots] ==
            [('establish', 60, 0, 60), ('discovery', 60, 60, 120)],
            'DIRECTOR_PLAYBACK', 'Expected two contiguous half-open shots.')
    samples, cuts, cameras, verified = {}, [], {}, []
    static = None
    for shot in shots:
        start = shot['metadata']['sequence_start']
        raster = shot['render']
        for local in range(shot['duration_frames']):
            frame = start+local
            authored = authored_samples.get(str(frame))
            state = evaluate_scene(bundle, shot['scene']['id'], shot['id'], local, source_root=project,
                                   sample_geometry=authored is not None)
            nodes = state['nodes']
            key = nodes['access-key']['world_matrix']
            key_error = float(np.max(np.abs(np.asarray(key)-key_frames[frame]['matrix'])))
            require(key_error < 1e-7, 'DIRECTOR_PLAYBACK', 'Portable key playback differs from authored attachment.')
            key_frames[frame] = dict(frame=frame, matrix=key)
            pins = sum(len(node['material_overrides']) for node in nodes.values())
            require(pins == 0, 'DIRECTOR_PLAYBACK', 'This native fixture requires its original GLB materials.')
            current = dict(actors={actor:nodes[actor]['world_matrix'] for actor in ACTORS},
                           lights={name:dict(matrix=to_blender(node['world_matrix']).tolist(),
                                             energy=node['light']['intensity'],color=node['light']['color'],
                                             type=node['light']['type']) for name,node in nodes.items()
                                   if node['type']=='light'},
                           world={name:state['world'][name] for name in ('background','exposure')},
                           raster={name:raster[name] for name in ('width','height')})
            if static is None:
                static = current
            require(current == static and state['world']['environment'] is None and
                    all(light['type']=='area' for light in current['lights'].values()),
                    'DIRECTOR_PLAYBACK', 'Native fixture requires static actor placement, area lights and world.')
            camera_id = state['camera']
            if local == 0:
                cuts.append(dict(frame=frame, camera=camera_id))
            optics_pin = (nodes[camera_id]['camera'], nodes[camera_id]['world_matrix'])
            if camera_id not in cameras:
                camera = camera_matrices(state, raster)
                require(camera['optics']['projection']['type']=='perspective',
                        'DIRECTOR_PLAYBACK', 'This native fixture requires perspective cameras.')
                cameras[camera_id] = (optics_pin, camera)
            require(camera_id == shot['camera'] and optics_pin == cameras[camera_id][0],
                    'DIRECTOR_PLAYBACK', 'Native fixture requires one static camera per shot.')
            if authored is None:
                continue
            require(state['native_geometry_status']=='sampled' and set(state['native_geometry'])==set(ACTORS) and
                    camera_id==authored['camera'] and not state['authored_mutated'] and not state['render_evaluated'],
                    'DIRECTOR_PLAYBACK', 'Portable playback did not sample the expected native actors and camera.')
            vertices = 0
            maximum = 0.
            for actor,meshes in state['native_geometry'].items():
                expected = authored['actors'][actor]['vertices']
                require(set(meshes)==set(expected), 'DIRECTOR_PLAYBACK', 'Portable actor mesh inventory differs.')
                for name,points in meshes.items():
                    actual, original = np.asarray(points), np.asarray(expected[name])
                    require(actual.shape==original.shape, 'DIRECTOR_PLAYBACK', 'Portable actor vertex inventory differs.')
                    vertices += len(actual)
                    maximum=max(maximum,float(np.max(np.abs(actual-original))))
            require(maximum < 1e-7, 'DIRECTOR_PLAYBACK', 'Portable native geometry differs from authored playback.')
            samples[frame] = state
            verified.append(dict(frame=frame, shot=shot['id'], local_frame=local, camera=camera_id,
                                 vertices=vertices, max_actor_component_delta_m=maximum, key_matrix_delta=key_error))
    for frame in (60,59):
        before = samples[frame]
        repeated = evaluate_scene(bundle, before['scene']['id'], before['shot']['id'], before['frame']['numerator'],
                                  source_root=project, sample_geometry=True)
        require(repeated==before, 'DIRECTOR_PLAYBACK', 'Out-of-order playback changed at the shot cut.')
    require(load_project(project)['inventory'] == bundle['inventory'],
            'DIRECTOR_PLAYBACK', 'Playback changed the verified portable project.')
    return static, cuts, {name:value[1] for name,value in cameras.items()}, samples, dict(
        project_sha256=project_sha256, runtime_frames=120, authoring_terminal_frame=120, sampled=verified,
        out_of_order_frames=[60,59], material_pins=0, native_material_source='glb-import', render_evaluated=False)


def build_performance(asset_root,destination,*,blender=None):
    asset_root=Path(asset_root).absolute();destination=Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(),'DIRECTOR_EXISTS','Use a new performance destination.')
    require(destination.parent.is_dir() and all(not p.is_symlink() for p in destination.parents),'DIRECTOR_PATH','Unsafe destination parent.')
    stage=Path(tempfile.mkdtemp(prefix='.director-performance-',dir=destination.parent))
    try:
        source_assets_sha256 = _copy_assets(asset_root, stage/'assets')
        derived = stage/'actor-rigs'
        derived.mkdir()
        for name in ACTORS:
            shutil.copytree(stage/'assets'/('rig-'+name), derived/('rig-'+name))
        rigs={name:load_rig(derived/('rig-'+name)) for name in ACTORS}
        scene=scene_document();atomic_json(stage/'scene.json',scene)
        for actor,rig in rigs.items():
            author_clip(rig,actor);save_rig(rig,operation='director-laboratory-animation')
            export_gltf(rig,stage/(actor+'-animated.glb'))
        snapshots={str(frame):evaluate_frame(rigs,scene,frame) for frame in SAMPLES}
        # Authoring samples must remain stable before binding portable playback.
        for frame in reversed(SAMPLES):require(evaluate_frame(rigs,scene,frame)==snapshots[str(frame)],'DIRECTOR_ORDER','Frame evaluation changed with order.')
        from rig_import import load_character_model
        lab = load_character_model(stage/'assets/laboratory.glb').geometry
        lab_bounds = {mesh.id: (mesh.positions.min(axis=0),mesh.positions.max(axis=0)) for mesh in lab.meshes}
        key = load_character_model(stage/'assets/access-key.glb').geometry
        key_vertices = np.concatenate([mesh.positions for mesh in key.meshes])
        triangles = {actor: {mesh.id: mesh.triangles for mesh in rig.geometry.meshes} for actor, rig in rigs.items()}
        visibility = []
        key_frames = []
        clearance = []
        for frame in range(121):
            sample = snapshots.get(str(frame)) or evaluate_frame(rigs,scene,frame)
            visibility.append(validate_visibility(sample))
            clearance.append(validate_lab_clearance(sample,triangles,lab_bounds,key_vertices))
            key_frames.append(dict(frame=frame,matrix=sample['key_matrix']))
        require(clearance[-1]['key_tabletop_gap_m'] > .15, 'DIRECTOR_SUPPORT', 'The carried key must lift clear of the workbench.')
        binding = bind_scene_project(stage, stage/'scene-project', key_frames)
        state, cuts, cameras, playback, playback_receipt = _evaluate_playback(
            stage/'scene-project', binding['project_sha256'], snapshots, key_frames)
        # Both robots must articulate, independently of their static placement.
        for actor in ACTORS:
            displacement=max(np.linalg.norm(np.asarray(snapshots['0']['actors'][actor]['vertices'][mesh])-np.asarray(snapshots['119']['actors'][actor]['vertices'][mesh]),axis=1).max()
                for mesh in snapshots['0']['actors'][actor]['vertices'])
            require(displacement>.02,'DIRECTOR_MOTION','Robot has no measured geometric motion.')
        job=dict(mode='create',output=str(stage/'laboratory.blend'),report=str(stage/'blender-created.json'),
            actors={actor:dict(source=str(stage/(actor+'-animated.glb')),matrix=to_blender(state['actors'][actor]).tolist(),meshes=list(rig.geometry.by_id())) for actor,rig in rigs.items()},
            laboratory=str(stage/'assets/laboratory.glb'),key=str(stage/'assets/access-key.glb'),
            key_frames=[dict(frame=s['frame'],matrix=to_blender(s['matrix']).tolist()) for s in key_frames],
            cameras={name:dict(matrix=blender_camera_transform(camera['world_matrix']).tolist(),
                               optics=camera['optics']['projection']) for name,camera in cameras.items()},
            cuts=cuts, lights=state['lights'], world=state['world'], raster=state['raster'], samples=SAMPLES)
        atomic_json(stage/'creation-job.json',job)
        executable=find_blender(blender);worker=Path(__file__).with_name('director_scene_worker.py')
        def run(payload,name):
            atomic_json(stage/(name+'-job.json'),payload)
            try:
                result=subprocess.run([executable,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1','--python',str(worker),'--',str(stage/(name+'-job.json'))],capture_output=True,text=True,timeout=180)
            except subprocess.TimeoutExpired as exc:
                from rig_errors import RigError
                raise RigError('DIRECTOR_BLENDER_TIMEOUT', 'Native scene creation exceeded 180 seconds.') from exc
            (stage/(name+'.log')).write_text(result.stdout+result.stderr)
            require(result.returncode==0,'DIRECTOR_BLENDER','Blender scene adapter failed.',observed=(result.stdout+result.stderr)[-3000:])
            return json.loads(Path(payload['report']).read_text())
        created=run(job,'create')
        reopened=run(dict(mode='reopen',input=str(stage/'laboratory.blend'),report=str(stage/'blender-reopened.json'),samples=SAMPLES),'reopen')
        require(reopened['armatures'] == 2 and reopened['cameras'] == 2 and reopened['lights'] == 2, 'DIRECTOR_BLENDER_STRUCTURE', 'Native scene lacks required rig/camera/light structure.')
        require(set(reopened['light_state']) == set(job['lights']), 'DIRECTOR_BLENDER_LIGHTS', 'Native light identities differ.')
        for name, expected in job['lights'].items():
            actual = reopened['light_state'][name]
            require(np.allclose(actual['color'], expected['color'], atol=1e-6, rtol=0) and
                    np.allclose(actual['matrix'], expected['matrix'], atol=1e-6, rtol=0) and
                    math.isclose(actual['energy'], expected['energy'], abs_tol=1e-6),
                    'DIRECTOR_BLENDER_LIGHTS', 'Native light state differs from canonical scene.')
        require(np.allclose(reopened['world']['background'], job['world']['background'], atol=1e-6, rtol=0) and
                math.isclose(reopened['world']['exposure'], job['world']['exposure'], abs_tol=1e-6),
                'DIRECTOR_BLENDER_WORLD', 'Native world state differs from canonical scene.')
        require([x['frame'] for x in reopened['samples']] == SAMPLES, 'DIRECTOR_BLENDER_SAMPLES', 'Native sample inventory differs.')
        require(created['material_slots']==reopened['material_slots'] and
                len(reopened['material_slots'])==reopened['meshes'] and
                all(slots and all(slots) for slots in reopened['material_slots'].values()),
                'DIRECTOR_BLENDER_MATERIALS', 'Imported GLB material slots did not survive native reopen.')
        maximum=0.
        for sample, evidence in zip(reopened['samples'], playback_receipt['sampled']):
            native=playback[sample['frame']]
            require(set(sample['actors']) == set(rigs), 'DIRECTOR_BLENDER_SAMPLES', 'Missing independently evaluated actor.')
            require(sample['camera'] == native['camera'], 'DIRECTOR_BLENDER_CAMERA', 'Native camera cut differs.')
            camera=cameras[native['camera']]
            # Compare the native float32 matrix with the owner's expected Blender-basis matrix.
            world_error=float(np.max(np.abs(np.asarray(sample['camera_world'])-job['cameras'][native['camera']]['matrix'])))
            projection_error=float(np.max(np.abs(np.asarray(sample['camera_projection'])-camera['projection_matrix'])))
            require(max(world_error,projection_error)<1e-6, 'DIRECTOR_BLENDER_CAMERA', 'Native camera matrices differ from canonical playback.')
            evidence.update(blender_camera_world_delta=world_error,blender_projection_delta=projection_error)
            key_expected = transform_points(np.asarray(native['nodes']['access-key']['world_matrix']), key_vertices)
            require(np.asarray(sample['key']).shape == key_expected.shape and np.allclose(sample['key'], key_expected, atol=1e-5, rtol=0), 'DIRECTOR_BLENDER_KEY', 'Native prop differs from authoritative hand transform.')
            for actor,meshes in sample['actors'].items():
                require(set(meshes) == set(native['native_geometry'][actor]), 'DIRECTOR_BLENDER_SAMPLES', 'Missing independently evaluated mesh.')
                for mesh,points in meshes.items():
                    expected=np.asarray(native['native_geometry'][actor][mesh])
                    maximum=max(maximum,float(np.linalg.norm(np.asarray(points)-expected,axis=1).max()))
        require(maximum<.01,'DIRECTOR_BLENDER_MATCH','Blender deformation differs from native evaluation.',observed=maximum)
        report=dict(schema_version=1,fixture=True,production_approved=False,source_assets_sha256=source_assets_sha256,
            scene_sha256=sha256(stage/'scene.json'),scene_project=binding,native_sha256=sha256(stage/'laboratory.blend'),samples=SAMPLES,visibility=visibility,
            clearance=dict(evaluated_frames=len(clearance),
                           minimum_key_gap_m=min(frame['key_tabletop_gap_m'] for frame in clearance),
                           final_key_gap_m=clearance[-1]['key_tabletop_gap_m'],
                           pickup_hand_gap_m=clearance[PICKUP_FRAME]['hand_gap_m'],
                           maximum_hand_vertex_depth_m=max(frame['hand_vertex_depth_m'] for frame in clearance)),
            playback=playback_receipt,max_vertex_error_m=maximum,reopened=reopened,created=created,frames=120,fps=24,review_status='unreviewed')
        atomic_json(stage/'performance.json',report)
        _rename_no_replace(stage,destination)
        return report
    finally:
        if stage.exists():shutil.rmtree(stage)


def validate_visibility(sample):
    camera = np.asarray(sample['nodes'][sample['camera']]['world_matrix'])
    inverse = np.linalg.inv(camera)
    report = {}
    for actor, evaluated in sample['actors'].items():
        vertices = np.concatenate([np.asarray(v) for v in evaluated['vertices'].values()])
        local = np.column_stack((vertices, np.ones(len(vertices)))) @ inverse.T
        depth = -local[:, 2]
        require(np.all(depth > .05), 'DIRECTOR_VISIBILITY', 'Robot crosses camera near plane.')
        coordinates = local[:, :2] / depth[:, None] / math.tan(.9/2)
        coordinates[:, 0] /= 640/360
        require(np.all(np.abs(coordinates) < 1), 'DIRECTOR_VISIBILITY', 'Robot is clipped by selected camera.')
        report[actor] = dict(minimum=coordinates.min(axis=0).tolist(), maximum=coordinates.max(axis=0).tolist())
    return dict(frame=sample['frame'], camera=sample['camera'], robots=report)


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--blender')
    arguments=parser.parse_args()
    print(json.dumps(build_performance(arguments.assets,arguments.destination,blender=arguments.blender)))
