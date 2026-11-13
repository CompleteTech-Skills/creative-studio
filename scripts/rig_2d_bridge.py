#!/usr/bin/env python3
"""Explicit native-rig frames imported through the unchanged 2D source workflow."""
from __future__ import annotations

import argparse
from copy import deepcopy
from fractions import Fraction
import json
import hashlib
from pathlib import Path
import tempfile
import time as clock

import numpy as np
from PIL import Image

from asset_contracts import MAX_SOURCE_PIXELS, MAX_SOURCE_BYTES, hash_object
from atomic_publish import _rename_no_replace
from profile_spec import load_profile
from rig_errors import MAX_JSON_BYTES, atomic_json, decode_json, open_payload, require, safe_path, sha256
from rig_models import load_rig, rig_signature

MAX_TRIANGLE_SAMPLES=1_000_000
MAX_VERTEX_SAMPLES=2_000_000

RENDERER = {
    'name': 'native-cpu-triangle-z-buffer',
    'materials': 'linear baseColorFactor converted to sRGB; no texture shading',
    'lighting': 'fixed ambient 0.30 plus diffuse 0.70 from normalized [-1,2,3]',
    'transparent': True,
    'review_status': 'pending',
}


def _renderer():
    return dict(RENDERER, implementation_sha256=hash_object({name:sha256(Path(__file__).with_name(name))
        for name in ('rig_render.py','rig_animation.py','rig_skinning.py')}))


def _captured_json(root, name, expected_sha256=None):
    """Authenticate and decode one bounded, safely opened byte snapshot."""
    root=Path(root).resolve()
    with open_payload(root,name) as (stream,info):
        require(info.st_size<=MAX_JSON_BYTES,'RIG_BRIDGE_BUDGET','Bridge JSON exceeds its byte budget.',asset=name)
        payload=stream.read(MAX_JSON_BYTES+1)
        require(len(payload)==info.st_size,'RIG_BRIDGE_HASH','Bridge JSON changed during capture.',asset=name)
    digest=hashlib.sha256(payload).hexdigest()
    require(expected_sha256 is None or digest==expected_sha256,'RIG_BRIDGE_HASH','Bridge JSON changed.',asset=name)
    return decode_json(payload),digest


def _time(value):
    require(isinstance(value, str) and 0 < len(value) <= 32, 'RIG_BRIDGE_TIME',
            'Start time must be a bounded decimal or rational string.')
    try:
        result=Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError('Invalid bridge start time.') from error
    require(0 <= result <= 120 and result.denominator <= 10**9, 'RIG_BRIDGE_TIME',
            'Start time must be nonnegative and within the clip limit.')
    return result


def _timing(profile, job, start):
    if job == 'base':
        return [(start, None)]
    state=next((state for state in profile['states'] if state['id']==job),None)
    require(state is not None, 'RIG_BRIDGE_JOB', 'Profile has no selected state.')
    result=[];time=start
    for duration in state['durations']:
        result.append((time,duration));time+=Fraction(duration,1000)
    return result


