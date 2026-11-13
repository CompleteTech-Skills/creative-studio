"""Selective, evidence-aware bridge to the unchanged 416-state catalog."""
from collections import Counter
from copy import deepcopy
import math
import numpy as np
from rig_animation import validate_animation
from rig_errors import object_hash, require
from rig_math import qangle
from state_catalog import load_library

DEFAULT_RECIPES={'static-neutral':'neutral','standing':'neutral','walking-forward':'walk','waving':'wave',
                 'crouching':'crouch','blinking':'blink-expression','sitting':'sit','tail-wagging':'tail-wag',
                 'looking-around':'head-turn','wing-fluttering':'wing-flap','glancing':'gaze'}


def rig_capabilities(rig):
    used={skin.joints[int(j)] for skin in rig.weights.values() for j in set(skin.indices[skin.weights>0].tolist())}
    roles={b['role'] for b in rig.skeleton['bones'] if b['id'] in used}
    capabilities={'body'}
    for prefix,cap in [('head','head'),('eye.','eyes'),('jaw','mouth'),('upper-arm.','arms'),('hand.','hands'),
                       ('thigh.','legs'),('upper-leg.','legs'),('foot.','feet'),('paw.','paws'),('paw.','feet'),
                       ('tail.','tail'),('ear.','ears'),('wing-','wings')]:
        if any(role.startswith(prefix) for role in roles):capabilities.add(cap)
    # A neutral catalog entry is bookkeeping, not an executable facial
    # capability. Only authored controls that can change represented geometry
    # count when admitting states that require an expressive face.
    bones={b['id']:b for b in rig.skeleton['bones']}
    influential=set(used)
    for name in used:
        parent=bones[name]['parent']
        while parent is not None and parent not in influential:
            influential.add(parent)
            parent=bones[parent]['parent']
    expressive=False
    for expression in rig.manifest['facial']['expressions'].values():
        for name,amount in expression['morphs'].items():
            qualifier,morph=(name.split('::',1) if '::' in name else (None,name))
            if any((qualifier is None or mesh.id==qualifier) and morph in mesh.morphs
                    and (amount or mesh.morph_defaults.get(morph,0.))
                    and any(np.any(values) for values in mesh.morphs[morph].values())
                    for mesh in rig.geometry.meshes):
                expressive=True
        for name,transform in expression['bones'].items():
            if name in influential and any(
                    qangle(value,bones[name]['rest'][path]) > 1e-7 if path=='rotation' else
                    not np.allclose(value,bones[name]['rest'][path],rtol=0,atol=1e-12)
                    for path,value in transform.items()):
                expressive=True
    if expressive:capabilities.add('expressive-face')
    if rig.manifest['archetype']=='quadruped':capabilities.add('quadrupedal')
    library=load_library();return sorted(capabilities & set(library.capabilities))



def _state_discovery(rig, state, requested, executable, capabilities, archetype, approved_props):
    """Separate catalog requirements from evidence in a validated executable recipe."""
    available=set(capabilities);anatomy=state['anatomy']
    present=[name for name in dict.fromkeys(requested) if name in rig.clips]
    missing=[name for name in dict.fromkeys(requested) if name not in rig.clips]
    inventory=dict(requested_clips=list(requested),available_clips=present,missing_clips=missing,
                   validated_complete_recipe=bool(executable),required_bones=None,
                   required_morph_targets=None,bone_channels=None,morph_channels=None,
                   contact_chains=None,constraints=None,skinned_joints=None,
                   required_accessories=deepcopy(state['props']['required']),
                   accessory_binding_status='Catalog prop approval only; no automatic socket or ownership binding is inferred.')
    if executable:
        meshes=rig.geometry.by_id();chains={chain['id']:chain for chain in rig.manifest['controllers']['ik']}
        bone_channels=set();morph_channels={};contact_ids=set()
        for name in dict.fromkeys(executable):
            clip=rig.clips[name]
            for track in clip['tracks']:
                if track['path']=='weights':morph_channels[track['target']]=list(meshes[track['target']].morphs)
                else:bone_channels.add((track['target'],track['path']))
            contact_ids.update(contact['chain'] for contact in clip['contacts'])
        skinned={joint for skin in rig.weights.values() for joint in skin.joints}
        required=skinned|{bone for bone,path in bone_channels}
        required.update(bone for name in contact_ids for bone in chains[name]['bones'])
        constraints=rig.manifest['controllers']['constraints']
        required.update(constraint['bone'] for constraint in constraints)
        required.update(constraint['target'] for constraint in constraints
                        if constraint['type'] in ('copy-position','copy-rotation','aim') and 'target' in constraint)
        parents={bone['id']:bone['parent'] for bone in rig.skeleton['bones']}
        for bone in list(required):
            parent=parents[bone]
            while parent is not None and parent not in required:
                required.add(parent);parent=parents[parent]
        inventory.update(required_bones=sorted(required),
            required_morph_targets=sorted(mesh+'::'+morph for mesh,names in morph_channels.items() for morph in names),
            bone_channels=[dict(bone=bone,path=path) for bone,path in sorted(bone_channels)],
            morph_channels=[dict(mesh=mesh,morphs=names) for mesh,names in sorted(morph_channels.items())],
            contact_chains=[dict(id=name,bones=list(chains[name]['bones'])) for name in sorted(contact_ids)],
            constraints=deepcopy(constraints),skinned_joints=sorted(skinned))
    return dict(catalog_requirements=dict(anatomy=deepcopy(anatomy),archetypes=list(state['archetypes']),props=deepcopy(state['props'])),
        compatibility=dict(missing_all=sorted(set(anatomy['requires_all'])-available),
            missing_any=[] if not anatomy['requires_any'] or available.intersection(anatomy['requires_any']) else list(anatomy['requires_any']),
            excluded_present=sorted(set(anatomy['excludes'])&available),
            missing_approved_props=sorted(set(state['props']['required'])-set(approved_props)),
            archetype_applicable=state['archetypes']==['any'] or archetype in state['archetypes']),
        recipe=inventory,provenance_refs=dict(rig=rig.manifest['id'],clips=list(dict.fromkeys(executable))))


