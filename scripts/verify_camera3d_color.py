"""Actual camera/shared-color acceptance; run with trusted Blender --python.

The original emissive cube is an engineering fixture, not reviewed artwork.
Only the existing shared OCIO authority performs exposure and white balance.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
if os.environ.get('HATCH_CAMERA_TEST_SITE'):
    sys.path.append(os.environ['HATCH_CAMERA_TEST_SITE'])


def foreground_error(image, expected, tolerance):
    """Compare every RGBA channel in the central 8x8 opaque emitter patch."""
    import numpy as np
    patch = image[28:36, 44:52, :]
    if patch.shape != (8, 8, 4):
        raise AssertionError('Missing foreground samples')
    error = float(np.abs(patch - expected).max())
    if not math.isfinite(error) or error > tolerance:
        raise AssertionError(f'Foreground maximum error {error} exceeds {tolerance}')
    return error


def main():
    import bpy
    import numpy as np
    from camera3d_blender import apply_camera
    from camera3d_color import apply_camera_with_color
    from lookdev_color import configuration
    from rig_math import BLENDER_FROM_CANONICAL

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    destination = args.destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Use a new camera color verification destination')
    destination.mkdir(parents=True)
    optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
    optics['depth_of_field']['enabled'] = False
    render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
    render.update(width=96, height=64)
    scene = bpy.context.scene
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 8
    scene.cycles.seed = 42
    scene.cycles.use_denoising = False
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 1
    scene.render.dither_intensity = 0
    location = (BLENDER_FROM_CANONICAL @ np.array([0, 0, -3, 1]))[:3]
    bpy.ops.mesh.primitive_cube_add(size=2, location=location)
    cube = bpy.context.object
    cube.name = 'Original-neutral-emissive-fixture'
    material = bpy.data.materials.new('Original-neutral-emission')
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    emission = nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value = (.3, .3, .3, 1)
    emission.inputs['Strength'].default_value = 1
    output = nodes.new('ShaderNodeOutputMaterial')
    material.node_tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])
    cube.data.materials.append(material)
    material_before = tuple(emission.inputs['Color'].default_value)
    world_before = tuple(scene.world.color)
    world_matrix = np.eye(4)
    camera = None
    receipts = []

    def render_pixels(target, label, linear=False):
        path = destination / (label + ('.exr' if linear else '.png'))
        target.render.filepath = str(path)
        bpy.ops.render.render(scene=target.name, write_still=True)
        image = bpy.data.images.load(str(path), check_existing=False)
        try:
            if tuple(image.size) != (96, 64):
                raise AssertionError('Decoded dimensions do not match the camera gate')
            pixels = np.array(image.pixels[:], dtype=np.float32).reshape(64, 96, 4)
            if not np.isfinite(pixels).all():
                raise AssertionError('Decoded output is malformed')
            return pixels
        finally:
            bpy.data.images.remove(image)

    # Render a separate camera/scene directly through Blender, before the binding
    # can change the fixture. The unfiltered emissive foreground is 0.3 linear.
    reference = bpy.data.scenes.new('Direct Blender OCIO reference')
    reference.collection.objects.link(cube)
    reference.world = scene.world
    reference.render.engine = 'CYCLES'
    reference.cycles.device = 'CPU'
    reference.cycles.samples = 8
    reference.cycles.seed = 42
    reference.cycles.use_denoising = False
    reference.render.threads_mode = 'FIXED'
    reference.render.threads = 1
    reference.render.resolution_x = 96
    reference.render.resolution_y = 64
    reference.render.resolution_percentage = 100
    reference.render.dither_intensity = 0
    direct_camera = bpy.data.objects.new('Reference camera', bpy.data.cameras.new('Reference optics'))
    direct_camera.rotation_euler.x = math.pi / 2
    direct_camera.data.lens = 50
    direct_camera.data.sensor_fit = 'HORIZONTAL'
    direct_camera.data.sensor_width = 36
    reference.collection.objects.link(direct_camera)
    reference.camera = direct_camera
    reference.display_settings.display_device = 'sRGB'
    view = reference.view_settings
    view.view_transform = 'Standard'
    view.look = 'None'
    view.gamma = 1
    view.use_curve_mapping = False
    view.use_white_balance = True
    if hasattr(view, 'use_hdr_view'):
        view.use_hdr_view = False
    image = reference.render.image_settings
    image.color_management = 'FOLLOW_SCENE'
    image.file_format = 'PNG'
    image.color_depth = '16'
    image.color_mode = 'RGBA'
    profiles = [('neutral', 6500, 10, 0), ('temperature-3200', 3200, 10, 0),
                ('tint-100', 6500, 100, 0), ('one-stop', 6500, 10, 1)]
    references = {}
    for label, temperature, tint, exposure in profiles:
        view.white_balance_temperature = temperature
        view.white_balance_tint = tint
        view.exposure = exposure
        references[label] = render_pixels(reference, 'reference-' + label)

    def capture(label, temperature, tint, exposure=0, *, linear=False):
        nonlocal camera
        color = configuration(view='Standard', exposure=exposure, temperature_kelvin=temperature,
                              tint=tint, output='scene-linear-exr' if linear else 'srgb-png')
        settings = dict(render, output='exr', color_space='linear-srgb') if linear else render
        camera, receipt = apply_camera_with_color(scene, optics, settings,
            world_matrix=world_matrix, color_configuration=color, camera_object=camera)
        pixels = render_pixels(scene, label, linear)
        if tuple(emission.inputs['Color'].default_value) != material_before or tuple(scene.world.color) != world_before:
            raise AssertionError('Color binding mutated scene illumination or source material')
        if camera['hatch_color_sha256'] != color['sha256'] or scene['hatch_color_sha256'] != color['sha256']:
            raise AssertionError('Camera and scene color bindings disagree')
        receipts.append(dict(label=label, configuration=color, native_receipt=receipt))
        return pixels

    # An existing JPEG scene cannot represent RGBA until its format changes.
    scene.render.image_settings.file_format = 'JPEG'
    scene.render.image_settings.color_mode = 'RGB'
    results = {label: capture(label, temp, tint, stops) for label, temp, tint, stops in profiles}
    if scene.render.image_settings.file_format != 'PNG' or scene.render.image_settings.color_mode != 'RGBA':
        raise AssertionError('JPEG to shared PNG color configuration failed')
    reference_errors = {label: foreground_error(results[label], pixels[28:36, 44:52, :], 2e-4)
                        for label, pixels in references.items()}
    neutral = results['neutral'][..., :3]
    temperature_difference = float(np.abs(neutral - results['temperature-3200'][..., :3]).mean())
    tint_difference = float(np.abs(neutral - results['tint-100'][..., :3]).mean())
    if temperature_difference <= .01 or tint_difference <= .01:
        raise AssertionError(f'Actual white balance did not change PNG: {temperature_difference}, {tint_difference}')
    scene.render.image_settings.file_format = 'JPEG'
    scene.render.image_settings.color_mode = 'RGB'
    exposure_difference = float((results['one-stop'][..., :3] - neutral).mean())
    if exposure_difference <= .01:
        raise AssertionError('One stop did not brighten the decoded PNG')
    linear_neutral = capture('linear-neutral', 6500, 10, linear=True)
    if scene.render.image_settings.file_format != 'OPEN_EXR' or scene.render.image_settings.color_mode != 'RGBA':
        raise AssertionError('JPEG to shared linear EXR color configuration failed')
    linear_changed = capture('linear-changed-view', 3200, 100, 1, linear=True)
    linear_errors = [foreground_error(pixels, (.3, .3, .3, 1.), 1e-5)
                     for pixels in (linear_neutral, linear_changed)]
    linear_difference = float(np.abs(linear_neutral - linear_changed).max())
    if linear_difference != 0:
        raise AssertionError(f'View white balance changed scene-linear EXR: {linear_difference}')
    blend = destination / 'camera-color.blend'
    bpy.context.window.scene = scene
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    reopened = bpy.context.scene
    if not reopened.view_settings.use_white_balance or reopened.view_settings.white_balance_temperature != 3200 or reopened.view_settings.white_balance_tint != 100:
        raise AssertionError('Shared color state did not survive native save/reopen')
    if reopened.view_settings.exposure != 1:
        raise AssertionError('Shared exposure did not survive native save/reopen')
    if reopened.camera['hatch_color_sha256'] != receipts[-1]['configuration']['sha256'] or reopened['hatch_color_sha256'] != receipts[-1]['configuration']['sha256']:
        raise AssertionError('Hash binding did not survive native save/reopen')
    former = reopened.camera
    linear_render = dict(render, output='exr', color_space='linear-srgb')
    apply_camera(reopened, optics, linear_render, world_matrix=world_matrix, camera_object=former, exposure=1)
    if former.get('hatch_color_sha256') != receipts[-1]['configuration']['sha256']:
        raise AssertionError('An unchanged effective exposure lost its camera binding')
    standalone = apply_camera(reopened, optics, linear_render, world_matrix=world_matrix, exposure=0)
    if any('hatch_color_sha256' in owner for owner in (former, standalone, reopened)):
        raise AssertionError('Standalone exposure change retained a stale color binding')
    (destination / 'resolved-inputs.json').write_text(json.dumps(dict(optics=optics, render=render, color_receipts=receipts), indent=2) + '\n')
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir() if p.is_file()}
    report = dict(ok=True, fixture=True, production_visual_approval=False,
        blender_version=bpy.app.version_string, saved_and_reopened=True, standalone_exposure_checked=True,
        png_white_balance_mean_difference=temperature_difference,
        png_one_stop_mean_increase=exposure_difference, png_direct_foreground_errors=reference_errors,
        exr_expected_foreground_rgba=[.3, .3, .3, 1.], exr_foreground_errors=linear_errors,
        png_tint_mean_difference=tint_difference, linear_exr_maximum_difference=linear_difference,
        source_material_and_lighting_unchanged=True, file_sha256=files,
        source_sha256={name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
            for name in ('camera3d_color.py', 'camera3d_blender.py', 'camera3d_math.py', 'lookdev_color.py', 'verify_camera3d_color.py')})
    (destination / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
