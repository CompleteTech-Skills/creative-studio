"""Selective, evidence-aware bridge to the unchanged 416-state catalog."""
from collections import Counter
from copy import deepcopy
import numpy as np
from rig_animation import validate_animation
from rig_errors import require
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


def map_states(rig,*,recipes=None,selected=None,pack=None,approved_props=()):
    library=load_library();recipes=DEFAULT_RECIPES if recipes is None else recipes
    resolved={library.resolve(name):([clips] if isinstance(clips,str) else clips) for name,clips in recipes.items()}
    names=list(library.states) if selected is None and pack is None else list(dict.fromkeys(
        [library.resolve(s) for s in (selected or [])]+(library.expand_pack(pack) if pack else [])))
    capabilities=rig_capabilities(rig);archetype=rig.manifest['archetype']
    if archetype not in library.archetypes:archetype='generic'
    mapping={};compiled={}
    for name in names:
        reasons=library.incompatibilities(name,archetype,capabilities,approved_props)
        clips=resolved.get(name,[])
        if reasons:status='requires-rig-capability';reason='; '.join(reasons);clips=[]
        elif not clips or any(c not in rig.clips for c in clips):
            status='requires-animation';reason='No complete executable 3D clip recipe is available for this state.';clips=[]
        else:
            for clip in clips:validate_animation(rig,rig.clips[clip])
            status='supported' if rig.manifest['review']['status']=='approved' else 'needs-review'
            reason='Executable 3D channels are available; state-specific semantic and visual review is required.'
            if status=='supported':reason='Executable recipe on this visually reviewed rig; semantic suitability remains specific to the chosen clips.'
            state=library.states[name]
            compiled[name]=dict(clips=clips,priority=state['priority'],interrupt=state['interrupt'],repeat=state['repeat'],
                legal_transitions=state['legal_transitions'],completion=state['completion'],recovery=state['recovery'],
                reduced_motion=deepcopy(state['reduced_motion']),cancel_state='static-neutral',
                anatomical_adaptation='Uses only existing weighted anatomy; no invented body parts.',semantic_review=state['semantic_review'])
        mapping[name]=dict(status=status,clips=clips,reason=reason)
    return dict(schema_version=1,catalog_version=library.version,catalog_sha256=library.sha256,total_catalog_states=len(library.states),
                selected_states=len(names),capabilities=capabilities,mapping=mapping,recipes=compiled,
                counts=dict(Counter(v['status'] for v in mapping.values())),all_416_animated=False)
