"""Strict structural/cross-record checks for unsigned retained physics evidence.

These checks bind records to one another. They are not identity signatures or
proof that an externally supplied receipt was produced by a particular person.
No file is opened and no backend is imported/executed by this module.
"""
import math
import re
import struct


class EvidenceError(ValueError):
    pass


def blend_container(payload):
    """Validate the bounded uncompressed64-bit container produced by this worker.

    This does not execute the file, inspect arbitrary embedded scripts, or
    authenticate a supplied file's historical solver provenance.
    """
    need(type(payload) is bytes and len(payload)>=36 and payload[:12]==b'BLENDER-v405',
         'Native artifact is not the admitted uncompressed Blender4.5 container.')
    position=12;dna=False;scene=False;objects=False
    while position<len(payload):
        need(position+24<=len(payload),'Native Blend block header is truncated.')
        code=payload[position:position+4]
        size=struct.unpack_from('<I',payload,position+4)[0]
        start=position+24;end=start+size
        need(end<=len(payload),'Native Blend block payload is truncated.')
        if code==b'ENDB':
            need(size==0 and end==len(payload) and dna and scene and objects,'Native Blend lacks its complete scene/object/DNA/end inventory.')
            return
        if code==b'DNA1':
            need(not dna and payload[start:start+8]==b'SDNANAME','Native Blend DNA descriptor is missing/duplicated.')
            dna=True
        if code[:2]==b'SC':scene=True
        if code[:2]==b'OB':objects=True
        position=end
    raise EvidenceError('Native Blend end marker is missing.')


def need(condition,message):
    if not condition:raise EvidenceError(message)


def number(value):
    return (type(value) is int and abs(value)<=10**300) or (type(value) is float and math.isfinite(value))


def integer(value):
    return type(value) is int or (type(value) is float and math.isfinite(value) and value.is_integer())


def object_fields(value,fields,label):
    need(type(value) is dict and set(value)==set(fields),label+' has missing/unknown fields or wrong object type.')


def sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def vector(value,size=3):
    return type(value) is list and len(value)==size and all(number(x) and abs(x)<=1e12 for x in value)


def module_identity(value):
    object_fields(value,('available','solver_version','api_version','module_path','module_sha256','python_version','platform'),'DIRECT module identity')
    need(value['available'] is True and value['solver_version']=='3.2.7' and
         integer(value['api_version']) and value['api_version']==202010061 and sha(value['module_sha256']) and
         type(value['module_path']) is str and 0<len(value['module_path'])<=4096 and value['module_path'].startswith('/') and
         value['module_path'].endswith('.so') and not any(ord(c)<32 for c in value['module_path']) and
         type(value['python_version']) is str and re.fullmatch(r'3\.(?:11|12)\.(?:0|[1-9][0-9]{0,5})',value['python_version']) is not None and
         value['platform'] in ('linux','darwin'), 'Unsupported or malformed recorded DIRECT module/interpreter identity.')


def blender_identity(value):
    object_fields(value,('application_version','application_build','solver_name','solver_version','platform','precision','device',
                        'render_device_discovery','render_devices','physics_gpu_support','render_gpu_execution'),'Native Blender identity')
    need(value['application_version']=='4.5.3 LTS' and value['application_build']=='67807e1800cc' and
         value['solver_name']=='Blender Bullet rigid-body world' and value['solver_version']=='not exposed separately by Blender' and
         value['platform'] in ('linux','darwin') and value['precision']=='mixed' and value['device']=='cpu' and
         value['physics_gpu_support']=='unsupported' and value['render_gpu_execution']=='not-executed',
         'Unsupported or contradictory recorded Blender identity.')
    discovery=value['render_device_discovery']
    need(type(discovery) is str and (discovery in ('not-available','executed-device-enumeration-only') or discovery.startswith('discovery-failed:')),
         'Invalid GPU discovery state.')
    need(type(value['render_devices']) is list and len(value['render_devices'])<=64,'Invalid GPU discovery inventory.')
    for device in value['render_devices']:
        object_fields(device,('name','type'),'Render device')
        need(type(device['name']) is str and len(device['name'])<=256 and type(device['type']) is str and 0<len(device['type'])<=64,
             'Invalid render device metadata.')


