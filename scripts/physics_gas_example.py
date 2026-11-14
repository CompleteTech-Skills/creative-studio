"""Native mesh smoke/fire engineering fixture; no pretrained or painted output."""
from copy import deepcopy
import hashlib
import json

from physics_backend_example import build_inputs
from physics_gas import DEFAULT_SETTINGS


def build_gas_inputs(*, fire=False, wind=0., obstacle=False, frames=24, resolution=16):
    world,payloads=build_inputs(frames=max(25,frames))
    world['frame_range']['end']=frames
    world['solver']['required_categories']=['rigid-body','gas']
    world['solver']['reproducibility']='not-guaranteed'
    world['solver'].update(fixed_step=dict(numerator=1,denominator=24),substeps=1,iterations=1)
    world['components']=[dict(id='smoke-domain',category='gas',
        transform=dict(translation=[0,4,0],rotation=[0,0,0,1]),size=[4,4,4],
        resolution=[resolution]*3,density=1.225,viscosity=0,temperature=473.15,
        combustion=fire,emitters=['ball'],colliders=['floor'])]
    world['budgets'].update(volume_cells=resolution**3,gas_cells=resolution**3,
        memory_bytes=2*1024**3,cache_bytes=256*1024**2,temporary_bytes=256*1024**2,
        bake_file_bytes=256*1024**2,timeout_seconds=120)
    for body in world['bodies']:
        body.update(mode='static',authority='static')
        body['mass'].update(kilograms=0,inertia=[[0,0,0],[0,0,0],[0,0,0]])
    if obstacle:
        world['bodies'][1]['transform']['translation']=[0,4,0]
        scene=json.loads(payloads['physics-scene.json'])
        next(node for node in scene['nodes'] if node['id']=='floor')['transform']['translation']=[0,4,0]
        payloads['physics-scene.json']=json.dumps(scene,sort_keys=True,separators=(',',':')).encode()
        sha=hashlib.sha256(payloads['physics-scene.json']).hexdigest()
        world['scene']['sha256']=sha
        project=json.loads(payloads['project.json'])
        for field in ('scenes','dependency_lock'):
            for ref in project[field]:
                if ref['id']=='physics-scene':ref['sha256']=sha
        payloads['project.json']=json.dumps(project,sort_keys=True,separators=(',',':')).encode()
    if wind:
        world['forces']=[dict(id='wind',type='wind',bodies=['smoke-domain'],vector=[1,0,0],point=[0,4,0],
            frame_range=deepcopy(world['frame_range']),strength=wind,seed=1,categories=['gas'])]
    settings=deepcopy(DEFAULT_SETTINGS)
    settings.update(emission_end_frame=min(8,frames-1),render_frames=[0,frames//2,frames-1],dissipation_frames=6)
    return world,payloads,settings
