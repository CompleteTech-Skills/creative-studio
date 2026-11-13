"""Parametric geometry catalogs over the shared Scene3D generator and publisher.

These are editable geometric assemblies, not rendered artwork or material approval.
A modeled hollow cave is distinct from the heightfield generator.
"""
from copy import deepcopy
import math
import random
import numpy as np

from scene3d_geometry import Mesh, primitive, wall
from scene3d_graph import require
from scene3d_terrain import Heightfield, scatter

PRESETS={
 'castle':'fantasy','village':'fantasy','ruins':'fantasy','forest-clearing':'fantasy',
 'dungeon':'fantasy','temple':'fantasy','magical-laboratory':'fantasy','cavern':'fantasy',
 'fantasy-tower':'fantasy','mystical-landscape':'fantasy',
 'spaceship-interior':'science-fiction','hangar':'science-fiction','corridor':'science-fiction',
 'research-facility':'science-fiction','industrial-chamber':'science-fiction',
 'control-room':'science-fiction','space-station':'science-fiction','futuristic-city':'science-fiction',
 'landing-pad':'science-fiction','alien-landscape':'science-fiction',
 'modular-technical-structure':'science-fiction'
}


def catalog():
    """Return independent, bounded input schemas and explicit capability limits."""
    common=dict(type='object',additionalProperties=False,required=['preset'],properties={
        'preset':dict(type='string',enum=sorted(PRESETS)),
        'scale':dict(type='number',minimum=.1,maximum=10,default=1),
        'detail':dict(type='integer',minimum=6,maximum=32,default=12)})
    entries=[]
    for identifier,family in sorted(PRESETS.items()):
        schema=deepcopy(common);schema['properties']['preset']={'const':identifier}
        entries.append(dict(id=identifier,version=1,family=family,input_schema=schema,
            base_bounds=dict(minimum=[-40,-5,-40],maximum=[40,30,40]),maximum_nodes=128,
            node_types=['mesh_instance'],asset_dependencies=[],default_seed=0,deterministic=True,
            implementation='scene3d-catalog-v1',regeneration='Shared preserve/reject snapshot policy.',
            limitations=['Geometry assembly only; use the shared lookdev owner for materials and lights.',
                         'Cavern is a modeled hollow tube, not a heightfield or arbitrary cave network.']))
    return entries


