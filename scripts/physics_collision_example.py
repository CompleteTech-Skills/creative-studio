"""Real collision playground inputs; visual source meshes and proxies are distinct."""
from copy import deepcopy
import json
import math

from physics_backend_example import build_inputs, _box


def build_collision_inputs(*, frames=97):
    from physics_backends import canonical, digest
    from physics_contracts import validate_contract
    from scene3d_contracts import validate_document
    world, payloads = build_inputs(backend='pybullet', frames=frames)
    world['collision_profile'] = 'explicit-proxies-v1'
    scene = json.loads(payloads['physics-scene.json'])
    prototype = deepcopy(world['bodies'][0]); node = deepcopy(scene['nodes'][0])
    floor = world['bodies'][1]
    floor['colliders'][0]['shape']['size'] = [24,.2,14]
    world['materials'][0]['restitution'] = .7
    world['materials'][0]['dynamic_friction'] = world['materials'][0]['static_friction'] = .5
    world['bodies'] = [floor]
    scene['nodes'] = [scene['nodes'][1]]; scene['roots'] = ['floor']

    def add(identifier, shape, position, *, velocity=(0,0,0), static=False, rotation=(0,0,0,1), material='engineering'):
        body=deepcopy(prototype);body['id']=identifier;body['transform']=dict(translation=list(position),rotation=list(rotation))
        body['colliders'][0].update(id=identifier+'-collider',shape=shape,material=material)
        body['linear_velocity']=list(velocity)
        body['mass']['inertia']=[[.2,0,0],[0,.2,0],[0,0,.2]]
        if static:
            body.update(mode='static',authority='static');body['mass'].update(kilograms=0,inertia=[[0,0,0],[0,0,0],[0,0,0]])
        visual=deepcopy(node);visual.update(id=identifier,name=identifier+' collision fixture');visual['transform']={**deepcopy(body['transform']),'scale':[1,1,1]}
        world['bodies'].append(body);scene['nodes'].append(visual);scene['roots'].append(identifier)
        return body

    add('bounce',dict(type='sphere',radius=.5),[-3,4,-2])
    angle=-.25
    add('ramp',dict(type='box',size=[5,.2,3]),[-6,1,0],static=True,rotation=[0,0,math.sin(angle/2),math.cos(angle/2)])
    add('slider',dict(type='box',size=[.7,.7,.7]),[-7,2.2,0],velocity=[1,0,0])
    add('roller',dict(type='cylinder',radius=.4,height=.8,axis='Z'),[-1,3,2],velocity=[2,0,0])
    add('left-ball',dict(type='sphere',radius=.5),[-2,.55,-4],velocity=[3,0,0])
    add('right-ball',dict(type='sphere',radius=.5),[2,.55,-4],velocity=[-3,0,0])
    add('excluded',dict(type='sphere',radius=.4),[5,3,0])
    world['collision_exclusions']=[['excluded','floor']]
    add('thin-wall',dict(type='box',size=[.02,3,2]),[0,2,5],static=True)
    add('projectile',dict(type='sphere',radius=.05),[-7,2,5],velocity=[25,0,0])
    vertices,triangles=_box([.8,.8,.8])
    add('hull',dict(type='convex-hull',source='ball-asset',vertices=[list(v) for v in vertices],triangles=[list(t) for t in triangles]),[7,3,-3])
    add('compound',dict(type='compound',children=[dict(transform=dict(translation=[x,0,0],rotation=[0,0,0,1]),
        shape=dict(type='sphere',radius=.3)) for x in [-.5,.5]]),[3,4,2])
    scene.update(name='Collision Playground',description='Explicit native proxies; dynamic contacts, ramp, filters, hull, compound and high-speed tunneling probe.')
    validate_document(scene,'scene');payloads['physics-scene.json']=canonical(scene)
    reference={**world['scene'],'sha256':digest(payloads['physics-scene.json'])}
    world['scene']=reference
    project=json.loads(payloads['project.json']);project['scenes']=[{**reference,'kind':'scene3d-scene'}]
    project['dependency_lock']=[{**reference,'kind':'scene3d-scene'} if item['kind']=='scene3d-scene' else item for item in project['dependency_lock']]
    validate_document(project,'project');payloads['project.json']=canonical(project)
    validate_contract(world)
    return world,payloads
