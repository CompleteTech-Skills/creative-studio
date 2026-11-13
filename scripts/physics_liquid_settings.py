"""Strict additive Mantaflow liquid settings; no solver is imported here."""
from copy import deepcopy
from fractions import Fraction
import math

import physics_backends as core

DEFAULT_SETTINGS = dict(schema_version=1, kind='hatch-3d-liquid-settings',
    emission_end_frame=12, flow_behavior='INFLOW', initial_velocity=[0.,0.,0.],
    particle_radius=1., mesh_scale=1, collision_distance=1.5,
    cfl=2., minimum_substeps=1, maximum_substeps=4,
    boundaries=dict(front=True,back=True,left=True,right=True,top=True,bottom=True),
    render_frames=[0,23,47], render_size=128, render_samples=8)


def schemas():
    props=dict(schema_version={'const':1},kind={'const':'hatch-3d-liquid-settings'},
        emission_end_frame=dict(type='integer',minimum=1,maximum=63),
        flow_behavior={'enum':['INFLOW','GEOMETRY']},
        initial_velocity=dict(type='array',minItems=3,maxItems=3,items=dict(type='number',minimum=-20,maximum=20)),
        particle_radius=dict(type='number',minimum=.5,maximum=2),
        mesh_scale=dict(type='integer',minimum=1,maximum=2),
        collision_distance=dict(type='number',minimum=.001,maximum=10),
        cfl=dict(type='number',minimum=.1,maximum=10),
        minimum_substeps=dict(type='integer',minimum=1,maximum=16),
        maximum_substeps=dict(type='integer',minimum=1,maximum=16),
        boundaries=dict(type='object',additionalProperties=False,properties={k:{'type':'boolean'} for k in DEFAULT_SETTINGS['boundaries']},required=list(DEFAULT_SETTINGS['boundaries'])),
        render_frames=dict(type='array',minItems=1,maxItems=8,uniqueItems=True,items=dict(type='integer',minimum=0,maximum=63)),
        render_size=dict(type='integer',minimum=64,maximum=256),render_samples=dict(type='integer',minimum=1,maximum=16))
    return {'physics-liquid-settings.schema.json':dict(**{'$schema':'https://json-schema.org/draft/2020-12/schema'},
        title='Hatch native liquid settings',type='object',additionalProperties=False,properties=props,required=list(props))}


def validate_settings(value, frames):
    value=core.parse(core.canonical(value))
    core.require(type(value) is dict and set(value)==set(DEFAULT_SETTINGS),
        'PHYSICS_LIQUID_SETTINGS','Liquid settings require the exact versioned fields.')
    core.require(type(value['schema_version']) is int and value['schema_version']==1 and value['kind']=='hatch-3d-liquid-settings',
        'PHYSICS_LIQUID_SETTINGS','Unknown liquid settings version.')
    for key,low,high in [('emission_end_frame',1,frames-1),('mesh_scale',1,2),('minimum_substeps',1,16),
        ('maximum_substeps',1,16),('render_size',64,256),('render_samples',1,16)]:
        core.require(type(value[key]) is int and low<=value[key]<=high,'PHYSICS_LIQUID_SETTINGS','Invalid integer setting: '+key)
    for key,low,high in [('particle_radius',.5,2),('collision_distance',.001,10),('cfl',.1,10)]:
        core.require(type(value[key]) in (int,float) and math.isfinite(value[key]) and low<=value[key]<=high,
            'PHYSICS_LIQUID_SETTINGS','Invalid scalar setting: '+key)
    v=value['initial_velocity']
    core.require(type(v) is list and len(v)==3 and all(type(x) in (int,float) and math.isfinite(x) and abs(x)<=20 for x in v),
        'PHYSICS_LIQUID_SETTINGS','Invalid initial velocity.')
    core.require(value['flow_behavior'] in ('INFLOW','GEOMETRY') and value['minimum_substeps']<=value['maximum_substeps'],
        'PHYSICS_LIQUID_SETTINGS','Unsupported flow or adaptive-step policy.')
    b=value['boundaries']
    core.require(type(b) is dict and set(b)==set(DEFAULT_SETTINGS['boundaries']) and all(type(x) is bool for x in b.values()),
        'PHYSICS_LIQUID_SETTINGS','Every domain boundary requires an explicit boolean.')
    r=value['render_frames']
    core.require(type(r) is list and 1<=len(r)<=8 and all(type(x) is int and 0<=x<frames for x in r) and len(set(r))==len(r),
        'PHYSICS_LIQUID_SETTINGS','Invalid render frame selection.')
    return value


