"""Typed curve sampling; skeletal transforms remain owned by rig_animation.

Tangents use value/frame, Bezier handles are relative time/value offsets.
The segment clock is converted to float only after exact interval selection.
"""
import numpy as np
from animation3d_time import frame_position
from animation3d_math import hermite, quaternion_log, quaternion_exp
from rig_math import quaternion, qmul, qinverse, slerp
from rig_animation import evaluate_track


def _slope(keys, positions, index, outgoing):
    key = keys[index]
    before = max(0, index-1); after = min(len(keys)-1, index+1)
    value = np.asarray(key['value'], dtype=float)
    back = ((value-np.asarray(keys[before]['value'])) / float(positions[index]-positions[before])
            if before != index else None)
    forward = ((np.asarray(keys[after]['value'])-value) / float(positions[after]-positions[index])
               if after != index else None)
    back = forward if back is None else back
    forward = back if forward is None else forward
    t, c, b = (key.get(name, 0) for name in ('tension','continuity','bias'))
    if outgoing:
        return (1-t)*((1+c)*(1+b)*back+(1-c)*(1-b)*forward)/2
    return (1-t)*((1-c)*(1+b)*back+(1+c)*(1-b)*forward)/2


def _control(keys, index):
    q = quaternion(keys[index]['value'])
    neighbors = [quaternion(keys[max(0,index-1)]['value']),
                 quaternion(keys[min(len(keys)-1,index+1)]['value'])]
    neighbors = [other if np.dot(q, other)>=0 else -other for other in neighbors]
    tangent = -.25*sum((quaternion_log(qmul(qinverse(q), other)) for other in neighbors), np.zeros(3))
    return quaternion(qmul(q, quaternion_exp(tangent)))


def _bezier(a,b,c,d,u):
    return (1-u)**3*a+3*(1-u)**2*u*b+3*(1-u)*u*u*c+u**3*d


def evaluate_segment(track, index, position):
    """Sample the validated segment starting at index; return JSON values."""
    keys = track['keys']; a,b = keys[index:index+2]
    positions = [frame_position(key['time']) for key in keys]
    duration = float(positions[index+1]-positions[index])
    u = float((position-positions[index])/(positions[index+1]-positions[index]))
    mode = a.get('interpolation', 'step' if track['value_type']=='boolean' else
                 'quaternion-slerp' if track['value_type']=='quaternion' else 'linear')
    av,bv = np.asarray(a['value']),np.asarray(b['value'])
    if mode == 'step': result = av
    elif mode in ('linear','hermite','quaternion-slerp'):
        wrap=lambda value: value if isinstance(value,list) else [value]
        native=dict(times=[0,duration],values=[wrap(a['value']),wrap(b['value'])],
                    path='rotation' if track['value_type']=='quaternion' else 'translation',
                    interpolation='CUBICSPLINE' if mode=='hermite' else 'LINEAR')
        if mode=='hermite':
            native.update(in_tangents=[[0]*len(wrap(a['value'])),wrap(b['in_tangent'])],
                          out_tangents=[wrap(a['out_tangent']),[0]*len(wrap(b['value']))])
        result=evaluate_track(native,u*duration)
    elif mode == 'quaternion-squad':
        result = slerp(slerp(av,bv,u),slerp(_control(keys,index),_control(keys,index+1),u),2*u*(1-u))
    elif mode == 'ease':
        fraction = float(hermite(0,1,1-a.get('ease_out',1),1-b.get('ease_in',1),u))
        result = av*(1-fraction)+bv*fraction
    elif mode in ('catmull-rom','tcb'):
        # Catmull-Rom is the T=C=B=0 specialization.
        use = keys if mode=='tcb' else [{k:v for k,v in key.items() if k not in ('tension','continuity','bias')} for key in keys]
        result = hermite(av,bv,_slope(use,positions,index,True),_slope(use,positions,index+1,False),u,duration)
    elif mode == 'bezier':
        out,inc = a['out_handle'],b['in_handle']
        ow = out.get('weight',1) if out.get('weighted',False) else 1
        iw = inc.get('weight',1) if inc.get('weighted',False) else 1
        x1,x2 = out['time_delta_frames']*ow,duration+inc['time_delta_frames']*iw
        lo,hi = 0.,1.
        for _ in range(60):
            mid = (lo+hi)/2
            if _bezier(0,x1,x2,duration,mid)<u*duration: lo=mid
            else: hi=mid
        v1 = av+np.asarray(out['value_delta'])*ow
        v2 = bv+np.asarray(inc['value_delta'])*iw
        result = _bezier(av,v1,v2,bv,(lo+hi)/2)
    else:
        raise AssertionError('Unvalidated interpolation mode: '+mode)
    return result.tolist() if isinstance(a['value'],list) else bool(np.asarray(result).item()) if track['value_type']=='boolean' else float(np.asarray(result).item())
