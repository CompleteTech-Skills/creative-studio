"""Actual Cycles normal-vector output, compared to analytic world-space TBN."""
import bpy,sys,json,math,hashlib
import numpy as np
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from lookdev_textures import DecodedTexture,create_blender_image
root=Path(sys.argv[sys.argv.index('--')+1])
root.mkdir(parents=True,exist_ok=True)
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.samples=1;scene.cycles.use_denoising=False
scene.render.resolution_x=scene.render.resolution_y=32;scene.render.resolution_percentage=100
scene.view_settings.view_transform='Standard';scene.view_settings.look='None';scene.render.image_settings.file_format='OPEN_EXR';scene.render.image_settings.color_depth='32'
bpy.ops.object.camera_add(location=(0,0,4));camera=bpy.context.object;camera.data.type='ORTHO';camera.data.ortho_scale=1;scene.camera=camera
bpy.ops.mesh.primitive_plane_add(size=2);mesh=bpy.context.object
mat=bpy.data.materials.new('direction');mat.use_nodes=True;mesh.data.materials.append(mat);nodes=mat.node_tree.nodes;links=mat.node_tree.links;nodes.clear()
output=nodes.new('ShaderNodeOutputMaterial');emission=nodes.new('ShaderNodeEmission');normal=nodes.new('ShaderNodeNormalMap');image_node=nodes.new('ShaderNodeTexImage')
color=np.array([[[.68,.74,.9,1]]],np.float32)
image_node.image=create_blender_image(DecodedTexture(color,'PNG','a'*64,'normal','data'),name='normal')
links.new(image_node.outputs['Color'],normal.inputs['Color'])
scale=nodes.new('ShaderNodeVectorMath');scale.operation='SCALE';scale.inputs[3].default_value=.5
add=nodes.new('ShaderNodeVectorMath');add.operation='ADD';add.inputs[1].default_value=(.5,.5,.5)
links.new(normal.outputs['Normal'],scale.inputs[0]);links.new(scale.outputs[0],add.inputs[0]);links.new(add.outputs[0],emission.inputs[0]);links.new(emission.outputs[0],output.inputs['Surface'])
report={};base=[tuple(x.uv) for x in mesh.data.uv_layers.active.data]
for case in ('base','mirror','rotate','scale','deform'):
 mesh.scale=(1,1,1)
 for loop,uv in zip(mesh.data.uv_layers.active.data,base):loop.uv=(1-uv[0],uv[1]) if case=='mirror' else ((uv[1],1-uv[0]) if case=='rotate' else uv)
 if case=='scale':mesh.scale=(.7,1.3,1)
 if case=='deform':
  mesh.shape_key_add(name='Basis');key=mesh.shape_key_add(name='Tilt')
  for v in key.data:v.co.z=.25*v.co.x
  key.value=0;key.keyframe_insert('value',frame=1);key.value=1;key.keyframe_insert('value',frame=2);scene.frame_set(2)
 scene.render.filepath=str(root/(case+'.exr'));bpy.ops.render.render(write_still=True)
 img=bpy.data.images.load(scene.render.filepath,check_existing=False);px=np.array(img.pixels[:],np.float32).reshape(32,32,4);report[case]=px[16,16,:3].tolist();bpy.data.images.remove(img)
(root/'report.json').write_text(json.dumps(report,indent=2))

(root/'manifest.json').write_text(json.dumps(dict(blender=bpy.app.version_string,engine=scene.render.engine,frames={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.exr')}),indent=2))
