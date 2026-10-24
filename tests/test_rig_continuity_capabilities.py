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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from continuity_capabilities import Admission, Policy, MATRIX, schemas, validate, discover
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
        self.assertEqual(result['outcome'],'supported_fallback')
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
        self.assertEqual(result['checks'][0]['outcome'],'supported_fallback')
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
        self.assertIn('SOURCE',result['checks'][0]['error'])

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
        self.assertEqual(result['checks'][0]['outcome'],'supported_fallback')
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
