#!/usr/bin/env python3
"""Optional genuine 3D rigging CLI. Existing 2D commands do not import this engine."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from rig_errors import RigError, atomic_json, read_json, require, timestamp


def parser():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor');t=sub.add_parser('templates');t.add_argument('action',choices=['list'],default='list',nargs='?')
    commands=['import','analyze','auto','weights','validate','test-motion','status','resume','configure','inspect','bone-edit','weights-inspect',
              'correct','review-ui','review-apply','pose','solve-ik','animate','animation-import','animation-bake','retarget','state-map',
              'render','export','export-validate','archive','restore','compare','facial-controls','demo','serve']
    for name in commands:
        q=sub.add_parser(name);q.add_argument('--run-dir');q.add_argument('--destination');q.add_argument('--file');q.add_argument('--blender')
        if name=='import':
            q.add_argument('--model',required=True);q.add_argument('--id');q.add_argument('--scale',type=float,default=1.)
            q.add_argument('--fixture',action='store_true');q.add_argument('--origin',choices=['import','provider'],default='import');q.add_argument('--attribution',default='User-supplied model.')
        if name=='auto':q.add_argument('--template',default='humanoid');q.add_argument('--landmarks');q.add_argument('--replace',action='store_true')
        if name=='weights':
            q.add_argument('--method',choices=['auto','diffusion','distance','segmented','rigid'],default='auto');q.add_argument('--regions');q.add_argument('--iterations',type=int,default=16)
        if name=='validate':q.add_argument('--require-animation',action='store_true')
        if name in ('correct','review-apply'):q.add_argument('--receipt',required=True)
        if name=='bone-edit':q.add_argument('--bone',required=True);q.add_argument('--author',required=True);q.add_argument('--reason',required=True)
        if name=='weights-inspect':q.add_argument('--mesh',required=True);q.add_argument('--vertex',type=int,default=0)
        if name in ('pose','render','solve-ik'):
            q.add_argument('--pose');q.add_argument('--clip');q.add_argument('--time',type=float,default=0.)
        if name=='solve-ik':q.add_argument('--chain',required=True);q.add_argument('--target',type=float,nargs=3,required=True)
        if name=='render':q.add_argument('--engine',choices=['native','blender'],default='native');q.add_argument('--camera',choices=['front','side','back','three-quarter'],default='three-quarter');q.add_argument('--sheet',action='store_true');q.add_argument('--transparent',action='store_true')
        if name in ('retarget','animation-import'):q.add_argument('--source',required=True)
        if name=='retarget':q.add_argument('--clip',required=True);q.add_argument('--profile');q.add_argument('--partial',action='store_true')
        if name=='animation-bake':q.add_argument('--clip',required=True);q.add_argument('--fps',type=int)
        if name=='state-map':q.add_argument('--state',action='append');q.add_argument('--pack');q.add_argument('--recipes');q.add_argument('--approved-prop',action='append',default=[])
        if name=='export':q.add_argument('--format',choices=['glb','gltf','blender','blend','frames'],default='glb');q.add_argument('--clip');q.add_argument('--fps',type=int,default=12)
        if name=='compare':q.add_argument('--other-run',required=True)
        if name=='demo':q.add_argument('--archetype',choices=['humanoid','quadruped','winged-creature','robotic','all'],default='all');q.add_argument('--fps',type=int,default=12)
        if name=='serve':q.add_argument('--intake',required=True);q.add_argument('--port',type=int,default=8766);q.add_argument('--token')
    return p


def dispatch(args):
    name=args.command
    if name=='doctor':
        from rig_doctor import doctor
        return doctor()
    if name=='templates':
        from rig_templates import list_templates
        return dict(templates=list_templates())
    if name=='serve':
        from rig_http import serve
        require(args.run_dir,'RIG_ARGUMENT','--run-dir is required for the jobs root.')
        return serve(args.run_dir,args.intake,port=args.port,token=args.token)
    if name=='demo':
        from rig_demo import create_demo
        require(args.destination,'RIG_ARGUMENT','--destination is required.')
        result=[]
        for archetype in (['humanoid','quadruped','winged-creature','robotic'] if args.archetype=='all' else [args.archetype]):
            target=Path(args.destination)/archetype if args.archetype=='all' else Path(args.destination)
            rig,report=create_demo(target,archetype,fps=args.fps);result.append(dict(path=str(rig.root),report=report))
        return dict(demos=result,ok=all(r['report']['ok'] for r in result))
    if name=='restore':
        from rig_package import restore_rig
        require(args.file and args.destination,'RIG_ARGUMENT','--file and --destination are required.')
        return restore_rig(args.file,args.destination)
    if name=='export-validate':
        from rig_interop import validate_gltf
        require(args.file,'RIG_ARGUMENT','--file is required.');return validate_gltf(args.file)
    require(args.run_dir,'RIG_ARGUMENT','--run-dir is required.')
    if name=='import':
        from rig_jobs import create_run,status
        create_run(args.model,args.run_dir,identifier=args.id,scale=args.scale,fixture=args.fixture,origin=args.origin,attribution=args.attribution,blender=args.blender)
        return status(args.run_dir)
    if name=='status':
        from rig_jobs import status
        return status(args.run_dir)
    from rig_models import load_rig,rig_signature
    from rig_operations import mutate,pose_operation,solve_operation,export_operation,resume
    rig=load_rig(args.run_dir)
    if name=='facial-controls':
        from rig_facial import expression_catalog
        return expression_catalog(rig)
    if name=='resume':return resume(args.run_dir)
    if name in ('analyze','validate','test-motion'):return mutate(args.run_dir,name,dict(require_animation=getattr(args,'require_animation',False)))
    if name=='auto':
        parameters=dict(template=args.template,replace=args.replace)
        if args.landmarks is not None:parameters['landmarks']=read_json(args.landmarks)
        return mutate(args.run_dir,'auto',parameters)
    if name=='weights':return mutate(args.run_dir,'weights',dict(method=args.method,iterations=args.iterations,regions=read_json(args.regions) if args.regions else None))
    if name=='configure':
        require(args.file,'RIG_ARGUMENT','--file must contain limit overrides.');return mutate(args.run_dir,name,dict(limits=read_json(args.file)))
    if name=='inspect':
        from rig_skeleton import inspect_skeleton
        return dict(skeleton=inspect_skeleton(rig.skeleton),controllers=rig.manifest['controllers'],attachments=rig.manifest['attachments'])
    if name=='weights-inspect':
        require(args.mesh in rig.weights,'RIG_MESH','Mesh is not bound.');skin=rig.weights[args.mesh]
        require(0<=args.vertex<len(skin.weights),'RIG_VERTEX_INDEX','Vertex index is out of range.')
        return dict(mesh=args.mesh,vertex=args.vertex,locked=args.vertex in skin.locked,
                    influences={skin.joints[int(j)]:float(w) for j,w in zip(skin.indices[args.vertex],skin.weights[args.vertex]) if w>0})
    if name=='bone-edit':
        require(args.file,'RIG_ARGUMENT','--file must contain corrected bone fields.')
        correction=dict(schema_version=1,kind='hatch-3d-rig-correction',rig_id=rig.manifest['id'],rig_version=rig.manifest['version'],
            expected_sha256=rig_signature(rig.manifest),author=args.author,reason=args.reason,created_at=timestamp(),
            edits=[dict(operation='bone-update',target=args.bone,value=read_json(args.file))])
        return mutate(args.run_dir,'correct',dict(correction=correction))
    if name in ('correct','review-apply'):
        receipt=read_json(args.receipt);operation='correct' if receipt.get('kind')=='hatch-3d-rig-correction' else 'review'
        return mutate(args.run_dir,operation,{('correction' if operation=='correct' else 'receipt'):receipt})
    if name=='review-ui':
        from rig_viewer import create_review_ui
        require(args.destination,'RIG_ARGUMENT','--destination is required.');return create_review_ui(rig,args.destination)
    if name in ('pose','solve-ik','render'):
        params=dict(clip=args.clip,time=args.time)
        if args.file:params.update(read_json(args.file))
        if args.pose:
            if Path(args.pose).is_file():params['pose']=read_json(args.pose)
            elif args.pose!='neutral':params['clip']=args.pose
        if name=='solve-ik':params.update(chain=args.chain,target=args.target);result=solve_operation(rig,params)
        else:result=pose_operation(rig,params)
        if name=='render':
            from rig_render import render_rig_preview,render_pose_sheet
            require(args.destination,'RIG_ARGUMENT','--destination is required.')
            if args.engine=='blender':
                from rig_blender import render_blender_preview
                require(not args.sheet,'RIG_RENDER_MODE','Use native --sheet or Blender sampled poses.')
                return render_blender_preview(rig,args.destination,pose=result,camera=args.camera,executable=args.blender)
            if args.sheet:return render_pose_sheet(rig,args.destination)
            render_rig_preview(rig,args.destination,pose=result,camera=args.camera,transparent=args.transparent)
            return dict(path=str(Path(args.destination).absolute()),rig_sha256=rig_signature(rig.manifest))
        if args.destination:atomic_json(args.destination,result)
        return result
    if name=='animate':
        require(args.file,'RIG_ARGUMENT','--file must contain a clip or keyframe authoring document.')
        data=read_json(args.file);return mutate(args.run_dir,'animate',dict(clip=data) if data.get('kind')=='hatch-3d-animation' else data)
    if name=='animation-import':
        from rig_import import load_character_model
        value=load_character_model(args.source,blender=args.blender,limits=rig.manifest['limits'])
        require(value.clips,'RIG_ANIMATION_IMPORT','Input contains no executable animation channels.')
        results=[]
        for clip in value.clips.values():
            clip['rig_id']=rig.manifest['id'];clip['skeleton_version']=rig.skeleton['version']
            results.append(mutate(args.run_dir,'animate',dict(clip=clip)))
        return dict(imported=results)
    if name=='animation-bake':
        from rig_animation import bake_clip
        require(args.clip in rig.clips,'RIG_CLIP','Clip is missing.')
        return mutate(args.run_dir,'animate',dict(clip=bake_clip(rig,rig.clips[args.clip],fps=args.fps)))
    if name=='retarget':return mutate(args.run_dir,'retarget',dict(source=str(Path(args.source).absolute()),clip=args.clip,
        profile=read_json(args.profile) if args.profile else None,partial=args.partial))
    if name=='state-map':return mutate(args.run_dir,name,dict(recipes=read_json(args.recipes) if args.recipes else None,selected=args.state,pack=args.pack,approved_props=args.approved_prop))
    if name=='export':
        require(args.destination,'RIG_ARGUMENT','--destination is required.')
        return export_operation(rig,dict(destination=args.destination,format=args.format,clip=args.clip,fps=args.fps,blender=args.blender))
    if name=='archive':
        from rig_package import package_rig
        require(args.destination,'RIG_ARGUMENT','--destination is required.');return package_rig(args.run_dir,args.destination)
    if name=='compare':
        other=load_rig(args.other_run);a={b['id']:b for b in rig.skeleton['bones']};b={v['id']:v for v in other.skeleton['bones']}
        return dict(before=rig_signature(rig.manifest),after=rig_signature(other.manifest),source_equal=rig.manifest['source']['sha256']==other.manifest['source']['sha256'],
            added_bones=sorted(set(b)-set(a)),removed_bones=sorted(set(a)-set(b)),changed_bones=[k for k in a.keys()&b.keys() if a[k]!=b[k]],
            before_clips=list(rig.clips),after_clips=list(other.clips))
    raise RigError('RIG_COMMAND','Unsupported command.')


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result=dispatch(args);print(json.dumps(result,indent=2,allow_nan=False,default=lambda x:bool(x) if type(x).__name__=='bool' else str(x)))
        return 1 if isinstance(result,dict) and result.get('ok') is False else 0
    except RigError as exc:print(json.dumps(dict(ok=False,error=exc.report),indent=2));return 1
    except (OSError,ValueError,KeyError,TypeError,ImportError) as exc:
        error=RigError('RIG_INPUT_OR_DEPENDENCY',str(exc),stage=args.command,correction='Inspect the input and run rig.py doctor; install requirements-rig.txt for 3D commands.')
        print(json.dumps(dict(ok=False,error=error.report),indent=2));return 1


if __name__=='__main__':raise SystemExit(main())
