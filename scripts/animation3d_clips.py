"""Detached validated clip edits, exact source clocks and immutable motion reuse.

These document operations do not run a player, state machine or renderer.
"""
from copy import deepcopy
from fractions import Fraction

from animation3d_contracts import KINDS, validate_contract
from animation3d_time import frame_position, rate, TICKS_PER_FRAME
from rig_errors import require, object_hash, atomic_json, read_json


def stamp(position):
    position=Fraction(position); ticks=position*TICKS_PER_FRAME
    require(ticks.denominator==1,'ANIMATION_TIME','Edited time exceeds the exact tick precision.')
    frame,subframe=divmod(ticks.numerator,TICKS_PER_FRAME)
    result={'frame':frame,'subframe_ticks':subframe}; frame_position(result)
    return result


def _clip(value):
    validate_contract(value)
    require(value['kind']==KINDS['clip'],'ANIMATION_KIND','Expected a named animation clip.')
    return deepcopy(value)


def edit_key(clip, track_id, *, key=None, remove=None):
    """Insert a unique key or remove the key at an exact timestamp atomically."""
    result=_clip(clip)
    require((key is None)!=(remove is None),'ANIMATION_EDIT','Choose insertion or removal.')
    track=next((t for t in result['tracks'] if t['id']==track_id),None)
    require(track is not None,'ANIMATION_REFERENCE','Track does not exist.')
    position=frame_position(key['time'] if key is not None else remove)
    found=[i for i,k in enumerate(track['keys']) if frame_position(k['time'])==position]
    if key is not None:
        require(not found,'ANIMATION_DUPLICATE','A key already occupies this time.')
        track['keys'].append(deepcopy(key)); track['keys'].sort(key=lambda k:frame_position(k['time']))
    else:
        require(bool(found),'ANIMATION_REFERENCE','No key occupies the requested time.')
        del track['keys'][found[0]]
    validate_contract(result)
    return result


def edit_clip(clip, *, name=None, duration=None, fps=None, add_track=None, remove_track=None):
    """Rename/extend/trim/configure a clip and add/remove actual typed tracks.

    Trimming never silently drops events or keys: use explicit edits first.
    """
    result=_clip(clip)
    for key,value in [('name',name),('duration_frames',duration),('fps',fps)]:
        if value is not None: result[key]=deepcopy(value)
    if add_track is not None: result['tracks'].append(deepcopy(add_track))
    if remove_track is not None:
        require(any(t['id']==remove_track for t in result['tracks']),'ANIMATION_REFERENCE','Track does not exist.')
        result['tracks']=[t for t in result['tracks'] if t['id']!=remove_track]
    validate_contract(result); return result


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
    added=deepcopy(keys)
    for key in added: key['time']=stamp(frame_position(key['time'])+Fraction(offset))
    track['keys'].extend(added); track['keys'].sort(key=lambda k:frame_position(k['time']))
    validate_contract(result); return result


def save_clip(path, clip):
    """Persist a validated document with the shared crash-safe writer."""
    atomic_json(path,_clip(clip))


def load_clip(path): return _clip(read_json(path))


def move_keys(clip,track_id,start,end,*,scale=Fraction(1),offset=0):
    """Move, duplicate externally, or scale a selected inclusive key range.

    Time is scaled around the range start. Collision/control validation makes
    the edit transactional, including ranges adjoining unselected curve keys.
    """
    result=_clip(clip); low,high=frame_position(start),frame_position(end)
    scale,offset=Fraction(scale),Fraction(offset)
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
    validate_contract(result); return result


def splice_frames(clip,start,count):
    """Insert count frames; a negative count removes that many frames.

    Removal explicitly discards keys/events inside the removed interval and
    clips contact intervals. An edit that leaves invalid curve controls rejects
    atomically; the caller can edit the join controls explicitly first.
    """
    result=_clip(clip)
    require(type(start) is int and type(count) is int and count!=0 and 0<=start<=result['duration_frames'],
            'ANIMATION_EDIT','Splice needs an integer frame and nonzero integer count.')
    require(count>0 or start-count<=result['duration_frames'],'ANIMATION_INTERVAL','Removal exceeds clip duration.')
    def shifted(position):
        if count>0:return position+count if position>=start else position
        if start<=position<start-count:return None
        return position+count if position>=start-count else position
    for track in result['tracks']:
        retained=[]
        for key in track['keys']:
            position=shifted(frame_position(key['time']))
            if position is not None:key['time']=stamp(position); retained.append(key)
        track['keys']=retained
    events=[]
    for event in result['events']:
        frame=shifted(event['frame'])
        if frame is not None:event['frame']=int(frame); events.append(event)
    result['events']=events
    contacts=[]
    for contact in result['contacts']:
        for field in ('start_frame','end_frame'):
            position=shifted(contact[field]); contact[field]=start if position is None else int(position)
        if contact['start_frame']<contact['end_frame']:contacts.append(contact)
    result['contacts']=contacts
    result['duration_frames']+=count
    validate_contract(result); return result


