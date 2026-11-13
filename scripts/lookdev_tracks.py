"""Bounded integer-frame look animation using native rig interpolation semantics.

Values are authored in scene-linear units. Discrete switches are stepped;
quaternion tracks use the established rig slerp/cubic evaluator. Evaluation is
stateless and suitable for random access/resume. It never mutates a base shot.
"""
from copy import deepcopy
import math
from lookdev_contracts import content_hash
from rig_errors import finite_json, require
from rig_animation import evaluate_track as native_track

MAX_FRAME=10_000_000


def validate_track(value):
    require(isinstance(value,dict) and set(value)=={'property','interpolation','keys'},'LOOKDEV_TRACK','Invalid track fields.')
    require(value['property'] in ('scalar','vector','color','quaternion','discrete'),'LOOKDEV_TRACK','Unknown value semantic.')
    require(value['interpolation'] in ('step','linear','smooth'),'LOOKDEV_TRACK','Unsupported interpolation.')
    if value['property']=='discrete':
        require(value['interpolation']=='step','LOOKDEV_TRACK','Discrete switches must be stepped.')
    keys=value['keys']
    require(isinstance(keys,list) and 0<len(keys)<=10000,'LOOKDEV_TRACK','Unbounded key count.')
    previous=-1;dimension=None
    for key in keys:
        require(isinstance(key,dict) and set(key)=={'frame','value'} and type(key['frame']) is int and
                previous<key['frame']<=MAX_FRAME,'LOOKDEV_TRACK','Frames must be increasing bounded integers.')
        previous=key['frame'];sample=key['value'];kind=value['property']
        finite_json(sample)
        if kind=='discrete':
            require(type(sample) in (str,bool) and (not isinstance(sample,str) or len(sample)<=128),
                    'LOOKDEV_TRACK','Invalid discrete value.')
        elif kind=='scalar':
            require(type(sample) in (int,float) and math.isfinite(sample),'LOOKDEV_TRACK','Invalid scalar.')
        else:
            require(isinstance(sample,list) and 2<=len(sample)<=4 and all(type(x) in (int,float) and math.isfinite(x) for x in sample),
                    'LOOKDEV_TRACK','Invalid vector.')
            if dimension is None:dimension=len(sample)
            require(len(sample)==dimension,'LOOKDEV_TRACK','Track dimensions differ.')
            if kind=='color':require(len(sample) in (3,4) and all(x>=0 for x in sample) and (len(sample)==3 or sample[3]<=1),'LOOKDEV_TRACK','Colors must be nonnegative scene-linear RGB with bounded alpha.')
            if kind=='quaternion':
                require(len(sample)==4 and abs(sum(x*x for x in sample)-1)<1e-5,'LOOKDEV_TRACK','Expected normalized XYZW quaternion.')
    return deepcopy(value)


def evaluate_track(track, frame):
    track=validate_track(track)
    require(type(frame) is int and 0<=frame<=MAX_FRAME,'LOOKDEV_TRACK','Expected bounded integer frame.')
    keys=track['keys']
    if frame<=keys[0]['frame']:return deepcopy(keys[0]['value'])
    if frame>=keys[-1]['frame']:return deepcopy(keys[-1]['value'])
    if track['interpolation']=='step':
        return deepcopy(next(key['value'] for key in reversed(keys) if key['frame']<=frame))
    kind=track['property'];times=[k['frame'] for k in keys];values=[k['value'] for k in keys]
    if kind=='scalar':values=[[v] for v in values]
    sample_frame=frame
    if track['interpolation']=='smooth':
        right=next(i for i,t in enumerate(times) if t>frame);left=right-1
        u=(frame-times[left])/(times[right]-times[left]);u=u*u*(3-2*u)
        sample_frame=times[left]+u*(times[right]-times[left])
    result=native_track({'times':times,'values':values,'interpolation':'LINEAR',
                         'path':'rotation' if kind=='quaternion' else 'translation'},sample_frame).tolist()
    return result[0] if kind=='scalar' else result


def evaluate_state(base, tracks, frame, *, locks=()):
    """Tracks address explicit tuples of existing dict keys, never arbitrary code."""
    require(isinstance(base,dict),'LOOKDEV_TRACK','Expected authored state.')
    require(isinstance(tracks,list) and len(tracks)<=1024,'LOOKDEV_TRACK','Unbounded track collection.')
    require(isinstance(locks,(list,tuple)) and all(isinstance(p,(list,tuple)) and all(isinstance(k,str) for k in p) for p in locks),
            'LOOKDEV_TRACK','Expected explicit continuity-lock paths.')
    finite_json(base)
    has_digest='sha256' in base
    if has_digest:
        require(base['sha256']==content_hash({k:v for k,v in base.items() if k!='sha256'}),'LOOKDEV_TRACK','Authored state changed.')
    result=deepcopy(base);occupied=set()
    for entry in tracks:
        require(isinstance(entry,dict) and set(entry)=={'path','track'},'LOOKDEV_TRACK','Invalid binding.')
        path=entry['path']
        require(isinstance(path,list) and 1<=len(path)<=8 and all(isinstance(k,str) and 0<len(k)<=128 for k in path),
                'LOOKDEV_TRACK','Invalid property path.')
        key=tuple(path)
        require(key not in occupied and not any(key[:len(old)]==old or old[:len(key)]==key for old in occupied),
                'LOOKDEV_TRACK','Duplicate or overlapping track binding.')
        occupied.add(key);target=result
        for part in path[:-1]:
            require(isinstance(target,dict) and part in target,'LOOKDEV_TRACK','Unknown authored path.');target=target[part]
        require(isinstance(target,dict) and path[-1] in target,'LOOKDEV_TRACK','Unknown authored property.')
        value=evaluate_track(entry['track'],frame)
        require(not any(tuple(p)==key[:len(p)] for p in locks) or value==target[path[-1]],
                'LOOKDEV_CONTINUITY_LOCK','Animated property violates continuity lock.')
        target[path[-1]]=value
    finite_json(result)
    if has_digest:result['sha256']=content_hash({k:v for k,v in result.items() if k!='sha256'})
    return result
