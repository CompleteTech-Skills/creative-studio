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
cache=DerivedTextureCache(80);reports=[];render_hashes={}
for color in ('red','blue'):
    pixels=np.zeros((4,4,4),np.float32);pixels[:,:,0 if color=='red' else 2]=1;pixels[:,:,3]=1
    source_hash=hashlib.sha256(pixels.tobytes()).hexdigest()
    source=DecodedTexture(pixels,'fixture-float32',source_hash,'base-color','linear-srgb')
    image,receipt=create_cached_blender_image(cache,source,name=color,profile=UploadProfile(2,80))
    assert receipt['selected_dimensions']==[2,2]
    assert receipt['actual_decoded_encoding']=='linear-srgb'
    assert receipt['pixel_layout']=='float32-rgba'
    assert receipt['decoded_row_order']=='top-down'
    assert receipt['native_row_order']=='bottom-up'
    assert image.colorspace_settings.name=='Non-Color'
    for name,digest in receipt['module_sha256'].items():
        assert hashlib.sha256((Path(job['scripts'])/name).read_bytes()).hexdigest()==digest
    actual=np.array(image.pixels[:],np.float32).reshape(2,2,4)[::-1]
    np.testing.assert_array_equal(actual,pixels[:2,:2])
    sampler.image=image;cache.clear()
    # Clearing CPU cache cannot invalidate native shader image bytes.
    scene.render.filepath=str(out/(color+'.png'));bpy.ops.render.render(write_still=True)
    render_hashes[color+'.png']=hashlib.sha256((out/(color+'.png')).read_bytes()).hexdigest()
    reports.append(receipt)
bpy.ops.wm.save_as_mainfile(filepath=str(out/'consumer.blend'))
bpy.ops.wm.open_mainfile(filepath=str(out/'consumer.blend'))
for receipt in reports:
    name='red' if receipt is reports[0] else 'blue'
    # Red is no longer bound to the shader and is deliberately not persistence
    # evidence. Blue remains referenced by the real material.
    if name=='blue':
        recovered=bpy.data.images[name]
        assert json.loads(recovered['texture_derivation'])==receipt
        rgba=np.array(recovered.pixels[:],np.float32).reshape(2,2,4)[::-1].copy()
        assert hashlib.sha256(rgba.tobytes()).hexdigest()==receipt['selected_float32_rgba_sha256']
(out/'report.json').write_text(json.dumps({'blender':bpy.app.version_string,'receipts':reports,'render_sha256':render_hashes,'reopen':'exact-blue-image-and-derivation'},indent=2))
