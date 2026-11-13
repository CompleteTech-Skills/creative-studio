"""Camera adapters over the shared Animation3D track and clock authorities.

No camera-specific keyframe format is introduced. Callers supply a real native
target registry; its source/target checks run before every sampled track.
"""
from copy import deepcopy
import math



def sample_camera_tracks(camera_id, base_transform, optics, tracks, time, registry,
                         *, duration_frames, horizontal_gate_mm=None):
    from animation3d_registry import sample_track
    from animation3d_time import frame_position
    from animation3d_contracts import KINDS, validate_contract, validate_track
    from camera3d_contracts import validate
    from camera3d_motion import validate_transform
    if type(duration_frames) is not int or not 1<=duration_frames<=10000000:
        raise ValueError('camera3d invalid animation duration')
    position=frame_position(time)
    if not 0<=position<=duration_frames:
        raise ValueError('camera3d time outside animation interval')
    if not isinstance(tracks,list) or len(tracks)>4096:
        raise ValueError('camera3d animation track limit exceeded')
    validate(optics,'optics');validate_transform(base_transform)
    transform,out=deepcopy(base_transform),deepcopy(optics)
    channels=set();controls={}
    # Validate the whole input before resolving/sampling any native source.
    for track in tracks:
        validate_contract({'schema_version':1,'kind':KINDS['track'],'value':track})
        validate_track(track,duration_frames)
        target=track['target']
        if target['kind']!='camera' or target['camera_id']!=camera_id:
            raise ValueError('camera3d track targets another owner/camera')
        prop=target['property']
        if prop in channels:raise ValueError('camera3d duplicate animated camera property')
        channels.add(prop)
    if {'focal_length','field_of_view'}<=channels:
        raise ValueError('camera3d focal length and field of view cannot compete')
    for track in tracks:
        prop=track['target']['property'];value=sample_track(track,time,registry)
        if prop in ('translation','rotation'):
            transform[prop]=value
        elif prop in ('focal_length','field_of_view'):
            if out['projection']['type']!='perspective':
                raise ValueError('camera3d perspective track targets orthographic optics')
            if prop=='field_of_view':
                if horizontal_gate_mm is None or isinstance(horizontal_gate_mm,bool) or not isinstance(horizontal_gate_mm,(int,float)) or not 0<horizontal_gate_mm<=1e9 or not math.isfinite(horizontal_gate_mm):
                    raise ValueError('camera3d horizontal FOV needs explicit effective gate width')
                if not 0<value<math.pi:raise ValueError('camera3d horizontal FOV must be in (0,pi) radians')
                value=horizontal_gate_mm/(2*math.tan(value/2))
            out['projection']['focal_length_mm']=value
        elif prop=='orthographic_size':
            if out['projection']['type']!='orthographic':
                raise ValueError('camera3d orthographic track targets perspective optics')
            out['projection']['orthographic_height_m']=value
        elif prop in ('focus_distance','aperture'):
            out['depth_of_field'][{'focus_distance':'focus_distance_m','aperture':'f_stop'}[prop]]=value
        elif prop=='exposure':controls['exposure_stops']=value
        else:raise ValueError('camera3d native camera property has no evaluated adapter')
    validate_transform(transform);validate(out,'optics')
    return {'transform':transform,'optics':out,'controls':controls,'terminal':position==duration_frames}


def smooth_samples(samples, *, radius=2):
    """Bake a bounded centered triangular filter with truncated edge windows.

    Samples are complete evaluated camera states. All output is generated at
    once; subsequent seeking is list indexing and has no filter state. Position,
    orientation and focus are filtered independently. Other optical properties
    are retained at their authored frame. Rotation uses deterministic ordered
    shortest-arc weighted SLERP; it is a filter, not a physical damping solver.
    """
    import numpy as np
    from camera3d_contracts import validate
    from camera3d_contracts import canonical_bytes
    import hashlib
    from camera3d_math import quaternion_slerp
    from camera3d_motion import validate_transform
    if type(radius) is not int or not 0<=radius<=32:
        raise ValueError('camera3d smoothing radius must be in 0..32')
    if not isinstance(samples,(list,tuple)) or not 1<=len(samples)<=100000:
        raise ValueError('camera3d smoothing requires 1..100000 samples')
    for state in samples:
        validate_transform(state['transform']);validate(state['optics'],'optics')
    dependency=hashlib.sha256(canonical_bytes({'samples':samples,'radius':radius,
        'algorithm':'centered-triangular-slerp-v1'})).hexdigest()
    output=[]
    for index,state in enumerate(samples):
        result=deepcopy(state);position=np.zeros(3);focus=0.;rotation=None;total=0
        for source in range(max(0,index-radius),min(len(samples),index+radius+1)):
            weight=radius+1-abs(index-source);sample=samples[source]
            position+=np.asarray(sample['transform']['translation'])*weight
            focus+=sample['optics']['depth_of_field']['focus_distance_m']*weight
            rotation=(np.asarray(sample['transform']['rotation']) if rotation is None else
                      quaternion_slerp(rotation,sample['transform']['rotation'],weight/(total+weight)))
            total+=weight
        result['transform']['translation']=(position/total).tolist()
        result['transform']['rotation']=rotation.tolist()
        result['optics']['depth_of_field']['focus_distance_m']=focus/total
        result['smoothing']={'algorithm':'centered-triangular-slerp-v1','radius':radius,
                             'boundary':'truncate-and-renormalize','source_frame':index}
        result['dependency_sha256']=dependency
        validate_transform(result['transform']);validate(result['optics'],'optics')
        output.append(result)
    return output


