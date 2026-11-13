"""Exact library bindings for independent scenes and a real Scene3D loader.

The library, dependency graph, native rig evaluator and Scene3D owner retain
their authorities. This adapter owns only consumer bindings and scoped edits.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile

from asset_library import read_source_bytes
from asset_library_contracts import portable_path
from asset_library_dependencies import DependencyGraph, location, location_key
from atomic_publish import _rename_no_replace
from rig_errors import RigError, atomic_bytes, decode_json, finite_json, job_lock, object_hash

MAX_SCENES=32
MAX_INSTANCES=128
MAX_BYTES=4*1024**2
ROLES={'asset','character','rig','animation','material'}
IDENTITY=dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1])


def require(ok,code,message):
    if not ok:raise RigError('LIBRARY_REUSE_'+code,message,stage='asset-reuse')


def identifier(value):
    require(isinstance(value,str) and re.fullmatch('[a-z][a-z0-9_-]{0,63}',value) is not None,
            'ID','Use a bounded lowercase consumer identifier.')
    return value


@dataclass(frozen=True)
class AssetBinding:
    location: dict
    path: str

    def document(self):return dict(location=deepcopy(self.location),path=self.path)


def binding(value):
    if isinstance(value,AssetBinding):value=value.document()
    require(isinstance(value,dict) and set(value)=={'location','path'},'BINDING','Use an exact location and inventoried path.')
    location(value['location']);portable_path(value['path'])
    return deepcopy(value)


def _transform(value):
    from jsonschema import Draft202012Validator
    from scene3d_contracts import definitions
    from scene3d_math import local_matrix
    require(Draft202012Validator(definitions()['transform']).is_valid(value),'TRANSFORM','Invalid native Scene3D transform.')
    local_matrix(value)
    return deepcopy(value)


def _overrides(value):
    require(isinstance(value,dict) and len(value)<=128,'MATERIAL','Use at most 128 explicit material slot overrides.')
    from math import isfinite
    for slot,material in value.items():
        require(isinstance(slot,str) and re.fullmatch('0|[1-9][0-9]{0,2}',slot) is not None,'MATERIAL','Material slots are decimal indices.')
        require(isinstance(material,dict) and set(material)=={'base_color','metallic','roughness'},'MATERIAL','Use explicit native PBR comparison values.')
        require(isinstance(material['base_color'],list) and len(material['base_color'])==4,'MATERIAL','Base color has four components.')
        require(all(type(x) in (int,float) and isfinite(x) and 0<=x<=1 for x in
                    [*material['base_color'],material['metallic'],material['roughness']]),'MATERIAL','PBR values must be finite in zero to one.')
    return deepcopy(value)


def _state(value):
    if value is None:return None
    require(isinstance(value,dict) and set(value)=={'clip','time'},'ANIMATION','Use an exact native clip and rational time.')
    require(isinstance(value['clip'],str) and 0<len(value['clip'])<=128,'ANIMATION','Invalid clip identifier.')
    require(isinstance(value['time'],str) and 0<len(value['time'])<=64,'ANIMATION','Use a bounded rational source time.')
    try:time=Fraction(value['time'])
    except (ValueError,ZeroDivisionError) as exc:raise RigError('LIBRARY_REUSE_ANIMATION','Invalid rational source time.') from exc
    require(0<=time<=600 and time.denominator<=1000000,'ANIMATION','Source time exceeds limits.')
    return dict(clip=value['clip'],time=str(time))


def validate_project(value):
    require(isinstance(value,dict) and set(value)=={'schema_version','kind','id','revision','scenes','history'},'PROJECT','Invalid binding document fields.')
    require(value['schema_version']==1 and value['kind']=='asset-reuse-project','PROJECT','Unsupported consumer document version.')
    identifier(value['id'])
    require(type(value['revision']) is int and 0<=value['revision']<=1_000_000,'REVISION','Invalid revision.')
    require(isinstance(value['scenes'],dict) and len(value['scenes'])<=MAX_SCENES,'LIMIT','Too many scene scopes.')
    count=0
    for name,scene in value['scenes'].items():
        identifier(name)
        require(isinstance(scene,dict) and set(scene)=={'instances','receipts'},'SCENE','Invalid scene scope.')
        require(isinstance(scene['instances'],dict) and isinstance(scene['receipts'],list) and len(scene['receipts'])<=128,'SCENE','Invalid scene instances or receipts.')
        for receipt in scene['receipts']:
            require(isinstance(receipt,dict) and set(receipt)=={'location','path'},'RECEIPT','Receipts retain exact library payload references.')
            binding(receipt)
        count+=len(scene['instances'])
        require(count<=MAX_INSTANCES,'LIMIT','Project exceeds 128 independent instances.')
        for name,instance in scene['instances'].items():
            identifier(name)
            require(name!='root','ID','The native hierarchy root identifier is reserved.')
            require(isinstance(instance,dict) and set(instance)=={'bindings','transform','material_overrides','animation_state'},'INSTANCE','Invalid instance fields.')
            bindings=instance['bindings']
            require(isinstance(bindings,dict) and set(bindings)<=ROLES and ('asset' in bindings)^('character' in bindings),'BINDING','Choose exactly one primary asset/character binding.')
            for item in bindings.values():binding(item)
            _transform(instance['transform']);_overrides(instance['material_overrides']);_state(instance['animation_state'])
    require(isinstance(value['history'],list) and len(value['history'])<=256,'HISTORY','Bounded project history required.')
    for event in value['history']:
        require(isinstance(event,dict),'HISTORY','Invalid consumer receipt.')
        event_keys={'operation','scene','instance','role','before','after','sha256'}
        if event.get('operation') in ('unbind','undo-unbind'):event_keys.add('revision')
        require(set(event)==event_keys,'HISTORY','Invalid consumer receipt fields.')
        require(event['operation'] in ('replace','undo','unbind','undo-unbind'),'HISTORY','Unknown consumer edit.')
        identifier(event['scene']);identifier(event['instance'])
        require(event['role'] in ROLES or event['role']=='instance' and event['operation'] in ('unbind','undo-unbind'),'HISTORY','Unknown binding role.')
        if event['operation'] in ('replace','undo'):
            binding(event['before']);binding(event['after'])
        else:
            require(type(event['revision']) is int and 1<=event['revision']<=value['revision'],'HISTORY','Invalid removal revision.')
            removed,empty=(event['before'],event['after']) if event['operation']=='unbind' else (event['after'],event['before'])
            require(empty is None,'HISTORY','Unbind history requires an absent side.')
            if event['role']=='instance':
                snapshot=dict(schema_version=1,kind='asset-reuse-project',id='history',revision=0,
                              scenes={'history':dict(instances={event['instance']:removed},receipts=[])},history=[])
                validate_project(snapshot)
            else:binding(removed)
        require(object_hash({k:v for k,v in event.items() if k!='sha256'})==event['sha256'],'HISTORY','Replacement receipt changed.')
    finite_json(value)
    require(len(json.dumps(value).encode())<=MAX_BYTES,'LIMIT','Consumer document exceeds four MiB.')
    return deepcopy(value)


def new_project(identifier_value):
    return validate_project(dict(schema_version=1,kind='asset-reuse-project',id=identifier(identifier_value),revision=0,scenes={},history=[]))


def _history_bindings(event):
    for side in ('before','after'):
        value=event[side]
        if value is None:continue
        if event['role']=='instance':yield from value['bindings'].values()
        else:yield value


class SceneLibraryAdapter:
    new_project=staticmethod(new_project)
    def __init__(self,graph:DependencyGraph):
        require(isinstance(graph,DependencyGraph),'GRAPH','Use the canonical authorized DependencyGraph.')
        self.graph=graph

    def capabilities(self):
        return dict(discovery=True,exact_versions=True,scene3d_loader=True,editor_drag_drop=False,
                    geometry='self-contained GLB',camera_light='native Scene3D node JSON',
                    preview=importlib.util.find_spec('asset_library_preview') is not None,
                    packaging=importlib.util.find_spec('asset_library_packages') is not None,
                    legacy='exact 2D adapters; no geometry conversion',coordinate_system='right-handed Y-up meters')

    def discover(self):
        return [dict(location=value,kind=self.graph.inspect(value)['identity']['kind']) for value in self.graph.catalog()]

    def _payload(self,item):
        item=binding(item);manifest=self.graph.inspect(item['location'])
        require(any(f['path']==item['path'] for f in manifest['files']),'PATH','Payload is not inventoried in the exact version.')
        return self.graph.owner(item['location']).read_own_file(item['location']['reference'],item['path']),manifest

    def add_scene(self,project,scene_id,*,receipts=None):
        result=validate_project(project);identifier(scene_id)
        require(scene_id not in result['scenes'],'EXISTS','Scene already exists.')
        result['scenes'][scene_id]=dict(instances={},receipts=deepcopy(receipts or []));result['revision']+=1
        return self.verify(result)['project']

    def instantiate(self,project,scene_id,instance_id,*,bindings,transform=None,material_overrides=None,animation_state=None):
        result=validate_project(project);identifier(instance_id)
        require(scene_id in result['scenes'],'SCENE','Scene scope is absent.')
        require(instance_id not in result['scenes'][scene_id]['instances'],'EXISTS','Instance already exists in this scene.')
        require(instance_id!='root','ID','The native hierarchy root identifier is reserved.')
        require(isinstance(bindings,dict),'BINDING','Bindings must be a role map.')
        instance=dict(bindings={key:binding(item) for key,item in bindings.items()},transform=_transform(transform or IDENTITY),
                      material_overrides=_overrides(material_overrides or {}),animation_state=_state(animation_state))
        result['scenes'][scene_id]['instances'][instance_id]=instance;result['revision']+=1
        self._evaluate(instance)
        return self.verify(result)['project']

    def set_animation(self,project,scene_id,instance_id,state):
        result=validate_project(project)
        try:instance=result['scenes'][scene_id]['instances'][instance_id]
        except KeyError as exc:raise RigError('LIBRARY_REUSE_INSTANCE','Missing scene instance.') from exc
        instance['animation_state']=_state(state);self._evaluate(instance);result['revision']+=1
        return self.verify(result)['project']

    def set_overrides(self,project,scene_id,instance_id,*,transform=None,material_overrides=None):
        result=validate_project(project)
        try:instance=result['scenes'][scene_id]['instances'][instance_id]
        except KeyError as exc:raise RigError('LIBRARY_REUSE_INSTANCE','Missing scene instance.') from exc
        if transform is not None:instance['transform']=_transform(transform)
        if material_overrides is not None:instance['material_overrides']=_overrides(material_overrides)
        self._evaluate(instance);result['revision']+=1
        return self.verify(result)['project']

    def versions(self,item):
        item=binding(item);self._payload(item)
        return [ref for ref in self.graph.owner(item['location']).list_references() if ref['id']==item['location']['reference']['id']]

    def dependencies(self,project):
        project=validate_project(project);scopes={}
        # Independent instances may deliberately pin different versions of one
        # asset. The dependency owner's conflict rules apply inside each scope.
        for scene_id,scene in project['scenes'].items():
            for instance_id,instance in scene['instances'].items():
                roots={location_key(b['location']):b['location'] for b in instance['bindings'].values()}
                scopes[scene_id+'/instance/'+instance_id]=self.graph.lock(list(roots.values()),include_optional=False)
            for i,item in enumerate(scene['receipts']):
                scopes[scene_id+'/receipt/'+str(i)]=self.graph.lock([item['location']],include_optional=False)
        for index,event in enumerate(project['history']):
            for side in ('before','after'):
                value=event[side]
                if value is None:continue
                items=list(value['bindings'].values()) if event['role']=='instance' else [value]
                for position,item in enumerate(items):
                    suffix=side+'-'+str(position) if event['role']=='instance' else side
                    scopes['history-'+str(index)+'-'+suffix]=self.graph.lock([item['location']],include_optional=False)
        return scopes

    def verify(self,project):
        project=validate_project(project);locks=self.dependencies(project)
        for scene in project['scenes'].values():
            for instance in scene['instances'].values():
                for item in instance['bindings'].values():self._payload(item)
            for item in scene['receipts']:self._payload(item)
        for event in project['history']:
            for item in _history_bindings(event):self._payload(item)
        return dict(project=project,sha256=object_hash(project),locks=locks,status='exact-bindings-verified',visual_review='not-reviewed')

    def compatibility(self,project,scene_id,instance_id):
        project=validate_project(project)
        try:instance=project['scenes'][scene_id]['instances'][instance_id]
        except KeyError as exc:raise RigError('LIBRARY_REUSE_INSTANCE','Missing scene instance.') from exc
        evaluated=self._evaluate(instance)
        return dict(compatible=True,kind=evaluated['kind'],native_geometry=evaluated['kind']=='geometry',renderer_executed=False)

    def preview(self,project,scene_id,instance_id,cache_root,**options):
        from asset_library_preview import create_preview
        instance=validate_project(project)['scenes'][scene_id]['instances'][instance_id]
        source=instance['bindings'].get('asset',instance['bindings'].get('character'))
        require(set(instance['bindings'])<= {'asset','character'} and not instance['material_overrides'] and instance['animation_state'] is None,
                'PREVIEW','Direct asset preview cannot represent composite instance overrides; load the Scene3D snapshot instead.')
        return create_preview(self.graph.owner(source['location']),source['location']['reference'],cache_root,logical_path=source['path'],**options)

    def impact(self,project,item):
        project=validate_project(project);item=binding(item);affected=[]
        for scene_id,scene in project['scenes'].items():
            for instance_id,instance in scene['instances'].items():
                roles=[role for role,source in instance['bindings'].items() if source==item]
                if roles:affected.append(dict(scene=scene_id,instance=instance_id,roles=roles))
        return dict(binding=item,affected=affected,unrelated_receipts_preserved=True)

    def replace(self,project,scene_id,instance_id,role,replacement):
        result=validate_project(project);replacement=binding(replacement)
        try:instance=result['scenes'][scene_id]['instances'][instance_id];previous=instance['bindings'][role]
        except (KeyError,TypeError) as exc:raise RigError('LIBRARY_REUSE_INSTANCE','Missing explicit replacement binding.') from exc
        require(previous!=replacement,'REPLACEMENT','Choose a different exact binding.')
        old_kind=self.graph.inspect(previous['location'])['identity']['kind']
        new_kind=self.graph.inspect(replacement['location'])['identity']['kind']
        require(old_kind==new_kind,'COMPATIBILITY','Replacement asset category differs.')
        instance['bindings'][role]=replacement;self._evaluate(instance)
        event=dict(operation='replace',scene=scene_id,instance=instance_id,role=role,before=previous,after=replacement)
        event['sha256']=object_hash(event);result['history'].append(event);result['revision']+=1
        return dict(project=self.verify(result)['project'],receipt=deepcopy(event),
                    invalidated=[dict(scene=scene_id,instance=instance_id)],unrelated_scenes_unchanged=True)

    def undo(self,project,receipt):
        result=validate_project(project)
        require(receipt in result['history'] and receipt['operation']=='replace','UNDO','Use a retained exact replacement receipt.')
        try:instance=result['scenes'][receipt['scene']]['instances'][receipt['instance']]
        except KeyError as exc:raise RigError('LIBRARY_REUSE_UNDO','Replacement target was removed.') from exc
        require(instance['bindings'].get(receipt['role'])==receipt['after'],'STALE','Binding changed after replacement; preserving the newer binding.')
        instance['bindings'][receipt['role']]=deepcopy(receipt['before']);self._evaluate(instance)
        event=dict(operation='undo',scene=receipt['scene'],instance=receipt['instance'],role=receipt['role'],before=receipt['after'],after=receipt['before'])
        event['sha256']=object_hash(event);result['history'].append(event);result['revision']+=1
        return self.verify(result)['project']

    def unbind(self,project,scene_id,instance_id,*,expected_revision,role=None,expected_binding=None):
        """Remove an optional role or whole instance from one explicit revision."""
        result=validate_project(project)
        require(type(expected_revision) is int and expected_revision==result['revision'],'STALE','Project revision changed.')
        try:instance=result['scenes'][scene_id]['instances'][instance_id]
        except KeyError as exc:raise RigError('LIBRARY_REUSE_INSTANCE','Selected instance is absent.') from exc
        if role is None:
            require(expected_binding is None,'BINDING','Whole-instance removal uses the exact project revision.')
            removed=deepcopy(instance);del result['scenes'][scene_id]['instances'][instance_id];event_role='instance'
        else:
            require(role in ('rig','animation','material'),'BINDING','Remove a primary asset by explicitly removing its whole instance.')
            require(role not in ('rig','animation') or instance['animation_state'] is None,'BINDING','Clear the selected animation state explicitly before unbinding its rig or animation source.')
            require(role in instance['bindings'],'BINDING','Selected role is absent.')
            removed=instance['bindings'][role]
            require(expected_binding is None or binding(expected_binding)==removed,'STALE','Selected exact binding changed.')
            del instance['bindings'][role];self._evaluate(instance);event_role=role
        event=dict(operation='unbind',scene=scene_id,instance=instance_id,role=event_role,before=removed,after=None,revision=result['revision']+1)
        event['sha256']=object_hash(event);result['history'].append(event);result['revision']+=1
        verified=self.verify(result)
        return dict(project=verified['project'],receipt=deepcopy(event),locks=verified['locks'],
                    invalidated=[dict(scene=scene_id,instance=instance_id)],immutable_assets_changed=False)

    def undo_unbind(self,project,receipt,*,expected_revision):
        result=validate_project(project)
        require(type(expected_revision) is int and expected_revision==result['revision'],'STALE','Project revision changed.')
        require(receipt in result['history'] and receipt['operation']=='unbind','UNDO','Use a retained unbind receipt.')
        position=result['history'].index(receipt)
        for later in result['history'][position+1:]:
            same_target=later['scene']==receipt['scene'] and later['instance']==receipt['instance']
            overlaps=receipt['role']=='instance' or later['role']=='instance' or later['role']==receipt['role']
            require(not(same_target and overlaps),'STALE','Binding history changed after this removal; preserving newer intent.')
        try:instances=result['scenes'][receipt['scene']]['instances']
        except KeyError as exc:raise RigError('LIBRARY_REUSE_UNDO','Selected scene was removed.') from exc
        if receipt['role']=='instance':
            require(receipt['instance'] not in instances,'STALE','Instance slot is occupied; preserving newer content.')
            instance=deepcopy(receipt['before']);self._evaluate(instance);instances[receipt['instance']]=instance
        else:
            require(receipt['instance'] in instances,'STALE','Selected instance was removed.')
            instance=instances[receipt['instance']]
            require(receipt['role'] not in instance['bindings'],'STALE','Binding slot is occupied; preserving newer content.')
            instance['bindings'][receipt['role']]=deepcopy(receipt['before']);self._evaluate(instance)
        event=dict(operation='undo-unbind',scene=receipt['scene'],instance=receipt['instance'],role=receipt['role'],before=None,after=deepcopy(receipt['before']),revision=result['revision']+1)
        event['sha256']=object_hash(event);result['history'].append(event);result['revision']+=1
        return self.verify(result)['project']

    def _model(self,item):
        payload,manifest=self._payload(item)
        require(payload.startswith(b'glTF'),'FORMAT','Native mesh bindings require a self-contained GLB.')
        from rig_import import load_character_model
        with tempfile.TemporaryDirectory(prefix='reuse149-model-') as temporary:
            path=Path(temporary)/'asset.glb';path.write_bytes(payload)
            from rig_contracts import DEFAULT_LIMITS
            imported=load_character_model(path,limits={**DEFAULT_LIMITS,'influences':8})
            require(len(imported.source_files)==1,'FORMAT','Scene adapter admits self-contained GLB payloads only.')
        return imported,manifest

    @staticmethod
    def _same_geometry(a,b):
        import numpy as np
        return len(a.meshes)==len(b.meshes) and all(x.id==y.id and np.array_equal(x.triangles,y.triangles) and
            x.positions.shape==y.positions.shape and np.allclose(x.positions,y.positions,atol=1e-6,rtol=0)
            for x,y in zip(a.meshes,b.meshes))

    def _evaluate(self,instance):
        sources=instance['bindings'];primary=sources.get('asset',sources.get('character'))
        payload,manifest=self._payload(primary);kind=manifest['identity']['kind']
        if kind in ('camera','light'):
            require(set(sources)<= {'asset','character'} and not instance['material_overrides'] and instance['animation_state'] is None,
                    'CAPABILITY','Camera/light instances cannot carry mesh rig or material bindings.')
            value=decode_json(payload)
            require(isinstance(value,dict) and value.get('type')==kind,'FORMAT','Use an exact native Scene3D camera/light node.')
            from jsonschema import Draft202012Validator
            from scene3d_contracts import definitions,_node_semantics,_transforms
            definitions_=definitions()
            require(Draft202012Validator(dict(definitions_['node'],**{'$defs':definitions_})).is_valid(value),'FORMAT','Invalid native Scene3D node.')
            _node_semantics(value);_transforms(value,'node')
            require(not value['constraints'] and not value['animation_bindings'] and not value['material_overrides'] and
                    value['source'] is None and 'attachment' not in value,'CAPABILITY','Camera/light bindings must be standalone native nodes.')
            return dict(kind=kind,node=deepcopy(value),manifest=manifest)
        require(kind in ('character','prop','environment','rig','material'),'CAPABILITY','This category is not native 3D geometry; use the legacy adapter.')
        imported,manifest=self._model(primary);geometry=deepcopy(imported.geometry);rig_source=imported
        if 'rig' in sources:
            rig_source,_=self._model(sources['rig'])
            require(self._same_geometry(geometry,rig_source.geometry),'COMPATIBILITY','Rig and character bind geometry differ.')
            require(rig_source.skeleton is not None and bool(rig_source.weights),'COMPATIBILITY','Rig binding lacks a real skeleton and skin.')
        if 'material' in sources:
            material_source,_=self._model(sources['material'])
            require(bool(material_source.geometry.materials) and all(mesh.material<len(material_source.geometry.materials) for mesh in geometry.meshes),
                    'MATERIAL','Material source lacks a used slot.')
            for name in ('materials','images','textures','samplers','extensions_used'):
                setattr(geometry,name,deepcopy(getattr(material_source.geometry,name)))
        if instance['animation_state'] is not None:
            require(rig_source.skeleton is not None and bool(rig_source.weights),'ANIMATION','Clip evaluation requires an actual bound rig.')
            animation_source=rig_source
            if 'animation' in sources:animation_source,_=self._model(sources['animation'])
            require(animation_source.skeleton==rig_source.skeleton and self._same_geometry(geometry,animation_source.geometry),
                    'COMPATIBILITY','Animation source is not calibrated to this native geometry and skeleton.')
            state=instance['animation_state'];require(state['clip'] in animation_source.clips,'ANIMATION','Requested native clip is missing.')
            clip=deepcopy(animation_source.clips[state['clip']]);time=Fraction(state['time'])
            require(time<=Fraction(str(clip['duration'])),'ANIMATION','Sample exceeds the native clip duration.')
            from rig_models import RigAsset
            from rig_animation import evaluate_animation
            from rig_skinning import evaluate_deformation
            clip['rig_id']='imported';clip['skeleton_version']=rig_source.skeleton['version']
            rig=RigAsset(dict(id='imported',skeleton=rig_source.skeleton),geometry,rig_source.weights,{clip['id']:clip})
            evaluated=evaluate_deformation(rig,evaluate_animation(rig,clip,float(time))['pose'],normals=True)
            for mesh in geometry.meshes:
                mesh.positions=evaluated[mesh.id]['positions'];mesh.attributes['NORMAL']=evaluated[mesh.id]['normals']
                mesh.morphs={};mesh.morph_defaults={}
        elif 'animation' in sources:
            animation_source,_=self._model(sources['animation'])
            require(bool(animation_source.clips),'ANIMATION','Animation binding contains no executable clips.')
            require(animation_source.skeleton==rig_source.skeleton and self._same_geometry(geometry,animation_source.geometry),
                    'COMPATIBILITY','Animation source is not calibrated to this native geometry and skeleton.')
        for slot,material in instance['material_overrides'].items():
            require(int(slot)<len(geometry.materials),'MATERIAL','Override refers to a missing native slot.')
            pbr=geometry.materials[int(slot)].setdefault('pbrMetallicRoughness',{})
            pbr.update(baseColorFactor=material['base_color'],metallicFactor=material['metallic'],roughnessFactor=material['roughness'])
        from rig_models import validate_geometry
        validate_geometry(geometry)
        return dict(kind='geometry',geometry=geometry,manifest=manifest)

    def legacy(self,item,*,legacy_type=None):
        """Return existing owners' exact 2D documents; never create geometry."""
        payload,manifest=self._payload(item);kind=manifest['identity']['kind']
        require(kind in ('legacy-pet','legacy-cast','legacy-clip','reference'),'LEGACY','Choose an explicit legacy/reference category.')
        from asset_library import legacy_roundtrip
        validation='exact-bytes; contextual receipt approval not reinterpreted'
        if kind.startswith('legacy-'):
            legacy_roundtrip(kind,payload);validation='existing-native-owner'
        else:
            require(legacy_type in ('sprite','drawing','style-snapshot','state-selection','state-catalog','source-receipt','review-receipt','archive-receipt'),
                    'LEGACY','Reference adapters require an explicit 2D source role.')
            if legacy_type=='style-snapshot':
                from style_registry import validate_snapshot
                validate_snapshot(decode_json(payload));validation='existing-style-owner'
            elif legacy_type=='state-selection':
                from state_catalog import validate_selection
                validate_selection(decode_json(payload));validation='existing-state-owner'
            elif legacy_type=='state-catalog':
                from state_catalog import load_library
                with tempfile.TemporaryDirectory(prefix='reuse149-states-') as temporary:
                    path=Path(temporary)/'states.json';path.write_bytes(payload);load_library(path)
                validation='existing-state-owner'
            elif legacy_type in ('sprite','drawing'):
                from PIL import Image
                import io
                with Image.open(io.BytesIO(payload)) as image:
                    require(image.width*image.height<=16_000_000,'LIMIT','2D inspection image exceeds pixel limit.');image.load()
                validation='decoded-2d-image; semantics unreviewed'
        return dict(kind=kind,dimensionality='2d',sha256=hashlib.sha256(payload).hexdigest(),
                    payload=payload,geometry=None,converted=False,legacy_type=legacy_type or kind,validation=validation,
                    review_status=manifest['metadata']['review_status'])

    def package_project(self,project,destination,*,package_id):
        from asset_library_packages import create_package
        report=self.verify(project);scopes={'scope-'+hashlib.sha256(name.encode()).hexdigest()[:24]:lock['graph']['roots'] for name,lock in report['locks'].items()}
        with tempfile.TemporaryDirectory(prefix='reuse149-package-') as temporary:
            path=Path(temporary)/'bindings.json';path.write_text(json.dumps(report['project'],sort_keys=True))
            return create_package(self.graph,[],destination,package_id=package_id,package_type='project',
                                  scopes=scopes,context_files={'bindings.json':path})

    def load_scene(self,project,scene_id,destination):
        """Publish a real evaluated Scene3D snapshot; animation remains a sample."""
        project=self.verify(project)['project'];require(scene_id in project['scenes'],'SCENE','Scene does not exist.')
        from scene3d_contracts import COORDINATES,canonical_bytes,load_project
        from scene3d_example import node,SETTINGS
        from rig_gltf import export_geometry
        import numpy as np
        destination=Path(destination).absolute()
        require(not destination.exists() and not destination.is_symlink(),'EXISTS','Load into a new destination.')
        require(destination.parent.is_dir() and not any(p.is_symlink() for p in (destination.parent,*destination.parents)),
                'PATH','Destination parent must be a real directory.')
        with tempfile.TemporaryDirectory(prefix='.reuse149-',dir=destination.parent) as temporary:
            stage=Path(temporary)/'project';stage.mkdir();refs=[];assets=[];nodes=[node('root')];cameras=[];lights=[];total_vertices=0;total_triangles=0
            provenance=dict(origin='derived',fixture=False,tool='asset-library-reuse-v1',inputs=[],attribution='Exact registered source attribution retained in bindings.json',license='See exact registered source manifests')
            def write(kind,id,**fields):
                value=dict(schema_version=1,kind='scene3d-'+kind,id=id,version='1.0.0',**fields)
                data=canonical_bytes(value);path='project.json' if kind=='project' else kind+'-'+id+'.json';(stage/path).write_bytes(data)
                ref=dict(id=id,version='1.0.0',path=path,sha256=hashlib.sha256(data).hexdigest(),kind=value['kind']);refs.append(ref);return ref
            bindings_bytes=json.dumps(project,sort_keys=True,separators=(',',':')).encode();(stage/'bindings.json').write_bytes(bindings_bytes)
            provenance['inputs']=[dict(path='bindings.json',sha256=hashlib.sha256(bindings_bytes).hexdigest())]
            for instance_id,instance in project['scenes'][scene_id]['instances'].items():
                evaluated=self._evaluate(instance);provenance['fixture']|=evaluated['manifest']['provenance']['fixture']
                if evaluated['kind']!='geometry':
                    item=evaluated['node'];item.update(id=instance_id,name=instance_id,parent='root',transform=deepcopy(instance['transform']))
                    nodes.append(item);(cameras if evaluated['kind']=='camera' else lights).append(instance_id);continue
                geometry=evaluated['geometry'];total_vertices+=geometry.vertex_count;total_triangles+=geometry.triangle_count
                require(total_vertices<=1_000_000 and total_triangles<=2_000_000,'LIMIT','Combined scene geometry exceeds owner limits.')
                path=instance_id+'.glb';export_geometry(geometry,stage/path,provenance=dict(fixture=evaluated['manifest']['provenance']['fixture']))
                file=dict(path=path,sha256=hashlib.sha256((stage/path).read_bytes()).hexdigest())
                points=np.concatenate([m.positions for m in geometry.meshes])
                asset=write('asset','asset-'+hashlib.sha256(instance_id.encode()).hexdigest()[:24],name=instance_id,asset_type='mesh',content=file,source=file,provenance=deepcopy(provenance),
                    coordinate_system=COORDINATES,original_coordinate_system='canonical',pivot=[0,0,0],meshes=[file],materials=[],
                    bounds=dict(minimum=points.min(axis=0).tolist(),maximum=points.max(axis=0).tolist()),collision=None,sockets=[],components={},skeleton=None,animations=[],lods=[],dependencies=[],
                    resources=dict(vertices=geometry.vertex_count,triangles=geometry.triangle_count,bytes=(stage/path).stat().st_size),validation=[],review=dict(status='unreviewed',receipts=[]))
                assets.append(asset);item=node(instance_id,'mesh_instance','root');item.update(asset=asset,transform=deepcopy(instance['transform']));nodes.append(item)
            native_scene='scene-'+hashlib.sha256(scene_id.encode()).hexdigest()[:24]
            shot_id='sample-'+hashlib.sha256(scene_id.encode()).hexdigest()[:24]
            scene=write('scene',native_scene,name=scene_id,description='Evaluated exact-library scene snapshot',revision=project['revision'],coordinate_system=COORDINATES,roots=['root'],nodes=nodes,
                world=dict(coordinate_system=COORDINATES,background=[.08,.08,.08],exposure=0,environment=None,metadata={}),collections=[],layers=[],cameras=cameras,lights=lights,recipes=[],dependencies=assets,bounds=None,shots=[shot_id] if cameras else [],continuity=[],metadata={},provenance=deepcopy(provenance))
            shots=[]
            if cameras:
                shots.append(write('shot',shot_id,name='Evaluated library sample',scene=scene,duration_frames=1,fps=dict(numerator=24,denominator=1),camera=cameras[0],camera_overrides={},tracks=[],bindings=[],material_overrides=[],temporal_constraints=[],render=SETTINGS,continuity=[],dependencies=assets,metadata={}))
            write('project','project',name=project['id'],status='fixture' if provenance['fixture'] else 'development',coordinate_system=COORDINATES,asset_catalog=None,assets=assets,characters=[],scenes=[scene],shots=shots,prefabs=[],animations=[],render_defaults=SETTINGS,
                budgets=dict(max_nodes=256,max_files=1024,max_bytes=256*1024**2,max_frames=1),created_at='2026-11-13T00:00:00Z',modified_at='2026-11-13T00:00:00Z',linked_2d_project=None,dependency_lock=deepcopy(refs),validation=[],provenance=deepcopy(provenance),metadata={})
            result=load_project(stage)
            self.verify(project)
            _rename_no_replace(stage,destination)
        return dict(path=str(destination),status=result['status'],bindings_sha256=object_hash(project),scene=scene_id,native_scene=native_scene,shot=shot_id if cameras else None,animation='evaluated-single-sample',renderer_executed=False)


