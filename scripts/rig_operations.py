"""Reusable operation dispatcher shared by the CLI and loopback HTTP API."""
from copy import deepcopy
from pathlib import Path
import numpy as np
from rig_errors import atomic_json, read_json, require, safe_path, sha256
from rig_jobs import run_step, save_rig, configure_limits, next_version
from rig_models import load_rig, rig_signature


STATUSES={'analyze':'geometry-analyzed','auto':'skeleton-generated','weights':'weights-generated','validate':'structurally-valid',
          'test-motion':'motion-tested','correct':'needs-correction','review':'review-approved','animate':'animation-ready',
          'retarget':'animation-ready','state-map':'animation-ready','configure':'needs-correction'}


def mutate(root,operation,parameters=None):
    parameters=parameters or {}
    require(operation in STATUSES,'RIG_OPERATION','Unsupported mutation operation.')
    fields={'analyze':{'require_animation'},'auto':{'template','replace','landmarks'},'weights':{'method','regions','iterations'},
            'validate':{'require_animation'},'test-motion':{'require_animation'},'correct':{'correction'},'review':{'receipt'},
            'configure':{'limits'},'animate':{'clip','id','keyframes','fps','loop','contacts','events','reduce'},
            'retarget':{'source','clip','profile','partial'},'state-map':{'recipes','selected','pack','approved_props'}}
    require(set(parameters)<=fields[operation],'RIG_OPERATION_SCHEMA','Unexpected operation parameter.',observed=sorted(set(parameters)-fields[operation]))
    def action(rig):
        artifacts=[];result={}
        if operation=='analyze':
            from rig_geometry import analyze_geometry
            result=analyze_geometry(rig.geometry,rig.manifest['limits']);atomic_json(rig.root/'validation/geometry.json',result);artifacts=['validation/geometry.json']
        elif operation=='auto':
            from rig_templates import generate_skeleton
            from rig_contracts import validate_contract
            if rig.manifest['skeleton'] and not parameters.get('replace',False):
                return dict(preserved_existing_rig=True,bones=len(rig.skeleton['bones']))
            landmarks=parameters.get('landmarks')
            if landmarks is not None:
                require(isinstance(landmarks,dict) and set(landmarks)=={'source_sha256','landmarks'} and landmarks['source_sha256']==rig.manifest['source']['sha256'],
                        'RIG_LANDMARK_SOURCE','Landmarks must be bound to the exact source model hash.')
                landmarks=landmarks['landmarks']
            fitted=generate_skeleton(rig.geometry,parameters.get('template','humanoid'),landmarks=landmarks,character_id=rig.manifest['id'])
            old=rig.manifest['skeleton']
            if old:fitted['skeleton']['version']=next_version(old['version'])
            for key in ('skeleton','controllers','attachments','uncertainties'):rig.manifest[key]=fitted[key]
            rig.manifest['archetype']=fitted['template_id'];rig.weights={};rig.clips={};rig.manifest['state_mapping']={}
            rig.manifest['facial']['eyes']=[b['id'] for b in rig.skeleton['bones'] if b['role'].startswith('eye.')]
            save_rig(rig,operation='generate-skeleton');result=dict(bones=len(rig.skeleton['bones']),uncertainties=len(fitted['uncertainties']),job_status=fitted['status'])
        elif operation=='weights':
            from rig_skinning import generate_skin_weights
            rig.weights=generate_skin_weights(rig.geometry,rig.skeleton,method=parameters.get('method','auto'),
                regions=parameters.get('regions'),previous=rig.weights,iterations=parameters.get('iterations',16),limits=rig.manifest['limits'])
            save_rig(rig,operation='generate-weights');result=dict(meshes=len(rig.weights),methods=sorted(set(w.method for w in rig.weights.values())))
        elif operation in ('validate','test-motion'):
            from rig_validation import validate_rig,test_motion_ranges
            result=validate_rig(rig,require_animation=parameters.get('require_animation',False)) if operation=='validate' else test_motion_ranges(rig)
            name='validation/'+('rig' if operation=='validate' else 'motion-ranges')+'.json';atomic_json(rig.root/name,result);artifacts=[name]
            from rig_errors import object_hash
            retained='validation/reports/'+object_hash(result)+'.json';atomic_json(rig.root/retained,result)
            rig.manifest['validation'].append(dict(path=retained,sha256=sha256(rig.root/retained)))
            atomic_json(rig.root/'rig.json',rig.manifest)
            ok=result['ok'] if operation=='validate' else result['report']['ok']
            require(ok,'RIG_VALIDATION_FAILED','Numerical validation failed; inspect the retained report.',asset=name,preserved=str(rig.root))
        elif operation=='correct':
            from rig_corrections import apply_rig_corrections
            _,result=apply_rig_corrections(rig,parameters['correction'])
        elif operation=='review':
            from rig_review import apply_review
            result=apply_review(rig,parameters['receipt']);result['job_status']='review-approved' if result['status']=='approved' else 'needs-correction'
        elif operation=='configure':configure_limits(rig,parameters['limits']);result=dict(limits=rig.manifest['limits'])
        elif operation=='animate':
            from rig_animation import clip_from_poses,validate_animation,reduce_keys
            if 'clip' in parameters:clip=deepcopy(parameters['clip'])
            else:
                authored=parameters['keyframes'];clip=clip_from_poses(rig,parameters['id'],[k['time'] for k in authored],[k['pose'] for k in authored],
                    fps=parameters.get('fps',24),loop=parameters.get('loop',False),contacts=parameters.get('contacts'),events=parameters.get('events'))
            validate_animation(rig,clip)
            if parameters.get('reduce'):clip=reduce_keys(clip,tolerance=parameters['reduce'])
            rig.clips[clip['id']]=clip;save_rig(rig,operation='author-animation');result=dict(clip=clip['id'],tracks=len(clip['tracks']))
        elif operation=='retarget':
            from rig_retarget import retarget_animation
            source=load_rig(parameters['source']);value=retarget_animation(source,rig,parameters['clip'],profile=parameters.get('profile'),partial=parameters.get('partial',False))
            rig.clips[value['clip']['id']]=value['clip'];save_rig(rig,operation='retarget-animation')
            name='validation/retarget-'+value['clip']['id']+'.json';atomic_json(rig.root/name,value['report']);artifacts=[name];result=value['report']
        elif operation=='state-map':
            from rig_states import map_states
            result=map_states(rig,recipes=parameters.get('recipes'),selected=parameters.get('selected'),pack=parameters.get('pack'),approved_props=parameters.get('approved_props',[]))
            rig.manifest['state_mapping'].update(result['mapping']);save_rig(rig,operation='map-selected-states')
            atomic_json(rig.root/'animations/state-mapping.json',result);artifacts=['animations/state-mapping.json']
        return dict(result=result,artifact_paths=artifacts,**({'job_status':result['job_status']} if 'job_status' in result else {}))
    return run_step(root,operation,parameters,action,status=STATUSES[operation])


