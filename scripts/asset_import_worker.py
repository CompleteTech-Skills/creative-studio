"""Application-owned Blender worker. Input is JSON, never user Python."""
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def fail(message):
    raise RuntimeError(message)


def inspect_scene():
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    return dict(objects=[dict(name=o.name, type=o.type, parent=o.parent.name if o.parent else None,
                             vertices=len(o.data.vertices) if o.type == 'MESH' else 0,
                             triangles=sum(len(p.vertices)-2 for p in o.data.polygons) if o.type == 'MESH' else 0)
                         for o in bpy.context.scene.objects],
                mesh_objects=len(meshes), vertices=sum(len(o.data.vertices) for o in meshes),
                armatures=sum(o.type == 'ARMATURE' for o in bpy.context.scene.objects),
                shape_keys=sum(max(0, len(o.data.shape_keys.key_blocks)-1) if o.data.shape_keys else 0 for o in meshes),
                materials=len(bpy.data.materials), actions=[a.name for a in bpy.data.actions])


def ensure_packed():
    if bpy.data.libraries or bpy.data.texts or any(o.animation_data and o.animation_data.drivers for o in bpy.data.objects):
        fail('Trusted blend profile forbids linked libraries, text blocks and expression drivers.')
    for image in bpy.data.images:
        if image.type not in ('RENDER_RESULT', 'COMPOSITING') and image.source == 'FILE' and not image.packed_file:
            fail('Trusted blend profile requires packed images.')


def preview(path):
    scene = bpy.context.scene
    meshes = [o for o in scene.objects if o.type == 'MESH']
    points = [o.matrix_world @ Vector(corner) for o in meshes for corner in o.bound_box]
    if not points:
        fail('No real mesh for preview.')
    minimum = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    maximum = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    center = (minimum + maximum) / 2
    radius = max((maximum-minimum).length, .1)
    data = bpy.data.cameras.new('HatchImportInspectionCamera')
    camera = bpy.data.objects.new('HatchImportInspectionCamera', data)
    scene.collection.objects.link(camera)
    camera.location = center + Vector((radius, -radius*1.6, radius*.8))
    camera.rotation_euler = (center-camera.location).to_track_quat('-Z', 'Y').to_euler()
    data.type = 'ORTHO'; data.ortho_scale = radius*1.3
    scene.camera = camera
    light_data = bpy.data.lights.new('HatchImportInspectionLight', 'AREA')
    light = bpy.data.objects.new('HatchImportInspectionLight', light_data)
    scene.collection.objects.link(light)
    light.location = center + Vector((radius, -radius, radius*2))
    light.rotation_euler = (center-light.location).to_track_quat('-Z', 'Y').to_euler()
    light_data.energy = 500; light_data.shape = 'DISK'; light_data.size = radius
    scene.render.engine = 'CYCLES'; scene.cycles.device = 'CPU'; scene.cycles.samples = 4
    scene.render.threads_mode = 'FIXED'; scene.render.threads = 2
    scene.render.resolution_x = scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'; scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(camera, do_unlink=True)
    bpy.data.objects.remove(light, do_unlink=True)


def main(job):
    mode = job['mode']; source = job.get('input')
    if mode in ('gltf', 'obj'):
        bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
    if mode == 'gltf':
        bpy.ops.import_scene.gltf(filepath=source)
    elif mode == 'obj':
        bpy.ops.wm.obj_import(filepath=source, forward_axis='Z', up_axis='Y')
    elif mode == 'blend':
        bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
        ensure_packed()
    elif mode == 'generate':
        bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
        p = job['parameters']
        if p['primitive'] == 'box':
            bpy.ops.mesh.primitive_cube_add(size=1)
            bpy.context.object.scale = p['dimensions']
        elif p['primitive'] == 'sphere':
            bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=p['dimensions'][0]/2)
        else: fail('Unsupported procedural primitive.')
        obj = bpy.context.object; obj.name = 'ProceduralAsset'
        material = bpy.data.materials.new('ProceduralSurface'); material.diffuse_color = tuple(p['color'])
        material.use_nodes = True
        material.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = tuple(p['color'])
        obj.data.materials.append(material)
        if p['animated']:
            obj.location.x = 0; obj.keyframe_insert(data_path='location', frame=0)
            obj.location.x = 1; obj.keyframe_insert(data_path='location', frame=24)
            bpy.context.scene.frame_start = 0; bpy.context.scene.frame_end = 24
            bpy.context.scene.render.fps = 24; bpy.context.scene.frame_set(0)
    elif mode == 'reopen':
        bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
        ensure_packed()
        result = inspect_scene()
        Path(job['report']).write_text(json.dumps(dict(result, blender_version=bpy.app.version_string)), encoding='utf-8')
        return
    else: fail('Unknown fixed worker mode.')
    result = inspect_scene()
    if result['vertices'] <= 0: fail('Import produced no actual geometry.')
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=job['blend'])
    bpy.ops.export_scene.gltf(filepath=job['glb'], export_format='GLB', export_extras=True,
                             export_animations=True, export_morph=True, export_skins=True, export_cameras=True,
                             export_lights=True, export_frame_range=False)
    preview(job['preview'])
    Path(job['report']).write_text(json.dumps(dict(result, blender_version=bpy.app.version_string)), encoding='utf-8')


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--')+1:]
    if len(args) != 1: fail('Expected one application job file.')
    with open(args[0], encoding='utf-8') as stream: request = json.load(stream)
    main(request)
