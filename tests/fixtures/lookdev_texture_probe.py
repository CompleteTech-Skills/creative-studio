"""Trusted test fixture: execute decoded texture pixels on real Blender meshes."""
import hashlib
import json
import math
from pathlib import Path
import sys

import bpy
import numpy as np
from mathutils import Vector

job = json.loads(Path(sys.argv[sys.argv.index('--') + 1]).read_text())
sys.path.insert(0, job['scripts'])
from lookdev_textures import DecodedTexture, create_blender_image

out = Path(job['output'])
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
scene.cycles.samples = 16
scene.cycles.use_denoising = False
scene.render.resolution_x = scene.render.resolution_y = 96
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.world.color = (0.06, 0.06, 0.06)
scene.view_settings.view_transform = 'Standard'
scene.view_settings.look = 'None'
scene.view_settings.exposure = 0
scene.view_settings.gamma = 1
bpy.ops.object.camera_add(location=(3, -4, 2.5))
camera = bpy.context.object
camera.rotation_euler = (Vector((0, 0, 0)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
camera.data.type = 'ORTHO'
camera.data.ortho_scale = 3.6
scene.camera = camera
bpy.ops.object.light_add(type='AREA', location=(-3, -4, 5))
light = bpy.context.object
light.data.energy = 400
light.data.shape = 'DISK'
light.data.size = 2
light.rotation_euler = (-light.location).to_track_quat('-Z', 'Y').to_euler()
image = create_blender_image(DecodedTexture(np.array(job['pixels'], dtype=np.float32),
    'PNG', job['source_sha256'], 'normal', 'data'), name='verified-normal')
material = bpy.data.materials.new('normal-probe')
material.use_nodes = True
shader = material.node_tree.nodes.get('Principled BSDF')
shader.inputs['Base Color'].default_value = (0.5, 0.5, 0.5, 1)
shader.inputs['Roughness'].default_value = 0.55
texture = material.node_tree.nodes.new('ShaderNodeTexImage')
texture.image = image
texture.extension = 'REPEAT'
texture.interpolation = 'Linear'
normal = material.node_tree.nodes.new('ShaderNodeNormalMap')
normal.space = 'TANGENT'
material.node_tree.links.new(texture.outputs['Color'], normal.inputs['Color'])
material.node_tree.links.new(normal.outputs['Normal'], shader.inputs['Normal'])
records = []
for case in ('curved', 'hard-edge', 'mirrored', 'rotated', 'nonuniform', 'deforming'):
    if case == 'hard-edge':
        bpy.ops.mesh.primitive_cube_add(size=1.7)
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16)
    mesh = bpy.context.object
    mesh.data.materials.append(material)
    if case != 'hard-edge':
        for polygon in mesh.data.polygons:
            polygon.use_smooth = True
    uv = mesh.data.uv_layers.active
    for loop in uv.data:
        if case == 'mirrored':
            loop.uv.x = 1 - loop.uv.x
        elif case == 'rotated':
            loop.uv = (loop.uv.y, 1 - loop.uv.x)
    if case == 'nonuniform':
        mesh.scale = (1, 0.55, 1.25)
    if case == 'deforming':
        mesh.shape_key_add(name='Basis')
        key = mesh.shape_key_add(name='Bent')
        for vertex in key.data:
            vertex.co.x += 0.5 * vertex.co.z * vertex.co.z
        key.value = 0
        key.keyframe_insert('value', frame=1)
        key.value = 1
        key.keyframe_insert('value', frame=2)
        scene.frame_set(2)
    for strength, suffix in ((0, 'flat'), (1, 'normal')):
        normal.inputs['Strength'].default_value = strength
        path = out / (case + '-' + suffix + '.png')
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        records.append({'case': case, 'strength': strength, 'file': path.name,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    if case != 'deforming':
        bpy.data.objects.remove(mesh, do_unlink=True)
pixels = np.array(image.pixels[:], dtype=np.float32).reshape(image.size[1], image.size[0], 4)[::-1]
np.testing.assert_allclose(pixels, np.array(job['pixels'], dtype=np.float32), atol=1e-7)
bpy.ops.wm.save_as_mainfile(filepath=str(out / 'textured.blend'))
bpy.ops.wm.open_mainfile(filepath=str(out / 'textured.blend'))
reopened = bpy.data.images['verified-normal']
pixels = np.array(reopened.pixels[:], dtype=np.float32).reshape(reopened.size[1], reopened.size[0], 4)[::-1]
np.testing.assert_allclose(pixels, np.array(job['pixels'], dtype=np.float32), atol=1e-7)
(out / 'report.json').write_text(json.dumps({'blender': bpy.app.version_string,
    'source_sha256': reopened['source_sha256'], 'packed': bool(reopened.packed_file),
    'pixel_max_error': float(np.max(abs(pixels - np.array(job['pixels'])))), 'renders': records}, indent=2))
