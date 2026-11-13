"""Trusted geometry-shadow controls. Run with the source-bound laboratory runner."""
from pathlib import Path
import hashlib,json,sys
import bpy
import numpy as np
from mathutils import Vector
job=json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text());sys.path.insert(0,job['scripts'])
from lookdev_laboratory_sequence import material_document,light_document
from lookdev_contracts import Registry,canonical_bytes
from lookdev_blender import material_plan,build_material,assign_material
from lookdev_lighting import light_plan,build_light
from verify_lookdev_lighting import reset,render
root=Path(job['destination']);scene=reset();scene.render.threads=1;scene.cycles.samples=64;scene.cycles.seed=912
scene.render.resolution_x=scene.render.resolution_y=128
outputs=[];metrics={}


def material(value):
    payload=canonical_bytes(value);(root/(value['id']+'.json')).write_bytes(payload)
    registry=Registry([(value['id']+'.json',payload)])
    return build_material(material_plan(registry,registry.reference(value['id'],value['version'])))


def lamp(value):
    payload=canonical_bytes(value);(root/(value['id']+'.json')).write_bytes(payload)
    registry=Registry([(value['id']+'.json',payload)])
    return build_light(light_plan(registry,registry.reference(value['id'],value['version'])))


def capture(name):
    pixels,info=render(root,name);outputs.append(info);return pixels[:,:,:3].mean(2)


def visibility(name,occluded,clear):
    mask=clear>1e-5
    ratio=np.divide(occluded,clear,out=np.ones_like(clear),where=mask)
    ratio=np.clip(ratio,0,1);weight=(1-ratio)*mask
    assert weight.sum()>1,'no geometry-dependent shadow'
    y,x=np.indices(clear.shape)
    center=[float(((x-63.5)*weight).sum()/weight.sum()),float(((y-63.5)*weight).sum()/weight.sum())]
    # Diagnostic is measured attenuation of actual rendered illumination.
    image=bpy.data.images.new('shadow-only-'+name,width=128,height=128,float_buffer=True)
    rgba=np.stack([weight,weight,weight,np.ones_like(weight)],axis=-1).astype('float32')
    image.pixels.foreach_set(rgba.ravel());image.update()
    scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_depth='8'
    image.save_render(str(root/('shadow-only-'+name+'.png')),scene=scene);bpy.data.images.remove(image)
    scene.render.image_settings.file_format='OPEN_EXR';scene.render.image_settings.color_depth='32'
    return {'centroid_pixels':center,'penumbra_pixels':int(((ratio>.05)&(ratio<.95)&mask).sum()),
            'dark_pixels':int(((ratio<.05)&mask).sum()),'mean_visibility':float(ratio[mask].mean())}


bpy.ops.mesh.primitive_plane_add(size=4)
floor=bpy.context.object;floor.name='receiving-floor'
assign_material(floor,material(material_document('shadow-floor',base_color=[.7,.7,.7,1],roughness=1)))
bpy.ops.mesh.primitive_cube_add(size=.5,location=(0,0,.65))
cube=bpy.context.object;cube.name='occluding-cube';cube.visible_camera=False
assign_material(cube,material(material_document('shadow-occluder',base_color=[.3,.3,.3,1])))
area=light_document('small-emitter');area['area_size_meters']=[.1,.1]
area['transform']['translation_meters']=[-1,3,1]
from camera3d_math import look_at
area['transform']['rotation_xyzw']=look_at([-1,3,1],[0,0,0]).tolist()
key=lamp(area)
cube.hide_render=True;clear=capture('area-clear');cube.hide_render=False
small=capture('area-small');metrics['small']=visibility('small',small,clear)
key.data.size=key.data.size_y=1.2
cube.hide_render=True;wide_clear=capture('area-wide-clear');cube.hide_render=False
wide=capture('area-wide');metrics['wide']=visibility('wide',wide,wide_clear)
assert metrics['wide']['penumbra_pixels']>metrics['small']['penumbra_pixels']*2
key.data.size=key.data.size_y=.1;key.location.x=1
key.rotation_euler=(-key.location).to_track_quat('-Z','Y').to_euler()
cube.hide_render=True;moved_clear=capture('area-moved-clear');cube.hide_render=False
moved=capture('area-moved');metrics['moved']=visibility('moved',moved,moved_clear)
assert metrics['small']['centroid_pixels'][0]>2
assert metrics['moved']['centroid_pixels'][0]<-2
key.location.x=-1;key.rotation_euler=(-key.location).to_track_quat('-Z','Y').to_euler()
cube.location.z=1.2
high=capture('occluder-high');metrics['high']=visibility('high',high,clear)
assert metrics['high']['centroid_pixels'][0]>metrics['small']['centroid_pixels'][0]*1.5
cube.location.z=.25
contact=capture('occluder-contact');metrics['contact']=visibility('contact',contact,clear)
assert float((contact[62:66,62:66]/clear[62:66,62:66]).mean())<.02
cube.location.z=.65
key.location.z=5;key.rotation_euler=(-key.location).to_track_quat('-Z','Y').to_euler()
cube.hide_render=True;far_clear=capture('area-far-clear');cube.hide_render=False
far=capture('area-far');metrics['far']=visibility('far',far,far_clear)
assert metrics['far']['centroid_pixels'][0]<metrics['small']['centroid_pixels'][0]*.8
bpy.data.objects.remove(key,do_unlink=True)
for kind in ('directional','spot'):
    value=light_document(kind+'-emitter',type=kind,transform=area['transform'])
    value.pop('area_size_meters')
    value['intensity']={'value':683,'unit':'lux' if kind=='directional' else 'candela'}
    if kind=='spot':value.update(radius_meters=0,spot_inner_radians=.8,spot_outer_radians=1.4)
    key=lamp(value)
    cube.hide_render=True;reference=capture(kind+'-clear');cube.hide_render=False
    pixels=capture(kind+'-shadow');metrics[kind]=visibility(kind,pixels,reference)
    bpy.data.objects.remove(key,do_unlink=True)
bpy.ops.wm.save_as_mainfile(filepath=str(root/'shadows.blend'))
(root/'report.json').write_text(json.dumps({'fixture':True,'visual_approval':False,'demonstration':'C geometry shadow controls',
    'blender':bpy.app.version_string,'engine':'CYCLES','samples':64,'seed':912,'metrics':metrics,'outputs':outputs,
    'diagnostics':'shadow-only PNGs are ratios from actual clear/occluded linear EXR renders; no painted texture input'},indent=2)+'\n')
