"""Offline continuity capability and fallback contracts."""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from continuity_capabilities import Admission, CapabilityRefusal, Policy, MATRIX, schemas, validate, discover
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
                inputs=[], input_types=[], output_types=[], resources=dict(memory_bytes=256*1024*1024,
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

    def test_superseded_project_revision_fails_integrity(self):
        request = self.request('historical-media', 'visual-reference')
        (self.root / 'project.json').write_bytes(b'{"fixture":true,"revision":2}\n')
        result = self.engine().run(request)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['integrity'], 'failed')
        self.assertEqual([item['unsatisfied_gates'] for item in result['checks']],
                         [['integrity'], ['integrity']])

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

    def test_schemas_match_runtime(self):
        from jsonschema import Draft202012Validator
        value=self.request(*MATRIX)
        Draft202012Validator(schemas()['request']).validate(value)
        self.assertEqual(validate(value,'request'),value)
        result=self.engine().run(value)
        Draft202012Validator(schemas()['result']).validate(result)
        self.assertEqual(result['report_sha256'],digest({k:v for k,v in result.items() if k!='report_sha256'}))

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

    def test_visual_comparison_cannot_clear_visual_gate_by_omission(self):
        request=self.request('visual-comparison')
        result=self.engine().run(request)
        self.assertIn('visual-approval',result['checks'][0]['missing_fields'])

    def test_used_snapshot_and_trusted_policy_are_retained(self):
        owner,request=self.bound('geometry-inspection')
        result=self.engine(owner).run(request)
        record=result['checks'][0]
        self.assertEqual(digest(record['snapshot']),record['snapshot_sha256'])
        self.assertEqual(record['snapshot']['expires_at'],owner.snapshot()['expires_at'])
        self.assertEqual(result['host_policy']['allowed_owners'],['hatch-image-masters','hatch-rig'])

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


    def test_native_load_uses_bound_manifest_digest_under_owner_lock(self):
        from contextlib import contextmanager
        from rig_errors import job_lock
        from rig_models import load_rig

        owner, request = self.bound('geometry-inspection')
        lock_held = False

        @contextmanager
        def track_lock(root):
            nonlocal lock_held
            with job_lock(root):
                lock_held = True
                try:
                    yield
                finally:
                    lock_held = False

        def assert_bound_load(root, *, limits, expected_manifest_sha256):
            self.assertTrue(lock_held)
            self.assertEqual(root, self.root / 'rig')
            self.assertEqual(expected_manifest_sha256, owner.bound['sha256'])
            return load_rig(root, limits=limits, expected_manifest_sha256=expected_manifest_sha256)

        with patch('rig_errors.job_lock', side_effect=track_lock), patch(
            'rig_models.load_rig', side_effect=assert_bound_load,
        ) as native_load:
            result = self.engine(owner).run(request)
        self.assertEqual(result['outcome'], 'partial', result)
        self.assertEqual(native_load.call_count, 2)


    def image_owner_and_request(self):
        from PIL import Image

        Image.new('RGB', (8, 8), 'red').save(self.root / 'master.png')
        owner = ImageMastersOwner(self.root, ['master.png'], 'evidence')
        request = self.request('video-encoding')
        request['checks'][0].update(inputs=owner.images, input_types=['image-master'],
                                    output_types=['verified-image-masters'])
        return owner, request

    def test_exposed_owner_bindings_are_read_only_copies(self):
        from PIL import Image

        rig, rig_request = self.bound('geometry-inspection')
        baseline = rig.bound
        alternate = self.root / 'other-rig.json'
        shutil.copyfile(self.root / rig.rig_path, alternate)
        rig_request['checks'][0]['inputs'][0].update(pointer(self.root, alternate, kind='hatch-3d-rig'))
        self.assertEqual(rig.bound, baseline)
        self.assertEqual(self.engine(rig).run(rig_request)['checks'][0]['unsatisfied_gates'],
                         ['canonical-reference'])
        for field in ('bound', 'root', 'rig_path', 'capability'):
            with self.subTest(field=field), self.assertRaises(AttributeError):
                setattr(rig, field, 'replacement')
        images, request = self.image_owner_and_request()
        alternate = self.root / 'other-master.png'
        Image.new('RGB', (8, 8), 'blue').save(alternate)
        request['checks'][0]['inputs'][0].update(pointer(self.root, alternate, kind='image-master'))
        self.assertNotEqual(images.images, request['checks'][0]['inputs'])
        self.assertEqual(self.engine(images).run(request)['checks'][0]['unsatisfied_gates'],
                         ['canonical-reference'])
        exposed = images.images
        exposed.clear()
        self.assertEqual(len(images.images), 1)
        for field in ('images', 'root'):
            with self.subTest(field=field), self.assertRaises(AttributeError):
                setattr(images, field, 'replacement')

    def test_native_refusal_codes_keep_their_gates_with_redacted_messages(self):
        owner, request = self.bound('geometry-inspection')
        for code, gate, integrity in (
            ('RIG_BUSY', 'ownership', 'verified'),
            ('RIG_LIMIT_MEMORY', 'resource-memory_bytes', 'verified'),
            ('RIG_HASH', 'integrity', 'failed'),
            ('RIG_SOURCE_CHANGED', 'integrity', 'failed'),
            ('RIG_PAYLOAD_IO', 'integrity', 'failed'),
            ('RIG_PAYLOAD_TYPE', 'integrity', 'failed'),
            ('RIG_PAYLOAD_PATH', 'integrity', 'failed'),
            ('RIG_DIRECTORY_CLOSED', 'integrity', 'failed'),
            ('RIG_DIRECTORY_CHANGED', 'integrity', 'failed'),
            ('RIG_DIRECTORY_FD', 'integrity', 'failed'),
            ('RIG_DIRECTORY_PATH', 'integrity', 'failed'),
            ('RIG_DIRECTORY_UNAVAILABLE', 'integrity', 'failed'),
        ):
            with self.subTest(code=code), patch(
                'rig_models.load_rig', side_effect=RigError(code, f'Native input at {self.root}'),
            ):
                result = self.engine(owner).run(request)
                self.assertEqual(result['integrity'], integrity)
                self.assertEqual(result['checks'][0]['unsatisfied_gates'], [gate])
                self.assertTrue(result['checks'][0]['error'].startswith(code + ':'))
                self.assertNotIn(str(self.root), json.dumps(result))

    def test_backend_payload_error_does_not_claim_native_dependency_drift(self):
        owner, request = self.bound('geometry-inspection')
        error = RigError('RIG_PAYLOAD_IO', f'Backend output at {self.root}')
        with patch.object(owner, 'execute', side_effect=error):
            result = self.engine(owner).run(request)
        row = result['checks'][0]
        self.assertEqual(result['integrity'], 'verified')
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual(row['unsatisfied_gates'], ['execution'])
        self.assertTrue(row['error'].startswith('CONTINUITY_CAPABILITY_EXECUTION:'))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_missing_native_dependency_fails_report_integrity(self):
        owner, request = self.bound('geometry-inspection')
        (self.root / 'rig/source/demo-robotic-input.glb').unlink()
        result = self.engine(owner).run(request)
        self.assertEqual(result['integrity'], 'failed')
        self.assertEqual(result['checks'][0]['unsatisfied_gates'], ['integrity'])
        self.assertTrue(result['checks'][0]['error'].startswith('RIG_MISSING_ASSET:'))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_unverified_execution_artifacts_fail_integrity(self):
        owner, request = self.image_owner_and_request()
        report_path = self.root / 'reported.json'
        report_path.write_text('{"frame": 1}')
        stale_pointer = pointer(self.root, report_path)
        report_path.write_text('{"frame": 2}')
        for evidence, code in (([stale_pointer], 'CONTINUITY_HASH'),
                               (['invalid pointer'], 'CONTINUITY_CAPABILITY_EVIDENCE')):
            with self.subTest(code=code), patch.object(owner, 'execute', return_value=dict(
                outcome='partial', evidence=evidence, measurements={},
                verified_fields=['image-masters'], limitations=[],
            )):
                result = self.engine(owner).run(request)
                self.assertEqual(result['integrity'], 'failed')
                self.assertEqual(result['checks'][0]['unsatisfied_gates'], ['integrity'])
                self.assertTrue(result['checks'][0]['error'].startswith(code + ':'))

    def test_image_master_preflight_refuses_count_and_size_before_reading(self):
        first = self.root / 'small-master.png'
        first.write_bytes(b'placeholder')
        oversized = self.root / 'oversized-master.png'
        with oversized.open('wb') as stream:
            stream.truncate(8 * 1024 * 1024 + 1)

        with patch('continuity_capability_owners._read_regular_bytes') as read:
            for paths in ([first.name] * 129, [first.name, oversized.name]):
                with self.subTest(count=len(paths)), self.assertRaises(CapabilityRefusal) as refusal:
                    ImageMastersOwner(self.root, paths, 'evidence')
                self.assertEqual(refusal.exception.gate, 'resource-admission')
            read.assert_not_called()

    def test_aggregate_report_limit_includes_failure_rows(self):
        request = self.request('physics-continuation', 'graphical-editor', 'historical-media')
        self.engine().run(request)
        with patch('continuity_capabilities.MAX_REPORT_BYTES', 768):
            with self.assertRaises(CapabilityRefusal) as refusal:
                self.engine().run(request)
        self.assertEqual(refusal.exception.code, 'CONTINUITY_CAPABILITY_REPORT_LIMIT')

    def test_cli_and_discovery_omit_private_paths(self):
        with patch('continuity_capabilities.shutil.which', return_value=str(self.root / 'private/tool')):
            result = discover()
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertIn('blender', [row['tool_name'] for row in result['capabilities']])
        command = [sys.executable, str(REPO / 'scripts/continuity_capabilities.py'),
                   'admit', '--root', str(self.root), '--request', str(self.root / 'missing.json')]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)['code'], 'CONTINUITY_REFERENCE_MISSING')
        self.assertNotIn(str(self.root), completed.stdout + completed.stderr)