def render_bridge(rig_root, destination, *, profile, job, clip, expected_rig_sha256,
                  camera='three-quarter', width=256, height=256, start_time='0'):
    """Render source PNGs only; this never approves or packages 2D artwork."""
    from rig_animation import evaluate_animation
    from rig_render import render_rig_preview
    from rig_skinning import evaluate_deformation

    rig=load_rig(rig_root)
    signature=rig_signature(rig.manifest)
    require(signature == expected_rig_sha256, 'RIG_BRIDGE_STALE', 'Rig revision changed before rendering.')
    require(clip in rig.clips, 'RIG_BRIDGE_CLIP', 'Select an existing native animation clip.')
    profile=Path(profile).absolute();profile_data,profile_hash=_captured_json(profile.parent,profile.name);definition=load_profile(profile_data)
    samples=_timing(definition,job,_time(start_time))
    require(type(width) is int and type(height) is int and 64 <= width <= 2048 and 64 <= height <= 2048,
            'RIG_BRIDGE_SIZE', 'Frame dimensions must be integers from 64 to 2048.')
    require(width*height*len(samples) <= MAX_SOURCE_PIXELS, 'RIG_BRIDGE_BUDGET',
            'Combined ordered-frame source exceeds the existing 2D pixel budget.')
    require(rig.geometry.triangle_count*len(samples)<=MAX_TRIANGLE_SAMPLES and
            rig.geometry.vertex_count*len(samples)<=MAX_VERTEX_SAMPLES,'RIG_BRIDGE_BUDGET',
            'The selected sequence exceeds the bounded geometry evaluation work budget.')
    require(width*height*160 + rig.geometry.vertex_count*3*8*6 <= rig.manifest['limits']['memory_bytes'],
            'RIG_BRIDGE_BUDGET', 'Rendering exceeds the native working-memory budget.')
    end=samples[-1][0]+Fraction(samples[-1][1] or 0,1000)
    require(end <= Fraction(str(rig.clips[clip]['duration'])), 'RIG_BRIDGE_DURATION',
            'Selected profile timing exceeds the clip; do not silently retime it.')
    destination=Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(), 'RIG_BRIDGE_EXISTS',
            'Use a new bridge destination; existing artifacts are preserved.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.rig-2d-',dir=destination.parent) as temporary:
        stage=Path(temporary)/'bridge';stage.mkdir()
        lower=np.full(3,np.inf);upper=np.full(3,-np.inf)
        deadline=clock.monotonic()+rig.manifest['limits']['timeout_seconds']
        for time,_ in samples:
            require(clock.monotonic()<deadline,'RIG_BRIDGE_TIMEOUT','Bridge evaluation exceeded its deadline.')
            pose=evaluate_animation(rig,clip,float(time))['pose']
            for value in evaluate_deformation(rig,pose).values():
                lower=np.minimum(lower,value['positions'].min(axis=0));upper=np.maximum(upper,value['positions'].max(axis=0))
        bounds=[lower.tolist(),upper.tolist()];frames=[]
        for index,(time,duration) in enumerate(samples):
            require(clock.monotonic()<deadline,'RIG_BRIDGE_TIMEOUT','Bridge rendering exceeded its deadline.')
            name=f'frame-{index:04d}.png';path=stage/name
            render_rig_preview(rig,path,clip=clip,time=float(time),width=width,height=height,
                               camera=camera,transparent=True,bounds=bounds)
            require(clock.monotonic()<deadline,'RIG_BRIDGE_TIMEOUT','Bridge rendering exceeded its deadline.')
            frames.append(dict(path=name,sha256=sha256(path),time=[time.numerator,time.denominator],duration_ms=duration))
        atomic_json(stage/'profile.json',definition)
        atomic_json(stage/'rig.json',rig.manifest)
        atomic_json(stage/'clip.json',rig.clips[clip])
        descriptor=dict(schema_version=1,kind='import',layout='image' if job=='base' else 'frames',
                        files=[frame['path'] for frame in frames],fixture=rig.manifest['source']['fixture'],
                        origin='Derived native 3D frames; source rig '+signature+'; visual review required.')
        atomic_json(stage/'source.json',descriptor)
        result=dict(schema_version=1,kind='hatch-rig-2d-bridge',job=job,clip=clip,
                    rig_sha256=signature,source_sha256=rig.manifest['source']['sha256'],
                    clip_sha256=sha256(stage/'clip.json'),rig_file_sha256=sha256(stage/'rig.json'),
                    profile_sha256=profile_hash,profile_file_sha256=sha256(stage/'profile.json'),
                    descriptor_sha256=sha256(stage/'source.json'),fixture=rig.manifest['source']['fixture'],
                    camera=camera,width=width,height=height,bounds=bounds,frames=frames,renderer=_renderer(),
                    coordinate_system=rig.manifest['coordinate_system'],time_unit='second-rational/duration-millisecond',
                    visual_review='pending')
        atomic_json(stage/'bridge.json',result)
        require(rig_signature(load_rig(rig_root).manifest)==signature and sha256(profile)==profile_hash,
                'RIG_BRIDGE_STALE', 'Rig or profile changed while rendering; staged output was discarded.')
        _rename_no_replace(stage,destination)
    return dict(path=str(destination),sha256=sha256(destination/'bridge.json'),manifest=result)


