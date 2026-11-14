"""Detached validated clip edits, exact source clocks and immutable motion reuse.

These document operations do not run a player, state machine or renderer.
"""
from copy import deepcopy
from fractions import Fraction

from animation3d_contracts import KINDS, validate_contract
from animation3d_time import frame_position, rate, TICKS_PER_FRAME
from rig_errors import require, object_hash, atomic_json, open_payload, decode_json


def _fraction(value,label):
    require(isinstance(value,(int,float,Fraction)) and not isinstance(value,bool),
            'ANIMATION_TIME',label+' must be a finite numeric clock value.')
    try:
        result=Fraction(str(value)) if isinstance(value,float) else Fraction(value)
    except (ValueError,OverflowError,TypeError,ZeroDivisionError):
        require(False,'ANIMATION_TIME',label+' must be a finite numeric clock value.')
    require(abs(result)<=10**9,'ANIMATION_LIMIT',label+' exceeds the numeric edit budget.')
    return result


def stamp(position):
    position=_fraction(position,'Position'); ticks=position*TICKS_PER_FRAME
    require(ticks.denominator==1,'ANIMATION_TIME','Edited time exceeds the exact tick precision.')
    frame,subframe=divmod(ticks.numerator,TICKS_PER_FRAME)
    result={'frame':frame,'subframe_ticks':subframe}; frame_position(result)
    return result


REGION_DISCRIMINATOR='animation3d_region_kind'
REGION_VERSION='boundary-v1'


def validate_named_regions(clip):
    """Pure semantic hook: validate reserved paired marker payloads, no I/O.

    The canonical clip schema validates the event fields first. This hook also
    guards its own minimal inputs so direct callers receive typed diagnostics.
    """
    require(isinstance(clip,dict) and isinstance(clip.get('events'),list),
            'ANIMATION_REGION','Named regions require a clip event array.')
    duration=clip.get('duration_frames')
    require(type(duration) in (int,float) and 0<duration<=10000000 and int(duration)==duration and len(clip['events'])<=4096,
            'ANIMATION_REGION','Region clip clock/event budget is invalid.')
    groups={}
    for event in clip['events']:
        require(isinstance(event,dict),'ANIMATION_REGION','A region boundary must be an event object.')
        payload=event.get('payload',{})
        require(isinstance(payload,dict),'ANIMATION_REGION','Event payload must be an object.')
        if REGION_DISCRIMINATOR not in payload:continue
        require(payload[REGION_DISCRIMINATOR]==REGION_VERSION,'ANIMATION_REGION','Unsupported region boundary version.')
        names=('animation3d_region_id','animation3d_region_name','animation3d_region_boundary','animation3d_region_peer')
        require(all(isinstance(payload.get(k),str) and payload[k] for k in names),
                'ANIMATION_REGION','Region boundary fields must be explicit text values.')
        identifier,name,boundary,peer=(payload[k] for k in names)
        require(len(identifier)<=122 and len(name)<=4090 and len(peer)<=128,'ANIMATION_REGION','Region boundary text exceeds its native marker budget.')
        require(boundary in ('start','end') and event.get('type')=='marker' and event.get('scope','clip')=='clip',
                'ANIMATION_REGION','A named region needs clip-scoped start/end markers.')
        other='end' if boundary=='start' else 'start'
        require(event.get('id')==identifier+':'+boundary and peer==identifier+':'+other and event.get('name')==name+':'+boundary,
                'ANIMATION_REGION','Region marker identity/name/peer differs from its pair.')
        frame=event.get('frame')
        require(type(frame) in (int,float) and 0<=frame<=clip.get('duration_frames',-1) and int(frame)==frame,
                'ANIMATION_REGION','Region frame must lie on an integer clip boundary.')
        group=groups.setdefault(identifier,{})
        require(boundary not in group,'ANIMATION_REGION','Region boundary is repeated.')
        group[boundary]=(int(frame),name)
    result=[]
    for identifier,group in sorted(groups.items()):
        require(set(group)=={'start','end'},'ANIMATION_REGION','Region pair is incomplete.')
        start,name=group['start'];end,end_name=group['end']
        require(name==end_name and start<end,'ANIMATION_REGION','Region name or half-open interval is inconsistent.')
        result.append(dict(id=identifier,name=name,start_frame=start,end_frame=end))
    return result


def _checked(value):
    validate_contract(value);validate_named_regions(value);return value


def _validate_clip(value):
    validate_contract(value)
    require(value['kind']==KINDS['clip'],'ANIMATION_KIND','Expected a named animation clip.')
    validate_named_regions(value)
    return value


def _clip(value):
    return deepcopy(_validate_clip(value))


def create_clip(identifier,name,fps,duration_frames,provenance,*,version='1.0.0',tracks=(),rig_binding=None):
    """Construct a validated editable timeline; no source/visual approval claim."""
    require(isinstance(tracks,(list,tuple)) and len(tracks)<=4096,'ANIMATION_LIMIT','Initial tracks must be a bounded array.')
    value=dict(schema_version=1,kind=KINDS['clip'],id=identifier,version=version,name=name,
               fps=deepcopy(fps),duration_frames=duration_frames,tags=[],categories=[],loop_mode='once',
               completion_mode='hold',default_speed=1,tracks=deepcopy(list(tracks)),events=[],contacts=[],
               root_motion=dict(enabled=False,translation_axes=[True,True,True],extract_rotation=False,mode='in_place'),
               interruption_policy='any_frame',provenance=deepcopy(provenance),dependencies=[])
    if rig_binding is not None:value['rig_binding']=deepcopy(rig_binding)
    return _clip(value)


def set_region(clip,identifier,name,start_frame,end_frame):
    """Atomically add/update one half-open range through existing marker events."""
    result=_clip(clip)
    require(isinstance(identifier,str) and isinstance(name,str),'ANIMATION_REGION','Region identifier and name must be text.')
    require(type(start_frame) is int and type(end_frame) is int and 0<=start_frame<end_frame<=result['duration_frames'],
            'ANIMATION_REGION','Region must be a nonempty integer half-open range.')
    existing={e['id']:e for e in result['events']};renamed={}
    for boundary,frame in (('start',start_frame),('end',end_frame)):
        event_id=identifier+':'+boundary;prior=existing.get(event_id)
        require(prior is None or prior.get('payload',{}).get(REGION_DISCRIMINATOR)==REGION_VERSION,
                'ANIMATION_DUPLICATE','Region event identifier belongs to another event.')
        marker=deepcopy(prior) if prior else {}
        if prior:renamed[prior['name']]=name+':'+boundary
        payload=marker.setdefault('payload',{})
        payload.update({REGION_DISCRIMINATOR:REGION_VERSION,'animation3d_region_id':identifier,
                        'animation3d_region_name':name,'animation3d_region_boundary':boundary,
                        'animation3d_region_peer':identifier+(':'+'end' if boundary=='start' else ':'+'start')})
        marker.update(id=event_id,name=name+':'+boundary,frame=frame,type='marker',scope='clip')
        existing[event_id]=marker
    result['events']=sorted(existing.values(),key=lambda e:(e['frame'],e['id']))
    if 'interruption_markers' in result:
        remaining_names={e['name'] for e in result['events'] if e['type']=='marker'}
        names=[]
        for prior_name in result['interruption_markers']:
            if prior_name in remaining_names:names.append(prior_name)
            if prior_name in renamed:names.append(renamed[prior_name])
        result['interruption_markers']=list(dict.fromkeys(names))
    return _checked(result)


