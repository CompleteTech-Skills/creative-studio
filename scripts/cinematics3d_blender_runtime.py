"""Fixed Blender-side cinematic mapping; called by the canonical Render3D worker.

This module does not load files, spawn processes, create geometry, render images
or publish output. Scene/render/output ownership stays in Render3D.
"""
from __future__ import annotations

import math


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _enum_available(owner, key, values):
    original = getattr(owner, key)
    result = []
    try:
        for value in values:
            try:
                setattr(owner, key, value)
                if getattr(owner, key) == value:
                    result.append(value)
            except (TypeError, ValueError, RuntimeError):
                pass
    finally:
        setattr(owner, key, original)
    return result


def inspect_scene(scene):
    """Probe actual RNA/node availability; never label this pixel verification."""
    import bpy
    engines = _enum_available(scene.render, 'engine', ['CYCLES', 'BLENDER_EEVEE_NEXT'])
    transforms = _enum_available(scene.view_settings, 'view_transform',
                                  ['Standard', 'AgX', 'Filmic', 'Raw', 'Khronos PBR Neutral'])
    tree = bpy.data.node_groups.new('hatch-cinematics-probe', 'ShaderNodeTree')
    nodes = []
    try:
        for name in ['ShaderNodeVolumePrincipled', 'ShaderNodeBackground']:
            try:
                node = tree.nodes.new(name); nodes.append(name); tree.nodes.remove(node)
            except (RuntimeError, TypeError):
                pass
    finally:
        bpy.data.node_groups.remove(tree)
    original_use_nodes=scene.use_nodes
    scene.use_nodes=True
    compositor=scene.node_tree
    try:
        for name in ['CompositorNodeRLayers', 'CompositorNodeGlare', 'CompositorNodeExposure',
                     'CompositorNodeColorBalance', 'CompositorNodeCryptomatteV2']:
            try:
                node=compositor.nodes.new(name); nodes.append(name); compositor.nodes.remove(node)
            except (RuntimeError, TypeError):
                pass
    finally:
        scene.use_nodes=original_use_nodes
    layer=scene.view_layers[0]
    pass_properties = {name: hasattr(layer, property_name) for name,property_name in {
        'depth':'use_pass_z','normal':'use_pass_normal','vector':'use_pass_vector',
        'cryptomatte':'use_pass_cryptomatte_object','albedo':'use_pass_diffuse_color'}.items()}
    camera=bpy.data.cameras.new('hatch-cinematics-camera-probe')
    try:
        dof = all(hasattr(camera.dof,k) for k in ('use_dof','focus_distance','aperture_fstop',
                      'aperture_blades','aperture_rotation','aperture_ratio'))
    finally:
        bpy.data.cameras.remove(camera)
    devices=[dict(id='CPU',name='CPU',type='CPU',initialized=False)]
    preferences=bpy.context.preferences.addons.get('cycles')
    if preferences is not None:
        try:
            preferences.preferences.refresh_devices()
            devices=[dict(id=d.id,name=d.name,type=d.type,initialized=False)
                     for d in preferences.preferences.devices]
            if not any(d['type']=='CPU' for d in devices):
                devices.insert(0,dict(id='CPU',name='CPU',type='CPU',initialized=False))
        except (RuntimeError,TypeError):
            pass
    mappings = {
        'depth-of-field': (dof, 'Native camera DOF RNA'),
        'volumetric-fog': ('ShaderNodeVolumePrincipled' in nodes, 'Native world volume shader'),
        'exposure': (hasattr(scene.view_settings,'exposure'), 'Native display exposure'),
        'tone-map': ('Standard' in transforms and 'AgX' in transforms, 'Native display transforms'),
        'motion-blur': (False, 'Exact shutter subframe scene sampling is not integrated'),
    }
    return dict(schema_version=1,kind='cinematics3d-blender-probe',application_version='Blender '+bpy.app.version_string,
                engines=engines,view_transforms=transforms,nodes=nodes,passes=pass_properties,
                transparency=hasattr(scene.render,'film_transparent'),
                effects={key:dict(available=available,method=method,pixel_verified=False)
                         for key,(available,method) in mappings.items()},
                devices=devices,rendered=False)


def _finite(value,low,high,label):
    _require(type(value) in (int,float) and low<=value<=high and math.isfinite(value),
             'Invalid cinematic '+label)


