"""Pure arbitrary-frame native rig clip evaluation for Animation3D.

This stage composes independent disjoint channels. Layer mixing, state machines,
constraints, IK and scene/camera/light evaluation retain their existing owners.
"""
from copy import deepcopy
from pathlib import Path

from animation3d_contracts import KINDS, validate_contract, target_key
from animation3d_registry import TargetRegistry, sample_track
from animation3d_time import frame_position,rate
from rig_errors import require, sha256, object_hash
from rig_skeleton import neutral_pose, evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation
from rig_contracts import validate_contract as validate_rig_contract


class _FrameRegistry(TargetRegistry):
    """Private entries admitted once by the owning evaluator for this call."""
    def resolve(self,target):
        entry=self._entries.get(target_key(target))
        require(entry is not None,'ANIMATION_TARGET','Target is not registered on the pinned native rig.')
        return deepcopy(entry)


class RigClipEvaluator:
    """Pinned native geometry and skeleton; evaluation creates fresh pose state.

    Persisted dependencies are still checked by TargetRegistry at every sample.
    Returned nested lists and dicts are detached from both sources and caches.
    """
    def __init__(self, root, actor_id):
        from animation3d_assets import verify_native
        root=Path(root)
        self._registry=TargetRegistry.from_rig(root,actor_id)
        entries=self._registry.document['entries']
        require(bool(entries),'ANIMATION_TARGET','Native rig has no registered channels.')
        source=entries[0]['source']
        _,rig=verify_native(root,source)
        validate_skeleton(rig.skeleton,rig.manifest['limits'])
        require(all(e['source']==source for e in entries),
                'ANIMATION_HASH','Native rig channels must bind one admitted manifest closure.')
        self._rig=deepcopy(rig); self._actor_id=actor_id
        self._dependency=source['sha256']

    def validate_clip(self, clip):
        """Validate all registered targets, including disabled channels, and
        exercise the actual native deformation at the two terminal boundaries.
        """
        validate_contract(clip)
        require(clip['kind']==KINDS['clip'],'ANIMATION_KIND','Expected a clip document.')
        registry=_FrameRegistry(self._registry.root,self._registry.document)
        for track in clip['tracks']:
            registry.resolve(track['target'])
        for time in ({'frame':0},{'frame':clip['duration_frames']}):
            self.evaluate([clip],time)
        return object_hash(clip)

    def evaluate(self, clips, time, *, pose=None):
        """Evaluate disjoint clip channels at one shared exact source timestamp.

        Compose independently clocked body/face clips through evaluate_sources.
        A supplied canonical pose is an explicit initial condition, not playback
        state. Unsupported executable clip features fail before any result.
        """
        return self.evaluate_sources([(clip,time) for clip in clips],pose=pose)

    def evaluate_sources(self, sources, *, pose=None):
        from itertools import islice
        sources=list(islice(iter(sources),65))
        require(len(sources)<=64,'ANIMATION_LIMIT','At most 64 independent source clips may be composed.')
        pose=deepcopy(pose) if pose is not None else neutral_pose(self._rig.skeleton)
        validate_rig_contract(pose,'Pose')
        initial_pose=object_hash(pose)
        require(pose['rig_version']==self._rig.skeleton['version'],'ANIMATION_IDENTITY','Pose is for a different rig version.')
        # All entries from from_rig bind the same immutable manifest closure.
        # Delegate source admission once per frame, then sample detached entries
        # against the pinned geometry instead of rereading the rig per channel.
        entries=self._registry.document['entries']
        require(bool(entries),'ANIMATION_TARGET','Native rig has no registered channels.')
        self._registry.resolve(entries[0]['target'])
        registry=_FrameRegistry(self._registry.root,self._registry.document)
        channels=set(); dependencies=[]
        source_keys=0
        require(self._rig.geometry.vertex_count*512+len(self._rig.skeleton['bones'])*2048<=self._rig.manifest['limits']['memory_bytes'],
                'ANIMATION_LIMIT','Detached evaluated geometry exceeds the conservative working memory budget.')
        for clip,time in sources:
            validate_contract(clip)
            require(clip['kind']==KINDS['clip'],'ANIMATION_KIND','Expected a clip document.')
            require(frame_position(time)<=clip['duration_frames'],'ANIMATION_TIME','Clip frame exceeds terminal boundary.')
            require(clip['duration_frames']/rate(clip['fps'])<=self._rig.manifest['limits']['duration'],
                    'ANIMATION_LIMIT','Clip duration exceeds the native evaluation budget.')
            source_keys+=sum(len(track['keys']) for track in clip['tracks'])
            require(source_keys<=self._rig.manifest['limits']['keyframes'],'ANIMATION_LIMIT','Concurrent clips exceed the native key budget.')
            require(not clip['root_motion']['enabled'] and not clip.get('constraints') and not clip.get('procedural_modifiers'),
                    'ANIMATION_UNSUPPORTED','Root extraction, constraints and procedural motion require their execution owners.')
            if 'rig_binding' in clip:
                source=clip['rig_binding']
                require(source['sha256']==self._dependency and source['id']==self._rig.manifest['id'] and source['version']==self._rig.manifest['version'],
                        'ANIMATION_IDENTITY','Clip names a different immutable rig.')
            if 'skeleton_signature' in clip:
                require(clip['skeleton_signature']==object_hash(self._rig.skeleton),'ANIMATION_IDENTITY','Clip skeleton signature differs from native rig.')
            dependencies.append(dict(clip=object_hash(clip),time=deepcopy(time)))
            for track in clip['tracks']:
                target=track['target']
                require(target['kind'] in ('bone','morph') and target['actor_id']==self._actor_id,
                        'ANIMATION_TARGET','Rig evaluation accepts only its registered actor bone/morph channels.')
                require(track.get('coordinate_space','local') in ('local','parent'),
                        'ANIMATION_UNSUPPORTED','World-space conversion requires an explicit scene parent adapter.')
                if not track['enabled']: continue
                channel=target_key(target)
                require(channel not in channels,'ANIMATION_DUPLICATE','Concurrent channels conflict; delegate blending to the mixer.')
                channels.add(channel)
                value=sample_track(track,time,registry)
                if target['kind']=='bone':
                    pose['bones'].setdefault(target['bone_id'],{})[target['property']]=value
                else:
                    pose['morphs'].setdefault(target['mesh_id'],{})[target['morph_id']]=value
        # Check initial morph inputs through the same native deformation owner.
        world=evaluate_fk(self._rig.skeleton,pose)
        geometry=evaluate_deformation(self._rig,pose)
        skins={mesh: [(world[joint]@skin.inverse_bind[i]).tolist() for i,joint in enumerate(skin.joints)]
               for mesh,skin in self._rig.weights.items()}
        return dict(pose=deepcopy(pose),world_matrices={k:v.tolist() for k,v in world.items()},
                    skin_matrices=skins,geometry={k:{field:v.tolist() for field,v in mesh.items()} for k,mesh in geometry.items()},
                    dependency_signature=object_hash(dict(rig=self._dependency,clips=dependencies,initial_pose=initial_pose)),
                    scope='native skeletal and morph geometry; no layer/state/constraint/scene evaluation',
                    contacts_solved=False)


