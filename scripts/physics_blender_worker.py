# Exact-clock adapter revision: native integral stepping restrictions recorded in timing.json.
"""Package-owned Blender physics worker. Not an entry point for arbitrary scenes.

The parent snapshots this exact file into a private workspace and supplies only
validated primitive geometry/configuration. This worker never imports user
Python, opens an uploaded .blend, loads an add-on, or evaluates a scene string.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector


PROTOCOL = 1
LAST_VALID_FRAME = None
BASIS = Matrix(((1., 0., 0., 0.), (0., 0., -1., 0.),
                (0., 1., 0., 0.), (0., 0., 0., 1.)))


class NativeFailure(ValueError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def emit(root, value):
    (root / 'worker-report.json').write_bytes(canonical(value))


def checked_job(path):
    path = Path(path)
    if path.name != 'worker-job.json' or path.is_symlink():
        raise ValueError('Expected the private worker-job.json.')
    if path.resolve().parent != Path(__file__).resolve().parent:
        raise ValueError('Worker job must be beside the exact privately copied worker.')
    payload = path.read_bytes()
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError('Worker job exceeds 32 MiB.')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate worker input key.')
            value[key] = item
        return value
    value = json.loads(payload, object_pairs_hook=pairs)
    keys = {'protocol', 'mode', 'world', 'world_sha256', 'meshes', 'worker_sha256', 'fixture'}
    if value.get('mode') in ('render-replay', 'reopen-replay'):
        keys.add('replay')
    if set(value) != keys:
        raise ValueError('Unknown or missing worker input.')
    if value['protocol'] != PROTOCOL or value['mode'] not in ('simulate', 'reopen', 'consumption-control', 'render-replay', 'reopen-replay'):
        raise ValueError('Unsupported worker operation.')
    if hashlib.sha256(canonical(value['world'])).hexdigest() != value['world_sha256']:
        raise ValueError('World digest differs from admitted configuration.')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != value['worker_sha256']:
        raise ValueError('Worker digest differs from admitted package bytes.')
    world = value['world']
    if len(world['bodies']) > 32 or not 1 <= world['frame_range']['end'] <= 1024:
        raise ValueError('Worker body/frame bound exceeded.')
    if world['frame_range']['start'] != 0 or world['components'] or world['joints'] or world['events']:
        raise ValueError('This worker supports only zero-origin, uncoupled rigid-body jobs.')
    return path.resolve().parent, value


def backend_identity():
    devices = []
    discovery = 'not-available'
    try:
        addon = bpy.context.preferences.addons.get('cycles')
        if addon is not None:
            addon.preferences.get_devices()
            devices = [dict(name=str(device.name)[:256], type=str(device.type))
                       for device in addon.preferences.devices][:64]
            discovery = 'executed-device-enumeration-only'
    except Exception as exc:
        discovery = 'discovery-failed:' + type(exc).__name__
    return dict(application_version=bpy.app.version_string,
                application_build=bpy.app.build_hash.decode('ascii'),
                solver_name='Blender Bullet rigid-body world',
                solver_version='not exposed separately by Blender',
                platform=sys.platform, precision='mixed', device='cpu',
                render_device_discovery=discovery, render_devices=devices,
                physics_gpu_support='unsupported', render_gpu_execution='not-executed')


def consumption_control(root, job):
    """Separate in-memory control; never saves/changes the retained .blend.

    Baked seeking under reversed gravity is compared with an explicit official
    Free Bake + clean recomputation. A matching pose alone is not consumption
    evidence. The pinned source explains why OUTDATED may coexist with BAKED.
    """
    world = job['world']
    dynamic = next(body for body in world['bodies'] if body['mode'] == 'dynamic')
    original = Vector(world['gravity'])
    altered = -10 * original if original.length > .01 else Vector((0, 100., 0))
    altered_native = BASIS.to_3x3() @ altered
    bpy.ops.wm.open_mainfile(filepath=str(root/'scene.blend'), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    cache = scene.rigidbody_world.point_cache
    before = dict(is_baked=cache.is_baked, is_outdated=cache.is_outdated)
    scene.gravity = altered_native
    frame = int(world['frame_range']['end'])-1
    baked = samples(scene, [dynamic], [frame])[0]['bodies'][0]['translation']
    bpy.ops.wm.open_mainfile(filepath=str(root/'scene.blend'), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    cache = scene.rigidbody_world.point_cache
    with bpy.context.temp_override(scene=scene, point_cache=cache):
        outcome = bpy.ops.ptcache.free_bake()
    if outcome != {'FINISHED'} or cache.is_baked:
        raise ValueError('Independent Free Bake control failed.')
    scene.gravity = altered_native
    recomputed = samples(scene, [dynamic], range(frame+1))[-1]['bodies'][0]['translation']
    distance = (Vector(recomputed)-Vector(baked)).length
    reference = json.loads((root/'states.json').read_bytes())['samples'][frame]['bodies']
    expected = next(body for body in reference if body['id'] == dynamic['id'])['translation']
    cache_error = (Vector(expected)-Vector(baked)).length
    return dict(ok=True, operation='consumption-control', backend=backend_identity(),
        source_file_modified=False, control_is_separate_in_memory=True,
        baked_flags=before, after_free_bake_flags=dict(is_baked=cache.is_baked, is_outdated=cache.is_outdated),
        changed_gravity_canonical=list(altered), body=dynamic['id'], frame=frame,
        baked_under_changed_gravity=baked, clean_recompute_under_changed_gravity=recomputed,
        baked_pose_error=cache_error, changed_gravity_displacement=distance,
        consumption_demonstrated=cache_error <= 1e-5 and distance > .01)


def samples(scene, bodies, frames):
    global LAST_VALID_FRAME
    result = []
    for frame in frames:
        scene.frame_set(frame + 1)
        graph = bpy.context.evaluated_depsgraph_get()
        states = []
        for body in bodies:
            obj = bpy.data.objects.get(body['id'])
            if obj is None:
                raise ValueError('A physical object disappeared during simulation.')
            matrix = BASIS.transposed() @ obj.evaluated_get(graph).matrix_world @ BASIS
            translation, rotation, scale = matrix.decompose()
            values = [*translation, rotation.x, rotation.y, rotation.z, rotation.w, *scale]
            if not all(math.isfinite(v) for v in values) or max(abs(v) for v in translation) > 1_000_000:
                raise NativeFailure('PHYSICS_NUMERICAL_INSTABILITY','Nonfinite or excessive physical state.')
            if any(abs(s - 1.) > 1e-5 for s in scale):
                raise NativeFailure('PHYSICS_NUMERICAL_INSTABILITY','Unexpected body scale after native evaluation.')
            states.append(dict(id=body['id'], translation=list(translation),
                               rotation=[rotation.x, rotation.y, rotation.z, rotation.w]))
        result.append(dict(frame=frame, bodies=states))
        LAST_VALID_FRAME=frame
    return result


def preview(root,scene,bodies, replay_framing=None):
    """A small, explicitly neutral diagnostic render, never art approval."""
    points=[bpy.data.objects[body['id']].evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.copy() for body in bodies]
    center=sum(points,Vector())/len(points)
    extent=max(1.,max((point-center).length for point in points))
    # Include actual mesh extents, so a broad floor is not cropped to its origin.
    extent=max(extent,max(max(bpy.data.objects[body['id']].dimensions) for body in bodies))
    if replay_framing is not None:
        center, extent = replay_framing
    camera_data=bpy.data.cameras.new('physics-diagnostic-camera')
    camera=bpy.data.objects.new('physics-diagnostic-camera',camera_data)
    scene.collection.objects.link(camera)
    camera.location=center+Vector((1.,-1.4,1.1)).normalized()*extent*2
    camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
    camera_data.type='ORTHO';camera_data.ortho_scale=extent*1.8
    if replay_framing is not None:
        camera_data.clip_end=max(1000., extent*10.)
    scene.camera=camera
    light_data=bpy.data.lights.new('physics-diagnostic-light','AREA')
    light=bpy.data.objects.new('physics-diagnostic-light',light_data)
    scene.collection.objects.link(light)
    light.location=center+Vector((2.,-2.,4.)).normalized()*extent
    light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler()
    light_data.energy=1000.;light_data.shape='DISK';light_data.size=extent
    environment=bpy.data.worlds.new('physics-diagnostic-world')
    environment.use_nodes=True
    environment.node_tree.nodes['Background'].inputs['Color'].default_value=(.3,.3,.3,1)
    environment.node_tree.nodes['Background'].inputs['Strength'].default_value=.8
    scene.world=environment
    for body in bodies:
        material=bpy.data.materials.new(body['id']+'-neutral-diagnostic')
        material.diffuse_color=(.1,.4,.8,1) if body['mode']=='dynamic' else (.35,.35,.35,1)
        if replay_framing is not None:
            material.use_nodes=True
            surface=material.node_tree.nodes.get('Principled BSDF')
            surface.inputs['Base Color'].default_value=material.diffuse_color
            surface.inputs['Roughness'].default_value=.65
        bpy.data.objects[body['id']].data.materials.append(material)
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=4
    scene.render.resolution_x=128;scene.render.resolution_y=128;scene.render.resolution_percentage=100
    scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA'
    scene.render.film_transparent=True;scene.render.filepath=str(root/'preview.png')
    if bpy.ops.render.render(write_still=True)!={'FINISHED'} or not (root/'preview.png').is_file():
        raise ValueError('Actual diagnostic render did not complete.')
    return dict(path='preview.png',sha256=hashlib.sha256((root/'preview.png').read_bytes()).hexdigest(),
                engine='Cycles',device='CPU',width=128,height=128,samples=4,
                scope='neutral solver diagnostic only',visual_review='not-performed')


def create(root, job):
    world = job['world']
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = int(world['frame_range']['end'])
    scene.render.fps = int(world['fps']['numerator'])
    scene.render.fps_base = float(world['fps']['denominator'])
    scene.gravity = (BASIS.to_3x3() @ Vector(world['gravity']))[:]
    materials = {item['id']: item for item in world['materials']}
    for body in world['bodies']:
        if body['category'] != 'rigid-body' or body['mode'] not in ('static', 'dynamic', 'kinematic'):
            raise ValueError('Unsupported body authority or category.')
        proxy = body['colliders'][0]
        if len(body['colliders']) != 1 or proxy['shape']['type'] not in ('sphere', 'box'):
            raise ValueError('Unsupported collision proxy.')
        source = job['meshes'][body['id']]
        if len(source['vertices']) > 4096 or len(source['triangles']) > 8192:
            raise ValueError('Worker geometry bound exceeded.')
        mesh = bpy.data.meshes.new(body['id'] + '-mesh')
        vertices = [tuple(BASIS.to_3x3() @ Vector(v)) for v in source['vertices']]
        mesh.from_pydata(vertices, [], source['triangles'])
        if mesh.validate(verbose=False):
            raise ValueError('Native mesh validation modified admitted geometry.')
        mesh.update()
        obj = bpy.data.objects.new(body['id'], mesh)
        scene.collection.objects.link(obj)
        obj.location = BASIS.to_3x3() @ Vector(body['transform']['translation'])
        x, y, z, w = body['transform']['rotation']
        rotation = Quaternion((w, x, y, z)).to_matrix().to_4x4()
        obj.rotation_mode = 'QUATERNION'
        obj.rotation_quaternion = (BASIS @ rotation @ BASIS.transposed()).to_quaternion()
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.rigidbody.object_add()
        rb = obj.rigid_body
        rb.type = 'PASSIVE' if body['mode'] == 'static' else 'ACTIVE'
        rb.kinematic = body['mode'] == 'kinematic'
        rb.collision_shape = 'SPHERE' if proxy['shape']['type'] == 'sphere' else 'BOX'
        rb.use_margin = True
        material = materials[proxy['material']]
        rb.friction = material['dynamic_friction']
        rb.restitution = material['restitution']
        rb.linear_damping = material['linear_damping']
        rb.angular_damping = material['angular_damping']
        rb.collision_margin = material['collision_margin']
        sleep = body.get('sleep',dict(enabled=False,initially_sleeping=False,linear_threshold=.4,angular_threshold=.5))
        rb.use_deactivation = sleep['enabled']
        rb.use_start_deactivated = sleep['initially_sleeping']
        rb.deactivate_linear_velocity = sleep.get('linear_threshold',.4)
        rb.deactivate_angular_velocity = sleep.get('angular_threshold',.5)
        if body['mode'] == 'kinematic':
            motion=source['motion']
            for key in motion['keys']:
                frame=1+key['frame']*world['fps']['numerator']*motion['fps']['denominator']/(world['fps']['denominator']*motion['fps']['numerator'])
                obj.location=BASIS.to_3x3() @ Vector(key['value'])
                obj.keyframe_insert(data_path='location',frame=frame)
            for curve in obj.animation_data.action.fcurves:
                for key in curve.keyframe_points:key.interpolation='LINEAR' if motion['interpolation']=='linear' else 'CONSTANT'
            obj.location=BASIS.to_3x3() @ Vector(body['transform']['translation'])
        if body['mode'] == 'dynamic':
            rb.mass = body['mass']['kilograms']
        obj.select_set(False)
    effectors=[]
    for force in world['forces']:
        vector=BASIS.to_3x3() @ Vector([x*force['strength'] for x in force['vector']])
        bpy.ops.object.effector_add(type='WIND', location=(0,0,0))
        effector=bpy.context.object;effector.name='physical-force-'+force['id']
        effector.rotation_euler=vector.to_track_quat('Z','Y').to_euler() if vector.length else (0,0,0)
        field=effector.field;field.shape='PLANE';field.flow=0;field.noise=0;field.falloff_power=0
        for frame,strength in [(1,0),(force['frame_range']['start']+2,vector.length*scene.render.fps),(force['frame_range']['end']+2,0)]:
            field.strength=strength;field.keyframe_insert(data_path='strength',frame=frame)
        for curve in effector.animation_data.action.fcurves:
            for key in curve.keyframe_points:key.interpolation='CONSTANT'
        effectors.append(dict(id=force['id'],object=effector.name,type=field.type,shape=field.shape,flow=field.flow,falloff_power=field.falloff_power,strength_per_newton=scene.render.fps))
    physics = scene.rigidbody_world
    if physics is None:
        raise ValueError('Native rigid-body world was not created.')
    fps = world['fps']['numerator'] / world['fps']['denominator']
    step = world['solver']['fixed_step']['numerator'] / world['solver']['fixed_step']['denominator']
    physics.substeps_per_frame = round(world['solver']['substeps'] / (fps * step))
    physics.solver_iterations = int(world['solver']['iterations'])
    cache = physics.point_cache
    cache.frame_start = 1
    cache.frame_end = scene.frame_end
    scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=str(root / 'scene.blend'))
    with bpy.context.temp_override(scene=scene, point_cache=cache):
        outcome = bpy.ops.ptcache.bake(bake=True)
    if outcome != {'FINISHED'} or not cache.is_baked:
        raise ValueError('Native rigid-body point cache bake did not complete.')
    values = samples(scene, world['bodies'], range(scene.frame_end))
    (root / 'states.json').write_bytes(canonical(dict(schema_version=1,
        kind='hatch-physics-samples', world_sha256=job['world_sha256'],
        frame_range=world['frame_range'], fps=world['fps'],
        fixture=job['fixture'], samples=values)))
    image=preview(root,scene,world['bodies'])
    bpy.ops.wm.save_as_mainfile(filepath=str(root / 'scene.blend'))
    return dict(ok=True, operation='simulate', backend=backend_identity(),
        native_bake_completed=True, native_cache_is_baked=cache.is_baked,
        native_cache_is_outdated=cache.is_outdated,
        cache_storage='embedded Blender point cache',
        sample_count=len(values), object_count=sum(obj.rigid_body is not None for obj in scene.objects),
        diagnostic_render=image,
        actual_substeps_per_frame=physics.substeps_per_frame,
        actual_solver_iterations=physics.solver_iterations,
        actual_fps=dict(numerator=scene.render.fps, denominator=scene.render.fps_base),
        actual_gravity_canonical=list(BASIS.to_3x3().transposed() @ scene.gravity),
        effective_mass_policy='explicit body mass; material density does not override body mass',
        seed_policy='recorded identity only; no stochastic parameter is used by this adapter',
        quality_policy='explicit admitted substeps/iterations; quality label does not retune solver',
        sphere_margin_policy='analytic sphere radius is inherent; nonzero additional skin is rejected',
        rigid_controls=dict(version=1,force_policy='Blender native WIND plane effector central force only',forces=world['forces'],effectors=effectors,
            bodies=[dict(id=body['id'],mode=body['mode'],kinematic=bpy.data.objects[body['id']].rigid_body.kinematic,
                sleep=dict(enabled=bpy.data.objects[body['id']].rigid_body.use_deactivation,
                    initially_sleeping=bpy.data.objects[body['id']].rigid_body.use_start_deactivated,
                    linear_threshold=bpy.data.objects[body['id']].rigid_body.deactivate_linear_velocity,
                    angular_threshold=bpy.data.objects[body['id']].rigid_body.deactivate_angular_velocity)) for body in world['bodies']]),
        actual_bodies=[dict(id=body['id'], type=bpy.data.objects[body['id']].rigid_body.type,
            shape=bpy.data.objects[body['id']].rigid_body.collision_shape,
            effective_mass=body['mass']['kilograms'] if body['mode']!='dynamic' else bpy.data.objects[body['id']].rigid_body.mass,
            friction=bpy.data.objects[body['id']].rigid_body.friction,
            restitution=bpy.data.objects[body['id']].rigid_body.restitution,
            linear_damping=bpy.data.objects[body['id']].rigid_body.linear_damping,
            angular_damping=bpy.data.objects[body['id']].rigid_body.angular_damping,
            collision_margin=bpy.data.objects[body['id']].rigid_body.collision_margin) for body in world['bodies']])


def reopen(root, job):
    # This exact path is generated by create() in the same private workspace.
    # The public adapter never accepts arbitrary .blend input files.
    bpy.ops.wm.open_mainfile(filepath=str(root / 'scene.blend'), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    cache = scene.rigidbody_world.point_cache
    if not cache.is_baked or cache.frame_end != job['world']['frame_range']['end']:
        raise ValueError('Saved native baked cache is missing or incomplete.')
    source = json.loads((root / 'states.json').read_bytes())
    count = len(source['samples'])
    # Reverse order proves cached seeking, rather than another sequential run.
    reread = samples(scene, job['world']['bodies'], reversed(range(count)))
    maximum = 0.
    for actual in reread:
        expected = source['samples'][actual['frame']]
        for left, right in zip(actual['bodies'], expected['bodies']):
            if left['id'] != right['id']:
                raise ValueError('Reopened body identity differs.')
            maximum = max(maximum, *(abs(a-b) for a, b in zip(left['translation'], right['translation'])))
            dot = abs(sum(a*b for a, b in zip(left['rotation'], right['rotation'])))
            maximum = max(maximum, abs(1. - dot))
    if maximum > 1e-5:
        raise ValueError('Reopened baked state differs from actual solver samples.')
    return dict(ok=True, operation='reopen', backend=backend_identity(),
                native_bake_completed=True, native_cache_is_baked=cache.is_baked,
                native_cache_is_outdated=cache.is_outdated, reversed_samples=count,
                maximum_replay_error=maximum, replay_tolerance=1e-5)


def replay_byte_count(value):
    """JSON Schema integer also permits finite integral-valued JSON floats."""
    if (type(value) not in (int, float) or not 0 <= value <= 256*1024*1024 or
        (type(value) is float and (not math.isfinite(value) or not value.is_integer()))):
        raise ValueError('Unbounded or nonintegral replay source size.')
    return int(value)


def replay_inputs(root, job):
    """Reject substitutions before loading our own generated .blend."""
    replay = job['replay']
    if set(replay) != {'cache_manifest_sha256', 'states_sha256', 'source_files',
                       'project_manifest', 'source_backend', 'meshes_sha256'}:
        raise ValueError('Unexpected replay binding.')
    source = root/'source-cache'
    payloads = {}
    # Descriptor relative, no-follow reads, including every ancestor.
    for item in replay['source_files']:
        if set(item) != {'path', 'bytes', 'sha256'}:
            raise ValueError('Invalid replay inventory.')
        byte_count = replay_byte_count(item['bytes'])
        parts = item['path'].split('/')
        if any(part in ('', '.', '..') for part in parts) or item['path'].startswith('/'):
            raise ValueError('Invalid replay source path.')
        fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                import stat
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != byte_count:
                    raise ValueError('Unbounded replay source.')
                data = stream.read(byte_count+1)
                after = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(data) != byte_count or hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise ValueError('Replay source changed.')
        finally:
            os.close(fd)
        if item['path'] in payloads:
            raise ValueError('Duplicate replay source.')
        payloads[item['path']] = data
    manifest = json.loads(payloads['cache.json'])
    if (hashlib.sha256(payloads['cache.json']).hexdigest() != replay['cache_manifest_sha256'] or
        hashlib.sha256(payloads['states.json']).hexdigest() != replay['states_sha256'] or
        manifest['backend'] != replay['source_backend'] or manifest['backend']['id'] != 'pybullet' or
        manifest['world_sha256'] != job['world_sha256'] or
        hashlib.sha256(payloads['world.json']).hexdigest() != job['world_sha256'] or
        hashlib.sha256(canonical(job['meshes'])).hexdigest() != replay['meshes_sha256'] or
        {item['path'] for item in manifest['files']} | {'cache.json'} != set(payloads)):
        raise ValueError('Replay source identity mismatch.')
    for item in manifest['files']:
        if hashlib.sha256(payloads[item['path']]).hexdigest() != item['sha256'] or len(payloads[item['path']]) != item['bytes']:
            raise ValueError('Replay manifest mismatch.')
    states = json.loads(payloads['states.json'])
    if states['world_sha256'] != job['world_sha256']:
        raise ValueError('Replay states world mismatch.')
    expected = list(range(job['world']['frame_range']['end']))
    if [sample['frame'] for sample in states['samples']] != expected:
        raise ValueError('Replay requires every integer cached frame.')
    ids = [body['id'] for body in job['world']['bodies']]
    for sample in states['samples']:
        if [body['id'] for body in sample['bodies']] != ids:
            raise ValueError('Replay body identity mismatch.')
        for body in sample['bodies']:
            if not all(math.isfinite(v) for v in body['translation']+body['rotation']):
                raise ValueError('Nonfinite replay pose.')
    return states


def replay_scene(root, job):
    states = replay_inputs(root, job)
    bodies = job['world']['bodies']
    images = []
    if job['mode'] == 'render-replay':
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.context.preferences.filepaths.save_version = 0
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = len(states['samples'])
        scene['physics_scope'] = 'DIRECT solved-state replay; Blender rendering only'
        scene['source_cache_sha256'] = job['replay']['cache_manifest_sha256']
        scene['states_sha256'] = job['replay']['states_sha256']
        scene['source_backend'] = canonical(job['replay']['source_backend']).decode('utf-8')
        scene['interpolation_accuracy'] = 'not-established; only integer cached frames verified'
        scene.render.fps = int(job['world']['fps']['numerator'])
        scene.render.fps_base = float(job['world']['fps']['denominator'])
        for body in bodies:
            source = job['meshes'][body['id']]
            if len(source['vertices']) > 4096 or len(source['triangles']) > 8192:
                raise ValueError('Replay geometry bound exceeded.')
            mesh = bpy.data.meshes.new(body['id']+'-mesh')
            mesh.from_pydata([tuple(BASIS.to_3x3() @ Vector(v)) for v in source['vertices']], [], source['triangles'])
            if mesh.validate(verbose=False):
                raise ValueError('Replay mesh changed during native validation.')
            mesh.update()
            obj = bpy.data.objects.new(body['id'], mesh)
            scene.collection.objects.link(obj)
            obj.rotation_mode = 'QUATERNION'
            for sample in states['samples']:
                pose = next(item for item in sample['bodies'] if item['id'] == body['id'])
                obj.location = BASIS.to_3x3() @ Vector(pose['translation'])
                x,y,z,w = pose['rotation']
                obj.rotation_quaternion = (BASIS @ Quaternion((w,x,y,z)).to_matrix().to_4x4() @ BASIS.transposed()).to_quaternion()
                obj.keyframe_insert(data_path='location', frame=sample['frame']+1)
                obj.keyframe_insert(data_path='rotation_quaternion', frame=sample['frame']+1)
        scene.frame_set(1)
        # Setup camera, lights and neutral materials exactly once.
        # Transform all eight corners of each admitted mesh's local bounds at
        # every solved pose. Thin floors retain their actual vertical extent.
        import itertools
        corners = {}
        for body in bodies:
            vertices = job['meshes'][body['id']]['vertices']
            bounds = [(min(v[axis] for v in vertices), max(v[axis] for v in vertices)) for axis in range(3)]
            corners[body['id']] = [Vector(point) for point in itertools.product(*bounds)]
        low = Vector((math.inf, math.inf, math.inf))
        high = Vector((-math.inf, -math.inf, -math.inf))
        for sample in states['samples']:
            for pose in sample['bodies']:
                origin = Vector(pose['translation'])
                x,y,z,w = pose['rotation']
                rotation = Quaternion((w,x,y,z))
                for corner in corners[pose['id']]:
                    point = BASIS.to_3x3() @ (origin + rotation @ corner)
                    for axis in range(3):
                        low[axis] = min(low[axis], point[axis])
                        high[axis] = max(high[axis], point[axis])
        center = (low+high)*.5
        # Half diagonal bounds the projected radius for every camera direction;
        # 1.3 * existing 1.8 scale leaves margin around its full diameter.
        extent = max(1., (high-low).length*.5)*1.3
        scene['replay_camera_scope'] = 'complete cached trajectory and transformed actual mesh bounds'
        image = preview(root, scene, bodies, replay_framing=(center, extent))
        frames = sorted({0, len(states['samples'])//2, len(states['samples'])-1})
        image['frame'] = 0; image['scope'] = 'DIRECT solved-state replay; neutral diagnostic only'
        images.append(image)
        for frame in frames[1:]:
            scene.frame_set(frame+1)
            name = 'preview-%04d.png' % frame
            scene.render.filepath = str(root/name)
            if bpy.ops.render.render(write_still=True) != {'FINISHED'}:
                raise ValueError('Replay render failed.')
            images.append(dict(image, frame=frame, path=name, sha256=hashlib.sha256((root/name).read_bytes()).hexdigest()))
        bpy.ops.wm.save_as_mainfile(filepath=str(root/'scene.blend'))
    else:
        if (root/'scene.blend').is_symlink():
            raise ValueError('Unsafe replay scene.')
        bpy.ops.wm.open_mainfile(filepath=str(root/'scene.blend'), load_ui=False, use_scripts=False)
        scene = bpy.context.scene
    if (scene.rigidbody_world is not None or scene.get('source_cache_sha256') != job['replay']['cache_manifest_sha256'] or
        scene.get('states_sha256') != job['replay']['states_sha256'] or
        scene.get('source_backend') != canonical(job['replay']['source_backend']).decode('utf-8') or
        {obj.name for obj in scene.objects if obj.type == 'MESH'} != {body['id'] for body in bodies}):
        raise ValueError('Saved replay native identity differs.')
    for body in bodies:
        obj = bpy.data.objects[body['id']]
        source = job['meshes'][body['id']]
        if obj.rigid_body is not None or len(obj.data.vertices) != len(source['vertices']) or len(obj.data.polygons) != len(source['triangles']):
            raise ValueError('Replay geometry/native solver identity differs.')
        for vertex, expected in zip(obj.data.vertices, source['vertices']):
            if (vertex.co - BASIS.to_3x3() @ Vector(expected)).length > 1e-6:
                raise ValueError('Reopened actual mesh vertices differ.')
        if [list(face.vertices) for face in obj.data.polygons] != source['triangles']:
            raise ValueError('Reopened actual mesh topology differs.')
    actual = samples(scene, bodies, reversed(range(len(states['samples']))))
    maximum = 0.
    for sample in actual:
        reference = states['samples'][sample['frame']]
        for pose, expected in zip(sample['bodies'], reference['bodies']):
            maximum = max(maximum, max(abs(a-b) for a,b in zip(pose['translation'], expected['translation'])))
            # q and -q encode the identical rotation.
            maximum = max(maximum, min(max(abs(a-b) for a,b in zip(pose['rotation'], expected['rotation'])),
                                       max(abs(a+b) for a,b in zip(pose['rotation'], expected['rotation']))))
    if maximum > 1e-5:
        raise ValueError('Native replay cached poses differ.')
    return dict(ok=True, operation=job['mode'], backend=backend_identity(),
                verified_frames=len(actual), maximum_replay_error=maximum,
                source_cache_sha256=job['replay']['cache_manifest_sha256'],
                states_sha256=job['replay']['states_sha256'], previews=images,
                worker_sha256=job['worker_sha256'], world_sha256=job['world_sha256'],
                source_backend=job['replay']['source_backend'],
                blend_sha256=hashlib.sha256((root/'scene.blend').read_bytes()).hexdigest(),
                scope='DIRECT solved-state replay; Blender rendering only', artistic_review='not-performed')


def main():
    root, job = checked_job(sys.argv[sys.argv.index('--') + 1])
    if tuple(bpy.app.version) != (4, 5, 3) or bpy.app.build_hash.decode('ascii') != '67807e1800cc':
        raise ValueError('Only actually tested Blender 4.5.3 build67807e1800cc is admitted.')
    if job['mode'] == 'simulate':
        result = create(root, job)
    elif job['mode'] == 'reopen':
        result = reopen(root, job)
    elif job['mode'] in ('render-replay', 'reopen-replay'):
        result = replay_scene(root, job)
    else:
        result = consumption_control(root, job)
    emit(root, result)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        root=Path(__file__).resolve().parent
        if root.name.startswith('hatch-physics-prepared-'):
            emit(root,dict(ok=False,error=dict(code=getattr(exc,'code','PHYSICS_BACKEND_CRASH'),
                message=str(exc)[:2048],last_valid_frame=LAST_VALID_FRAME,
                category='numerical-instability' if isinstance(exc,NativeFailure) else 'backend-crash',
                cache_valid=False,recoverable=False)))
        raise
