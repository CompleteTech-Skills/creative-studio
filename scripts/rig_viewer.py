"""Portable offline Three.js rig inspection bundle; serve only on localhost."""
from pathlib import Path
import shutil
from rig_errors import atomic_json, require
from rig_gltf import export_gltf
from rig_models import rig_signature


def create_review_ui(rig,destination,*,clip_aliases=None,reduced_motion_clip=None):
    aliases={} if clip_aliases is None else clip_aliases
    require(isinstance(aliases,dict) and len(aliases)<=256 and all(
        isinstance(name,str) and 0<len(name)<=128 and isinstance(target,str) and target in rig.clips
        and (name not in rig.clips or name==target) for name,target in aliases.items()),
        'RIG_VIEWER_CLIP_ALIAS','Clip aliases must bind exact current native clip IDs without shadowing them.')
    try:
        for name in aliases:name.encode('utf-8')
    except UnicodeError:
        require(False,'RIG_VIEWER_CLIP_ALIAS','Clip labels must use valid Unicode scalar values.')
    reduced=aliases.get('neutral','neutral' if 'neutral' in rig.clips else None) if reduced_motion_clip is None else reduced_motion_clip
    require(reduced is None or isinstance(reduced,str) and reduced in rig.clips,
            'RIG_VIEWER_REDUCED_CLIP','Reduced-motion clip must belong to the current rig; absent clips use its rest pose.')
    pinned={a['id']:a for a in rig.manifest['animations']}
    for target in set(aliases.values())|({reduced} if reduced is not None else set()):
        clip=rig.clips[target]
        require(target in pinned and clip['id']==target and clip['version']==pinned[target]['version']
                and clip['rig_id']==rig.manifest['id'] and clip['skeleton_version']==rig.skeleton['version'],
                'RIG_VIEWER_CLIP_VERSION','Clip labels must refer to the current rig and pinned clip/skeleton versions.')
    require(all(w.indices.shape[1]<=4 for w in rig.weights.values()),'RIG_VIEWER_INFLUENCES',
            'The pinned Three.js viewer supports four influences; use an eight-influence-capable external consumer for that profile.')
    destination=Path(destination).absolute();require(not destination.exists(),'RIG_VIEWER_EXISTS','Use a new review destination.')
    assets=Path(__file__).resolve().parents[1]/'assets/rig-viewer';shutil.copytree(assets,destination)
    export_gltf(rig,destination/'character.glb')
    atomic_json(destination/'review.json',dict(schema_version=1,rig_sha256=rig_signature(rig.manifest),rig=rig.manifest,clips=rig.clips,
                                             clip_aliases=dict(aliases),reduced_motion_clip=reduced))
    return dict(path=str(destination/'index.html'),serve=f'python -m http.server 8765 --bind 127.0.0.1 --directory {destination}',
                url='http://127.0.0.1:8765/',offline=True,review_status=rig.manifest['review']['status'])
