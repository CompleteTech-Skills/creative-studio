"""Admission regressions plus real native geometry smoke tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import director_capabilities as cap
from director_contracts import validate_method_result
from rig_errors import RigError, object_hash

ROOT=Path(__file__).resolve().parents[1]


def request(operations, probes=()):
    value=next(d for d in json.loads((ROOT/'examples/director-contracts/contracts.json').read_text())
               if d['kind']=='director-request' and d['method']=='CapabilityRegistry.probe')
    value['input'].update(operations=list(operations),local_probes=list(probes),provider_configuration_refs=[])
    value['context']['deadline']=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
    return value


class CapabilityTests(unittest.TestCase):
    def test_missing_tools_have_precise_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            result=cap.probe(request(['blender.cpu','codec.h264','planning.provider']),Path(directory)/'proof',
                             configuration={'blender':None,'ffmpeg':None,'ffprobe':None})
            self.assertEqual({x['status'] for x in result['value']['capabilities']},{'unavailable'})
            self.assertEqual({x['code'] for x in result['diagnostics']},
                             {'CAPABILITY_MISSING_BLENDER','CAPABILITY_MISSING_FFMPEG_OR_FFPROBE','CAPABILITY_MISSING_PLANNING_CREDENTIALS','CAPABILITY_PRICE_UNKNOWN'})
            validate_method_result('CapabilityRegistry.probe',result)
            self.assertTrue((Path(directory)/'proof/result.json').is_file())

    def test_discovery_does_not_start_workers_or_providers(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cap,'_worker',side_effect=AssertionError('Executed')):
            result=cap.probe(request(['native.acquisition','module.scene3d','planning.provider']),Path(directory)/'proof',
                configuration={'planning_provider':{'id':'external','configured':True}})
            self.assertEqual({x['status'] for x in result['value']['capabilities']},{'untested'})
            self.assertFalse(any(x['execution_verified'] for x in result['value']['capabilities']))
            for path in (Path(directory)/'proof/receipts').glob('*.json'):
                evidence=json.loads(path.read_text())
                self.assertFalse(evidence['execution_verified'])
                self.assertEqual(evidence['dependency_fingerprint'],object_hash(evidence['dependencies']))

    def test_provider_descriptor_rejects_secret_or_endpoint(self):
        for extra in ('secret','token','endpoint','api_key'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory, self.assertRaises(RigError):
                cap.probe(request(['planning.provider']),Path(directory)/'proof',
                    configuration={'planning_provider':{'id':'external','configured':True,extra:'do-not-record'}})

    def test_unsupported_operation_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            result=cap.probe(request(['made-up']),Path(directory)/'proof')
            self.assertEqual(result['diagnostics'][0]['code'],'CAPABILITY_UNSUPPORTED_OPERATION')
            with self.assertRaises(RigError):cap.assert_admission(result,['made-up'])

    def test_duplicate_and_unrequested_probes_rejected(self):
        for value in (request(['blender.cpu','blender.cpu']),request(['blender.cpu'],['codec.h264'])):
            with tempfile.TemporaryDirectory() as directory,self.assertRaises(RigError):
                cap.probe(value,Path(directory)/'proof')

    def test_existing_destination_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'proof';path.mkdir();(path/'mine').write_text('original')
            with self.assertRaises(RigError):cap.probe(request([]),path)
            self.assertEqual((path/'mine').read_text(),'original')

    def test_racing_destination_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'proof';publish=cap._rename_no_replace
            def race(stage,target):
                path.mkdir();(path/'mine').write_text('race')
                publish(stage,target)
            with patch.object(cap,'_rename_no_replace',side_effect=race),self.assertRaises(OSError):
                cap.probe(request([]),path)
            self.assertEqual((path/'mine').read_text(),'race')

    def test_expired_deadline_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            value=request([]);value['context']['deadline']='2000-01-01T00:00:00Z'
            with self.assertRaises(RigError):cap.probe(value,Path(directory)/'proof')
            self.assertFalse((Path(directory)/'proof').exists())

    def test_three_real_native_importers_and_exact_evidence(self):
        operations=['importer.obj','importer.glb','importer.gltf']
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'proof';result=cap.probe(request(operations,operations),path)
            self.assertTrue(all(x['execution_verified'] for x in result['value']['capabilities']),result['diagnostics'])
            cap.assert_admission(result,operations,evidence_root=path,resources={'max_provider_calls':0})
            for reference in result['artifacts']:
                evidence=json.loads((path/reference['path']).read_text())
                self.assertEqual(evidence['execution']['vertices'],3)
                self.assertEqual(evidence['execution']['triangles'],1)
            changed=deepcopy(result);changed['value']['capabilities'][0]['limits']['max_provider_calls']=100
            with self.assertRaises(RigError):cap.assert_admission(changed,operations)
            with self.assertRaises(RigError):cap.assert_admission(result,operations,resources={'max_provider_calls':1})
            with self.assertRaises(RigError):cap.assert_admission(result,operations,now=datetime.now(timezone.utc)+timedelta(days=1))
            (path/result['artifacts'][0]['path']).write_text('{}')
            with self.assertRaises(RigError):cap.assert_admission(result,operations,evidence_root=path)

    def test_probe_order_does_not_change_dependency_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            first=cap.probe(request(['module.scene3d','native.acquisition']),Path(directory)/'a')
            second=cap.probe(request(['native.acquisition','module.scene3d']),Path(directory)/'b')
            a=[json.loads((Path(directory)/'a'/r['path']).read_text())['dependency_fingerprint'] for r in first['artifacts']]
            b=[json.loads((Path(directory)/'b'/r['path']).read_text())['dependency_fingerprint'] for r in second['artifacts']]
            self.assertEqual(a,b)

    def test_worker_times_out_and_drops_output(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            result,text=cap._worker([sys.executable,'-c','import time;print("secret");time.sleep(10)'],directory,1)
            self.assertFalse(result['success']);self.assertEqual(result['reason'],'timeout');self.assertEqual(text,'')
            self.assertFalse((Path(directory)/'worker-output.txt').exists())

    def test_failed_gpu_explicit_cpu_fallback(self):
        import sys
        with tempfile.TemporaryDirectory() as directory,patch.object(cap,'_blender_probe',return_value={'success':False,'reason':'worker-failed'}):
            result=cap.probe(request(['blender.gpu'],['blender.gpu']),Path(directory)/'proof',configuration={'blender':sys.executable})
            record=json.loads((Path(directory)/'proof'/result['artifacts'][0]['path']).read_text())
            self.assertEqual(record['fallback'],'blender.cpu-requires-separate-probe-and-explicit-policy')
            with self.assertRaises(RigError):cap.assert_admission(result,['blender.gpu'])

    def test_production_policy_is_separate_from_probe_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            value=cap.probe(request(['module.scene3d']),Path(directory)/'proof',
                configuration={'timeout_seconds':1,'production_limits':{'max_cpu_seconds':7200}})
            self.assertEqual(value['value']['capabilities'][0]['limits']['max_cpu_seconds'],7200)
        for limits in ({'max_provider_calls':1},{'max_cpu_seconds':True},{'max_memory_bytes':-1}):
            with tempfile.TemporaryDirectory() as directory,self.assertRaises(RigError):
                cap.probe(request([]),Path(directory)/'proof',configuration={'production_limits':limits})

    def test_bare_malformed_snapshot_is_typed_failure(self):
        for value in (None,{},[],{'capabilities':[]}):
            with self.subTest(value=value),self.assertRaises(RigError):cap.assert_admission(value,[])
