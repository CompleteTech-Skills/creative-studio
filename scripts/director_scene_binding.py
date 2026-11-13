"""Bind a Director performance to canonical Scene3D documents and exact files.

This adapter does not evaluate motion or render. Native rig clips and Scene3D
tracks remain their owners' formats. The new directory is published only after
complete Scene3D closure and native geometry/rig validation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import stat
import tempfile

import numpy as np

from asset_library import read_source_bytes
from asset_library_contracts import MAX_FILE_BYTES
from atomic_publish import _rename_no_replace
from rig_errors import decode_json, require, sha256
from rig_import import load_character_model
from rig_math import decompose
from rig_animation import evaluate_animation
from rig_skeleton import evaluate_fk
from scene3d_math import evaluate_transforms
from rig_models import load_rig
from scene3d_contracts import COORDINATES, all_refs, canonical_bytes, load_project, validate_document
from scene3d_example import node

ACTORS = ('aster', 'moss')
SETTINGS = dict(backend='blender-cycles', width=640, height=360, samples=16,
                transparent=False, color_space='srgb', output='png', device='cpu')
PROVENANCE = dict(origin='procedural-fixture', fixture=True, tool='director-scene-binding-v1',
                  inputs=[], attribution='Hatch Pet procedural engineering fixture', license='CC0-1.0')
MAX_BYTES = 256 * 1024**2
MAX_FILES = 1024


def _check(condition, message):
    require(condition, 'DIRECTOR_SCENE_BINDING', message)


def _copy_file(source, target, budget):
    payload = read_source_bytes(source, min(MAX_FILE_BYTES, budget[0]))
    budget[0] -= len(payload)
    budget[1] += 1
    _check(budget[0] >= 0 and budget[1] <= MAX_FILES, 'Source file/byte budget exceeded.')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _file(root, path):
    return dict(path=path, sha256=sha256(root/path))


def _reference(root, path, kind='evidence', identifier=None, version='1.0.0'):
    return dict(id=identifier or 'file-'+hashlib.sha256(path.encode()).hexdigest()[:48],
                kind=kind, version=version, **_file(root, path))


def _write(root, path, value):
    validate_document(value)
    (root/path).write_bytes(canonical_bytes(value))
    return _reference(root, path, value['kind'], value['id'], value['version'])


def _copy_native(source, root, actor, budget):
    original = source/'assets'/('rig-'+actor)
    _check(original.is_dir() and not original.is_symlink(), 'Native rig directory is missing or linked.')
    paths=[]
    for path in sorted(original.rglob('*')):
        _check(not path.is_symlink(), 'Native source contains a symlink.')
        if path.is_dir():continue
        _check(stat.S_ISREG(path.stat().st_mode), 'Native source contains a special file.')
        relative='native/'+actor+'/'+path.relative_to(original).as_posix()
        _copy_file(path,root/relative,budget);paths.append(relative)
    rig=load_rig(root/'native'/actor)
    expected=[actor+'-'+shot for shot in ('investigation','establish','discovery')]
    _check(all(name in rig.clips for name in expected), 'Required native shot clips are absent.')
    clips=[next(c for c in rig.manifest['animations'] if c['id']==name) for name in expected]
    rig_ref=_reference(root,'native/'+actor+'/rig.json','hatch-3d-rig',rig.manifest['id'],rig.manifest['version'])
    clip_refs={clip['id']:_reference(root,'native/'+actor+'/'+clip['path'],'hatch-3d-animation',clip['id'],clip['version']) for clip in clips}
    dependencies=[rig_ref,*clip_refs.values()]
    used={reference['path'] for reference in dependencies}
    dependencies += [_reference(root,path) for path in paths if path not in used]
    return rig,rig_ref,clip_refs,dependencies


def _asset(root, identifier, content_path, source_path, geometry, *, skeleton=None, animations=(), dependencies=()):
    points=np.concatenate([mesh.positions for mesh in geometry.meshes])
    bounds=dict(minimum=points.min(axis=0).tolist(),maximum=points.max(axis=0).tolist())
    value=dict(schema_version=1,kind='scene3d-asset',id=identifier,version='1.0.0',name=identifier,
               asset_type='character' if skeleton else 'prop' if identifier=='access-key' else 'environment',
               content=_file(root,content_path),source=_file(root,source_path),provenance=deepcopy(PROVENANCE),
               coordinate_system=COORDINATES,original_coordinate_system='right-handed Y-up meters',
               pivot=[0,0,0],meshes=[_file(root,content_path)],materials=[],bounds=bounds,collision=None,
               sockets=[],components={},skeleton=skeleton,animations=list(animations),lods=[],
               dependencies=list(dependencies),resources=dict(vertices=geometry.vertex_count,
               triangles=geometry.triangle_count,bytes=(root/content_path).stat().st_size),validation=[],
               review=dict(status='unreviewed',receipts=[]))
    return _write(root,identifier+'-asset.json',value),bounds


def bind_scene_project(performance_root, destination, key_frames):
    """Create one portable immutable Scene3D project from verified native inputs.

    key_frames supplies every canonical boundary 0..120 as a finite 4x4 TRS
    matrix sampled by the caller's authoritative native rig evaluator.
    """
    source=Path(performance_root).absolute();destination=Path(destination).absolute()
    _check(source.is_dir() and not source.is_symlink(), 'Performance source must be a real directory.')
    _check(not destination.exists() and not destination.is_symlink(), 'Use a new binding destination.')
    _check(destination.parent.is_dir() and all(not p.is_symlink() for p in destination.parents), 'Destination parent must be canonical.')
    _check(isinstance(key_frames,list) and len(key_frames)==121 and
           all(isinstance(entry,dict) and set(entry)=={'frame','matrix'} and type(entry['frame']) is int and entry['frame']==i
               for i,entry in enumerate(key_frames)), 'Supply exactly ordered frame boundaries 0..120.')
    poses=[]
    for entry in key_frames:
        matrix=np.asarray(entry['matrix'],dtype=float)
        _check(matrix.shape==(4,4) and np.isfinite(matrix).all(), 'Invalid key transform.')
        poses.append(decompose(matrix))
    stage=Path(tempfile.mkdtemp(prefix='.director-scene-bind-',dir=destination.parent))
    try:
        budget=[MAX_BYTES,0]
        assets=[];native={};bounds={};rigs={}
        _copy_file(source/'scene.json',stage/'source-scene.json',budget)
        scene=decode_json((stage/'source-scene.json').read_bytes())
        validate_document(scene,'scene')
        for actor in ACTORS:
            rig,rig_ref,clip_refs,dependencies=_copy_native(source,stage,actor,budget)
            content=actor+'-animated.glb'
            _copy_file(source/content,stage/content,budget)
            # Independently parse the actual interchange bytes as well as native rig.
            imported=load_character_model(stage/content)
            _check(imported.geometry.vertex_count>0 and imported.geometry.triangle_count>0, 'Empty animated GLB.')
            source_path='native/'+actor+'/'+rig.manifest['source']['path']
            reference,bound=_asset(stage,actor,content,source_path,rig.geometry,skeleton=rig_ref,
                                  animations=list(clip_refs.values()),dependencies=dependencies)
            assets.append(reference);native[actor]=(reference,clip_refs);bounds[actor]=bound;rigs[actor]=rig
        for identifier in ('laboratory','access-key'):
            content=identifier+'.glb';_copy_file(source/'assets'/content,stage/content,budget)
            geometry=load_character_model(stage/content).geometry
            reference,bound=_asset(stage,identifier,content,content,geometry)
            assets.append(reference);bounds[identifier]=bound
        actor_world=np.asarray(evaluate_transforms(scene)['aster']['world_matrix'])
        _check('hand.right' in {bone['id'] for bone in rigs['aster'].skeleton['bones']}, 'Aster lacks native hand.right attachment.')
        for frame, supplied in enumerate(key_frames):
            sampled=evaluate_animation(rigs['aster'],'aster-investigation',max(72,frame)/24,contacts=False,constraints=False)
            expected=actor_world@evaluate_fk(rigs['aster'].skeleton,sampled['pose'])['hand.right']
            _check(np.allclose(expected,np.asarray(supplied['matrix']),atol=1e-7,rtol=0), 'Key motion differs from the native hand.right attachment.')
        key_refs={}
        for shot,start in [('establish',0),('discovery',60)]:
            tracks=[]
            for prop,interpolation in [('translation','linear'),('rotation','slerp'),('scale','linear')]:
                tracks.append(dict(target='access-key',property=prop,interpolation=interpolation,
                    keys=[dict(frame=i,value=pose[prop]) for i,pose in enumerate(poses[start:start+61])]))
            key_animation=dict(schema_version=1,kind='scene3d-animation',id='access-key-'+shot,version='1.0.0',
                duration_frames=60,fps=dict(numerator=24,denominator=1),tracks=tracks,bindings=[],dependencies=[],provenance=dict(deepcopy(PROVENANCE),inputs=[_file(stage,'source-scene.json'),_file(stage,'native/aster/rig.json')]))
            key_refs[shot]=_write(stage,'key-'+shot+'.json',key_animation)
        _check(set(ACTORS) <= {n['id'] for n in scene['nodes']}, 'Scene lacks both persistent actors.')
        for item in scene['nodes']:
            if item['id'] not in native:continue
            actor=item['id'];reference,clips=native[actor]
            item.update(type='skinned_character',asset=reference,bounds=bounds[actor],animation_bindings=[])
        for identifier in ('laboratory','access-key'):
            item=node(identifier,'animated_prop' if identifier=='access-key' else 'mesh_instance','root')
            item.update(asset=next(a for a in assets if a['id']==identifier),bounds=bounds[identifier])
            if identifier=='access-key':
                item['metadata']={'owner_after_frame':72,'owner_id':'aster','ownership_kind':'single-owner-no-handoff','sampled_from':'native-hand.right','pickup_frame':72}
            scene['nodes'].append(item)
        scene['dependencies']=assets+list(key_refs.values());scene['shots']=['establish','discovery']
        scene_ref=_write(stage,'scene.json',scene)
        shots=[]
        for identifier,camera,start,end in [('establish','wide',0,60),('discovery','investigation',60,120)]:
            shot=dict(schema_version=1,kind='scene3d-shot',id=identifier,version='1.0.0',name=identifier,
                scene=scene_ref,duration_frames=60,fps=dict(numerator=24,denominator=1),camera=camera,
                camera_overrides={},tracks=[],bindings=[dict(target=actor,clip=native[actor][1][actor+'-'+identifier],start_frame=0,end_frame=60,speed=1,loop=False) for actor in ACTORS]+[dict(target='access-key',clip=key_refs[identifier],start_frame=0,end_frame=60,speed=1,loop=False)],material_overrides=[],temporal_constraints=[],
                render=deepcopy(SETTINGS),continuity=[],dependencies=[],metadata={'sequence_start':start,'sequence_end':end})
            shots.append(_write(stage,identifier+'-shot.json',shot))
        typed={}
        for path in stage.glob('*.json'):
            document=decode_json(path.read_bytes())
            for reference in all_refs(document):
                if 'kind' in reference:typed[(reference['kind'],reference['id'],reference['version'],reference['path'],reference['sha256'])]=reference
        # Project root references must be included in its exhaustive lock too.
        for reference in assets+shots+[scene_ref,*key_refs.values()]:
            typed[(reference['kind'],reference['id'],reference['version'],reference['path'],reference['sha256'])]=reference
        project=dict(schema_version=1,kind='scene3d-project',id='director-laboratory-project',version='1.0.0',
            name='Director two-robot laboratory',status='fixture',coordinate_system=COORDINATES,asset_catalog=None,
            assets=assets,characters=[a for a in assets if a['id'] in ACTORS],scenes=[scene_ref],shots=shots,
            prefabs=[],animations=list(key_refs.values()),render_defaults=deepcopy(SETTINGS),
            budgets=dict(max_nodes=256,max_files=MAX_FILES,max_bytes=MAX_BYTES,max_frames=120),
            created_at='2026-11-12T00:00:00Z',modified_at='2026-11-12T00:00:00Z',linked_2d_project=None,
            dependency_lock=[typed[key] for key in sorted(typed)],validation=[],provenance=deepcopy(PROVENANCE),
            metadata={'sequence':'establish:0-60,discovery:60-120','artistic_approval':False})
        _write(stage,'project.json',project)
        bundle=load_project(stage)
        report=dict(status=bundle['status'],files=len(bundle['inventory']),bytes=bundle['bytes'],
                    project_sha256=sha256(stage/'project.json'),source_scene_sha256=sha256(source/'scene.json'),
                    production_approved=False)
        _rename_no_replace(stage,destination)
        return report
    finally:
        if stage.exists():shutil.rmtree(stage)
