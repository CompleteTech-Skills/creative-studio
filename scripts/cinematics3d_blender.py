"""Compile cinematic authoring contracts for the canonical Render3D Blender worker.

This is an adapter, not a renderer. It creates no geometry, starts no processes,
writes no media and never substitutes output for an unsupported effect.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

from camera3d_contracts import validate as validate_camera
from camera3d_blender import optical_plan
from cinematics3d_contracts import Registry, canonical_bytes, effect_registry, validate_document, portable_path
from cinematics3d_blender_runtime import validate_plan
from rig_errors import decode_json, finite_json, object_hash, require, RigError
from render3d_contracts import validate as validate_render

VERSION='1.0.0'
MAPPED_EFFECTS={'exposure','tone-map','depth-of-field','volumetric-fog'}
ADAPTER_SOURCES=('cinematics3d_blender.py','cinematics3d_blender_runtime.py','camera3d_blender.py','camera3d_math.py')


def adapter_hash():
    return object_hash({name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                        for name in ADAPTER_SOURCES})


def validate_probe(value):
    finite_json(value)
    keys={'schema_version','kind','application_version','engines','view_transforms','nodes','passes','transparency','effects','rendered','devices'}
    require(isinstance(value,dict) and set(value)==keys and type(value['schema_version']) is int
            and value['schema_version']==1 and value['kind']=='cinematics3d-blender-probe',
            'CINEMATICS_PROBE','Invalid cinematic probe shape.')
    require(isinstance(value['application_version'],str) and value['application_version'].startswith('Blender 4.5.')
            and len(value['application_version'])<=128,'CINEMATICS_PROBE','Expected a Blender 4.5 LTS probe.')
    for name,limit in [('engines',8),('view_transforms',32),('nodes',32)]:
        items=value[name]
        require(isinstance(items,list) and len(items)<=limit and all(isinstance(x,str) and 0<len(x)<=128 for x in items)
                and len(items)==len(set(items)),'CINEMATICS_PROBE','Invalid '+name+' inventory.')
    require(isinstance(value['devices'],list) and len(value['devices'])<=64
            and all(isinstance(v,dict) and set(v)=={'id','name','type','initialized'}
                    and all(isinstance(v[k],str) and 0<len(v[k])<=256 for k in ('id','name','type'))
                    and v['initialized'] is False for v in value['devices']),
            'CINEMATICS_PROBE','Invalid device inventory.')
    require(value['rendered'] is False and type(value['transparency']) is bool,'CINEMATICS_PROBE','Probe is not pixel evidence.')
    require(isinstance(value['passes'],dict) and set(value['passes'])=={'depth','normal','vector','cryptomatte','albedo'}
            and all(type(v) is bool for v in value['passes'].values()),'CINEMATICS_PROBE','Invalid pass inventory.')
    require(isinstance(value['effects'],dict) and set(value['effects'])==MAPPED_EFFECTS|{'motion-blur'},
            'CINEMATICS_PROBE','Invalid effect inventory.')
    for entry in value['effects'].values():
        require(isinstance(entry,dict) and set(entry)=={'available','method','pixel_verified'}
                and type(entry['available']) is bool and entry['pixel_verified'] is False
                and isinstance(entry['method'],str) and 0<len(entry['method'])<=256,
                'CINEMATICS_PROBE','Invalid effect probe record.')
    return deepcopy(value)


def compile_plan(registry, stack_reference, request, probe, *, input_bytes=None):
    """Return a new request plus hash-bound native settings; never mutate inputs.

    Probe inventory is caller-provided and must originate in the canonical
    worker. The worker rechecks properties before execution, and result decoding
    still belongs to Render3D's independent verifier.
    """
    require(isinstance(registry,Registry),'CINEMATICS_REGISTRY','Expected a verified cinematic registry.')
    validate_render(request,'RenderRequest');probe=validate_probe(probe)
    require(request['settings']['engine']=='cycles' and request['settings']['device_policy']=='cpu',
            'CINEMATICS_BACKEND','Cinematic integration currently uses the verified Cycles CPU backend.')
    require('CYCLES' in probe['engines'],'CINEMATICS_BACKEND','Installed Blender does not expose Cycles.')
    require(registry.get(stack_reference)['scope']!='object','CINEMATICS_UNSUPPORTED',
            'Object-local settings require an object-aware native adapter.')
    effective=deepcopy(request);effects=registry.effective_stack(stack_reference)
    require(len(effects)<=128,'CINEMATICS_LIMIT','Too many effective effects.')
    supplied={} if input_bytes is None else input_bytes
    require(isinstance(supplied,dict) and len(supplied)<=128,'CINEMATICS_INPUT','Invalid input byte inventory.')
    configuration={'documents/'+path:data for path,data in registry.bytes().items()}
    external=registry.external_bytes()
    configuration.update({'inputs/'+path:data for path,data in external.items()})
    for path,data in supplied.items():
        portable_path(path)
        require(isinstance(data,bytes) and len(data)<=2*1024**2,'CINEMATICS_INPUT','Expected bounded native input bytes.')
        require(external.get(path)==data,'CINEMATICS_HASH','Native input differs from registry-bound bytes.')
    require(len(configuration)<=1152,'CINEMATICS_LIMIT','Too many native configuration files.')
    require(sum(map(len,configuration.values()))<=16*1024**2,'CINEMATICS_LIMIT','Native configuration exceeds 16 MiB.')
    plan=dict(schema_version=1,kind='cinematics3d-native-plan',request_sha256='0'*64,
              configuration_sha256=object_hash({path:hashlib.sha256(data).hexdigest() for path,data in configuration.items()}),
              probe_sha256=object_hash(probe),adapter_sha256=adapter_hash(),stack=deepcopy(stack_reference),sources=[],camera=request['camera'],dof=None,fog=None,disabled=[],
              effective_settings={})
    for effect in effects:
        validate_document(effect)
        operation=effect['operation'];name=operation['type'];params=operation['parameters']
        require(name in MAPPED_EFFECTS,'CINEMATICS_UNSUPPORTED','Cinematic effect has no verified native mapping: '+name)
        require(probe['effects'][name]['available'],'CINEMATICS_UNSUPPORTED','Blender lacks '+name+': '+probe['effects'][name]['method'])
        plan['sources'].append(registry.reference(effect['id'],effect['version']))
        if name=='exposure':
            effective['settings']['exposure']=params['stops']
        elif name=='tone-map':
            require(params['view'] in ('standard','agx'),'CINEMATICS_UNSUPPORTED','Render3D supports Standard and AgX display transforms.')
            require({'standard':'Standard','agx':'AgX'}[params['view']] in probe['view_transforms'],
                    'CINEMATICS_UNSUPPORTED','Requested display transform is absent.')
            effective['settings']['view_transform']=params['view']
        elif name=='depth-of-field':
            require(plan['dof'] is None,'CINEMATICS_CONFLICT','Multiple native DOF controls are ambiguous.')
            reference=effect['inputs'][0];payload=supplied.get(reference['path'])
            require(isinstance(payload,bytes) and len(payload)<=2*1024**2,'CINEMATICS_INPUT','Missing bounded optics bytes.')
            require(hashlib.sha256(payload).hexdigest()==reference['sha256'],'CINEMATICS_HASH','Changed native optics bytes.')
            try:
                optics=validate_camera(decode_json(payload),'optics')
            except ValueError as exc:
                raise RigError('CINEMATICS_INPUT',str(exc)) from exc
            require((optics['id'],optics['version'],optics['kind'])==(reference['id'],reference['version'],reference['kind']),
                    'CINEMATICS_REFERENCE','Optics identity mismatch.')
            projection=optics['projection'];dof=optics['depth_of_field']
            require(projection['type']=='perspective','CINEMATICS_UNSUPPORTED','Physical DOF requires perspective optics.')
            require(dof['aperture_blades']==0 or dof['aperture_blades']>=3,'CINEMATICS_UNSUPPORTED','Blender aperture blades must be zero or at least three.')
            plan['dof']=dict(enabled=params['enabled'] and dof['enabled'],focus_distance_m=dof['focus_distance_m'],
                f_stop=dof['f_stop'],aperture_blades=dof['aperture_blades'],aperture_rotation_rad=dof['aperture_rotation_rad'],
                anamorphic_ratio=dof['anamorphic_ratio'],focal_length_mm=projection['focal_length_mm'],
                sensor_width_mm=projection['sensor_width_mm'],sensor_height_mm=projection['sensor_height_mm'],
                sensor_fit=projection['sensor_fit'],shift_x=projection['shift_x'],shift_y=projection['shift_y'],
                near_clip_m=projection['near_clip_m'],far_clip_m=projection['far_clip_m'])
            # Projection/gate/shift semantics belong to Camera3D. The worker
            # transports its already-computed native settings without a second
            # optics implementation or optional host packages inside Blender.
            resolved=deepcopy(optics);resolved['depth_of_field']['enabled']=plan['dof']['enabled']
            width,height=effective['settings']['resolution']
            camera_render=dict(schema_version=1,kind='camera3d-render',id='cinematic-render',version='1.0.0',
                width=width,height=height,pixel_aspect=dict(numerator=1,denominator=1),overscan=1,gate_fit='crop',
                output=effective['settings']['output']['format'],color_space=effective['settings']['output']['color_space'],
                transparent=effective['settings']['output']['alpha']=='straight',max_pixels=width*height)
            try:
                native=optical_plan(resolved,camera_render)
            except ValueError as exc:
                raise RigError('CINEMATICS_INPUT',str(exc)) from exc
            plan['dof']['native_gate']={key:native[key] for key in ('type','lens','sensor_width','shift_x','shift_y','clip_start','clip_end')}
        elif name=='volumetric-fog':
            require(effect['scope']!='object','CINEMATICS_UNSUPPORTED','Object-local fog requires the LookDev volume owner.')
            require(plan['fog'] is None,'CINEMATICS_CONFLICT','Multiple world volume controls are ambiguous.')
            plan['fog']=deepcopy(params)
    validate_render(effective,'RenderRequest')
    require(effective['settings']['output']['alpha']!='straight' or probe['transparency'],
            'CINEMATICS_UNSUPPORTED','Native transparent film is unavailable.')
    for pass_name in effective['settings']['output']['passes']:
        require(pass_name=='combined' or probe['passes'].get(pass_name,False),
                'CINEMATICS_UNSUPPORTED','Native pass is unavailable: '+pass_name)
    plan['effective_settings']={key:deepcopy(effective['settings'][key]) for key in ('exposure','view_transform')}
    plan['effective_settings']['alpha']=effective['settings']['output']['alpha']
    effective['settings']['cinematics_sha256']=object_hash({k:v for k,v in plan.items() if k!='request_sha256'})
    validate_render(effective,'RenderRequest')
    plan['request_sha256']=object_hash(effective)
    try:
        validate_plan(plan)
    except ValueError as exc:
        raise RigError('CINEMATICS_PLAN',str(exc)) from exc
    validate_configuration(plan,configuration)
    return dict(request=effective,plan=plan,configuration=configuration,probe=probe,executed=False)


def validate_bound_plan(plan,request):
    try:
        validate_plan(plan)
    except ValueError as exc:
        raise RigError('CINEMATICS_PLAN',str(exc)) from exc
    require(plan['adapter_sha256']==adapter_hash(),'CINEMATICS_STALE','Native cinematic adapter changed; recompile the plan.')
    require(plan['request_sha256']==object_hash(request) and plan['camera']==request['camera'],
            'CINEMATICS_STALE','Native cinematic plan is bound to a different render request.')
    require(request['settings'].get('cinematics_sha256')==object_hash({k:v for k,v in plan.items() if k!='request_sha256'}),
            'CINEMATICS_STALE','Native cinematic plan differs from the cache-bound settings.')
    expected={key:request['settings'][key] for key in ('exposure','view_transform')}
    expected['alpha']=request['settings']['output']['alpha']
    require(plan['effective_settings']==expected,'CINEMATICS_STALE','Native effective settings differ from the render request.')
    return deepcopy(plan)


def validate_configuration(plan, configuration):
    require(isinstance(configuration,dict) and len(configuration)<=1152,
            'CINEMATICS_INPUT','Invalid native configuration files.')
    aliases=set(); total=0
    for path,data in configuration.items():
        portable_path(path)
        require(path.startswith(('documents/','inputs/')) and path.casefold() not in aliases,
                'CINEMATICS_INPUT','Invalid configuration path or alias.')
        aliases.add(path.casefold())
        require(isinstance(data,bytes) and len(data)<=2*1024**2,'CINEMATICS_INPUT','Invalid configuration bytes.')
        total+=len(data)
    require(total<=16*1024**2,'CINEMATICS_LIMIT','Native configuration exceeds 16 MiB.')
    require(object_hash({path:hashlib.sha256(data).hexdigest() for path,data in configuration.items()})==plan['configuration_sha256'],
            'CINEMATICS_HASH','Configuration bytes differ from the compiled plan.')
    return dict(configuration)


def render(registry, stack_reference, request, snapshot_root, destination, *, backend=None, input_bytes=None, cancelled=None):
    """Compile against an actual probe and execute through the canonical owner."""
    if backend is None:
        from render3d_blender import BlenderBackend
        backend=BlenderBackend()
    probe=backend.cinematics_probe()
    compiled=compile_plan(registry,stack_reference,request,probe,input_bytes=input_bytes)
    return backend.render_frames(compiled['request'],snapshot_root,destination,cancelled=cancelled,
                                 cinematics=compiled)


def validate_compiled(compiled, request):
    """Recompile original documents before trusting claimed settings/provenance.

    Digests bind bytes but do not establish that arbitrary native plan fields
    actually follow those authoring documents. Recompilation enforces that link.
    """
    require(isinstance(compiled,dict) and set(compiled)=={'request','plan','configuration','probe','executed'}
            and compiled['executed'] is False and compiled['request']==request,
            'CINEMATICS_INPUT','Invalid compiled cinematic request.')
    plan=validate_bound_plan(compiled['plan'],request)
    files=validate_configuration(plan,compiled['configuration'])
    probe=validate_probe(compiled['probe'])
    require(object_hash(probe)==plan['probe_sha256'],'CINEMATICS_HASH','Probe differs from compiled plan.')
    documents=[(path[len('documents/'):],payload) for path,payload in files.items() if path.startswith('documents/')]
    inputs={path[len('inputs/'):]:payload for path,payload in files.items() if path.startswith('inputs/')}
    restored=Registry(documents,external=inputs)
    expected=compile_plan(restored,plan['stack'],request,probe,input_bytes=inputs)
    require(expected['plan']==plan and expected['request']==request and expected['configuration']==files,
            'CINEMATICS_STALE','Native settings differ from retained authoring documents.')
    return plan,files
