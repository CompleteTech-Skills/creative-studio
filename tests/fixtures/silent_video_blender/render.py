"""Render two small synthetic poses and inventory Blender's output files."""

import hashlib
import json
from pathlib import Path

import bpy


def main():
    root = Path.cwd()
    scene = bpy.context.scene
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()
    assert bpy.app.version == (4, 5, 3), bpy.app.version_string

    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 2
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.image_settings.color_depth = '8'
    scene.render.film_transparent = False
    scene.view_settings.view_transform = 'Standard'
    scene.world.use_nodes = True
    scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0

    fps = {'numerator': 30000, 'denominator': 1001}
    scene.render.fps = 30
    scene.render.fps_base = 1.001
    scene.frame_start, scene.frame_end = 1, 2

    bpy.ops.object.camera_add(location=(0, 0, 5))
    scene.camera = bpy.context.object
    scene.camera.data.type = 'ORTHO'
    scene.camera.data.ortho_scale = 4

    bpy.ops.mesh.primitive_cube_add(size=1)
    cube = bpy.context.object
    for frame, x in ((1, -1), (2, 1)):
        cube.location.x = x
        cube.keyframe_insert(data_path='location', frame=frame)

    material = bpy.data.materials.new('Fixture emission')
    material.use_nodes = True
    nodes = material.node_tree.nodes
    emission = nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value = (1, 0, 0, 1)
    material.node_tree.links.new(emission.outputs[0], nodes['Material Output'].inputs['Surface'])
    cube.data.materials.append(material)

    frames = []
    for index, frame in enumerate(range(scene.frame_start, scene.frame_end + 1)):
        scene.frame_set(frame)
        path = root / f'pose-{frame:04d}.png'
        scene.render.filepath = str(path)
        assert bpy.ops.render.render(write_still=True) == {'FINISHED'}
        data = path.read_bytes()
        frames.append({'index': index, 'path': path.name,
                       'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})

    manifest = {'schema_version': 1, 'kind': 'blender-frame-manifest',
                'engine': scene.render.engine, 'fps': fps, 'frames': frames}
    (root / 'blender-frame-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


main()
