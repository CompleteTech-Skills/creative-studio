"""Native scene/rig adapter for the bounded Director two-robot performance."""
from __future__ import annotations
from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import numpy as np

from atomic_publish import _rename_no_replace
from camera3d_math import look_at, blender_camera_transform
from rig_animation import clip_from_poses, evaluate_animation
from rig_blender import find_blender
from rig_contracts import COORDINATES
from rig_errors import atomic_json, require, sha256, object_hash
from rig_gltf import export_gltf
from rig_ik import solve_ik
from rig_jobs import save_rig
from rig_math import axis_angle, compose, transform_points
from rig_models import load_rig
from rig_skeleton import neutral_pose, rotate_bone, evaluate_fk
from rig_skinning import evaluate_deformation
from scene3d_example import node
from scene3d_math import evaluate_transforms, to_blender
from scene3d_contracts import validate_document

ACTORS = {'aster': [-.5, 0., -1.65], 'moss': [1.2, 0., -.3]}
SAMPLES = [0, 36, 72, 96, 119]


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
            target = rest*(1-amount) + np.array([.30, 1.10+.25*lift, .35-.15*lift])*amount
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
    pickup=evaluate_animation(rigs['aster'],'aster-investigation',3.,contacts=False,constraints=False)['pose']
    hand=evaluate_fk(rigs['aster'].skeleton,pickup)['hand.right']
    key=(np.asarray(transforms['aster']['world_matrix'])@hand) if frame<72 else np.asarray(actors['aster']['bones']['hand.right'])
    return dict(frame=frame,time=dict(numerator=frame,denominator=24),nodes=transforms,actors=actors,
                key_matrix=key.tolist(),key_owner='aster' if frame>=72 else None,camera='wide' if frame<60 else 'investigation')



def _copy_assets(source, destination):
    from asset_library import read_source_bytes
    from asset_library_contracts import MAX_FILE_BYTES
    require(source.is_dir() and not source.is_symlink(), 'DIRECTOR_SOURCE', 'Asset source must be a regular directory.')
    files=[];total=0
    for path in source.rglob('*'):
        require(not path.is_symlink(), 'DIRECTOR_SOURCE', 'Asset source cannot contain symlinks.')
        require(path.is_dir() or path.is_file(), 'DIRECTOR_SOURCE', 'Asset source contains a special file.')
        if path.is_file():
            # Native job locks are process coordination files, not rig payloads.
            if path.parent == source and path.name in {'.rig-aster.rig.lock', '.rig-moss.rig.lock'}:
                continue
            total+=path.stat().st_size;files.append(path)
            require(len(files)<=4096 and total<=256*1024**2, 'DIRECTOR_LIMIT', 'Asset snapshot exceeds bounds.')
    destination.mkdir()
    for path in files:
        target=destination/path.relative_to(source);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(read_source_bytes(path, MAX_FILE_BYTES))
    # The four source identities are pinned independently from derived animation.
    report=json.loads((destination/'assets.json').read_text())
    for name in ['aster','moss','laboratory','access-key']:
        record=report['assets'][name]
        require(record['source'] == name+'.glb', 'DIRECTOR_SOURCE', 'Unexpected original source path.')
        require(sha256(destination/record['source'])==record['identity']['source_sha256'], 'DIRECTOR_SOURCE', 'Original source hash changed.')