class ProjectStore:
    """Bounded CAS persistence; old binding revisions and receipts are retained."""
    def __init__(self,root):
        self.root=Path(root).absolute()
        require(not any(p.is_symlink() for p in (self.root,*self.root.parents)),'PATH','Project storage cannot contain symlinks.')

    def load(self):
        require(not any(p.is_symlink() for p in (self.root,*self.root.parents)),'PATH','Project storage cannot contain symlinks.')
        payload=read_source_bytes(self.root/'project.json',MAX_BYTES)
        return validate_project(decode_json(payload))

    def save(self,project,*,expected_sha256):
        project=validate_project(project);payload=json.dumps(project,sort_keys=True,separators=(',',':')).encode()
        with job_lock(self.root):
            self.root.mkdir(mode=0o700,parents=False,exist_ok=True)
            require(not any(p.is_symlink() for p in (self.root,*self.root.parents)) and self.root.is_dir(),'PATH','Project root changed.')
            if os.name=='posix':require(self.root.stat().st_uid==os.getuid() and self.root.stat().st_mode&0o077==0,'PATH','Project storage must be private to its owner.')
            path=self.root/'project.json'
            current=self.load() if path.exists() else None
            require((object_hash(current) if current is not None else None)==expected_sha256,'STALE','Saved project changed; reload before writing.')
            revision=self.root/'revisions'/ (object_hash(project)+'.json');revision.parent.mkdir(exist_ok=True)
            require(not revision.parent.is_symlink(),'PATH','Revision storage cannot be a symlink.')
            if revision.exists():require(read_source_bytes(revision,MAX_BYTES)==payload,'CORRUPT','Retained project revision changed.')
            else:
                with revision.open('xb') as stream:stream.write(payload)
            atomic_bytes(path,payload)
        return dict(path=str(path),sha256=object_hash(project),revision=project['revision'])

    def unbind(self,adapter,scene_id,instance_id,*,expected_sha256,expected_revision,role=None,expected_binding=None):
        project=self.load()
        require(object_hash(project)==expected_sha256,'STALE','Saved project changed; reload before unbinding.')
        result=adapter.unbind(project,scene_id,instance_id,expected_revision=expected_revision,
                              role=role,expected_binding=expected_binding)
        result['saved']=self.save(result['project'],expected_sha256=expected_sha256)
        return result

    def undo_unbind(self,adapter,receipt,*,expected_sha256,expected_revision):
        project=self.load()
        require(object_hash(project)==expected_sha256,'STALE','Saved project changed; reload before undo.')
        restored=adapter.undo_unbind(project,receipt,expected_revision=expected_revision)
        return dict(project=restored,saved=self.save(restored,expected_sha256=expected_sha256))

    def set_animation(self,adapter,scene_id,instance_id,state,*,expected_sha256,expected_revision):
        project=self.load()
        require(object_hash(project)==expected_sha256 and type(expected_revision) is int and project['revision']==expected_revision,
                'STALE','Saved project changed; reload before changing animation.')
        updated=adapter.set_animation(project,scene_id,instance_id,state)
        return dict(project=updated,saved=self.save(updated,expected_sha256=expected_sha256))