def map_states(rig,*,recipes=None,selected=None,pack=None,approved_props=()):
    library=load_library();recipes=DEFAULT_RECIPES if recipes is None else recipes
    resolved={library.resolve(name):([clips] if isinstance(clips,str) else clips) for name,clips in recipes.items()}
    names=list(library.states) if selected is None and pack is None else list(dict.fromkeys(
        [library.resolve(s) for s in (selected or [])]+(library.expand_pack(pack) if pack else [])))
    capabilities=rig_capabilities(rig);archetype=rig.manifest['archetype']
    if archetype not in library.archetypes:archetype='generic'
    mapping={};compiled={};events={};discovery={};controllers_validated=False
    for name in names:
        reasons=library.incompatibilities(name,archetype,capabilities,approved_props)
        requested=resolved.get(name,[])
        clips=requested
        if reasons:status='requires-rig-capability';reason='; '.join(reasons);clips=[]
        elif not clips or any(c not in rig.clips for c in clips):
            status='requires-animation';reason='No complete executable 3D clip recipe is available for this state.';clips=[]
        else:
            if not controllers_validated:
                from rig_ik import validate_controllers
                validate_controllers(rig.skeleton,rig.manifest['controllers']);controllers_validated=True
            for clip in clips:validate_animation(rig,rig.clips[clip])
            status='supported' if rig.manifest['review']['status']=='approved' else 'needs-review'
            reason='Executable 3D channels are available; state-specific semantic and visual review is required.'
            if status=='supported':reason='Executable recipe on this visually reviewed rig; semantic suitability remains specific to the chosen clips.'
            state=library.states[name]
            compiled[name]=dict(clips=clips,priority=state['priority'],interrupt=state['interrupt'],repeat=state['repeat'],
                legal_transitions=state['legal_transitions'],completion=state['completion'],recovery=state['recovery'],
                reduced_motion=deepcopy(state['reduced_motion']),events=deepcopy(state['events']),cancel_state='static-neutral',
                anatomical_adaptation='Uses only existing weighted anatomy; no invented body parts.',semantic_review=state['semantic_review'])
        mapping[name]=dict(status=status,clips=clips,reason=reason)
        discovery[name]=_state_discovery(rig,library.states[name],requested,clips if name in compiled else [],
                                         capabilities,archetype,approved_props)
        if name in compiled:
            for event in dict.fromkeys([*library.states[name]['events'], 'state:'+name,
                                       *('state:'+alias for alias in library.states[name]['aliases'])]):
                require(event not in events or events[event] == name, 'RIG_STATE_EVENT',
                        'Selected catalog event identifies conflicting states.')
                events[event] = name
    clip_ids=sorted({clip for recipe in compiled.values() for clip in recipe['clips']})
    clips=[rig.clips[name] for name in clip_ids]
    samples=sum(max(2,int(math.ceil(clip['duration']*clip['fps']))+1) for clip in clips)
    position_bytes=rig.geometry.vertex_count*3*8
    estimate=dict(scope='one-cycle-per-unique-existing-clip',clip_ids=clip_ids,unique_clips=len(clip_ids),
        duration_seconds=sum(clip['duration'] for clip in clips),frame_samples=samples,
        keyframes=sum(len(track['times']) for clip in clips for track in clip['tracks']),
        keyframe_scalar_values=sum(len(value) for clip in clips for track in clip['tracks'] for value in track['values']),
        float64_position_bytes_per_frame=position_bytes,
        float64_all_sample_position_bytes=position_bytes*samples,model_calls=0,
        encoded_output_bytes=None,wall_time_seconds=None,
        limitations='Position buffers only; excludes normals, textures, pose matrices, renderer memory, encoded output and repeated runtime loops.')
    return dict(schema_version=1,catalog_version=library.version,catalog_sha256=library.sha256,total_catalog_states=len(library.states),
                selected_states=len(names),capabilities=capabilities,mapping=mapping,recipes=compiled,
                counts=dict(Counter(v['status'] for v in mapping.values())),events=events,work_estimate=estimate,discovery=discovery,
                asset_provenance=dict(rig=dict(id=rig.manifest['id'],version=rig.manifest['version'],
                    manifest_sha256=object_hash(rig.manifest),source=deepcopy(rig.manifest['source']),
                    provenance=deepcopy(rig.manifest['provenance'])),
                    clips={name:dict(id=name,version=rig.clips[name]['version'],sha256=object_hash(rig.clips[name]),
                                     provenance=deepcopy(rig.clips[name]['provenance'])) for name in clip_ids}),
                all_416_animated=False)