def validate_plan(plan):
    """Strict small worker protocol; host also validates original owner documents."""
    keys={'schema_version','kind','request_sha256','configuration_sha256','probe_sha256',
          'adapter_sha256','stack','sources','camera','dof','fog','disabled','effective_settings'}
    _require(isinstance(plan,dict) and set(plan)==keys and type(plan['schema_version']) is int and plan['schema_version']==1
             and plan['kind']=='cinematics3d-native-plan', 'Invalid cinematic plan')
    for key in ('request_sha256','configuration_sha256','probe_sha256','adapter_sha256'):
        value=plan[key]
        _require(isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value),
                 'Invalid cinematic digest')
    _require(isinstance(plan['camera'],str) and 0<len(plan['camera'])<=128,'Invalid cinematic camera')
    _require(isinstance(plan['sources'],list) and len(plan['sources'])<=128
             and all(isinstance(v,dict) and set(v)=={'id','version','kind','path','sha256'} for v in plan['sources']),
             'Invalid cinematic sources')
    _require(isinstance(plan['disabled'],list) and len(plan['disabled'])<=128
             and all(isinstance(v,str) and 0<len(v)<=128 for v in plan['disabled']),
             'Invalid disabled cinematic effects')
    settings=plan['effective_settings']
    _require(isinstance(settings,dict) and set(settings)=={'exposure','view_transform','alpha'},'Invalid effective settings')
    _finite(settings['exposure'],-32,32,'exposure')
    _require(settings['view_transform'] in ('standard','agx') and settings['alpha'] in ('opaque','straight'),
             'Invalid effective display settings')
    if plan['dof'] is not None:
        d=plan['dof']
        _require(isinstance(d,dict) and set(d)=={'enabled','focus_distance_m','f_stop','aperture_blades',
                   'aperture_rotation_rad','anamorphic_ratio','focal_length_mm','sensor_width_mm','sensor_height_mm',
                   'sensor_fit','shift_x','shift_y','near_clip_m','far_clip_m','native_gate'},
                 'Invalid DOF plan')
        _require(type(d['enabled']) is bool,'Invalid DOF enabled')
        for key in ('focus_distance_m','f_stop','anamorphic_ratio','focal_length_mm','sensor_width_mm','sensor_height_mm'):
            _finite(d[key],1e-9,1e9,key)
        n=d['native_gate']
        _require(isinstance(n,dict) and set(n)=={'type','lens','sensor_width','shift_x','shift_y','clip_start','clip_end'}
                 and n['type']=='PERSP','Invalid native camera gate')
        for key in ('lens','sensor_width','clip_start','clip_end'):_finite(n[key],1e-9,1e9,key)
        for key in ('shift_x','shift_y'):_finite(n[key],-1e9,1e9,key)
        _require(d['sensor_fit'] in ('horizontal','vertical','auto'),'Invalid sensor fit')
        _finite(d['shift_x'],-100,100,'shift x');_finite(d['shift_y'],-100,100,'shift y')
        _finite(d['near_clip_m'],1e-9,1e9,'near clip');_finite(d['far_clip_m'],d['near_clip_m'],1e9,'far clip')
        _finite(d['aperture_rotation_rad'],-1e9,1e9,'aperture rotation')
        _require(type(d['aperture_blades']) is int and (d['aperture_blades']==0 or 3<=d['aperture_blades']<=32),
                 'Invalid aperture blades')
    if plan['fog'] is not None:
        fog=plan['fog']
        _require(isinstance(fog,dict) and set(fog)=={'density_per_meter','anisotropy'},'Invalid volume plan')
        _finite(fog['density_per_meter'],0,100,'density');_finite(fog['anisotropy'],-.99,.99,'anisotropy')
    reference=plan['stack']
    _require(isinstance(reference,dict) and set(reference)=={'id','version','kind','path','sha256'}
             and all(isinstance(reference[k],str) and 0<len(reference[k])<=4096 for k in reference)
             and reference['kind']=='cinematics3d-stack' and len(reference['sha256'])==64
             and all(c in '0123456789abcdef' for c in reference['sha256']), 'Invalid cinematic stack reference')
    for reference in plan['sources']:
        _require(all(isinstance(reference[k],str) and 0<len(reference[k])<=4096 for k in reference), 'Invalid cinematic reference')
        _require(reference['kind']=='cinematics3d-effect' and len(reference['sha256'])==64
                 and all(c in '0123456789abcdef' for c in reference['sha256']), 'Invalid cinematic effect reference')
    return plan


