"""Trusted physics lifecycle tests; real solver positives are explicitly opt-in.

Mocked process success is used only to prove that missing outputs are rejected.
It never supplies positive readiness, cache, or numerical solver evidence.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import venv

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as physics
from physics_backend_example import build_inputs


def error_code(test,expected,operation):
    with test.assertRaises(physics.BackendError) as raised:
        operation()
    test.assertEqual(raised.exception.report['code'],expected)
    test.assertFalse(raised.exception.report['cache_valid'])
    return raised.exception


class PhysicsBackendAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.world,self.payloads=build_inputs()
        self.backend=physics.BlenderBackend('/not/an/installed/blender')
        self.temp=tempfile.TemporaryDirectory(prefix='hatch-physics-tests-')
        self.root=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_configuration_is_not_solver_or_source_readiness(self):
        report=self.backend.validate_scene(self.world)
        self.assertEqual(report['status'],'configuration-valid')
        self.assertFalse(report['native_scene_verified'])
        self.assertFalse(report['payloads_verified'])
        self.assertFalse(report['solver_executed'])
        self.assertEqual(report['steps_per_frame'],10)

    def test_integral_json_numbers_are_canonical_and_caller_is_unchanged(self):
        original=deepcopy(self.world)
        self.world['fps']['numerator']=24.0
        self.world['frame_range']['end']=49.0
        self.world['solver']['substeps']=1.0
        self.world['solver']['iterations']=20.0
        self.world['budgets']['bodies']=32.0
        before=deepcopy(self.world)
        self.assertEqual(self.backend.validate_scene(self.world)['world_sha256'],self.backend.validate_scene(original)['world_sha256'])
        self.assertEqual(self.world,before)

    def test_embedded_scripts_unknown_fields_and_nonfinite_values_fail_before_execution(self):
        for mutate in [lambda w:w.update(python_script='raise RuntimeError("must not run")'),
                       lambda w:w['bodies'][0]['transform']['translation'].__setitem__(0,float('nan')),
                       lambda w:w['solver'].update(worker_script='/tmp/arbitrary.py')]:
            value=deepcopy(self.world);mutate(value)
            with patch.object(self.backend,'_discover') as discover:
                error_code(self,'PHYSICS_SCHEMA',lambda:self.backend.prepare(value,self.payloads))
                discover.assert_not_called()

    def test_declared_unsupported_parameters_are_never_silently_ignored(self):
        cases=[('PHYSICS_CAPABILITY_UNAVAILABLE',lambda w:w['solver']['required_categories'].append('cloth')),
               ('PHYSICS_BODY_UNSUPPORTED',lambda w:w['bodies'][0]['linear_velocity'].__setitem__(0,1)),
               ('PHYSICS_SHAPE_UNSUPPORTED',lambda w:w['bodies'][0]['colliders'][0].update(sensor=True)),
               ('PHYSICS_SHAPE_UNSUPPORTED',lambda w:w['bodies'][0]['colliders'][0].update(mask=1)),
               ('PHYSICS_SHAPE_UNSUPPORTED',lambda w:w['bodies'][0]['colliders'][0]['transform']['translation'].__setitem__(0,1)),
               ('PHYSICS_MATERIAL_UNSUPPORTED',lambda w:w['materials'][0].update(static_friction=.8)),
               ('PHYSICS_MARGIN_UNSUPPORTED',lambda w:w['materials'][0].update(collision_margin=.1)),
               ('PHYSICS_TIME_UNSUPPORTED',lambda w:w['solver'].update(fixed_step=dict(numerator=1,denominator=241))),
               ('PHYSICS_REPRODUCIBILITY',lambda w:w['solver'].update(reproducibility='pinned-numerical')),
               ('PHYSICS_INERTIA_UNSUPPORTED',lambda w:w['bodies'][0]['mass'].update(inertia=[[.2,0,0],[0,.2,0],[0,0,.2]])),
               ('PHYSICS_RESOURCE_LIMIT',lambda w:w['budgets'].update(memory_bytes=1024))]
        for code,mutate in cases:
            with self.subTest(code=code):
                value=deepcopy(self.world);mutate(value)
                error_code(self,code,lambda:self.backend.validate_scene(value))

    def test_valid_joint_and_force_requests_are_explicit_capability_errors(self):
        joint=dict(id='hinge',type='hinge',body_a='ball',body_b='floor',
            anchor_a=dict(translation=[0,0,0],rotation=[0,0,0,1]),anchor_b=dict(translation=[0,0,0],rotation=[0,0,0,1]),
            axis=[1,0,0],limits=[-1,1],stiffness=1,damping=.1,motor_velocity=0,motor_max_force=0,collide_connected=False)
        force=dict(id='push',type='force',bodies=['ball'],vector=[1,0,0],point=[0,0,0],
            frame_range=dict(start=0,end=49),strength=1,seed=1,categories=['rigid-body'])
        for key,item in [('joints',joint),('forces',force)]:
            value=deepcopy(self.world);value[key]=[item]
            error_code(self,'PHYSICS_CAPABILITY_UNAVAILABLE' if key=='joints' else 'PHYSICS_FORCE_UNSUPPORTED',lambda:self.backend.validate_scene(value))

    def test_box_property_storage_is_not_extra_contact_skin_support(self):
        value=deepcopy(self.world)
        material=deepcopy(value['materials'][0]);material.update(id='box-only',collision_margin=.05)
        value['materials'].append(material)
        value['bodies'][1]['colliders'][0]['material']='box-only'
        # Sphere still requests zero: this must reject the box request itself,
        # not accidentally rely on the analytic sphere's earlier limitation.
        self.assertEqual(value['materials'][0]['collision_margin'],0)
        error_code(self,'PHYSICS_MARGIN_UNSUPPORTED',lambda:self.backend.validate_scene(value))

    def test_unsafe_source_paths_aliases_and_mutable_payloads_fail_before_discovery(self):
        cases=[('../escape',b'x'),('a/../escape',b'x'),('CON.txt',b'x'),('COM¹.txt',b'x'),('e\u0301.obj',b'x'),('input.py\n',b'x')]
        for name,data in cases:
            with self.subTest(name=name),patch.object(self.backend,'_discover') as discover:
                error_code(self,'PHYSICS_PATH',lambda:self.backend.prepare(self.world,{name:data}))
                discover.assert_not_called()
        with patch.object(self.backend,'_discover') as discover:
            error_code(self,'PHYSICS_PATH_ALIAS',lambda:self.backend.prepare(self.world,{'a':b'x','a-b':b'x','a/b':b'x'}))
            error_code(self,'PHYSICS_INPUTS',lambda:self.backend.prepare(self.world,{'project.json':bytearray(b'{}')}))
            discover.assert_not_called()

    def test_process_failure_timeout_and_cancellation_have_no_successful_cache(self):
        error=error_code(self,'PHYSICS_BACKEND_CRASH',lambda:physics._run_process(
            [sys.executable,'-I','-c','import sys;print("actual worker failure",flush=True);sys.exit(7)'],self.root,
            timeout=5,memory_bytes=1024**3,output_bytes=16*1024**2))
        self.assertEqual(error.report['process_exit_code'],7)
        self.assertIn('actual worker failure',error.report['stdout_tail'])
        start=time.monotonic()
        error_code(self,'PHYSICS_TIMEOUT',lambda:physics._run_process(
            [sys.executable,'-I','-c','import os,time;print(os.getpid(),flush=True);time.sleep(30)'],self.root,
            timeout=.15,memory_bytes=1024**3,output_bytes=16*1024**2,label='timeout'))
        self.assertLess(time.monotonic()-start,4)
        pid=int((self.root/'timeout-stdout.log').read_text().strip())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        cancel=threading.Event();timer=threading.Timer(.1,cancel.set);timer.start()
        try:
            error_code(self,'PHYSICS_CANCELLED',lambda:physics._run_process(
                [sys.executable,'-I','-c','import time;time.sleep(30)'],self.root,
                timeout=5,memory_bytes=1024**3,output_bytes=16*1024**2,cancel_token=cancel,label='cancel'))
        finally:timer.cancel()

    def test_worker_numerical_failure_preserves_last_valid_frame(self):
        # A real failing process exercises the error protocol only. This is not
        # a positive solver/readiness test or a fabricated numerical result.
        code='import json,sys;open("worker-report.json","w").write(json.dumps({"ok":False,"error":{"code":"PHYSICS_NUMERICAL_INSTABILITY","message":"bad evaluated state","last_valid_frame":3}}));sys.exit(2)'
        failure=error_code(self,'PHYSICS_NUMERICAL_INSTABILITY',lambda:physics._run_process(
            [sys.executable,'-I','-c',code],self.root,timeout=5,memory_bytes=1024**3,output_bytes=16*1024**2,label='numerical'))
        self.assertEqual(failure.report['last_valid_frame'],3)
        self.assertEqual(failure.report['category'],'numerical-instability')

    def test_native_scene_and_real_mesh_semantics_are_not_replaced_by_hashes(self):
        identity=dict(id='blender',adapter_version='1.0.0',solver_name='test-negative-only',solver_version='test',
            application_version='test',platform=sys.platform,precision='mixed',device='cpu',executable_sha256='0'*64)
        world=deepcopy(self.world)
        world['bodies'][0]['colliders'][0]['shape']['radius']=.6
        world['bodies'][0]['mass']['inertia']=[[.144,0,0],[0,.144,0],[0,0,.144]]
        with patch.object(self.backend,'_discover',return_value=identity):
            error_code(self,'PHYSICS_GEOMETRY',lambda:self.backend.prepare(world,self.payloads))
            other=deepcopy(self.world)
            other['scene'].update(path='project.json',sha256=hashlib.sha256(self.payloads['project.json']).hexdigest())
            error_code(self,'PHYSICS_SOURCE_IDENTITY',lambda:self.backend.prepare(other,self.payloads))
            bad=dict(self.payloads);bad['project.json']=b'{}'
            error_code(self,'PHYSICS_SOURCE_INVALID',lambda:self.backend.prepare(self.world,bad))

    def test_process_output_budget_and_global_os_worker_lock(self):
        error_code(self,'PHYSICS_RESOURCE_LIMIT',lambda:physics._run_process(
            [sys.executable,'-I','-c','import sys,time;sys.stdout.write("x"*2000000);sys.stdout.flush();time.sleep(1)'],self.root,
            timeout=5,memory_bytes=1024**3,output_bytes=65536,label='output'))
        with physics._slot():
            error_code(self,'PHYSICS_BUSY',lambda:physics._run_process(
                [sys.executable,'-I','-c','raise RuntimeError("must not launch")'],self.root,
                timeout=5,memory_bytes=1024**3,output_bytes=16*1024**2,label='locked'))
        self.assertFalse((self.root/'locked-stdout.log').exists())

    def test_fast_exit_worker_output_and_each_log_cap_are_enforced(self):
        sleep = time.sleep
        for name, code, cap, size in (
            ('total', 'open("payload.bin","wb").write(b"x"*524288)', 65536, 524288),
            ('stdout', 'import os;os.write(1,b"x"*9437184)', 32*1024**2, 9437184),
            ('stderr', 'import os;os.write(2,b"x"*9437184)', 32*1024**2, 9437184),
        ):
            with self.subTest(name=name):
                root=self.root/name;root.mkdir()
                # Let the real short-lived child finish within one polling
                # interval. The worker itself never sleeps, and no successful
                # process result or file-size check is mocked.
                with patch.object(physics.time,'sleep',side_effect=lambda _:sleep(.5)):
                    failure=error_code(self,'PHYSICS_RESOURCE_LIMIT',lambda:physics._run_process(
                        [sys.executable,'-I','-c',code],root,timeout=5,
                        memory_bytes=1024**3,output_bytes=cap))
                self.assertEqual(failure.report['process_exit_code'],0)
                payload=root/('payload.bin' if name=='total' else 'simulation-'+name+'.log')
                self.assertEqual(payload.stat().st_size,size)

    def test_owned_descendant_is_killed_even_when_leader_exits_on_term(self):
        def living(pid):
            try:
                value=Path('/proc',str(pid),'status').read_text()
                return not any(line.startswith('State:') and '\tZ' in line for line in value.splitlines())
            except FileNotFoundError:return False
        for mode in ('timeout','cancel'):
            folder=self.root/mode;folder.mkdir()
            child='import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);open("child.pid","w").write(str(os.getpid()));time.sleep(30)'
            parent='import subprocess,sys,time;subprocess.Popen([sys.executable,"-I","-c",'+repr(child)+']);time.sleep(30)'
            cancel=threading.Event() if mode=='cancel' else None
            timer=threading.Timer(1.,cancel.set) if cancel is not None else None
            if timer:timer.start()
            try:
                error_code(self,'PHYSICS_CANCELLED' if cancel is not None else 'PHYSICS_TIMEOUT',lambda:physics._run_process(
                    [sys.executable,'-I','-c',parent],folder,timeout=5 if cancel is not None else 1.,memory_bytes=1024**3,
                    output_bytes=16*1024**2,cancel_token=cancel))
                pid=int((folder/'child.pid').read_text())
                try:
                    deadline=time.monotonic()+1
                    while living(pid) and time.monotonic()<deadline:time.sleep(.025)
                    self.assertFalse(living(pid),'Owned descendant survived after its leader exited on TERM.')
                finally:
                    # If regression control fails, terminate only this test's
                    # recorded child, never an unrelated PID or desktop service.
                    if living(pid):os.kill(pid,signal.SIGKILL)
            finally:
                if timer:timer.cancel()

    def test_descriptor_reader_rejects_links_fifo_and_parent_traversal(self):
        (self.root/'regular').write_bytes(b'authentic')
        (self.root/'link').symlink_to(self.root/'regular')
        os.mkfifo(self.root/'pipe')
        with physics._Reader(self.root) as reader:
            self.assertEqual(reader.read('regular'),b'authentic')
            error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:reader.read('link'))
            error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:reader.read('pipe'))
            error_code(self,'PHYSICS_PATH',lambda:reader.read('../regular'))
            error_code(self,'PHYSICS_CACHE_CORRUPT',reader.names)

    def test_transient_blender_atomic_save_name_is_not_a_crash(self):
        # Genuine worker saves scene.blend@ then atomically renames it. A file
        # disappearing between walk and lstat must not fake a backend failure.
        temporary=self.root/'scene.blend@';temporary.write_bytes(b'x')
        original=Path.lstat
        def replaced(path,*args,**kwargs):
            if path==temporary:
                temporary.unlink(missing_ok=True)
                raise FileNotFoundError()
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',replaced):
            self.assertEqual(physics._tree_size(self.root),0)

    def test_stdlib_import_in_actual_empty_environment_and_optional_missing_result(self):
        root=self.root/'empty-python';venv.EnvBuilder(with_pip=False).create(root)
        interpreter=root/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        code='import sys;sys.path.insert(0,'+repr(str(Path(physics.__file__).parent))+');import physics_backends;assert not ({"numpy","PIL","bpy","pybullet","physics_contracts","scene3d_contracts"}&set(sys.modules))'
        process=subprocess.run([str(interpreter),'-I','-c',code],capture_output=True,text=True,timeout=20)
        self.assertEqual(process.returncode,0,process.stderr)
        report=physics.PyBulletBackend(interpreter).probe(smoke=True)
        self.assertEqual(report.state,'unavailable')
        self.assertFalse(report.headless_smoke_verified)
        self.assertEqual(report.evidence['code'],'PHYSICS_BACKEND_UNAVAILABLE')

    def test_missing_blender_and_distinct_capability_states(self):
        report=self.backend.probe(smoke=True)
        self.assertEqual(report.state,'unavailable')
        self.assertFalse(report.headless_smoke_verified)
        for code,state in [('PHYSICS_BACKEND_VERSION_UNSUPPORTED','unsupported'),('PHYSICS_BUSY','degraded'),
                           ('PHYSICS_TIMEOUT','failed-validation')]:
            self.assertEqual(physics._failure_state(code),state)


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Explicit real physics backend test gate is not enabled.')
class BlenderPhysicsLifecycleTests(unittest.TestCase):
    backend_class=physics.BlenderBackend

    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory(prefix='hatch-physics-real-')
        cls.root=Path(cls.directory.name)
        cls.backend=cls.backend_class()
        cls.world,cls.payloads=build_inputs(backend=cls.backend.backend_id)
        cls.prepared=cls.backend.prepare(cls.world,cls.payloads)
        cls.result=cls.backend.simulate(cls.prepared,verify_consumption=cls.backend.backend_id=='blender')
        cls.cache=cls.root/'cache'
        cls.backend.bake(cls.result,cls.cache)

    @classmethod
    def tearDownClass(cls):
        cls.backend.cleanup(cls.prepared)
        cls.directory.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hatch-physics-case-')
        self.path=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def copy_cache(self):
        destination=self.path/'cache';shutil.copytree(self.cache,destination);return destination

    def write_cache_file(self,cache,name,value):
        manifest=physics.parse((cache/'cache.json').read_bytes())
        data=physics.canonical(value);(cache/name).write_bytes(data)
        for file in manifest['files']:
            if file['path']==name:file.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        manifest['usage']['output_bytes']=sum(file['bytes'] for file in manifest['files'])
        (cache/'cache.json').write_bytes(physics.canonical(manifest))

    def test_valid_integral_float_manifest_lengths_are_normalized_before_io(self):
        cache=self.copy_cache()
        manifest=physics.parse((cache/'cache.json').read_bytes())
        for file in manifest['files']:file['bytes']=float(file['bytes'])
        manifest['sample_count']=49.0;manifest['usage']['solver_steps']=480.0
        (cache/'cache.json').write_bytes(physics.canonical(manifest))
        self.assertEqual(self.backend.verify(cache)['sample_count'],49)

    def test_malformed_receipt_request_and_report_objects_are_structured_failures(self):
        for number,name in enumerate(['verification.json','worker-job.json','simulation-report.json','worker-report.json']):
            for suffix,value in [('array',[]),('null',None),('text','invalid'),('unknown',{'unexpected':True})]:
                with self.subTest(file=name,value=suffix):
                    cache=self.path/(str(number)+'-'+suffix);shutil.copytree(self.cache,cache)
                    self.write_cache_file(cache,name,value)
                    error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:self.backend.verify(cache))

    def test_cache_backend_versions_cannot_contradict_execution_observations(self):
        cache=self.copy_cache()
        manifest=physics.parse((cache/'cache.json').read_bytes())
        manifest['backend'].update(solver_version='False solver99',application_version='False application99')
        (cache/'cache.json').write_bytes(physics.canonical(manifest))
        error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:self.backend.verify(cache))
        # Even changing the duplicate receipt header cannot override the actual
        # typed native/module observations retained by the execution report.
        receipt=physics.parse((cache/'verification.json').read_bytes())
        receipt['backend_identity']=deepcopy(manifest['backend'])
        self.write_cache_file(cache,'verification.json',receipt)
        error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:self.backend.verify(cache))

    def test_actual_solver_fall_contact_and_independent_numerical_oracle(self):
        initial=self.backend.inspect(self.cache,0)
        middle=self.backend.inspect(self.cache,12)
        final=self.backend.inspect(self.cache,48)
        height=lambda state:next(body for body in state['bodies'] if body['id']=='ball')['translation'][1]
        self.assertEqual(height(initial),3)
        self.assertAlmostEqual(height(middle),3-.5*9.81*.5**2,delta=.02)
        self.assertAlmostEqual(height(middle),3-9.81/240**2*(120*121)/2,delta=1e-4)
        self.assertAlmostEqual(height(final),.5,delta=.03)
        self.assertEqual(final['time'],dict(numerator=2,denominator=1))
        self.assertGreater(self.result.usage['peak_memory_bytes'],0)
        self.assertEqual(self.result.usage['solver_steps'],480)
        self.assertTrue(self.backend.verify(self.cache)['fixture'])

    def test_second_shape_profile_is_genuinely_executed(self):
        report=physics._additional_shape_smoke(self.backend)
        self.assertEqual((report['dynamic_shape'],report['static_shape']),('box','sphere'))
        self.assertAlmostEqual(report['actual_final_height'],.5,delta=.1)

    def test_basis_and_changed_gravity_are_actual_solver_controls(self):
        world=deepcopy(self.world);world['gravity']=[0,0,9.81]
        prepared=self.backend.prepare(world,self.payloads)
        try:
            result=self.backend.simulate(prepared)
            states=physics.parse((prepared.root/'states.json').read_bytes())
            last=states['samples'][-1]['bodies'][0]['translation']
            self.assertAlmostEqual(last[1],3,delta=1e-4)
            self.assertAlmostEqual(last[2],9.81/240**2*(480*481)/2,delta=.005)
            self.assertNotEqual(result.world_sha256,self.result.world_sha256)
        finally:self.backend.cleanup(prepared)

    def test_native_bake_consumption_is_distinct_from_solver_restart(self):
        if self.backend.backend_id=='blender':
            control=physics.parse((self.prepared.root/'native-consumption.json').read_bytes())
            self.assertTrue(control['consumption_demonstrated'])
            self.assertTrue(control['baked_flags']['is_baked'])
            self.assertFalse(control['after_free_bake_flags']['is_baked'])
            self.assertGreater(control['changed_gravity_displacement'],100)
            self.assertAlmostEqual(control['baked_pose_error'],0,delta=1e-5)
            # Never conceal/reset the pinned backend's flag; the source and
            # consumption control determine the bounded baked-read semantics.
            self.assertIs(type(control['baked_flags']['is_outdated']),bool)
            from PIL import Image
            with Image.open(self.cache/'preview.png') as image:
                self.assertEqual(image.size,(128,128));image.verify()
        else:
            contacts=physics.parse((self.cache/'contacts.json').read_bytes())
            self.assertTrue(any(frame['contacts'] for frame in contacts['samples']))
        self.assertIn('clean restart',self.backend.verify(self.cache)['native_continuation'])

    def test_complete_cache_inventory_hashes_staleness_and_numeric_corruption(self):
        cache=self.copy_cache()
        (cache/'extra.py').write_text('raise RuntimeError("must never execute")')
        error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:self.backend.verify(cache))
        (cache/'extra.py').unlink()
        error_code(self,'PHYSICS_CACHE_STALE',lambda:self.backend.verify(cache,expected_world_sha256='0'*64))
        payload=(cache/'states.json').read_bytes();(cache/'states.json').write_bytes(payload+b' ')
        error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:self.backend.inspect(cache,0))
        error_code(self,'PHYSICS_CACHE_SAMPLE',lambda:self.backend.inspect(self.cache,49))
        error_code(self,'PHYSICS_CACHE_SAMPLE',lambda:self.backend.inspect(self.cache,True))

    def test_cache_link_replacement_cannot_read_unrelated_target(self):
        cache=self.copy_cache();outside=self.path/'outside';outside.write_bytes(b'untouched')
        (cache/'states.json').unlink();(cache/'states.json').symlink_to(outside)
        error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:self.backend.verify(cache))
        self.assertEqual(outside.read_bytes(),b'untouched')

    def test_native_integral_float_source_preserves_original_bytes(self):
        from scene3d_contracts import all_refs
        world,payloads=build_inputs(backend=self.backend.backend_id)
        def floats(value):
            if type(value) is int:return float(value)
            if type(value) is list:return [floats(item) for item in value]
            if type(value) is dict:return {key:floats(item) for key,item in value.items()}
            return value
        documents={name:floats(json.loads(data)) for name,data in payloads.items() if name.endswith('.json')}
        digests={}
        def rehash(name):
            if name in digests:return digests[name]
            if name in documents:
                for pointer in all_refs(documents[name]):pointer['sha256']=rehash(pointer['path'])
                payloads[name]=(json.dumps(documents[name],sort_keys=True,separators=(',',':'))+'\n').encode()
            digests[name]=hashlib.sha256(payloads[name]).hexdigest()
            return digests[name]
        rehash('project.json')
        world['scene']['sha256']=digests[world['scene']['path']]
        before=deepcopy(payloads)
        self.assertIn(b'"schema_version":1.0',payloads['project.json'])
        prepared=self.backend.prepare(world,payloads)
        try:
            for name,data in before.items():self.assertEqual((prepared.root/'inputs'/name).read_bytes(),data)
            result=self.backend.simulate(prepared)
            states=physics.parse((prepared.root/'states.json').read_bytes())
            self.assertEqual(len(states['samples']),49)
            self.assertAlmostEqual(states['samples'][-1]['bodies'][0]['translation'][1],.5,delta=.03)
            self.assertEqual(payloads,before)
        finally:self.backend.cleanup(prepared)

    def test_native_cache_artifact_format_is_checked_after_rehash(self):
        cache=self.copy_cache()
        if self.backend.backend_id=='blender':
            data=b'not a valid Blend container'
            name='scene.blend'
        else:
            data=physics.canonical([])
            name='contacts.json'
        manifest=physics.parse((cache/'cache.json').read_bytes())
        (cache/name).write_bytes(data)
        for file in manifest['files']:
            if file['path']==name:file.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        manifest['usage']['output_bytes']=sum(file['bytes'] for file in manifest['files'])
        (cache/'cache.json').write_bytes(physics.canonical(manifest))
        error_code(self,'PHYSICS_CACHE_CORRUPT',lambda:self.backend.verify(cache))

    def test_fixture_origin_cannot_be_relabelled_by_editing_cache_flags(self):
        cache=self.copy_cache()
        manifest=physics.parse((cache/'cache.json').read_bytes())
        value=physics.parse((cache/'states.json').read_bytes());value['fixture']=False
        data=physics.canonical(value);(cache/'states.json').write_bytes(data)
        manifest['fixture']=False
        for file in manifest['files']:
            if file['path']=='states.json':file.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
        manifest['usage']['output_bytes']=sum(file['bytes'] for file in manifest['files'])
        (cache/'cache.json').write_bytes(physics.canonical(manifest))
        error_code(self,'PHYSICS_PROVENANCE',lambda:self.backend.verify(cache))

    def test_immutable_result_ownership_and_exclusive_destination_races(self):
        modified=replace(self.result,usage={**self.result.usage,'solver_steps':0})
        error_code(self,'PHYSICS_OWNERSHIP',lambda:self.backend.bake(modified,self.path/'forged'))
        import atomic_publish
        original=atomic_publish._rename_no_replace
        target=self.path/'racing'
        unrelated=self.path/'.unrelated-stage';unrelated.mkdir();(unrelated/'sentinel').write_bytes(b'preserve')
        def racing(stage,destination):
            Path(destination).mkdir()
            return original(stage,destination)
        with patch.object(atomic_publish,'_rename_no_replace',racing):
            error_code(self,'PHYSICS_DESTINATION_EXISTS',lambda:self.backend.bake(self.result,target))
        self.assertTrue(target.is_dir());self.assertEqual(list(target.iterdir()),[])
        self.assertEqual((unrelated/'sentinel').read_bytes(),b'preserve')
        self.assertFalse(any(self.path.glob('.racing.physics-*')))
        link=self.path/'link';absent=self.path/'absent';link.symlink_to(absent,target_is_directory=True)
        error_code(self,'PHYSICS_DESTINATION_EXISTS',lambda:self.backend.bake(self.result,link))
        self.assertTrue(link.is_symlink());self.assertFalse(absent.exists())

    def test_export_contains_exact_solver_samples_and_rejects_unimplemented_formats(self):
        output=self.path/'animation'
        report=self.backend.export_animation(self.cache,output)
        self.assertEqual(report['sample_count'],49)
        value=physics.parse((output/'transforms.json').read_bytes())
        source=physics.parse((self.cache/'states.json').read_bytes())
        self.assertEqual(value['samples'],source['samples'])
        self.assertEqual(value['review_status'],'unreviewed')
        self.assertTrue(value['fixture'])
        error_code(self,'PHYSICS_EXPORT_UNSUPPORTED',lambda:self.backend.export_animation(self.cache,self.path/'glb',format='glb'))

    def test_changed_prepared_worker_job_or_source_cannot_execute(self):
        prepared=self.backend.prepare(self.world,self.payloads)
        try:
            value=physics.parse((prepared.root/'worker-job.json').read_bytes())
            value['meshes']['ball']['vertices'][0][0]+=1
            (prepared.root/'worker-job.json').write_bytes(physics.canonical(value))
            error_code(self,'PHYSICS_INPUT_CHANGED',lambda:self.backend.simulate(prepared))
        finally:self.backend.cleanup(prepared)
        prepared=self.backend.prepare(self.world,self.payloads)
        try:
            prepared.input_inventory.clear()
            error_code(self,'PHYSICS_OWNERSHIP',lambda:self.backend.simulate(prepared))
            # Restore only the caller-visible record for explicit owned cleanup.
            prepared.input_inventory.update(self.backend._owned[prepared.token]['inventory'])
        finally:self.backend.cleanup(prepared)

    def test_missing_outputs_are_failure_even_if_process_reports_success(self):
        prepared=self.backend.prepare(self.world,self.payloads)
        try:
            identity=deepcopy(self.backend._owned[prepared.token]['identity'])
            with patch.object(self.backend,'_discover',return_value=identity),patch.object(physics,'_run_process',return_value=dict(elapsed_seconds=.1,peak_memory_bytes=0)):
                error_code(self,'PHYSICS_OUTPUT_MISSING',lambda:self.backend.simulate(prepared))
            self.assertFalse((prepared.root/'cache.json').exists())
        finally:self.backend.cleanup(prepared)


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Explicit real physics backend test gate is not enabled.')
class PyBulletPhysicsLifecycleTests(BlenderPhysicsLifecycleTests):
    @classmethod
    def setUpClass(cls):
        path=os.environ.get('HATCH_PYBULLET_PYTHON')
        if not path:
            raise unittest.SkipTest('Optional PyBullet interpreter not configured; no readiness claimed.')
        cls.backend_class=lambda:physics.PyBulletBackend(path)
        super().setUpClass()


if __name__=='__main__':unittest.main()