class PoseLibrary:
    """Bounded named native poses; cache policy covers stored pose JSON estimates.

    The caller supplies an already admitted native rig; its independent geometry
    memory and process RSS are outside the cached-pose allowance."""
    def __init__(self, rig, *, max_entries=4096, max_pose_bytes=64*1024*1024):
        require(type(max_entries) is int and 1<=max_entries<=4096,
                'ANIMATION_LIMIT','Pose library entry policy must be within 1..4096.')
        require(type(max_pose_bytes) is int and 1<=max_pose_bytes<=64*1024*1024,
                'ANIMATION_LIMIT','Pose library cached-pose policy must be within 1..64 MiB.')
        self._rig=deepcopy(rig); self._poses={}
        self._max_entries=max_entries; self._max_pose_bytes=max_pose_bytes; self._memory=0

    def add(self, pose):
        import hashlib,json
        from rig_load_limits import _JSON_FACTOR
        from animation3d_poses import validate_pose
        validate_rig_contract(pose,'Pose')
        digest=hashlib.sha256(); encoded=0
        for part in json.JSONEncoder(sort_keys=True,separators=(',',':'),allow_nan=False).iterencode(pose):
            encoded+=len(part)
            require(encoded*_JSON_FACTOR<=self._max_pose_bytes,
                    'ANIMATION_LIMIT','Pose library entry exceeds its conservative JSON object allowance.')
            digest.update(part.encode('utf-8'))
        digest=digest.hexdigest(); key=pose['id']; found=self._poses.get(key)
        require(found is None or found[0]==digest,'ANIMATION_IDENTITY','Named pose already has different immutable content.')
        if found is not None: return digest
        estimate=encoded*_JSON_FACTOR
        require(len(self._poses)<self._max_entries and self._memory+estimate<=self._max_pose_bytes,
                'ANIMATION_LIMIT','Pose library capacity exceeded; existing entries remain unchanged.')
        value=validate_pose(self._rig,pose)
        self._poses[key]=(digest,value); self._memory+=estimate
        return digest

    def get(self, identifier, digest):
        found=self._poses.get(identifier)
        require(found is not None and found[0]==digest,'ANIMATION_HASH','Named pose hash differs or is unavailable.')
        return deepcopy(found[1])

    def duplicate(self, identifier, digest, new_identifier):
        from animation3d_poses import duplicate_pose
        return self.add(duplicate_pose(self._rig,self.get(identifier,digest),new_identifier))

    def load(self, path):
        from animation3d_poses import load_pose
        return self.add(load_pose(self._rig,path))

    def save(self, identifier, digest, path):
        from animation3d_poses import save_pose
        save_pose(self._rig,path,self.get(identifier,digest))

    def compare(self, first, first_digest, second, second_digest):
        from animation3d_poses import compare_poses
        return compare_poses(self._rig,self.get(first,first_digest),self.get(second,second_digest))
