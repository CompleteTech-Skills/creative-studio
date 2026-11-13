"""Reusable operation dispatcher shared by the CLI and loopback HTTP API."""
from copy import deepcopy
from pathlib import Path
import math
import re
import numpy as np
from rig_errors import atomic_json, read_json, require, safe_path, sha256
from rig_jobs import run_step, save_rig, configure_limits, next_version, write_rig_manifest, retain_artifact
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
            result=analyze_geometry(rig.geometry,rig.manifest['limits']);artifacts=[retain_artifact(rig.root, 'geometry', result)]
        elif operation=='auto':
            from rig_templates import generate_skeleton
            from rig_contracts import validate_contract
            if rig.manifest['skeleton'] and not parameters.get('replace',False):
                return dict(preserved_existing_rig=True,bones=len(rig.skeleton['bones']))
            landmarks=parameters.get('landmarks')
            if 'landmarks' in parameters:
                require(isinstance(landmarks,dict) and set(landmarks)=={'source_sha256','landmarks'} and landmarks['source_sha256']==rig.manifest['source']['sha256']
                        and isinstance(landmarks['landmarks'],dict),
                        'RIG_LANDMARK_SOURCE','Landmarks must be bound to the exact source model hash.')
                landmarks=landmarks['landmarks']
            fitted=generate_skeleton(rig.geometry,parameters.get('template','humanoid'),landmarks=landmarks,character_id=rig.manifest['id'])
            old=rig.manifest['skeleton']
            if old:fitted['skeleton']['version']=next_version(old['version'])
            for key in ('skeleton','controllers','attachments','uncertainties'):rig.manifest[key]=fitted[key]
            rig.manifest['archetype']=fitted['template_id'];rig.weights={};rig.clips={};rig.manifest['state_mapping']={}
            rig.manifest['facial']['eyes']=[b['id'] for b in rig.skeleton['bones'] if b['role'].startswith('eye.')]
            if 'eyelids' in rig.manifest['facial']:
                eyes=set(rig.manifest['facial']['eyes'])
                rig.manifest['facial']['eyelids']=[binding for binding in rig.manifest['facial']['eyelids'] if binding['eye'] in eyes]
            from rig_facial import validate_eyelid_bindings
            validate_eyelid_bindings(rig)
            save_rig(rig,operation='generate-skeleton');result=dict(bones=len(rig.skeleton['bones']),uncertainties=len(fitted['uncertainties']),job_status=fitted['status'])
        elif operation=='weights':
            from rig_skinning import generate_skin_weights
            rig.weights=generate_skin_weights(rig.geometry,rig.skeleton,method=parameters.get('method','auto'),
                regions=parameters.get('regions'),previous=rig.weights,iterations=parameters.get('iterations',16),limits=rig.manifest['limits'])
            save_rig(rig,operation='generate-weights');result=dict(meshes=len(rig.weights),methods=sorted(set(w.method for w in rig.weights.values())))
        elif operation in ('validate','test-motion'):
            from rig_validation import validate_rig,test_motion_ranges
            result=validate_rig(rig,require_animation=parameters.get('require_animation',False)) if operation=='validate' else test_motion_ranges(rig)
            retained=retain_artifact(rig.root, operation, result);artifacts=[retained]
            name=retained
            rig.manifest['validation'].append(dict(path=retained,sha256=sha256(rig.root/retained)))
            write_rig_manifest(rig.root, rig.manifest)
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
            name=retain_artifact(rig.root, 'retarget-'+value['clip']['id'], value['report']);artifacts=[name];result=value['report']
        elif operation=='state-map':
            from rig_states import map_states
            from rig_models import RigAsset
            from rig_validation import validate_rig
            # Mapping saves a new revision, so compile against the review state
            # that save_rig will publish rather than the superseded approval.
            rig.manifest['review']=dict(status='pending', receipts=[])
            result=map_states(rig,recipes=parameters.get('recipes'),selected=parameters.get('selected'),pack=parameters.get('pack'),approved_props=parameters.get('approved_props',[]))
            for previous in rig.manifest['state_mapping'].values():
                if previous['status'] in ('supported','supported-with-adaptation'):
                    previous.update(status='needs-review', reason=('A new rig revision requires renewed visual review. '+previous['reason'])[:4096])
            rig.manifest['state_mapping'].update(result['mapping']);save_rig(rig,operation='map-selected-states')
            clip_ids=sorted({clip for recipe in result['recipes'].values() for clip in recipe['clips']})
            # Share immutable arrays, skeleton and bindings; validate only the
            # requested recipes without scanning unrelated animation clips.
            selected=RigAsset(rig.manifest, rig.geometry, rig.weights,
                              {name:rig.clips[name] for name in clip_ids}, rig.root)
            report=validate_rig(selected, require_animation=False)
            report['kind']='hatch-3d-selected-state-readiness'
            structure=any(check['name']=='structure' and check['status']=='passed' for check in report['checks'])
            status=('animation-ready' if report['ok'] and result['recipes'] else
                    'structurally-valid' if report['ok'] else
                    'skeleton-generated' if structure and not rig.weights else 'needs-correction')
            result.update(job_status=status, readiness=dict(scope='selected-recipes-only',
                clip_ids=clip_ids, executable_recipes=len(result['recipes']), report=report,
                full_package_acceptance=False, visual_approval=False))
            readiness=retain_artifact(rig.root, 'selected-state-readiness', result['readiness'])
            rig.manifest['validation'].append(dict(path=readiness, sha256=sha256(rig.root/readiness)))
            write_rig_manifest(rig.root, rig.manifest)
            artifacts=[readiness, retain_artifact(rig.root, 'state-mapping', result)]
        return dict(result=result,artifact_paths=artifacts,**({'job_status':result['job_status']} if 'job_status' in result else {}))
    return run_step(root,operation,parameters,action,status=STATUSES[operation])


