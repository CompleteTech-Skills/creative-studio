"""Blender camera-asset serialization using the canonical physical adapter.

Call inside Blender with one already evaluated camera state. No scene geometry,
animation evaluator, renderer, or source-project save operation is introduced.
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path

from atomic_publish import _rename_no_replace, pinned_directory, private_stage
from camera3d_blender import apply_camera, optical_plan
from camera3d_math import _matrix
from rig_errors import atomic_json, object_hash, sha256


def export_camera_library(destination, optics, render, world_matrix, *,
                          exposure=0.0, motion_blur=False, shutter_frames=0.5):
    """Publish a new appendable .blend containing one resolved physical camera.

    The new library scene is private to this operation. Existing scene objects,
    active cameras and the current .blend filepath are retained. Full scene
    exports and animated camera baking remain separate owner integrations.
    """
    optical_plan(optics, render, exposure=exposure,
                 motion_blur=motion_blur, shutter_frames=shutter_frames)
    world = _matrix(world_matrix, rigid=True)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('camera3d native export destination must be new')
    if not destination.parent.is_dir():
        raise ValueError('camera3d native export parent must exist')
    canonical = dict(schema_version=1, kind='camera3d-export-snapshot',
        optics=deepcopy(optics), render=deepcopy(render), world_matrix=world.tolist(),
        exposure=float(exposure), motion_blur=motion_blur,
        shutter_frames=float(shutter_frames))
    canonical_hash = object_hash(canonical)
    scene = None
    camera = None
    try:
        with pinned_directory(destination.parent) as parent_fd, private_stage(
                parent_fd, prefix='.camera-native-export-') as (wrapper, wrapper_fd):
            import bpy
            stage = wrapper / 'payload'
            stage.mkdir(mode=0o700)
            scene = bpy.data.scenes.new('Camera3DAsset')
            scene.render.engine = 'CYCLES'
            camera = apply_camera(scene, optics, render, world_matrix=world,
                name='Camera3DAsset', exposure=exposure, motion_blur=motion_blur,
                shutter_frames=shutter_frames)
            scene['hatch_camera3d_canonical_sha256'] = canonical_hash
            scene['hatch_camera3d_export_scope'] = 'one evaluated camera; no geometry or animation'
            # Only this private scene and its indirect camera/collection data are
            # written. No source scene, external asset or source-project path enters
            # the library closure. libraries.write does not switch the open project.
            bpy.data.libraries.write(str(stage / 'camera.blend'), {scene},
                                     path_remap='ABSOLUTE', fake_user=True, compress=True)
            atomic_json(stage / 'canonical.json', canonical)
            receipt = dict(schema_version=1, kind='camera3d-native-export-receipt',
                blender_version=bpy.app.version_string, scene=scene.name,
                camera=camera.name, canonical_sha256=canonical_hash,
                scope='one evaluated physical camera; no geometry or animation',
                production_render_settings=False,
                files={name: sha256(stage / name) for name in ('camera.blend', 'canonical.json')})
            atomic_json(stage / 'receipt.json', receipt)
            _rename_no_replace('payload', destination.name,
                               source_dir_fd=wrapper_fd, destination_dir_fd=parent_fd)
            return receipt
    finally:
        if scene is not None:
            # Include partial objects left by an adapter exception, before the
            # return value could be assigned to ``camera``. This scene began
            # empty and contains only IDs created by this operation.
            camera_data = [obj.data for obj in scene.objects
                           if obj.type == 'CAMERA' and obj.data is not None]
            for obj in list(scene.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.scenes.remove(scene)
            for data in camera_data:
                data.use_fake_user = False
                if data.users == 0:
                    bpy.data.cameras.remove(data)
