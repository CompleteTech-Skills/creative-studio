"""Hash-bound manual corrections; geometry and original sources remain immutable."""
from __future__ import annotations
from copy import deepcopy
from rig_contracts import validate_contract
from rig_errors import atomic_json, object_hash, require, safe_path, sha256
from rig_jobs import next_version, save_rig
from rig_models import normalize_anatomy_bindings, _retain_anatomy_occurrences, rig_signature
from rig_skeleton import skeleton_from_heads, validate_skeleton
from rig_ik import validate_controllers
from rig_skinning import edit_weights, copy_weights, smooth_weights, validate_skin


_BONE_FIELDS = ('id', 'role', 'parent', 'head', 'tail', 'deform', 'mirror', 'roll')


def replace_fit(rig, fitted):
    """Refresh the fit-owned records, keeping every independent uncertainty."""
    from rig_contracts import definitions
    manifest = rig.manifest
    normalize_anatomy_bindings(manifest, occurrences=rig._anatomy_occurrences)
    bindings = iter(manifest.get('anatomy_uncertainty_bindings', []))
    independent = [issue for issue in manifest['uncertainties']
                   if issue['code'] != 'RIG_ANATOMY_UNCERTAIN' or next(bindings, None) is None]
    count = len(independent) + len(fitted['uncertainties'])
    limit = definitions()['Rig']['properties']['uncertainties']['maxItems']
    require(count <= limit, 'RIG_LIMIT_UNCERTAINTY',
            'Refit uncertainty count exceeds the rig limit.', asset='uncertainties',
            expected=limit, observed=count)
    bone_ids = {bone['id'] for bone in fitted['skeleton']['bones']}
    manifest['anatomy_uncertainty_bindings'] = [
        None for issue in independent if issue['code'] == 'RIG_ANATOMY_UNCERTAIN'
    ] + [
        object_hash(issue) if issue['element'] in bone_ids else None
        for issue in fitted['uncertainties'] if issue['code'] == 'RIG_ANATOMY_UNCERTAIN'
    ]
    for key in ('skeleton', 'controllers', 'attachments'):
        manifest[key] = fitted[key]
    manifest['archetype'] = fitted['template_id']
    manifest['uncertainties'] = independent + fitted['uncertainties']
    _retain_anatomy_occurrences(rig)