def apply_frame(scene, plan):
    """Apply after the scene owner sets camera/light state, before owner rendering."""
    validate_plan(plan)
    _require(scene.camera is not None,'Cinematic scene has no active camera')
    # The backend binds camera identifier to its actual selected native object.
    if plan['dof'] is not None:
        camera=scene.camera.data;d=plan['dof']
        _require(camera.type=='PERSP','Native optical DOF requires a perspective camera')
        n=d['native_gate'];camera.sensor_fit='HORIZONTAL'
        camera.shift_x=n['shift_x'];camera.shift_y=n['shift_y']
        camera.clip_start=n['clip_start'];camera.clip_end=n['clip_end'];camera.dof.focus_object=None
        camera.lens=n['lens'];camera.sensor_width=n['sensor_width'];camera.sensor_height=d['sensor_height_mm']
        camera.dof.use_dof=d['enabled'];camera.dof.focus_distance=d['focus_distance_m'];camera.dof.aperture_fstop=d['f_stop']
        camera.dof.aperture_blades=d['aperture_blades'];camera.dof.aperture_rotation=d['aperture_rotation_rad'];camera.dof.aperture_ratio=d['anamorphic_ratio']
        for owner,key,wanted in [(camera.dof,'focus_distance',d['focus_distance_m']),
                (camera.dof,'aperture_fstop',d['f_stop']),(camera.dof,'aperture_ratio',d['anamorphic_ratio']),
                (camera.dof,'aperture_rotation',d['aperture_rotation_rad']),(camera,'lens',n['lens']),
                (camera,'sensor_width',n['sensor_width']),(camera,'sensor_height',d['sensor_height_mm']),
                (camera,'shift_x',n['shift_x']),(camera,'shift_y',n['shift_y']),
                (camera,'clip_start',n['clip_start']),(camera,'clip_end',n['clip_end'])]:
            _require(math.isclose(getattr(owner,key),wanted,rel_tol=1e-6,abs_tol=1e-8),'Blender clamped cinematic '+key)
        _require(camera.dof.aperture_blades==d['aperture_blades'] and camera.dof.use_dof==d['enabled'],
                 'Blender changed cinematic DOF settings')
    if plan['fog'] is not None:
        _require(scene.world is not None,'Cinematic volume needs the owner world')
        scene.world.use_nodes=True;tree=scene.world.node_tree
        output=next((n for n in tree.nodes if n.bl_idname=='ShaderNodeOutputWorld' and n.is_active_output),None)
        _require(output is not None,'World output is absent')
        name='hatch-cinematics-world-volume'
        node=tree.nodes.get(name)
        if node is None:
            _require(not output.inputs['Volume'].is_linked,'Existing world volume must be owned by LookDev')
            node=tree.nodes.new('ShaderNodeVolumePrincipled');node.name=name
            tree.links.new(node.outputs['Volume'],output.inputs['Volume'])
        _require(node.bl_idname=='ShaderNodeVolumePrincipled','Cinematic volume name collision')
        node.inputs['Density'].default_value=plan['fog']['density_per_meter']
        node.inputs['Anisotropy'].default_value=plan['fog']['anisotropy']
        for socket,key in [('Density','density_per_meter'),('Anisotropy','anisotropy')]:
            _require(math.isclose(node.inputs[socket].default_value,plan['fog'][key],rel_tol=1e-6,abs_tol=1e-8),
                     'Blender clamped cinematic '+key)
    return inspect_effective(scene,plan)


def inspect_effective(scene,plan):
    """Read actual RNA after mapping; successful settings do not prove pixel change."""
    result=dict(camera=plan['camera'],exposure=scene.view_settings.exposure,
                view_transform=scene.view_settings.view_transform,transparent=scene.render.film_transparent,
                dof=None,fog=None,pixel_verified=False)
    if plan['dof'] is not None:
        d=scene.camera.data.dof
        result['dof']=dict(enabled=d.use_dof,focus_distance_m=d.focus_distance,f_stop=d.aperture_fstop,
                           aperture_blades=d.aperture_blades,aperture_rotation_rad=d.aperture_rotation,anamorphic_ratio=d.aperture_ratio,
                           focal_length_mm=scene.camera.data.lens,sensor_width_mm=scene.camera.data.sensor_width,
                           sensor_height_mm=scene.camera.data.sensor_height,sensor_fit=scene.camera.data.sensor_fit,
                           shift_x=scene.camera.data.shift_x,shift_y=scene.camera.data.shift_y,
                           near_clip_m=scene.camera.data.clip_start,far_clip_m=scene.camera.data.clip_end)
    if plan['fog'] is not None:
        node=scene.world.node_tree.nodes['hatch-cinematics-world-volume']
        result['fog']=dict(density_per_meter=node.inputs['Density'].default_value,anisotropy=node.inputs['Anisotropy'].default_value)
    return result