def retime_clip(clip, scale=Fraction(1), offset=0):
    """Scale a complete clip clock; preserve Hermite derivatives/Bezier geometry.

    Offset inserts initial time, extending the duration. Negative offsets that
    remove keys are rejected; use an explicit source range to trim playback.
    """
    result=_clip(clip); scale,offset=Fraction(scale),Fraction(offset)
    require(scale>0 and offset>=0,'ANIMATION_EDIT','Retime requires positive scale and nonnegative offset.')
    duration=result['duration_frames']*scale+offset
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
        position=event['frame']*scale+offset
        require(position.denominator==1,'ANIMATION_TIME','Event must remain on an integer frame.')
        event['frame']=int(position)
    for contact in result['contacts']:
        for field in ('start_frame','end_frame'):
            position=contact[field]*scale+offset
            require(position.denominator==1,'ANIMATION_TIME','Contact must remain on an integer frame.')
            contact[field]=int(position)
    validate_contract(result); return result


def reverse_clip(clip):
    result=_clip(clip); duration=result['duration_frames']
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
    for event in result['events']: event['frame']=duration-event['frame']
    result['events'].sort(key=lambda e:(e['frame'],e['id']))
    for contact in result['contacts']:
        contact['start_frame'],contact['end_frame']=duration-contact['end_frame'],duration-contact['start_frame']
    # Reversing extracted root displacement changes its direction.
    if 'cycle_displacement' in result['root_motion']:
        result['root_motion']['cycle_displacement']=[-v for v in result['root_motion']['cycle_displacement']]
    validate_contract(result); return result


def source_time(clip, output_time, output_fps, *, source_in=0, source_out=None,
                speed=Fraction(1), reverse=False, loop=False):
    """Map elapsed output time into an explicit half-open source range.

    Nonloop playback holds its terminal source key; reverse starts at out.
    """
    _clip(clip); source_in=Fraction(source_in)
    source_out=Fraction(clip['duration_frames'] if source_out is None else source_out)
    speed=Fraction(speed)
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


class ClipHistory:
    """Validated reversible document transactions; callers never alias history."""
    def __init__(self, clip): self._past=[]; self._future=[]; self._value=_clip(clip)
    @property
    def document(self): return deepcopy(self._value)
    def apply(self, operation, *args, **kwargs):
        candidate=_clip(operation(self.document,*args,**kwargs))
        self._past.append(self._value); self._value=candidate; self._future=[]
        return self.document
    def undo(self):
        require(bool(self._past),'ANIMATION_EDIT','Nothing to undo.')
        self._future.append(self._value); self._value=self._past.pop(); return self.document
    def redo(self):
        require(bool(self._future),'ANIMATION_EDIT','Nothing to redo.')
        self._past.append(self._value); self._value=self._future.pop(); return self.document


class ClipLibrary:
    """Named exact-version content-addressed clips; conflicting writes reject."""
    def __init__(self): self._clips={}
    def add(self, clip):
        value=_clip(clip); key=(value['id'],value['version']); digest=object_hash(value)
        require(key not in self._clips or self._clips[key][0]==digest,'ANIMATION_IDENTITY','An immutable clip version already has different content.')
        self._clips[key]=(digest,value); return digest
    def get(self, identifier, version, digest):
        found=self._clips.get((identifier,version))
        require(found is not None and found[0]==digest,'ANIMATION_HASH','Exact clip version/hash is unavailable.')
        return deepcopy(found[1])
    def search(self, text='', tags=()):
        return [dict(id=v['id'],version=v['version'],sha256=h,name=v['name']) for h,v in self._clips.values()
                if text.casefold() in v['name'].casefold() and set(tags)<=set(v['tags'])]