def native_report(value,world,operation):
    common={'ok','operation','backend','native_bake_completed','native_cache_is_baked','native_cache_is_outdated'}
    fields=common|({'cache_storage','sample_count','object_count','diagnostic_render','actual_substeps_per_frame','actual_solver_iterations',
        'actual_fps','actual_gravity_canonical','effective_mass_policy','seed_policy','quality_policy','sphere_margin_policy','actual_bodies'}
        if operation=='simulate' else {'reversed_samples','maximum_replay_error','replay_tolerance'})
    if operation=='simulate' and 'rigid_controls' in value: fields.add('rigid_controls')
    if operation=='simulate' and world['joints']:
        fields.add('joints')
        from physics_joints import validate_native_report
        validate_native_report(value.get('joints'),world)
    object_fields(value,fields,'Blender '+operation+' report')
    if operation=='simulate': rigid_report(value,world,'blender')
    blender_identity(value['backend'])
    need(value['ok'] is True and value['operation']==operation and value['native_bake_completed'] is True and
         value['native_cache_is_baked'] is True and type(value['native_cache_is_outdated']) is bool,'Invalid native bake observations.')
    if operation=='simulate':
        need(value['cache_storage']=='embedded Blender point cache' and
             all(integer(value[key]) for key in ('sample_count','object_count','actual_substeps_per_frame','actual_solver_iterations')) and
             vector(value['actual_gravity_canonical']), 'Invalid native physical count/settings observations.')
        object_fields(value['actual_fps'],('numerator','denominator'),'Native frame rate')
        need(all(number(x) and x>0 for x in value['actual_fps'].values()),'Invalid native frame rate values.')
        need(value['effective_mass_policy']=='explicit body mass; material density does not override body mass' and
             value['seed_policy']=='recorded identity only; no stochastic parameter is used by this adapter' and
             value['quality_policy']=='explicit admitted substeps/iterations; quality label does not retune solver' and
             value['sphere_margin_policy']=='analytic sphere radius is inherent; nonzero additional skin is rejected',
             'Recorded physical parameter policy differs from this adapter.')
        image=value['diagnostic_render']
        object_fields(image,('path','sha256','engine','device','width','height','samples','scope','visual_review'),'Diagnostic render')
        need(image['path']=='preview.png' and sha(image['sha256']) and image['engine']=='Cycles' and image['device']=='CPU' and
             image['width']==128 and image['height']==128 and image['samples']==4 and
             image['scope']=='neutral solver diagnostic only' and image['visual_review']=='not-performed','Invalid diagnostic render observations.')
        need(type(value['actual_bodies']) is list and len(value['actual_bodies'])==len(world['bodies']),'Invalid native body observations.')
        for body in value['actual_bodies']:
            object_fields(body,('id','type','shape','effective_mass','friction','restitution','linear_damping','angular_damping','collision_margin'),'Native body')
            need(type(body['id']) is str and body['type'] in ('ACTIVE','PASSIVE') and body['shape'] in ('SPHERE','BOX') and
                 all(number(body[key]) for key in ('effective_mass','friction','restitution','linear_damping','angular_damping','collision_margin')),
                 'Invalid native physical body values.')
    else:
        need(integer(value['reversed_samples']) and number(value['maximum_replay_error']) and 0<=value['maximum_replay_error']<=1e-5 and
             number(value['replay_tolerance']) and value['replay_tolerance']==1e-5,'Invalid native replay measurements.')


