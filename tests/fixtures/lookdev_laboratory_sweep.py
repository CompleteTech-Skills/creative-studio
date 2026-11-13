"""Trusted fixed nine-cell rendered experiment, not executable asset content."""
import hashlib
import json
from pathlib import Path
import sys
import bpy
from mathutils import Vector
job=json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text())
sys.path.insert(0,job['scripts'])
from lookdev_blender import build_material,assign_material
root=Path(job['destination'])
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene
scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=64
scene.cycles.seed=4242;scene.cycles.use_denoising=False
scene.render.threads_mode='FIXED';scene.render.threads=1
scene.render.resolution_x=scene.render.resolution_y=192;scene.render.resolution_percentage=100
scene.render.film_transparent=True
scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA'
scene.render.image_settings.color_depth='8'
scene.view_settings.view_transform='Standard';scene.view_settings.look='None'
scene.view_settings.exposure=0;scene.view_settings.gamma=1
scene.world.use_nodes=True
scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value=0
bpy.ops.mesh.primitive_uv_sphere_add(segments=64,ring_count=32,radius=1)
sphere=bpy.context.object;sphere.name='controlled-sphere'
for face in sphere.data.polygons: face.use_smooth=True
bpy.ops.object.camera_add(location=(0,-5,0))
camera=bpy.context.object;camera.rotation_euler=(Vector((0,0,0))-camera.location).to_track_quat('-Z','Y').to_euler()
camera.data.type='ORTHO';camera.data.ortho_scale=2.4;scene.camera=camera
bpy.ops.object.light_add(type='AREA',location=(-2,-3,2))
light=bpy.context.object;light.data.energy=200;light.data.shape='DISK';light.data.size=.8
light.rotation_euler=(-light.location).to_track_quat('-Z','Y').to_euler()
report={'fixture':True,'visual_approval':False,'blender':bpy.app.version_string,'engine':scene.render.engine,
        'samples':64,'seed':4242,'geometry':'same64x32smoothsphere','camera':'sameorthographic2.4',
        'light':{'type':'AREA','location':list(light.location),'energy':200,'size':.8},'materials':{}}
for plan in job['plans']:
    material=build_material(plan);material.use_fake_user=True
    sphere.data.materials.clear();assign_material(sphere,material)
    node=next(n for n in material.node_tree.nodes if n.bl_idname=='ShaderNodeBsdfPrincipled')
    identifier=plan['reference']['id']
    report['materials'][identifier]={'metallic':node.inputs['Metallic'].default_value,
        'roughness':node.inputs['Roughness'].default_value,'plan_sha256':plan['sha256']}
    scene.render.filepath=str(root/(identifier+'.png'));bpy.ops.render.render(write_still=True)
bpy.ops.wm.save_as_mainfile(filepath=str(root/'sweep.blend'))
(root/'render-report.json').write_text(json.dumps(report,indent=2)+'\n')