def build_performance(asset_root,destination,*,blender=None):
    asset_root=Path(asset_root).absolute();destination=Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(),'DIRECTOR_EXISTS','Use a new performance destination.')
    require(destination.parent.is_dir() and all(not p.is_symlink() for p in destination.parents),'DIRECTOR_PATH','Unsafe destination parent.')
    stage=Path(tempfile.mkdtemp(prefix='.director-performance-',dir=destination.parent))
    try:
        _copy_assets(asset_root, stage/'assets')
        rigs={name:load_rig(stage/'assets'/('rig-'+name)) for name in ACTORS}
        scene=scene_document();atomic_json(stage/'scene.json',scene)
        for actor,rig in rigs.items():
            author_clip(rig,actor);save_rig(rig,operation='director-laboratory-animation')
            export_gltf(rig,stage/(actor+'-animated.glb'))
        snapshots={str(frame):evaluate_frame(rigs,scene,frame) for frame in SAMPLES}
        # Canonical frame evaluator is pure: order does not mutate the native state.
        for frame in reversed(SAMPLES):require(evaluate_frame(rigs,scene,frame)==snapshots[str(frame)],'DIRECTOR_ORDER','Frame evaluation changed with order.')
        visibility = [validate_visibility(sample) for sample in snapshots.values()]
        all_samples=[evaluate_frame(rigs,scene,frame,include_vertices=False) for frame in range(121)]
        from director_scene_binding import bind_scene_project
        binding = bind_scene_project(stage, stage/'scene-project', [dict(frame=s['frame'], matrix=s['key_matrix']) for s in all_samples])
        # Both robots must articulate, independently of their static placement.
        for actor in ACTORS:
            displacement=max(np.linalg.norm(np.asarray(snapshots['0']['actors'][actor]['vertices'][mesh])-np.asarray(snapshots['119']['actors'][actor]['vertices'][mesh]),axis=1).max()
                for mesh in snapshots['0']['actors'][actor]['vertices'])
            require(displacement>.02,'DIRECTOR_MOTION','Robot has no measured geometric motion.')
        transforms=evaluate_transforms(scene)
        job=dict(mode='create',output=str(stage/'laboratory.blend'),report=str(stage/'blender-created.json'),
            actors={actor:dict(source=str(stage/(actor+'-animated.glb')),matrix=to_blender(transforms[actor]['world_matrix']).tolist(),meshes=list(rig.geometry.by_id())) for actor,rig in rigs.items()},
            laboratory=str(stage/'assets/laboratory.glb'),key=str(stage/'assets/access-key.glb'),
            key_frames=[dict(frame=s['frame'],matrix=to_blender(s['key_matrix']).tolist()) for s in all_samples],
            cameras={name:dict(matrix=blender_camera_transform(transforms[name]['world_matrix']).tolist(),fov=.9) for name in scene['cameras']},
            lights={item['id']:dict(position=(np.array(item['transform']['translation'])[[0,2,1]]*np.array([1,-1,1])).tolist(),energy=item['light']['intensity']) for item in scene['nodes'] if item['type']=='light'},samples=SAMPLES)
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
        require([x['frame'] for x in reopened['samples']] == SAMPLES, 'DIRECTOR_BLENDER_SAMPLES', 'Native sample inventory differs.')
        from rig_import import load_character_model
        key_geometry = load_character_model(stage/'assets/access-key.glb').geometry
        key_vertices = np.concatenate([mesh.positions for mesh in key_geometry.meshes])
        maximum=0.
        for sample in reopened['samples']:
            native=snapshots[str(sample['frame'])]
            require(set(sample['actors']) == set(rigs), 'DIRECTOR_BLENDER_SAMPLES', 'Missing independently evaluated actor.')
            require(sample['camera'] == native['camera'], 'DIRECTOR_BLENDER_CAMERA', 'Native camera cut differs.')
            key_expected = transform_points(np.asarray(native['key_matrix']), key_vertices)
            require(np.asarray(sample['key']).shape == key_expected.shape and np.allclose(sample['key'], key_expected, atol=1e-5, rtol=0), 'DIRECTOR_BLENDER_KEY', 'Native prop differs from authoritative hand transform.')
            for actor,meshes in sample['actors'].items():
                require(set(meshes) == set(native['actors'][actor]['vertices']), 'DIRECTOR_BLENDER_SAMPLES', 'Missing independently evaluated mesh.')
                for mesh,points in meshes.items():
                    expected=np.asarray(native['actors'][actor]['vertices'][mesh])
                    maximum=max(maximum,float(np.linalg.norm(np.asarray(points)-expected,axis=1).max()))
        require(maximum<.01,'DIRECTOR_BLENDER_MATCH','Blender deformation differs from native evaluation.',observed=maximum)
        report=dict(schema_version=1,fixture=True,production_approved=False,source_assets_sha256=sha256(asset_root/'assets.json'),
            scene_sha256=sha256(stage/'scene.json'),scene_project=binding,native_sha256=sha256(stage/'laboratory.blend'),samples=SAMPLES,visibility=visibility,
            max_vertex_error_m=maximum,reopened=reopened,created=created,frames=120,fps=24,review_status='unreviewed')
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