class _BoneEdits:
    """Track identities separately from the public names used in ordered edits."""

    def __init__(self, bones):
        self.original = {bone['id']: bone for bone in bones}
        self.specs = [{key: deepcopy(bone[key]) for key in _BONE_FIELDS} for bone in bones]
        self.active = {name: name for name in self.original}
        self.removed = {}
        self.departures = {}
        self.authored = {}

    def add(self, name):
        displaced = self.removed.get(name)
        self.active[name] = displaced.pop() if displaced else object()

    def depart(self, name, destination, step):
        identity = self.active.pop(name)
        self.departures.setdefault(name, []).append((step, identity))
        if destination is None:
            self.removed.setdefault(name, []).append(identity)
        else:
            self.active[destination] = identity

    def reconcile_references(self, manifest):
        # A reference belongs to the bone which occupied its name when authored.
        # Only the first departure after that edit can identify its owner. A name
        # without a departure stays as authored, including invalid references
        # that the normal validation path must reject.
        final_name = {identity: name for name, identity in self.active.items()}

        def resolve(name, step):
            if not isinstance(name, str):
                return name
            for departure, identity in self.departures.get(name, ()):
                if departure > step:
                    return final_name.get(identity)
            return name

        for bone in self.specs:
            bone['mirror'] = resolve(bone['mirror'], bone.get('mirror_edit', 0))
        controllers = manifest['controllers']
        for kind, items in (('ik', controllers['ik']), ('constraint', controllers['constraints']),
                            ('attachment', manifest['attachments'])):
            kept = []
            for item in items:
                step = self.authored.get((kind, item['id']), 0)
                if kind == 'ik':
                    item['bones'] = [resolve(name, step) for name in item['bones']]
                    if None in item['bones']:
                        continue
                else:
                    for field in ('bone', 'target'):
                        if field in item:
                            item[field] = resolve(item[field], step)
                    if item['bone'] is None or ('target' in item and item['target'] is None):
                        continue
                kept.append(item)
            items[:] = kept

        facial = manifest['facial']
        facial_step = self.authored.get(('facial', None), 0)
        facial['eyes'] = [name for eye in facial['eyes']
                          if (name := resolve(eye, facial_step)) is not None]
        if 'eyelids' in facial:
            kept = []
            for binding in facial['eyelids']:
                name = resolve(binding['eye'], facial_step)
                if name is not None:
                    binding['eye'] = name
                    kept.append(binding)
            facial['eyelids'] = kept
        for key, expression in facial['expressions'].items():
            step = max(facial_step, self.authored.get(('expression', key), 0))
            expression['bones'] = {name: pose for bone, pose in expression['bones'].items()
                                   if (name := resolve(bone, step)) is not None}

    def reconcile_evidence(self, skeleton, manifest, correction):
        final_name = {identity: name for name, identity in self.active.items()}
        corrected = set()
        for bone in skeleton['bones']:
            name = bone['id']
            identity = self.active[name]
            original = self.original.get(identity)
            parent_identity = self.active.get(bone['parent'])
            if (original is None or parent_identity != original['parent'] or
                    any(bone[field] != original[field] for field in ('head', 'tail', 'roll'))):
                bone['evidence'] = dict(source='human-landmark', confidence=1.,
                    reference='Correction by ' + correction['author'] + ': ' + correction['reason'])
                corrected.add(identity)
            else:
                bone['evidence'] = original['evidence']
        bindings = iter(manifest.get('anatomy_uncertainty_bindings', []))
        kept, kept_bindings = [], []
        for issue in manifest['uncertainties']:
            if issue['code'] == 'RIG_ANATOMY_UNCERTAIN':
                binding = next(bindings, None)
                identity = issue['element']
                if binding is None or identity not in self.original or binding != object_hash(issue):
                    kept_bindings.append(None)
                else:
                    if identity not in final_name:
                        continue
                    issue = dict(issue, element=final_name[identity])
                    if identity in corrected:
                        issue['resolution'] = 'corrected'
                    kept_bindings.append(object_hash(issue))
            kept.append(issue)
        manifest['uncertainties'] = kept
        manifest['anatomy_uncertainty_bindings'] = kept_bindings


def validate_correction_binding(rig, correction):
    """Validate the bounded original envelope before resolving execution inputs."""
    if isinstance(correction, dict) and isinstance(correction.get('edits'), list):
        from rig_contracts import definitions
        limit=definitions()['RigCorrection']['properties']['edits']['maxItems']
        require(len(correction['edits']) <= limit, 'RIG_SCHEMA',
                'Correction exceeds the edit limit.', stage='RigCorrection',
                asset='edits', expected=limit, observed=len(correction['edits']))
    validate_contract(correction, 'RigCorrection')
    require(correction['rig_id']==rig.manifest['id'] and correction['rig_version']==rig.manifest['version']
            and correction['expected_sha256']==rig_signature(rig.manifest),
            'RIG_STALE_CORRECTION', 'Correction targets a different rig revision; rebase it against the current rig.')


