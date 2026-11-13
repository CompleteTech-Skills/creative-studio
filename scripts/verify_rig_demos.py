#!/usr/bin/env python3
"""Build/verify the actual three-character + mechanical + retarget acceptance set."""
from __future__ import annotations
import argparse
import importlib.metadata
from pathlib import Path
import platform
import shutil
import tempfile
import numpy as np
from rig_errors import atomic_json,read_json,require,safe_path,sha256,timestamp
from rig_models import load_rig,rig_signature


ARCHETYPES=('humanoid','quadruped','winged-creature','robotic')


def generate(root,*,fps=12):
    from rig_demo import create_demo
    from rig_jobs import save_rig
    from rig_states import map_states
    from rig_retarget import retarget_animation
    from rig_corrections import apply_rig_corrections
    root=Path(root).absolute();require(not root.exists(),'RIG_DEMO_EXISTS','Acceptance generation needs a new directory.');root.mkdir(parents=True)
    for name in ARCHETYPES:
        rig,_=create_demo(root/name,name,fps=fps)
        if name=='humanoid':
            correction=dict(schema_version=1,kind='hatch-3d-rig-correction',rig_id=rig.manifest['id'],rig_version=rig.manifest['version'],
                expected_sha256=rig_signature(rig.manifest),author='Procedural fixture author',reason='Lock a manually refined torso blend for correction coverage.',created_at=timestamp(),
                edits=[dict(operation='weights',target='torso',value=dict(assignments={'0':{'chest':.95,'spine':.05}},lock=True))])
            rig,_=apply_rig_corrections(rig,correction)
        mapping=map_states(rig);rig.manifest['state_mapping']=mapping['mapping'];save_rig(rig,operation='selective-catalog-mapping')
        atomic_json(rig.root/'animations/state-mapping.json',mapping)
        atomic_json(rig.root/'rig/skeleton.json',rig.skeleton);atomic_json(rig.root/'rig/controllers.json',rig.manifest['controllers'])
        print('generated '+name,flush=True)
    for name,proportions in [('humanoid',(.82,1.18,1.)),('quadruped',(.82,.72,.88))]:
        source=load_rig(root/name);target,_=create_demo(root/(name+'-retarget'),name,identifier='demo-'+name+'-retarget',proportions=proportions,animate=False,fps=fps)
        result=retarget_animation(source,target,'walk');target.clips[result['clip']['id']]=result['clip'];save_rig(target,operation='proportion-aware-retarget')
        atomic_json(target.root/'rig/retarget-profile.json',result['profile']);atomic_json(target.root/'validation/retarget.json',result['report'])
        print('retargeted '+name,flush=True)


