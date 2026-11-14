"""Package-owned Blender liquid worker; only opens its own hash-bound output."""
import hashlib
import gzip
import struct
import json
import math
import os
import stat
from pathlib import Path
import sys

import bpy
from mathutils import Matrix,Quaternion,Vector

BASIS=Matrix(((1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sample(scene,domain,frames,max_vertices,max_triangles):
    result=[]
    for frame in frames:
        scene.frame_set(frame+1)
        evaluated=domain.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh=evaluated.to_mesh()
        try:
            mesh.calc_loop_triangles()
            if len(mesh.vertices)>max_vertices or len(mesh.loop_triangles)>max_triangles:
                raise ValueError('Evaluated liquid mesh exceeded admitted geometry limit.')
            vertices=[list(BASIS.inverted()@(domain.matrix_world@v.co)) for v in mesh.vertices]
            triangles=[list(t.vertices) for t in mesh.loop_triangles]
            # Blender exposes the domain cage after the fluid has entirely left an
            # open boundary. An explicitly empty native mesh has no liquid surface.
            cache=Path(bpy.path.abspath(domain.modifiers['Native liquid domain'].domain_settings.cache_directory))
            with gzip.open(cache/'mesh'/f'fluid_mesh_{frame+1:04d}.bobj.gz','rb') as stream:
                native_count=struct.unpack('<i',stream.read(4))[0]
            if native_count==0:
                vertices=[];triangles=[]
            if any(not math.isfinite(x) for p in vertices for x in p):raise ValueError('Nonfinite liquid geometry.')
            result.append(dict(frame=frame,vertices=vertices,triangles=triangles))
        finally:evaluated.to_mesh_clear()
    return result


def render(scene,root,settings,prefix):
    for frame in settings['render_frames']:
        scene.frame_set(frame+1);scene.render.filepath=str(root/f'{prefix}frame-{frame:04d}.png')
        bpy.ops.render.render(write_still=True)


def main():
    descriptor=os.environ.get('HATCH_PHYSICS_DIRECTORY_FD')
    if descriptor is None:
        root=Path(__file__).resolve().parent
        arg=Path(sys.argv[sys.argv.index('--')+1]).resolve()
    else:
        if not descriptor.isascii() or not descriptor.isdigit():raise ValueError('Invalid pinned worker directory.')
        owned=os.fstat(int(descriptor));cwd=os.stat('.')
        if not stat.S_ISDIR(owned.st_mode) or (owned.st_dev,owned.st_ino)!=(cwd.st_dev,cwd.st_ino):
            raise ValueError('Pinned worker directory differs from its cwd.')
        root=Path('/proc/self/fd')/descriptor
        relative=Path(sys.argv[sys.argv.index('--')+1])
        if relative!=Path('liquid-job.json'):raise ValueError('Unexpected pinned liquid job path.')
        arg=root/relative
    if arg!=root/'liquid-job.json':raise ValueError('Unexpected liquid job path.')
    job=json.loads(arg.read_bytes())
    fields={'protocol','mode','expected_blend_sha256','world','settings','meshes','worker_sha256','world_sha256','settings_sha256'}
    if set(job)!=fields or job['protocol']!=1:raise ValueError('Invalid liquid worker protocol.')
    worker_source=root/'worker.py' if descriptor is not None else Path(__file__)
    if hashlib.sha256(worker_source.read_bytes()).hexdigest()!=job['worker_sha256']:raise ValueError('Worker source changed.')
    for key in ['world','settings']:
        if hashlib.sha256(canonical(job[key])).hexdigest()!=job[key+'_sha256']:raise ValueError('Liquid request bytes changed.')
    world,settings=job['world'],job['settings'];d=world['components'][0];frames=world['frame_range']['end']
    if not (len(world['components'])==1 and d['category']=='liquid' and 2<=frames<=64 and len(world['bodies'])<=16 and
        8<=d['resolution'][0]<=64 and len(set(d['resolution']))==1 and d['max_secondary_particles']==0 and
        len(settings['render_frames'])<=8 and 64<=settings['render_size']<=256):raise ValueError('Liquid worker bounds exceeded.')
    if job['mode']=='reopen':
        path=root/'scene.blend'
        if hashlib.sha256(path.read_bytes()).hexdigest()!=job['expected_blend_sha256']:raise ValueError('Native liquid project changed.')
        bpy.ops.wm.open_mainfile(filepath=str(path),load_ui=False,use_scripts=False)
        scene=bpy.context.scene;domain=bpy.data.objects[d['id']]
        if scene.render.fps!=world['fps']['numerator'] or scene.render.fps_base!=world['fps']['denominator']:
            raise ValueError('Reopened native liquid clock differs from the admitted clock.')
        samples=sample(scene,domain,reversed(range(frames)),world['budgets']['vertices'],world['budgets']['triangles'])
        render(scene,root,settings,'replay-')
        (root/'replay-mesh-samples.json').write_bytes(canonical(dict(samples=samples)))
        (root/'replay-report.json').write_bytes(canonical(dict(mode='reopen',blend_sha256=job['expected_blend_sha256'],
            frames=list(range(frames)),version=bpy.app.version_string,build=bpy.app.build_hash.decode(),
            clock=dict(fps_numerator=scene.render.fps,fps_base=scene.render.fps_base))))
        return
    if job['mode']!='simulate' or job['expected_blend_sha256'] is not None:raise ValueError('Invalid liquid operation.')
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not bpy.app.build_options.fluid or not bpy.app.build_options.openvdb:raise ValueError('Mantaflow/OpenVDB unavailable.')
    scene=bpy.context.scene;scene.frame_start=1;scene.frame_end=frames
    scene.render.fps=world['fps']['numerator'];scene.render.fps_base=world['fps']['denominator']
    if scene.render.fps!=world['fps']['numerator'] or scene.render.fps_base!=world['fps']['denominator']:
        raise ValueError('Native liquid clock changed or clamped the admitted clock.')
    scene.gravity=BASIS.to_3x3()@Vector(world['gravity'])
    objects={}
    for body in world['bodies']:
        source=job['meshes'][body['id']]
        mesh=bpy.data.meshes.new(body['id']);mesh.from_pydata(source['vertices'],[],source['triangles']);mesh.update()
        obj=bpy.data.objects.new(body['id'],mesh);scene.collection.objects.link(obj)
        t=body['transform'];q=t['rotation']
        obj.matrix_world=BASIS@(Matrix.Translation(Vector(t['translation']))@Quaternion((q[3],q[0],q[1],q[2])).to_matrix().to_4x4())
        if body['mode']=='kinematic':
            motion=source['motion']
            for key in motion['keys']:
                frame=1+key['frame']*world['fps']['numerator']*motion['fps']['denominator']/(world['fps']['denominator']*motion['fps']['numerator'])
                obj.location=BASIS.to_3x3()@Vector(key['value']);obj.keyframe_insert(data_path='location',frame=frame)
            for curve in obj.animation_data.action.fcurves:
                for key in curve.keyframe_points:key.interpolation='LINEAR' if motion['interpolation']=='linear' else 'CONSTANT'
            obj.location=BASIS.to_3x3()@Vector(t['translation'])
        objects[body['id']]=obj
    for identifier in d['emitters']:
        obj=objects[identifier];mod=obj.modifiers.new('Native liquid inlet','FLUID');mod.fluid_type='FLOW';bpy.context.view_layer.update()
        flow=mod.flow_settings;flow.flow_type='LIQUID';flow.flow_behavior=settings['flow_behavior']
        flow.use_initial_velocity=True;flow.velocity_coord=BASIS.to_3x3()@Vector(settings['initial_velocity'])
        if settings['flow_behavior']=='INFLOW':
            flow.use_inflow=True;flow.keyframe_insert(data_path='use_inflow',frame=1);flow.keyframe_insert(data_path='use_inflow',frame=settings['emission_end_frame'])
            flow.use_inflow=False;flow.keyframe_insert(data_path='use_inflow',frame=settings['emission_end_frame']+1)
        obj.hide_render=True
    for identifier in d['colliders']:
        obj=objects[identifier];mod=obj.modifiers.new('Native liquid obstacle','FLUID');mod.fluid_type='EFFECTOR';bpy.context.view_layer.update()
        mod.effector_settings.surface_distance=settings['collision_distance']
    center=BASIS.to_3x3()@Vector(d['transform']['translation']);size=d['size'][0]
    bpy.ops.mesh.primitive_cube_add(size=size,location=center);domain=bpy.context.object;domain.name=d['id']
    mod=domain.modifiers.new('Native liquid domain','FLUID');mod.fluid_type='DOMAIN';bpy.context.view_layer.update();ds=mod.domain_settings
    ds.domain_type='LIQUID';ds.resolution_max=int(d['resolution'][0])
    ds.cache_frame_start=1;ds.cache_frame_end=frames;ds.cache_type='ALL';ds.cache_data_format='OPENVDB';ds.cache_mesh_format='BOBJECT';ds.cache_directory=str(root/'cache')
    ds.use_mesh=True;ds.mesh_scale=settings['mesh_scale'];ds.particle_radius=settings['particle_radius']
    ds.use_spray_particles=False;ds.use_foam_particles=False;ds.use_bubble_particles=False
    ds.use_viscosity=d['viscosity']>0;ds.viscosity_value=d['viscosity']
    ds.cfl_condition=settings['cfl'];ds.timesteps_min=settings['minimum_substeps'];ds.timesteps_max=settings['maximum_substeps']
    for name,value in settings['boundaries'].items():setattr(ds,'use_collision_border_'+name,value)
    observed=dict(fps_numerator=scene.render.fps,fps_base=scene.render.fps_base,
        resolution=ds.resolution_max,mesh_scale=ds.mesh_scale,particle_radius=ds.particle_radius,
        viscosity_enabled=ds.use_viscosity,viscosity=ds.viscosity_value,cfl=ds.cfl_condition,
        minimum_substeps=ds.timesteps_min,maximum_substeps=ds.timesteps_max,
        boundaries={name:getattr(ds,'use_collision_border_'+name) for name in settings['boundaries']})
    bpy.context.view_layer.objects.active=domain;bpy.ops.object.select_all(action='DESELECT');domain.select_set(True)
    bpy.ops.wm.save_as_mainfile(filepath=str(root/'scene.blend'))
    if bpy.ops.fluid.bake_all()!={'FINISHED'} or not ds.has_cache_baked_data or not ds.has_cache_baked_mesh:
        raise ValueError('Native liquid data and mesh baking did not complete.')
    samples=sample(scene,domain,range(frames),world['budgets']['vertices'],world['budgets']['triangles'])
    ds.cache_directory='//cache'
    material=bpy.data.materials.new('Liquid review blue');material.diffuse_color=(.03,.25,.8,1);material.use_nodes=True
    material.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value=(.03,.25,.8,1)
    material.node_tree.nodes['Principled BSDF'].inputs['Roughness'].default_value=.2;domain.data.materials.append(material)
    for name,obj in objects.items():
        if name in ('front','right'):obj.hide_render=True
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=settings['render_samples'];scene.cycles.seed=0;scene.cycles.use_adaptive_sampling=False
    scene.render.resolution_x=scene.render.resolution_y=settings['render_size'];scene.render.resolution_percentage=100
    scene.world=bpy.data.worlds.new('Liquid review');scene.world.color=(.15,.15,.15)
    bpy.ops.object.camera_add(location=center+Vector((size*1.5,-size*2,size*.8)));camera=bpy.context.object
    camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler();camera.data.type='ORTHO';camera.data.ortho_scale=size*1.5;scene.camera=camera
    bpy.ops.object.light_add(type='AREA',location=center+Vector((size,-size,size*2)));lamp=bpy.context.object;lamp.data.energy=1500;lamp.data.size=size*2
    lamp.rotation_euler=(center-lamp.location).to_track_quat('-Z','Y').to_euler()
    render(scene,root,settings,'')
    bpy.ops.wm.save_as_mainfile(filepath=str(root/'scene.blend'))
    report=dict(protocol=1,backend='Blender Mantaflow',version=bpy.app.version_string,build=bpy.app.build_hash.decode(),
        native_bake=True,frame_count=frames,cache_format='OPENVDB+BOBJECT',observed=observed,
        world_sha256=job['world_sha256'],settings_sha256=job['settings_sha256'],render_frames=settings['render_frames'],
        numerical_clock='native adaptive CFL; no rigid fixed-step equivalence claimed',
        coupling='authored kinematic obstacle to liquid only; no liquid-to-rigid feedback',mass_conservation='not guaranteed or certified')
    (root/'liquid-report.json').write_bytes(canonical(report));(root/'mesh-samples.json').write_bytes(canonical(dict(samples=samples)))


if __name__=='__main__':main()