def limit_motion(samples, fps, *, max_speed_m_per_s, max_acceleration_m_per_s2,
                 max_jerk_m_per_s3, damping=0.0):
    """Bake finite speed/acceleration/jerk limits from rest, without hidden state.

    Authored positions are goals, not a promise to reach an infeasible endpoint.
    The result reports per-frame goal error. If the speed boundary conflicts
    with the acceleration/jerk bounds, admission fails instead of reporting an
    unchecked solution. Rotation and optical tracks are retained unchanged.
    """
    import hashlib
    import numpy as np
    from animation3d_time import rate
    from camera3d_contracts import canonical_bytes, validate
    from camera3d_motion import validate_transform
    from camera3d_paths import _number
    speed=_number(max_speed_m_per_s,'maximum speed',1e-9,1e9)
    acceleration_limit=_number(max_acceleration_m_per_s2,'maximum acceleration',1e-9,1e9)
    jerk=_number(max_jerk_m_per_s3,'maximum jerk',1e-9,1e9)
    damping=_number(damping,'velocity damping')
    dt=float(1/rate(fps))
    if not isinstance(samples,(list,tuple)) or not 1<=len(samples)<=100000:
        raise ValueError('camera3d motion limit bake requires 1..100000 samples')
    for sample in samples:
        validate_transform(sample['transform']);validate(sample['optics'],'optics')
    identity=hashlib.sha256(canonical_bytes({'samples':samples,'fps':fps,
        'max_speed_m_per_s':speed,'max_acceleration_m_per_s2':acceleration_limit,
        'max_jerk_m_per_s3':jerk,'damping':damping,'algorithm':'bounded-camera-motion-v1'})).hexdigest()
    def clamp(vector,limit):
        length=float(np.linalg.norm(vector))
        return vector if length<=limit else vector*(limit/length)
    position=np.asarray(samples[0]['transform']['translation'],dtype=float)
    velocity=np.zeros(3);acceleration=np.zeros(3);output=[]
    for index,sample in enumerate(samples):
        goal=np.asarray(sample['transform']['translation'],dtype=float)
        measured_jerk=0.
        if index:
            desired_velocity=(goal-position)/dt
            desired_acceleration=(desired_velocity-velocity)/dt-velocity*damping/dt
            next_acceleration=acceleration+clamp(desired_acceleration-acceleration,jerk*dt)
            next_acceleration=clamp(next_acceleration,acceleration_limit)
            next_velocity=clamp(velocity+next_acceleration*dt,speed)
            actual_acceleration=(next_velocity-velocity)/dt
            measured_jerk=float(np.linalg.norm(actual_acceleration-acceleration)/dt)
            if measured_jerk>jerk*(1+1e-9) or np.linalg.norm(actual_acceleration)>acceleration_limit*(1+1e-9):
                raise ValueError('camera3d speed/acceleration/jerk bounds have no admitted step')
            position=position+next_velocity*dt
            velocity,acceleration=next_velocity,actual_acceleration
        result=deepcopy(sample);result['transform']['translation']=position.tolist()
        result['dependency_sha256']=identity
        result['motion_limits']={'algorithm':'bounded-camera-motion-v1','source_frame':index,
            'speed_m_per_s':float(np.linalg.norm(velocity)),
            'acceleration_m_per_s2':float(np.linalg.norm(acceleration)),
            'jerk_m_per_s3':measured_jerk,'goal_error_m':float(np.linalg.norm(goal-position)),
            'initial_velocity_m_per_s':[0,0,0],'initial_acceleration_m_per_s2':[0,0,0]}
        validate_transform(result['transform']);output.append(result)
    return output
