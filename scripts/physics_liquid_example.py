"""Real native box meshes for a pouring-water engineering fixture."""
from copy import deepcopy

from physics_backend_example import _box,build_inputs
from physics_backends import canonical,digest
from physics_contracts import COORDINATES,validate_contract
from physics_liquid_settings import DEFAULT_SETTINGS
from scene3d_contracts import validate_document
from scene3d_example import node,SETTINGS,PROVENANCE


def build_liquid_inputs(*,frames=48,resolution=24,viscosity=0.,moving=False,container=True):
    world,_=build_inputs(frames=max(25,frames))
    world.update(id='pouring-water-fixture',assets=[],bodies=[],components=[])
    world['frame_range']['end']=frames
    world['solver'].update(fixed_step=dict(numerator=1,denominator=24),substeps=1,iterations=1,
        required_categories=['rigid-body','liquid'],reproducibility='not-guaranteed')
    world['budgets'].update(bodies=16,colliders=16,volume_cells=resolution**3,liquid_cells=resolution**3,
        memory_bytes=2*1024**3,cache_bytes=256*1024**2,temporary_bytes=256*1024**2,
        bake_file_bytes=256*1024**2,timeout_seconds=180,output_frames=64)
    payloads={};nodes=[];refs=[]
    boxes=[('inlet',[.36,.36,.36],[0,2.25,0])]
    if container:
        boxes += [('floor',[1.8,.1,1.8],[0,.05,0]),('left',[.1,1.,1.8],[-.85,.55,0]),
            ('right',[.1,1.,1.8],[.85,.55,0]),('front',[1.8,1.,.1],[0,.55,-.85]),('back',[1.8,1.,.1],[0,.55,.85])]
    if moving:boxes.append(('paddle',[.2,.6,1.2],[-.5,.45,0]))
    for identifier,size,translation in boxes:
        vertices,triangles=_box(size)
        obj=('# Procedural engineering fixture, not production artwork.\n'+''.join('v %.9g %.9g %.9g\n'%v for v in vertices)+
            ''.join('f %d %d %d\n'%tuple(x+1 for x in f) for f in triangles)).encode()
        path=identifier+'.obj';payloads[path]=obj;file=dict(path=path,sha256=digest(obj))
        asset=dict(schema_version=1,kind='scene3d-asset',id=identifier+'-asset',version='1.0.0',name=identifier+' mesh',asset_type='mesh',
            content=file,source=file,provenance=deepcopy(PROVENANCE),coordinate_system=COORDINATES,original_coordinate_system="right-handed Y-up meters",
            pivot=[0,0,0],meshes=[file],materials=[],bounds=dict(minimum=[-v/2 for v in size],maximum=[v/2 for v in size]),collision=None,
            sockets=[],components={},skeleton=None,animations=[],lods=[],dependencies=[],resources=dict(vertices=8,triangles=12,bytes=len(obj)),
            validation=[],review=dict(status='unreviewed',receipts=[]))
        validate_document(asset,'asset');payloads[identifier+'-asset.json']=canonical(asset)
        reference=dict(id=asset['id'],version=asset['version'],kind=asset['kind'],path=identifier+'-asset.json',sha256=digest(canonical(asset)))
        refs.append(reference)
        n=node(identifier,'mesh_instance');n['asset']=reference;n['transform']['translation']=translation;nodes.append(n)
        world['assets'].append(dict(id=asset['id'],version=asset['version'],kind='geometry',path=path,sha256=digest(obj)))
        world['bodies'].append(dict(id=identifier,category='rigid-body',mode='static',authority='static',parent=None,asset=asset['id'],
            transform=dict(translation=translation,rotation=[0,0,0,1]),mass=dict(kilograms=0,center_of_mass=[0,0,0],inertia=[[0,0,0]]*3),
            linear_velocity=[0,0,0],angular_velocity=[0,0,0],animation=None,
            colliders=[dict(id=identifier+'-collider',transform=dict(translation=[0,0,0],rotation=[0,0,0,1]),shape=dict(type='box',size=size),
                material='engineering',group=1,mask=2**32-1,sensor=False)]))
    scene=dict(schema_version=1,kind='scene3d-scene',id='liquid-scene',version='1.0.0',name='Pouring water',revision=0,
        description='Actual box source and container; liquid motion is computed by Mantaflow.',coordinate_system=COORDINATES,
        roots=[n['id'] for n in nodes],nodes=nodes,world=dict(coordinate_system=COORDINATES,background=[.1,.1,.1],exposure=0,environment=None,metadata={}),
        collections=[],layers=[],cameras=[],lights=[],recipes=[],dependencies=refs,bounds=None,shots=[],continuity=[],metadata={},provenance=deepcopy(PROVENANCE))
    validate_document(scene,'scene');payloads['liquid-scene.json']=canonical(scene)
    scene_ref=dict(id=scene['id'],version=scene['version'],kind=scene['kind'],path='liquid-scene.json',sha256=digest(canonical(scene)))
    project=dict(schema_version=1,kind='scene3d-project',id='liquid-project',version='1.0.0',name='Liquid engineering fixture',status='fixture',
        coordinate_system=COORDINATES,asset_catalog=None,assets=refs,characters=[],scenes=[scene_ref],shots=[],prefabs=[],animations=[],render_defaults=SETTINGS,
        budgets=dict(max_nodes=32,max_files=64,max_bytes=1024*1024,max_frames=128),created_at='2026-11-13T00:00:00Z',modified_at='2026-11-13T00:00:00Z',
        linked_2d_project=None,dependency_lock=[*refs,scene_ref],validation=[],provenance=deepcopy(PROVENANCE),metadata={})
    world['scene']={**scene_ref,'kind':'scene'}
    if moving:
        clip=dict(schema_version=1,kind='scene3d-animation',id='paddle-motion',version='1.0.0',duration_frames=frames,
            fps=deepcopy(world['fps']),tracks=[dict(target='paddle',property='translation',interpolation='linear',
                keys=[dict(frame=0,value=[-.5,.45,0]),dict(frame=frames-1,value=[.5,.45,0])])],bindings=[],dependencies=[],provenance=deepcopy(PROVENANCE))
        validate_document(clip,'animation');payloads['paddle-motion.json']=canonical(clip)
        r=dict(id=clip['id'],version=clip['version'],kind=clip['kind'],path='paddle-motion.json',sha256=digest(canonical(clip)))
        project['animations']=[r];project['dependency_lock'].append(r)
        next(b for b in world['bodies'] if b['id']=='paddle').update(mode='kinematic',authority='animation',animation=clip['id'])
        world['assets'].append({**r,'kind':'animation'})
    validate_document(project,'project');payloads['project.json']=canonical(project)
    world['components']=[dict(id='liquid-domain',category='liquid',transform=dict(translation=[0,1.5,0],rotation=[0,0,0,1]),
        size=[3.,3.,3.],resolution=[resolution]*3,density=1000,viscosity=viscosity,temperature=293.15,combustion=False,
        emitters=['inlet'],colliders=[b['id'] for b in world['bodies'] if b['id']!='inlet'],max_secondary_particles=0)]
    validate_contract(world)
    settings=deepcopy(DEFAULT_SETTINGS);settings.update(emission_end_frame=min(12,frames-1),render_frames=[0,frames//2,frames-1])
    return world,payloads,settings