def verify_bridge(rig_root, bridge, *, expected_sha256):
    """Verify exact source/profile/frame bindings; this is not visual approval."""
    bridge=Path(bridge).absolute()
    manifest,_=_captured_json(bridge,'bridge.json',expected_sha256)
    keys={'schema_version','kind','job','clip','rig_sha256','source_sha256','clip_sha256','rig_file_sha256',
          'profile_sha256','profile_file_sha256','descriptor_sha256','fixture','camera','width','height',
          'bounds','frames','renderer','coordinate_system','time_unit','visual_review'}
    require(isinstance(manifest,dict) and set(manifest)==keys and manifest['schema_version']==1 and
            type(manifest['schema_version']) is int and manifest['kind']=='hatch-rig-2d-bridge' and manifest['visual_review']=='pending',
            'RIG_BRIDGE_SCHEMA','Unsupported bridge manifest.')
    require(manifest['camera'] in ('front','side','back','three-quarter'),'RIG_BRIDGE_SCHEMA','Unsupported bridge camera.')
    rig=load_rig(rig_root)
    require(rig_signature(rig.manifest)==manifest['rig_sha256'] and
            rig.manifest['source']['sha256']==manifest['source_sha256'] and
            rig.manifest['source']['fixture'] is manifest['fixture'],
            'RIG_BRIDGE_STALE','Bridge does not describe the current rig source/revision.')
    require(manifest['clip'] in rig.clips,'RIG_BRIDGE_CLIP','Bridge clip no longer exists.')
    copies={}
    for path,key in [('rig.json','rig_file_sha256'),('clip.json','clip_sha256'),('profile.json','profile_file_sha256'),('source.json','descriptor_sha256')]:
        copies[path],_=_captured_json(bridge,path,manifest[key])
    require(copies['rig.json']==rig.manifest and copies['clip.json']==rig.clips[manifest['clip']],
            'RIG_BRIDGE_STALE','Copied rig or clip differs from native source.')
    definition=load_profile(copies['profile.json'])
    require(isinstance(manifest['frames'],list) and 0<len(manifest['frames'])<=64,
            'RIG_BRIDGE_SCHEMA','Bridge needs bounded frames.')
    require(manifest['renderer']==_renderer() and manifest['coordinate_system']==rig.manifest['coordinate_system'] and
            manifest['time_unit']=='second-rational/duration-millisecond',
            'RIG_BRIDGE_STALE','Bridge renderer or coordinate authority differs.')
    require(type(manifest['width']) is int and type(manifest['height']) is int and
            64<=manifest['width']<=2048 and 64<=manifest['height']<=2048 and
            manifest['width']*manifest['height']*len(manifest['frames'])<=MAX_SOURCE_PIXELS,
            'RIG_BRIDGE_BUDGET','Bridge frame dimensions exceed the source budget.')
    require(isinstance(manifest['frames'][0],dict),'RIG_BRIDGE_SCHEMA','Invalid frame record.')
    start=manifest['frames'][0].get('time')
    require(isinstance(start,list) and len(start)==2 and all(type(v)is int for v in start) and 0<start[1]<=10**9,
            'RIG_BRIDGE_SCHEMA','Frame times must be rational seconds.')
    beginning=Fraction(*start)
    require(0<=beginning<=120,'RIG_BRIDGE_TIME','Bridge start time exceeds clip bounds.')
    timing=_timing(definition,manifest['job'],beginning)
    require(timing[-1][0]+Fraction(timing[-1][1] or 0,1000)<=Fraction(str(rig.clips[manifest['clip']]['duration'])),
            'RIG_BRIDGE_DURATION','Bridge timing exceeds native clip.')
    require(len(timing)==len(manifest['frames']),'RIG_BRIDGE_TIME','Frame count differs from profile.')
    paths=[]
    for index,(frame,(time,duration)) in enumerate(zip(manifest['frames'],timing)):
        require(isinstance(frame,dict) and set(frame)=={'path','sha256','time','duration_ms'} and
                frame['path']==f'frame-{index:04d}.png' and frame['time']==[time.numerator,time.denominator] and
                frame['duration_ms']==duration,'RIG_BRIDGE_SCHEMA','Frame identity/timing differs from profile.')
        path=safe_path(bridge,frame['path'],exists=True)
        require(sha256(path)==frame['sha256'],'RIG_BRIDGE_HASH','Rendered frame changed.',asset=frame['path'])
        require(path.stat().st_size<=MAX_SOURCE_BYTES,'RIG_BRIDGE_BUDGET','Frame file exceeds the source byte budget.')
        with Image.open(path) as image:
            require(image.mode=='RGBA' and image.size==(manifest['width'],manifest['height']) and getattr(image,'n_frames',1)==1,
                    'RIG_BRIDGE_IMAGE','Frame pixels differ from their declared RGBA dimensions.')
            image.load()
        paths.append(frame['path'])
    descriptor=copies['source.json']
    require(descriptor==dict(schema_version=1,kind='import',layout='image' if manifest['job']=='base' else 'frames',
        files=paths,fixture=manifest['fixture'],origin='Derived native 3D frames; source rig '+manifest['rig_sha256']+'; visual review required.'),
        'RIG_BRIDGE_SCHEMA','Import descriptor differs from verified frame order.')
    return manifest


