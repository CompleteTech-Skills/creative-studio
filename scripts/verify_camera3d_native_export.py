"""Actual Blender library save/reopen acceptance for evaluated camera assets."""
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


def main():
    import bpy
    import numpy as np
    import camera3d_native_export
    from camera3d_native_export import export_camera_library
    from unittest.mock import patch
    from camera3d_math import projection_matrix, world_transform
    from rig_math import BLENDER_FROM_CANONICAL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    destination = args.destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Use a new native camera acceptance destination')
    destination.mkdir()
    optics = json.loads((ROOT / 'examples/camera3d/optics.json').read_text())
    render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
    render.update(width=160, height=120, max_pixels=1000000, overscan=1.2,
                  pixel_aspect=dict(numerator=2, denominator=1))
    optics['projection'].update(sensor_fit='vertical', shift_x=.13, shift_y=-.07)
    optics['depth_of_field'].update(enabled=True, focus_distance_m=3, f_stop=1.4)
    world = world_transform(dict(translation=[2, 1, -3], scale=[1, 1, 1],
        rotation=[0, math.sin(.3), 0, math.cos(.3)]))
    def source_state():
        return dict(filepath=bpy.data.filepath, active_scene=bpy.context.scene.name,
            active_camera=bpy.context.scene.camera.name if bpy.context.scene.camera else None,
            scenes=sorted(bpy.data.scenes.keys()), objects=sorted(bpy.data.objects.keys()),
            cameras=sorted(bpy.data.cameras.keys()))
    before = source_state()
    rejected = deepcopy(optics)
    rejected['projection']['focal_length_mm'] = .5
    try:
        export_camera_library(destination / 'unsupported', rejected, render, world)
    except ValueError:
        pass
    else:
        raise AssertionError('Unsupported native focal length was silently clamped')
    if (destination / 'unsupported').exists() or source_state() != before:
        raise AssertionError('Failed native export leaked output or changed the source project')
    exports = []
    replacements = []
    for label in ('perspective', 'orthographic'):
        value = deepcopy(optics)
        if label == 'orthographic':
            value['projection'] = dict(type=label, orthographic_height_m=.1,
                near_clip_m=.1, far_clip_m=100, shift_x=.13, shift_y=-.07)
            value['depth_of_field']['enabled'] = False
        options = dict(exposure=1 if label == 'perspective' else -1,
                       motion_blur=label == 'perspective', shutter_frames=.25)
        write_json = camera3d_native_export.atomic_json
        def write_and_replace(path, document):
            write_json(path, document)
            if path.name == 'receipt.json':
                # Exercise the original publication gap with an actual library:
                # replace its public outer wrapper after all bytes are hashed.
                wrapper = next(p for p in destination.glob('.camera-native-export-*')
                               if p.is_dir() and not (p / 'foreign.txt').exists())
                wrapper.rename(destination / ('moved-wrapper-' + label))
                wrapper.mkdir()
                (wrapper / 'foreign.txt').write_bytes(b'preserve foreign replacement')
                replacements.append(wrapper)
        with patch.object(camera3d_native_export, 'atomic_json', side_effect=write_and_replace):
            receipt = export_camera_library(destination / label, value, render, world, **options)
        if (destination / label / 'foreign.txt').exists():
            raise AssertionError('Native export published an unverified stage replacement')
        if any((path / 'foreign.txt').read_bytes() != b'preserve foreign replacement'
               for path in replacements):
            raise AssertionError('Native export cleanup touched a foreign replacement')
        if source_state() != before:
            raise AssertionError('Native camera export changed existing project IDs or active state')
        exports.append((label, receipt))
    cases = []
    for label, receipt in exports:
        root = destination / label
        for name, digest in receipt['files'].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise AssertionError('Native exported bytes changed before independent reopen')
        canonical = json.loads((root / 'canonical.json').read_text())
        bpy.ops.wm.open_mainfile(filepath=str(root / 'camera.blend'), load_ui=False, use_scripts=False)
        scene = bpy.data.scenes[receipt['scene']]
        camera = scene.camera
        if camera is None or len(scene.objects) != 1 or len(bpy.data.scenes) != 1:
            raise AssertionError('Native library did not retain its exact camera-only closure')
        if scene.get('hatch_camera3d_canonical_sha256') != receipt['canonical_sha256']:
            raise AssertionError('Native library lost its canonical state identity')
        p = canonical['optics']['depth_of_field']
        if (camera.data.dof.use_dof != p['enabled']
                or not math.isclose(camera.data.dof.focus_distance, p['focus_distance_m'], rel_tol=2e-6)
                or not math.isclose(camera.data.dof.aperture_fstop, p['f_stop'], rel_tol=2e-6)
                or camera.data.dof.aperture_blades != p['aperture_blades']):
            raise AssertionError('Native DOF state changed during library write/reopen')
        if (scene.view_settings.exposure != canonical['exposure']
                or scene.render.use_motion_blur != canonical['motion_blur']
                or scene.render.motion_blur_shutter != canonical['shutter_frames']):
            raise AssertionError('Native exposure/shutter state changed during reopen')
        bpy.context.view_layer.update()
        matrix = np.asarray(camera.matrix_world)
        world_error = float(np.max(np.abs(BLENDER_FROM_CANONICAL.T @ matrix - world)))
        observed = np.array(camera.calc_matrix_camera(bpy.context.evaluated_depsgraph_get(),
            x=render['width'], y=render['height'], scale_x=scene.render.pixel_aspect_x,
            scale_y=scene.render.pixel_aspect_y))
        error = float(np.max(np.abs(observed - projection_matrix(canonical['optics'], render))))
        if error >= 2e-5 or world_error >= 2e-5:
            raise AssertionError(f'Native camera state mismatch: projection={error}, world={world_error}')
        cases.append(dict(camera=label, projection_error=error, world_error=world_error,
                          blend_sha256=receipt['files']['camera.blend']))
    report = dict(ok=True, fixture=True, blender_version=bpy.app.version_string,
        scope='two static evaluated physical camera assets; no scene/animation export',
        source_project_unchanged=True, rejected_unsupported_without_leak=True, cases=cases,
        stage_replacement_preserved=True,
        source_sha256={name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
            for name in ('camera3d_native_export.py', 'camera3d_blender.py', 'camera3d_math.py',
                         'verify_camera3d_native_export.py')})
    (destination / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