def direct_report(value,world):
    object_fields(value,('ok','operation','backend','connection_mode','sample_count','object_count',
                        'actual_solver_steps','actual_bodies','cache_storage','restart','contacts_queried') +
                        (('effective_engine','maximum_steps_per_output_interval','steps_per_output_interval') if type(value) is dict and 'effective_engine' in value else ('actual_steps_per_frame',)) +
                        (('rigid_controls',) if type(value) is dict and 'rigid_controls' in value else ()), 'DIRECT solver report')
    rigid_report(value,world,'pybullet')
    if 'effective_engine' in value:
        need(integer(value['maximum_steps_per_output_interval']) and type(value['steps_per_output_interval']) is list and
             all(integer(x) for x in value['steps_per_output_interval']),'Invalid actual output interval counts.')
        object_fields(value['effective_engine'],('fixed_time_step','internal_substeps','iterations'),'DIRECT engine')
        need(number(value['effective_engine']['fixed_time_step']) and integer(value['effective_engine']['internal_substeps']) and integer(value['effective_engine']['iterations']),
             'Malformed native engine observations.')
    module_identity(value['backend'])
    need(value['ok'] is True and value['operation']=='simulate' and value['connection_mode']=='DIRECT' and value['contacts_queried'] is True and
         all(integer(value[key]) for key in ('sample_count','object_count','actual_solver_steps')) and
         value['cache_storage']=='immutable native transform/contact samples' and value['restart']=='clean-restart-only','Invalid DIRECT observations.')
    need(type(value['actual_bodies']) is list and len(value['actual_bodies'])==len(world['bodies']),'Invalid DIRECT body observations.')
    for body in value['actual_bodies']:
        object_fields(body,('id','mass','friction','inertia','center_of_mass','restitution','native_margin','margin_semantics'),'DIRECT body')
        need(type(body['id']) is str and all(number(body[key]) for key in ('mass','friction','restitution','native_margin')) and
             vector(body['inertia']) and vector(body['center_of_mass']) and body['margin_semantics'] in ('inherent sphere radius','transformed primitive wrapper margin','explicit box collision margin'),
             'Invalid DIRECT physical body observations.')


def consumption_report(value,world):
    object_fields(value,('ok','operation','backend','source_file_modified','control_is_separate_in_memory','baked_flags','after_free_bake_flags',
        'changed_gravity_canonical','body','frame','baked_under_changed_gravity','clean_recompute_under_changed_gravity','baked_pose_error',
        'changed_gravity_displacement','consumption_demonstrated'),'Native consumption control')
    blender_identity(value['backend'])
    need(value['ok'] is True and value['operation']=='consumption-control' and value['source_file_modified'] is False and
         value['control_is_separate_in_memory'] is True and value['consumption_demonstrated'] is True and
         type(value['body']) is str and value['body'] in {body['id'] for body in world['bodies']} and integer(value['frame']) and value['frame']==world['frame_range']['end']-1,
         'Invalid native cache-consumption identity/operation.')
    for name in ('baked_flags','after_free_bake_flags'):
        object_fields(value[name],('is_baked','is_outdated'),'Native cache flags')
        need(all(type(flag) is bool for flag in value[name].values()),'Invalid native cache flags.')
    need(value['baked_flags']['is_baked'] is True and value['after_free_bake_flags']['is_baked'] is False and
         all(vector(value[name]) for name in ('changed_gravity_canonical','baked_under_changed_gravity','clean_recompute_under_changed_gravity')) and
         number(value['baked_pose_error']) and 0<=value['baked_pose_error']<=1e-5 and
         number(value['changed_gravity_displacement']) and value['changed_gravity_displacement']>.01,
         'Invalid native consumption control measurements.')