def remove_region(clip,identifier):
    result=_clip(clip);regions=validate_named_regions(result)
    require(any(r['id']==identifier for r in regions),'ANIMATION_REFERENCE','Named region does not exist.')
    result['events']=[e for e in result['events'] if e.get('payload',{}).get('animation3d_region_id')!=identifier or REGION_DISCRIMINATOR not in e.get('payload',{})]
    if 'interruption_markers' in result:
        names={e['name'] for e in result['events'] if e['type']=='marker'}
        result['interruption_markers']=[name for name in result['interruption_markers'] if name in names]
    return _checked(result)


def region_range(clip,identifier):
    value=_clip(clip);region=next((r for r in validate_named_regions(value) if r['id']==identifier),None)
    require(region is not None,'ANIMATION_REFERENCE','Named region does not exist.')
    return dict(source_in=region['start_frame'],source_out=region['end_frame'])


def edit_key(clip, track_id, *, key=None, remove=None):
    """Insert a unique key or remove the key at an exact timestamp atomically."""
    result=_clip(clip)
    require((key is None)!=(remove is None),'ANIMATION_EDIT','Choose insertion or removal.')
    track=next((t for t in result['tracks'] if t['id']==track_id),None)
    require(track is not None,'ANIMATION_REFERENCE','Track does not exist.')
    require(key is None or isinstance(key,dict) and 'time' in key,'ANIMATION_EDIT','A key must contain its exact timestamp.')
    position=frame_position(key['time'] if key is not None else remove)
    found=[i for i,k in enumerate(track['keys']) if frame_position(k['time'])==position]
    if key is not None:
        require(not found,'ANIMATION_DUPLICATE','A key already occupies this time.')
        track['keys'].append(deepcopy(key)); track['keys'].sort(key=lambda k:frame_position(k['time']))
    else:
        require(bool(found),'ANIMATION_REFERENCE','No key occupies the requested time.')
        del track['keys'][found[0]]
    return _checked(result)


def edit_clip(clip, *, name=None, duration=None, fps=None, add_track=None, remove_track=None,
              identifier=None,version=None,tags=None,categories=None,loop_mode=None,
              completion_mode=None,default_speed=None,add_event=None,remove_event=None):
    """Rename/extend/trim/configure a clip and add/remove actual typed tracks.

    Trimming never silently drops events or keys: use explicit edits first.
    """
    result=_clip(clip)
    for key,value in [('name',name),('duration_frames',duration),('fps',fps),('id',identifier),('version',version),
                      ('tags',tags),('categories',categories),('loop_mode',loop_mode),
                      ('completion_mode',completion_mode),('default_speed',default_speed)]:
        if value is not None: result[key]=deepcopy(value)
    if add_track is not None: result['tracks'].append(deepcopy(add_track))
    if remove_track is not None:
        require(any(t['id']==remove_track for t in result['tracks']),'ANIMATION_REFERENCE','Track does not exist.')
        result['tracks']=[t for t in result['tracks'] if t['id']!=remove_track]
    if add_event is not None:result['events'].append(deepcopy(add_event))
    if remove_event is not None:
        require(any(e['id']==remove_event for e in result['events']),'ANIMATION_REFERENCE','Event does not exist.')
        result['events']=[e for e in result['events'] if e['id']!=remove_event]
    return _checked(result)


def copy_keys(clip, track_id, start, end):
    value=_clip(clip); low,high=frame_position(start),frame_position(end)
    require(low<=high,'ANIMATION_INTERVAL','Copy range is reversed.')
    track=next((t for t in value['tracks'] if t['id']==track_id),None)
    require(track is not None,'ANIMATION_REFERENCE','Track does not exist.')
    return [deepcopy(k) for k in track['keys'] if low<=frame_position(k['time'])<=high]


def paste_keys(clip, track_id, keys, offset=0):
    """Paste detached keys with one exact offset; reject collisions atomically."""
    result=_clip(clip); track=next((t for t in result['tracks'] if t['id']==track_id),None)
    require(track is not None,'ANIMATION_REFERENCE','Track does not exist.')
    require(isinstance(keys,(list,tuple)) and len(keys)<=250000,'ANIMATION_LIMIT','Pasted keys must be a bounded key array.')
    added=deepcopy(keys); offset=_fraction(offset,'Paste offset')
    for key in added:
        require(isinstance(key,dict) and 'time' in key,'ANIMATION_EDIT','A pasted key must contain its exact timestamp.')
        key['time']=stamp(frame_position(key['time'])+offset)
    track['keys'].extend(added); track['keys'].sort(key=lambda k:frame_position(k['time']))
    return _checked(result)


def save_clip(path, clip):
    """Persist a validated document with the shared crash-safe writer."""
    atomic_json(path,_clip(clip))


def load_clip(path,*,max_bytes=16*1024**2,expected_sha256=None):
    from pathlib import Path
    require(type(max_bytes) is int and 1<=max_bytes<=16*1024**2,'ANIMATION_LIMIT','Clip read limit must be 1 byte..16 MiB.')
    path=Path(path)
    with open_payload(path.parent.resolve(strict=True),path.name) as (stream,metadata):
        require(metadata.st_size<=max_bytes,'ANIMATION_LIMIT','Clip exceeds the bounded document size.')
        payload=stream.read(max_bytes+1)
        require(len(payload)==metadata.st_size,'ANIMATION_HASH','Clip changed during bounded read.')
    import hashlib
    require(expected_sha256 is None or hashlib.sha256(payload).hexdigest()==expected_sha256,'ANIMATION_HASH','Clip differs from its expected exact bytes.')
    return _clip(decode_json(payload))


