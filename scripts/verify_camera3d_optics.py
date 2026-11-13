"""Actual Blender optical acceptance fixture. Run via trusted --python only.

Creates original procedural geometry, checks numerical projection parity, saves
and reopens the project, and compares physically rendered focused/defocused
frames. It does not award visual approval or render a production sequence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
if os.environ.get('HATCH_CAMERA_TEST_SITE'):
    # Test harness supplies its installed pure-Python schema validator. Blender's
    # own numerical packages retain precedence over this appended site directory.
    sys.path.append(os.environ['HATCH_CAMERA_TEST_SITE'])


def main():
    import bpy
    import numpy as np
    from mathutils import Vector
    from camera3d_blender import apply_camera
    from camera3d_math import projection_matrix, world_transform
    from rig_math import BLENDER_FROM_CANONICAL

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    destination = args.destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Use a new optical-verification destination')
    destination.mkdir(parents=True)
    optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
    render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
    render.update(width=192, height=128)
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 16
    scene.cycles.seed = 42
    scene.cycles.use_denoising = False
    scene.render.threads_mode = 'FIXED'; scene.render.threads = 2
    maximum = 0.0
    cases = []
    transformed_world = world_transform(dict(translation=[1, 2, 3],
        rotation=[0, .3826834323650898, 0, .9238795325112867], scale=[1, 1, 1]))
    before_camera_count = len(bpy.data.cameras)
    unsupported = deepcopy(optics)
    unsupported['projection']['focal_length_mm'] = 0.5
    try:
        apply_camera(scene, unsupported, render, world_matrix=transformed_world)
    except ValueError:
        pass
    else:
        raise AssertionError('Blender silently admitted an unrepresentable lens')
    if len(bpy.data.cameras) != before_camera_count:
        raise AssertionError('Rejected optics leaked a camera datablock')
    for projection_type in ('perspective', 'orthographic'):
        for fit in ('horizontal', 'vertical', 'auto'):
            for gate_fit in ('crop', 'expand'):
                value = deepcopy(optics)
                if projection_type == 'perspective':
                    value['projection'].update(sensor_fit=fit, shift_x=.13, shift_y=-.07)
                else:
                    value['projection'] = dict(type='orthographic', orthographic_height_m=3,
                                               shift_x=.13, shift_y=-.07, near_clip_m=.1, far_clip_m=100)
                settings = dict(render, gate_fit=gate_fit, overscan=1.2,
                                pixel_aspect=dict(numerator=2, denominator=1))
                camera = apply_camera(scene, value, settings, world_matrix=transformed_world)
                # Blender stores object matrices as float32; inspect the actual
                # returned values without pretending they meet the float64
                # authoring helper's stricter orthonormality tolerance.
                observed_world = BLENDER_FROM_CANONICAL.T @ np.asarray(camera.matrix_world)
                if not np.allclose(observed_world, transformed_world, rtol=1e-6, atol=1e-6):
                    raise AssertionError('Blender camera world transform disagrees with canonical transform')
                observed = np.array(camera.calc_matrix_camera(bpy.context.evaluated_depsgraph_get(),
                    x=settings['width'], y=settings['height'],
                    scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y))
                error = float(np.abs(observed - projection_matrix(value, settings)).max())
                maximum = max(maximum, error)
                cases.append(dict(type=projection_type, fit=fit, gate_fit=gate_fit, error=error))
                if error >= 2e-5:
                    raise AssertionError(f'projection mismatch: {cases[-1]}')
                data = camera.data
                bpy.data.objects.remove(camera, do_unlink=True)
                bpy.data.cameras.remove(data)

    # Orthographic world coverage must not be constrained by perspective
    # sensor-width limits (which Blender measures in millimeters).
    for height in (.1, 10000):
        value = deepcopy(optics)
        value['projection'] = dict(type='orthographic', orthographic_height_m=height,
                                  shift_x=0, shift_y=0, near_clip_m=.1, far_clip_m=100)
        camera = apply_camera(scene, value, render, world_matrix=transformed_world)
        observed = np.array(camera.calc_matrix_camera(bpy.context.evaluated_depsgraph_get(),
            x=render['width'], y=render['height'],
            scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y))
        error = float(np.abs(observed - projection_matrix(value, render)).max())
        maximum = max(maximum, error)
        cases.append(dict(type='orthographic', height=height, error=error))
        if error >= 2e-5:
            raise AssertionError(f'orthographic scale mismatch: {cases[-1]}')
        data = camera.data
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(data)

    # Independent depth-separated real meshes in camera-local coordinates.
    bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
    world = np.eye(4)
    # Fill the frame at two depths so physical defocus is measurable over real
    # image area, rather than diluted by a nearly empty background.
    for index, (x, z, size) in enumerate(((-.38, -3, .5), (.85, -7, 1.5))):
        location = (BLENDER_FROM_CANONICAL @ np.array([x, 0, z, 1]))[:3]
        bpy.ops.mesh.primitive_cube_add(size=size, location=location)
        cube = bpy.context.object
        cube.name = f'depth-cube-{index}'
        material = bpy.data.materials.new(f'checker-{index}')
        material.use_nodes = True
        nodes = material.node_tree.nodes; links = material.node_tree.links
        checker = nodes.new('ShaderNodeTexChecker'); checker.inputs['Scale'].default_value = 8
        shader = nodes.get('Principled BSDF')
        links.new(checker.outputs['Color'], shader.inputs['Base Color'])
        shader.inputs['Roughness'].default_value = .7
        cube.data.materials.append(material)
    scene.world.color = (.8, .8, .8)
    bpy.ops.object.light_add(type='AREA', location=(0, 0, 0))
    light = bpy.context.object; light.data.energy = 300; light.data.shape = 'DISK'; light.data.size = 5
    # Blender camera/light view local -Z; orient light toward canonical -Z.
    light.rotation_euler = (1.5707963267948966, 0, 0)
    optics['projection']['focal_length_mm'] = 85
    optics['depth_of_field'].update(enabled=True, focus_distance_m=3, f_stop=.6)
    camera = apply_camera(scene, optics, render, world_matrix=world)
    scene.view_settings.view_transform = 'Standard'
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.film_transparent = False
    rendered = []
    def render_pixels(label):
        scene.render.filepath = str(destination / f'{label}.png')
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(scene.render.filepath, check_existing=False)
        pixels = np.array(image.pixels[:], dtype=np.float32)
        bpy.data.images.remove(image)
        return pixels
    for label, enabled in [('pinhole', False), ('physical-dof', True)]:
        optics['depth_of_field']['enabled'] = enabled
        camera = apply_camera(scene, optics, render, world_matrix=world, camera_object=camera)
        rendered.append(render_pixels(label))
    difference = float(np.abs(rendered[0] - rendered[1]).mean())
    if difference <= .01:
        raise AssertionError(f'physical DOF did not visibly change actual render: {difference}')
    optics['depth_of_field']['enabled'] = False
    camera = apply_camera(scene, optics, render, world_matrix=world, camera_object=camera, exposure=1)
    exposure_pixels = render_pixels('exposure-one-stop')
    exposure_difference = float(np.abs(rendered[0] - exposure_pixels).mean())
    if exposure_difference <= .01:
        raise AssertionError('exposure did not change the actual render')
    camera = apply_camera(scene, optics, render, world_matrix=world, camera_object=camera)
    moving = bpy.data.objects['depth-cube-0']
    moving.location.x = -.6; moving.keyframe_insert('location', frame=0)
    moving.location.x = -.16; moving.keyframe_insert('location', frame=2)
    scene.frame_set(1)
    sharp_motion = render_pixels('moving-sharp')
    camera = apply_camera(scene, optics, render, world_matrix=world, camera_object=camera,
                          motion_blur=True, shutter_frames=1)
    blurred_motion = render_pixels('moving-blur')
    motion_difference = float(np.abs(sharp_motion - blurred_motion).mean())
    if motion_difference <= .001:
        raise AssertionError('physical motion blur did not change the actual render')
    optics['depth_of_field']['enabled'] = True
    camera = apply_camera(scene, optics, render, world_matrix=world, camera_object=camera,
                          motion_blur=True, shutter_frames=1)
    blend = destination / 'optics.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    reopened = bpy.context.scene.camera
    if not reopened.data.dof.use_dof or abs(reopened.data.dof.focus_distance - 3) > 1e-6:
        raise AssertionError('saved optical state did not survive independent Blender reopen')
    if not bpy.context.scene.render.use_motion_blur or bpy.context.scene.view_settings.exposure != 0:
        raise AssertionError('saved shutter/exposure state did not survive independent Blender reopen')
    (destination / 'resolved-inputs.json').write_text(json.dumps(dict(optics=optics, render=render), indent=2) + '\n')
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir() if p.is_file()}
    report = dict(ok=True, fixture=True, production_visual_approval=False,
                  blender_version=bpy.app.version_string, maximum_projection_error=maximum,
                  projection_cases=cases, dof_pixel_difference=difference,
                  exposure_pixel_difference=exposure_difference,
                  motion_blur_pixel_difference=motion_difference,
                  saved_and_reopened=True, file_sha256=files, samples=16, threads=2,
                  source_sha256={name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
                                 for name in ('camera3d_math.py', 'camera3d_blender.py', 'verify_camera3d_optics.py')},
                  python_version=sys.version, numpy_version=np.__version__,
                  jsonschema_version=importlib.metadata.version('jsonschema'))
    (destination / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