def admit(world, settings):
    world=core._world(world)
    frames=world['frame_range']['end']
    core.require(world['frame_range']['start']==0 and 2<=frames<=64 and world['solver']['backend']=='blender' and
        set(world['solver']['required_categories'])=={'rigid-body','liquid'},
        'PHYSICS_LIQUID_UNSUPPORTED','Liquid jobs require 2..64 zero-origin frames and explicit static/kinematic body plus liquid categories.')
    core.require(len(world['components'])==1 and world['components'][0]['category']=='liquid' and not world['joints'] and not world['events'] and not world['forces'],
        'PHYSICS_LIQUID_UNSUPPORTED','This adapter supports one liquid domain without rigid joints/events/force fields.')
    d=world['components'][0]
    fps=world['fps']
    core.require(1<=Fraction(fps['numerator'],fps['denominator'])<=240 and
        fps['numerator']<=32767 and fps['denominator']<=1000000,
        'PHYSICS_LIQUID_UNSUPPORTED','Liquid output clocks must be 1..240 fps with exactly representable native fps/base fields.')
    core.require(d['transform']['rotation']==[0,0,0,1] and len(set(d['size']))==1 and len(set(d['resolution']))==1 and
        8<=d['resolution'][0]<=64 and .1<=d['size'][0]<=100 and all(abs(x)<=1000 for x in d['transform']['translation']),
        'PHYSICS_LIQUID_UNSUPPORTED','Liquid domain must be an unrotated cube with bounded 8..64 resolution.')
    core.require(not d['combustion'] and d['max_secondary_particles']==0 and d['density']==1000 and d['temperature']==293.15 and 0<=d['viscosity']<=1,
        'PHYSICS_LIQUID_UNSUPPORTED','Only normalized incompressible water density, explicit viscosity and no secondary particles are supported.')
    core.require(Fraction(**world['solver']['fixed_step'])==Fraction(world['fps']['denominator'],world['fps']['numerator']) and
        world['solver']['substeps']==1 and world['solver']['iterations']==1 and 'stability' not in world['solver'],
        'PHYSICS_LIQUID_UNSUPPORTED','Liquid timing uses the output interval and separately recorded adaptive CFL settings.')
    ids={b['id'] for b in world['bodies']};roles=d['emitters']+d['colliders']
    core.require(1<=len(ids)<=16 and set(roles)==ids and len(roles)==len(set(roles)),
        'PHYSICS_LIQUID_UNSUPPORTED','Each native source body needs exactly one emitter or obstacle role.')
    for b in world['bodies']:
        core.require(b['mode'] in ('static','kinematic') and b['parent'] is None and
            b['linear_velocity']==[0,0,0] and b['angular_velocity']==[0,0,0] and
            (b['mode']!='kinematic' or b['id'] in d['colliders']),
            'PHYSICS_LIQUID_UNSUPPORTED','Only static sources and authored kinematic obstacles are supported; no rigid-fluid force feedback.')
        core.require(len(b['colliders'])==1 and b['colliders'][0]['shape']['type'] in ('sphere','box'),
            'PHYSICS_LIQUID_UNSUPPORTED','Liquid source admission currently supports actual sphere/box meshes.')
        c=b['colliders'][0]
        core.require(c['transform']==dict(translation=[0,0,0],rotation=[0,0,0,1]) and not c['sensor'] and c['group']==1 and c['mask']==2**32-1,
            'PHYSICS_LIQUID_UNSUPPORTED','Liquid sources require identity nonsensor unfiltered collider transforms.')
    return world,validate_settings(settings,frames)