def template_objects(recipe,build):
    """Compile a named assembly into ordinary local mesh objects, with no new publisher."""
    p=recipe['parameters']
    require(isinstance(p,dict) and not set(p)-{'preset','scale','detail'} and 'preset' in p,
            'Template needs preset and optional scale/detail.')
    preset=p['preset'];require(isinstance(preset,str) and preset in PRESETS,'Unknown environment template.')
    require(len(recipe['id'])<=24,'Template recipe IDs are limited to 24 characters to preserve child identity budgets.')
    scale=p.get('scale',1)
    require(type(scale) in (int,float) and math.isfinite(scale) and .1<=scale<=10,'Template scale must be in [0.1,10].')
    detail=p.get('detail',12)
    require(type(detail) in (int,float) and math.isfinite(detail) and detail==int(detail) and 6<=detail<=32,
            'Template detail must be an integer in [6,32].')
    detail=int(detail)
    result=[];labels=set();rng=random.Random(recipe['seed'])
    def direct(label,mesh,position=(0,0,0),rotation=None):
        require(label not in labels,'Duplicate template part.');labels.add(label)
        require(len(result)<recipe['budget'],'Template node budget exceeded.','SCENE3D_LAYOUT')
        result.append(dict(id=recipe['id']+'-'+label,mesh=mesh,position=list(position),rotation=rotation))
    def emit(label,kind,parameters,position=(0,0,0),rotation=None):
        require(label not in labels,'Duplicate template assembly.');labels.add(label)
        sub=dict(recipe,id=recipe['id']+'-'+label,generator=kind,parameters=parameters)
        for item in build(sub):
            require(len(result)<recipe['budget'],'Template node budget exceeded.','SCENE3D_LAYOUT')
            item['position']=[a+b for a,b in zip(item['position'],position)]
            if rotation is not None:
                require(item['rotation'] is None,'Rotating an authored assembly requires the scene transform owner.')
                item['rotation']=rotation
            result.append(item)
    def shape(label,kind,position=(0,0,0),rotation=None,**dimensions):
        emit(label,'primitive',dict(kind=kind,dimensions=dimensions),position,rotation)
    def rooms(label,position=(0,0,0),**parameters):
        emit(label,'rooms',dict(external_door=True,**parameters),position)
    def ground(width=40,depth=40,elevation=0,**parameters):
        emit('land','terrain',dict(width=width,depth=depth,resolution=17,elevation=elevation,**parameters))
    quarter=[0,0,math.sqrt(.5),math.sqrt(.5)]
    yaw=[0,math.sqrt(.5),0,math.sqrt(.5)]

    if preset=='village':
        ground()
        for i,(x,z) in enumerate(((-8,-6),(8,-6),(0,9))):
            rooms(f'house-{i}',(x,0,z),rooms=1,width=6,depth=6,height=3)
            shape(f'roof-{i}','ramp',(x,3.1,z),width=6.5,height=1.6,depth=6.5)
        shape('well','pipe',(0,.6,0),radius=1.4,inner_radius=1.05,height=1.2,segments=detail)
        shape('road','plane',(0,.02,0),width=2,depth=30)
    elif preset=='castle':
        ground()
        for i,(x,z) in enumerate(((-8,-8),(-8,8),(8,-8),(8,8))):
            shape(f'tower-{i}','cylinder',(x,4,z),radius=2,height=8,segments=detail)
            shape(f'top-{i}','cone',(x,9,z),radius=2.4,height=2,segments=detail)
        opening=[dict(x=6.5,y=0,width=3,height=3.5)]
        direct('gate',wall(16,5,.6,opening),(0,0,-8))
        direct('rear',wall(16,5,.6),(0,0,8))
        for i,x in enumerate((-8,8)):direct(f'side-{i}',wall(16,5,.6),(x,0,0),yaw)
        rooms('keep',rooms=1,width=7,depth=7,height=5)
    elif preset=='ruins':
        ground()
        for i in range(8):
            x=(i%4-1.5)*4;z=(i//4-.5)*8;height=rng.uniform(1.3,3.7)
            direct(f'broken-wall-{i}',wall(3,height,.5),(x,0,z))
            shape(f'fallen-{i}','beam',(x,.3,z+1.5),width=2.6,height=.6,depth=.7)
        for i,x in enumerate((-7,7)):shape(f'column-{i}','cylinder',(x,1.6,0),radius=.6,height=3.2,segments=detail)
    elif preset=='forest-clearing':
        mask=[[1 if abs(x-8)<=2 and abs(z-8)<=2 else 0 for x in range(17)] for z in range(17)]
        ground(elevation=.5,max_slope=30,mask=mask,scatter_count=12,spacing=3,exclusions=[[-5,-5,5,5]])
        shape('clearing','plane',(0,.05,0),width=8,depth=8)
        shape('stump','cylinder',(0,.4,0),radius=1,height=.8,segments=detail)
    elif preset=='dungeon':
        rooms('cells',rooms=4,width=5,depth=5,height=3,windows=False)
        for i in range(4):
            shape(f'plinth-{i}','box',(i*5,.25,1.8),width=1.2,height=.5,depth=1)
            shape(f'torch-{i}','cylinder',(i*5,2,-2.1),radius=.08,height=.6,segments=detail)
    elif preset=='temple':
        shape('foundation','platform',(0,.75,0),width=12,height=1.5,depth=12)
        shape('stairs','stairs',(0,0,-8),width=10,height=1.5,depth=4,steps=6)
        for i,(x,z) in enumerate(((-4,-4),(-4,0),(-4,4),(4,-4),(4,0),(4,4))):
            shape(f'column-{i}','cylinder',(x,4.5,z),radius=.45,height=6,segments=detail)
        shape('roof','platform',(0,7.8,0),width=13,height=.6,depth=13)
        rooms('sanctum',(0,1.5,0),rooms=1,width=5,depth=6,height=4)
    elif preset=='magical-laboratory':
        rooms('laboratory',rooms=2,width=7,depth=7,height=4)
        for i,x in enumerate((0,7)):
            shape(f'table-{i}','box',(x,.8,2),width=3,height=1.6,depth=1)
            shape(f'orbit-{i}','torus',(x,2,2),radius=.9,tube=.08,segments=detail,rings=8)
            shape(f'orb-{i}','sphere',(x,2,2),radius=.5,segments=detail,rings=8)
    elif preset=='cavern':
        # Hollow modeled stone has actual undercut surfaces; no heightfield claim.
        shape('modeled-cave','pipe',(0,5,0),rotation=quarter,radius=6,inner_radius=5,height=24,segments=detail)
        shape('walkable-floor','platform',(0,.15,0),width=24,height=.3,depth=5)
        for i,x in enumerate((-8,0,8)):
            shape(f'stalagmite-{i}','cone',(x,1,3.5),radius=.7,height=2,segments=detail)
    elif preset=='fantasy-tower':
        # A horseshoe ring leaves a genuine full-height entrance, unlike a solid cylinder.
        radius=4;thickness=.5;height=12
        for i in range(detail):
            angle=2*math.pi*i/detail
            if abs(math.atan2(math.sin(angle),math.cos(angle)))<.5:continue
            x,z=radius*math.cos(angle),radius*math.sin(angle)
            q=[0,math.sin((-angle)/2),0,math.cos((-angle)/2)]
            shape(f'wall-{i}','box',(x,height/2,z),rotation=q,width=thickness,height=height,depth=2*radius*math.tan(math.pi/detail))
        shape('floor','cylinder',(0,.15,0),radius=4,height=.3,segments=detail)
        shape('spire','cone',(0,14,0),radius=4.7,height=4,segments=detail)
        shape('stairs','stairs',(-1,0,0),width=1.2,height=8,depth=6,steps=16)
    elif preset=='mystical-landscape':
        ground(elevation=3,max_slope=25)
        for i,(x,y,z) in enumerate(((-7,6,-4),(5,8,2),(0,10,9))):
            shape(f'floating-{i}','cylinder',(x,y,z),radius=2,height=.4,segments=detail)
            shape(f'underside-{i}','cone',(x,y-1,z),rotation=[1,0,0,0],radius=2,height=2,segments=detail)
    elif preset in ('spaceship-interior','corridor','space-station'):
        count=4 if preset=='corridor' else 3
        rooms('spine',rooms=count,width=5,depth=5,height=3,windows=preset!='corridor')
        for i in range(count):
            shape(f'overhead-{i}','pipe',(i*5,2.7,1.9),rotation=quarter,radius=.15,inner_radius=.1,height=4.5,segments=detail)
            shape(f'panel-{i}','box',(i*5,.8,-1.8),width=1.8,height=1.6,depth=.6)
        if preset=='space-station':
            for i,x in enumerate((0,5,10)):shape(f'technical-ring-{i}','torus',(x,1.5,0),rotation=quarter,radius=4,tube=.25,segments=detail,rings=8)
    elif preset=='hangar':
        rooms('hangar',rooms=1,width=18,depth=14,height=8,door_width=8,door_height=6)
        shape('landing-base','platform',(2,.2,0),width=10,height=.4,depth=7)
        shape('vehicle-hull','cylinder',(2,3,0),rotation=quarter,radius=2,height=8,segments=detail)
        for i,(x,z) in enumerate(((-1,-1.5),(-1,1.5),(5,-1.5),(5,1.5))):
            shape(f'gear-{i}','beam',(x,1,z),width=.3,height=2,depth=.3)
    elif preset=='research-facility':
        rooms('labs',rooms=3,width=6,depth=6,height=4)
        for i in range(3):
            shape(f'bench-{i}','box',(i*6,.8,1.9),width=3,height=1.6,depth=1)
            shape(f'chamber-{i}','cylinder',(i*6,1.2,-1.8),radius=.7,height=2.4,segments=detail)
    elif preset=='industrial-chamber':
        rooms('factory',rooms=1,width=14,depth=12,height=6)
        for i,(x,z) in enumerate(((-3,-3),(-3,3),(3,-3),(3,3))):
            shape(f'machine-{i}','box',(x,1.2,z),width=2,height=2.4,depth=2)
        shape('services','curve',points=[[-5,4,-4],[5,4,-4],[5,4,4]],radius=.25,segments=detail)
    elif preset=='control-room':
        rooms('control',rooms=1,width=12,depth=10,height=4)
        shape('dais','cylinder',(0,.15,0),radius=2,height=.3,segments=detail)
        for i,(x,z) in enumerate(((-3,-3),(-3,0),(-3,3),(3,-3),(3,0),(3,3))):
            shape(f'console-{i}','ramp',(x,0,z),width=1.5,height=1.2,depth=1)
    elif preset=='futuristic-city':
        ground()
        for i in range(9):
            x=(i%3-1)*11;z=(i//3-1)*11;height=rng.uniform(4,10)
            rooms(f'block-{i}',(x,0,z),rooms=1,width=7,depth=7,height=height)
            shape(f'antenna-{i}','pipe',(x,height+1,z),radius=.1,inner_radius=.05,height=2,segments=detail)
    elif preset=='landing-pad':
        ground()
        shape('pad','cylinder',(0,.4,0),radius=9,height=.8,segments=detail)
        for i,(x,z) in enumerate(((-8,-8),(-8,8),(8,-8),(8,8))):
            shape(f'beacon-{i}','cylinder',(x,1.5,z),radius=.25,height=3,segments=detail)
    elif preset=='alien-landscape':
        ground(elevation=4,max_slope=25)
        terrain=Heightfield(width=40,depth=40,resolution=17,seed=recipe['seed'],elevation=4,max_slope=25)
        for i,point in enumerate(scatter(terrain,8,spacing=4,seed=recipe['seed'],clearance=1.2,max_slope=25)):
            height=rng.uniform(2,5)
            shape(f'crystal-{i}','cone',(point[0],point[1]+height/2,point[2]),radius=1.1,height=height,segments=detail)
    else: # modular technical structures
        shape('deck','platform',(0,.3,0),width=18,height=.6,depth=12)
        for i,(x,z) in enumerate(((-7,-4),(-7,4),(7,-4),(7,4))):
            shape(f'column-{i}','beam',(x,3,z),width=.5,height=6,depth=.5)
        for i,z in enumerate((-4,4)):shape(f'truss-{i}','beam',(0,6,z),width=15,height=.5,depth=.5)
        shape('service-pipe','curve',points=[[-7,5,-4],[7,5,-4],[7,5,4]],radius=.2,segments=detail)
    # Uniform scaling stays in the established matrix/mesh coordinate convention.
    for item in result:
        item['mesh'].vertices=(np.asarray(item['mesh'].vertices,dtype=float)*scale).tolist()
        item['mesh'].validate();item['position']=[v*scale for v in item['position']]
    require(len(result)<=128,'Template exceeds its declared node bound.','SCENE3D_LAYOUT')
    require(len({o['id'] for o in result})==len(result),'Template child identities collide.')
    return result
