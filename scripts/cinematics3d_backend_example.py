"""Real procedural Scene3D / cinematic adapter evidence; no visual approval.

Run only with explicit optional Blender and renderer dependencies. Uses the
canonical renderer and independent pixel decoder; retains every source/output.
"""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from cinematics3d_contracts import Registry, canonical_bytes, default_effect
from cinematics3d_blender import compile_plan
from render3d_blender import BlenderBackend
from render3d_snapshot import create_snapshot, Scene3DAdapter
from render3d_verify import verify_image
from scene3d_example import build_example
from scene3d_contracts import load_project
from rig_errors import atomic_json


def reference(value,path):
    return dict(id=value['id'],version=value['version'],kind=value['kind'],path=path,
                sha256=hashlib.sha256(canonical_bytes(value)).hexdigest())


def configuration(effects,inputs=None):
    stack=dict(schema_version=1,kind='cinematics3d-stack',id='native-stack',version='1.0.0',registry_version='1.0.0',
               metadata=dict(fixture=True,description='Native engineering evidence, unreviewed.'),scope='shot',parent=None,
               effects=[reference(e,e['id']+'.json') for e in effects],bypass=[])
    registry=Registry([(v['id']+'.json',canonical_bytes(v)) for v in [*effects,stack]],external=inputs)
    return registry,registry.reference(stack['id'],stack['version'])


def build_scene(root):
    build_example(root)
    # Face the canonical camera with geometry at two depths. Both are actual
    # triangles in an owner-decoded OBJ; there is no alternate scene evaluator.
    (root/'triangle.obj').write_text('v -1 -1 0\nv 1 -1 0\nv 0 1 0\nf 1 2 3\n')
    docs={p.name:json.loads(p.read_text()) for p in root.glob('*.json')}
    scene=docs['example-scene.json'];scene['nodes'][1]['transform']['translation']=[0,0,0]
    scene['nodes'][2]['transform']['translation']=[0,0,5]
    rear=deepcopy(scene['nodes'][1]);rear['id']='rear';rear['name']='rear';rear['transform']['translation']=[1.5,0,-2]
    scene['nodes'].append(rear)
    asset=docs['triangle.json'];asset['bounds']=dict(minimum=[-1,-1,0],maximum=[1,1,0])
    asset['resources']['bytes']=(root/'triangle.obj').stat().st_size
    done=set()
    def save(name):
        if name in done:return
        def visit(value):
            if isinstance(value,dict):
                if 'path' in value and 'sha256' in value:
                    path=value['path']
                    if path in docs:save(path)
                    value['sha256']=hashlib.sha256((root/path).read_bytes()).hexdigest()
                for child in value.values():visit(child)
            elif isinstance(value,list):
                for child in value:visit(child)
        visit(docs[name]);(root/name).write_bytes(canonical_bytes(docs[name]));done.add(name)
    for name in docs:save(name)
    load_project(root)


def run(destination,executable):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
    build_scene(destination/'scene');create_snapshot(destination/'scene',destination/'snapshot',Scene3DAdapter())
    payload=(destination/'snapshot'/'snapshot.json').read_bytes()
    request=dict(schema_version=1,kind='render3d-request',id='cinematic-fixture',project=dict(id='project',version='1.0.0'),
        scene=dict(id='example-scene',version='1.0.0'),shot=dict(id='example-shot',version='1.0.0'),mode='3d',
        snapshot=dict(path='snapshot.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()),
        fps=dict(numerator=24,denominator=1),frame_range=dict(start=0,end=1),source_start=dict(numerator=0,denominator=1),
        playback_rate=dict(numerator=1,denominator=1),camera='camera',priority=0,max_attempts=1,cache_policy='verify-reuse',
        destination=dict(path='output',owner_token='cinematic-fixture-owner'),settings=dict(engine='cycles',device_policy='cpu',
        device='cpu',allow_cpu_fallback=False,resolution=[96,96],samples=16,seed=4,denoise=False,
        output=dict(format='png',bit_depth=8,passes=['combined'],alpha='opaque',color_space='srgb'),exposure=0,view_transform='standard',
        resources=dict(memory_bytes=2*1024**3,storage_bytes=256*1024**2,timeout_seconds=120,workers=1,max_frames=10)))
    backend=BlenderBackend(executable);probe=backend.cinematics_probe();atomic_json(destination/'probe.json',probe)
    optics=dict(schema_version=1,kind='camera3d-optics',id='optics',version='1.0.0',
        projection=dict(type='perspective',focal_length_mm=40,sensor_width_mm=36,sensor_height_mm=24,sensor_fit='vertical',shift_x=0,shift_y=0,near_clip_m=.1,far_clip_m=100),
        depth_of_field=dict(enabled=True,focus_distance_m=5,f_stop=.5,aperture_blades=7,aperture_rotation_rad=0,anamorphic_ratio=1))
    optics_bytes=canonical_bytes(optics)
    exposure=default_effect('exposure','exposure');exposure['operation']['parameters']['stops']=1
    fog=default_effect('volumetric-fog','fog');fog['operation']['parameters']['density_per_meter']=.08
    dof=default_effect('depth-of-field','dof');dof['inputs']=[reference(optics,'optics.json')]
    variants=[('baseline',[],None),('exposure',[exposure],None),('fog',[fog],None),('dof',[dof],{'optics.json':optics_bytes}),('exr',[exposure],None)]
    outputs={};pixels={}
    for name,effects,inputs in variants:
        current=deepcopy(request);current['id']=name;current['destination']['path']=name
        if name=='exr':current['settings']['output'].update(format='exr',bit_depth=32,color_space='linear')
        registry,stack=configuration(effects,inputs)
        compiled=compile_plan(registry,stack,current,probe,input_bytes=inputs)
        atomic_json(destination/(name+'-request.json'),compiled['request'])
        result=backend.render_frames(compiled['request'],destination/'snapshot',destination/name,cinematics=compiled)
        outputs[name]=dict(result_sha256=hashlib.sha256((destination/name/'result.json').read_bytes()).hexdigest(),
            verification=result['verification'],images=result['images'])
        image=destination/name/result['images'][0]['file']['path']
        decoded=verify_image(image,output=current['settings']['output'],resolution=current['settings']['resolution'],_pixels=True)
        if name!='exr':pixels[name]=decoded['_pixels']
        # Reopen retained original config bytes and require exact reconstruction.
        retained=destination/name/'cinematics'
        restored={p.relative_to(retained).as_posix():p.read_bytes() for p in retained.rglob('*.json') if p.name!='plan.json'}
        from cinematics3d_blender import validate_configuration
        validate_configuration(compiled['plan'],restored)
    import numpy as np
    differences={name:float(np.mean(np.abs(np.stack([pixels[name][c] for c in 'RGB'])-np.stack([pixels['baseline'][c] for c in 'RGB']))))
                 for name in ('exposure','fog','dof')}
    if not all(value>0.0001 for value in differences.values()):raise ValueError('Expected native effect did not measurably change pixels: '+str(differences))
    evidence=dict(schema_version=1,fixture=True,visual_approval=False,executed_at=datetime.now(timezone.utc).isoformat(),
        probe=probe,outputs=outputs,pixel_mean_absolute_difference=differences,configuration_reopened=True,
        limitations=['96px procedural engineering fixture; not final film or artistic approval.',
                      'Motion blur, auxiliary passes and GPU rendering remain explicitly unavailable in this adapter.'])
    atomic_json(destination/'evidence.json',evidence);return evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path);parser.add_argument('--blender',required=True)
    args=parser.parse_args();print(json.dumps(run(args.destination,args.blender),indent=2))