def move_keys(clip,track_id,start,end,*,scale=Fraction(1),offset=0):
    """Move, duplicate externally, or scale a selected inclusive key range.

    Time is scaled around the range start. Collision/control validation makes
    the edit transactional, including ranges adjoining unselected curve keys.
    """
    result=_clip(clip); low,high=frame_position(start),frame_position(end)
    scale,offset=_fraction(scale,'Scale'),_fraction(offset,'Offset')
    require(low<=high and scale>0,'ANIMATION_INTERVAL','Invalid retime range or scale.')
    track=next((t for t in result['tracks'] if t['id']==track_id),None)
    require(track is not None,'ANIMATION_REFERENCE','Track does not exist.')
    selected=[k for k in track['keys'] if low<=frame_position(k['time'])<=high]
    require(bool(selected),'ANIMATION_REFERENCE','Key selection is empty.')
    for key in selected:
        key['time']=stamp(low+(frame_position(key['time'])-low)*scale+offset)
        for field in ('in_tangent','out_tangent'):
            if field in key:
                value=key[field]; key[field]=[float(v/scale) for v in value] if isinstance(value,list) else float(value/scale)
        for field in ('in_handle','out_handle'):
            if field in key:key[field]['time_delta_frames']*=float(scale)
    track['keys'].sort(key=lambda k:frame_position(k['time']))
    return _checked(result)


def _annotate_edit(result, source, operation, **fields):
    parameters=result['provenance'].setdefault('parameters',{})
    prefix=next((f'clip_edit_{i}_' for i in range(32) if f'clip_edit_{i}_source_sha256' not in parameters),None)
    require(prefix is not None and len(parameters)+2+len(fields)<=128,
            'ANIMATION_LIMIT','No provenance capacity remains for the edit record.')
    parameters.update({prefix+'source_sha256':object_hash(source),prefix+'operation':operation,
                       **{prefix+k:v for k,v in fields.items()}})


def splice_frames(clip,start,count,*,registry=None,quaternion_tolerance_rad=None,max_bake_keys=4096):
    """Insert a held range or remove a range, preserving all unaffected samples.

    Removed-range joins are deliberate stepped cuts on the discrete tick clock.
    Events in a removed half-open range are explicitly removed; contact intervals
    are clipped. Source data and earlier history remain unchanged.
    """
    source=_validate_clip(clip)
    require(type(start) is int and type(count) is int and count!=0 and 0<=start<=source['duration_frames'],
            'ANIMATION_EDIT','Splice needs an integer frame and nonzero integer count.')
    require(count>0 or start-count<=source['duration_frames'],'ANIMATION_INTERVAL','Removal exceeds clip duration.')
    require(0<source['duration_frames']+count<=10000000,'ANIMATION_INTERVAL','Edited duration is outside the clip clock.')
    if count<0:
        require(not any(max(r['start_frame'],start)<min(r['end_frame'],start-count) for r in validate_named_regions(source)),
                'ANIMATION_REGION','Removal intersects a named region; edit/remove that range explicitly first.')
    result=_prepare_split(source,registry,quaternion_tolerance_rad,max_bake_keys)
    for index,track in enumerate(result['tracks']):
        if not track['keys']:continue
        if count>0:
            cut=_cut_track(track,Fraction(start))
            boundary=next(k for k in cut['keys'] if frame_position(k['time'])==start)
            held=deepcopy(boundary);held['interpolation']='step';held.pop('out_handle',None)
            keys=[deepcopy(k) for k in cut['keys'] if frame_position(k['time'])<start]+[held]
            for key in cut['keys']:
                position=frame_position(key['time'])
                if position<start:continue
                key=deepcopy(key);key['time']=stamp(position+count)
                if position==start:key.pop('in_handle',None)
                keys.append(key)
        else:
            end=start-count
            cut=_cut_track(track,Fraction(end))
            before=Fraction(start)-Fraction(1,TICKS_PER_FRAME)
            if start>0:cut=_cut_track(cut,before)
            keys=[deepcopy(k) for k in cut['keys'] if start>0 and frame_position(k['time'])<=before]
            if keys:keys[-1]['interpolation']='step';keys[-1].pop('out_handle',None)
            for key in cut['keys']:
                position=frame_position(key['time'])
                if position<end:continue
                key=deepcopy(key);key['time']=stamp(position+count)
                if position==end:key.pop('in_handle',None)
                keys.append(key)
        result['tracks'][index]=deepcopy(cut);result['tracks'][index]['keys']=keys
        _prune_controls(result['tracks'][index])
    def shifted(position):
        if count>0:return position+count if position>=start else position
        if start<=position<start-count:return None
        return position+count if position>=start-count else position
    events=[]
    for event in result['events']:
        payload=event.get('payload',{})
        is_ending=payload.get(REGION_DISCRIMINATOR)==REGION_VERSION and payload.get('animation3d_region_boundary')=='end' and event['frame']==start
        frame=start if is_ending else shifted(event['frame'])
        if frame is not None:event['frame']=int(frame);events.append(event)
    result['events']=events
    contacts=[]
    for contact in result['contacts']:
        if count>0:
            if contact['start_frame']>=start:contact['start_frame']+=count
            if contact['end_frame']>start:contact['end_frame']+=count
        else:
            for field in ('start_frame','end_frame'):
                position=shifted(contact[field]);contact[field]=start if position is None else int(position)
        if contact['start_frame']<contact['end_frame']:contacts.append(contact)
    result['contacts']=contacts;result['duration_frames']+=count
    if 'interruption_markers' in result:
        names={e['name'] for e in events if e['type']=='marker'}
        result['interruption_markers']=[name for name in result['interruption_markers'] if name in names]
    _annotate_edit(result,clip,'splice_frames',start_frame=start,count_frames=count)
    return _checked(result)


def retime_clip(clip, scale=Fraction(1), offset=0):
    """Scale a complete clip clock; preserve Hermite derivatives/Bezier geometry.

    Offset inserts initial time, extending the duration. Negative offsets that
    remove keys are rejected; use an explicit source range to trim playback.
    """
    result=_clip(clip); scale,offset=_fraction(scale,'Scale'),_fraction(offset,'Offset')
    require(scale>0 and offset>=0,'ANIMATION_EDIT','Retime requires positive scale and nonnegative offset.')
    duration=Fraction(result['duration_frames'])*scale+offset
    require(duration.denominator==1,'ANIMATION_TIME','Clip terminal duration must remain an integer frame.')
    result['duration_frames']=int(duration)
    for track in result['tracks']:
        for key in track['keys']:
            key['time']=stamp(frame_position(key['time'])*scale+offset)
            for field in ('in_tangent','out_tangent'):
                if field in key:
                    value=key[field]; key[field]=[float(Fraction(x)/scale) for x in value] if isinstance(value,list) else float(Fraction(value)/scale)
            for field in ('in_handle','out_handle'):
                if field in key: key[field]['time_delta_frames']*=float(scale)
    for event in result['events']:
        position=Fraction(event['frame'])*scale+offset
        require(position.denominator==1,'ANIMATION_TIME','Event must remain on an integer frame.')
        event['frame']=int(position)
    for contact in result['contacts']:
        for field in ('start_frame','end_frame'):
            position=Fraction(contact[field])*scale+offset
            require(position.denominator==1,'ANIMATION_TIME','Contact must remain on an integer frame.')
            contact[field]=int(position)
    return _checked(result)


