"""Fixed trusted actual scenes for experiments B,H,I. No executable asset input."""
from pathlib import Path
import hashlib,json,math,sys
from importlib.metadata import version
import bpy
import numpy as np
from mathutils import Vector
job=json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text());sys.path.insert(0,job['scripts'])
from lookdev_contracts import Registry,canonical_bytes,content_hash
from lookdev_blender import material_plan,build_material,assign_material
from lookdev_lighting import light_plan,build_light,build_environment
from lookdev_laboratory_sequence import material_document,light_document,frame_documents,inspect_mesh_assignment
from verify_lookdev_lighting import reset,render,radiance_fixture
root=Path(job['destination']);outputs=[]


def plan_material(value):
    registry=Registry([(value['id']+'.json',canonical_bytes(value))])
    reference=registry.reference(value['id'],value['version'])
    return material_plan(registry,reference)


def plan_light(value):
    registry=Registry([(value['id']+'.json',canonical_bytes(value))])
    return light_plan(registry,registry.reference(value['id'],value['version']))


def record_document(value):
    payload=canonical_bytes(value);digest=hashlib.sha256(payload).hexdigest()
    target=root/(value['id']+'.json')
    assert not target.exists() or target.read_bytes()==payload,'immutable resource identity collision'
    target.write_bytes(payload)
    return digest


def surface(obj,value,slot):
    record_document(value);plan=plan_material(value);assign_material(obj,build_material(plan))
    obj['hatch_semantic_slot']=slot;obj['hatch_material_reference']=json.dumps(plan['reference'],sort_keys=True)
    return plan['reference']


def sphere(name,location,scale,value,slot):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32,ring_count=16,radius=1,location=location)
    obj=bpy.context.object;obj.name=name;obj.scale=scale
    for face in obj.data.polygons:face.use_smooth=True
    return obj,surface(obj,value,slot)


def setup(body=None):
    scene=reset();scene.render.threads=1;scene.cycles.samples=32;scene.cycles.seed=901
    scene.cycles.use_animated_seed=False;scene.cycles.use_adaptive_sampling=False;scene.cycles.use_light_tree=False
    scene.render.fps=4;scene.frame_start=0;scene.frame_end=7
    camera=scene.camera;camera.location=(2.5,-4,2.1)
    camera.rotation_euler=(Vector((0,0,.7))-camera.location).to_track_quat('-Z','Y').to_euler()
    camera.data.ortho_scale=2.8
    body=body or material_document('canonical-body')
    refs={}
    _,refs['body']=sphere('character-body',(0,0,.8),(.55,.45,.7),body,'body')
    eye=material_document('canonical-eye',base_color=[.005,.005,.005,1],roughness=.2)
    for x in (-.2,.2):
        _,ref=sphere('character-eye-'+str(x),(x,-.415,1.02),(.075,.055,.09),eye,'eyes')
        refs['eyes']=ref
    bpy.ops.mesh.primitive_plane_add(size=20)
    surface(bpy.context.object,material_document('floor',base_color=[.3,.3,.3,1],roughness=.9),'ground')
    return scene,refs


def capture(name):
    pixels,info=render(root,name);outputs.append(info)
    return pixels


def difference(a,b):return float(np.abs(a[:,:,:3]-b[:,:,:3]).mean())