def records(manifest,world,receipt,request,simulation,last_report,source_meshes,worker_hash,job_hash):
    backend=manifest['backend']['id']
    common={'schema_version','world_sha256','source_admission','project_manifest','backend_identity','worker_job_sha256',
            'worker_sha256','executable_sha256','solver_report','native_continuation','artistic_review'}
    specific={'native_reopen','native_consumption_control','unsupported','restart'} if backend=='blender' else {'backend_module','sample_cache_verification'}
    object_fields(receipt,common|specific,'Execution verification receipt')
    need(integer(receipt['schema_version']) and receipt['schema_version']==1 and receipt['world_sha256']==manifest['world_sha256'] and
         receipt['source_admission']=='native Scene3D contract/hash closure and actual rig OBJ decode' and
         type(receipt['project_manifest']) is str and receipt['backend_identity']==manifest['backend'] and
         receipt['worker_sha256']==worker_hash and receipt['worker_job_sha256']==job_hash and
         receipt['executable_sha256']==manifest['backend']['executable_sha256'] and
         receipt['native_continuation']=='unsupported-clean-restart-only' and receipt['artistic_review']=='not-performed',
         'Receipt identity/source/version/worker binding differs.')
    object_fields(request,('protocol','mode','world','world_sha256','meshes','worker_sha256','fixture'),'Retained worker request')
    need(integer(request['protocol']) and request['protocol']==1 and request['mode']=='simulate' and request['world']==world and
         request['world_sha256']==manifest['world_sha256'] and request['worker_sha256']==worker_hash and
         request['fixture'] is manifest['fixture'] and type(request['meshes']) is dict and request['meshes']==source_meshes,
         'Retained worker request differs from admitted native geometry/world.')
    need(simulation==receipt['solver_report'],'Retained simulation report differs from its verification record.')
    header=manifest['backend']
    if backend=='blender':
        native_report(simulation,world,'simulate')
        native_report(receipt['native_reopen'],world,'reopen')
        native=simulation['backend']
        need(header['application_version']=='Blender '+native['application_version'] and header['solver_name']==native['solver_name'] and
             header['solver_version']==native['solver_version'] and header['precision']==native['precision'] and header['device']==native['device'],
             'Blender backend identity contradicts actual solver observations.')
        need((native['platform']=='linux' and header['platform'].startswith('Linux-')) or
             (native['platform']=='darwin' and header['platform'].startswith(('macOS-','Darwin-'))),
             'Recorded Blender platform contradicts actual native observations.')
        control=receipt['native_consumption_control']
        if control is not None:consumption_report(control,world)
        need(last_report==(control if control is not None else receipt['native_reopen']),'Last worker report contradicts the recorded operation.')
        need(receipt['restart']=='clean-restart-only' and receipt['unsupported']==(([] if world['joints'] else ['joints'])+['off-center-forces','torques','impulses','initial-velocity','shifted-center-of-mass','custom-inertia','sensors','mask-filtering','other-categories'] if 'rigid_controls' in simulation else ['joints','forces','initial-velocity','sensors','mask-filtering','other-categories']),
             'Unsupported feature/recovery declarations differ.')
    else:
        module_identity(receipt['backend_module'])
        direct_report(simulation,world)
        need(simulation['backend']==receipt['backend_module'] and last_report==simulation and
             header['solver_name']=='PyBullet DIRECT (bundled Bullet)' and header['solver_version']=='PyBullet '+receipt['backend_module']['solver_version'] and
             header['application_version']=='Python '+receipt['backend_module']['python_version'] and header['precision']=='mixed' and header['device']=='cpu',
             'DIRECT backend identity contradicts actual module/interpreter observations.')
        native=receipt['backend_module']
        need((native['platform']=='linux' and header['platform'].startswith('Linux-')) or
             (native['platform']=='darwin' and header['platform'].startswith(('macOS-','Darwin-'))),
             'Recorded DIRECT platform contradicts actual module observations.')


