#!/usr/bin/env python3
"""Execute the real two-scene, exact-library package/restore acceptance workflow.

All generated artwork is a procedural engineering fixture. The script consumes
canonical library, scene, export, package and renderer APIs; it owns no registry.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import shutil


def require(value, message):
    from rig_errors import RigError
    if not value: raise RigError('LIBRARY_DEMO_ACCEPTANCE', message)


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def signatures(library, references, destination):
    """Decode exact stored payloads and retain inspectable semantic signatures."""
    from rig_import import load_character_model
    from rig_errors import object_hash
    destination.mkdir()
    result={}
    for kind, reference in references.items():
        manifest=library.resolve(reference)
        own={f['path']:dict(sha256=f['sha256'],bytes=f['bytes']) for f in manifest['files']}
        entry=dict(reference=reference,files=own,dependencies=manifest['dependencies'])
        for item in manifest['files']:
            payload=library.read_file(reference,item['path'])
            require(digest(payload)==item['sha256'],'Stored payload hash differs from immutable manifest.')
        if kind not in ('camera','light','texture'):
            path=destination/(kind+'.glb');path.write_bytes(library.read_file(reference,'runtime/asset.glb'))
            model=load_character_model(path)
            entry['native']=dict(vertices=model.geometry.vertex_count,triangles=model.geometry.triangle_count,
                meshes=[dict(id=m.id,vertices=len(m.positions),triangles=len(m.triangles),
                    positions=digest(m.positions.tobytes()),topology=digest(m.triangles.tobytes()),material=m.material)
                    for m in model.geometry.meshes],
                skeleton=object_hash(model.skeleton),bones=len(model.skeleton['bones']) if model.skeleton else 0,
                clips={name:object_hash(clip) for name,clip in model.clips.items()},
                materials=model.geometry.materials,textures=[digest(t.data) for t in model.geometry.images])
        result[kind]=entry
    return result


def render_scene(adapter, project, scene_id, destination, executable):
    """Resolve through the library adapter, then use the ordinary Scene3D renderer."""
    from scene3d_contracts import load_project
    from render3d_snapshot import Scene3DAdapter, create_snapshot
    from render3d_blender import BlenderBackend
    from rig_errors import atomic_json
    destination.mkdir(parents=True)
    adapter.load_scene(project,scene_id,destination/'scene')
    bundle=load_project(destination/'scene');doc=bundle['project']
    scene_ref=doc['scenes'][0];shot_ref=doc['shots'][0]
    shot=bundle['documents'][shot_ref['path']]
    create_snapshot(destination/'scene',destination/'snapshot',Scene3DAdapter())
    snapshot=destination/'snapshot/snapshot.json'
    identity=lambda d:dict(id=d['id'],version=d['version'])
    request=dict(schema_version=1,kind='render3d-request',id='library-acceptance',
        project=identity(doc),scene=identity(scene_ref),shot=identity(shot_ref),mode='3d',
        snapshot=dict(path='snapshot.json',sha256=digest(snapshot.read_bytes()),bytes=snapshot.stat().st_size),
        fps=dict(numerator=4,denominator=1),frame_range=dict(start=0,end=1),
        source_start=dict(numerator=0,denominator=1),playback_rate=dict(numerator=1,denominator=1),
        camera=shot['camera'],settings=dict(engine='cycles',device_policy='cpu',device=None,
            allow_cpu_fallback=False,resolution=[96,96],samples=4,seed=17,denoise=False,
            output=dict(format='png',bit_depth=8,passes=['combined'],alpha='opaque',color_space='srgb'),
            exposure=0,view_transform='standard',resources=dict(memory_bytes=2*1024**3,
                storage_bytes=256*1024**2,timeout_seconds=120,workers=1,max_frames=4)),
        priority=0,max_attempts=1,cache_policy='verify-reuse',
        destination=dict(path='render',owner_token='library-acceptance'))
    atomic_json(destination/'request.json',request)
    result=BlenderBackend(executable).render_frames(request,destination/'snapshot',destination/'render')
    require(result['verification']['status']=='passed','Actual renderer verification failed.')
    return dict(path=str(destination/'render'/result['images'][0]['file']['path']),result=result)


def demonstrate(output, executable):
    import numpy as np
    from PIL import Image
    from asset_library import AssetLibrary
    from asset_library_dependencies import DependencyGraph, locator
    from asset_library_demo_fixtures import create_fixtures, register_fixtures
    from asset_library_reuse import SceneLibraryAdapter
    from asset_library_packages import create_package, verify_package, inspect_package, restore_package
    from asset_library_export import ExportPipeline
    from rig_errors import atomic_json, RigError
    from rig_math import matrix_quaternion
    output=Path(output).absolute();output.mkdir(parents=True,exist_ok=False)
    fixture=create_fixtures(output/'source')
    library=AssetLibrary(output/'library',create=True);refs=register_fixtures(library,fixture)
    graph=DependencyGraph({'local':library});adapter=SceneLibraryAdapter(graph)
    project=adapter.new_project('library-acceptance')
    def binding(kind):
        path='native/'+kind+'.json' if kind in ('camera','light') else 'runtime/asset.glb'
        return dict(location=locator('local',refs[kind]),path=path)
    def transform(location):return dict(translation=location,rotation=[0,0,0,1],scale=[1,1,1])
    for scene_id,shift,camera_offset in [('scene-one',-.45,[3,2,4]),('scene-two',.45,[-3,2,4])]:
        project=adapter.add_scene(project,scene_id)
        project=adapter.instantiate(project,scene_id,'robot',bindings={k:binding(k) for k in ('character','rig','animation','material')},
            transform=transform([shift,0,0]),animation_state=dict(clip=fixture['clip'],time='0'))
        for kind,location in [('environment',[0,0,0]),('prop',[-shift,.05,.55]),('light',[1.5,3,2.5])]:
            project=adapter.instantiate(project,scene_id,kind,bindings=dict(asset=binding(kind)),transform=transform(location))
        camera=transform(camera_offset)
        direction=-np.asarray(camera_offset,float)+[0,.6,0]
        direction/=np.linalg.norm(direction)
        right=np.cross(direction,[0,1,0]);right/=np.linalg.norm(right)
        up=np.cross(right,direction)
        camera['rotation']=matrix_quaternion(np.column_stack((right,up,-direction))).tolist()
        project=adapter.instantiate(project,scene_id,'camera',bindings=dict(asset=binding('camera')),transform=camera)
    atomic_json(output/'project.json',project)
    before=signatures(library,refs,output/'decoded-original')
    roots=[locator('local',ref) for ref in refs.values()]
    closure=graph.lock(roots)
    atomic_json(output/'dependency-lock.json',closure)
    exports=output/'exports';exports.mkdir()
    export=ExportPipeline(library,blender=executable).export(refs['character'],exports/'character',format='glb')
    package=create_package(graph,roots,output/'package',package_id='library-two-scene',package_type='project',
        policy=dict(require_sources=True,require_license_evidence=True,intended_use='test'),
        context_files={'project.json':output/'project.json','fixtures.json':output/'source/fixtures.json'})
    verify_package(output/'package')
    restored=restore_package(output/'package',output/'relocated/isolated')
    restored_library=AssetLibrary(restored['library'])
    after=signatures(restored_library,refs,output/'decoded-restored')
    require(before==after,'Asset identity/geometry/rig/clip/material data changed after restoration.')
    restored_project=json.loads((Path(restored['restored'])/'context/project.json').read_text())
    require(restored_project==project,'Restored scene bindings changed.')
    restored_adapter=SceneLibraryAdapter(DependencyGraph({'local':restored_library}))
    renders={};pixel_signatures={}
    for phase,owner,doc in [('original',adapter,project),('restored',restored_adapter,restored_project)]:
        renders[phase]={}
        for scene_id in ('scene-one','scene-two'):
            renders[phase][scene_id]=[]
            for time in ('0','1/2'):
                sampled=deepcopy(doc)
                sampled=owner.set_animation(sampled,scene_id,'robot',dict(clip=fixture['clip'],time=time))
                result=render_scene(owner,sampled,scene_id,output/'renders'/phase/scene_id/('sample-'+time.replace('/','-')),executable)
                with Image.open(result['path']) as image:pixels=np.asarray(image.convert('RGBA'))
                pixel_signatures[(phase,scene_id,time)]=pixels
                result.update(time=time,pixel_sha256=digest(pixels.tobytes()))
                renders[phase][scene_id].append(result)
    metrics={}
    for scene_id in ('scene-one','scene-two'):
        first=pixel_signatures[('original',scene_id,'0')].astype(float)
        later=pixel_signatures[('original',scene_id,'1/2')].astype(float)
        delta=float(np.abs(first-later).mean());require(delta>.01,'Animated frames must visibly differ.')
        metrics[scene_id]=dict(motion_mean_pixel_delta=delta,restored_max_pixel_delta=0)
        for time in ('0','1/2'):
            old=pixel_signatures[('original',scene_id,time)]
            new=pixel_signatures[('restored',scene_id,time)]
            difference=int(np.abs(old.astype(int)-new.astype(int)).max())
            require(difference==0,'Restored render pixels changed on the same pinned CPU backend.')
    require(not np.array_equal(pixel_signatures[('original','scene-one','0')],pixel_signatures[('original','scene-two','0')]),
            'Distinct scene compositions must produce different rendered content.')
    broken=output/'missing-payload-package';shutil.copytree(output/'package',broken)
    missing_sha=fixture['texture_sha256'];missing=broken/'blobs'/missing_sha
    exact=missing.read_bytes();missing.unlink()
    diagnostic=inspect_package(broken)
    require(diagnostic['state']=='incomplete' and not diagnostic['verified'],'Missing required texture must block completeness.')
    blocked=output/'must-not-restore'
    try:restore_package(broken,blocked)
    except (RigError,OSError,ValueError):pass
    else:require(False,'Missing required payload unexpectedly restored.')
    require(not blocked.exists() and not missing.exists(),'Failed restore must not fabricate replacement bytes.')
    missing.write_bytes(b'X'*len(exact))
    require(not inspect_package(broken)['verified'],'Wrong replacement bytes must remain blocked.')
    missing.write_bytes(exact)
    require(verify_package(broken)['verified'],'Exact recovery must restore package verification.')
    recovery=restore_package(broken,output/'repaired-isolated')
    require(AssetLibrary(recovery['library']).resolve(refs['texture'])==library.resolve(refs['texture']),
            'Exact recovered texture identity changed.')
    result=dict(schema_version=1,fixture=True,production_approved=False,passed=True,
        references=refs,source_fixture=fixture,semantic_signatures=before,package=package,restored=restored,
        export=export,renders=renders,metrics=metrics,negative=dict(expected_missing_asset=refs['texture'],
            missing_sha256=missing_sha,diagnostic=diagnostic,wrong_bytes_blocked=True,exact_recovery=True),
        limitations=['Procedural engineering fixtures; no artistic approval.',
            'Exact restored pixel comparison applies to this pinned same-host CPU Blender run.'])
    atomic_json(output/'acceptance.json',result)
    inventory={}
    for path in sorted(output.rglob('*')):
        if path.is_file():
            data=path.read_bytes();inventory[path.relative_to(output).as_posix()]=dict(bytes=len(data),sha256=digest(data))
    atomic_json(output/'manifest.json',dict(schema_version=1,fixture=True,files=inventory))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--blender',required=True)
    args=parser.parse_args();result=demonstrate(args.output,args.blender)
    print(json.dumps(dict(passed=result['passed'],acceptance=str(args.output/'acceptance.json'))))


if __name__=='__main__':main()
