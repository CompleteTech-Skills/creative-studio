"""Real owner probes and adversarial capability/fallback boundaries."""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from continuity_capabilities import Admission, Policy, MATRIX, MANDATORY_FIELDS, OwnerAdapter, schemas, validate, discover
from continuity_capability_owners import RigOwner, ImageMastersOwner, pointer
from continuity_contracts import VERSION, canonical_bytes, digest
from rig_errors import RigError

REPO = Path(__file__).resolve().parents[1]


class CapabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'evidence').mkdir()
        (self.root/'project.json').write_bytes(b'{"fixture":true}\n')
        self.project = pointer(self.root, self.root/'project.json', kind='project-revision')

    def tearDown(self): self.temp.cleanup()

    def request(self, *caps):
        return dict(schema_version=VERSION, kind='continuity-capability-request', request_id='fixture-request',
            project_revision=self.project, checks=[dict(id='check-'+str(i), capability=cap, schema_version=VERSION,
                inputs=[], input_types=[], output_types=[], resources=dict(memory_bytes=128*1024*1024,
                disk_bytes=8*1024*1024, cpu_seconds=30, gpu_bytes=0, concurrency=1), required_fields=[],
                allow_fallback=True, gpu_required=False, needs_visual_approval=False) for i,cap in enumerate(caps)])

    def rig(self, cap):
        if not (self.root/'rig').exists(): shutil.copytree(REPO/'examples/rig-3d/robotic', self.root/'rig')
        return RigOwner(self.root, 'rig/rig.json', 'evidence', cap)

    def engine(self, *owners, **policy):
        return Admission(self.root, policy=Policy(allowed_owners=frozenset(['hatch-rig','hatch-image-masters']),
            asset_rights=True, max_cpu_seconds=4096, max_disk_bytes=2**32, **policy), owners=owners)

    def bound(self, cap):
        owner = self.rig(cap); request = self.request(cap)
        request['checks'][0].update(inputs=[owner.bound], input_types=['hatch-3d-rig'], output_types=['native-inspection'])
        return owner, request

    def test_discovery_is_not_execution_or_approval(self):
        result = discover()
        self.assertEqual(len(result['capabilities']), 13)
        self.assertTrue(all(v['status']=='untested' for v in result['capabilities']))
        result = self.engine().run(self.request(*MATRIX))
        self.assertEqual(result['outcome'],'blocked_missing_capability')
        self.assertFalse(result['visual_approval'])
        self.assertEqual(len(result['checks']),13)

    def test_actual_rig_geometry_probe_and_source_preservation(self):
        owner, request = self.bound('geometry-inspection')
        before={p.relative_to(self.root/'rig').as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.root/'rig').rglob('*') if p.is_file()}
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'partial')
        self.assertIn('source-hierarchy',result['checks'][0]['missing_fields'])
        report=result['checks'][0]['measurements']['geometry']
        self.assertGreater(report['vertices'],0)
        self.assertGreater(report['triangles'],0)
        after={p.relative_to(self.root/'rig').as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.root/'rig').rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_actual_pose_bind_and_skin_data(self):
        for cap in ('scene-animation-evaluation','rig-compatibility'):
            owner, request=self.bound(cap)
            result=self.engine(owner).run(request)
            self.assertEqual(result['outcome'],'partial',result)
            m=result['checks'][0]['measurements']
            self.assertTrue(m['world_matrices']);self.assertTrue(m['deformation']);self.assertTrue(m['skin_bindings'])

    def test_actual_cpu_reference_does_not_grant_visual_approval(self):
        owner, request=self.bound('visual-reference')
        request['checks'][0]['needs_visual_approval']=True
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'needs_visual_review')
        self.assertIn('visual-approval',result['checks'][0]['missing_fields'])
        from PIL import Image
        image=next(x for x in result['checks'][0]['evidence'] if x['path'].endswith('.png'))
        with Image.open(self.root/image['path']) as pic:
            self.assertEqual(pic.size,(64,64));self.assertGreater(len(set(pic.getdata())),10)
        self.assertFalse(result['visual_approval'])

    def test_gpu_required_cannot_fallback_to_cpu(self):
        owner, request=self.bound('gpu-rendering');request['checks'][0]['gpu_required']=True
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed');self.assertIn('gpu-required',result['checks'][0]['unsatisfied_gates'])

    def test_gpu_cpu_fallback_is_actual_cpu(self):
        owner, request=self.bound('gpu-rendering')
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'supported_fallback')
        self.assertEqual(result['checks'][0]['measurements']['device'],'cpu')

    def test_missing_backend_does_not_suppress_independent_geometry(self):
        owner, request=self.bound('geometry-inspection')
        request['checks']+=self.request('blender-native-inspection','physics-continuation','graphical-editor')['checks']
        for i,c in enumerate(request['checks']):c['id']='check-'+str(i)
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'partial')
        self.assertEqual(result['checks'][0]['outcome'],'partial')
        self.assertTrue(all(x['outcome']=='blocked_missing_capability' for x in result['checks'][1:]))

    def test_missing_encoder_preserves_actual_masters_not_fake_video(self):
        from PIL import Image
        Image.new('RGB',(8,8),'red').save(self.root/'one.png')
        before=(self.root/'one.png').read_bytes()
        owner=ImageMastersOwner(self.root,['one.png'],'evidence')
        request=self.request('video-encoding');request['checks'][0].update(inputs=owner.images,
            input_types=['image-master'],output_types=['verified-image-masters'],required_fields=['silent-video-export'])
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'partial')
        self.assertIn('silent-video-export',result['checks'][0]['missing_fields'])
        self.assertFalse(result['checks'][0]['measurements']['video_exported'])
        self.assertEqual((self.root/'one.png').read_bytes(),before)
        self.assertFalse(list(self.root.glob('*.mp4')))

    def test_invalid_source_hash_blocks_all_checks_even_without_backend(self):
        request=self.request('historical-media','visual-reference')
        request['project_revision']['sha256']='0'*64
        result=self.engine().run(request)
        self.assertEqual(result['outcome'],'failed');self.assertEqual(result['integrity'],'failed')
        self.assertTrue(all(x['unsatisfied_gates']==['integrity'] for x in result['checks']))

    def test_missing_canonical_file_is_integrity_failure(self):
        request=self.request('physics-continuation');(self.root/'project.json').unlink()
        result=self.engine().run(request)
        self.assertEqual(result['outcome'],'failed');self.assertEqual(result['integrity'],'failed')

    def test_symlink_source_never_waived(self):
        try:(self.root/'alias.json').symlink_to(self.root/'project.json')
        except OSError:self.skipTest('host symlink creation unavailable')
        request=self.request('historical-media');request['project_revision']['path']='alias.json'
        self.assertEqual(self.engine().run(request)['outcome'],'failed')

    def test_cancellation_remains_explicit(self):
        result=self.engine().run(self.request('physics-continuation'),canceled=lambda:True)
        self.assertEqual(result['outcome'],'canceled')

    def test_fallback_disallowed_blocks(self):
        owner, request=self.bound('geometry-inspection');request['checks'][0]['allow_fallback']=False
        self.assertEqual(self.engine(owner).run(request)['outcome'],'blocked_missing_capability')

    def test_permissions_and_resource_policy_not_request_authority(self):
        owner, request=self.bound('geometry-inspection')
        for policy,gate in ((Policy(),'permission'),(Policy(asset_rights=True,allowed_owners=frozenset(['hatch-rig']),
                max_memory_bytes=1),'resource-memory_bytes')):
            result=Admission(self.root,policy=policy,owners=[owner]).run(request)
            self.assertEqual(result['outcome'],'failed');self.assertIn(gate,result['checks'][0]['unsatisfied_gates'])

    def test_schema_or_type_mismatch_never_admitted(self):
        owner, request=self.bound('geometry-inspection')
        for k,v in (('schema_version','99.0.0'),('input_types',['painted-2d-image']),('output_types',['approved-artwork'])):
            altered=copy.deepcopy(request);altered['checks'][0][k]=v
            self.assertEqual(self.engine(owner).run(altered)['outcome'],'blocked_missing_capability')

    def test_source_bound_probe_cannot_run_on_different_rig(self):
        owner, request=self.bound('geometry-inspection');request['checks'][0]['inputs']=[]
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('canonical-reference',result['checks'][0]['unsatisfied_gates'])

    def test_expired_probe_is_not_current_capability(self):
        owner, request=self.bound('geometry-inspection')
        future=datetime.now(timezone.utc)+timedelta(days=1)
        self.assertEqual(self.engine(owner).run(request,now=future)['outcome'],'blocked_missing_capability')

    def test_integer_float_parity_copy_and_boundaries(self):
        request=self.request('geometry-inspection');original=copy.deepcopy(request)
        for k in request['checks'][0]['resources']:request['checks'][0]['resources'][k]=float(request['checks'][0]['resources'][k])
        normalized=validate(request,'request')
        self.assertTrue(all(type(v) is int for v in normalized['checks'][0]['resources'].values()))
        self.assertTrue(all(type(v) is float for v in request['checks'][0]['resources'].values()))
        for invalid in (True,.25,float('inf'),float('nan'),2**54,-1):
            altered=copy.deepcopy(original);altered['checks'][0]['resources']['concurrency']=invalid
            with self.assertRaises((ValueError,RigError)):validate(altered,'request')

    def test_unknown_fields_duplicate_checks_and_paths_rejected(self):
        request=self.request('geometry-inspection')
        for change in (lambda r:r.update(command='sh'),lambda r:r['checks'].append(copy.deepcopy(r['checks'][0])),
                       lambda r:r['project_revision'].update(path='../secret')):
            value=copy.deepcopy(request);change(value)
            with self.assertRaises(ValueError):validate(value,'request')

    def test_schemas_match_runtime(self):
        from jsonschema import Draft202012Validator
        value=self.request(*MATRIX)
        Draft202012Validator(schemas()['request']).validate(value)
        self.assertEqual(validate(value,'request'),value)
        result=self.engine().run(value)
        Draft202012Validator(schemas()['result']).validate(result)
        self.assertEqual(result['report_sha256'],digest({k:v for k,v in result.items() if k!='report_sha256'}))


    def test_aggregate_budget_is_not_reset_per_check(self):
        request=self.request('geometry-inspection','visual-reference','physics-continuation')
        with self.assertRaisesRegex(ValueError,'aggregate'):
            Admission(self.root,policy=Policy()).run(request)

    def test_failed_owner_does_not_suppress_independent_check(self):
        owner, request=self.bound('geometry-inspection')
        request['checks'].append(self.request('visual-reference')['checks'][0])
        request['checks'][1]['id']='visual'
        class Broken(type(owner)):
            def snapshot(self): raise ValueError('broken owner schema')
        broken=object.__new__(Broken)
        result=self.engine(broken,owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(len(result['checks']),2)
        self.assertEqual(result['checks'][0]['outcome'],'partial')
        self.assertEqual(result['checks'][1]['unsatisfied_gates'],['owner-contract'])

    def test_whole_process_resource_measurement_is_explicit(self):
        owner, request=self.bound('geometry-inspection')
        result=self.engine(owner).run(request)
        m=result['checks'][0]['measurements']['admission_resources']
        self.assertGreaterEqual(m['process_cpu_seconds'],0)
        self.assertIn(m['rss_status'],('measured','unavailable'))

    def test_schema_files_are_current(self):
        for kind,schema in schemas().items():
            actual=json.loads((REPO/'profiles'/('continuity-capability-'+kind+'.schema.json')).read_text())
            self.assertEqual(schema,actual)


    def test_actual_owner_disk_budget_cannot_be_waived(self):
        owner, request=self.bound('visual-reference')
        request['checks'][0]['resources']['disk_bytes']=1
        before=set((self.root/'evidence').iterdir())
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('disk reservation',result['checks'][0]['error'])
        self.assertEqual(before,set((self.root/'evidence').iterdir()))

    def test_network_policy_is_never_a_fallback(self):
        owner, request=self.bound('geometry-inspection')
        owner._snap['offline']=False
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('network-policy',result['checks'][0]['unsatisfied_gates'])

    def test_visual_comparison_cannot_clear_visual_gate_by_omission(self):
        request=self.request('visual-comparison')
        result=self.engine().run(request)
        self.assertIn('visual-approval',result['checks'][0]['missing_fields'])

    def test_native_lock_collision_is_not_missing_backend(self):
        from rig_errors import job_lock
        owner, request=self.bound('geometry-inspection')
        with job_lock(self.root/'rig'):
            result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('owns this rig',result['checks'][0]['error'])


    def policy_owner(self, capability, *, malformed=None, execute=None, expiry=None):
        # This explicitly synthetic in-process adapter tests policy invariants,
        # not real production backend or artistic acceptance. Real native adapter
        # coverage is tested separately above.
        test=self
        if not (self.root/'rig').exists():shutil.copytree(REPO/'examples/rig-3d/robotic',self.root/'rig')
        native=pointer(self.root,self.root/'rig/rig.json',kind='hatch-3d-rig')
        class Fixture(OwnerAdapter):
            def __init__(self):self.lock=threading.Lock();self.calls=0
            def snapshot(self):
                return dict(schema_version=VERSION,kind='continuity-capability-snapshot',
                    capability_id=capability,implementation_id=MATRIX[capability][0][0],
                    owner='hatch-rig',backend='policy-fixture',backend_version='1.0.0',contract_version=VERSION,
                    input_types=['hatch-3d-rig'],output_types=['native-inspection'],operations=[capability],
                    status='available',discovered=True,tested_operations=[capability],evidence=[test.project],
                    limits=dict(memory_bytes=512*1024*1024,disk_bytes=64*1024*1024,cpu_seconds=60,gpu_bytes=0,concurrency=1),
                    offline=True,asset_rights_required=True,
                    expires_at=expiry or '2099-01-01T00:00:00Z',limitations=[])
            @contextmanager
            def reserve(self,check):
                with self.lock:yield
            def execute(self,check):
                self.calls+=1
                payload=(test.root/'project.json').read_bytes()
                result=dict(outcome='executed',evidence=[test.project],
                    measurements=dict(fixture=True,source_sha256=hashlib.sha256(payload).hexdigest(),model_invoked=False,device='cpu'),
                    verified_fields=list(MANDATORY_FIELDS[capability])+['source-conversion-binding'],limitations=[])
                if malformed:result.update(malformed)
                if execute:execute(result)
                return result
        return Fixture(),native

    def test_all_thirteen_categorical_matrix_invariants(self):
        for cap in MATRIX:
            with self.subTest(cap=cap):
                owner,native=self.policy_owner(cap)
                request=self.request(cap);request['checks'][0].update(inputs=[native],input_types=['hatch-3d-rig'],output_types=['native-inspection'])
                result=self.engine(owner).run(request)
                check=result['checks'][0]
                self.assertEqual(owner.calls,1)
                self.assertFalse(result['visual_approval'])
                if cap in ('graphical-editor','video-encoding','historical-media'):
                    self.assertEqual(check['outcome'],'partial')
                    self.assertTrue(check['missing_fields'])
                elif cap=='visual-comparison':self.assertEqual(check['outcome'],'needs_visual_review')
                else:self.assertEqual(check['outcome'],'supported_fallback',check)

    def test_wrong_native_filename_is_rejected_before_probe(self):
        self.rig('geometry-inspection')
        with self.assertRaisesRegex(ValueError,'rig.json'):
            RigOwner(self.root,'rig/identity.json','evidence','geometry-inspection')

    def test_project_change_is_global_integrity_failure(self):
        owner,request=self.bound('geometry-inspection')
        changed=False
        def change():
            nonlocal changed
            if not changed:
                (self.root/'project.json').write_bytes(b'{"changed":true}')
                changed=True
            return False
        result=self.engine(owner).run(request,canceled=change)
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['integrity'],'failed')
        self.assertIn('integrity',result['checks'][0]['unsatisfied_gates'])

    def test_malformed_owner_responses_keep_complete_structured_report(self):
        for malformed in (dict(limitations=[None]),dict(evidence=[None]),dict(measurements=['wrong']),
                          dict(verified_fields='wrong'),dict(limitations=['x'*4097])):
            owner,native=self.policy_owner('geometry-inspection',malformed=malformed)
            request=self.request('geometry-inspection','physics-continuation')
            request['checks'][0].update(inputs=[native],input_types=['hatch-3d-rig'],output_types=['native-inspection'])
            result=self.engine(owner).run(request)
            self.assertEqual(result['outcome'],'failed')
            self.assertEqual(len(result['checks']),2)
            self.assertEqual(result['checks'][1]['outcome'],'blocked_missing_capability')

    def test_oversized_native_and_combined_reports_are_bounded_failures(self):
        # Each individual response is valid JSON; size limits apply before final
        # report serialization, preserving all check outcomes.
        owner,native=self.policy_owner('geometry-inspection',malformed=dict(measurements={'large':'x'*1048577}))
        request=self.request('geometry-inspection','physics-continuation');request['checks'][0]['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed');self.assertEqual(len(result['checks']),2)
        owner,native=self.policy_owner('geometry-inspection',malformed=dict(measurements={'large':'x'*700000}))
        request=self.request(*(['geometry-inspection']*8))
        for check in request['checks']:check['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed');self.assertEqual(len(result['checks']),8)
        self.assertLess(len(canonical_bytes(result)),8*1024*1024)

    def test_batch_and_reservation_expiry_are_current(self):
        current=[datetime(2026,10,24,tzinfo=timezone.utc)]
        def advance(result):current[0]+=timedelta(seconds=2)
        owner,native=self.policy_owner('geometry-inspection',execute=advance,expiry='2026-10-24T00:00:01Z')
        request=self.request('geometry-inspection','geometry-inspection')
        for check in request['checks']:check['inputs']=[native]
        engine=self.engine(owner);engine.clock=lambda:current[0]
        result=engine.run(request)
        self.assertEqual(owner.calls,1)
        self.assertEqual(result['checks'][1]['outcome'],'blocked_missing_capability')

    def test_used_snapshot_and_trusted_policy_are_retained(self):
        owner,request=self.bound('geometry-inspection')
        result=self.engine(owner).run(request)
        record=result['checks'][0]
        self.assertEqual(digest(record['snapshot']),record['snapshot_sha256'])
        self.assertEqual(record['snapshot']['expires_at'],owner.snapshot()['expires_at'])
        self.assertEqual(result['host_policy']['allowed_owners'],['hatch-image-masters','hatch-rig'])

    def test_unavailable_snapshot_can_have_unknown_version(self):
        owner,native=self.policy_owner('geometry-inspection')
        snapshot=owner.snapshot();snapshot.update(status='unavailable',backend_version=None,tested_operations=[],evidence=[])
        self.assertIsNone(validate(snapshot,'snapshot')['backend_version'])
        snapshot['status']='available'
        with self.assertRaises(ValueError):validate(snapshot,'snapshot')

    def test_painted_input_cannot_replace_geometry_or_3d_reference(self):
        owner,native=self.policy_owner('visual-reference')
        request=self.request('visual-reference');request['checks'][0]['inputs']=[self.project]
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('canonical-reference',result['checks'][0]['unsatisfied_gates'])
        self.assertEqual(owner.calls,0)

    def test_deterministic_fallback_cannot_claim_llm_execution(self):
        owner,native=self.policy_owner('ai-interpretation',malformed=dict(measurements={'model_invoked':True}))
        request=self.request('ai-interpretation')
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('no model executed',result['checks'][0]['error'])


    def test_opaque_measurement_maps_are_not_reference_objects(self):
        metadata=dict(id='opaque',version='not-a-version',path='../opaque-user-string',sha256='not-a-hash',kind='note')
        owner,native=self.policy_owner('geometry-inspection',malformed=dict(measurements={'metadata':metadata}))
        request=self.request('geometry-inspection');request['checks'][0]['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(result['checks'][0]['outcome'],'supported_fallback')
        self.assertEqual(result['checks'][0]['measurements']['metadata'],metadata)

    def test_nullable_snapshot_integer_fields_normalize_on_copy(self):
        owner,request=self.bound('geometry-inspection')
        result=self.engine(owner).run(request)
        result['checks'][0]['snapshot']['limits']['memory_bytes']=float(result['checks'][0]['snapshot']['limits']['memory_bytes'])
        parsed=validate(result,'result')
        self.assertIs(type(parsed['checks'][0]['snapshot']['limits']['memory_bytes']),int)
        self.assertIs(type(result['checks'][0]['snapshot']['limits']['memory_bytes']),float)


    def test_owner_required_review_records_missing_field_and_gate(self):
        owner,native=self.policy_owner('visual-reference',malformed=dict(outcome='needs_visual_review'))
        request=self.request('visual-reference');request['checks'][0]['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'needs_visual_review')
        self.assertIn('visual-approval',result['checks'][0]['missing_fields'])
        self.assertIn('visual-approval',result['checks'][0]['unsatisfied_gates'])

    def test_duplicate_probe_and_input_hashing_is_linear_in_unique_bytes(self):
        import continuity_capabilities as module
        original=module.verify_references
        records=[]
        def counted(*args,**kwargs):
            start=time.process_time()
            result=original(*args,**kwargs)
            records.append(dict(seconds=time.process_time()-start,bytes=result['total_bytes']))
            return result
        owner,native=self.policy_owner('geometry-inspection')
        request=self.request(*(['geometry-inspection']*16))
        for check in request['checks']:check['inputs']=[native]
        with patch.object(module,'verify_references',counted):result=self.engine(owner).run(request)
        self.assertEqual(len(records),2,records)  # initial unique closure and final fresh closure
        self.assertEqual(sum(r['bytes'] for r in records),2*((self.root/'project.json').stat().st_size+(self.root/'rig/rig.json').stat().st_size))
        self.assertGreaterEqual(result['admission_resources']['process_cpu_seconds'],sum(r['seconds'] for r in records))
        self.assertEqual(len(result['checks']),16)

    def test_budget_includes_validation_and_hashing_not_only_owner_calls(self):
        import continuity_capabilities as module
        owner,native=self.policy_owner('geometry-inspection')
        request=self.request('geometry-inspection');request['checks'][0].update(inputs=[native])
        request['checks'][0]['resources']['cpu_seconds']=1
        original=module.verify_references
        def expensive(*args,**kwargs):
            result=original(*args,**kwargs)
            until=time.process_time()+.55
            while time.process_time()<until:pass
            return result
        with patch.object(module,'verify_references',expensive):result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('resource-admission',result['checks'][0]['unsatisfied_gates'])
        self.assertGreater(result['admission_resources']['process_cpu_seconds'],1)

    def test_final_hash_pass_detects_changes_despite_unreliable_fingerprint(self):
        import continuity_capabilities as module
        owner,native=self.policy_owner('geometry-inspection')
        request=self.request('geometry-inspection');request['checks'][0]['inputs']=[native]
        def change(result):(self.root/'project.json').write_bytes(b'{"changed":true}')
        owner.execute=lambda check:(change({}) or dict(outcome='executed',evidence=[],measurements={},verified_fields=[],limitations=[]))
        # Simulate a platform reporting an unchanged metadata fingerprint. Final
        # hash verification must still catch the changed canonical project bytes.
        with patch.object(module.ReferencePass,'_fingerprint',return_value=(1,2,3,4,5)):
            result=self.engine(owner).run(request)
        self.assertEqual(result['integrity'],'failed')
        self.assertEqual(result['outcome'],'failed')


    def test_exhausted_probe_or_reservation_never_starts_native_execution(self):
        import continuity_capabilities as module
        for stage in ('probe','reserve'):
            owner,native=self.policy_owner('geometry-inspection')
            request=self.request('geometry-inspection');request['checks'][0]['inputs']=[native]
            request['checks'][0]['resources']['cpu_seconds']=1
            fake_cpu=[0.]
            if stage=='probe':
                (self.root/'new-probe.json').write_bytes(b'{"fixture":true}')
                proof=pointer(self.root,self.root/'new-probe.json')
                original_snapshot=owner.snapshot
                def snapshot():
                    value=original_snapshot();value['evidence']=[proof];return value
                owner.snapshot=snapshot
                original_verify=module.verify_references
                def verify(*args,**kwargs):
                    result=original_verify(*args,**kwargs)
                    if any(x['path']=='new-probe.json' for x in result['files']):fake_cpu[0]=2.
                    return result
                context=patch.object(module,'verify_references',verify)
            else:
                original_reserve=owner.reserve
                @contextmanager
                def reserve(check):
                    with original_reserve(check):
                        fake_cpu[0]=2.
                        yield
                owner.reserve=reserve
                context=patch.object(module,'_message',module._message)
            with context,patch.object(module.time,'process_time',side_effect=lambda:fake_cpu[0]):
                result=self.engine(owner).run(request)
            self.assertEqual(owner.calls,0,stage)
            self.assertEqual(result['outcome'],'failed')
            self.assertIn('resource-admission',result['checks'][0]['unsatisfied_gates'])

    def test_imports_do_not_import_optional_engines(self):
        # Import at module scope is stdlib + shared JSON safety only. Static
        # AST validates this boundary without spawning while native slot held.
        import ast
        for name in ('continuity_capabilities.py','continuity_capability_owners.py'):
            tree=ast.parse((REPO/'scripts'/name).read_text())
            top=[n for n in tree.body if isinstance(n,(ast.Import,ast.ImportFrom))]
            imports=[n.module if isinstance(n,ast.ImportFrom) else a.name for n in top
                     for a in (n.names if isinstance(n,ast.Import) else [None])]
            self.assertFalse(any(x in ('numpy','PIL','rig_models','rig_api') for x in imports))
