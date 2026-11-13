"""Trusted opt-in actual style execution probe; never executes input asset code."""
import hashlib,json,sys
from pathlib import Path
import bpy
from mathutils import Vector

job=json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text())
for source in reversed(job['scripts']):sys.path.insert(0,source)
from lookdev_contracts import Registry,canonical_bytes
from lookdev_blender import material_plan,build_material
from lookdev_styles import mapping_ids,resolve_mapping,execute_mapping
from lookdev_color import configuration,apply_configuration
root=Path(job['output']);root.mkdir(exist_ok=False)
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.samples=8
scene.cycles.seed=843;scene.cycles.use_denoising=False
scene.render.threads_mode='FIXED';scene.render.threads=1
scene.render.resolution_x=scene.render.resolution_y=96;scene.render.resolution_percentage=100
scene.world.color=(.2,.2,.2)
bpy.ops.object.camera_add(location=(2.8,-4,2.2));camera=bpy.context.object
camera.rotation_euler=(Vector((0,0,.6))-camera.location).to_track_quat('-Z','Y').to_euler()
camera.data.type='ORTHO';camera.data.ortho_scale=2.8;scene.camera=camera
bpy.ops.object.light_add(type='AREA',location=(-2,-3,4));light=bpy.context.object
light.data.energy=500;light.data.shape='DISK';light.data.size=2
light.rotation_euler=(Vector((0,0,.6))-light.location).to_track_quat('-Z','Y').to_euler()
bpy.ops.mesh.primitive_plane_add(size=200);floor=bpy.context.object
floor_mat=bpy.data.materials.new('floor');floor_mat.use_nodes=True
floor_mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value=(.08,.12,.2,1)
floor.data.materials.append(floor_mat)

def builder(resource):
    registry=Registry([('material.json',canonical_bytes(resource))])
    return build_material(material_plan(registry,registry.reference(resource['id'],resource['version'])))

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def configure(value):
    # A prior export override must not silently supersede the authored look.
    scene.view_settings.use_curve_mapping=True
    scene.render.image_settings.color_management='OVERRIDE'
    scene.render.image_settings.view_settings.exposure=-3
    receipt=apply_configuration(scene,value)
    assert scene.render.image_settings.color_management=='FOLLOW_SCENE'
    assert not scene.view_settings.use_curve_mapping
    return receipt

report={'blender':bpy.app.version_string,'styles':{},'color':{}}
for name in mapping_ids():
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3,radius=.65,location=(0,0,.7));obj=bpy.context.object
    frame=None
    if name=='metallic-foil':
        bpy.data.objects.remove(obj,do_unlink=True)
        bpy.ops.mesh.primitive_cube_add(size=1.1,location=(0,0,.65));obj=bpy.context.object
    if name=='stained-glass':
        # Four actual lead bars surround a closed glass panel.
        bpy.data.objects.remove(obj,do_unlink=True)
        bpy.ops.mesh.primitive_cube_add(size=1,location=(0,0,.8));obj=bpy.context.object
        obj.scale=(.65,.08,.65)
        bars=[]
        for x,z,sx,sz in [(-.7,.8,.05,.7),(.7,.8,.05,.7),(0,.1,.7,.05),(0,1.5,.7,.05)]:
            bpy.ops.mesh.primitive_cube_add(size=2,location=(x,0,z));bar=bpy.context.object;bar.scale=(sx,.1,sz);bars.append(bar)
        bpy.ops.object.select_all(action='DESELECT')
        for bar in bars:bar.select_set(True)
        bpy.context.view_layer.objects.active=bars[0];bpy.ops.object.join();frame=bars[0]
    old_material=bpy.data.materials.new('unrelated-'+name);old_material.use_nodes=True
    obj.data.materials.append(old_material);obj.material_slots[0].link='OBJECT';obj.material_slots[0].material=old_material
    sibling=obj.copy();sibling.data=obj.data;bpy.context.collection.objects.link(sibling);sibling.hide_render=True
    sibling_mesh=sibling.data
    if frame:
        frame.data.materials.append(old_material);frame.material_slots[0].link='OBJECT';frame.material_slots[0].material=old_material
    mapping=resolve_mapping(name)
    execution=execute_mapping(obj,mapping,material_builder=builder,color=(.45,.1,.04,1.),frame_object=frame)
    assert obj.material_slots[0].material.name==execution['material']
    assert sibling.data==sibling_mesh and sibling.material_slots[0].material==old_material
    if frame:assert frame.material_slots[0].material!=old_material
    configure(configuration(view='AgX'))
    path=root/(name+'.png');scene.render.filepath=str(path);bpy.ops.render.render(write_still=True)
    report['styles'][name]={'execution':execution,'png_sha256':digest(path),'faces':len(obj.data.polygons),
        'smooth_faces':sum(p.use_smooth for p in obj.data.polygons),'mapping':mapping,
        'effective_slot_verified':True,'linked_instance_preserved':True}
    if name=='porcelain':
        # Save actual OCIO outputs and HDR values; a second display transform
        # must not affect scene-linear EXR samples.
        for key,config in [('standard',configuration(view='Standard')),('agx',configuration(view='AgX')),
                           ('warm',configuration(view='Standard',temperature_kelvin=3200)),
                           ('exposed',configuration(view='Standard',exposure=1)),
                           ('linear',configuration(output='scene-linear-exr')),
                           ('linear-display-changed',configuration(output='scene-linear-exr',exposure=2,temperature_kelvin=3200))]:
            receipt=configure(config);suffix='.exr' if config['output']=='scene-linear-exr' else '.png'
            out=root/('color-'+key+suffix);scene.render.filepath=str(out);bpy.ops.render.render(write_still=True)
            report['color'][key]={'configuration':config,'receipt':receipt,'file_sha256':digest(out)}
    bpy.data.objects.remove(obj,do_unlink=True)
    bpy.data.objects.remove(sibling,do_unlink=True)
    if frame:bpy.data.objects.remove(frame,do_unlink=True)
report['sources']={str(Path(folder)/name):digest(Path(folder)/name) for folder in job['scripts'] for name in
    ('lookdev_styles.py','lookdev_color.py','lookdev_blender.py') if (Path(folder)/name).exists()}
(root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