def reverse_clip(clip):
    result=_clip(clip); duration=int(result['duration_frames'])
    fields=('in_tangent','out_tangent','in_handle','out_handle','ease_in','ease_out')
    for track in result['tracks']:
        original=deepcopy(track['keys']); keys=list(reversed(track['keys']))
        for i,key in enumerate(keys):
            old=deepcopy(key); key['time']=stamp(duration-frame_position(old['time']))
            for field in fields: key.pop(field,None)
            for incoming,outgoing in [('in_tangent','out_tangent'),('in_handle','out_handle'),('ease_in','ease_out')]:
                for source,target in [(incoming,outgoing),(outgoing,incoming)]:
                    if source in old:
                        value=deepcopy(old[source])
                        if 'tangent' in source: value=[-v for v in value] if isinstance(value,list) else -value
                        if 'handle' in source: value['time_delta_frames']=-value['time_delta_frames']
                        key[target]=value
            if 'bias' in key: key['bias']=-key['bias']
            key.pop('interpolation',None)
            if i<len(keys)-1:
                source=original[len(keys)-2-i]
                key['interpolation']=source.get('interpolation','step' if track['value_type']=='boolean' else 'quaternion-slerp' if track['value_type']=='quaternion' else 'linear')
        # A step is right-continuous. Reversing it is left-continuous; the
        # document clock has discrete ticks, so place the preceding value one
        # tick after the mirrored boundary instead of holding the wrong value.
        for a,b in zip(original,original[1:]):
            mode=a.get('interpolation','step' if track['value_type']=='boolean' else 'linear')
            if mode=='step':
                start=duration-frame_position(b['time']); end=duration-frame_position(a['time'])
                next_tick=start+Fraction(1,TICKS_PER_FRAME)
                if next_tick<end:
                    keys.append(dict(time=stamp(next_tick),value=deepcopy(a['value']),interpolation='step'))
        track['keys']=sorted(keys,key=lambda k:frame_position(k['time']))
    for event in result['events']:
        event['frame']=duration-int(event['frame'])
        payload=event.get('payload',{})
        if payload.get(REGION_DISCRIMINATOR)==REGION_VERSION:
            boundary='end' if payload['animation3d_region_boundary']=='start' else 'start'
            identifier=payload['animation3d_region_id']
            payload['animation3d_region_boundary']=boundary
            payload['animation3d_region_peer']=identifier+(':'+'end' if boundary=='start' else ':'+'start')
            event['id']=identifier+':'+boundary;event['name']=payload['animation3d_region_name']+':'+boundary
    result['events'].sort(key=lambda e:(e['frame'],e['id']))
    for contact in result['contacts']:
        contact['start_frame'],contact['end_frame']=duration-int(contact['end_frame']),duration-int(contact['start_frame'])
    # Reversing extracted root displacement changes its direction.
    if 'cycle_displacement' in result['root_motion']:
        result['root_motion']['cycle_displacement']=[-v for v in result['root_motion']['cycle_displacement']]
    return _checked(result)


def _bake_quaternion_track(track, registry, tolerance, max_keys, max_samples=None):
    """Bounded adaptive SLERP conversion with explicit sampled-error evidence."""
    import math
    import numpy as np
    from rig_math import slerp
    from animation3d_registry import sample_track
    require(type(tolerance) in (int,float) and 0<tolerance<=.1,
            'ANIMATION_EDIT','Quaternion baking needs a finite angular tolerance in (0, .1] radians.')
    require(type(max_keys) is int and 2<=max_keys<=250000,
            'ANIMATION_LIMIT','Quaternion bake key cap must be 2..250000.')
    require(registry is not None,'ANIMATION_OWNER','Quaternion baking needs the admitted target registry.')
    entry=registry._validated_track_entry(track)
    class PinnedRegistry:
        def _validated_track_entry(self, candidate):
            require(candidate is self.current,'ANIMATION_OWNER','Curve sampler must use the admitted detached track.')
            return entry
    pinned=PinnedRegistry(); cache={}; count=0; accepted_error=0.
    positions=[frame_position(k['time']) for k in track['keys']]
    def sample(t):
        nonlocal count
        if t not in cache:
            count+=1
            require(count<=(max_keys*16 if max_samples is None else max_samples),'ANIMATION_LIMIT','Quaternion bake sample budget exceeded.')
            from bisect import bisect_right
            segment=max(0,min(len(positions)-2,bisect_right(positions,t)-1))
            # SQUAD uses only each endpoint and its adjacent neighbors. Sampling
            # that exact native neighborhood avoids repeated full-track scans.
            current=dict(track,keys=track['keys'][max(0,segment-1):min(len(positions),segment+3)])
            pinned.current=current
            cache[t]=sample_track(current,stamp(t),pinned)
        return cache[t]
    def angular(a,b):
        dot=abs(float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b))))
        return 2*math.acos(min(1.,max(0.,dot)))
    result=deepcopy(track); result['keys']=[]
    def append(t,value,mode):
        if result['keys'] and frame_position(result['keys'][-1]['time'])==t:
            result['keys'][-1]['interpolation']=mode
            return
        require(len(result['keys'])<max_keys,'ANIMATION_LIMIT','Quaternion bake key budget exceeded.')
        result['keys'].append(dict(time=stamp(t),value=deepcopy(value),interpolation=mode))
    def segment(a,b,depth=0):
        nonlocal accepted_error
        av,bv=sample(a),sample(b); worst=0.
        # Snap internal probes to the declared discrete tick clock.
        probes=sorted({Fraction((a*TICKS_PER_FRAME + (b-a)*TICKS_PER_FRAME*f).numerator//
                                (a*TICKS_PER_FRAME + (b-a)*TICKS_PER_FRAME*f).denominator,TICKS_PER_FRAME)
                       for f in (Fraction(1,4),Fraction(1,2),Fraction(3,4))})
        for t in probes:
            if a<t<b:worst=max(worst,angular(sample(t),slerp(av,bv,float((t-a)/(b-a)))))
        if worst<=tolerance:
            accepted_error=max(accepted_error,worst)
            append(a,av,'quaternion-slerp'); append(b,bv,'quaternion-slerp'); return
        middle_ticks=((a+b)*TICKS_PER_FRAME/2).__floor__()
        middle=Fraction(middle_ticks,TICKS_PER_FRAME)
        require(depth<32 and a<middle<b,'ANIMATION_LIMIT','Quaternion tolerance cannot be represented within the bounded tick clock.')
        segment(a,middle,depth+1);segment(middle,b,depth+1)
    keys=track['keys']
    for index,(a,b) in enumerate(zip(keys,keys[1:])):
        start,end=frame_position(a['time']),frame_position(b['time'])
        if _segment_mode(track,a)=='quaternion-squad':segment(start,end)
        else:
            append(start,a['value'],_segment_mode(track,a));append(end,b['value'],'quaternion-slerp')
    if len(keys)==1:append(frame_position(keys[0]['time']),keys[0]['value'],'quaternion-slerp')
    return result,dict(tolerance_rad=float(tolerance),maximum_probe_error_rad=accepted_error,
                       samples=count,keys=len(result['keys']))