POSE_FIELDS = {'pose', 'clip', 'time', 'expressions', 'gaze', 'head_follow', 'fingers', 'appendages'}
SOLVE_FIELDS = {'chain', 'target', 'pole', 'orientation'}


def _number(value, lower, upper, field):
    require(type(value) in (int, float) and lower <= value <= upper and math.isfinite(value),
            'RIG_OPERATION_SCHEMA', 'Expected a finite bounded number for '+field, asset=field)


def _vector(value, size, field):
    require(isinstance(value, (list, tuple)) and len(value) == size,
            'RIG_OPERATION_SCHEMA', 'Expected a numeric vector for '+field, asset=field)
    for number in value:_number(number, -1e9, 1e9, field)


def _object(value, allowed, field, required=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= allowed,
            'RIG_OPERATION_SCHEMA', 'Expected a closed object for '+field, asset=field)


def _pose_parameters(parameters, *, solve=False):
    _object(parameters, POSE_FIELDS | (SOLVE_FIELDS if solve else set()), 'parameters')
    if 'pose' in parameters:require(isinstance(parameters['pose'], dict), 'RIG_OPERATION_SCHEMA', 'pose must be a Pose object.')
    if parameters.get('clip') is not None:
        require(isinstance(parameters['clip'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', parameters['clip']),
                'RIG_OPERATION_SCHEMA', 'clip must be an existing clip identifier or null.')
    if 'time' in parameters:_number(parameters['time'], 0, 1e9, 'time')
    if 'head_follow' in parameters:_number(parameters['head_follow'], 0, 1, 'head_follow')
    if 'gaze' in parameters and parameters['gaze'] is not None:_vector(parameters['gaze'], 3, 'gaze')
    if 'expressions' in parameters:
        expressions=parameters['expressions']
        require(isinstance(expressions, dict) and len(expressions) <= 128,
                'RIG_OPERATION_SCHEMA', 'expressions must be an object with at most 128 entries.')
        for name, amount in expressions.items():
            require(isinstance(name, str) and 0 < len(name) <= 4096, 'RIG_OPERATION_SCHEMA', 'Expression names must be bounded strings.')
            _number(amount, 0, 1, 'expressions.'+name)
    if 'fingers' in parameters:
        fingers=parameters['fingers'];_object(fingers, {'side','curl','spread','fingers'}, 'fingers', {'side'})
        require(fingers['side'] in ('left','right'), 'RIG_OPERATION_SCHEMA', 'Finger side must be left or right.')
        if 'curl' in fingers:_number(fingers['curl'], 0, 1, 'fingers.curl')
        if 'spread' in fingers:_number(fingers['spread'], -1, 1, 'fingers.spread')
        names=fingers.get('fingers')
        if names is not None:
            require(isinstance(names, list) and 1 <= len(names) <= 32
                    and all(isinstance(name, str) and 0 < len(name) <= 128 for name in names)
                    and len(set(names)) == len(names), 'RIG_OPERATION_SCHEMA', 'Finger names must be a bounded, nonempty unique list.')
    if 'appendages' in parameters:
        values=parameters['appendages'];_object(values, {'time','tail_curl','tail_wag','wing_flap','wing_fold','wing_twist','wing_side','seed','secondary'}, 'appendages')
        for name in ('tail_curl','tail_wag','wing_flap','wing_fold','wing_twist','secondary'):
            if name in values:_number(values[name], -1, 1, 'appendages.'+name)
        if 'time' in values:_number(values['time'], 0, 120, 'appendages.time')
        if 'seed' in values:require(type(values['seed']) is int and -(2**31) <= values['seed'] < 2**31,
                                   'RIG_OPERATION_SCHEMA', 'Appendage seed must be a bounded integer.')
        if 'wing_side' in values:require(values['wing_side'] in ('both','left','right'), 'RIG_OPERATION_SCHEMA', 'Wing side must be both, left or right.')
    if solve:
        require('chain' in parameters and isinstance(parameters['chain'], str) and 0 < len(parameters['chain']) <= 128,
                'RIG_OPERATION_SCHEMA', 'A bounded chain identifier is required.')
        require('target' in parameters, 'RIG_OPERATION_SCHEMA', 'A target vector is required.')
        _vector(parameters['target'], 3, 'target')
        if parameters.get('pole') is not None:_vector(parameters['pole'], 3, 'pole')
        if parameters.get('orientation') is not None:
            from rig_math import quaternion
            _vector(parameters['orientation'], 4, 'orientation');quaternion(parameters['orientation'], strict=True)


def _check_pose(rig, pose):
    from rig_contracts import validate_contract
    from rig_skeleton import evaluate_fk
    validate_contract(pose, 'Pose')
    require(pose['rig_version'] == rig.skeleton['version'], 'RIG_POSE_VERSION', 'Pose targets a different skeleton version.')
    evaluate_fk(rig.skeleton, pose)
    meshes=rig.geometry.by_id()
    require(set(pose['morphs']) <= set(meshes), 'RIG_MORPH_REFERENCE', 'Pose references a missing mesh.')
    for mesh, weights in pose['morphs'].items():
        require(set(weights) <= set(meshes[mesh].morphs), 'RIG_MORPH_REFERENCE', 'Pose references a missing morph.')
        for amount in weights.values():_number(amount, -2, 2, 'pose.morphs')


def pose_operation(rig,parameters):
    from rig_skeleton import neutral_pose
    from rig_animation import evaluate_animation
    from rig_facial import apply_expressions,gaze_at,finger_controls,appendage_controls
    from rig_constraints import apply_constraints
    _pose_parameters(parameters)
    if parameters.get('clip') is not None:require(parameters['clip'] in rig.clips, 'RIG_CLIP', 'Requested clip does not exist.')
    pose=deepcopy(parameters['pose']) if 'pose' in parameters else evaluate_animation(rig,parameters['clip'],parameters.get('time',0.))['pose'] if parameters.get('clip') else neutral_pose(rig.skeleton)
    _check_pose(rig, pose)
    if parameters.get('expressions'):pose=apply_expressions(rig,pose,parameters['expressions'])
    if 'gaze' in parameters:pose=gaze_at(rig,pose,parameters['gaze'],head_follow=parameters.get('head_follow',0.))
    if parameters.get('fingers'):pose=finger_controls(rig,pose,**parameters['fingers'])
    if parameters.get('appendages'):pose=appendage_controls(rig,pose,**parameters['appendages'])
    pose,_=apply_constraints(rig.skeleton,pose,rig.manifest['controllers']['constraints'])
    _check_pose(rig, pose)
    return pose


def solve_operation(rig,parameters):
    from rig_ik import solve_ik
    _pose_parameters(parameters, solve=True)
    chain=next((c for c in rig.manifest['controllers']['ik'] if c['id']==parameters['chain']),None)
    require(chain is not None,'RIG_IK_CHAIN','Requested IK chain does not exist.')
    pose_parameters={name:value for name,value in parameters.items() if name in POSE_FIELDS}
    result=solve_ik(rig.skeleton,pose_operation(rig,pose_parameters),chain,parameters['target'],
                    constraints=rig.manifest['controllers']['constraints'],pole=parameters.get('pole'),
                    orientation=parameters.get('orientation'))
    _check_pose(rig, result.pose)
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
