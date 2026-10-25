"""Exact-byte target registry using the native rig loader as target authority.

The document can describe other owner domains, but this adapter only resolves
actual rig bones/morphs. Camera/light/material/scene owners are never guessed.
"""
from copy import deepcopy
from pathlib import Path

from animation3d_contracts import KINDS, target_key, validate_contract
from animation3d_time import frame_position
from rig_errors import RigError, require, safe_path, sha256


def _load_rig():
    try:
        from rig_models import load_rig
    except ImportError as exc:
        raise RigError('ANIMATION_DEPENDENCY',
            'Install requirements-rig.txt in the optional 3D environment before resolving native targets.',
            stage='animation3d') from exc
    return load_rig


class TargetRegistry:
    def __init__(self, root, document):
        validate_contract(document)
        require(document['kind']==KINDS['registry'],'ANIMATION_REGISTRY','Expected target registry.')
        self.root=Path(root).resolve()
        self.document=deepcopy(document)
        self._entries={target_key(e['target']):deepcopy(e) for e in document['entries']}

    @classmethod
    def from_rig(cls, root, actor_id):
        load_rig=_load_rig()
        root=Path(root).resolve(); rig=load_rig(root)
        from rig_skeleton import validate_skeleton
        validate_skeleton(rig.skeleton,rig.manifest['limits'])
        source=dict(id=rig.manifest['id'],version=rig.manifest['version'],path='rig.json',sha256=sha256(root/'rig.json'),kind='rig')
        entries=[]
        for bone in rig.skeleton['bones']:
            for prop in ('translation','rotation','scale'):
                entries.append(dict(target=dict(kind='bone',actor_id=actor_id,bone_id=bone['id'],property=prop),owner='rig',source=source))
        for mesh in rig.geometry.meshes:
            for morph in mesh.morphs:
                entries.append(dict(target=dict(kind='morph',actor_id=actor_id,mesh_id=mesh.id,morph_id=morph,property='weight'),owner='rig',source=source,minimum=0,maximum=1))
        return cls(root,dict(schema_version=1,kind=KINDS['registry'],id=actor_id+'-targets',version='1.0.0',entries=entries))

    def resolve(self, target):
        entry=self._entries.get(target_key(target))
        require(entry is not None,'ANIMATION_TARGET','Target is not registered.',stage='animation3d')
        require(entry['owner']=='rig' and entry['source']['kind']=='rig','ANIMATION_OWNER',
            'This owner needs its explicit validated adapter.',stage='animation3d')
        load_rig=_load_rig()
        source=entry['source']; path=safe_path(self.root,source['path'],exists=True)
        require(path.name=='rig.json','ANIMATION_OWNER','Rig source must be a native rig.json.',stage='animation3d')
        require(path.stat().st_size<=32*1024**2 and sha256(path)==source['sha256'],'ANIMATION_HASH','Rig source hash changed.',stage='animation3d')
        rig=load_rig(path.parent)
        from rig_skeleton import validate_skeleton
        validate_skeleton(rig.skeleton,rig.manifest['limits'])
        require(sha256(path)==source['sha256'] and rig.manifest['id']==source['id'] and rig.manifest['version']==source['version'],
            'ANIMATION_IDENTITY','Loaded rig identity/version differs from registry.',stage='animation3d')
        if target['kind']=='bone':
            require(target['bone_id'] in {b['id'] for b in rig.skeleton['bones']},'ANIMATION_TARGET','Bone is absent from rig.',stage='animation3d')
        elif target['kind']=='morph':
            mesh=rig.geometry.by_id().get(target['mesh_id'])
            require(mesh is not None and target['morph_id'] in mesh.morphs,'ANIMATION_TARGET','Morph is absent from rig.',stage='animation3d')
        else:
            require(False,'ANIMATION_OWNER','Rig adapter resolves only bone and morph properties.',stage='animation3d')
        return deepcopy(entry)

    def validate_track(self, track, duration_frames=None):
        from animation3d_contracts import validate_track
        validate_contract(dict(schema_version=1,kind=KINDS['track'],value=track))
        validate_track(track,duration_frames)
        entry=self.resolve(track['target'])
        if 'minimum' in entry:
            for value in [track['default_value']]+[key['value'] for key in track['keys']]:
                require(isinstance(value,(int,float)) and entry['minimum']<=value<=entry['maximum'],
                    'ANIMATION_RANGE','Track value exceeds registered target bounds.',stage='animation3d')
        return deepcopy(track)


def sample_track(track, time, registry):
    """Sample a declared target through the existing rig track mathematics.

    Returned values are not a complete pose/scene, solved contact, or rendered
    frame. Caller must delegate those operations to their native owners.
    """
    try:
        import numpy as np
        from rig_animation import evaluate_track
    except ImportError as exc:
        raise RigError('ANIMATION_DEPENDENCY',
            'Install requirements-rig.txt in the optional 3D environment before sampling native targets.',
            stage='animation3d') from exc
    registry.validate_track(track)
    entry=registry.resolve(track['target'])
    position=frame_position(time)
    keys=track['keys']
    if not track['enabled'] or not keys:
        return deepcopy(track['default_value'])
    positions=[frame_position(key['time']) for key in keys]
    if position<=positions[0]: return deepcopy(keys[0]['value'])
    if position>=positions[-1]: return deepcopy(keys[-1]['value'])
    right=next(i for i,p in enumerate(positions) if p>position); left=right-1
    a,b=keys[left],keys[right]
    mode=a.get('interpolation','step' if track['value_type']=='boolean' else 'quaternion-slerp' if track['value_type']=='quaternion' else 'linear')
    if mode=='step': return deepcopy(a['value'])
    # Convert only the bounded segment duration/fraction, not a large absolute
    # frame number. Native Hermite tangents are value/frame on this boundary.
    duration=float(positions[right]-positions[left]); offset=float(position-positions[left])
    wrap=lambda value: value if isinstance(value,list) else [value]
    native=dict(times=[0,duration],values=[wrap(a['value']),wrap(b['value'])],
        path='rotation' if track['value_type']=='quaternion' else 'translation',
        interpolation='CUBICSPLINE' if mode=='hermite' else 'LINEAR')
    if mode=='hermite':
        native.update(in_tangents=[[0]*len(wrap(a['value'])),wrap(b['in_tangent'])],
                      out_tangents=[wrap(a['out_tangent']),[0]*len(wrap(b['value']))])
    result=evaluate_track(native,offset)
    require(np.isfinite(result).all(),'ANIMATION_NUMERICAL','Interpolation produced nonfinite output.',stage='animation3d')
    value=result.tolist() if isinstance(a['value'],list) else float(result[0])
    # Overshoot may violate scale/morph domain even when endpoints are valid.
    from animation3d_contracts import _check_value
    _check_value(value,track['target']['kind'],track['target']['property'])
    if 'minimum' in entry:
        require(entry['minimum']<=value<=entry['maximum'],'ANIMATION_RANGE',
                'Interpolated value exceeds registered target bounds.',stage='animation3d')
    return value
