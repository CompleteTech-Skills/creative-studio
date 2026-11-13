"""Fixed Blender Mantaflow worker. It never opens supplied Blender projects."""
import hashlib
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector

BASIS = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def main():
    root = Path(__file__).resolve().parent
    job_path = Path(sys.argv[sys.argv.index('--')+1]).resolve()
    if job_path != root/'gas-job.json':
        raise ValueError('Expected the private gas job beside the fixed worker.')
    job = json.loads(job_path.read_bytes())
    if set(job) != {'protocol', 'mode', 'expected_blend_sha256', 'world', 'settings', 'meshes', 'worker_sha256', 'world_sha256', 'settings_sha256'} or job['protocol'] != 1:
        raise ValueError('Invalid fixed gas worker protocol.')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != job['worker_sha256']:
        raise ValueError('Worker source changed.')
    if hashlib.sha256(canonical(job['world'])).hexdigest() != job['world_sha256'] or hashlib.sha256(canonical(job['settings'])).hexdigest() != job['settings_sha256']:
        raise ValueError('Admitted settings changed.')
    world, settings = job['world'], job['settings']
    component = world['components'][0]
    if not (len(world['components'])==1 and 2<=world['frame_range']['end']<=48 and
            len(world['bodies'])<=16 and 8<=component['resolution'][0]<=64 and
            len(set(component['resolution']))==1 and len(settings['render_frames'])<=8 and
            64<=settings['render_size']<=256 and 1<=settings['render_samples']<=16):
        raise ValueError('Gas allocation bounds exceeded.')
    if job['mode']=='reopen':
        scene_path=root/'scene.blend'
        if hashlib.sha256(scene_path.read_bytes()).hexdigest()!=job['expected_blend_sha256']:
            raise ValueError('Generated native gas project changed before replay.')
        bpy.ops.wm.open_mainfile(filepath=str(scene_path),load_ui=False,use_scripts=False)
        scene=bpy.context.scene
        for frame in reversed(settings['render_frames']):
            scene.frame_set(frame+1)
            scene.render.filepath=str(root/f'replay-frame-{frame:04d}.png')
            bpy.ops.render.render(write_still=True)
        (root/'replay-report.json').write_bytes(canonical(dict(mode='reopen',blend_sha256=job['expected_blend_sha256'],
            frames=settings['render_frames'],version=bpy.app.version_string,build=bpy.app.build_hash.decode())))
        return
    if job['mode']!='simulate' or job['expected_blend_sha256'] is not None:
        raise ValueError('Unsupported gas worker operation.')
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not bpy.app.build_options.fluid or not bpy.app.build_options.openvdb:
        raise ValueError('This Blender build lacks Mantaflow or OpenVDB.')
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = world['frame_range']['end']
    scene.render.fps = world['fps']['numerator']
    scene.render.fps_base = world['fps']['denominator']
    scene.gravity = BASIS.to_3x3() @ Vector(world['gravity'])
    objects = {}
    for body in world['bodies']:
        data = job['meshes'][body['id']]
        mesh = bpy.data.meshes.new(body['id'])
        mesh.from_pydata(data['vertices'], [], data['triangles'])
        mesh.update()
        obj = bpy.data.objects.new(body['id'], mesh)
        scene.collection.objects.link(obj)
        t = body['transform']
        from mathutils import Quaternion
        q = t['rotation']
        matrix = Matrix.Translation(Vector(t['translation'])) @ Quaternion((q[3],q[0],q[1],q[2])).to_matrix().to_4x4()
        obj.matrix_world = BASIS @ matrix
        objects[body['id']] = obj
    observed_sources = {}
    for identifier in component['emitters']:
        obj = objects[identifier]
        mod = obj.modifiers.new('Mantaflow emitter', 'FLUID')
        mod.fluid_type = 'FLOW'
        bpy.context.view_layer.update()
        flow = mod.flow_settings
        flow.flow_type = 'BOTH' if component['combustion'] else 'SMOKE'
        flow.flow_behavior = 'INFLOW'
        flow.density = component['density']/settings['reference_density']
        flow.temperature = (component['temperature']-settings['ambient_temperature'])/settings['temperature_scale']
        flow.fuel_amount = settings['fuel']
        observed_sources[identifier] = dict(density=flow.density,temperature=flow.temperature,fuel=flow.fuel_amount)
        flow.use_inflow = True
        flow.keyframe_insert(data_path='use_inflow', frame=1)
        flow.keyframe_insert(data_path='use_inflow', frame=settings['emission_end_frame'])
        flow.use_inflow = False
        flow.keyframe_insert(data_path='use_inflow', frame=settings['emission_end_frame']+1)
        obj.hide_render = True
    for identifier in component['colliders']:
        obj = objects[identifier]
        mod = obj.modifiers.new('Mantaflow collision', 'FLUID')
        mod.fluid_type = 'EFFECTOR'
        bpy.context.view_layer.update()
        mod.effector_settings.surface_distance = settings['collision_distance']
        observed_sources[identifier] = dict(collision_distance=mod.effector_settings.surface_distance)
    size = component['size']
    location = BASIS.to_3x3() @ Vector(component['transform']['translation'])
    bpy.ops.mesh.primitive_cube_add(size=2, location=location)
    domain = bpy.context.object
    domain.name = component['id']
    domain.scale = (size[0]/2,size[2]/2,size[1]/2)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    mod = domain.modifiers.new('Mantaflow gas', 'FLUID')
    mod.fluid_type = 'DOMAIN'
    bpy.context.view_layer.update()
    ds = mod.domain_settings
    ds.domain_type = 'GAS'
    ds.resolution_max = component['resolution'][0]
    ds.cache_frame_start = 1
    ds.cache_frame_end = scene.frame_end
    ds.cache_type = 'ALL'
    ds.cache_data_format = 'OPENVDB'
    ds.cache_directory = str(root/'cache')
    ds.use_adaptive_domain = False
    ds.use_noise = False
    ds.use_dissolve_smoke = settings['dissipation_enabled']
    ds.dissolve_speed = settings['dissipation_frames']
    ds.use_dissolve_smoke_log = False
    ds.vorticity = settings['vorticity']
    ds.burning_rate = settings['burning_rate']
    ds.alpha = settings['density_buoyancy']
    ds.beta = settings['heat_buoyancy']
    # Mantaflow chooses adaptive CFL substeps. Record its actual policy rather
    # than pretending the rigid-body fixed timestep is this solver's clock.
    ds.cfl_condition = settings['cfl']
    ds.timesteps_min = settings['minimum_substeps']
    ds.timesteps_max = settings['maximum_substeps']
    observed=dict(dissipation_enabled=ds.use_dissolve_smoke,dissipation_frames=ds.dissolve_speed,vorticity=ds.vorticity,
        burning_rate=ds.burning_rate,density_buoyancy=ds.alpha,heat_buoyancy=ds.beta,collision_distance=settings['collision_distance'],
        cfl=ds.cfl_condition,minimum_substeps=ds.timesteps_min,maximum_substeps=ds.timesteps_max)
    for force in world['forces']:
        bpy.ops.object.effector_add(type=force['type'].upper(), location=BASIS.to_3x3() @ Vector(force['point']))
        obj = bpy.context.object
        obj.field.strength = force['strength']
        obj.field.seed = force['seed']
        if force['type'] == 'wind':
            direction = BASIS.to_3x3() @ Vector(force['vector'])
            obj.rotation_euler = direction.to_track_quat('Z','Y').to_euler()
    bpy.context.view_layer.objects.active = domain
    bpy.ops.object.select_all(action='DESELECT')
    domain.select_set(True)
    bpy.ops.wm.save_as_mainfile(filepath=str(root/'scene.blend'))
    if bpy.ops.fluid.bake_all() != {'FINISHED'} or not ds.has_cache_baked_data:
        raise ValueError('Mantaflow did not complete native baking.')
    ds.cache_directory = '//cache'
    material = bpy.data.materials.new('Actual gas fields')
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new('ShaderNodeOutputMaterial')
    volume = nodes.new('ShaderNodeVolumePrincipled')
    volume.inputs['Density'].default_value = settings['render_density']
    volume.inputs['Blackbody Intensity'].default_value = 1 if component['combustion'] else 0
    material.node_tree.links.new(volume.outputs['Volume'], output.inputs['Volume'])
    domain.data.materials.append(material)
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = settings['render_samples']
    scene.render.resolution_x = scene.render.resolution_y = settings['render_size']
    scene.render.resolution_percentage = 100
    scene.world = bpy.data.worlds.new('Neutral gas review')
    scene.world.color = (.035,.035,.035)
    center = location
    bpy.ops.object.camera_add(location=center+Vector((size[0]*2,-size[0]*3,size[0]*1.2)))
    camera = bpy.context.object
    camera.rotation_euler = (center-camera.location).to_track_quat('-Z','Y').to_euler()
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = size[0]*1.6
    scene.camera = camera
    bpy.ops.object.light_add(type='AREA', location=center+Vector((size[0],-size[0],size[0]*2)))
    lamp = bpy.context.object
    lamp.data.energy = 1000
    lamp.data.shape = 'DISK'
    lamp.data.size = size[0]*2
    lamp.rotation_euler = (center-lamp.location).to_track_quat('-Z','Y').to_euler()
    for frame in settings['render_frames']:
        scene.frame_set(frame+1)
        scene.render.filepath = str(root/f'frame-{frame:04d}.png')
        bpy.ops.render.render(write_still=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(root/'scene.blend'))
    report = dict(protocol=1, backend='Blender Mantaflow', version=bpy.app.version_string,
        build=bpy.app.build_hash.decode(), native_bake=bool(ds.has_cache_baked_data),
        frame_count=scene.frame_end, cache_format='OPENVDB', requested_resolution=component['resolution'],
        fixed_step_policy='adaptive CFL native substeps; rigid-body fixed_step is not claimed',
        effective_fps=dict(numerator=scene.render.fps,denominator=scene.render.fps_base),
        cfl=ds.cfl_condition,minimum_substeps=ds.timesteps_min,maximum_substeps=ds.timesteps_max,
        observed_sources=observed_sources,render_frames=settings['render_frames'],world_sha256=job['world_sha256'],settings_sha256=job['settings_sha256'],observed=observed)
    (root/'gas-report.json').write_bytes(canonical(report))


if __name__ == '__main__':
    main()
