"""Actual camera/shared-color acceptance; run with trusted Blender --python.

The original emissive cube is an engineering fixture, not reviewed artwork.
Only the existing shared OCIO authority performs exposure and white balance.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
if os.environ.get('HATCH_CAMERA_TEST_SITE'):
    sys.path.append(os.environ['HATCH_CAMERA_TEST_SITE'])


def main():
    import bpy
    import numpy as np
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

    def capture(label, temperature, tint, linear=False):
        nonlocal camera
        color = configuration(view='Standard', temperature_kelvin=temperature,
                              tint=tint, output='scene-linear-exr' if linear else 'srgb-png')
        settings = dict(render, output='exr', color_space='linear-srgb') if linear else render
        camera, receipt = apply_camera_with_color(scene, optics, settings,
            world_matrix=world_matrix, color_configuration=color, camera_object=camera)
        scene.render.filepath = str(destination / (label + ('.exr' if linear else '.png')))
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(scene.render.filepath, check_existing=False)
        pixels = np.array(image.pixels[:], dtype=np.float32).reshape(-1, 4)
        bpy.data.images.remove(image)
        if not np.isfinite(pixels).all() or len(pixels) != 96 * 64:
            raise AssertionError('Decoded output is malformed')
        if tuple(emission.inputs['Color'].default_value) != material_before or tuple(scene.world.color) != world_before:
            raise AssertionError('Color binding mutated scene illumination or source material')
        if camera['hatch_color_sha256'] != color['sha256'] or scene['hatch_color_sha256'] != color['sha256']:
            raise AssertionError('Camera and scene color bindings disagree')
        receipts.append(dict(label=label, configuration=color, native_receipt=receipt))
        return pixels

    # An existing JPEG scene cannot represent RGBA until its format changes.
    scene.render.image_settings.file_format = 'JPEG'
    scene.render.image_settings.color_mode = 'RGB'
    neutral = capture('neutral', 6500, 10)
    if scene.render.image_settings.file_format != 'PNG' or scene.render.image_settings.color_mode != 'RGBA':
        raise AssertionError('JPEG to shared PNG color configuration failed')
    warm = capture('temperature-3200', 3200, 10)
    tint = capture('tint-100', 6500, 100)
    temperature_difference = float(np.abs(neutral[:, :3] - warm[:, :3]).mean())
    tint_difference = float(np.abs(neutral[:, :3] - tint[:, :3]).mean())
    if temperature_difference <= .01 or tint_difference <= .01:
        raise AssertionError(f'Actual white balance did not change PNG: {temperature_difference}, {tint_difference}')
    scene.render.image_settings.file_format = 'JPEG'
    scene.render.image_settings.color_mode = 'RGB'
    linear_neutral = capture('linear-neutral', 6500, 10, True)
    if scene.render.image_settings.file_format != 'OPEN_EXR' or scene.render.image_settings.color_mode != 'RGBA':
        raise AssertionError('JPEG to shared linear EXR color configuration failed')
    linear_changed = capture('linear-changed-view', 3200, 100, True)
    linear_difference = float(np.abs(linear_neutral - linear_changed).max())
    if linear_difference != 0:
        raise AssertionError(f'View white balance changed scene-linear EXR: {linear_difference}')
    blend = destination / 'camera-color.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    reopened = bpy.context.scene
    if not reopened.view_settings.use_white_balance or reopened.view_settings.white_balance_temperature != 3200 or reopened.view_settings.white_balance_tint != 100:
        raise AssertionError('Shared color state did not survive native save/reopen')
    if reopened.camera['hatch_color_sha256'] != receipts[-1]['configuration']['sha256'] or reopened['hatch_color_sha256'] != receipts[-1]['configuration']['sha256']:
        raise AssertionError('Hash binding did not survive native save/reopen')
    (destination / 'resolved-inputs.json').write_text(json.dumps(dict(optics=optics, render=render, color_receipts=receipts), indent=2) + '\n')
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir() if p.is_file()}
    report = dict(ok=True, fixture=True, production_visual_approval=False,
        blender_version=bpy.app.version_string, saved_and_reopened=True,
        png_white_balance_mean_difference=temperature_difference,
        png_tint_mean_difference=tint_difference, linear_exr_maximum_difference=linear_difference,
        source_material_and_lighting_unchanged=True, file_sha256=files,
        source_sha256={name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
            for name in ('camera3d_color.py', 'camera3d_blender.py', 'camera3d_math.py', 'lookdev_color.py', 'verify_camera3d_color.py')})
    (destination / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