def pose_operation(rig,parameters):
    from rig_contracts import validate_contract
    from rig_skeleton import neutral_pose
    from rig_animation import evaluate_animation,save_pose
    from rig_facial import apply_expressions,gaze_at,finger_controls,appendage_controls
    from rig_constraints import apply_constraints
    pose=deepcopy(parameters['pose']) if isinstance(parameters.get('pose'),dict) else evaluate_animation(rig,parameters['clip'],parameters.get('time',0.))['pose'] if parameters.get('clip') else neutral_pose(rig.skeleton)
    validate_contract(pose,'Pose')
    require(pose['rig_version']==rig.skeleton['version'],'RIG_POSE_VERSION','Pose targets a different skeleton version.')
    if parameters.get('expressions'):pose=apply_expressions(rig,pose,parameters['expressions'])
    if parameters.get('gaze'):pose=gaze_at(rig,pose,parameters['gaze'],head_follow=parameters.get('head_follow',0.))
    if parameters.get('fingers'):pose=finger_controls(rig,pose,**parameters['fingers'])
    if parameters.get('appendages'):pose=appendage_controls(rig,pose,**parameters['appendages'])
    pose,_=apply_constraints(rig.skeleton,pose,rig.manifest['controllers']['constraints'])
    validate_contract(pose,'Pose')
    return pose


def solve_operation(rig,parameters):
    from rig_ik import solve_ik
    chain=next((c for c in rig.manifest['controllers']['ik'] if c['id']==parameters['chain']),None)
    require(chain is not None,'RIG_IK_CHAIN','Requested IK chain does not exist.')
    result=solve_ik(rig.skeleton,pose_operation(rig,parameters),chain,parameters['target'],
                    constraints=rig.manifest['controllers']['constraints'],orientation=parameters.get('orientation'))
    return dict(pose=result.pose,report=result.report())


def export_operation(rig,parameters):
    destination=Path(parameters['destination']).absolute();kind=parameters.get('format','glb')
    if kind in ('glb','gltf'):
        from rig_gltf import export_gltf
        require(destination.suffix=='.'+kind,'RIG_EXPORT_SUFFIX','Destination extension must match the requested format.')
        return export_gltf(rig,destination)
    if kind in ('blend','blender'):
        from rig_blender import export_blender
        return export_blender(rig,destination,executable=parameters.get('blender'))
    if kind=='frames':
        from rig_render import render_frame_sequence
        return render_frame_sequence(rig,destination,clip=parameters['clip'],fps=parameters.get('fps',12),camera=parameters.get('camera','three-quarter'))
    require(False,'RIG_EXPORT_FORMAT','Choose glb, gltf, blender or frames.')


def resume(root):
    job=read_json(Path(root)/'job.json');resumed=[]
    for name,step in list(job['steps'].items()):
        if step['status'] in ('failed','running','interrupted') and name in STATUSES:
            resumed.append(mutate(root,name,step['parameters']))
    return dict(resumed=resumed,status=read_json(Path(root)/'job.json')['status'])
