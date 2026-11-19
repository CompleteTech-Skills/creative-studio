"""Resolved Camera3D optics applied to an existing Blender scene.

This is an in-process adapter, not a scene loader, renderer, or job scheduler.
Callers own immutable source resolution and render publication. Importing this
module never imports bpy; apply_camera requires actual Blender execution.
"""
from __future__ import annotations

import math

from camera3d_contracts import validate
from camera3d_math import optical_gate, world_transform, blender_camera_transform, _matrix
from lookdev_color import (CAMERA_COLOR_OWNER, COLOR_HASH, EFFECTIVE_COLOR_HASH,
                           camera_is_shared, color_state, effective_color_hash,
                           release_camera_color, restore_color_state, restore_property,
                           verify_color_state)


def camera_world(camera, *, parent_world=None, rig=None):
    """Compose an already resolved scene parent and optional Camera3D rig mount.

The scene owner supplies parent_world; this adapter does not guess unresolved
node transforms. A rig mount's full ancestor chain precedes camera-local TRS.
"""
    import numpy as np
    validate(camera, 'camera')
    if camera['parent'] is not None and parent_world is None:
        raise ValueError('camera3d scene-parent world transform is required')
    parent = np.eye(4) if parent_world is None else _matrix(parent_world, rigid=True)
    if rig is not None:
        validate(rig, 'rig')
        nodes = {node['id']: node for node in rig['nodes']}
        mounts = [node for node in nodes.values() if node['camera'] is not None
                  and node['camera']['id'] == camera['id']
                  and node['camera']['version'] == camera['version']]
        if len(mounts) != 1:
            raise ValueError('camera3d rig must mount this camera exactly once')
        chain = []
        node = mounts[0]
        while node is not None:
            chain.append(node)
            node = nodes.get(node['parent'])
        for node in reversed(chain):
            parent = world_transform(node['transform'], parent)
    return world_transform(camera['transform'], parent)


def optical_plan(optics, render, *, exposure=0.0, motion_blur=False, shutter_frames=0.5):
    """Validate and derive Blender's optical controls without importing bpy."""
    width, height = optical_gate(optics, render)
    for value, low, high, label in ((exposure, -32, 32, 'exposure'),
                                   (shutter_frames, 0, 1, 'shutter_frames')):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value):
            raise ValueError(f'camera3d {label} must be finite in [{low},{high}]')
    if not isinstance(motion_blur, bool):
        raise ValueError('camera3d motion_blur must be boolean')
    p, dof = optics['projection'], optics['depth_of_field']
    if p['type'] == 'orthographic' and dof['enabled']:
        raise ValueError('camera3d Blender orthographic physical DOF is unsupported')
    if dof['aperture_blades'] in (1, 2):
        raise ValueError('camera3d Blender aperture blades must be zero or at least three')
    aspect = width / height
    return dict(type='PERSP' if p['type'] == 'perspective' else 'ORTHO',
                lens=p.get('focal_length_mm'),
                # Blender's sensor width is always millimeters, even when its
                # orthographic scale expresses world-space meter coverage.
                sensor_width=width * render['overscan'] if p['type'] == 'perspective' else 36.0,
                ortho_scale=width * render['overscan'],
                shift_x=-p['shift_x'] / render['overscan'],
                shift_y=-p['shift_y'] / (render['overscan'] * aspect),
                clip_start=p['near_clip_m'], clip_end=p['far_clip_m'],
                use_dof=dof['enabled'], focus_distance=dof['focus_distance_m'],
                aperture_fstop=dof['f_stop'], aperture_blades=dof['aperture_blades'],
                aperture_rotation=dof['aperture_rotation_rad'], aperture_ratio=dof['anamorphic_ratio'],
                exposure=float(exposure), motion_blur=motion_blur, shutter_frames=float(shutter_frames))


def _verify_exact(owner, attribute, value):
    observed = getattr(owner, attribute)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isclose(float(observed), float(value), rel_tol=2e-6,
                            abs_tol=min(1e-7, abs(float(value)) * 2e-6)):
            raise ValueError(f'camera3d Blender cannot represent {attribute}={value}')
    elif observed != value:
        raise ValueError(f'camera3d Blender cannot represent {attribute}={value}')


def _set_exact(owner, attribute, value):
    setattr(owner, attribute, value)
    _verify_exact(owner, attribute, value)


