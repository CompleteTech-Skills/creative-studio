"""Fixed trusted fixture used by test_rig_lookdev_blender; never asset code."""
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

job_path, mode = sys.argv[sys.argv.index('--') + 1:]
job = json.loads(Path(job_path).read_text())
sys.path.insert(0, job['scripts'])
from lookdev_blender import assign_material, build_material, probe, apply_displacement
from rig_errors import RigError
from copy import deepcopy
from lookdev_contracts import content_hash

root = Path(job['destination'])


def inspect(material):
    node = next(n for n in material.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    return {key: node.inputs[key].default_value for key in ('Metallic', 'Roughness', 'IOR', 'Alpha')}


def aim(obj, point):
    obj.rotation_euler = (Vector(point) - obj.location).to_track_quat('-Z', 'Y').to_euler()


if mode == 'render':
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = job.get('engine', 'CYCLES')
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 8
    scene.cycles.seed = 4242
    scene.cycles.use_denoising = False
    scene.cycles.max_bounces = 6
    scene.cycles.diffuse_bounces = 3
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 1
    scene.render.resolution_x = scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '8'
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'Medium High Contrast'
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.world.use_nodes = True
    scene.world.node_tree.nodes.get('Background').inputs['Color'].default_value = (0.05, 0.05, 0.05, 1)
    scene.world.node_tree.nodes.get('Background').inputs['Strength'].default_value = 0.3
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=0.7, location=(0, 0, 0.8))
    sphere = bpy.context.object
    sphere.name = 'lookdev-sphere'
    for face in sphere.data.polygons:
        face.use_smooth = True
    bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, 0))
    floor = bpy.context.object
    floor_mat = bpy.data.materials.new('fixture-floor')
    floor_mat.use_nodes = True
    floor_mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = (0.15, 0.15, 0.15, 1)
    floor_mat.node_tree.nodes.get('Principled BSDF').inputs['Roughness'].default_value = 0.9
    floor.data.materials.append(floor_mat)
    bpy.ops.object.camera_add(location=(2.5, -4, 2.2))
    camera = bpy.context.object
    aim(camera, (0, 0, 0.75))
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = 2.4
    scene.camera = camera
    for name, location, energy, size in [('key', (-2, -2, 3), 180, 0.6), ('rim', (0, 2, 1.8), 280, 1.0)]:
        bpy.ops.object.light_add(type='AREA', location=location)
        light = bpy.context.object
        light.name = name
        light.data.energy = energy
        light.data.shape = 'DISK'
        light.data.size = size
        aim(light, (0, 0, 0.8))
    invalid = deepcopy(job['plans'][0])
    invalid['engine'] = 'BLENDER_EEVEE_NEXT' if scene.render.engine == 'CYCLES' else 'CYCLES'
    invalid['sha256'] = content_hash({k: v for k, v in invalid.items() if k != 'sha256'})
    before_count = len(bpy.data.materials)
    try:
        build_material(invalid)
        raise AssertionError('Backend mismatch accepted')
    except RigError as exc:
        assert exc.report['code'] == 'LOOKDEV_BLENDER_ENGINE'
    assert len(bpy.data.materials) == before_count
    advanced = build_material(job['advanced'])
    advanced.use_fake_user = True
    advanced_bsdf = next(n for n in advanced.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    for socket, expected in ([] if job.get('engine') == 'BLENDER_EEVEE_NEXT' else [('Specular IOR Level', 0.3), ('Coat Weight', 0.7),
                             ('Coat Roughness', 0.2), ('Sheen Weight', 0.3),
                             ('Sheen Roughness', 0.6), ('Emission Strength', 2.0),
                             ('Transmission Weight', 0.25), ('Anisotropic', 0.4)]):
        assert abs(advanced_bsdf.inputs[socket].default_value - expected) < 1e-6, socket
    result = {'blender': bpy.app.version_string, 'engine': scene.render.engine,
              'samples': scene.cycles.samples, 'probe': probe(), 'materials': {}}
    for value in job['plans']:
        material = build_material(value)
        material.use_fake_user = True
        if sphere.data.materials:
            sphere.data.materials.clear()
        assign_material(sphere, material)
        identifier = value['reference']['id']
        result['materials'][identifier] = inspect(material)
        for key in ('Metallic', 'Roughness', 'IOR'):
            expected = value['surface'][key.lower()]
            assert abs(result['materials'][identifier][key] - expected) < 1e-6
        if identifier == 'sss-on':
            assert any(n.bl_idname == 'ShaderNodeBsdfPrincipled' and n.inputs['Subsurface Weight'].default_value > 0 for n in material.node_tree.nodes)
        scene.render.filepath = str(root / (identifier + '.png'))
        bpy.ops.render.render(write_still=True)
    # A real image-driven Displace modifier must change vertices, never just a label.
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=8, y_subdivisions=8, size=1, location=(100, 100, 100))
    displaced = bpy.context.object
    displaced.name = 'displacement-fixture'
    image = bpy.data.images.new('height-fixture', width=2, height=2)
    image.pixels = [0.8, 0.8, 0.8, 1.0] * 4
    image.update()
    initial_mesh = displaced.data
    initial = [tuple(vertex.co) for vertex in initial_mesh.vertices]
    setting = dict(enabled=True, scale_meters=0.25, midlevel=0.5, max_subdivision=0)
    try:
        apply_displacement(displaced, image, setting, max_vertices=1)
        raise AssertionError('Exceeded vertex budget accepted')
    except RigError as exc:
        assert exc.report['code'] == 'LOOKDEV_DISPLACEMENT_LIMIT'
    assert displaced.data == initial_mesh
    assert [tuple(vertex.co) for vertex in displaced.data.vertices] == initial
    modifier = displaced.modifiers.new('existing-rig-owner', 'ARMATURE')
    try:
        apply_displacement(displaced, image, setting)
        raise AssertionError('Existing modifier was flattened')
    except RigError as exc:
        assert exc.report['code'] == 'LOOKDEV_DISPLACEMENT_OWNER'
    assert displaced.modifiers[0] == modifier
    displaced.modifiers.remove(modifier)
    result['displacement'] = apply_displacement(displaced, image, setting)
    assert displaced.data != initial_mesh
    maximum_change = max(abs(vertex.co.z - old[2]) for vertex, old in zip(displaced.data.vertices, initial))
    assert maximum_change > 0.01
    result['displacement']['maximum_vertex_z_change'] = maximum_change
    result['displacement']['source_data_preserved'] = [tuple(vertex.co) for vertex in initial_mesh.vertices] == initial
    bpy.ops.wm.save_as_mainfile(filepath=str(root / 'materials.blend'))
    (root / 'report.json').write_text(json.dumps(result, indent=2))
elif mode == 'reopen':
    bpy.ops.wm.open_mainfile(filepath=str(root / 'materials.blend'), load_ui=False)
    sphere = bpy.data.objects['lookdev-sphere']
    material = bpy.data.materials['hatch:glossy']
    sphere.data.materials.clear()
    assign_material(sphere, material)
    bpy.context.scene.render.filepath = str(root / 'reopened-glossy.png')
    bpy.ops.render.render(write_still=True)
    result = {'material_count': sum(m.name.startswith('hatch:') for m in bpy.data.materials),
              'glossy_plan_sha256': material['hatch_plan_sha256'], 'sockets': inspect(material)}
    (root / 'reopen.json').write_text(json.dumps(result, indent=2))
else:
    raise ValueError('Unknown fixture operation')