def _prepare_split(clip, registry, tolerance, max_keys):
    require(type(max_keys) is int and 2<=max_keys<=250000,'ANIMATION_LIMIT','Quaternion bake key cap must be 2..250000.')
    require(tolerance is None or type(tolerance) in (int,float) and 0<tolerance<=.1,'ANIMATION_EDIT','Invalid quaternion bake tolerance.')
    value=_clip(clip); reports=[];remaining_keys=max_keys;remaining_samples=max_keys*16
    for index,track in enumerate(value['tracks']):
        if any(_segment_mode(track,k)=='quaternion-squad' for k in track['keys'][:-1]):
            require(tolerance is not None,'ANIMATION_UNSUPPORTED',
                    'Exact SQUAD subdivision is unavailable; request an explicit bounded angular bake.')
            require(remaining_keys>=2 and remaining_samples>0,'ANIMATION_LIMIT','Aggregate quaternion bake budget exhausted.')
            value['tracks'][index],report=_bake_quaternion_track(track,registry,tolerance,remaining_keys,remaining_samples)
            remaining_keys-=report['keys'];remaining_samples-=report['samples'];reports.append(report)
    if reports:
        parameters=value['provenance'].setdefault('parameters',{})
        prefix=next((f'squad_bake_{i}_' for i in range(16) if f'squad_bake_{i}_source_sha256' not in parameters),None)
        require(prefix is not None and len(parameters)+5<=128,'ANIMATION_LIMIT','No provenance capacity remains for the conversion record.')
        parameters.update({prefix+'source_sha256':object_hash(clip),prefix+'tolerance_rad':float(tolerance),
                           prefix+'maximum_probe_error_rad':max(r['maximum_probe_error_rad'] for r in reports),
                           prefix+'samples':sum(r['samples'] for r in reports),prefix+'keys':sum(r['keys'] for r in reports)})
    return value


def _segment_mode(track, key):
    return key.get('interpolation', 'step' if track['value_type']=='boolean' else
                   'quaternion-slerp' if track['value_type']=='quaternion' else 'linear')


def _editable_track(track):
    """Convert polynomial segments to equivalent Bezier geometry before edits."""
    import numpy as np
    from animation3d_curves import _slope
    result=deepcopy(track); keys=result['keys']; original=deepcopy(keys)
    positions=[frame_position(k['time']) for k in keys]
    for key in keys:
        for field in ('in_tangent','out_tangent','in_handle','out_handle','ease_in','ease_out','tension','continuity','bias'):
            key.pop(field,None)
    for i,(a,b) in enumerate(zip(original,original[1:])):
        mode=_segment_mode(track,a); duration=float(positions[i+1]-positions[i])
        require(mode!='quaternion-squad','ANIMATION_UNSUPPORTED',
                'SQUAD subdivision needs an explicit quaternion control representation.')
        av,bv=np.asarray(a['value'],dtype=float),np.asarray(b['value'],dtype=float)
        shape=lambda v:v.tolist() if isinstance(a['value'],list) else float(v.item())
        if mode=='bezier':
            keys[i]['out_handle']=deepcopy(a['out_handle']); keys[i+1]['in_handle']=deepcopy(b['in_handle'])
        elif mode in ('hermite','ease','catmull-rom','tcb'):
            if mode=='hermite': left,right=np.asarray(a['out_tangent']),np.asarray(b['in_tangent'])
            elif mode=='ease':
                left=(bv-av)*(1-a.get('ease_out',1))/duration
                right=(bv-av)*(1-b.get('ease_in',1))/duration
            else:
                use=original if mode=='tcb' else [{k:v for k,v in key.items() if k not in ('tension','continuity','bias')} for key in original]
                left,right=_slope(use,positions,i,True),_slope(use,positions,i+1,False)
            keys[i]['out_handle']=dict(time_delta_frames=duration/3,value_delta=shape(left*duration/3))
            keys[i+1]['in_handle']=dict(time_delta_frames=-duration/3,value_delta=shape(-right*duration/3))
            mode='bezier'
        keys[i]['interpolation']=mode
    if keys: keys[-1]['interpolation']='step' if track['value_type']=='boolean' else 'quaternion-slerp' if track['value_type']=='quaternion' else 'linear'
    return result


def _cut_track(track, position):
    """Insert one exact boundary, subdividing existing Bezier control geometry."""
    import numpy as np
    from animation3d_curves import evaluate_segment, _bezier
    result=_editable_track(track); keys=result['keys']
    if not keys:return result
    positions=[frame_position(k['time']) for k in keys]
    if position in positions:return result
    if position<positions[0] or position>positions[-1]:
        source=keys[0] if position<positions[0] else keys[-1]
        keys.append(dict(time=stamp(position),value=deepcopy(source['value']),interpolation='step'))
        keys.sort(key=lambda k:frame_position(k['time'])); return result
    right=next(i for i,p in enumerate(positions) if p>position); left=right-1
    a,b=keys[left],keys[right]; mode=_segment_mode(result,a)
    value=evaluate_segment(result,left,position)
    middle=dict(time=stamp(position),value=value,interpolation=mode)
    if mode=='bezier':
        av,bv=np.asarray(a['value'],dtype=float),np.asarray(b['value'],dtype=float)
        shape=lambda v:v.tolist() if isinstance(a['value'],list) else float(v.item())
        duration=float(positions[right]-positions[left]); target=float(position-positions[left])
        outgoing,incoming=a['out_handle'],b['in_handle']
        ow=outgoing.get('weight',1) if outgoing.get('weighted',False) else 1
        iw=incoming.get('weight',1) if incoming.get('weighted',False) else 1
        points=[(0.,av),(outgoing['time_delta_frames']*ow,av+np.asarray(outgoing['value_delta'])*ow),
                (duration+incoming['time_delta_frames']*iw,bv+np.asarray(incoming['value_delta'])*iw),(duration,bv)]
        lo,hi=0.,1.
        for _ in range(64):
            u=(lo+hi)/2
            if _bezier(*(p[0] for p in points),u)<target:lo=u
            else:hi=u
        u=(lo+hi)/2
        lerp=lambda p,q:((1-u)*p[0]+u*q[0],(1-u)*p[1]+u*q[1])
        ab,bc,cd=[lerp(p,q) for p,q in zip(points,points[1:])]
        abc,bcd=lerp(ab,bc),lerp(bc,cd); center=lerp(abc,bcd)
        # The exact document boundary is retained; floating control geometry
        # is relative to that boundary, not a rounded frame timestamp.
        def handle(point,origin):return dict(time_delta_frames=point[0]-origin[0],value_delta=shape(point[1]-origin[1]))
        a['out_handle']=handle(ab,points[0]); middle['in_handle']=handle(abc,center)
        middle['out_handle']=handle(bcd,center); b['in_handle']=handle(cd,points[3])
        middle['value']=shape(center[1])
    keys.insert(right,middle); return result


