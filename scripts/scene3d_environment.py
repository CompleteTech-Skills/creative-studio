"""Procedural Scene3D recipes with immutable snapshots and explicit regeneration conflicts.

Meshes remain editable scene nodes, and hash-bound recipes are separate resources.
There is no implicit renderer, material translator, cave generator or AI dependency.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from scene3d_contracts import COORDINATES, canonical_bytes, load_project, validate_document
from scene3d_example import node, build_example
from scene3d_geometry import primitive, wall, count, positive
from scene3d_graph import SceneGraph, require, _inventories
from scene3d_snapshot import snapshot_project
from scene3d_terrain import Heightfield, scatter


def encode(value):
    if isinstance(value, dict) and str(value.get('kind', '')).startswith('scene3d-'):
        return canonical_bytes(value)
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def fingerprint(value):
    value=deepcopy(value)
    value['metadata'].pop('generated_fingerprint',None)
    return digest(value)


def plan(recipe):
    """Validate a complete recipe and compute actual geometry before touching a scene."""
    require(isinstance(recipe,dict) and set(recipe)=={'id','version','seed','generator','parameters','budget'},'Invalid recipe fields.')
    identifier=recipe['id']
    require(isinstance(identifier,str) and identifier.isascii() and identifier.replace('-','').isalnum() and len(identifier)<=48,'Invalid recipe ID.')
    require(recipe['version']==1 and type(recipe['version']) is int,'Unsupported generator version.')
    require(type(recipe['seed']) is int and 0<=recipe['seed']<2**32,'Invalid generator seed.')
    count(recipe['budget'],'node budget',1,1000)
    p=recipe['parameters'];require(isinstance(p,dict),'Parameters must be an object.')
    objects=[]
    def add(label,mesh,position=(0,0,0),rotation=None):
        require(len(objects)<recipe['budget'],'Generator node budget exceeded.','SCENE3D_LAYOUT')
        mesh.validate();objects.append(dict(id=identifier+'-'+label,mesh=mesh,position=list(position),rotation=rotation))
    kind=recipe['generator'];require(isinstance(kind,str),'Generator must be text.')
    if kind=='primitive':
        require(set(p)=={'kind','dimensions'} and isinstance(p['kind'],str) and isinstance(p['dimensions'],dict),
                'Primitive requires a text kind and dimensions object.')
        add('mesh',primitive(p['kind'],**p['dimensions']))
    elif kind=='terrain':
        allowed={'width','depth','resolution','elevation','max_slope','heights','smooth','paths','scatter_count','spacing','scatter_slope','exclusions'}
        require(not set(p)-allowed,'Unknown terrain parameter.')
        terrain=Heightfield(seed=recipe['seed'],**{k:v for k,v in p.items() if k in {'width','depth','resolution','elevation','max_slope','heights'}})
        if p.get('smooth',0):terrain.smooth(p['smooth'])
        paths=p.get('paths',[])
        require(isinstance(paths,list) and len(paths)<=8,'Terrain supports at most eight paths.')
        require(all(isinstance(path,dict) and isinstance(path.get('points'),list) for path in paths)
                and sum(len(path['points']) for path in paths)<=64,'Path vertex budget exceeded.')
        for path in paths:
            require(isinstance(path,dict) and set(path)=={'points','width','elevation'},'Invalid terrain path.')
            terrain.path(**path)
        add('ground',terrain.mesh())
        total=count(p.get('scatter_count',0),'scatter count',0,10000)
        positions=[] if total==0 else scatter(terrain,total,spacing=p.get('spacing',1.3),seed=recipe['seed'],clearance=.65,
                        max_slope=p.get('scatter_slope',30),exclusions=p.get('exclusions',()))
        for i,position in enumerate(positions):
            # Reusable low-polygon tree: actual trunk and crown meshes, separately editable.
            add(f'trunk-{i}',primitive('cylinder',radius=.12,height=1.2,segments=8),[position[0],position[1]+.6,position[2]])
            add(f'crown-{i}',primitive('cone',radius=.65,height=1.6,segments=8),[position[0],position[1]+1.8,position[2]])
    elif kind in ('rooms','workshop','fantasy','science-fiction'):
        require(not set(p)-{'rooms','width','depth','height','thickness','door_width','door_height','windows'},'Unknown architecture parameter.')
        rooms=count(p.get('rooms',2),'rooms',1,20)
        w,d,h,t=[positive(p.get(k,v),k) for k,v in [('width',5),('depth',5),('height',3),('thickness',.2)]]
        dw=positive(p.get('door_width',1.2),'door width');dh=positive(p.get('door_height',2.2),'door height')
        require(dw<w-2*t and dh<h and t<min(w,d)/4,'Impossible room opening or wall thickness.','SCENE3D_LAYOUT')
        require(type(p.get('windows',True)) is bool,'Windows must be boolean.')
        # Shared walls are emitted once; doorway continuity is structural, not approximate adjacency.
        door=[dict(x=(d-dw)/2,y=0,width=dw,height=dh)]
        for i in range(rooms):
            x=i*w
            add(f'floor-{i}',primitive('box',width=w,height=t,depth=d),[x,-t/2,0])
            add(f'ceiling-{i}',primitive('box',width=w,height=t,depth=d),[x,h+t/2,0])
            windows=[dict(x=w*.35,y=h*.5,width=w*.3,height=h*.25)] if p.get('windows',True) else []
            for side in (-1,1):add(f'outer-{i}-{side}',wall(w,h,t,windows),[x,0,side*d/2])
            if kind=='fantasy':
                for side in (-1,1):add(f'tower-{i}-{side}',primitive('cylinder',radius=.6,height=h+1,segments=12),[x-w/2,(h+1)/2,side*d/2])
            if kind=='science-fiction':
                add(f'beam-{i}',primitive('beam',width=.2,height=h,depth=.25),[x,h/2,0])
        # Along X, these walls are rotated to occupy the YZ plane.
        q=[0,2**-.5,0,2**-.5]
        for i in range(rooms+1):
            openings=door if 0<i<rooms else []
            add(f'partition-{i}',wall(d,h,t,openings),[(i-.5)*w,0,0],q)
    else:
        require(False,'Unsupported generator; heightfields do not model caves or overhangs.','SCENE3D_GENERATOR')
    require(sum(len(o['mesh'].triangles) for o in objects)<=200000,'Aggregate triangle budget exceeded.')
    return objects


def _prepare_recipe(bundle,scene,recipe,*,policy='preserve',offset=None,parent=None):
    """Prepare one recipe entirely in memory; publication is owned by apply_recipes."""
    require(policy in ('preserve','reject'),'Regeneration policy must be preserve or reject.')
    objects=plan(recipe);scene=deepcopy(scene)
    offset=[0,0,0] if offset is None else offset
    require(isinstance(offset,(list,tuple)) and len(offset)==3 and all(type(x) in (int,float) and math.isfinite(x) and abs(x)<=1000000 for x in offset),'Offset must be a bounded finite vector.')
    require(parent is None or isinstance(parent,str),'Parent must be a node ID or null.')
    require(scene['kind']=='scene3d-scene','Select a scene manifest.')
    rid=recipe['id'];old={n['id']:n for n in scene['nodes'] if n['metadata'].get('generator_id')==rid}
    records=scene['metadata'].get('generator_nodes_'+rid,'[]');previous=json.loads(records)
    require(isinstance(previous,list) and all(isinstance(i,str) for i in previous),'Invalid generator node history.')
    modified={i for i,n in old.items() if fingerprint(n)!=n['metadata'].get('generated_fingerprint')}
    tombstones=json.loads(scene['metadata'].get('generator_deleted_'+rid,'[]'))
    require(isinstance(tombstones,list) and all(isinstance(i,str) for i in tombstones),'Invalid generator deletion history.')
    deleted=(set(previous)|set(tombstones))-set(old)
    conflicts=sorted(modified|deleted)
    require(policy!='reject' or not conflicts,'Generated nodes contain manual overrides: '+', '.join(conflicts),'SCENE3D_GENERATOR_CONFLICT')
    scene['nodes']=[n for n in scene['nodes'] if n['id'] not in old or n['id'] in modified]
    retained={n['id'] for n in scene['nodes']};payloads={};documents={};refs=[]
    for item in objects:
        identifier=item['id']
        if identifier in modified or identifier in deleted:continue
        require(identifier not in retained,'Generated node ID collides with authored node.','SCENE3D_GENERATOR_CONFLICT')
        mesh=item['mesh'];raw=mesh.obj();sha=hashlib.sha256(raw).hexdigest();path='generated/mesh-'+sha+'.obj'
        payloads[path]=raw;file=dict(path=path,sha256=sha)
        asset=dict(schema_version=1,kind='scene3d-asset',id='mesh-'+sha[:32],version='1.0.0',name=identifier,
            asset_type='mesh',content=file,source=file,provenance=dict(origin='authored',fixture=False,tool='scene3d-generator-v1',
            inputs=[],attribution='Procedural scene geometry',license='CC0-1.0'),coordinate_system=COORDINATES,
            original_coordinate_system='right-handed Y-up meters',pivot=[0,0,0],meshes=[file],materials=[],bounds=mesh.bounds,
            collision=None,sockets=[],components={},skeleton=None,animations=[],lods=[],dependencies=[],
            resources=dict(vertices=len(mesh.vertices),triangles=len(mesh.triangles),bytes=len(raw)),validation=[],review=dict(status='unreviewed',receipts=[]))
        validate_document(asset,'asset');asset_path='generated/'+asset['id']+'.json';asset_sha=digest(asset)
        # Geometry may repeat with different labels; asset identity binds the mesh, not node label.
        asset['name']=asset['id'];asset_sha=digest(asset)
        reference=dict(path=asset_path,sha256=asset_sha,id=asset['id'],version='1.0.0',kind='scene3d-asset')
        if asset_path not in bundle['documents']:documents[asset_path]=asset
        else:require(bundle['documents'][asset_path]==asset,'Generated asset identity collision.')
        refs.append(reference)
        n=node(identifier,'mesh_instance',parent);n['asset']=reference
        n['transform']['translation']=[a+b for a,b in zip(item['position'],offset)]
        if item['rotation'] is not None:n['transform']['rotation']=item['rotation']
        n['bounds']=mesh.bounds;n['metadata']={'generator_id':rid,'generator_version':1}
        n['metadata']['generated_fingerprint']=fingerprint(n);scene['nodes'].append(n)
    persisted=deepcopy(recipe) if offset==[0,0,0] and parent is None else dict(recipe=recipe,placement=dict(offset=list(offset),parent=parent))
    recipe_raw=encode(persisted);recipe_path='generated/recipe-'+digest(persisted)+'.json';payloads[recipe_path]=recipe_raw
    old_recipe=scene['metadata'].get('generator_recipe_'+rid)
    scene['recipes']=[r for r in scene['recipes'] if r['path']!=old_recipe]+[dict(path=recipe_path,sha256=digest(persisted))]
    scene['metadata']['generator_recipe_'+rid]=recipe_path
    scene['metadata']['generator_deleted_'+rid]=json.dumps(sorted(deleted),separators=(',',':'))
    require(len(scene['metadata']['generator_deleted_'+rid])<=4096,'Generator deletion history exceeds metadata budget.')
    scene['metadata']['generator_nodes_'+rid]=json.dumps([o['id'] for o in objects],separators=(',',':'))
    require(len(scene['metadata']['generator_nodes_'+rid])<=4096,'Generator history exceeds metadata budget.')
    existing={(r['id'],r['version']):r for r in scene['dependencies']}
    for ref in refs:existing[(ref['id'],ref['version'])]=ref
    scene['dependencies']=list(existing.values());_inventories(scene)
    return dict(scene=scene,documents=documents,payloads=payloads,conflicts=conflicts,
                generated=len(objects)-len(set(o['id'] for o in objects)&(modified|deleted)),
                triangles=sum(len(o['mesh'].triangles) for o in objects))


def apply_recipes(source,destination,scene_path,recipes,*,policy='preserve',offsets=None,parents=None):
    """Prepare up to 16 named recipes, validate together, and publish one snapshot.

    Offsets are local translations and parents are exact node IDs. Both are
    recorded beside each recipe; existing SceneGraph owns hierarchy evaluation.
    A failure in any recipe leaves the source and destination unchanged.
    """
    require(isinstance(recipes,(list,tuple)) and 1<=len(recipes)<=16,'A batch contains one to sixteen recipes.')
    require(all(isinstance(r,dict) and isinstance(r.get('id'),str) for r in recipes),'Recipes need text IDs.')
    ids=[r['id'] for r in recipes];require(len(set(ids))==len(ids),'Duplicate recipe IDs in one batch.')
    offsets={} if offsets is None else offsets;parents={} if parents is None else parents
    require(isinstance(offsets,dict) and isinstance(parents,dict) and not (set(offsets)|set(parents))-set(ids),'Placement maps must select batch recipe IDs.')
    bundle=load_project(source)
    require(scene_path in bundle['documents'],'Unknown selected scene.')
    scene=deepcopy(bundle['documents'][scene_path]);documents={};payloads={};conflicts=[];generated=0;triangles=0
    context=dict(bundle);context['documents']=dict(bundle['documents'])
    for recipe in recipes:
        prepared=_prepare_recipe(context,scene,recipe,policy=policy,offset=offsets.get(recipe['id']),parent=parents.get(recipe['id']))
        scene=prepared['scene'];generated+=prepared['generated'];triangles+=prepared['triangles']
        require(len(scene['nodes'])<=10000 and triangles<=200000,'Aggregate generator batch budget exceeded.')
        for path,raw in prepared['payloads'].items():
            require(path not in payloads or payloads[path]==raw,'Generated payload collision.')
            payloads[path]=raw
        documents.update(prepared['documents']);context['documents'].update(prepared['documents']);conflicts+=prepared['conflicts']
    scene['revision']+=1
    graph=SceneGraph(scene)
    result=snapshot_project(source,destination,graph,scene_path=scene_path,derived_documents=documents,extra_payloads=payloads)
    return dict(snapshot=result,conflicts=sorted(set(conflicts)),generated=generated)


def apply_recipe(source,destination,scene_path,recipe,*,policy='preserve',offset=None,parent=None):
    """Publish one recipe; preserve changed nodes and persistent deleted-node tombstones."""
    identifier=recipe.get('id') if isinstance(recipe,dict) else None
    return apply_recipes(source,destination,scene_path,[recipe],policy=policy,
                         offsets=None if offset is None else {identifier:offset},
                         parents=None if parent is None else {identifier:parent})


def create_environments(destination,recipes,*,offsets=None,parents=None):
    """Create a first project from an atomic batch through the existing snapshot owner."""
    destination=Path(destination).absolute()
    with tempfile.TemporaryDirectory(prefix='scene3d-generator-') as temp:
        source=Path(temp)/'source';build_example(source)
        project=json.loads((source/'project.json').read_text())
        project['budgets'].update(max_nodes=10000,max_files=4096,max_bytes=256*1024*1024)
        (source/'project.json').write_bytes(canonical_bytes(project))
        return apply_recipes(source,destination,'example-scene.json',recipes,offsets=offsets,parents=parents)


def create_environment(destination,recipe):
    """Create a first editable project containing one generated environment."""
    return create_environments(destination,[recipe])


def load_meshes(root, scene_path):
    """Read verified generator meshes keyed by node ID, in node-local coordinates.

    General imported assets remain owned by the import/evaluation adapter. This
    adapter accepts only the exact bounded triangular OBJ grammar emitted here.
    """
    from asset_library import read_source_bytes
    from scene3d_geometry import Mesh
    root=Path(root);bundle=load_project(root)
    scene=bundle['documents'][scene_path];require(scene['kind']=='scene3d-scene','Select a scene.')
    result={}
    for n in scene['nodes']:
        if 'generator_id' not in n['metadata'] or 'asset' not in n:continue
        asset=bundle['documents'][n['asset']['path']]
        require(asset['provenance']['tool']=='scene3d-generator-v1','Generated node uses a foreign geometry owner.')
        content=asset['content'];record=bundle['inventory'][content['path']]
        raw=read_source_bytes(root/content['path'],record['bytes'])
        require(len(raw)==record['bytes'] and hashlib.sha256(raw).hexdigest()==content['sha256'],'Geometry changed after validation.')
        vertices=[];triangles=[]
        for line in raw.decode('ascii').splitlines():
            parts=line.split()
            if not parts or parts[0]=='#':continue
            if parts[0]=='v':
                require(len(parts)==4,'Malformed generated vertex.')
                vertices.append([float(x) for x in parts[1:]])
            elif parts[0]=='f':
                require(len(parts)==4,'Generated mesh must be triangular.')
                triangles.append([int(x.split('/')[0])-1 for x in parts[1:]])
            else:require(parts[0] in ('vn','vt'),'Unexpected generated OBJ record.')
        mesh=Mesh(vertices,triangles).validate()
        result[n['id']]=dict(vertices=mesh.vertices,triangles=mesh.triangles)
    return result
