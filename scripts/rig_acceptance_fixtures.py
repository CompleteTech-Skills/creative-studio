"""Specialty engineering-fixture recipes consumed by the authoritative demo gate.

These functions extend existing source-bound demonstrations; they do not replace
verify_rig_demos or supply subjective production/artistic approval.
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import numpy as np
from rig_animation import bake_clip, clip_from_poses, evaluate_animation
from rig_errors import atomic_json, object_hash, require, sha256
from rig_jobs import code_signature, save_rig
from rig_models import Mesh, SkinWeights, load_rig, rig_signature
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation


FIXTURES=('humanoid','quadruped','winged-creature','robotic','humanoid-retarget','quadruped-retarget')


def _code_reference(module_path):
    import subprocess
    module_path=Path(module_path).resolve()
    try:
        head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=module_path.parents[1],text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.CalledProcessError):head=None
    return dict(head=head,module_sha256=sha256(module_path))


def _inventory(root):
    result={}
    for path in sorted(Path(root).rglob('*')):
        require(not path.is_symlink(),'RIG_FIXTURE_SYMLINK','Specialty proof inputs must not contain symlinks.',asset=str(path))
        if path.is_file():result[path.relative_to(root).as_posix()]=sha256(path)
    return result


def _derived_run(rig,destination):
    destination=Path(destination)
    require(rig.root is not None and rig.manifest['source']['fixture'], 'RIG_FIXTURE_SOURCE',
            'Specialty recipes require an existing explicitly labelled engineering fixture.')
    require(not destination.exists(),'RIG_FIXTURE_EXISTS','Use a new derived proof directory.')
    source=Path(rig.root).resolve()
    require(source not in destination.resolve().parents,'RIG_FIXTURE_PATH','Derived run must be outside the input run.')
    shutil.copytree(source,destination)
    result=deepcopy(rig);result.root=destination.resolve()
    return result


def _export(rig,directory,*,blender=False):
    from rig_gltf import export_gltf
    from rig_import import load_character_model
    from rig_interop import compare_imported
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    glb=directory/'fixture.glb';gltf=directory/'fixture.gltf'
    binary=export_gltf(rig,glb);text=export_gltf(rig,gltf)
    native=compare_imported(rig,load_character_model(glb),sample_times=(.125,.5,.875))
    require(native['ok'],'RIG_FIXTURE_ROUNDTRIP','Specialty fixture lost evaluated deformation on native reimport.',observed=native)
    result=dict(glb=dict(path=str(glb),sha256=binary['sha256']),gltf=dict(path=str(gltf),sha256=text['sha256']),
                native_roundtrip=native,independent_interoperability='Requires the authoritative Khronos/Blender/browser gates.')
    if blender:
        from rig_blender import export_blender
        receipt=export_blender(rig,directory/'fixture.blend')
        require(receipt['ok'] and receipt['reopened'],'RIG_FIXTURE_BLENDER','Specialty fixture failed independent Blender reopen.',observed=receipt)
        result['blender']=receipt
    return result


def garment_proof(rig,directory,*,blender=False):
    """Bind a separate sleeve to actual arm weights and prove nonrigid motion."""
    directory=Path(directory);derived=_derived_run(rig,directory/'run')
    require('arm-right' in derived.geometry.by_id() and 'wave' in derived.clips,
            'RIG_GARMENT_FIXTURE','Humanoid proof requires actual arm-right geometry and wave animation.')
    original=derived.geometry.by_id()['arm-right'];garment=deepcopy(original);garment.id='proof-skinned-sleeve'
    center=garment.positions.mean(axis=0);garment.positions=center+(garment.positions-center)*1.035
    garment.name='Explicit derived synthetic sleeve';garment.region='proof-garment'
    garment.material=len(derived.geometry.materials)
    derived.geometry.materials.append(dict(name='proof-sleeve',pbrMetallicRoughness=dict(baseColorFactor=[.9,.1,.65,1.],metallicFactor=0.,roughnessFactor=.7)))
    binding=deepcopy(derived.weights[original.id]);binding.mesh=garment.id
    require(len(set(binding.indices[binding.weights>0].tolist()))>=2,'RIG_GARMENT_BINDING','Sleeve must use multiple represented deform joints.')
    derived.geometry.meshes.append(garment);derived.weights[garment.id]=binding
    derived.clips={'wave':derived.clips['wave']}
    save_rig(derived,operation='explicit-synthetic-skinned-garment');derived=load_rig(derived.root)
    neutral=evaluate_deformation(derived,neutral_pose(derived.skeleton))[garment.id]['positions']
    indices=np.linspace(0,len(neutral)-1,min(64,len(neutral)),dtype=int)
    distances=np.linalg.norm(neutral[indices,None,:]-neutral[None,indices,:],axis=2)
    displacement=nonrigid=0.
    for time in (.25,.5,.75,1.):
        vertices=evaluate_deformation(derived,evaluate_animation(derived,'wave',time)['pose'])[garment.id]['positions']
        displacement=max(displacement,float(np.linalg.norm(vertices-neutral,axis=1).max()))
        nonrigid=max(nonrigid,float(np.max(np.abs(np.linalg.norm(vertices[indices,None,:]-vertices[None,indices,:],axis=2)-distances))))
    require(displacement>1e-3 and nonrigid>1e-5,'RIG_GARMENT_DEFORMATION','Separate garment must deform nonrigidly under real arm motion.',observed=dict(displacement_m=displacement,pair_distance_change_m=nonrigid))
    report=dict(scope='explicit derived synthetic skinned sleeve, not cloth simulation',source_rig_sha256=rig_signature(rig.manifest),
        derived_rig_sha256=rig_signature(derived.manifest),derived_root=str(derived.root),mesh=garment.id,
        vertex_count=len(garment.positions),triangle_count=len(garment.triangles),max_vertex_displacement_m=displacement,
        max_pair_distance_change_m=nonrigid,artifacts=_export(derived,directory/'export',blender=blender),visual_review='pending')
    atomic_json(directory/'garment-proof.json',report);return report


def tail_proof(rig,directory,*,blender=False):
    """Aim an existing tail chain and bake deterministic secondary motion."""
    from rig_facial import appendage_controls
    from rig_ik import solve_ik
    directory=Path(directory);derived=_derived_run(rig,directory/'run')
    chain=next((c for c in derived.manifest['controllers']['ik'] if c['id']=='tail-target'),None)
    require(chain is not None and 'tail-mesh' in derived.geometry.by_id(),'RIG_TAIL_FIXTURE','Proof requires an existing bound tail and tail-target controller.')
    neutral=neutral_pose(derived.skeleton);rest=evaluate_fk(derived.skeleton)
    root=rest[chain['bones'][0]][:3,3];end=rest[chain['bones'][-1]][:3,3]
    target=root+(end-root)*.95+np.array([.02,.01,0.])
    solved=solve_ik(derived.skeleton,neutral,chain,target,constraints=derived.manifest['controllers']['constraints'])
    require(solved.converged,'RIG_TAIL_TARGET','Existing tail solver must reach the explicit fixture target.',observed=solved.report())
    actual=evaluate_fk(derived.skeleton,solved.pose)[chain['bones'][-1]][:3,3]
    endpoint_error=float(np.linalg.norm(actual-target))
    before=evaluate_deformation(derived,neutral)['tail-mesh']['positions']
    after=evaluate_deformation(derived,solved.pose)['tail-mesh']['positions']
    target_displacement=float(np.linalg.norm(after-before,axis=1).max())
    require(endpoint_error<=chain['tolerance'] and target_displacement>1e-3,'RIG_TAIL_TARGET','Tail target must move real bound vertices and meet its tolerance.')
    times=np.linspace(0.,1.,9).tolist()
    poses=[appendage_controls(derived,neutral,time=t,tail_curl=.2,tail_wag=.3,secondary=.6,seed=13) for t in times]
    repeat=[appendage_controls(derived,neutral,time=t,tail_curl=.2,tail_wag=.3,secondary=.6,seed=13) for t in times]
    require(poses==repeat,'RIG_TAIL_SEED','Seeded secondary motion must reproduce authoritative poses exactly.')
    other=appendage_controls(derived,neutral,time=.5,tail_curl=.2,tail_wag=.3,secondary=.6,seed=29)
    seed_difference=float(np.linalg.norm(evaluate_deformation(derived,poses[4])['tail-mesh']['positions']-
                                          evaluate_deformation(derived,other)['tail-mesh']['positions'],axis=1).max())
    require(seed_difference>1e-6,'RIG_TAIL_SEED','Changing the explicit secondary seed must change real tail geometry.')
    aim=clip_from_poses(derived,'proof-tail-aim',[0.,1.],[neutral,solved.pose],fps=8)
    authored=clip_from_poses(derived,'proof-tail-secondary',times,poses,fps=8)
    baked=bake_clip(derived,authored,fps=8)
    portable=deepcopy(derived);portable.manifest['controllers']=dict(schema_version=1,ik=[],constraints=[])
    max_bake_error=0.
    for time in times:
        a=evaluate_deformation(derived,evaluate_animation(derived,authored,time)['pose'])['tail-mesh']['positions']
        b=evaluate_deformation(portable,evaluate_animation(portable,baked,time)['pose'])['tail-mesh']['positions']
        max_bake_error=max(max_bake_error,float(np.linalg.norm(a-b,axis=1).max()))
    require(max_bake_error<=1e-8,'RIG_TAIL_BAKE','Baked ordinary channels must reproduce sampled constrained tail geometry.',observed=max_bake_error)
    derived.clips={aim['id']:aim,baked['id']:baked};save_rig(derived,operation='explicit-synthetic-tail-target-and-bake');derived=load_rig(derived.root)
    report=dict(scope='existing tail-target IK and seeded bounded secondary controls baked to ordinary channels',
        source_rig_sha256=rig_signature(rig.manifest),derived_rig_sha256=rig_signature(derived.manifest),derived_root=str(derived.root),
        chain=chain['id'],target=target.tolist(),endpoint_error_m=endpoint_error,tolerance_m=chain['tolerance'],
        max_target_vertex_displacement_m=target_displacement,seed=13,max_changed_seed_vertex_delta_m=seed_difference,
        max_baked_sample_vertex_error_m=max_bake_error,sample_times=times,artifacts=_export(derived,directory/'export',blender=blender),visual_review='pending')
    atomic_json(directory/'tail-proof.json',report);return report


def grip_render_proof(rig,directory,*,blender=False,render=False):
    """Use the cooperative grip authority, then render its exact posed geometry."""
    import importlib
    from rig_attachments import socket_world
    from rig_demo import tube
    from rig_math import decompose
    try:grips_api=importlib.import_module('rig_grips')
    except ImportError as exc:
        from rig_errors import RigError
        raise RigError('RIG_GRIP_DEPENDENCY','Specialty grip proof needs the reviewed rig_grips authority.') from exc
    directory=Path(directory);derived=_derived_run(rig,directory/'run');neutral=neutral_pose(derived.skeleton)
    prop=dict(id='proof-grip-bar',owner=None,transform=decompose(np.eye(4)))
    grips=[]
    for side in ('left','right'):
        matrix=socket_world(derived,neutral,'grip.'+side)
        matrix[:3,3]+=np.array([.22 if side=='left' else -.22,.15,.15])
        grips.append(dict(id='hold.'+side,chain='arm.'+side,socket='grip.'+side,transform=decompose(matrix)))
    material=len(derived.geometry.materials)
    derived.geometry.materials.append(dict(name='proof-grip-bar',pbrMetallicRoughness=dict(baseColorFactor=[1.,.05,.7,1.],metallicFactor=0.,roughnessFactor=.7)))
    bar=tube('proof-grip-bar',[g['transform']['translation'] for g in grips],.035,material,sides=12,subdivisions=3)
    derived.geometry.meshes.append(bar);root=derived.skeleton['root']
    # Keep the source nondeforming hierarchy root unchanged. The derived static
    # prop has its own explicit deforming anchor and positive skin influences.
    anchor=deepcopy(next(b for b in derived.skeleton['bones'] if b['id']==root))
    anchor.update(id='proof-grip-anchor',role='prop-anchor',parent=root,deform=True,mirror=None,
                  rest=dict(translation=[0.,0.,0.],rotation=[0.,0.,0.,1.],scale=[1.,1.,1.]),
                  evidence=dict(source='procedural-fixture',reference='Explicit derived static grip-bar anchor',confidence=1.))
    derived.skeleton['bones'].append(anchor)
    rest=evaluate_fk(derived.skeleton)
    bar_weights=np.zeros((len(bar.positions),4));bar_weights[:,0]=1.
    derived.weights[bar.id]=SkinWeights(bar.id,[anchor['id']],np.zeros((len(bar.positions),4),dtype=np.int64),
        bar_weights,np.linalg.inv(rest[anchor['id']])[None,:,:],method='rigid')
    save_rig(derived,operation='explicit-synthetic-grip-object')
    recipe=grips_api.create_grip_pose(derived,'proof-bar-grip','1.0.0',prop,grips)
    solved=grips_api.solve_grip_pose(derived,neutral,prop,recipe)
    clip=clip_from_poses(derived,'proof-two-hand-grip',[0.,1.],[neutral,solved['pose']],fps=8)
    derived.clips={clip['id']:clip};save_rig(derived,operation='explicit-synthetic-grip-animation');derived=load_rig(derived.root)
    # Bind the retained reusable recipe to the final derived closure revision.
    recipe=grips_api.create_grip_pose(derived,'proof-bar-grip','1.0.0',prop,grips)
    solved=grips_api.solve_grip_pose(derived,neutral,prop,recipe)
    require(solved['prop']==prop and not solved['report']['ownership_changed'],'RIG_GRIP_OWNER','Demonstration solving must preserve explicit prop ownership.')
    evaluated=evaluate_deformation(derived,solved['pose']);base=evaluate_deformation(derived,neutral)
    hand_displacement={side:float(np.linalg.norm(evaluated['hand-'+side]['positions']-base['hand-'+side]['positions'],axis=1).max()) for side in ('left','right')}
    require(all(v>.05 for v in hand_displacement.values()),'RIG_GRIP_GEOMETRY','Both actual weighted hands must move toward the bar.',observed=hand_displacement)
    hand_to_grip={side:float(np.linalg.norm(evaluated['hand-'+side]['positions']-
        np.asarray(next(c['grip_world'] for c in solved['report']['contacts'] if c['socket']=='grip.'+side))[:3,3],axis=1).min())
        for side in ('left','right')}
    require(all(v<=.1 for v in hand_to_grip.values()),'RIG_GRIP_GEOMETRY',
            'Each rendered hand mesh must occupy the actual authored grip neighborhood.',observed=hand_to_grip)
    prop_error=float(np.linalg.norm(evaluated[bar.id]['positions']-bar.positions,axis=1).max())
    require(prop_error<=1e-8,'RIG_GRIP_OBJECT','Rendered static bar geometry must remain at the exact authored object grip frame.',observed=prop_error)
    atomic_json(directory/'grip-recipe.json',recipe)
    artifacts=_export(derived,directory/'export',blender=blender)
    rendered=dict(executed=False,reason='render flag is false; numeric/export proof only')
    if render:
        from rig_render import render_rig_preview
        points=np.concatenate([v['positions'] for v in evaluated.values()]);bounds=[points.min(axis=0),points.max(axis=0)]
        without=deepcopy(derived);without.geometry.meshes=[m for m in without.geometry.meshes if m.id!=bar.id];without.weights.pop(bar.id)
        before=render_rig_preview(without,directory/'without-prop.png',pose=solved['pose'],width=256,height=256,camera='front',bounds=bounds)
        picture=render_rig_preview(derived,directory/'hand-prop-grip.png',pose=solved['pose'],width=256,height=256,camera='front',bounds=bounds)
        changed=int(np.any(np.asarray(before)!=np.asarray(picture),axis=2).sum())
        require(changed>=4,'RIG_GRIP_RENDER','Actual prop triangles must be visible in the same posed hand frame.',observed=changed)
        rendered=dict(executed=True,renderer='native CPU z-buffer over the same skinned triangles',camera='front',
            frame=dict(path=str(directory/'hand-prop-grip.png'),sha256=sha256(directory/'hand-prop-grip.png')),
            negative_control=dict(path=str(directory/'without-prop.png'),sha256=sha256(directory/'without-prop.png')),
            changed_pixels=changed,visual_review='pending')
        if blender:
            from rig_blender import render_blender_preview
            receipt=artifacts['blender']
            rendered['blender']=render_blender_preview(derived,directory/'blender-frames',clip=clip['id'],times=[1.],
                blend_path=receipt['path'],blend_sha256=receipt['sha256'])
    report=dict(scope='cooperative hand contact with an explicitly static unowned synthetic bar; no ownership activation',
        source_rig_sha256=rig_signature(rig.manifest),derived_rig_sha256=rig_signature(derived.manifest),derived_root=str(derived.root),
        grip_authority=_code_reference(grips_api.__file__),recipe_sha256=object_hash(recipe),
        prop_anchor=anchor['id'],contact=solved['report'],hand_vertex_displacement_m=hand_displacement,nearest_hand_vertex_to_grip_m=hand_to_grip,max_static_prop_vertex_error_m=prop_error,
        artifacts=artifacts,render=rendered,visual_review='pending')
    atomic_json(directory/'grip-proof.json',report);return report


def verify_extended_fixtures(root,*,blender=False,render=False,validator=False):
    """Run bounded specialty recipes after the authoritative six-fixture gate."""
    require(all(type(flag) is bool for flag in (blender,render,validator)), 'RIG_FIXTURE_FLAGS','Proof flags must be booleans.')
    root=Path(root).resolve();sources={name:load_rig(root/name) for name in FIXTURES}
    before={name:_inventory(root/name) for name in FIXTURES}
    proof_root=root/'specialization-proofs';proof_root.mkdir(exist_ok=True)
    destination=Path(tempfile.mkdtemp(prefix='attempt-',dir=proof_root))
    reports=dict(garment=garment_proof(sources['humanoid'],destination/'garment',blender=blender),
        tail=tail_proof(sources['quadruped'],destination/'tail',blender=blender),
        grip=grip_render_proof(sources['humanoid'],destination/'grip',blender=blender,render=render))
    if validator:
        from rig_interop import validate_gltf
        for name,entry in reports.items():
            entry['khronos']={}
            for extension in ('glb','gltf'):
                receipt=validate_gltf(entry['artifacts'][extension]['path'])
                require(receipt['ok'],'RIG_FIXTURE_GLTF','Specialty asset failed independent standards validation.',asset=name,observed=receipt['issues'])
                entry['khronos'][extension]=receipt
    after={name:_inventory(root/name) for name in FIXTURES}
    require(before==after,'RIG_FIXTURE_SOURCE_MUTATION','Specialty proofs must preserve every base fixture byte.')
    inventory=dict(before=before,after=after);atomic_json(destination/'source-closure-hashes.json',inventory)
    report=dict(schema_version=1,kind='hatch-rig-specialty-fixture-evidence',code_sha256=code_signature(),code=_code_reference(__file__),
        source_rigs={name:rig_signature(rig.manifest) for name,rig in sources.items()},source_bytes_preserved=True,
        source_inventory=dict(path=str(destination/'source-closure-hashes.json'),sha256=sha256(destination/'source-closure-hashes.json')),
        destination=str(destination),gates=dict(native_numeric=True,native_export_reimport=True,blender=blender,
        rendered_grip=render,khronos=validator),fixtures=reports,production_artwork_approved=False,visual_review='pending')
    atomic_json(destination/'specialty-evidence.json',report);return report
