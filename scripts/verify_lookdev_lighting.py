"""Actual small-scene Blender validation for lookdev_lighting.

Invoke with Blender 4.5 LTS --background --factory-startup --disable-autoexec
--python-exit-code 1 --python THIS_FILE -- NEW_DESTINATION. This engineering
fixture is not a production-art approval or a render queue implementation.
"""
from pathlib import Path
import hashlib
import json
import math
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bpy
import numpy as np
from camera3d_math import look_at
from lookdev_contracts import Registry, canonical_bytes
from lookdev_lighting import build_light, build_environment, light_plan, probe


def light(kind='point', position=(0,1,0), target=(0,0,0), power=683, **extras):
    up = (0,0,1) if position[0] == target[0] and position[2] == target[2] else (0,1,0)
    value = dict(schema_version=1,id='fixture-'+kind,version='1.0.0',kind='light3d',dependencies=[],
        provenance={'origin':'procedural-fixture','tool':'verify_lookdev_lighting',
                    'created_at':'2026-11-13T00:00:00Z','source_hashes':[],
                    'attribution':'Self-authored engineering fixture',
                    'license':{'identifier':'CC0-1.0','source':'self-authored'}},
        validation_status='unvalidated',capabilities=[],type=kind,
        transform={'translation_meters':list(position),'rotation_xyzw':look_at(position,target,up=up).tolist()},
        color={'encoding':'linear-srgb','value':[1,1,1]},
        intensity={'value':power,'unit':{'point':'candela','spot':'candela','directional':'lux',
                                       'area':'watt','environment':'watt-per-square-meter'}[kind]},
        casts_shadow=True,render_policy={'strict':True,'allow_approximations':False,
                                       'required_features':[],'fallback':'reject'})
    if kind in ('point','spot'):value['radius_meters']=0
    if kind == 'spot':value.update(spot_inner_radians=.5,spot_outer_radians=1.)
    if kind == 'area':value['area_size_meters']=[.1,.1]
    value.update(extras)
    return value


def resolve(value, options=None):
    registry=Registry([('light.json',canonical_bytes(value))])
    return light_plan(registry,registry.reference(value['id'],value['version']),
                      engine=bpy.context.scene.render.engine,options=options)


def material(color=(.6,.6,.6,1), metallic=0., roughness=.5, emission=0.):
    mat=bpy.data.materials.new('fixture-material');mat.use_nodes=True
    principled=mat.node_tree.nodes.get('Principled BSDF')
    principled.inputs['Base Color'].default_value=color
    principled.inputs['Metallic'].default_value=metallic
    principled.inputs['Roughness'].default_value=roughness
    principled.inputs['Emission Color'].default_value=color
    principled.inputs['Emission Strength'].default_value=emission
    return mat


def plane(size=4):
    bpy.ops.mesh.primitive_plane_add(size=size)
    obj=bpy.context.object;obj.data.materials.append(material());return obj


def reset(engine='CYCLES'):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene=bpy.context.scene
    scene.render.engine=engine
    scene.render.resolution_x=96;scene.render.resolution_y=96;scene.render.resolution_percentage=100
    scene.render.threads_mode='FIXED';scene.render.threads=2
    scene.render.image_settings.file_format='OPEN_EXR';scene.render.image_settings.color_mode='RGBA'
    scene.render.image_settings.color_depth='32';scene.render.film_transparent=False
    scene.view_settings.view_transform='Standard';scene.view_settings.look='None'
    scene.world=bpy.data.worlds.new('black');scene.world.use_nodes=True
    scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value=0
    if engine=='CYCLES':
        scene.cycles.device='CPU';scene.cycles.samples=64;scene.cycles.seed=842
        scene.cycles.use_denoising=False;scene.cycles.max_bounces=6
        scene.cycles.diffuse_bounces=4;scene.cycles.glossy_bounces=4
    camera=bpy.data.cameras.new('fixture-camera');obj=bpy.data.objects.new('fixture-camera',camera)
    scene.collection.objects.link(obj);scene.camera=obj
    obj.location=(0,0,5);camera.type='ORTHO';camera.ortho_scale=4
    return scene