def rigid_report(report,world,backend):
    def custom_inertia(body):
        shape=body['colliders'][0]['shape'];mass=body['mass']['kilograms']
        if body['mode']!='dynamic':return False
        if shape['type']=='sphere':diagonal=[.4*mass*shape['radius']**2]*3
        elif shape['type']=='box':
            x,y,z=shape['size'];diagonal=[mass*(y*y+z*z)/12,mass*(x*x+z*z)/12,mass*(x*x+y*y)/12]
        else:return True
        return any(abs(body['mass']['inertia'][i][j]-(diagonal[i] if i==j else 0))>max(diagonal)*1e-6 for i in range(3) for j in range(3))
    requested = bool(world['forces']) or any(body['mode']=='kinematic' or 'sleep' in body or custom_inertia(body) or
        any(body['linear_velocity']+body['angular_velocity']+body['mass']['center_of_mass']) for body in world['bodies'])
    if 'rigid_controls' not in report:
        need(not requested, 'Native report omits requested physical controls.')
        return
    value=report['rigid_controls']
    object_fields(value,('version','bodies','force_policy','forces')+(('effectors',) if backend=='blender' else ()),'Rigid control observations')
    need(integer(value['version']) and value['version']==1 and same_json(value['forces'],world['forces']) and type(value['bodies']) is list and len(value['bodies'])==len(world['bodies']),
         'Rigid control identity/count differs.')
    need(value['force_policy']==('Blender native WIND plane effector central force only' if backend=='blender' else
         'native external force/torque per fixed step; discrete impulse is integrated wrench over one complete native step'), 'Unknown physical control policy.')
    for observed,body in zip(value['bodies'],world['bodies']):
        fields=('id','mode','sleep')+(('kinematic',) if backend=='blender' else
            ('inertial_position','inertial_rotation','principal_inertia','initial_linear_velocity','initial_angular_velocity','collision'))
        object_fields(observed,fields,'Rigid body controls')
        need(observed['id']==body['id'] and observed['mode']==body['mode'],'Physical authority differs.')
        expected=dict(enabled=body.get('sleep',{}).get('enabled',False),initially_sleeping=body.get('sleep',{}).get('initially_sleeping',False),
            linear_threshold=body.get('sleep',{}).get('linear_threshold',.4) if backend=='blender' else None,
            angular_threshold=body.get('sleep',{}).get('angular_threshold',.5) if backend=='blender' else None)
        settings=observed['sleep']
        object_fields(settings,('enabled','initially_sleeping','linear_threshold','angular_threshold'),'Sleep observations')
        need(type(settings['enabled']) is bool and type(settings['initially_sleeping']) is bool and
             settings['enabled']==expected['enabled'] and settings['initially_sleeping']==expected['initially_sleeping'] and
             all((settings[k] is None if expected[k] is None else number(settings[k]) and abs(settings[k]-expected[k])<=1e-6) for k in ('linear_threshold','angular_threshold')), 'Native sleep settings differ.')
        if backend=='blender':
            need(type(observed['kinematic']) is bool and observed['kinematic']==(body['mode']=='kinematic'), 'Native kinematic authority differs.')
        else:
            shape=observed['collision']
            object_fields(shape,('type','dimensions','translation','rotation'),'Native collision child')
            need(shape['type'] in ('sphere','box') and vector(shape['dimensions']) and vector(shape['translation']) and
                 type(shape['rotation']) is list and len(shape['rotation'])==4 and all(number(x) for x in shape['rotation']) and
                 abs(sum(x*x for x in shape['rotation'])-1)<=1e-6, 'Native primitive collision child is malformed.')
            need(vector(observed['inertial_position']) and all(abs(a-b)<=1e-6 for a,b in zip(observed['inertial_position'],body['mass']['center_of_mass'])) and
                 type(observed['inertial_rotation']) is list and len(observed['inertial_rotation'])==4 and
                 all(number(x) for x in observed['inertial_rotation']) and abs(sum(x*x for x in observed['inertial_rotation'])-1)<=1e-6 and
                 vector(observed['principal_inertia']) and
                 all(vector(observed['initial_'+kind+'_velocity']) and all(abs(a-b)<=1e-6 for a,b in zip(observed['initial_'+kind+'_velocity'],body[kind+'_velocity'])) for kind in ('linear','angular')),
                 'Native initial velocity or inertial frame differs.')
    if backend=='blender':
        need(type(value['effectors']) is list and len(value['effectors'])==len(world['forces']),'Native force effector count differs.')
        for observed,force in zip(value['effectors'],world['forces']):
            object_fields(observed,('id','object','type','shape','flow','falloff_power','strength_per_newton'),'Native force effector')
            need(observed['id']==force['id'] and type(observed['object']) is str and observed['type']=='WIND' and observed['shape']=='PLANE' and
                 number(observed['flow']) and observed['flow']==0 and number(observed['falloff_power']) and observed['falloff_power']==0 and number(observed['strength_per_newton']) and
                 observed['strength_per_newton']==world['fps']['numerator'],'Native central force mapping differs.')


def same_json(actual,expected):
    """Compare bounded known-shape records without bool/number equivalence."""
    if type(expected) is dict:
        return type(actual) is dict and set(actual)==set(expected) and all(same_json(actual[k],v) for k,v in expected.items())
    if type(expected) is list:
        return type(actual) is list and len(actual)==len(expected) and all(same_json(a,b) for a,b in zip(actual,expected))
    if type(expected) in (int,float):return number(actual) and actual==expected
    return type(actual) is type(expected) and actual==expected
