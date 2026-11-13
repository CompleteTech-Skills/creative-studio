"""Stateless procedural Camera3D movements in canonical meters/XYZW.

Targets are already evaluated world points supplied by the native scene/rig
owner. This module never guesses missing target positions or carries smoothing
state from an earlier frame.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math

from camera3d_paths import _number

MOTIONS = {'static', 'dolly', 'truck', 'pedestal', 'pan', 'tilt', 'orbit',
           'crane', 'follow', 'dolly-zoom', 'shake', 'path'}


def validate_transform(transform):
    from camera3d_contracts import validate
    from camera3d_math import world_transform
    validate({'schema_version':1,'kind':'camera3d-transform','id':'sample-transform',
              'version':'1.0.0','local':transform,'parent':None},'transform')
    world_transform(transform)
    return transform


def _clock(time, duration_frames, fps):
    if type(duration_frames) is not int or not 1 <= duration_frames <= 10000000:
        raise ValueError('camera3d duration must be an integer in 1..10000000')
    if not isinstance(time, dict) or not {'frame'} <= time.keys() or not time.keys() <= {'frame','subframe_ticks'}:
        raise ValueError('camera3d expected exact frame/subframe time')
    frame,ticks=time['frame'],time.get('subframe_ticks',0)
    if type(frame) is not int or type(ticks) is not int or not 0<=ticks<1000000:
        raise ValueError('camera3d invalid integer clock')
    position=Fraction(frame)+Fraction(ticks,1000000)
    if not 0<=position<=duration_frames:
        raise ValueError('camera3d sample is beyond terminal boundary')
    if not isinstance(fps,dict) or set(fps)!={'numerator','denominator'}:
        raise ValueError('camera3d expected rational frame rate')
    n,d=fps['numerator'],fps['denominator']
    if type(n) is not int or type(d) is not int or not 1<=n<=1000000 or not 1<=d<=1000000 or math.gcd(n,d)!=1:
        raise ValueError('camera3d frame rate must be a bounded reduced rational')
    rate=Fraction(n,d)
    if not Fraction(1,1000)<=rate<=1000:
        raise ValueError('camera3d frame rate outside 0.001..1000')
    return float(position/duration_frames), float(position/rate), position==duration_frames


def ease(progress, mode='linear'):
    t=_number(progress,'curve progress')
    if mode=='linear': return t
    if mode=='step': return float(t==1)
    if mode=='ease-in': return t*t
    if mode=='ease-out': return t*(2-t)
    if mode=='ease-in-out': return t*t*(3-2*t)
    raise ValueError('camera3d unsupported easing')


def sample_motion(base_transform, optics, time, duration_frames, motion, parameters=None,
                  *, target_position=None, path=None, parent_world=None,
                  fps=None):
    """Return a detached sample including a dependency hash of every input.

    Only explicit supported parameters are admitted. Distances and angles are
    signed; orbit radii are positive. Follow adds a world-space offset to the
    current target. Rotation/aim constraints belong to the subsequent constraint
    pass. Dolly zoom synchronizes focal length with optical-axis target depth.
    """
    import numpy as np
    from camera3d_contracts import validate, canonical_bytes
    from camera3d_math import world_transform, quaternion_multiply
    from rig_math import axis_angle, vector, qmatrix
    validate(optics,'optics')
    validate_transform(base_transform)
    if motion not in MOTIONS:
        raise ValueError('camera3d unsupported procedural motion')
    allowed={'static':set(),'dolly':{'distance_m','easing'},'truck':{'distance_m','easing'},
             'pedestal':{'distance_m','easing'},'pan':{'angle_rad','easing'},'tilt':{'angle_rad','easing'},
             'orbit':{'angle_rad','radius_x_m','radius_z_m','height_m','easing'},
             'crane':{'delta_m','arc_height_m','easing'},'follow':{'offset_m'},
             'dolly-zoom':{'distance_m','easing'},
             'shake':{'amplitude_m','angle_rad','frequency_hz','seed','envelope'},
             'path':{'easing','reverse','offset_m'}}[motion]
    params=deepcopy({} if parameters is None else parameters)
    if not isinstance(params,dict) or not set(params)<=allowed:
        raise ValueError('camera3d unknown motion parameter')
    progress,seconds,terminal=_clock(time,duration_frames,{'numerator':24,'denominator':1} if fps is None else fps)
    t=ease(progress,params.get('easing','linear'))
    transform,result_optics=deepcopy(base_transform),deepcopy(optics)
    start=vector(base_transform['translation']); position=start.copy()
    rotation=qmatrix(base_transform['rotation'])
    target=None if target_position is None else vector(target_position)
    distance=_number(params.get('distance_m',0),'motion distance',-1e6,1e6)
    angle=_number(params.get('angle_rad',0),'motion angle',-1e6,1e6)
    if motion in ('dolly','dolly-zoom','truck','pedestal'):
        direction={'dolly':-rotation[:,2],'dolly-zoom':-rotation[:,2],
                   'truck':rotation[:,0],'pedestal':np.array([0.,1.,0.])}[motion]
        position=start+direction*distance*t
    elif motion in ('pan','tilt'):
        axis=[0,1,0] if motion=='pan' else rotation[:,0]
        transform['rotation']=quaternion_multiply(axis_angle(axis,angle*t),base_transform['rotation']).tolist()
    elif motion=='crane':
        delta=vector(params.get('delta_m',[0,0,0]))
        arc=_number(params.get('arc_height_m',0),'crane height',-1e6,1e6)
        position=start+delta*t+np.array([0,arc*math.sin(math.pi*t),0])
    elif motion=='follow':
        if target is None: raise ValueError('camera3d follow requires evaluated target')
        position=target+vector(params.get('offset_m',[0,0,0]))
    elif motion=='orbit':
        if target is None: raise ValueError('camera3d orbit requires evaluated target')
        delta=start-target
        radius=float(np.linalg.norm(delta[[0,2]]))
        rx=_number(params.get('radius_x_m',radius),'orbit X radius',1e-9,1e6)
        rz=_number(params.get('radius_z_m',radius),'orbit Z radius',1e-9,1e6)
        azimuth=math.atan2(delta[0],delta[2])+angle*t
        height=_number(params.get('height_m',float(delta[1])),'orbit height',-1e6,1e6)
        position=target+np.array([rx*math.sin(azimuth),height,rz*math.cos(azimuth)])
    elif motion=='shake':
        amplitude=_number(params.get('amplitude_m',0),'shake amplitude',0,1000)
        frequency=_number(params.get('frequency_hz',1),'shake frequency',0,1000)
        seed=params.get('seed',0)
        if type(seed) is not int or not 0<=seed<2**32: raise ValueError('camera3d invalid shake seed')
        envelope=params.get('envelope',True)
        if type(envelope) is not bool: raise ValueError('camera3d envelope must be boolean')
        scale=math.sin(math.pi*progress)**2 if envelope else 1.
        phases=[int.from_bytes(hashlib.sha256(f'{seed}:{i}'.encode()).digest()[:8],'big')/2**64*math.tau for i in range(3)]
        signal=np.array([math.sin(math.tau*frequency*seconds*(1+i*.17)+phases[i]) for i in range(3)])
        position=start+signal*amplitude*scale
        if angle:
            from camera3d_math import euler_xyz
            transform['rotation']=quaternion_multiply(euler_xyz(signal*angle*scale),base_transform['rotation']).tolist()
    elif motion=='path':
        if path is None: raise ValueError('camera3d path movement requires a compiled CameraPath')
        if (path.document['duration_frames']!=duration_frames or
                path.document['fps']!=({'numerator':24,'denominator':1} if fps is None else fps)):
            raise ValueError('camera3d path timing differs; explicit authored retiming is required')
        value=path.sample(t,reverse=params.get('reverse',False),offset_m=params.get('offset_m',[0,0,0]),parent_world=parent_world)
        position=np.asarray(value['position_m'])
        if path.document['orientation']=='tangent':
            if np.linalg.norm(value['tangent'])<1e-9: raise ValueError('camera3d path tangent is undefined')
            from camera3d_math import look_at
            transform['rotation']=look_at(position,position+value['tangent']).tolist()
        elif path.document['orientation']=='target':
            if target is None: raise ValueError('camera3d target-oriented path requires evaluated target')
            from camera3d_math import look_at
            transform['rotation']=look_at(position,target).tolist()
    if motion=='dolly-zoom':
        p=result_optics['projection']
        if target is None or p['type']!='perspective': raise ValueError('camera3d dolly zoom needs a perspective camera and target')
        before=float(np.dot(target-start,-rotation[:,2]));after=float(np.dot(target-position,-rotation[:,2]))
        if before<=0 or after<=0: raise ValueError('camera3d dolly zoom target crosses or is behind camera')
        p['focal_length_mm']*=after/before
    transform['translation']=position.tolist()
    validate_transform(transform);validate(result_optics,'optics')
    dependency={'base_transform':base_transform,'optics':optics,'time':time,'duration_frames':duration_frames,
                'motion':motion,'parameters':params,'target_position':None if target is None else target.tolist(),
                'fps':{'numerator':24,'denominator':1} if fps is None else fps,
                'path':None if path is None else {'document':path.document,'table_samples':path.table_samples},
                'parent_world':None if parent_world is None else np.asarray(parent_world).tolist(),
                'algorithm':'camera3d-motion-v1'}
    return {'transform':transform,'optics':result_optics,'terminal':terminal,
            'dependency_sha256':hashlib.sha256(canonical_bytes(dependency)).hexdigest()}