def _preflight(owner, attribute, value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        property_ = owner.bl_rna.properties[attribute]
        if not property_.hard_min <= value <= property_.hard_max:
            raise ValueError(f'camera3d Blender cannot represent {attribute}={value}')


def _camera_settings(camera, plan):
    yield camera, 'type', plan['type']
    yield camera, 'sensor_fit', 'HORIZONTAL'
    for key in ('sensor_width', 'shift_x', 'shift_y', 'clip_start', 'clip_end'):
        yield camera, key, plan[key]
    scale = 'lens' if plan['type'] == 'PERSP' else 'ortho_scale'
    yield camera, scale, plan[scale]
    yield camera.dof, 'focus_object', None
    for key in ('use_dof', 'focus_distance', 'aperture_fstop', 'aperture_blades', 'aperture_rotation', 'aperture_ratio'):
        yield camera.dof, key, plan[key]


def _verify_activation(scene, camera_object, camera, pose, plan, render_values, exposure, expected_color):
    import numpy as np
    if scene.camera is None or scene.camera.original != camera_object:
        raise ValueError('camera3d Blender changed the active camera during evaluation')
    active = scene.camera
    if active.data.original != camera:
        raise ValueError('camera3d Blender changed the active camera data during evaluation')
    if not np.allclose(active.matrix_world, pose, rtol=1e-6, atol=1e-6):
        raise ValueError('camera3d Blender changed the camera world transform during evaluation')
    for owner, key, value in _camera_settings(active.data, plan):
        _verify_exact(owner, key, value)
    _verify_exact(scene.render, 'engine', 'CYCLES')
    for key, value in render_values.items():
        _verify_exact(scene.render, key, value)
    if expected_color:
        verify_color_state([(owner, field, expected_color[field]) for owner, field, _ in color_state(scene)])
    elif scene.view_settings.exposure != exposure:
        raise ValueError('camera3d Blender changed exposure during evaluation')


def apply_camera(scene, optics, render, *, world_matrix, name='Camera3D',
                 camera_object=None, exposure=0.0, motion_blur=False, shutter_frames=0.5,
                 color_sha256=None):
    """Apply resolved physical state, returning an actual Blender camera object.

    World transforms are canonical Y-up. Blender uses its native -Z-view/+Y-up
    camera local frame; only the world basis is converted. Target tracking and
    animation must be sampled by their owners before this call. A supplied
    camera must be unparented, unanimated and unconstrained to avoid hidden
    Blender evaluation overriding authoritative sampled state. The shared color
    binding can include its validated hash in the same camera activation.
    """
    import bpy
    from mathutils import Matrix
    if not isinstance(name, str) or not 1 <= len(name) <= 128 or any(ord(c) < 32 for c in name):
        raise ValueError('camera3d Blender camera name must be 1..128 printable characters')
    plan = optical_plan(optics, render, exposure=exposure,
                        motion_blur=motion_blur, shutter_frames=shutter_frames)
    matrix = blender_camera_transform(world_matrix)
    if scene.render.engine != 'CYCLES':
        raise ValueError('camera3d physical camera adapter requires Cycles')
    if camera_object is not None:
        if (camera_object.type != 'CAMERA' or camera_object.parent is not None
                or camera_object.constraints or camera_object.animation_data
                or camera_object.data.animation_data or camera_object.library is not None):
            raise ValueError('camera3d requires an unparented, unanimated, unconstrained camera')
        if camera_object.name not in scene.objects:
            raise ValueError('camera3d camera must belong to the supplied scene')
        owner = camera_object.get(CAMERA_COLOR_OWNER)
        if camera_is_shared(scene, camera_object) or (owner is not None and owner != scene):
            raise ValueError('camera3d cannot activate a shared camera object; supply an exclusive camera')
    aspect = render['pixel_aspect']['numerator'] / render['pixel_aspect']['denominator']
    render_values = dict(resolution_x=int(render['width']), resolution_y=int(render['height']),
                         resolution_percentage=100, pixel_aspect_x=max(1.0, aspect),
                         pixel_aspect_y=max(1.0, 1.0 / aspect),
                         use_motion_blur=plan['motion_blur'], motion_blur_shutter=plan['shutter_frames'])
    for key, value in render_values.items():
        _preflight(scene.render, key, value)
    _preflight(scene.view_settings, 'exposure', plan['exposure'])
    pose = Matrix(matrix.tolist())
    previous_render = {key: getattr(scene.render, key) for key in render_values}
    previous_exposure = scene.view_settings.exposure
    previous_active = scene.camera
    previous_data = None if camera_object is None else camera_object.data
    previous_world = None if camera_object is None else camera_object.matrix_world.copy()
    # Restoring a matrix alone can decompose an artist's negative scale and
    # multi-turn Euler into different editable rotation/scale channels.
    previous_channels = {} if camera_object is None else {
        key: tuple(getattr(camera_object, key)) for key in (
            'location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale')}
    previous_scene_hash = scene.get(COLOR_HASH)
    previous_effective_hash = scene.get(EFFECTIVE_COLOR_HASH)
    previous_color = color_state(scene) if previous_scene_hash is not None else []
    color_was_current = bool(previous_color) and (
        effective_color_hash(previous_scene_hash, previous_color) == previous_effective_hash)
    if color_sha256 is not None:
        if not color_was_current or color_sha256 != previous_scene_hash or not math.isclose(
                previous_exposure, plan['exposure'], rel_tol=2e-6,
                abs_tol=min(1e-7, abs(plan['exposure']) * 2e-6)):
            raise ValueError('camera3d shared color binding requires configured scene exposure and hash')
    binding_owners = []
    if previous_active is not None:
        binding_owners.append(previous_active)
    if camera_object is not None and camera_object != previous_active:
        binding_owners.append(camera_object)
    previous_bindings = [(owner, key, owner.get(key)) for owner in binding_owners
                         for key in (COLOR_HASH, CAMERA_COLOR_OWNER)]
    new_object = None
    camera = bpy.data.cameras.new(name)
    try:
        for owner, key, value in _camera_settings(camera, plan):
            _preflight(owner, key, value)
            _set_exact(owner, key, value)
        camera['hatch_camera3d_owned'] = True
        # Finish the range-sensitive writes before swapping the active camera.
        for key, value in render_values.items():
            _set_exact(scene.render, key, value)
        applied_exposure = previous_exposure
        if color_sha256 is None:
            _set_exact(scene.view_settings, 'exposure', plan['exposure'])
            applied_exposure = scene.view_settings.exposure
        if camera_object is None:
            new_object = bpy.data.objects.new(name, camera)
            scene.collection.objects.link(new_object)
            camera_object = new_object
        else:
            camera_object.data = camera
        camera_object.matrix_world = pose
        scene.camera = camera_object
        for layer in scene.view_layers:
            layer.update()
        # A post-update handler can edit the original ID. Drivers can also
        # override only an evaluated copy. Check both before committing proof.
        expected_color = {field: applied_exposure if field == 'exposure' else original
                          for _, field, original in previous_color}
        for observed_scene in (scene, *(layer.depsgraph.scene_eval for layer in scene.view_layers)):
            _verify_activation(observed_scene, camera_object, camera, pose, plan,
                               render_values, applied_exposure, expected_color)
        if (scene.get(COLOR_HASH) != previous_scene_hash
                or scene.get(EFFECTIVE_COLOR_HASH) != previous_effective_hash):
            raise ValueError('camera3d scene color binding changed during evaluation')
        if previous_active is not None and previous_active != camera_object:
            release_camera_color(scene, previous_active)
        if color_sha256 is not None:
            camera_object[CAMERA_COLOR_OWNER] = scene
            camera_object[COLOR_HASH] = color_sha256
        else:
            if not color_was_current or scene.view_settings.exposure != previous_exposure:
                restore_property(scene, COLOR_HASH, None)
                restore_property(scene, EFFECTIVE_COLOR_HASH, None)
            if camera_object != previous_active or camera_object.get(COLOR_HASH) != scene.get(COLOR_HASH):
                release_camera_color(scene, camera_object)
    except Exception:
        if scene.camera != previous_active:
            scene.camera = previous_active
        if new_object is not None:
            bpy.data.objects.remove(new_object, do_unlink=True)
        elif previous_data is not None:
            if camera_object.data != previous_data:
                camera_object.data = previous_data
            if camera_object.matrix_world != previous_world:
                camera_object.matrix_world = previous_world
            for key, original in previous_channels.items():
                if tuple(getattr(camera_object, key)) != original:
                    setattr(camera_object, key, original)
        if scene.render.engine != 'CYCLES':
            scene.render.engine = 'CYCLES'
        for key, value in previous_render.items():
            if getattr(scene.render, key) != value:
                setattr(scene.render, key, value)
        if scene.view_settings.exposure != previous_exposure:
            scene.view_settings.exposure = previous_exposure
        for owner, key, original in previous_bindings:
            restore_property(owner, key, original)
        restore_property(scene, COLOR_HASH, previous_scene_hash)
        restore_property(scene, EFFECTIVE_COLOR_HASH, previous_effective_hash)
        if camera.users == 0:
            bpy.data.cameras.remove(camera)
        restore_color_state(previous_color)
        raise
    # Reclaiming an unused datablock is best-effort after camera activation.
    # The previous datablock must stay available until rollback is unnecessary.
    try:
        if (previous_data is not None and previous_data.get('hatch_camera3d_owned')
                and previous_data.users == 0):
            bpy.data.cameras.remove(previous_data)
    except Exception:
        pass
    return camera_object