def render(destination,name):
    scene=bpy.context.scene
    start=time.monotonic();path=destination/(name+'.exr');scene.render.filepath=str(path)
    bpy.ops.render.render(write_still=True)
    image=bpy.data.images.load(str(path),check_existing=False)
    try:
        pixels=np.asarray(image.pixels[:],dtype=np.float64).reshape(image.size[1],image.size[0],4)
        assert np.isfinite(pixels).all(), 'Nonfinite rendered pixels'
        png=destination/(name+'.png')
        scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_depth='8'
        image.save_render(str(png),scene=scene)
        scene.render.image_settings.file_format='OPEN_EXR';scene.render.image_settings.color_depth='32'
    finally:bpy.data.images.remove(image)
    return pixels,{'name':name,'seconds':time.monotonic()-start,
                   'exr_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                   'png_sha256':hashlib.sha256(png.read_bytes()).hexdigest()}


def center(pixels):return float(np.mean(pixels[46:50,46:50,:3]))


def radiance_fixture(width=64,height=32,scale=1.):
    # Self-authored equirectangular high-dynamic-range studio window, stored as
    # exact flat RGBE source. Bright and dim directions are genuine float values.
    body=bytearray()
    for y in range(height):
        for x in range(width):
            value=12. if width//8 <= x < width//3 and height//4 <= y < 3*height//4 else .05
            mantissa,exponent=math.frexp(value*scale)
            component=int(mantissa*256)
            body.extend((component,component,component,exponent+128))
    return b'#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y '+str(height).encode()+b' +X '+str(width).encode()+b'\n'+body


def main(destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
    outputs=[];metrics={}
    scene=reset();metrics['probe']=probe()
    # Real inverse square response at a small central surface patch.
    plane();point=build_light(resolve(light()))
    near,info=render(destination,'point-near');outputs.append(info)
    point.location.z=2
    far,info=render(destination,'point-far');outputs.append(info)
    ratio=center(near)/center(far);assert 3.8 < ratio < 4.2, ratio
    metrics['point_inverse_square_ratio']=ratio
    # Receiver exclusions execute in Cycles rather than remaining metadata.
    reset();floor=plane()
    build_light(resolve(light(), options={'excluded_receivers':['floor']}),
                receivers={'floor':floor})
    excluded,info=render(destination,'point-excluded');outputs.append(info)
    assert center(excluded)<1e-6,center(excluded)
    metrics['excluded_receiver_center']=center(excluded)
    # Direct-sun illuminance is independent of translation.
    reset();plane();sun=build_light(resolve(light('directional')))
    close,info=render(destination,'sun-near');outputs.append(info)
    sun.location=(20,30,40)
    distant,info=render(destination,'sun-far');outputs.append(info)
    ratio=center(close)/center(distant);assert abs(ratio-1)<1e-6,ratio
    metrics['sun_translation_ratio']=ratio
    # All four physical source types execute, including exact spot cone mapping.
    reset();plane();spot=build_light(resolve(light('spot')))
    spot_pixels,info=render(destination,'spot');outputs.append(info)
    assert center(spot_pixels)>.01
    metrics['spot_center']=center(spot_pixels)
    # Geometry-dependent shadows: changing only the finite emitter size widens
    # the partial-visibility region. Normalize by an unobstructed render.
    scene=reset();plane();area=build_light(resolve(light('area',position=(-1.2,2,.4),power=150)))
    clear,info=render(destination,'area-clear');outputs.append(info)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24,ring_count=12,radius=.35,location=(0,0,.7))
    sphere=bpy.context.object;sphere.data.materials.append(material())
    hard,info=render(destination,'area-hard');outputs.append(info)
    area.data.size=1.5;area.data.size_y=1.5
    sphere.hide_render=True
    clear_soft,info=render(destination,'area-soft-clear');outputs.append(info)
    sphere.hide_render=False
    soft,info=render(destination,'area-soft');outputs.append(info)
    denominator=np.maximum(clear[:,:,:3].mean(2),1e-5)
    hard_vis=hard[:,:,:3].mean(2)/denominator
    soft_vis=soft[:,:,:3].mean(2)/np.maximum(clear_soft[:,:,:3].mean(2),1e-5)
    # Exclude projected sphere and its immediate contact region.
    yy,xx=np.indices(hard_vis.shape);mask=(xx-48)**2+(yy-48)**2>12**2
    hard_partial=int(np.count_nonzero(mask & (hard_vis>.15) & (hard_vis<.85)))
    soft_partial=int(np.count_nonzero(mask & (soft_vis>.15) & (soft_vis<.85)))
    assert soft_partial>hard_partial+30,(hard_partial,soft_partial)
    assert np.min(hard_vis[mask])<.15
    metrics['shadow_partial_pixels']={'small':hard_partial,'large':soft_partial}
    # Actual emission and diffuse interreflection. A red wall is lit from its
    # front while the ground receives the bounced red illumination.
    scene=reset();floor=plane();floor.data.materials[0]=material((.8,.8,.8,1))
    bpy.ops.mesh.primitive_plane_add(size=2,location=(-.7,0,1),rotation=(0,math.pi/2,0))
    wall=bpy.context.object;wall.data.materials.append(material((.8,.03,.03,1),roughness=1))
    build_light(resolve(light('area',position=(.1,1,0),target=(-.7,1,0),power=100,
                                  area_size_meters=[.5,.5])))
    scene.cycles.diffuse_bounces=0
    direct,info=render(destination,'bounce-disabled');outputs.append(info)
    scene.cycles.diffuse_bounces=4
    bounced,info=render(destination,'bounce-enabled');outputs.append(info)
    delta=bounced[35:60,42:66,:3].mean((0,1))-direct[35:60,42:66,:3].mean((0,1))
    assert delta[0]>.003 and delta[0]>delta[1]*2,delta
    metrics['diffuse_red_bounce_delta']=delta.tolist()
    scene=reset();plane();dark,info=render(destination,'emission-disabled');outputs.append(info)
    bpy.ops.mesh.primitive_plane_add(size=1,location=(0,0,1),rotation=(math.pi,0,0))
    emitter=bpy.context.object;emitter.data.materials.append(material((1,.5,.1,1),emission=5))
    lit,info=render(destination,'emission-enabled');outputs.append(info)
    assert center(lit)>center(dark)+.03
    metrics['emission_ground_delta']=center(lit)-center(dark)
    # Exact source HDR maps actually illuminate and rotate specular reflections.
    reset();plane();bpy.ops.mesh.primitive_uv_sphere_add(segments=32,ring_count=16,radius=.7,location=(0,0,.7))
    sphere=bpy.context.object;sphere.data.materials.append(material((.8,.8,.8,1),metallic=1,roughness=.12))
    payload=radiance_fixture();(destination/'studio.hdr').write_bytes(payload)
    file={'path':'studio.hdr','sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)}
    texture={'schema_version':1,'id':'studio-hdr','version':'1.0.0','kind':'texture3d','dependencies':[],
        'provenance':light()['provenance'],'validation_status':'unvalidated','capabilities':[],
        'file':file,'role':'environment','encoding':'linear-srgb','width':64,'height':32,
        'channels':3,'bit_depth':32,'wrap':'repeat','filter':'linear','normal_orientation':'not-applicable'}
    texture_ref={'id':texture['id'],'version':texture['version'],'kind':texture['kind'],'path':'texture.json',
                 'sha256':hashlib.sha256(canonical_bytes(texture)).hexdigest()}
    env=light('environment',power=1,environment=texture_ref,dependencies=[texture_ref])
    env['transform']['rotation_xyzw']=[0,0,0,1]
    registry=Registry([('texture.json',canonical_bytes(texture)),('env.json',canonical_bytes(env))],payloads={'studio.hdr':payload})
    reference=registry.reference(env['id'],env['version'])
    initial=light_plan(registry,reference)
    bpy.context.scene.world=build_environment(initial,payload=payload)
    hdr,info=render(destination,'hdri');outputs.append(info)
    # Identical directional HDR data at a different pixel magnitude must produce
    # the same requested reference irradiance, through the exact-byte path.
    import copy
    scaled_payload=radiance_fixture(scale=4.)
    scaled_texture=copy.deepcopy(texture)
    scaled_texture['file'].update(sha256=hashlib.sha256(scaled_payload).hexdigest(),
                                  bytes=len(scaled_payload))
    scaled_ref=copy.deepcopy(texture_ref)
    scaled_ref['sha256']=hashlib.sha256(canonical_bytes(scaled_texture)).hexdigest()
    scaled_env=copy.deepcopy(env);scaled_env['environment']=scaled_ref;scaled_env['dependencies']=[scaled_ref]
    scaled_registry=Registry([('texture.json',canonical_bytes(scaled_texture)),
                              ('env.json',canonical_bytes(scaled_env))],payloads={'studio.hdr':scaled_payload})
    scaled_plan=light_plan(scaled_registry,scaled_registry.reference(scaled_env['id'],scaled_env['version']))
    bpy.context.scene.world=build_environment(scaled_plan,payload=scaled_payload)
    hdr_scaled,info=render(destination,'hdri-source-scaled');outputs.append(info)
    scale_delta=float(np.max(np.abs(hdr_scaled-hdr)))
    assert scale_delta<1e-5,scale_delta
    metrics['hdri_source_scale_max_delta']=scale_delta
    exposed=light_plan(registry,reference,options={'exposure_stops':1})
    bpy.context.scene.world=build_environment(exposed,payload=payload)
    hdr_exposed,info=render(destination,'hdri-exposure');outputs.append(info)
    exposure_ratio=float(hdr_exposed[:,:,:3].mean()/hdr[:,:,:3].mean())
    assert 1.95<exposure_ratio<2.05,exposure_ratio
    metrics['hdri_exposure_ratio']=exposure_ratio
    env['transform']['rotation_xyzw']=[0,math.sin(math.pi/4),0,math.cos(math.pi/4)]
    registry=Registry([('texture.json',canonical_bytes(texture)),('env.json',canonical_bytes(env))],payloads={'studio.hdr':payload})
    rotated=light_plan(registry,registry.reference(env['id'],env['version']))
    bpy.context.scene.world=build_environment(rotated,payload=payload)
    rotate,info=render(destination,'hdri-rotated');outputs.append(info)
    diff=float(np.mean(np.abs(hdr[:,:,:3]-rotate[:,:,:3])))
    assert diff>.01,diff
    metrics['hdri_rotation_mean_difference']=diff
    # EEVEE is actually exercised; GI parity is deliberately not asserted.
    reset('BLENDER_EEVEE_NEXT');plane();build_light(resolve(light('area',position=(0,2,0),power=100)))
    eevee,info=render(destination,'eevee-area');outputs.append(info)
    assert center(eevee)>.01
    metrics['eevee_area_center']=center(eevee)
    result={'schema_version':1,'fixture':True,'visual_approval':False,'blender':bpy.app.version_string,
            'source_sha256':hashlib.sha256(Path(__file__).with_name('lookdev_lighting.py').read_bytes()).hexdigest(),
            'source_files': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ('lookdev_lighting.py', 'lookdev_textures.py',
                                          'verify_lookdev_lighting.py')},
            'metrics':metrics,'frames':outputs}
    (destination/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    print('LOOKDEV_LIGHTING_VERIFIED',json.dumps(metrics,sort_keys=True))


if __name__=='__main__':main(sys.argv[sys.argv.index('--')+1])
