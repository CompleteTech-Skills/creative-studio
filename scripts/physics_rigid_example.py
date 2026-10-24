"""Simultaneous real sphere/cube/heavy-object engineering scene, not animation."""
from copy import deepcopy
import json
from physics_backend_example import build_inputs
from physics_contracts import validate_contract
from scene3d_contracts import validate_document
from physics_rigid import density_mass


def build_falling_inputs(*,backend='blender',frames=73):
    from physics_backends import canonical,digest
    world,payloads=build_inputs(backend=backend,frames=frames)
    box,box_payloads=build_inputs(backend=backend,dynamic_shape='box',frames=frames)
    cube_mesh=box_payloads['ball.obj'];payloads['cube.obj']=cube_mesh
    asset=json.loads(box_payloads['ball-asset.json']);asset['id']='cube-asset';asset['name']='Cube engineering mesh'
    for reference in [asset['content'],asset['source'],*asset['meshes']]:reference['path']='cube.obj'
    validate_document(asset,'asset');payloads['cube-asset.json']=canonical(asset)
    reference=dict(id='cube-asset',version='1.0.0',kind='scene3d-asset',path='cube-asset.json',sha256=digest(payloads['cube-asset.json']))
    world['assets'].append(dict(id='cube-asset',version='1.0.0',kind='geometry',path='cube.obj',sha256=digest(cube_mesh)))
    scene=json.loads(payloads['physics-scene.json']);scene['description']='Simultaneous real sphere, cube and heavy cube; solver-driven engineering fixture.'
    scene['nodes'][0]['transform']['translation']=[-2,3,0];world['bodies'][0]['transform']['translation']=[-2,3,0]
    box_scene=json.loads(box_payloads['physics-scene.json'])
    for identifier,translation,density in [('cube',[0,4,0],1),('heavy',[2,5,0],10)]:
        body=deepcopy(box['bodies'][0]);body['id']=identifier;body['asset']='cube-asset';body['transform']['translation']=translation
        body['colliders'][0]['id']=identifier+'-collider';body=density_mass(body,density)
        world['bodies'].append(body)
        node=deepcopy(box_scene['nodes'][0]);node['id']=identifier;node['name']=identifier+' engineering body';node['asset']=deepcopy(reference)
        node['transform']['translation']=translation;scene['nodes'].append(node);scene['roots'].append(identifier)
    scene['dependencies'].append(reference);validate_document(scene,'scene');payloads['physics-scene.json']=canonical(scene)
    scene_reference=dict(id=scene['id'],version=scene['version'],kind=scene['kind'],path='physics-scene.json',sha256=digest(payloads['physics-scene.json']))
    world['scene']={**scene_reference,'kind':'scene'}
    project=json.loads(payloads['project.json']);project['name']='Simultaneous native Falling Objects engineering fixture'
    project['assets'].append(reference);project['scenes']=[scene_reference]
    project['dependency_lock']=[r for r in project['dependency_lock'] if r['kind']!='scene3d-scene']+[reference,scene_reference]
    validate_document(project,'project');payloads['project.json']=canonical(project)
    validate_contract(world)
    return world,payloads


def build_stack_inputs(*,backend='blender'):
    from physics_backends import canonical,digest
    world,payloads=build_falling_inputs(backend=backend,frames=73)
    positions={'cube':[0,.5,0],'heavy':[0,1.5,0]}
    for body in world['bodies']:
        if body['id'] in positions:body['transform']['translation']=positions[body['id']]
    scene=json.loads(payloads['physics-scene.json'])
    for node in scene['nodes']:
        if node['id'] in positions:node['transform']['translation']=positions[node['id']]
    scene['description']='Actual two-cube mass-ratio10 stack and independent falling sphere; engineering fixture.'
    validate_document(scene,'scene');payloads['physics-scene.json']=canonical(scene)
    reference={**world['scene'],'kind':'scene3d-scene','sha256':digest(payloads['physics-scene.json'])}
    world['scene']={**reference,'kind':'scene'}
    project=json.loads(payloads['project.json']);project['scenes']=[reference]
    project['dependency_lock']=[reference if r['kind']=='scene3d-scene' else r for r in project['dependency_lock']]
    validate_document(project,'project');payloads['project.json']=canonical(project)
    validate_contract(world)
    return world,payloads
