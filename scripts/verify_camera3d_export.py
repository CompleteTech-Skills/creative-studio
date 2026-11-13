"""Prepare Khronos-validated cameras, then verify real Blender glTF imports.

Host: python scripts/verify_camera3d_export.py --prepare --destination NEW_DIR
Blender: --python this_file -- --verify --destination PREPARED_DIR
"""
from __future__ import annotations
import argparse
from copy import deepcopy
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


def prepare(destination):
    import numpy as np
    from camera3d_export import export_gltf
    from camera3d_math import world_transform
    if destination.exists() or destination.is_symlink():
        raise ValueError('Use a new camera interchange fixture directory')
    destination.mkdir()
    optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
    render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
    render.update(width=160, height=120, max_pixels=1000000, overscan=1.2,
                  pixel_aspect=dict(numerator=2, denominator=1))
    world = world_transform(dict(translation=[2, 1, -3], scale=[1, 1, 1],
        rotation=[0, math.sin(.3), 0, math.cos(.3)]))
    optics['projection']['sensor_fit'] = 'vertical'
    export_gltf(destination / 'perspective', optics, render, world)
    ortho = deepcopy(optics)
    ortho['projection'] = dict(type='orthographic', orthographic_height_m=.1,
                              near_clip_m=.1, far_clip_m=100, shift_x=0, shift_y=0)
    export_gltf(destination / 'orthographic', ortho, render, world)
    shifted = deepcopy(optics)
    shifted['projection']['shift_x'] = .2
    shifted['depth_of_field']['enabled'] = True
    export_gltf(destination / 'explicit-loss', shifted, render, np.eye(4), allow_loss=True)


def verify(destination):
    import bpy
    import numpy as np
    from camera3d_math import projection_matrix
    from rig_math import BLENDER_FROM_CANONICAL
    cases = []
    for label in ('perspective', 'orthographic'):
        directory = destination / label
        receipt = json.loads((directory / 'receipt.json').read_text())
        if not receipt['independently_validated']:
            raise AssertionError('Fixture lacks independent Khronos acceptance')
        for name, digest in receipt['files'].items():
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
                raise AssertionError('Fixture file changed after Khronos acceptance')
        canonical = json.loads((directory / 'canonical.json').read_text())
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(directory / 'camera.gltf'))
        cameras = [obj for obj in bpy.context.scene.objects if obj.type == 'CAMERA']
        if len(cameras) != 1:
            raise AssertionError('Independent glTF import lost or duplicated the camera')
        scene = bpy.context.scene
        camera = cameras[0]
        render = canonical['render']
        scene.render.resolution_x = render['width']
        scene.render.resolution_y = render['height']
        scene.render.resolution_percentage = 100
        aspect = render['pixel_aspect']['numerator'] / render['pixel_aspect']['denominator']
        scene.render.pixel_aspect_x = max(1, aspect)
        scene.render.pixel_aspect_y = max(1, 1 / aspect)
        bpy.context.view_layer.update()
        world_error = float(np.max(np.abs(
            BLENDER_FROM_CANONICAL.T @ np.asarray(camera.matrix_world)
            - np.array(canonical['world_matrix']))))
        observed = np.array(camera.calc_matrix_camera(bpy.context.evaluated_depsgraph_get(),
            x=render['width'], y=render['height'],
            scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y))
        error = float(np.max(np.abs(observed - projection_matrix(canonical['optics'], render))))
        if error >= 2e-5 or world_error >= 2e-5:
            raise AssertionError(f'Actual glTF import mismatch: {label}, projection={error}, world={world_error}')
        cases.append(dict(camera=label, projection_error=error, world_error=world_error,
                          gltf_sha256=receipt['files']['camera.gltf']))
    report = dict(ok=True, fixture=True, scope='static camera core glTF projection and pose',
                  blender_version=bpy.app.version_string, cases=cases,
                  physical_effect_interoperability=False,
                  source_sha256={name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
                      for name in ('camera3d_export.py', 'verify_camera3d_export.py', 'camera3d_math.py')})
    (destination / 'blender-import-acceptance.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--verify', action='store_true')
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:])
    (prepare if args.prepare else verify)(args.destination.absolute())


if __name__ == '__main__':
    main()