def main(argv=None):
    import argparse
    from asset_library import AssetLibrary
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['verify','load','dependencies','capabilities'])
    parser.add_argument('--library',action='append',required=True,help='Authorized alias=/private/library')
    parser.add_argument('--project',type=Path)
    parser.add_argument('--scene');parser.add_argument('--destination',type=Path)
    args=parser.parse_args(argv)
    try:
        libraries={}
        for item in args.library:
            alias,separator,path=item.partition('=')
            require(separator and alias not in libraries,'LIBRARY','Supply unique alias=path mappings.')
            libraries[alias]=AssetLibrary(path)
        adapter=SceneLibraryAdapter(DependencyGraph(libraries))
        if args.command=='capabilities':result=adapter.capabilities()
        else:
            require(args.project is not None,'PROJECT','Supply a saved binding document.')
            project=validate_project(decode_json(read_source_bytes(args.project,MAX_BYTES)))
            if args.command=='verify':result=adapter.verify(project)
            elif args.command=='dependencies':result=adapter.dependencies(project)
            else:
                require(args.scene is not None and args.destination is not None,'REQUEST','Load requires a scene and new destination.')
                result=adapter.load_scene(project,args.scene,args.destination)
        print(json.dumps(result,sort_keys=True));return 0
    except (RigError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps(dict(status='failed',diagnostic=getattr(exc,'report',dict(message=str(exc))))));return 2


if __name__=='__main__':raise SystemExit(main())
