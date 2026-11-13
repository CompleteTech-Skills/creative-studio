"""Trusted actual Cycles sampler oracle, independent of shader graph construction."""
import hashlib
import json
from pathlib import Path
import sys
import bpy
import numpy as np
from mathutils import Vector
job = json.loads(Path(sys.argv[sys.argv.index('--') + 1]).read_text())
sys.path.insert(0, job['scripts'])
if job.get('native_site'): sys.path.insert(0, job['native_site'])
import lookdev_triplanar
from lookdev_triplanar import create_triplanar_color
from rig_errors import RigError
from lookdev_texture_cache import DerivedTextureCache
from lookdev_textures import project_surface_uvs, verify_texture_document
from rig_math import BLENDER_FROM_CANONICAL
out = Path(job['output']); document = job['document']
payload = (out / 'original.png').read_bytes()
texture = verify_texture_document(document, payload)
bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene; scene.render.engine = 'CYCLES'; scene.cycles.device = 'CPU'
scene.cycles.samples = 1; scene.cycles.use_denoising = False
scene.render.resolution_x = scene.render.resolution_y = 32; scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'OPEN_EXR'; scene.render.image_settings.color_depth = '32'
scene.view_settings.view_transform = 'Standard'
bpy.ops.mesh.primitive_plane_add(size=.3); plane = bpy.context.object
material = bpy.data.materials.new('triplanar-fixture'); material.use_nodes = True; plane.data.materials.append(material)
nodes = material.node_tree.nodes; nodes.clear()
output = nodes.new('ShaderNodeOutputMaterial'); emission = nodes.new('ShaderNodeEmission')
material.node_tree.links.new(emission.outputs[0], output.inputs['Surface'])
# Real native rollback retains caller-owned nodes and every unrelated image.
before_nodes = set(nodes); before_images = set(bpy.data.images)
try:
    create_triplanar_color(material.node_tree, document, payload[:-1], DerivedTextureCache())
except RigError:
    pass
else:
    raise AssertionError('Changed original source was admitted')
original_sampler = lookdev_triplanar.configure_blender_sampler
def fail_sampler(*args, **kwargs):
    raise RuntimeError('Injected failure after real image and nodes were allocated')
lookdev_triplanar.configure_blender_sampler = fail_sampler
try:
    try:
        create_triplanar_color(material.node_tree, document, payload, DerivedTextureCache())
    except RuntimeError as error:
        if 'Injected failure' not in str(error): raise
    else:
        raise AssertionError('Injected failure did not propagate')
finally:
    lookdev_triplanar.configure_blender_sampler = original_sampler
if set(nodes) != before_nodes or set(bpy.data.images) != before_images:
    raise AssertionError('Failed projection changed caller node/image ownership')
color, alpha, receipt = create_triplanar_color(material.node_tree, document, payload,
    DerivedTextureCache(), sharpness=4)
material.node_tree.links.new(color, emission.inputs['Color'])
bpy.ops.object.camera_add(); camera = bpy.context.object; camera.data.type = 'ORTHO'; camera.data.ortho_scale = .01
scene.camera = camera
point = np.array([.18, .38, .58]); center = BLENDER_FROM_CANONICAL[:3,:3] @ point
cases = [('positive-x',[1,0,0]),('negative-x',[-1,0,0]),('positive-y',[0,1,0]),
         ('negative-y',[0,-1,0]),('positive-z',[0,0,1]),('negative-z',[0,0,-1]),
         ('seam-left',[1,.999,0]),('seam-center',[1,1,0]),('seam-right',[.999,1,0])]
results=[]
for label, normal in cases:
    normal = np.array(normal, dtype=float); normal /= np.linalg.norm(normal)
    direction = Vector(BLENDER_FROM_CANONICAL[:3,:3] @ normal)
    plane.location = center; plane.scale = (3, .7, 1.2); plane.rotation_mode = 'QUATERNION'; plane.rotation_quaternion = direction.to_track_quat('Z','Y')
    camera.location = Vector(center) + direction * 2
    camera.rotation_euler = (-direction).to_track_quat('-Z','Y').to_euler()
    scene.render.filepath = str(out / (label + '.exr')); bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(scene.render.filepath, check_existing=False)
    observed = np.array(image.pixels[:], dtype=np.float32).reshape(32,32,4)[16,16,:3]
    bpy.data.images.remove(image)
    uv, weights = project_surface_uvs([point], [normal], mode='triplanar', sharpness=4)
    samples=[]
    for u,v in uv[0]:
        # Blender's image origin is bottom-left; canonical buffers are top-down.
        x = int((u % 1) * texture.width); y = texture.height - 1 - int((v % 1) * texture.height)
        samples.append(texture.pixels[y,x,:3])
    expected = np.sum(np.array(samples) * weights[0,:,None], axis=0)
    error = float(abs(observed-expected).max())
    if error > 2e-5: raise AssertionError((label, observed.tolist(), expected.tolist(), error))
    results.append(dict(label=label, observed=observed.tolist(), expected=expected.tolist(),
                        maximum_error=error, sha256=hashlib.sha256((out/(label+'.exr')).read_bytes()).hexdigest()))
if np.max(abs(np.array(results[-1]['observed'])-results[-3]['observed'])) > .01:
    raise AssertionError('Triplanar color is discontinuous across equal-weight seam')
# The returned alpha socket carries the same weighted source alpha separately.
material.node_tree.links.new(alpha, emission.inputs['Color'])
scene.render.filepath = str(out/'alpha.exr'); bpy.ops.render.render(write_still=True)
image = bpy.data.images.load(scene.render.filepath, check_existing=False)
alpha_pixel = np.array(image.pixels[:], dtype=np.float32).reshape(32,32,4)[16,16,:3]
bpy.data.images.remove(image)
if np.max(abs(alpha_pixel - texture.pixels[0,0,3])) > 2e-5:
    raise AssertionError(('Source alpha changed', alpha_pixel.tolist()))
material.node_tree.links.new(color, emission.inputs['Color'])
bpy.ops.wm.save_as_mainfile(filepath=str(out/'triplanar.blend'))
bpy.ops.wm.open_mainfile(filepath=str(out/'triplanar.blend'), load_ui=False, use_scripts=False)
images = [node.image for node in bpy.data.materials['triplanar-fixture'].node_tree.nodes if node.bl_idname=='ShaderNodeTexImage']
if len(images)!=3 or len(set(images))!=1 or images[0].get('source_sha256')!=document['file']['sha256']:
    raise AssertionError('Packed sampler source binding did not survive reopen')
(out/'report.json').write_text(json.dumps(dict(blender=bpy.app.version_string,cases=results,
    source_sha256=hashlib.sha256(payload).hexdigest(),receipt=receipt,saved_and_reopened=True,
    alpha_observed=alpha_pixel.tolist(),alpha_sha256=hashlib.sha256((out/'alpha.exr').read_bytes()).hexdigest(),
    rollback_preserved_caller_nodes_and_images=True,
    script_sha256={name:hashlib.sha256((Path(job['scripts'])/name).read_bytes()).hexdigest()
                  for name in ('lookdev_triplanar.py','lookdev_textures.py','lookdev_texture_blender.py','lookdev_texture_sampling.py','rig_math.py')}),indent=2))