def _prune_controls(track):
    keys=track['keys']
    for i,key in enumerate(keys):
        if not i:key.pop('in_handle',None)
        if i==len(keys)-1:
            key.pop('out_handle',None)
            key['interpolation']='step' if track['value_type']=='boolean' else 'quaternion-slerp' if track['value_type']=='quaternion' else 'linear'


def split_clip(clip, frame, *, left_id=None, right_id=None, registry=None,
               quaternion_tolerance_rad=None, max_bake_keys=4096):
    """Split at an integer frame; boundary events belong only to the right.

    Cubics are subdivided geometrically. SQUAD has no persistent control-point
    representation and rejects rather than changing its neighboring controls.
    """
    source=_validate_clip(clip)
    require(type(frame) is int and 0<frame<source['duration_frames'],'ANIMATION_INTERVAL','Split frame must be strictly inside the clip.')
    require(not source['root_motion']['enabled'],'ANIMATION_UNSUPPORTED','Split extracted root motion through its execution owner.')
    named=validate_named_regions(source)
    require(not any(r['start_frame']<frame<r['end_frame'] for r in named),'ANIMATION_REGION','Split crosses a named region; edit/remove that range explicitly first.')
    value=_prepare_split(source,registry,quaternion_tolerance_rad,max_bake_keys)
    left,right=deepcopy(value),deepcopy(value)
    left['id']=left_id or value['id']+'-left'; right['id']=right_id or value['id']+'-right'
    left['duration_frames']=frame; right['duration_frames']-=frame
    left.pop('exit_pose',None);right.pop('entry_pose',None)
    for i,track in enumerate(value['tracks']):
        cut=_cut_track(track,Fraction(frame))
        left['tracks'][i]=deepcopy(cut); right['tracks'][i]=deepcopy(cut)
        left['tracks'][i]['keys']=[k for k in left['tracks'][i]['keys'] if frame_position(k['time'])<=frame]
        right['tracks'][i]['keys']=[k for k in right['tracks'][i]['keys'] if frame_position(k['time'])>=frame]
        for key in right['tracks'][i]['keys']:key['time']=stamp(frame_position(key['time'])-frame)
        _prune_controls(left['tracks'][i]); _prune_controls(right['tracks'][i])
    left_regions={r['id'] for r in named if r['end_frame']<=frame}
    def goes_left(event):
        payload=event.get('payload',{})
        return payload['animation3d_region_id'] in left_regions if REGION_DISCRIMINATOR in payload else event['frame']<frame
    left['events']=[e for e in left['events'] if goes_left(e)]
    right['events']=[e for e in right['events'] if not goes_left(e)]
    for event in right['events']:event['frame']-=frame
    for result,start,end in ((left,0,frame),(right,frame,value['duration_frames'])):
        result['contacts']=[c for c in result['contacts'] if c['start_frame']<end and c['end_frame']>start]
        for contact in result['contacts']:
            contact['start_frame']=max(start,contact['start_frame'])-start
            contact['end_frame']=min(end,contact['end_frame'])-start
        if 'interruption_markers' in result:
            names={e['name'] for e in result['events'] if e['type']=='marker'}
            result['interruption_markers']=[name for name in result['interruption_markers'] if name in names]
        _checked(result)
    return left,right


