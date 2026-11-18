"""Package-owned Blender worker for one static engineering fixture."""
import json
from pathlib import Path
import sys

import bpy

POINTS = ((0, 0, 0), (1, 0, 0), (0, 2, 1))


def main():
    mode, = sys.argv[sys.argv.index('--') + 1:]
    if bpy.app.version != (4, 5, 3) or bpy.app.build_hash != b'67807e1800cc':
        raise RuntimeError('The fixture adapter requires Blender 4.5.3 build 67807e1800cc.')
    if bpy.app.autoexec_fail or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise RuntimeError('The fixture worker requires auto-execution disabled.')
    source = str(Path('fixture.blend').absolute())
    if mode == 'create':
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        scene.render.filepath = '//'
        scene.unit_settings.system = 'METRIC'
        scene.unit_settings.scale_length = 1
        mesh = bpy.data.meshes.new('Triangle')
        mesh.from_pydata(POINTS, [], [(0, 1, 2)])
        mesh.update()
        scene.collection.objects.link(bpy.data.objects.new('Triangle', mesh))
        bpy.ops.file.pack_all()
        # Write scene data without the workspace's file-browser state.
        bpy.data.libraries.write(source, {scene}, path_remap='NONE', compress=False)
    elif mode == 'reopen':
        bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
        scene = bpy.context.scene
        objects = list(scene.objects)
        if (len(objects) != 1 or objects[0].type != 'MESH' or bpy.data.libraries
                or bpy.data.texts or bpy.data.images or bpy.data.actions or bpy.data.materials):
            raise RuntimeError('Unexpected contents in the app-authored packed fixture.')
        obj = objects[0]
        mesh = obj.data
        points = [tuple(vertex.co) for vertex in mesh.vertices]
        faces = [tuple(face.vertices) for face in mesh.polygons]
        if (points != list(POINTS) or faces != [(0, 1, 2)] or obj.animation_data
                or obj.modifiers or obj.constraints or mesh.shape_keys
                or scene.unit_settings.system != 'METRIC' or scene.unit_settings.scale_length != 1):
            raise RuntimeError('Packed fixture geometry or units changed on reopen.')
        bpy.ops.export_scene.gltf(filepath=str(Path('roundtrip.glb').absolute()),
                                  export_format='GLB', export_yup=True, export_animations=False)
        Path('reopen.json').write_text(json.dumps(dict(version='.'.join(map(str, bpy.app.version)),
            build=bpy.app.build_hash.decode('ascii'), vertices=len(points), triangles=len(faces),
            unit_scale=scene.unit_settings.scale_length, external_dependencies=0)),
            encoding='utf-8')
    else:
        raise RuntimeError('Unknown app-owned fixture operation.')


if __name__ == '__main__':
    main()