def apply_rig_corrections(rig, correction, *, persist=True, resolve_template=None):
    validate_correction_binding(rig, correction)
    result=deepcopy(rig);skeleton_changed=False;invalidated=[]
    normalize_anatomy_bindings(result.manifest, occurrences=result._anatomy_occurrences)
    _retain_anatomy_occurrences(result)
    bones = _BoneEdits(result.skeleton['bones'] if result.manifest['skeleton'] else [])
    for step, edit in enumerate(correction['edits'], start=1):
        operation,target,value=edit['operation'],edit['target'],deepcopy(edit['value'])
        index={b['id']:b for b in bones.specs}
        if operation in ('bone-add','bone-update','bone-remove'):
            if operation=='bone-add':
                require(target not in index and isinstance(value,dict), 'RIG_BONE_EDIT', 'New bone ID must be unique.')
                require(set(value) <= set(_BONE_FIELDS) and
                        {'role','parent','head','tail','deform'} <= set(value), 'RIG_BONE_EDIT', 'Provide bone role, parent, head, tail and deform flag.')
                value.update(id=target);value.setdefault('mirror',None);value.setdefault('roll',0.)
                bones.add(target)
                value['mirror_edit'] = step
                bones.specs.append(value)
            else:
                require(target in index, 'RIG_BONE_EDIT', 'Bone does not exist: '+target)
                if operation=='bone-remove':
                    require(not any(b['parent']==target for b in bones.specs), 'RIG_BONE_CHILDREN', 'Reparent or remove children before deleting a bone.')
                    bones.depart(target, None, step)
                    bones.specs.remove(index[target])
                else:
                    require(isinstance(value,dict) and set(value) <= set(_BONE_FIELDS),
                            'RIG_BONE_EDIT', 'Unsupported bone correction field.')
                    renamed=value.get('id',target)
                    require(renamed==target or renamed not in index, 'RIG_BONE_EDIT', 'Renamed bone ID already exists.')
                    index[target].update(value)
                    if 'mirror' in value:
                        index[target]['mirror_edit'] = step
                    if renamed!=target:
                        bones.depart(target, renamed, step)
                        for bone in bones.specs:
                            if bone['parent'] == target:
                                bone['parent'] = renamed
            skeleton_changed=True
        elif operation=='weights':
            require(not skeleton_changed, 'RIG_CORRECTION_ORDER', 'Regenerate binding after skeletal edits before editing weights.')
            require(target in result.weights and isinstance(value,dict) and set(value) <= {'assignments','copy','smooth','mirror','lock'},
                    'RIG_WEIGHT_EDIT', 'Weight correction needs a bound mesh and supported actions.')
            mesh=result.geometry.by_id()[target];skin=result.weights[target]
            influence_limit=min(result.manifest['limits']['influences'],skin.indices.shape[1])
            if 'assignments' in value:skin=edit_weights(skin,{int(k):v for k,v in value['assignments'].items()},max_influences=influence_limit,lock=value.get('lock',True))
            if 'copy' in value:skin=copy_weights(skin,{int(k):v for k,v in value['copy'].items()},lock=value.get('lock',True))
            if 'smooth' in value:
                require(isinstance(value['smooth'],dict) and set(value['smooth']) <= {'iterations','strength'}, 'RIG_WEIGHT_EDIT', 'Unsupported smoothing option.')
                skin=smooth_weights(mesh,skin,**value['smooth'])
            if 'mirror' in value:
                mirror=value['mirror'];require(isinstance(mirror,dict) and set(mirror)=={'pairs','approved'} and mirror['approved'] is True,
                                              'RIG_MIRROR_APPROVAL','Use explicit vertex pairs on a reviewed symmetric mesh.')
                weight_bones={b['id']:b for b in result.skeleton['bones']};assignments={}
                for dst,src in mirror['pairs'].items():
                    require(0 <= int(src) < len(skin.weights), 'RIG_VERTEX_INDEX','Mirror source is out of range.')
                    weights={}
                    for index_,weight in zip(skin.indices[int(src)],skin.weights[int(src)]):
                        if weight<=0:continue
                        source_id=skin.joints[index_];mapped=weight_bones[source_id].get('mirror') or source_id
                        weights[mapped]=weights.get(mapped,0.)+float(weight)
                    assignments[int(dst)]=weights
                skin=edit_weights(skin,assignments,max_influences=influence_limit,lock=value.get('lock',True))
            validate_skin(skin,mesh,result.skeleton,result.manifest['limits']);result.weights[target]=skin
        elif operation in ('constraint','ik','attachment'):
            kind={'constraint':'JointConstraint','ik':'IKChain','attachment':'AttachmentPoint'}[operation]
            items=result.manifest['attachments'] if operation=='attachment' else result.manifest['controllers']['constraints' if operation=='constraint' else 'ik']
            items[:]=[x for x in items if x['id']!=target]
            if value is not None:
                require(isinstance(value,dict) and value.get('id')==target,'RIG_EDIT_ID','Edited object ID must match target.')
                items.append(validate_contract(value,kind))
                bones.authored[operation, target] = step
        elif operation=='facial':
            result.manifest['facial']=validate_contract(value,'FacialController')
            bones.authored['facial', None] = step
        elif operation=='expression':
            expressions=result.manifest['facial']['expressions']
            if value is None:expressions.pop(target,None)
            else:
                expressions[target]=value
                bones.authored['expression', target] = step
            validate_contract(result.manifest['facial'],'FacialController')
        elif operation=='morph':
            from rig_facial import create_morph_target,edit_morph_target
            from rig_models import validate_geometry
            require(target in result.geometry.by_id() and isinstance(value,dict) and set(value)<={'name','action','deltas','positions','assignments'},
                    'RIG_MORPH_EDIT','Morph correction needs a mesh and explicit action/name/coordinates.')
            mesh=result.geometry.by_id()[target];name=value['name'];action=value.get('action','edit')
            if action=='remove':
                require(name in mesh.morphs,'RIG_MORPH_MISSING','Morph target does not exist.');mesh.morphs.pop(name);mesh.morph_defaults.pop(name,None)
            elif action=='create':mesh=create_morph_target(mesh,name,deltas=value.get('deltas'),positions=value.get('positions'))
            elif action=='edit':mesh=edit_morph_target(mesh,name,{int(k):v for k,v in value['assignments'].items()})
            else:require(False,'RIG_MORPH_EDIT','Choose create, edit or remove.')
            result.geometry.meshes=[mesh if m.id==target else m for m in result.geometry.meshes];validate_geometry(result.geometry,result.manifest['limits'])
            result.manifest['facial']['morph_targets']={m.id:list(m.morphs) for m in result.geometry.meshes if m.morphs}
            if action in ('create','remove'):
                stale=[key for key,clip in result.clips.items() if any(t['target']==target and t['path']=='weights' for t in clip['tracks'])]
                for key in stale:result.clips.pop(key)
                invalidated.extend('clip:'+key for key in stale);result.manifest['state_mapping']={}
            if action=='remove':
                if 'eyelids' in result.manifest['facial']:
                    result.manifest['facial']['eyelids']=[binding for binding in result.manifest['facial']['eyelids']
                        if (binding['mesh'],binding['morph'])!=(target,name)]
                for expression in result.manifest['facial']['expressions'].values():
                    expression['morphs'].pop(target+'::'+name,None)
                    if not any(name in m.morphs for m in result.geometry.meshes):expression['morphs'].pop(name,None)
        elif operation=='template':
            from rig_templates import generate_skeleton
            require(isinstance(value,dict) and set(value) <= {'template','landmarks'} and 'template' in value,'RIG_TEMPLATE_EDIT','Specify a template and optional landmarks.')
            template=value['template'] if resolve_template is None else resolve_template(value['template'])
            fitted=generate_skeleton(result.geometry,template,landmarks=value.get('landmarks'),character_id=result.manifest['character']['id'])
            bones.reconcile_references(result.manifest)
            replace_fit(result, fitted)
            bones = _BoneEdits(fitted['skeleton']['bones'])
            skeleton_changed = True
    if skeleton_changed:
        bones.reconcile_references(result.manifest)
        skeleton=skeleton_from_heads(bones.specs,identifier=result.skeleton['id'],version=next_version(result.skeleton['version']))
        bones.reconcile_evidence(skeleton, result.manifest, correction)
        result.manifest['skeleton']=skeleton
        _retain_anatomy_occurrences(result)
        invalidated=['skins','animations','state_mapping','review','validation'];result.weights={};result.clips={};result.manifest['state_mapping']={}
    validate_skeleton(result.skeleton,result.manifest['limits'])
    validate_controllers(result.skeleton,result.manifest['controllers'])
    bone_ids={b['id'] for b in result.skeleton['bones']}
    require(all(s['bone'] in bone_ids for s in result.manifest['attachments']),'RIG_ATTACHMENT_REFERENCE','Attachment bone is missing.')
    require(set(result.manifest['facial']['eyes']) <= bone_ids,'RIG_EYE_REFERENCE','Eye control bone is missing.')
    from rig_facial import validate_eyelid_bindings
    validate_eyelid_bindings(result)
    if persist:
        digest=object_hash(correction);atomic_json(safe_path(result.root,'rig/corrections/'+digest+'.json'),correction)
        save_rig(result,operation='manual-correction')
    return result,dict(invalidated=invalidated,rig_sha256=rig_signature(result.manifest),source_preserved=True)