def join_clips(left, right, *, identifier=None, name=None, registry=None,
               quaternion_tolerance_rad=None,max_bake_keys=4096):
    """Join compatible clips with an equal seam; differing state rejects.

    Exact matching targets/track settings and clip metadata are required. Events
    keep identity; a split contact with matching metadata is rejoined by ID.
    """
    left,right=_clip(left),_clip(right)
    require(not ({r['id'] for r in validate_named_regions(left)} & {r['id'] for r in validate_named_regions(right)}),
            'ANIMATION_REGION','Joined clips have conflicting named region identities.')
    ignored={'id','name','duration_frames','tracks','events','contacts','entry_pose','exit_pose','interruption_markers','tags','categories'}
    require({k:v for k,v in left.items() if k not in ignored}=={k:v for k,v in right.items() if k not in ignored},
            'ANIMATION_IDENTITY','Clip execution metadata differs.')
    require(not left['root_motion']['enabled'],'ANIMATION_UNSUPPORTED','Join extracted root motion through its execution owner.')
    original_left,original_right=left,right
    left=_prepare_split(left,registry,quaternion_tolerance_rad,max_bake_keys)
    prior_parameters=original_left['provenance'].get('parameters',{})
    used=sum(v for k,v in left['provenance'].get('parameters',{}).items() if k.endswith('_keys') and k.startswith('squad_bake_') and k not in prior_parameters)
    remaining=max_bake_keys-used
    right_squad=any(_segment_mode(t,k)=='quaternion-squad' for t in right['tracks'] for k in t['keys'][:-1])
    require(not right_squad or remaining>=2,'ANIMATION_LIMIT','Aggregate quaternion join bake budget exhausted.')
    right=_prepare_split(right,registry,quaternion_tolerance_rad,max(2,remaining))
    require(len(left['tracks'])==len(right['tracks']),'ANIMATION_IDENTITY','Track sets differ.')
    result=deepcopy(left); offset=int(left['duration_frames']); result['duration_frames']=offset+int(right['duration_frames'])
    if identifier is not None:result['id']=identifier
    if name is not None:result['name']=name
    right_tracks={t['id']:t for t in right['tracks']}
    for index,track in enumerate(left['tracks']):
        other=right_tracks.get(track['id'])
        require(other is not None and {k:v for k,v in track.items() if k!='keys'}=={k:v for k,v in other.items() if k!='keys'},
                'ANIMATION_IDENTITY','Track target or settings differ.')
        a=_cut_track(track,Fraction(offset)); b=_cut_track(other,Fraction(0))
        if not a['keys'] and not b['keys']:continue
        require(bool(a['keys']) and bool(b['keys']),'ANIMATION_CONTINUITY','Join seam has an unmatched keyed track.')
        same=a['keys'][-1]['value']==b['keys'][0]['value']
        if track['value_type']=='quaternion':
            import numpy as np
            av,bv=np.asarray(a['keys'][-1]['value']),np.asarray(b['keys'][0]['value'])
            av,bv=av/np.linalg.norm(av),bv/np.linalg.norm(bv)
            same=bool(np.allclose(av,bv,atol=1e-12,rtol=0) or np.allclose(av,-bv,atol=1e-12,rtol=0))
        require(same,'ANIMATION_CONTINUITY','Join seam values differ; author an explicit transition.')
        seam=deepcopy(b['keys'][0]); seam['time']=stamp(offset)
        seam['value']=deepcopy(a['keys'][-1]['value'])
        if 'in_handle' in a['keys'][-1]:seam['in_handle']=deepcopy(a['keys'][-1]['in_handle'])
        keys=a['keys'][:-1]+[seam]
        for key in b['keys'][1:]:
            key=deepcopy(key); key['time']=stamp(frame_position(key['time'])+offset); keys.append(key)
        result['tracks'][index]['keys']=keys
    result['events']+=deepcopy(right['events'])
    for event in result['events'][len(left['events']):]:event['frame']+=offset
    contacts={c['id']:deepcopy(c) for c in result['contacts']}
    for source in right['contacts']:
        contact=deepcopy(source); contact['start_frame']+=offset; contact['end_frame']+=offset
        prior=contacts.get(contact['id'])
        if prior:
            require({k:v for k,v in prior.items() if k not in ('start_frame','end_frame')}=={k:v for k,v in contact.items() if k not in ('start_frame','end_frame')}
                    and prior['end_frame']==contact['start_frame'],'ANIMATION_CONTINUITY','Contact identity conflicts across join.')
            prior['end_frame']=contact['end_frame']
        else:contacts[contact['id']]=contact
    result['contacts']=list(contacts.values())
    right_parameters=right['provenance'].get('parameters',{})
    original_parameters=original_right['provenance'].get('parameters',{})
    prefixes=[k[:-len('source_sha256')] for k in right_parameters if k.startswith('squad_bake_') and k.endswith('_source_sha256') and k not in original_parameters]
    for source_prefix in prefixes:
        parameters=result['provenance'].setdefault('parameters',{})
        prefix=next((f'squad_bake_{i}_' for i in range(16) if f'squad_bake_{i}_source_sha256' not in parameters),None)
        block={prefix+k[len(source_prefix):]:v for k,v in right_parameters.items() if k.startswith(source_prefix)} if prefix else {}
        require(prefix is not None and len(parameters)+len(block)<=128,'ANIMATION_LIMIT','No provenance capacity remains for the right conversion record.')
        parameters.update(block)
    for field in ('tags','categories','interruption_markers'):
        if field in left or field in right:result[field]=list(dict.fromkeys(left.get(field,[])+right.get(field,[])))
    if 'exit_pose' in right:result['exit_pose']=right['exit_pose']
    else:result.pop('exit_pose',None)
    return _checked(result)


def source_time(clip, output_time, output_fps, *, source_in=0, source_out=None,
                speed=Fraction(1), reverse=False, loop=False):
    """Map elapsed output time into an explicit half-open source range.

    Nonloop playback holds its terminal source key; reverse starts at out.
    """
    _clip(clip); source_in=_fraction(source_in,'Source start')
    source_out=_fraction(clip['duration_frames'] if source_out is None else source_out,'Source end')
    speed=_fraction(speed,'Playback speed')
    require(0<=source_in<source_out<=clip['duration_frames'] and speed>0,
            'ANIMATION_INTERVAL','Invalid source range or playback speed.')
    elapsed=frame_position(output_time)/rate(output_fps)*rate(clip['fps'])*speed
    length=source_out-source_in
    elapsed=elapsed%length if loop else min(length,elapsed)
    return stamp(source_out-elapsed if reverse else source_in+elapsed)


def events_between(clip, previous, current):
    """Forward interval (previous,current]; reverse [current,previous).

    Explicit source positions avoid hidden player history. A caller crossing a
    loop seam submits the two intervals separately; no events are fabricated.
    """
    value=_clip(clip); a,b=frame_position(previous),frame_position(current)
    require(max(a,b)<=value['duration_frames'],'ANIMATION_TIME','Event interval exceeds clip duration.')
    events=[e for e in value['events'] if (a<e['frame']<=b if a<=b else b<=e['frame']<a)]
    return deepcopy(sorted(events,key=lambda e:(e['frame'],e['id']),reverse=b<a))


def _document_cost(value):
    """Conservative retained JSON-object estimate, distinct from process RSS."""
    import json
    return len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'))*64


class ClipHistory:
    """Validated reversible transactions with bounded retained document state."""
    def __init__(self, clip, *, max_edits=256,max_bytes=64*1024*1024):
        require(type(max_edits) is int and 1<=max_edits<=4096,'ANIMATION_LIMIT','Edit history capacity must be 1..4096.')
        require(type(max_bytes) is int and 1<=max_bytes<=1024**3,'ANIMATION_LIMIT','History estimate must be 1 byte..1 GiB.')
        value=_validate_clip(clip);cost=_document_cost(value)
        require(cost<=max_bytes,'ANIMATION_LIMIT','Initial history document exceeds its memory estimate.')
        self._past=[];self._future=[];self._value=deepcopy(value);self._max_edits=max_edits
        self._past_costs=[];self._future_costs=[];self._current_cost=cost;self._bytes=cost;self._max_bytes=max_bytes
    @property
    def document(self):return deepcopy(self._value)
    @property
    def estimated_bytes(self):return self._bytes
    def apply(self, operation, *args, **kwargs):
        require(len(self._past)<self._max_edits,'ANIMATION_LIMIT','Edit history is full; save an explicit checkpoint before further edits.')
        candidate=_validate_clip(operation(self.document,*args,**kwargs));cost=_document_cost(candidate)
        admitted=self._bytes-sum(self._future_costs)+cost
        require(admitted<=self._max_bytes,'ANIMATION_LIMIT','Edit history memory estimate exceeded.')
        owned=deepcopy(candidate)
        self._past.append(self._value);self._past_costs.append(self._current_cost)
        self._value=owned;self._current_cost=cost;self._bytes=admitted;self._future=[];self._future_costs=[]
        return self.document
    def undo(self):
        require(bool(self._past),'ANIMATION_EDIT','Nothing to undo.')
        self._future.append(self._value);self._future_costs.append(self._current_cost)
        self._value=self._past.pop();self._current_cost=self._past_costs.pop();return self.document
    def redo(self):
        require(bool(self._future),'ANIMATION_EDIT','Nothing to redo.')
        self._past.append(self._value);self._past_costs.append(self._current_cost)
        self._value=self._future.pop();self._current_cost=self._future_costs.pop();return self.document


