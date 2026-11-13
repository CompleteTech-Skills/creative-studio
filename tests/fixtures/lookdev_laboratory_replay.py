"""Exact replay diagnosis with explicit, recorded reference sampler state."""
from pathlib import Path
import hashlib,importlib.util,json,sys
import bpy
import numpy as np
job_path=Path(sys.argv[sys.argv.index('--')+1]);job=json.loads(job_path.read_text())
sys.path.insert(0,job['scripts'])
from lookdev_contracts import content_hash
from lookdev_lighting import build_light
source=Path(__file__).with_name('lookdev_laboratory_sequence.py')
spec=importlib.util.spec_from_file_location('trusted_sequence_fixture',source)
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
root=Path(job['destination']);attempts=[];previous=None;previous_config=None
for attempt in range(3):
    state,body,light=fixture.frame_documents(3);scene,refs=fixture.setup(body)
    fixture.record_document(light);lamp=build_light(fixture.plan_light(light))
    bpy.ops.mesh.primitive_cube_add(size=.3,location=(state['occluder_x'],.2,.45))
    fixture.surface(bpy.context.object,fixture.material_document('occluder',base_color=[.7,.2,.08,1]),'moving-occluder')
    scene.frame_set(3);bpy.context.view_layer.update()
    geometry_hashes={}
    for obj in bpy.data.objects:
        if obj.type=='MESH':
            obj.data.calc_loop_triangles()
            positions=np.empty(len(obj.data.vertices)*3,dtype='<f4');obj.data.vertices.foreach_get('co',positions)
            triangles=np.empty(len(obj.data.loop_triangles)*3,dtype='<i4');obj.data.loop_triangles.foreach_get('vertices',triangles)
            geometry_hashes[obj.name]={'positions_sha256':hashlib.sha256(positions.tobytes()).hexdigest(),
                'triangles_sha256':hashlib.sha256(triangles.tobytes()).hexdigest()}
    configuration={'geometry':geometry_hashes,'seed':scene.cycles.seed,'animated_seed':scene.cycles.use_animated_seed,
        'adaptive_sampling':scene.cycles.use_adaptive_sampling,'light_tree':scene.cycles.use_light_tree,
        'samples':scene.cycles.samples,'threads':scene.render.threads,'threads_mode':scene.render.threads_mode,
        'device':scene.cycles.device,'frame':scene.frame_current,
        'objects':[{'name':obj.name,'type':obj.type,'world_matrix':[list(row) for row in obj.matrix_world],
            'materials':[mat.get('hatch_plan_sha256') for mat in obj.data.materials] if obj.type=='MESH' else [],
            'light':{'energy':obj.data.energy,'color':list(obj.data.color),'size':obj.data.size} if obj.type=='LIGHT' else None}
            for obj in sorted(bpy.data.objects,key=lambda obj:obj.name)]}
    pixels=fixture.capture('replay-'+str(attempt));digest=hashlib.sha256(pixels.astype('<f4').tobytes()).hexdigest()
    entry={'attempt':attempt,'decoded_linear_rgba_sha256':digest,'configuration':configuration,'configuration_sha256':content_hash(configuration)}
    if previous is not None:
        delta=np.abs(previous-pixels);entry.update(max_abs_delta=float(delta.max()),mean_abs_delta=float(delta.mean()),
            bit_exact=np.array_equal(previous,pixels),same_configuration=configuration==previous_config)
    attempts.append(entry)
    (root/'replay-report.json').write_text(json.dumps({'fixture':True,'diagnostic_only':True,'attempts':attempts},indent=2)+'\n')
    if previous is not None:
        assert configuration==previous_config,'actual scene/sampler state changed'
        assert np.array_equal(previous,pixels),'explicit reference sampler still changes decoded pixels'
    previous=pixels;previous_config=configuration
(root/'report.json').write_text(json.dumps({'fixture':True,'diagnostic_only':True,'blender':bpy.app.version_string,
    'attempts':attempts,'outputs':fixture.outputs,'result':'same actual state and bit-exact float pixels across3rebuilds'},indent=2)+'\n')