def main():
    report={'fixture':True,'visual_approval':False,'blender':bpy.app.version_string,'engine':'CYCLES',
            'samples':32,'seed':901,'frames_per_second':4,'metrics':{},'outputs':outputs,
            'reference_sampler':{'animated_seed':False,'adaptive_sampling':False,'light_tree':False,'threads':1},
            'dependency_versions':{'numpy':np.__version__,'jsonschema':version('jsonschema')},
            'unsupported':['full demonstration A,C,D,E,F,G,J','browser comparison','subjective production art approval']}
    # H: exact semantic assignment and material documents survive independent shots.
    character={'id':'laboratory-character','version':'1.0.0','origin':'self-authored-engineering-fixture',
               'geometry':'one body sphere and two eye spheres; fixed transforms in trusted source',
               'slots':{'body':plan_material(material_document('canonical-body'))['reference'],
                        'eyes':plan_material(material_document('canonical-eye',base_color=[.005,.005,.005,1],roughness=.2))['reference']}}
    character_hash=content_hash(character);(root/'character.json').write_bytes(canonical_bytes(character))
    shots=[]
    for index in range(3):
        scene,refs=setup();assert refs==character['slots']
        light=light_document('warm-key' if index==2 else 'studio-key',**({'temperature_kelvin':2800} if index==2 else {}))
        record_document(light);lp=plan_light(light);build_light(lp)
        if index==1:
            scene.camera.location=(-2.5,-4,1.7)
            scene.camera.rotation_euler=(Vector((0,0,.7))-scene.camera.location).to_track_quat('-Z','Y').to_euler()
        pixels=capture('shot-'+str(index+1))
        expected_plans={'body':plan_material(material_document('canonical-body')),
            'eyes':plan_material(material_document('canonical-eye',base_color=[.005,.005,.005,1],roughness=.2))}
        assignments={obj.name:{'slot':obj['hatch_semantic_slot'],'reference':json.loads(obj['hatch_material_reference']),
                     'actual_mesh':inspect_mesh_assignment(obj,expected_plans[obj['hatch_semantic_slot']])}
                     for obj in bpy.data.objects if obj.name.startswith('character-')}
        assert len(assignments)==3
        assert all(v['reference']==refs[v['slot']] for v in assignments.values())
        assert content_hash(character)==character_hash
        shots.append({'scene':'warm-scene' if index==2 else 'studio-scene','shot':index+1,
            'character_sha256':character_hash,'material_references':refs,'light_reference':lp['reference'],
            'assignments':assignments,'camera_location_blender':list(scene.camera.location)})
    assert shots[0]['light_reference']==shots[1]['light_reference']
    assert shots[0]['light_reference']!=shots[2]['light_reference']
    report['shots']=shots
    report['completed_stages']=['H'];report['status']='incomplete'
    (root/'partial-report.json').write_text(json.dumps(report,indent=2)+'\n')
    # I: independent absolute-frame evaluation; emission, moving occluder and light.
    frames={};attempts=[];pixels_by_frame={}
    for frame in (7,0,3,1,6,2,5,4,3):
        state,body,light=frame_documents(frame);scene,refs=setup(body)
        record_document(light);lp=plan_light(light);build_light(lp)
        bpy.ops.mesh.primitive_cube_add(size=.3,location=(state['occluder_x'],.2,.45))
        surface(bpy.context.object,material_document('occluder',base_color=[.7,.2,.08,1]),'moving-occluder')
        scene.frame_set(frame)
        pixels=capture('frame-'+str(frame).zfill(4)+'-attempt-'+str(len(attempts)))
        digest=hashlib.sha256(pixels.astype('<f4').tobytes()).hexdigest()
        if frame in frames:assert frames[frame]['decoded_linear_rgba_sha256']==digest,'random seek changed pixels'
        pixels_by_frame[frame]=pixels
        frames[frame]={'state':state,'decoded_linear_rgba_sha256':digest,'body_reference':refs['body'],'light_reference':lp['reference']}
        attempts.append({'frame':frame,'decoded_linear_rgba_sha256':digest})
    assert difference(pixels_by_frame[0],pixels_by_frame[7])>.01
    report['animation']={'frames':frames,'attempts':attempts,'first_last_linear_rgb_mae':difference(pixels_by_frame[0],pixels_by_frame[7]),
        'repeat_frame':3,'duration_seconds':2,'geometry_shadow':'actual cube moves across lit floor; inspect numbered frames'}
    report['completed_stages'].append('I')
    (root/'partial-report.json').write_text(json.dumps(report,indent=2)+'\n')
    # B: same metallic surface reacts to exact HDR pixels and environment rotation.
    scene=reset();scene.render.threads=1;scene.cycles.samples=32;scene.cycles.seed=901
    scene.cycles.use_animated_seed=False;scene.cycles.use_adaptive_sampling=False;scene.cycles.use_light_tree=False
    sphere('reflective-sphere',(0,0,0),(1,1,1),material_document('environment-metal',metallic=1,roughness=.1,base_color=[.8,.8,.8,1]),'metal')
    black=capture('environment-none')
    payload=radiance_fixture();(root/'studio.hdr').write_bytes(payload)
    texture={'schema_version':1,'id':'studio-hdr','version':'1.0.0','kind':'texture3d','dependencies':[],
        'provenance':material_document('texture-provenance')['provenance'],'validation_status':'unvalidated','capabilities':[],
        'file':{'path':'studio.hdr','sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)},
        'role':'environment','encoding':'linear-srgb','width':64,'height':32,'channels':3,'bit_depth':32,
        'wrap':'repeat','filter':'linear','normal_orientation':'not-applicable'}
    texture_ref={'id':'studio-hdr','version':'1.0.0','kind':'texture3d','path':'studio-hdr.json','sha256':hashlib.sha256(canonical_bytes(texture)).hexdigest()}
    environments=[]
    for angle in (0,math.pi/2):
        light=light_document('environment-'+str(len(environments)),type='environment',
            intensity={'value':1,'unit':'watt-per-square-meter'},environment=texture_ref,dependencies=[texture_ref],
            transform={'translation_meters':[0,0,0],'rotation_xyzw':[0,math.sin(angle/2),0,math.cos(angle/2)]})
        light.pop('area_size_meters')
        registry=Registry([('studio-hdr.json',canonical_bytes(texture)),(light['id']+'.json',canonical_bytes(light))],payloads={'studio.hdr':payload})
        lp=light_plan(registry,registry.reference(light['id'],light['version']),options={'camera_visible':False})
        record_document(light);record_document(texture);scene.world=build_environment(lp,payload=payload)
        pixels=capture('environment-'+str(len(environments)));environments.append(pixels)
    assert difference(black,environments[0])>.01
    assert difference(environments[0],environments[1])>.01
    report['metrics']['metal_environment_linear_rgb_mae']=difference(black,environments[0])
    report['metrics']['metal_rotated_environment_linear_rgb_mae']=difference(environments[0],environments[1])
    # Every reported canonical reference resolves to its exact retained payload.
    references=[]
    for shot in shots:references.extend(list(shot['material_references'].values())+[shot['light_reference']])
    for frame in frames.values():references.extend([frame['body_reference'],frame['light_reference']])
    for reference in references:
        assert hashlib.sha256((root/reference['path']).read_bytes()).hexdigest()==reference['sha256']
    report['resolved_reference_count']=len(references)
    bpy.ops.wm.save_as_mainfile(filepath=str(root/'laboratory.blend'))
    report['completed_stages'].append('B-environment');report['status']='completed-scoped-experiments'
    (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')

if __name__=="__main__":main()
