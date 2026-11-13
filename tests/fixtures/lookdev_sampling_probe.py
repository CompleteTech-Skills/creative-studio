"""Trusted Blender test fixture for actual cached pixels in a native shader."""
import hashlib
import json
from pathlib import Path
import sys

import bpy
import numpy as np

job=json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text())
sys.path.insert(0,job['scripts'])
from lookdev_texture_blender import create_cached_blender_image
from lookdev_texture_cache import DerivedTextureCache, UploadProfile
from lookdev_textures import DecodedTexture
out=Path(job['output'])
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=4
scene.render.resolution_x=scene.render.resolution_y=32;scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG';scene.view_settings.view_transform='Standard'
scene.view_settings.look='None';scene.view_settings.exposure=0;scene.view_settings.gamma=1
bpy.ops.mesh.primitive_plane_add(size=2)
plane=bpy.context.object
material=bpy.data.materials.new('cache-consumer');material.use_nodes=True;plane.data.materials.append(material)
nodes=material.node_tree.nodes;nodes.clear()
output=nodes.new('ShaderNodeOutputMaterial');emission=nodes.new('ShaderNodeEmission');sampler=nodes.new('ShaderNodeTexImage')
material.node_tree.links.new(sampler.outputs['Color'],emission.inputs['Color'])
material.node_tree.links.new(emission.outputs[0],output.inputs['Surface'])
bpy.ops.object.camera_add(location=(0,0,3));scene.camera=bpy.context.object;scene.camera.data.type='ORTHO';scene.camera.data.ortho_scale=2
from lookdev_texture_sampling import configure_blender_sampler
source=DecodedTexture(np.array(job['pixels'],np.float32),'PNG',job['document']['file']['sha256'],'base-color','srgb')
image,receipt=create_cached_blender_image(DerivedTextureCache(),source,name='sampling',profile=UploadProfile(4,336))
sampler.image=image
reports=[]
for label,filtering,wrapping,u in [('linear','linear','repeat',.5),('nearest','nearest','repeat',.5),
                                  ('repeat','nearest','repeat',1.1),('clamp','nearest','clamp',1.1)]:
    document=dict(job['document'],filter=filtering,wrap=wrapping)
    for loop in plane.data.uv_layers.active.data:loop.uv=(u,.5)
    plan=configure_blender_sampler(sampler,document)
    scene.render.filepath=str(out/(label+'.png'));bpy.ops.render.render(write_still=True)
    reports.append(dict(label=label,sampler=plan,sha256=hashlib.sha256((out/(label+'.png')).read_bytes()).hexdigest()))
(out/'report.json').write_text(json.dumps({'blender':bpy.app.version_string,'cases':reports},indent=2))