class ClipLibrary:
    """Bounded detached clip index; AssetLibrary owns persistent versions/blobs."""
    def __init__(self,*,max_clips=4096,max_bytes=64*1024*1024):
        require(type(max_clips) is int and 1<=max_clips<=4096,'ANIMATION_LIMIT','Clip capacity must be 1..4096.')
        require(type(max_bytes) is int and 1<=max_bytes<=1024**3,'ANIMATION_LIMIT','Clip memory estimate must be 1 byte..1 GiB.')
        import threading
        self._lock=threading.RLock()
        self._clips={};self._max_clips=max_clips;self._max_bytes=max_bytes;self._bytes=0
    def _admit(self,clip):
        _validate_clip(clip)
        key=(clip['id'],clip['version']);digest=object_hash(clip)
        prior=self._clips.get(key)
        require(prior is None or prior[0]==digest,'ANIMATION_IDENTITY','An immutable clip version already has different content.')
        # Same conservative JSON factor as native document admission; this is a
        # cache estimate, not an operating-system allocation sandbox.
        cost=_document_cost(clip)
        require(prior is not None or len(self._clips)<self._max_clips,'ANIMATION_LIMIT','Clip index capacity exceeded.')
        require(prior is not None or self._bytes+cost<=self._max_bytes,'ANIMATION_LIMIT','Clip index memory estimate exceeded.')
        return deepcopy(clip),key,digest,cost,prior
    def add(self, clip):
        with self._lock:
            value,key,digest,cost,prior=self._admit(clip)
            if prior is None:self._clips[key]=(digest,value);self._bytes+=cost
            return digest
    def get(self, identifier, version, digest):
        with self._lock:
            found=self._clips.get((identifier,version))
            require(found is not None and found[0]==digest,'ANIMATION_HASH','Exact clip version/hash is unavailable.')
            return deepcopy(found[1])
    def search(self, text='', tags=()):
        from itertools import islice
        require(isinstance(text,str) and len(text)<=4096,'ANIMATION_EDIT','Search text must be bounded text.')
        require(not isinstance(tags,(str,bytes)),'ANIMATION_EDIT','Search tags must be an iterable of text values.')
        try:tags=list(islice(iter(tags),129))
        except TypeError:require(False,'ANIMATION_EDIT','Search tags must be an iterable of text values.')
        require(len(tags)<=128 and all(isinstance(t,str) and len(t)<=4096 for t in tags),
                'ANIMATION_LIMIT','Search tags must be bounded text values.')
        wanted=set(tags)
        with self._lock:
            return [dict(id=v['id'],version=v['version'],sha256=h,name=v['name'])
                    for _,(h,v) in sorted(self._clips.items())
                    if text.casefold() in v['name'].casefold() and wanted<=set(v['tags'])]
    def duplicate(self,identifier,version,digest,*,new_id,new_name=None,new_version=None):
        value=self.get(identifier,version,digest)
        value['id']=new_id
        if new_name is not None:value['name']=new_name
        if new_version is not None:value['version']=new_version
        return _clip(value)
    def versions(self,identifier):
        with self._lock:
            return [dict(id=i,version=v,sha256=h,name=clip['name'])
                    for (i,v),(h,clip) in sorted(self._clips.items()) if i==identifier]
    def restore(self,identifier,version,digest):
        # Selecting an older immutable version never rewrites another version.
        return self.get(identifier,version,digest)
    def compare(self,left,right):
        a=self.get(*left);b=self.get(*right)
        return dict(left_sha256=object_hash(a),right_sha256=object_hash(b),
                    equal=a==b,changed_fields=[k for k in sorted(set(a)|set(b)) if a.get(k)!=b.get(k)])
    def import_clip(self,path):
        value=load_clip(path,max_bytes=max(1,min(16*1024**2,self._max_bytes//64)));digest=self.add(value)
        return dict(id=value['id'],version=value['version'],sha256=digest)
    def load_asset(self,resolver,reference,path):
        """Resolve one exact clip through the canonical persistent authority."""
        from asset_library_contracts import load_manifest,canonical_bytes,validate
        validate(reference,'AssetRef')
        manifest=load_manifest(canonical_bytes(resolver.resolve(reference)))
        require((manifest['identity']['id'],manifest['version'],manifest['manifest_sha256'])==(reference['id'],reference['version'],reference['sha256']),
                'ANIMATION_IDENTITY','Resolver returned a different asset identity/version/hash.')
        require(manifest['identity']['kind']=='animation','ANIMATION_OWNER','Asset is not an animation.')
        record=next((f for f in manifest['files'] if f['path']==path),None)
        require(record is not None and record['format']=='json','ANIMATION_OWNER','Exact asset file must be a JSON clip.')
        require(record['bytes']<=min(16*1024**2,self._max_bytes//64),'ANIMATION_LIMIT','Clip asset exceeds the document/cache admission estimate.')
        payload=resolver.read_file(reference,path)
        import hashlib
        require(len(payload)==record['bytes'] and hashlib.sha256(payload).hexdigest()==record['sha256'],
                'ANIMATION_HASH','Resolved clip file differs from exact asset manifest.')
        value=_clip(decode_json(payload));digest=self.add(value)
        return dict(id=value['id'],version=value['version'],sha256=digest,asset=deepcopy(reference),path=path)
    def register_asset(self,library,manifest,sources,path):
        """Delegate exclusive immutable publication; no second asset store."""
        from asset_library_contracts import load_manifest,canonical_bytes
        manifest=load_manifest(canonical_bytes(manifest))
        require(isinstance(sources,dict),'ANIMATION_EDIT','Asset sources must be an explicit path mapping.')
        require(manifest['identity']['kind']=='animation','ANIMATION_OWNER','Asset is not an animation.')
        record=next((f for f in manifest['files'] if f['path']==path),None)
        require(record is not None and record['format']=='json' and path in sources,
                'ANIMATION_OWNER','Animation manifest must name an explicit JSON clip source.')
        value=load_clip(sources[path],max_bytes=max(1,min(16*1024**2,self._max_bytes//64)),expected_sha256=record['sha256'])
        # Keep view admission stable while its canonical immutable transaction
        # executes; another thread cannot replace or exhaust the admitted slot.
        with self._lock:
            self._admit(value)
            reference=library.register(manifest,sources)
            return self.load_asset(library,reference,path)