def verify(root,*,blender=False,browser=False,validator=False,render=False,archives=False,specializations=False):
    from rig_validation import validate_rig,test_motion_ranges
    from rig_gltf import export_gltf
    from rig_import import load_character_model
    from rig_interop import compare_imported,validate_gltf
    root=Path(root).resolve();results=[];archive_directory=None
    archive_root = safe_path(root, 'packages') if archives else None
    for name in (*ARCHETYPES,'humanoid-retarget','quadruped-retarget'):
        rig=load_rig(root/name);numeric=validate_rig(rig,require_animation=True)
        require(numeric['ok'],'RIG_DEMO_NUMERIC','Fixture numerical acceptance failed.',asset=name,observed=numeric)
        atomic_json(rig.root/'validation/numeric.json',numeric)
        if name in ARCHETYPES:
            ranges=test_motion_ranges(rig);atomic_json(rig.root/'validation/motion-ranges.json',ranges)
            require(ranges['report']['ok'],'RIG_DEMO_RANGE','Motion range test failed.',asset=name)
        if name in ARCHETYPES[:3]:require(len(rig.clips)>=6,'RIG_DEMO_CLIPS','A required character has fewer than six real motions.')
        glb=rig.root/'exports/character.glb';export=export_gltf(rig,glb);export_gltf(rig,rig.root/'exports/character.gltf')
        atomic_json(rig.root/'validation/export.json',export)
        native=compare_imported(rig,load_character_model(glb));atomic_json(rig.root/'validation/native-roundtrip.json',native)
        require(native['ok'],'RIG_DEMO_NATIVE_ROUNDTRIP','Native export/import deformation comparison failed.',asset=name,observed=native)
        entry=dict(name=name,rig_sha256=rig_signature(rig.manifest),fixture=True,numeric=numeric['metrics'],
                   native_roundtrip=native,clips=list(rig.clips),artifacts=dict(glb=f'{name}/exports/character.glb'))
        if validator:
            report=validate_gltf(glb);atomic_json(rig.root/'validation/khronos.json',report)
            require(report['ok'],'RIG_DEMO_GLTF','Khronos found invalid glTF.',asset=name,observed=report['issues'])
            entry['khronos']=dict(errors=report['issues']['numErrors'],warnings=report['issues']['numWarnings'],version=report['validator'])
        if blender:
            from rig_blender import export_blender
            report=export_blender(rig,rig.root/'exports/character.blend');atomic_json(rig.root/'validation/blender.json',report)
            require(report['ok'],'RIG_DEMO_BLENDER','Independent Blender comparison failed.',asset=name,observed=report.get('max_vertex_error_m'))
            entry['blender']=dict(version=report['blender_version'],max_vertex_error_m=report['max_vertex_error_m'],reopened=report['reopened'])
            entry['artifacts']['blend']=f'{name}/exports/character.blend'
            from rig_blender import render_blender_preview
            selected='walk' if 'walk' in rig.clips else 'hinge-sweep' if 'hinge-sweep' in rig.clips else next(iter(rig.clips))
            with tempfile.TemporaryDirectory(prefix='hatch-demo-blender-preview-') as temporary:
                preview=render_blender_preview(rig,Path(temporary)/'frames',clip=selected,times=[0.,.65],
                    blend_path=rig.root/'exports/character.blend',blend_sha256=report['sha256'])
                folder=rig.root/'preview/blender';folder.mkdir(parents=True,exist_ok=True)
                for frame in preview['frames']:
                    source=Path(frame['path']);shutil.copyfile(source,folder/source.name);frame['path']='preview/blender/'+source.name
                atomic_json(rig.root/'validation/blender-preview.json',preview)
            entry['artifacts']['blender_preview']=f'{name}/preview/blender/frame-0001.png'
            if validator:
                second=validate_gltf(rig.root/'exports/character.roundtrip.glb');atomic_json(rig.root/'validation/blender-khronos.json',second)
                require(second['ok'],'RIG_DEMO_BLENDER_GLTF','Blender re-export failed independent glTF validation.',asset=name)
        if render:
            from rig_render import render_pose_sheet,render_frame_sequence
            sheet=render_pose_sheet(rig,rig.root/'preview/pose-sheet.png');atomic_json(rig.root/'validation/pose-sheet.json',sheet)
            clip='walk' if 'walk'in rig.clips else 'hinge-sweep' if 'hinge-sweep'in rig.clips else list(rig.clips)[0]
            destination=rig.root/'preview/frames'
            if not destination.exists():render_frame_sequence(rig,destination,clip=clip,fps=12,width=256,height=256)
            entry['artifacts']['pose_sheet']=f'{name}/preview/pose-sheet.png';entry['artifacts']['animation']=f'{name}/preview/frames/animation.webp'
        if browser:
            from rig_viewer import create_review_ui
            from rig_browser import verify_browser
            destination=rig.root/'review-ui'
            if not destination.exists():create_review_ui(rig,destination)
            report=verify_browser(rig,destination,screenshot=rig.root/'preview/browser.png');atomic_json(rig.root/'validation/threejs.json',report)
            require(report['ok'],'RIG_DEMO_BROWSER','Independent Three.js comparison failed.',asset=name,observed=report.get('max_vertex_error_m'))
            entry['threejs']=dict(version=report['version'],browser=report['browser'],max_vertex_error_m=report['max_vertex_error_m'],
                                  reduced_motion_mode=report['reduced_motion_mode'],
                                  reduced_motion_max_vertex_error_m=report['reduced_motion_max_vertex_error_m'])
            entry['artifacts']['viewer']=f'{name}/review-ui/index.html'
        job=read_json(rig.root/'job.json');job['rig_sha256']=rig_signature(rig.manifest)
        if blender and browser and validator:job['status']='roundtrip-verified'
        elif job['status']!='roundtrip-verified':job['status']='animation-ready'
        atomic_json(rig.root/'job.json',job)
        if archives:
            from rig_package import package_rig,restore_rig
            if archive_directory is None:
                package_root = archive_root
                package_root.mkdir(parents=True, exist_ok=True)
                archive_directory = Path(tempfile.mkdtemp(prefix='verification-', dir=package_root))
            path=archive_directory/(name+'.zip');package=package_rig(rig.root,path)
            with tempfile.TemporaryDirectory(prefix='hatch-demo-restore-') as temporary:
                restored=Path(temporary)/'rig';restore_rig(path,restored)
                require(validate_rig(load_rig(restored),require_animation=True)['ok'],'RIG_DEMO_ARCHIVE','Restored rig failed actual evaluation.')
            entry['archive']=dict(path=path.relative_to(root).as_posix(),sha256=package['sha256'],restored_and_evaluated=True)
        results.append(entry);print('verified '+name,flush=True)
        atomic_json(root/'acceptance.partial.json',dict(fixtures=results,complete=False))
    specialized=None
    if specializations:
        from rig_acceptance_fixtures import verify_extended_fixtures
        specialized=verify_extended_fixtures(root,blender=blender,render=render,validator=validator)
    output=dict(schema_version=1,created_at=timestamp(),status='automated-fixture-acceptance-passed',production_artwork_approved=False,
        visual_review='Agent inspection documented separately; production visual approval remains pending.',
        tool_versions=dict(python=platform.python_version(),numpy=importlib.metadata.version('numpy'),jsonschema=importlib.metadata.version('jsonschema'),Pillow=importlib.metadata.version('Pillow')),
        required_fixtures=[*ARCHETYPES,'humanoid-retarget','quadruped-retarget'],
        gates=dict(numeric=True,independent_khronos=validator,independent_blender=blender,independent_threejs=browser,
                   blender_cpu_render=blender,rendered_evidence=render,archive_restore=archives,
                   specializations=specializations),fixtures=results,specializations=specialized)
    atomic_json(root/'acceptance.json',output);(root/'acceptance.partial.json').unlink(missing_ok=True);return output


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--destination',default='examples/rig-3d');p.add_argument('--generate',action='store_true')
    for flag in ('blender','browser','validator','render','archives','specializations'):p.add_argument('--'+flag,action='store_true')
    a=p.parse_args()
    if a.generate:generate(a.destination)
    verify(a.destination,**{k:getattr(a,k) for k in ('blender','browser','validator','render','archives','specializations')})


if __name__=='__main__':main()
