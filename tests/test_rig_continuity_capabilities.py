"""Offline continuity capability and fallback contracts."""
import copy
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from continuity_capabilities import Admission, CapabilityRefusal, Policy, MATRIX, MANDATORY_FIELDS, OwnerAdapter, schemas, validate, discover
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

    def test_cpu_renderer_fallback_requires_cpu_device(self):
        owner, request = self.bound('gpu-rendering')
        execute = owner.execute
        for device in ('gpu', None):
            def execute_with_device(check):
                result = execute(check)
                result['measurements']['device'] = device
                return result

            with self.subTest(device=device), patch.object(owner, 'execute', side_effect=execute_with_device):
                result = self.engine(owner).run(request)
                self.assertEqual(result['outcome'], 'failed')
                self.assertEqual(result['checks'][0]['unsatisfied_gates'], ['execution'])
                self.assertTrue(result['checks'][0]['error'].startswith('CONTINUITY_CAPABILITY_DEVICE:'))

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

    def test_late_cancellation_preserves_asserted_failures_and_aggregate(self):
        owner, request = self.image_owner_and_request()
        request['checks'][0]['resources']['cpu_seconds'] = 1
        request['checks'].append(self.request('physics-continuation')['checks'][0])
        request['checks'][1]['id'] = 'check-1'

        for owner_outcome, wall, cpu, expected, gate in (
            ('partial', 2, 0, 'failed', 'resource-cpu_seconds'),
            ('partial', 0, 2, 'failed', 'resource-cpu_seconds'),
            ('failed', 0, 0, 'failed', None),
            ('partial', 0, 0, 'canceled', 'cancellation'),
        ):
            finished = False

            def execute(check):
                nonlocal finished
                finished = True
                return dict(outcome=owner_outcome, evidence=[], measurements={},
                            verified_fields=[], limitations=['Fixture owner result'])

            timers = SimpleNamespace(monotonic=lambda: wall if finished else 0,
                                     process_time=lambda: cpu if finished else 0)
            with self.subTest(owner_outcome=owner_outcome, wall=wall, cpu=cpu), patch.object(
                owner, 'execute', side_effect=execute,
            ), patch('continuity_capabilities.time', timers):
                result = self.engine(owner).run(request, canceled=lambda: finished)
                first, second = result['checks']
                self.assertTrue(finished)
                self.assertEqual(result['outcome'], expected)
                self.assertEqual(first['outcome'], expected)
                self.assertEqual(first['unsatisfied_gates'], [gate] if gate else [])
                self.assertIn('Fixture owner result', first['limitations'])
                self.assertEqual(second['outcome'], 'canceled')
                self.assertEqual(second['unsatisfied_gates'], ['cancellation'])
                if gate == 'resource-cpu_seconds':
                    self.assertTrue(first['error'].startswith('CONTINUITY_CAPABILITY_LIMIT:'))
                    measured = first['measurements']['admission_resources']
                    self.assertEqual((measured['wall_seconds'], measured['process_cpu_seconds']), (wall, cpu))

    def test_fallback_disallowed_blocks(self):
        owner, request=self.bound('geometry-inspection');request['checks'][0]['allow_fallback']=False
        self.assertEqual(self.engine(owner).run(request)['outcome'],'blocked_missing_capability')

    def test_permissions_and_resource_policy_not_request_authority(self):
        owner, request=self.bound('geometry-inspection')
        for policy,gate in ((Policy(),'permission'),(Policy(asset_rights=True,allowed_owners=frozenset(['hatch-rig']),
                max_memory_bytes=1),'resource-memory_bytes')):
            result=Admission(self.root,policy=policy,owners=[owner]).run(request)
            self.assertEqual(result['outcome'],'failed');self.assertIn(gate,result['checks'][0]['unsatisfied_gates'])

    def test_native_peak_memory_measurement_and_windows_independent_baseline(self):
        from continuity_capabilities import _peak_memory_bytes
        peak = _peak_memory_bytes()
        print('Native process lifetime peak bytes:',peak,flush=True)
        self.assertIsNotNone(peak)
        self.assertGreater(peak,0)
        if sys.platform == 'win32':
            import os
            import subprocess
            # .NET Process exposes the OS peak independently of our ctypes
            # declarations; query this Python PID, not the PowerShell child.
            baseline = int(subprocess.check_output(['powershell','-NoProfile','-Command',
                '(Get-Process -Id '+str(os.getpid())+').PeakWorkingSet64'],text=True).strip())
            print('Independent Windows .NET process peak bytes:',baseline,flush=True)
            self.assertGreater(baseline,0)
            self.assertLess(abs(peak-baseline),64*1024*1024)

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

    def test_native_input_io_failure_is_redacted_and_fails_integrity(self):
        owner, request = self.bound('geometry-inspection')
        error = PermissionError(f'Cannot read source at {self.root}')
        with patch('rig_models.load_rig', side_effect=error):
            result = self.engine(owner).run(request)
        row = result['checks'][0]
        self.assertEqual(result['integrity'], 'failed')
        self.assertEqual(row['unsatisfied_gates'], ['integrity'])
        self.assertTrue(row['error'].startswith('RIG_PAYLOAD_IO:'))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_backend_payload_error_does_not_claim_native_dependency_drift(self):
        owner, request = self.bound('geometry-inspection')
        for error in (RigError('RIG_PAYLOAD_IO', f'Backend output at {self.root}'),
                      PermissionError(f'Cannot write output at {self.root}')):
            with self.subTest(error_type=type(error)), patch.object(owner, 'execute', side_effect=error):
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

    def test_partial_and_visual_review_require_evidence_for_verified_fields(self):
        owner, request = self.image_owner_and_request()
        request['checks'][0]['required_fields'] = ['image-masters']
        for outcome in ('partial', 'needs_visual_review'):
            for fields in ([], ['image-masters']):
                with self.subTest(outcome=outcome, fields=fields), patch.object(
                    owner, 'execute', return_value=dict(outcome=outcome, evidence=[],
                                                       measurements={}, verified_fields=fields, limitations=[]),
                ):
                    result = self.engine(owner).run(request)
                    row = result['checks'][0]
                    self.assertIn('image-masters', row['missing_fields'])
                    if fields:
                        self.assertEqual(result['outcome'], 'failed')
                        self.assertEqual(result['integrity'], 'failed')
                        self.assertEqual(row['unsatisfied_gates'], ['integrity'])
                        self.assertTrue(row['error'].startswith('CONTINUITY_CAPABILITY_EVIDENCE:'))
                    else:
                        self.assertEqual(result['outcome'], outcome)
                        self.assertEqual(result['integrity'], 'verified')

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

    def test_final_integrity_failure_cannot_make_a_check_exceed_its_cap(self):
        owner, request = self.image_owner_and_request()
        baseline = self.engine(owner).run(request)
        check_limit = len(canonical_bytes(baseline['checks'][0])) + 40
        late_failure = CapabilityRefusal(
            'CONTINUITY_HASH', 'integrity',
            'The exact reference verified before execution no longer matches the bound bytes during final verification',
        )
        with patch('continuity_capabilities.MAX_CHECK_BYTES', check_limit), patch(
            'continuity_capabilities.ReferencePass.final', side_effect=late_failure,
        ), self.assertRaises(CapabilityRefusal) as refusal:
            self.engine(owner).run(request)
        self.assertEqual(refusal.exception.code, 'CONTINUITY_CAPABILITY_REPORT_LIMIT')
        self.assertIn('one MiB check limit', refusal.exception.message)

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

    def test_source_bound_probe_cannot_run_on_different_rig(self):
        owner, request=self.bound('geometry-inspection');request['checks'][0]['inputs']=[]
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('canonical-reference',result['checks'][0]['unsatisfied_gates'])

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

    def test_measured_memory_limit_blocks_before_owner_execution(self):
        owner, request=self.bound('geometry-inspection')
        request['checks'][0]['resources']['memory_bytes']=1
        with patch.object(owner, 'execute', wraps=owner.execute) as execute:
            result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('resource-memory_bytes',result['checks'][0]['unsatisfied_gates'])
        execute.assert_not_called()

    def test_network_policy_is_never_a_fallback(self):
        owner, request=self.bound('geometry-inspection')
        owner._snap['offline']=False
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('network-policy',result['checks'][0]['unsatisfied_gates'])

    def test_nullable_snapshot_integer_fields_normalize_on_copy(self):
        owner,request=self.bound('geometry-inspection')
        result=self.engine(owner).run(request)
        result['checks'][0]['snapshot']['limits']['memory_bytes']=float(result['checks'][0]['snapshot']['limits']['memory_bytes'])
        parsed=validate(result,'result')
        self.assertIs(type(parsed['checks'][0]['snapshot']['limits']['memory_bytes']),int)
        self.assertIs(type(result['checks'][0]['snapshot']['limits']['memory_bytes']),float)

    def test_native_lock_collision_is_not_missing_backend(self):
        from rig_errors import job_lock

        owner, request = self.bound('geometry-inspection')
        with job_lock(self.root / 'rig'):
            result = self.engine(owner).run(request)

        self.assertEqual(result['outcome'], 'failed')
        row = result['checks'][0]
        self.assertEqual(row['unsatisfied_gates'], ['ownership'])
        self.assertEqual(row['error'].partition(':')[0], 'RIG_BUSY')

    def test_standalone_probe_rejects_omitted_capability(self):
        import continuity_capability_probe as probe

        destination = self.root / 'probe'
        with patch.dict(probe.MATRIX):
            del probe.MATRIX['historical-media']
            with self.assertRaisesRegex(AssertionError, r"matrix capability mismatch: missing=\['historical-media'\]"):
                probe.run(destination)
        self.assertFalse((destination / 'report.json').exists())

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

    def test_owner_required_review_records_missing_field_and_gate(self):
        owner,native=self.policy_owner('visual-reference',malformed=dict(outcome='needs_visual_review'))
        request=self.request('visual-reference');request['checks'][0]['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(result['outcome'],'needs_visual_review')
        self.assertIn('visual-approval',result['checks'][0]['missing_fields'])
        self.assertIn('visual-approval',result['checks'][0]['unsatisfied_gates'])

    def test_whole_process_resource_measurement_is_explicit(self):
        owner, request=self.bound('geometry-inspection')
        result=self.engine(owner).run(request)
        m=result['checks'][0]['measurements']['admission_resources']
        self.assertGreaterEqual(m['process_cpu_seconds'],0)
        self.assertIn(m['rss_status'],('measured','unavailable'))

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

    def test_unknown_fields_duplicate_checks_and_paths_rejected(self):
        request=self.request('geometry-inspection')
        for change in (lambda r:r.update(command='sh'),lambda r:r['checks'].append(copy.deepcopy(r['checks'][0])),
                       lambda r:r['project_revision'].update(path='../secret')):
            value=copy.deepcopy(request);change(value)
            with self.assertRaises(ValueError):validate(value,'request')

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

    @patch('continuity_capabilities._peak_memory_bytes', lambda: 64 * 1024 * 1024)
    def test_oversized_native_and_combined_reports_are_bounded_failures(self):
        # Each individual response is valid JSON; size limits apply before final
        # report serialization, preserving all check outcomes.
        owner,native=self.policy_owner('geometry-inspection',malformed=dict(measurements={'large':'x'*1048577}))
        request=self.request('geometry-inspection','physics-continuation');request['checks'][0]['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(owner.calls, 1)
        self.assertEqual(result['outcome'], 'failed')
        geometry, physics = result['checks']
        self.assertEqual(geometry['outcome'], 'failed')
        self.assertEqual(geometry['error'], 'CONTINUITY_CAPABILITY_REPORT_LIMIT: Native result exceeds the one MiB check limit')
        self.assertEqual(geometry['unsatisfied_gates'], ['resource-admission'])
        self.assertEqual(physics['outcome'], 'blocked_missing_capability')

        owner,native=self.policy_owner('geometry-inspection',malformed=dict(measurements={'large':'x'*700000}))
        request=self.request(*(['geometry-inspection']*8))
        for check in request['checks']:check['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(owner.calls, 8)
        self.assertEqual(result['outcome'], 'failed')
        checks = result['checks']
        self.assertEqual([check['outcome'] for check in checks], ['supported_fallback'] * 5 + ['failed'] * 3)
        for check in checks[5:]:
            self.assertEqual(check['error'], 'CONTINUITY_CAPABILITY_REPORT_LIMIT: Complete result exceeds the four MiB report limit')
            self.assertEqual(check['unsatisfied_gates'], ['resource-admission'])
        self.assertLessEqual(len(canonical_bytes(result)), 4 * 1024 * 1024)

    def test_opaque_measurement_maps_are_not_reference_objects(self):
        metadata=dict(id='opaque',version='not-a-version',path='../opaque-user-string',sha256='not-a-hash',kind='note')
        owner,native=self.policy_owner('geometry-inspection',malformed=dict(measurements={'metadata':metadata}))
        request=self.request('geometry-inspection');request['checks'][0]['inputs']=[native]
        result=self.engine(owner).run(request)
        self.assertEqual(result['checks'][0]['outcome'],'supported_fallback')
        self.assertEqual(result['checks'][0]['measurements']['metadata'],metadata)

    @patch('continuity_capabilities._peak_memory_bytes', lambda: 64 * 1024 * 1024)
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
        self.assertEqual(owner.calls, 16)
        self.assertEqual([check['outcome'] for check in result['checks']], ['supported_fallback'] * 16)

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
                context=nullcontext()
            with context,patch.object(module.time,'process_time',side_effect=lambda:fake_cpu[0]):
                result=self.engine(owner).run(request)
            self.assertEqual(owner.calls,0,stage)
            self.assertEqual(result['outcome'],'failed')
            self.assertIn('resource-admission',result['checks'][0]['unsatisfied_gates'])
