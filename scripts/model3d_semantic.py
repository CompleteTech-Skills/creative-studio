"""Copy-based editable semantic assemblies over native Geometry and transforms.

Part labels are explicit authoring metadata, never inferred anatomy or approval.
Rigid part poses do not create skins, joint solvers or functioning animation.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from copy import deepcopy,copy
import hashlib
import re
import numpy as np

from rig_contracts import DEFAULT_LIMITS
from rig_errors import finite_json, object_hash, require
from rig_models import Geometry, Mesh, validate_geometry, _MATERIAL_TEXTURE_SLOTS
from model3d_coordinates import transform_geometry


def identifier(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value) is not None,
            'MODEL3D_PART_ID','Part and mesh identifiers must be portable and bounded.')
    return value


def matrix(value):
    require(isinstance(value,(list,tuple,np.ndarray)), 'MODEL3D_PART_TRANSFORM','Expected an affine matrix.')
    try:
        rows=list(value)
        require(all(isinstance(row,(list,tuple,np.ndarray)) for row in rows)
                and all(isinstance(item,(int,float,np.integer,np.floating)) and not isinstance(item,(bool,np.bool_))
                        for row in rows for item in row),'MODEL3D_PART_TRANSFORM','Transform scalars must be numbers, never booleans.')
        raw=np.asarray(value)
        require(raw.dtype.kind in 'fiu','MODEL3D_PART_TRANSFORM','Transforms require numeric values; strings and booleans are not coordinates.')
        result=np.asarray(value,dtype=float)
    except (ValueError,TypeError,OverflowError):require(False,'MODEL3D_PART_TRANSFORM','Invalid numeric transform.')
    require(result.shape==(4,4) and np.isfinite(result).all() and np.array_equal(result[3],[0,0,0,1]),
            'MODEL3D_PART_TRANSFORM','Expected finite affine 4x4 matrix.')
    scales=np.max(np.abs(result[:3,:3]),axis=0)
    require(np.all(scales>0) and abs(np.linalg.det(result[:3,:3]/scales))>1e-12,
            'MODEL3D_PART_TRANSFORM','Transform is singular or ill conditioned.')
    try:inverse=np.linalg.inv(result)
    except np.linalg.LinAlgError:require(False,'MODEL3D_PART_TRANSFORM','Transform has no representable inverse.')
    require(np.isfinite(inverse).all(),'MODEL3D_PART_TRANSFORM','Transform inverse exceeds numeric range.')
    return result.copy()


def _mesh_hash(mesh):
    arrays={'positions':mesh.positions,'triangles':mesh.triangles,**{'attribute:'+k:v for k,v in mesh.attributes.items()}}
    arrays.update({'morph:'+k+':'+s:v for k,target in mesh.morphs.items() for s,v in target.items()})
    arrays={key:np.asarray(value,dtype='<i8' if key=='triangles' else '<f8') for key,value in arrays.items()}
    return object_hash(dict(metadata=dict(id=mesh.id,name=mesh.name,region=mesh.region,source_node=mesh.source_node,
                            material=mesh.material,morph_defaults=mesh.morph_defaults),
                            arrays={k:dict(shape=list(v.shape),dtype=v.dtype.str,
                                           sha256=hashlib.sha256(v.tobytes()).hexdigest()) for k,v in sorted(arrays.items())}))


def _edit_budget(asset,*,extra_vertices=0,extra_triangles=0):
    require(asset.geometry.vertex_count+extra_vertices<=DEFAULT_LIMITS['vertices']
            and asset.geometry.triangle_count+extra_triangles<=DEFAULT_LIMITS['triangles'],
            'MODEL3D_PART_LIMIT','Edit exceeds native geometry budgets before allocation.')
    amount=sum(m.positions.nbytes+m.triangles.nbytes+sum(v.nbytes for v in m.attributes.values())
               +sum(v.nbytes for target in m.morphs.values() for v in target.values()) for m in asset.geometry.meshes)
    amount+=sum(len(i.data) for i in asset.geometry.images)+sum(map(len,asset.evidence.values()))
    require(amount*12<=DEFAULT_LIMITS['memory_bytes'],'MODEL3D_PART_LIMIT','Edit copy/transform working set exceeds its memory budget.')


def _resource_hashes(geometry,materials):
    references=set()
    for material in materials:
        groups=[(material,('normalTexture','occlusionTexture','emissiveTexture')),
                (material.get('pbrMetallicRoughness',{}),('baseColorTexture','metallicRoughnessTexture'))]
        groups += [(material.get('extensions',{}).get(extension,{}),slots) for extension,slots in _MATERIAL_TEXTURE_SLOTS.items()]
        for group,slots in groups:
            for key in slots:
                if key in group:references.add(group[key]['index'])
    return [dict(texture=geometry.textures[i],image_sha256=hashlib.sha256(geometry.images[geometry.textures[i]['source']].data).hexdigest(),
                 sampler=geometry.samplers[geometry.textures[i]['sampler']] if 'sampler' in geometry.textures[i] else None)
            for i in sorted(references)]


@dataclass
class SemanticAsset:
    geometry: Geometry
    parts: dict
    id: str='asset'
    version: int=1
    history: list=field(default_factory=list)
    evidence: dict=field(default_factory=dict)
    identity: dict|None=None
    style: dict|None=None
    poses: dict=field(default_factory=dict)
    states: dict=field(default_factory=dict)
    derivatives: dict=field(default_factory=dict)
    fixture: bool|None=None


def create_asset(geometry, *, asset_id='asset',fixture=None):
    identifier(asset_id);validate_geometry(geometry)
    _edit_budget(SemanticAsset(geometry,{},id=asset_id))
    require(fixture is None or type(fixture) is bool,'MODEL3D_PART_SCHEMA','Fixture origin must be explicit or unknown.')
    result=SemanticAsset(deepcopy(geometry),{},id=asset_id,fixture=fixture)
    for mesh in result.geometry.meshes:
        identifier(mesh.id)
        result.parts[mesh.id]=dict(id=mesh.id,parent=None,meshes=[mesh.id],matrix=np.eye(4).tolist(),
                                  pivot=[0.,0.,0.],visible=True,semantic='unknown',side='unknown',
                                  attachments=[],evidence=[],review='pending')
    validate_asset(result)
    return result


def world_matrices(asset):
    require(isinstance(asset.parts,dict) and 0<len(asset.parts)<=4096,'MODEL3D_PARTS','Expected bounded parts.')
    result={};depths={}
    for key in asset.parts:
        path=[];current=key;seen=set()
        while current is not None and current not in result:
            require(current in asset.parts and current not in seen,'MODEL3D_PART_HIERARCHY','Unknown parent or hierarchy cycle.')
            require(len(path)<128,'MODEL3D_PART_HIERARCHY','Part hierarchy exceeds 128 levels.')
            seen.add(current);path.append(current);current=asset.parts[current]['parent']
        parent=np.eye(4) if current is None else result[current];depth=0 if current is None else depths[current]
        for item in reversed(path):
            depth+=1;require(depth<=128,'MODEL3D_PART_HIERARCHY','Part hierarchy exceeds 128 levels.')
            parent=parent@matrix(asset.parts[item]['matrix'])
            require(np.isfinite(parent).all(),'MODEL3D_PART_TRANSFORM','Accumulated transform overflowed.')
            matrix(parent);result[item]=parent;depths[item]=depth
    return result


def validate_asset(asset):
    require(isinstance(asset,SemanticAsset),'MODEL3D_PARTS','Expected native semantic asset.')
    identifier(asset.id)
    require(isinstance(asset.geometry,Geometry) and isinstance(asset.geometry.meshes,list)
            and all(isinstance(m,Mesh) and isinstance(m.positions,np.ndarray) and isinstance(m.triangles,np.ndarray)
                    and m.positions.dtype.kind in 'fiu' and isinstance(m.attributes,dict)
                    and isinstance(m.morphs,dict) for m in asset.geometry.meshes),
            'MODEL3D_PART_SCHEMA','Semantic edits require native Mesh arrays and Geometry containers.')
    validate_geometry(asset.geometry)
    require(isinstance(asset.parts,dict) and 0<len(asset.parts)<=4096,'MODEL3D_PART_SCHEMA','Parts must be a bounded object.')
    require(asset.fixture is None or type(asset.fixture) is bool,'MODEL3D_PART_SCHEMA','Invalid fixture classification.')
    require(all(m.positions.dtype.kind not in 'iu' or np.all(np.abs(m.positions.astype(object))<=2**53)
                for m in asset.geometry.meshes),'MODEL3D_PART_NUMERIC','Integer source coordinates exceed the exact float64 editing range.')
    require(type(asset.version) is int and 1<=asset.version<=1000000,'MODEL3D_PART_VERSION','Invalid revision.')
    known=asset.geometry.by_id();owned=[]
    for key,part in asset.parts.items():
        identifier(key)
        require(isinstance(part,dict) and set(part)=={'id','parent','meshes','matrix','pivot','visible','semantic',
                'side','attachments','evidence','review'} and part['id']==key,
                'MODEL3D_PART_SCHEMA','Malformed semantic part.')
        require(part['parent'] is None or isinstance(part['parent'],str),'MODEL3D_PART_HIERARCHY','Invalid parent.')
        require(isinstance(part['meshes'],list) and len(part['meshes'])<=4096 and all(isinstance(m,str) and m in known for m in part['meshes']),
                'MODEL3D_PART_BINDING','Part references missing native mesh.')
        owned+=part['meshes']
        require(type(part['visible']) is bool and isinstance(part['semantic'],str) and 0<len(part['semantic'])<=128
                and part['side'] in ('left','right','center','unknown') and part['review'] in ('pending','approved','rejected'),
                'MODEL3D_PART_SCHEMA','Invalid semantic metadata.')
        require(isinstance(part['pivot'],(list,tuple)) and len(part['pivot'])==3
                and all(type(x) in (int,float) and np.isfinite(x) for x in part['pivot']),
                'MODEL3D_PART_SCHEMA','Invalid local pivot.')
        require(isinstance(part['attachments'],list) and len(part['attachments'])<=128
                and isinstance(part['evidence'],list) and len(part['evidence'])<=128,
                'MODEL3D_PART_SCHEMA','Part metadata exceeds its budget.')
        for attachment in part['attachments']:
            require(isinstance(attachment,dict) and set(attachment)=={'id','point','target'}
                    and isinstance(attachment['point'],list) and len(attachment['point'])==3
                    and all(type(x) in (int,float) and np.isfinite(x) for x in attachment['point'])
                    and (attachment['target'] is None or (isinstance(attachment['target'],str) and attachment['target'] in asset.parts)),
                    'MODEL3D_PART_SCHEMA','Attachments require a native local point and optional known target.')
            identifier(attachment['id'])
    require(len(owned)==len(set(owned)) and set(owned)==set(known),'MODEL3D_PART_BINDING','Every native mesh must have one semantic owner.')
    world_matrices(asset)
    require(isinstance(asset.history,list) and isinstance(asset.poses,dict)
            and isinstance(asset.states,dict) and isinstance(asset.derivatives,dict),
            'MODEL3D_PART_SCHEMA','History/pose/state/dependency metadata has unsupported types.')
    for record in asset.history:
        require(isinstance(record,dict) and set(record)=={'operation','from_version','affected','before','after'}
                and isinstance(record['operation'],str) and 0<len(record['operation'])<=16384
                and type(record['from_version']) is int and 0<record['from_version']<asset.version
                and isinstance(record['affected'],list) and len(record['affected'])<=4096
                and all(isinstance(key,str) for key in record['affected'])
                and all(isinstance(record[k],str) and re.fullmatch(r'[0-9a-f]{64}',record[k]) is not None for k in ('before','after')),
                'MODEL3D_PART_SCHEMA','Invalid persisted edit history.')
    for name,pose in asset.poses.items():
        identifier(name)
        require(isinstance(pose,dict) and 0<len(pose)<=4096,'MODEL3D_PART_POSE','Invalid persisted pose.')
        for key,value in pose.items():identifier(key);matrix(value)
    for name,entry in asset.states.items():
        require(isinstance(name,str) and len(name)<=128 and isinstance(entry,dict)
                and set(entry)=={'pose','status','catalog_sha256','animation','review'}
                and (entry['pose'] is None or isinstance(entry['pose'],str))
                and entry['status'] in ('rigid-pose','unavailable') and entry['animation'] is False
                and ((entry['pose'] is None)==(entry['status']=='unavailable'))
                and entry['review']=='pending','MODEL3D_STATE','Invalid persisted rigid-state mapping.')
    if asset.style is not None:
        require(isinstance(asset.style,dict) and set(asset.style)=={'snapshot','adaptations','source_sha256','geometry_generated'}
                and asset.style['geometry_generated'] is False and isinstance(asset.style['adaptations'],dict)
                and set(asset.style['adaptations'])=={'geometry','material','texture','render'}
                and all(isinstance(v,str) and 0<len(v)<=4096 for v in asset.style['adaptations'].values()),
                'MODEL3D_STYLE','Invalid frozen style adaptation.')
        from style_registry import validate_snapshot
        try:validate_snapshot(asset.style['snapshot'])
        except (ValueError,TypeError) as exc:require(False,'MODEL3D_STYLE','Invalid frozen source style: '+str(exc)[:512])
        require(asset.style['source_sha256']==object_hash(asset.style['snapshot']),
                'MODEL3D_STYLE','Frozen style source hash differs.')
    dependency_signatures=None
    for name,entry in asset.derivatives.items():
        identifier(name)
        require(isinstance(entry,dict) and set(entry)=={'parts','artifact_sha256','stale','inputs','identity_sha256','style_sha256'}
                and isinstance(entry['parts'],list) and 0<len(entry['parts'])<=4096
                and all(isinstance(key,str) for key in entry['parts']) and type(entry['stale']) is bool
                and isinstance(entry['inputs'],dict) and set(entry['inputs'])==set(entry['parts'])
                and isinstance(entry['artifact_sha256'],str) and re.fullmatch(r'[0-9a-f]{64}',entry['artifact_sha256']) is not None,
                'MODEL3D_PART_DEPENDENCY','Invalid persisted derivative dependencies.')
        require(entry['stale'] or set(entry['parts'])<=set(asset.parts),
                'MODEL3D_PART_DEPENDENCY','A deleted dependency must invalidate its derivative.')
        if not entry['stale']:
            if dependency_signatures is None:dependency_signatures=part_signatures(asset)
            actual=dependency_signatures
            require(entry['inputs']=={key:actual[key] for key in entry['parts']}
                    and (entry['identity_sha256'] is None or entry['identity_sha256']==object_hash(asset.identity))
                    and (entry['style_sha256'] is None or entry['style_sha256']==object_hash(asset.style)),
                    'MODEL3D_PART_DEPENDENCY','A derivative cannot remain current after its recorded dependencies change.')
    finite_json(dict(history=asset.history,identity=asset.identity,style=asset.style,poses=asset.poses,
                     states=asset.states,derivatives=asset.derivatives,parts=asset.parts))
    require(len(asset.history)<=10000 and len(asset.poses)<=128 and len(asset.states)<=416
            and len(asset.derivatives)<=4096,'MODEL3D_PART_LIMIT','Metadata budget exceeded.')
    require(isinstance(asset.evidence,dict) and len(asset.evidence)<=4096
            and all(isinstance(k,str) and isinstance(v,bytes) for k,v in asset.evidence.items())
            and sum(map(len,asset.evidence.values()))<=256*1024**2,'MODEL3D_PART_LIMIT','Evidence exceeds its bounded byte budget.')
    require(all(hashlib.sha256(v).hexdigest()==k for k,v in asset.evidence.items()),
            'MODEL3D_PART_EVIDENCE','Evidence keys must retain their exact original byte hashes.')
    _edit_budget(asset)
    return asset


def evaluated_geometry(asset, *, include_hidden=False):
    validate_asset(asset);world=world_matrices(asset)
    result=Geometry([],deepcopy(asset.geometry.materials),deepcopy(asset.geometry.images),deepcopy(asset.geometry.textures),
                    deepcopy(asset.geometry.samplers),list(asset.geometry.extensions_used))
    known=asset.geometry.by_id()
    for key,part in asset.parts.items():
        visible=part['visible'];parent=part['parent']
        while parent is not None:
            visible=visible and asset.parts[parent]['visible'];parent=asset.parts[parent]['parent']
        if visible or include_hidden:
            if part['meshes']:
                result.meshes+=_transformed_meshes(asset.geometry,[known[m] for m in part['meshes']],world[key])
    require(bool(result.meshes),'MODEL3D_PART_HIDDEN','All geometry is hidden; no mesh export is possible.')
    validate_geometry(result);return result


def _transformed_meshes(geometry,meshes,transform):
    # Source resources have already passed native validation. Numeric transform
    # semantics depend only on mesh arrays; avoid cloning/decoding unrelated
    # meshes and full texture tables for every part. Restore exact slots after
    # the existing native copy-transform authority evaluates selected arrays.
    slots={mesh.id:mesh.material for mesh in meshes};selected=[]
    for mesh in meshes:
        item=copy(mesh);item.material=0;selected.append(item)
    transformed=transform_geometry(Geometry(selected,[{}]),transform).meshes
    for mesh in transformed:mesh.material=slots[mesh.id]
    return transformed


def part_signatures(asset):
    world=world_matrices(asset);meshes=asset.geometry.by_id();result={};resources={}
    for key,part in asset.parts.items():
        metadata=deepcopy(part);metadata.pop('review')
        visible=part['visible'];parent=part['parent']
        while parent is not None:
            visible=visible and asset.parts[parent]['visible'];parent=asset.parts[parent]['parent']
        slots=tuple(meshes[mid].material for mid in part['meshes'])
        if slots not in resources:resources[slots]=_resource_hashes(asset.geometry,[asset.geometry.materials[i] for i in slots])
        result[key]=object_hash(dict(part=metadata,world=world[key].tolist(),
            effective_visible=visible,
            meshes=[_mesh_hash(meshes[mid]) for mid in part['meshes']],
            materials=[asset.geometry.materials[meshes[mid].material] for mid in part['meshes']],
            resources=resources[slots]))
    return result


def _finish(original,result,operation):
    validate_asset(original)
    retained=result.derivatives;result.derivatives={}
    try:validate_asset(result)
    finally:result.derivatives=retained
    before=part_signatures(original);after=part_signatures(result)
    affected={key for key in set(before)|set(after) if before.get(key)!=after.get(key)}
    for key in affected & set(result.parts):result.parts[key]['review']='pending'
    for entry in result.derivatives.values():
        if affected & set(entry['parts']):entry['stale']=True
        if entry.get('identity_sha256') is not None and entry['identity_sha256']!=object_hash(result.identity):entry['stale']=True
        if entry.get('style_sha256') is not None and entry['style_sha256']!=object_hash(result.style):entry['stale']=True
    for entry in result.states.values():
        pose=entry['pose']
        if pose is not None and (pose not in result.poses or not set(result.poses[pose])<=set(result.parts)):
            entry.update(pose=None,status='unavailable',review='pending')
    result.version=original.version+1
    result.history.append(dict(operation=operation,from_version=original.version,
                               affected=sorted(affected),before=object_hash(before),after=object_hash(after)))
    if result.identity is not None:
        from model3d_semantic_evidence import validate_identity
        report=validate_identity(result)
        require(not result.identity['strict'] or report['ok'],'MODEL3D_IDENTITY','Edit violates frozen measurable identity constraints.')
    validate_asset(result);return result


def select(asset,part_id):
    identifier(part_id);validate_asset(asset);require(part_id in asset.parts,'MODEL3D_PART_UNKNOWN','Unknown semantic part.')
    return deepcopy(asset.parts[part_id])


def transform_part(asset,part_id,transform,*,relative=False):
    select(asset,part_id);require(type(relative) is bool,'MODEL3D_PART_TRANSFORM','relative must be boolean.')
    result=deepcopy(asset);m=matrix(transform)
    result.parts[part_id]['matrix']=(matrix(result.parts[part_id]['matrix'])@m if relative else m).tolist()
    return _finish(asset,result,'transform:'+part_id)


def reparent_part(asset,part_id,parent,*,preserve_world=True):
    select(asset,part_id);require(parent is None or (isinstance(parent,str) and parent in asset.parts),'MODEL3D_PART_UNKNOWN','Unknown new parent.')
    require(type(preserve_world) is bool,'MODEL3D_PART_TRANSFORM','preserve_world must be boolean.')
    world=world_matrices(asset);result=deepcopy(asset);result.parts[part_id]['parent']=parent
    world_matrices(result)
    if preserve_world:
        result.parts[part_id]['matrix']=(np.linalg.inv(world[parent])@world[part_id] if parent is not None else world[part_id]).tolist()
    return _finish(asset,result,'reparent:'+part_id)


def set_visibility(asset,part_id,visible):
    select(asset,part_id);require(type(visible) is bool,'MODEL3D_PART_SCHEMA','visible must be boolean.')
    result=deepcopy(asset);result.parts[part_id]['visible']=visible
    return _finish(asset,result,'visibility:'+part_id)


def group_parts(asset,group_id,part_ids,*,parent=None):
    """Add an explicit transform-only root/group, preserving selected worlds."""
    validate_asset(asset);identifier(group_id)
    require(group_id not in asset.parts and len(asset.parts)<4096 and isinstance(part_ids,list) and bool(part_ids)
            and all(isinstance(key,str) and key in asset.parts for key in part_ids)
            and len(set(part_ids))==len(part_ids) and (parent is None or (isinstance(parent,str) and parent in asset.parts)),
            'MODEL3D_PART_GROUP','Choose a new group, explicit known children and optional known parent.')
    require(all(not any(ancestor in part_ids for ancestor in _ancestors(asset,key)) for key in part_ids),
            'MODEL3D_PART_GROUP','Select independent subtree roots, not both ancestor and descendant.')
    world=world_matrices(asset);result=deepcopy(asset)
    result.parts[group_id]=dict(id=group_id,parent=parent,meshes=[],matrix=np.eye(4).tolist(),pivot=[0.,0.,0.],
        visible=True,semantic='unknown',side='unknown',attachments=[],review='pending',
        evidence=[dict(kind='authored-native-transform-group',children=list(part_ids))])
    group_world=np.eye(4) if parent is None else world[parent]
    for key in part_ids:
        result.parts[key]['parent']=group_id;result.parts[key]['matrix']=(np.linalg.inv(group_world)@world[key]).tolist()
    return _finish(asset,result,'group:'+group_id)


def _ancestors(asset,key):
    result=[];parent=asset.parts[key]['parent']
    while parent is not None:result.append(parent);parent=asset.parts[parent]['parent']
    return result


def label_part(asset,part_id,*,semantic,side='unknown',pivot=None,attachments=None):
    select(asset,part_id);result=deepcopy(asset);part=result.parts[part_id]
    part.update(semantic=semantic,side=side)
    if pivot is not None:part['pivot']=deepcopy(pivot)
    if attachments is not None:part['attachments']=deepcopy(attachments)
    return _finish(asset,result,'authored-label:'+part_id)


def duplicate_part(asset,part_id,new_prefix):
    select(asset,part_id);identifier(new_prefix)
    subtree=[part_id];cursor=0
    while cursor<len(subtree):
        subtree += [key for key,part in asset.parts.items() if part['parent']==subtree[cursor]];cursor+=1
    require(len(asset.parts)+len(subtree)<=4096,'MODEL3D_PART_LIMIT','Duplicate exceeds part budget.')
    mids={mid for key in subtree for mid in asset.parts[key]['meshes']};known=asset.geometry.by_id()
    _edit_budget(asset,extra_vertices=sum(len(known[mid].positions) for mid in mids),
                 extra_triangles=sum(len(known[mid].triangles) for mid in mids))
    mapping={key:identifier(new_prefix if key==part_id else new_prefix+':'+key) for key in subtree}
    require(not set(mapping.values()) & set(asset.parts),'MODEL3D_PART_ID','Duplicate part identifier collides.')
    result=deepcopy(asset);known=asset.geometry.by_id();new_meshes=[]
    for key in subtree:
        part=deepcopy(asset.parts[key]);part['id']=mapping[key]
        if part['parent'] in mapping:part['parent']=mapping[part['parent']]
        for attachment in part['attachments']:
            if attachment['target'] in mapping:attachment['target']=mapping[attachment['target']]
        part['meshes']=[]
        for mid in asset.parts[key]['meshes']:
            mesh=deepcopy(known[mid]);mesh.id=identifier(new_prefix+':'+mid)
            require(mesh.id not in known and mesh.id not in {m.id for m in new_meshes},'MODEL3D_PART_ID','Duplicate mesh identifier collides.')
            part['meshes'].append(mesh.id);new_meshes.append(mesh)
        result.parts[part['id']]=part
        part['evidence'].append(dict(kind='authored-native-duplicate',source_part=key,
                                     source_meshes=asset.parts[key]['meshes'],target_meshes=list(part['meshes'])))
    result.geometry.meshes+=new_meshes
    return _finish(asset,result,'duplicate:'+part_id)


def replace_part(asset,part_id,meshes):
    select(asset,part_id)
    require(isinstance(meshes,list) and 0<len(meshes)<=4096 and all(isinstance(m,Mesh) for m in meshes),
            'MODEL3D_PART_BINDING','Supply real native replacement meshes with valid existing material slots.')
    for mesh in meshes:identifier(mesh.id)
    old=set(asset.parts[part_id]['meshes']);remaining={m.id for m in asset.geometry.meshes if m.id not in old}
    require(not {m.id for m in meshes}&remaining,'MODEL3D_PART_ID','Replacement mesh identifiers collide.')
    candidate=copy(asset.geometry);candidate.meshes=[m for m in asset.geometry.meshes if m.id not in old]+meshes
    validate_geometry(candidate);_edit_budget(SemanticAsset(candidate,{},evidence=asset.evidence))
    result=deepcopy(asset)
    result.geometry.meshes=[m for m in result.geometry.meshes if m.id not in old]+deepcopy(meshes)
    result.parts[part_id]['meshes']=[m.id for m in meshes]
    return _finish(asset,result,'replace:'+part_id)


def split_part(asset,part_id,groups):
    """Split a single native mesh by an explicit complete face partition."""
    select(asset,part_id);part=asset.parts[part_id]
    require(len(part['meshes'])==1 and isinstance(groups,dict) and 1<len(groups)<=4096,
            'MODEL3D_PART_SPLIT','Choose one mesh and at least two explicit face groups.')
    mesh=asset.geometry.by_id()[part['meshes'][0]];all_faces=[]
    for key,indices in groups.items():
        identifier(key);require(key not in asset.parts,'MODEL3D_PART_ID','Split part identifier collides.')
        require(isinstance(indices,list) and bool(indices) and all(type(i) is int and 0<=i<len(mesh.triangles) for i in indices),
                'MODEL3D_PART_SPLIT','Face partitions must use valid integer indices.')
        all_faces+=indices
    require(sorted(all_faces)==list(range(len(mesh.triangles))),'MODEL3D_PART_SPLIT','Partitions must cover every face exactly once.')
    result=deepcopy(asset);result.parts[part_id]['meshes']=[]
    result.geometry.meshes=[m for m in result.geometry.meshes if m.id!=mesh.id]
    for key,indices in groups.items():
        selected=mesh.triangles[indices];vertices,inverse=np.unique(selected,return_inverse=True)
        item=deepcopy(mesh);item.id=identifier(key+':mesh');item.positions=mesh.positions[vertices].copy()
        item.triangles=inverse.reshape(-1,3);item.attributes={k:v[vertices].copy() for k,v in mesh.attributes.items()}
        item.morphs={name:{k:v[vertices].copy() for k,v in target.items()} for name,target in mesh.morphs.items()}
        new=deepcopy(part);new.update(id=key,parent=part_id,meshes=[item.id],matrix=np.eye(4).tolist())
        new['evidence'].append(dict(kind='explicit-native-face-partition',source_part=part_id,
                                    source_mesh=mesh.id,source_faces=list(indices),target_mesh=item.id))
        result.parts[key]=new;result.geometry.meshes.append(item)
    return _finish(asset,result,'split:'+part_id)


def combine_parts(asset,part_ids,new_id):
    """Bake compatible sibling parts into one editable mesh in parent space."""
    validate_asset(asset);identifier(new_id)
    require(isinstance(part_ids,list) and len(part_ids)>1 and all(isinstance(key,str) for key in part_ids)
            and len(set(part_ids))==len(part_ids)
            and all(key in asset.parts for key in part_ids) and new_id not in asset.parts,
            'MODEL3D_PART_COMBINE','Choose unique known parts and a new identifier.')
    parents={asset.parts[key]['parent'] for key in part_ids}
    require(len(parents)==1,'MODEL3D_PART_COMBINE','Combined parts must be siblings.')
    require(len({asset.parts[key]['visible'] for key in part_ids})==1,
            'MODEL3D_PART_COMBINE','Combining differently visible parts would change represented visibility.')
    world=world_matrices(asset);parent=next(iter(parents));parent_world=np.eye(4) if parent is None else world[parent]
    known=asset.geometry.by_id();items=[]
    for key in part_ids:
        selected=[known[mid] for mid in asset.parts[key]['meshes']]
        require(bool(selected),'MODEL3D_PART_COMBINE','Transform-only groups cannot be combined as mesh parts.')
        items+=_transformed_meshes(asset.geometry,selected,np.linalg.inv(parent_world)@world[key])
    first=items[0]
    require(all(m.material==first.material and set(m.attributes)==set(first.attributes)
                and {k:sorted(v) for k,v in m.morphs.items()}=={k:sorted(v) for k,v in first.morphs.items()}
                and m.morph_defaults==first.morph_defaults for m in items),
            'MODEL3D_PART_COMBINE','Material/vertex/morph layouts must match; incompatible data is never dropped.')
    count=sum(len(m.positions) for m in items);offset=0;triangles=[]
    require(count<=DEFAULT_LIMITS['vertices'] and sum(len(m.triangles) for m in items)<=DEFAULT_LIMITS['triangles'],
            'MODEL3D_PART_LIMIT','Combined geometry exceeds native limits.')
    for item in items:triangles.append(item.triangles.astype(np.int64)+offset);offset+=len(item.positions)
    merged=deepcopy(first);merged.id=identifier(new_id+':mesh');merged.name=new_id;merged.region='';merged.source_node=-1
    merged.positions=np.concatenate([m.positions for m in items]);merged.triangles=np.concatenate(triangles)
    merged.attributes={k:np.concatenate([m.attributes[k] for m in items]) for k in first.attributes}
    merged.morphs={name:{k:np.concatenate([m.morphs[name][k] for m in items]) for k in target} for name,target in first.morphs.items()}
    result=deepcopy(asset);removed={mid for key in part_ids for mid in asset.parts[key]['meshes']}
    result.geometry.meshes=[m for m in result.geometry.meshes if m.id not in removed]+[merged]
    new=deepcopy(asset.parts[part_ids[0]]);new.update(id=new_id,parent=parent,meshes=[merged.id],matrix=np.eye(4).tolist(),
        semantic='unknown',side='unknown',pivot=[0.,0.,0.],attachments=[])
    new['evidence'].append(dict(kind='explicit-native-combination',source_parts={key:deepcopy(asset.parts[key]) for key in part_ids},
                               source_meshes=[m.id for m in items],target_mesh=merged.id))
    for key in part_ids:
        local=matrix(asset.parts[key]['matrix'])
        for attachment in asset.parts[key]['attachments']:
            item=deepcopy(attachment);item['id']=identifier(key+':'+item['id'])
            item['point']=(local[:3,:3]@np.asarray(item['point'])+local[:3,3]).tolist()
            if item['target'] in part_ids:item['target']=new_id
            new['attachments'].append(item)
    result.parts[new_id]=new
    for key in part_ids:del result.parts[key]
    for key,part in result.parts.items():
        if part['parent'] in part_ids:
            part['parent']=new_id;part['matrix']=(np.linalg.inv(parent_world)@world[key]).tolist()
        for attachment in part['attachments']:
            if attachment['target'] in part_ids:attachment['target']=new_id
    return _finish(asset,result,'combine:'+','.join(part_ids))


def change_material(asset,part_id,material):
    select(asset,part_id);require(isinstance(material,dict),'MODEL3D_PART_MATERIAL','Expected native PBR material.')
    result=deepcopy(asset);slot=len(result.geometry.materials);result.geometry.materials.append(deepcopy(material))
    for mid in result.parts[part_id]['meshes']:result.geometry.by_id()[mid].material=slot
    return _finish(asset,result,'material:'+part_id)


def define_pose(asset,name,transforms):
    identifier(name);require(isinstance(transforms,dict) and bool(transforms) and set(transforms)<=set(asset.parts),
            'MODEL3D_PART_POSE','Pose must reference explicit represented parts.')
    result=deepcopy(asset);result.poses[name]={key:matrix(m).tolist() for key,m in transforms.items()}
    return _finish(asset,result,'define-rigid-pose:'+name)


def apply_pose(asset,name):
    require(name in asset.poses,'MODEL3D_PART_POSE','Unknown rigid part pose.')
    result=deepcopy(asset)
    for key,m in result.poses[name].items():
        require(key in result.parts,'MODEL3D_PART_POSE','Pose is stale after a part edit.')
        result.parts[key]['matrix']=deepcopy(m)
    return _finish(asset,result,'apply-rigid-pose:'+name)


def register_derivative(asset,name,parts,artifact_sha256,*,bind_identity=True,bind_style=True):
    """Record explicit dependencies; a hash is evidence binding, not approval."""
    identifier(name)
    require(isinstance(parts,list) and bool(parts) and len(set(parts))==len(parts) and set(parts)<=set(asset.parts),
            'MODEL3D_PART_DEPENDENCY','Derivative must bind known unique native parts.')
    require(isinstance(artifact_sha256,str) and re.fullmatch(r'[0-9a-f]{64}',artifact_sha256) is not None
            and type(bind_identity) is bool and type(bind_style) is bool,
            'MODEL3D_PART_DEPENDENCY','Derivative requires an exact hash and explicit binding policy.')
    result=deepcopy(asset);signatures=part_signatures(asset)
    result.derivatives[name]=dict(parts=list(parts),artifact_sha256=artifact_sha256,stale=False,
        inputs={key:signatures[key] for key in parts},identity_sha256=object_hash(asset.identity) if bind_identity else None,
        style_sha256=object_hash(asset.style) if bind_style else None)
    return _finish(asset,result,'register-derivative:'+name)
