"""Resolved Camera3D optics applied to an existing Blender scene.

This is an in-process adapter, not a scene loader, renderer, or job scheduler.
Callers own immutable source resolution and render publication. Importing this
module never imports bpy; apply_camera requires actual Blender execution.
"""
from __future__ import annotations

import math

from camera3d_contracts import validate
from camera3d_math import optical_gate, world_transform, blender_camera_transform, _matrix


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
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
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
                lens=p.get('focal_length_mm'), sensor_width=width * render['overscan'],
                ortho_scale=width * render['overscan'],
                shift_x=-p['shift_x'] / render['overscan'],
                shift_y=-p['shift_y'] / (render['overscan'] * aspect),
                clip_start=p['near_clip_m'], clip_end=p['far_clip_m'],
                use_dof=dof['enabled'], focus_distance=dof['focus_distance_m'],
                aperture_fstop=dof['f_stop'], aperture_blades=dof['aperture_blades'],
                aperture_rotation=dof['aperture_rotation_rad'], aperture_ratio=dof['anamorphic_ratio'],
                exposure=float(exposure), motion_blur=motion_blur, shutter_frames=float(shutter_frames))


def _set_exact(owner, attribute, value):
    """Reject Blender's silent clamping instead of claiming unsupported optics."""
    setattr(owner, attribute, value)
    observed = getattr(owner, attribute)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isclose(float(observed), float(value), rel_tol=2e-6,
                            abs_tol=min(1e-7, abs(float(value)) * 2e-6)):
            raise ValueError(f'camera3d Blender cannot represent {attribute}={value}')
    elif observed != value:
        raise ValueError(f'camera3d Blender cannot represent {attribute}={value}')


def _preflight(owner, attribute, value):
    property_ = owner.bl_rna.properties[attribute]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not property_.hard_min <= value <= property_.hard_max:
            raise ValueError(f'camera3d Blender cannot represent {attribute}={value}')


def _configure_camera(camera, plan):
    _set_exact(camera, 'type', plan['type'])
    _set_exact(camera, 'sensor_fit', 'HORIZONTAL')
    for key in ('sensor_width', 'shift_x', 'shift_y', 'clip_start', 'clip_end'):
        _set_exact(camera, key, plan[key])
    _set_exact(camera, 'lens' if plan['type'] == 'PERSP' else 'ortho_scale',
               plan['lens'] if plan['type'] == 'PERSP' else plan['ortho_scale'])
    camera.dof.focus_object = None
    for key in ('use_dof', 'focus_distance', 'aperture_fstop', 'aperture_blades', 'aperture_rotation', 'aperture_ratio'):
        _set_exact(camera.dof, key, plan[key])
    camera['hatch_camera3d_owned'] = True


def apply_camera(scene, optics, render, *, world_matrix, name='Camera3D',
                 camera_object=None, exposure=0.0, motion_blur=False, shutter_frames=0.5):
    """Apply resolved physical state, returning an actual Blender camera object.

    World transforms are canonical Y-up. Blender uses its native -Z-view/+Y-up
    camera local frame; only the world basis is converted. Target tracking and
    animation must be sampled by their owners before this call. A supplied
    camera must be unparented, unanimated and unconstrained to avoid hidden
    Blender evaluation overriding authoritative sampled state.
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
    aspect = render['pixel_aspect']['numerator'] / render['pixel_aspect']['denominator']
    render_values = dict(resolution_x=int(render['width']), resolution_y=int(render['height']),
                         resolution_percentage=100, pixel_aspect_x=max(1.0, aspect),
                         pixel_aspect_y=max(1.0, 1.0 / aspect),
                         use_motion_blur=plan['motion_blur'], motion_blur_shutter=plan['shutter_frames'])
    for key, value in render_values.items():
        _preflight(scene.render, key, value)
    _preflight(scene.view_settings, 'exposure', plan['exposure'])
    camera = bpy.data.cameras.new(name)
    try:
        for key in ('sensor_width', 'shift_x', 'shift_y', 'clip_start', 'clip_end'):
            _preflight(camera, key, plan[key])
        _preflight(camera, 'lens' if plan['type'] == 'PERSP' else 'ortho_scale',
                   plan['lens'] if plan['type'] == 'PERSP' else plan['ortho_scale'])
        for key in ('use_dof', 'focus_distance', 'aperture_fstop', 'aperture_blades', 'aperture_rotation', 'aperture_ratio'):
            _preflight(camera.dof, key, plan[key])
        _configure_camera(camera, plan)
    except Exception:
        bpy.data.cameras.remove(camera)
        raise
    if camera_object is None:
        camera_object = bpy.data.objects.new(name, camera)
        scene.collection.objects.link(camera_object)
    else:
        previous = camera_object.data
        camera_object.data = camera
        if previous.get('hatch_camera3d_owned') and previous.users == 0:
            bpy.data.cameras.remove(previous)
    camera_object.matrix_world = Matrix(matrix.tolist())
    for key, value in render_values.items():
        _set_exact(scene.render, key, value)
    _set_exact(scene.view_settings, 'exposure', plan['exposure'])
    scene.camera = camera_object
    bpy.context.view_layer.update()
    return camera_object