def import_bridge(rig_root, bridge, run_root, *, expected_sha256, note):
    """Import through existing ownership/review rules and retain portable lineage."""
    import hatch
    from source_interface import import_source
    manifest=verify_bridge(rig_root,bridge,expected_sha256=expected_sha256)
    run=Path(run_root).resolve();bridge=Path(bridge).resolve()
    require(not bridge.is_relative_to(run),'RIG_BRIDGE_PATH','Bridge originals must be outside the 2D run.')
    with hatch.ownership(run), tempfile.TemporaryDirectory(prefix='.rig-2d-import-',dir=run.parent) as temporary:
        data=hatch.manifest(run)
        require(sha256(run/'profile.json')==manifest['profile_sha256'],'RIG_BRIDGE_PROFILE',
                'The 2D run uses a different exact profile.')
        require(rig_signature(load_rig(rig_root).manifest)==manifest['rig_sha256'],
                'RIG_BRIDGE_STALE','Rig changed before import.')
        stage=Path(temporary)
        # The legacy importer reads images more than once. Give it private
        # exact-byte captures, not concurrently editable external paths.
        records=[(frame['path'],frame['sha256']) for frame in manifest['frames']]
        records.append(('source.json',manifest['descriptor_sha256']))
        for name,digest in records:
            with open_payload(bridge,name) as (stream,info):
                require(info.st_size<=MAX_SOURCE_BYTES,'RIG_BRIDGE_BUDGET','Source file exceeds its byte budget.')
                payload=stream.read(MAX_SOURCE_BYTES+1)
                require(len(payload)==info.st_size,'RIG_BRIDGE_HASH','Bridge source changed during capture.')
            (stage/name).write_bytes(payload)
            require(sha256(stage/name)==digest,'RIG_BRIDGE_HASH','Bridge source changed before import.')
        result=import_source(run,data,manifest['job'],stage/'source.json',note)
        job=hatch.job_for(data,manifest['job'])
        for record,frame in zip(job['import_receipt']['files'],manifest['frames']):
            record['origin_path']=str(bridge/frame['path'])
        job['import_receipt']['rig_2d_bridge']=dict(sha256=expected_sha256,manifest=deepcopy(manifest),
            target_constraints=dict(profile_sha256=sha256(run/'profile.json'),
                                    request_sha256=sha256(run/'pet_request.json'),
                                    style_sha256=data.get('style_sha256')))
        hatch.save(run,data)
    return dict(**result,bridge_sha256=expected_sha256,rig_sha256=manifest['rig_sha256'],review_status='pending')


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    render=sub.add_parser('render');render.add_argument('--rig',required=True);render.add_argument('--destination',required=True)
    render.add_argument('--profile',required=True);render.add_argument('--job',required=True);render.add_argument('--clip',required=True)
    render.add_argument('--expected-rig-sha256',required=True);render.add_argument('--camera',default='three-quarter')
    render.add_argument('--width',type=int,default=256);render.add_argument('--height',type=int,default=256);render.add_argument('--start-time',default='0')
    for command in ('verify','import'):
        p=sub.add_parser(command);p.add_argument('--rig',required=True);p.add_argument('--bridge',required=True);p.add_argument('--expected-sha256',required=True)
        if command=='import':p.add_argument('--run',required=True);p.add_argument('--note',required=True)
    args=parser.parse_args();values=vars(args);command=values.pop('command');rig=values.pop('rig')
    try:
        if command=='render':result=render_bridge(rig,values.pop('destination'),**values)
        elif command=='verify':result=verify_bridge(rig,values.pop('bridge'),**values)
        else:result=import_bridge(rig,values.pop('bridge'),values.pop('run'),**values)
        print(json.dumps(result,indent=2));return 0
    except (ValueError,OSError,KeyError,TypeError) as error:
        print(json.dumps({'error':getattr(error,'report',{'message':str(error)})}));return 1


if __name__=='__main__':raise SystemExit(main())
