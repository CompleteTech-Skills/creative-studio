"""Dependency reports derived from the canonical rig and selected catalog state.

The native map_states owner supplies validated executable clips and controllers.
This module neither loads a second catalog nor owns playback, persistence or rigs.
"""
from copy import deepcopy
from rig_errors import object_hash, require


def state_discovery(rig, state, requested, executable, capabilities, archetype, approved_props):
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
        # Native deformation reads defaults even without a weights channel,
        # including zero defaults. Keep these dependencies separate from tracks.
        required_morphs={mesh+'::'+morph for mesh,names in morph_channels.items() for morph in names}
        for mesh in meshes.values():
            require(set(mesh.morph_defaults) <= set(mesh.morphs), 'RIG_MORPH_REFERENCE',
                    'Default morph target is unavailable.', asset=mesh.id)
            required_morphs.update(mesh.id+'::'+morph for morph in mesh.morph_defaults)
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
            required_morph_targets=sorted(required_morphs),
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


def asset_provenance(rig, clip_ids):
    """Copy exact native source and selected executable clip provenance."""
    return dict(rig=dict(id=rig.manifest['id'],version=rig.manifest['version'],
                        manifest_sha256=object_hash(rig.manifest),source=deepcopy(rig.manifest['source']),
                        provenance=deepcopy(rig.manifest['provenance'])),
                clips={name:dict(id=name,version=rig.clips[name]['version'],sha256=object_hash(rig.clips[name]),
                                 provenance=deepcopy(rig.clips[name]['provenance'])) for name in clip_ids})
