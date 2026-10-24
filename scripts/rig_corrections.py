"""Hash-bound manual corrections; geometry and original sources remain immutable."""
from __future__ import annotations
from copy import deepcopy
import numpy as np
from rig_contracts import validate_contract
from rig_errors import atomic_json, object_hash, require, safe_path, sha256
from rig_jobs import next_version, save_rig
from rig_models import rig_signature
from rig_skeleton import skeleton_from_heads, validate_skeleton
from rig_ik import validate_controllers
from rig_skinning import edit_weights, copy_weights, smooth_weights, validate_skin


def apply_rig_corrections(rig, correction, *, persist=True):
    validate_contract(correction, 'RigCorrection')
    require(correction['rig_id']==rig.manifest['id'] and correction['rig_version']==rig.manifest['version']
            and correction['expected_sha256']==rig_signature(rig.manifest),
            'RIG_STALE_CORRECTION', 'Correction targets a different rig revision; rebase it against the current rig.')
    result=deepcopy(rig);skeleton_changed=False;invalidated=[]
    specs=[];evidence={}
    if result.manifest['skeleton']:
        for bone in result.skeleton['bones']:
            specs.append({k:deepcopy(bone[k]) for k in ('id','role','parent','head','tail','deform','mirror','roll')})
            evidence[bone['id']]=deepcopy(bone['evidence'])
    for edit in correction['edits']:
        operation,target,value=edit['operation'],edit['target'],deepcopy(edit['value'])
        index={b['id']:b for b in specs}
        if operation in ('bone-add','bone-update','bone-remove'):
            if operation=='bone-add':
                require(target not in index and isinstance(value,dict), 'RIG_BONE_EDIT', 'New bone ID must be unique.')
                require(set(value) <= {'id','role','parent','head','tail','deform','mirror','roll'} and
                        {'role','parent','head','tail','deform'} <= set(value), 'RIG_BONE_EDIT', 'Provide bone role, parent, head, tail and deform flag.')
                value.update(id=target);value.setdefault('mirror',None);value.setdefault('roll',0.)
                specs.append(value)
            else:
                require(target in index, 'RIG_BONE_EDIT', 'Bone does not exist: '+target)
                if operation=='bone-remove':
                    require(not any(b['parent']==target for b in specs), 'RIG_BONE_CHILDREN', 'Reparent or remove children before deleting a bone.')
                    specs=[b for b in specs if b['id']!=target]
                    for b in specs:
                        if b.get('mirror')==target:b['mirror']=None
                    result.manifest['controllers']['ik']=[c for c in result.manifest['controllers']['ik'] if target not in c['bones']]
                    result.manifest['controllers']['constraints']=[c for c in result.manifest['controllers']['constraints'] if c['bone']!=target and c.get('target')!=target]
                    result.manifest['attachments']=[s for s in result.manifest['attachments'] if s['bone']!=target]
                    result.manifest['facial']['eyes']=[x for x in result.manifest['facial']['eyes'] if x!=target]
                    for expression in result.manifest['facial']['expressions'].values():expression['bones'].pop(target,None)
                else:
                    require(isinstance(value,dict) and set(value) <= {'id','role','parent','head','tail','deform','mirror','roll'},
                            'RIG_BONE_EDIT', 'Unsupported bone correction field.')
                    renamed=value.get('id',target)
                    require(renamed==target or renamed not in index, 'RIG_BONE_EDIT', 'Renamed bone ID already exists.')
                    index[target].update(value)
                    if renamed!=target:
                        for bone in specs:
                            for field in ('parent','mirror'):
                                if bone.get(field)==target:bone[field]=renamed
                        for c in result.manifest['controllers']['ik']:c['bones']=[renamed if x==target else x for x in c['bones']]
                        for c in result.manifest['controllers']['constraints']:
                            for field in ('bone','target'):
                                if c.get(field)==target:c[field]=renamed
                        for s in result.manifest['attachments']:
                            if s['bone']==target:s['bone']=renamed
                        result.manifest['facial']['eyes']=[renamed if x==target else x for x in result.manifest['facial']['eyes']]
                        for expression in result.manifest['facial']['expressions'].values():
                            if target in expression['bones']:expression['bones'][renamed]=expression['bones'].pop(target)
                        target=renamed
            evidence[target]=dict(source='human-landmark',reference='Correction by '+correction['author']+': '+correction['reason'],confidence=1.)
            skeleton_changed=True
        elif operation=='weights':
            require(not skeleton_changed, 'RIG_CORRECTION_ORDER', 'Regenerate binding after skeletal edits before editing weights.')
            require(target in result.weights and isinstance(value,dict) and set(value) <= {'assignments','copy','smooth','mirror','lock'},
                    'RIG_WEIGHT_EDIT', 'Weight correction needs a bound mesh and supported actions.')
            mesh=result.geometry.by_id()[target];skin=result.weights[target]
            if 'assignments' in value:skin=edit_weights(skin,{int(k):v for k,v in value['assignments'].items()},max_influences=result.manifest['limits']['influences'],lock=value.get('lock',True))
            if 'copy' in value:skin=copy_weights(skin,{int(k):int(v) for k,v in value['copy'].items()},lock=value.get('lock',True))
            if 'smooth' in value:
                require(isinstance(value['smooth'],dict) and set(value['smooth']) <= {'iterations','strength'}, 'RIG_WEIGHT_EDIT', 'Unsupported smoothing option.')
                skin=smooth_weights(mesh,skin,**value['smooth'])
            if 'mirror' in value:
                mirror=value['mirror'];require(isinstance(mirror,dict) and set(mirror)=={'pairs','approved'} and mirror['approved'] is True,
                                              'RIG_MIRROR_APPROVAL','Use explicit vertex pairs on a reviewed symmetric mesh.')
                bones={b['id']:b for b in result.skeleton['bones']};assignments={}
                for dst,src in mirror['pairs'].items():
                    require(0 <= int(src) < len(skin.weights), 'RIG_VERTEX_INDEX','Mirror source is out of range.')
                    weights={}
                    for index_,weight in zip(skin.indices[int(src)],skin.weights[int(src)]):
                        if weight<=0:continue
                        source_id=skin.joints[index_];mapped=bones[source_id].get('mirror') or source_id
                        weights[mapped]=weights.get(mapped,0.)+float(weight)
                    assignments[int(dst)]=weights
                skin=edit_weights(skin,assignments,max_influences=result.manifest['limits']['influences'],lock=value.get('lock',True))
            validate_skin(skin,mesh,result.skeleton,result.manifest['limits']);result.weights[target]=skin
        elif operation in ('constraint','ik','attachment'):
            kind={'constraint':'JointConstraint','ik':'IKChain','attachment':'AttachmentPoint'}[operation]
            items=result.manifest['attachments'] if operation=='attachment' else result.manifest['controllers']['constraints' if operation=='constraint' else 'ik']
            items[:]=[x for x in items if x['id']!=target]
            if value is not None:
                require(isinstance(value,dict) and value.get('id')==target,'RIG_EDIT_ID','Edited object ID must match target.')
                items.append(validate_contract(value,kind))
        elif operation=='facial':result.manifest['facial']=validate_contract(value,'FacialController')
        elif operation=='expression':
            expressions=result.manifest['facial']['expressions']
            if value is None:expressions.pop(target,None)
            else:expressions[target]=value
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
                for expression in result.manifest['facial']['expressions'].values():
                    expression['morphs'].pop(target+'::'+name,None)
                    if not any(name in m.morphs for m in result.geometry.meshes):expression['morphs'].pop(name,None)
        elif operation=='template':
            from rig_templates import generate_skeleton
            require(isinstance(value,dict) and set(value) <= {'template','landmarks'} and 'template' in value,'RIG_TEMPLATE_EDIT','Specify a template and optional landmarks.')
            fitted=generate_skeleton(result.geometry,value['template'],landmarks=value.get('landmarks'),character_id=result.manifest['character']['id'])
            for key in ('skeleton','controllers','attachments','uncertainties'):result.manifest[key]=fitted[key]
            specs=[{k:b[k] for k in ('id','role','parent','head','tail','deform','mirror','roll')} for b in fitted['skeleton']['bones']]
            evidence={b['id']:b['evidence'] for b in fitted['skeleton']['bones']};skeleton_changed=True
    if skeleton_changed:
        skeleton=skeleton_from_heads(specs,identifier=result.skeleton['id'],version=next_version(result.skeleton['version']))
        for b in skeleton['bones']:b['evidence']=evidence[b['id']]
        result.manifest['skeleton']=skeleton
        invalidated=['skins','animations','state_mapping','review','validation'];result.weights={};result.clips={};result.manifest['state_mapping']={}
        for u in result.manifest['uncertainties']:
            if evidence.get(u['element'],{}).get('source')=='human-landmark':u['resolution']='corrected'
    validate_skeleton(result.skeleton,result.manifest['limits'])
    validate_controllers(result.skeleton,result.manifest['controllers'])
    bone_ids={b['id'] for b in result.skeleton['bones']}
    require(all(s['bone'] in bone_ids for s in result.manifest['attachments']),'RIG_ATTACHMENT_REFERENCE','Attachment bone is missing.')
    require(set(result.manifest['facial']['eyes']) <= bone_ids,'RIG_EYE_REFERENCE','Eye control bone is missing.')
    if persist:
        digest=object_hash(correction);atomic_json(safe_path(result.root,'rig/corrections/'+digest+'.json'),correction)
        save_rig(result,operation='manual-correction')
    return result,dict(invalidated=invalidated,rig_sha256=rig_signature(result.manifest),source_preserved=True)
