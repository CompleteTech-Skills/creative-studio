"""Fixed Model3D operations run by the pinned rig Blender worker."""
from pathlib import Path
import json

import bpy


def run(job, blender_vector, export_glb):
    mode = job['mode']
    if mode == 'model3d-create':
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=job['input'], merge_vertices=False)
        for obj in bpy.context.scene.objects:
            obj['hatchFixture'] = job['fixture']
    else:
        bpy.ops.wm.open_mainfile(filepath=job['input'], load_ui=False, use_scripts=False)
        export_glb(job['export'], animations=False)
        if mode == 'model3d-edit':
            obj = bpy.data.objects[job['part']]
            delta = blender_vector(job['offset'])
            # Standalone rig_gltf Geometry has identity node transforms.
            for vertex in obj.data.vertices:
                vertex.co += delta
            obj.data.update()
            material = obj.data.materials[0]
            if job['copy_material']:
                material = material.copy()
                material.name = job['material_name']
                obj.data.materials[0] = material
            shader, = (node for node in material.node_tree.nodes if node.type == 'BSDF_PRINCIPLED')
            shader.inputs['Base Color'].default_value = job['color']
            material.diffuse_color = job['color']
    if mode != 'model3d-export':
        bpy.ops.wm.save_as_mainfile(filepath=job['output'], compress=True)
    Path(job['report']).write_text(json.dumps({'blender_version': bpy.app.version_string}), encoding='utf-8')
