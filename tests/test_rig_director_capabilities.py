"""Native importer capability proofs; optional 3D gate only."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
import director_capabilities as cap
from rig_errors import RigError,object_hash
from test_director_capabilities import request


class NativeCapabilityTests(unittest.TestCase):
    def test_three_real_native_importers_and_exact_evidence(self):
        operations=['importer.obj','importer.glb','importer.gltf']
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'proof';result=cap.probe(request(operations,operations),path)
            self.assertTrue(all(x['execution_verified'] for x in result['value']['capabilities']),result['diagnostics'])
            cap.assert_admission(result,operations,evidence_root=path,resources={'max_provider_calls':0})
            for reference in result['artifacts']:
                evidence=json.loads((path/reference['path']).read_text())
                self.assertEqual(evidence['execution']['vertices'],3)
                self.assertEqual(evidence['execution']['triangles'],1)
            stripped=deepcopy(result['value'])
            stripped['capabilities'][0]['probe_evidence'][0]['dependencies']=[]
            stripped['digest']=object_hash({k:v for k,v in stripped.items() if k!='digest'})
            with self.assertRaisesRegex(RigError,'receipt payloads differ'):
                cap.assert_admission(stripped,operations,evidence_root=path)
            changed=deepcopy(result);changed['value']['capabilities'][0]['limits']['max_provider_calls']=100
            with self.assertRaises(RigError):cap.assert_admission(changed,operations)
            changed['value']['digest']=object_hash({k:v for k,v in changed['value'].items() if k!='digest'})
            with self.assertRaises(RigError):cap.assert_admission(changed,operations,evidence_root=path)
            with self.assertRaises(RigError):cap.assert_admission(result,operations,resources={'max_provider_calls':1})
            with self.assertRaises(RigError):cap.assert_admission(result,operations,now=datetime.now(timezone.utc)+timedelta(days=1))
            (path/result['artifacts'][0]['path']).write_text('{}')
            with self.assertRaises(RigError):cap.assert_admission(result,operations,evidence_root=path)

    def test_empty_execution_evidence_cannot_admit(self):
        with tempfile.TemporaryDirectory() as directory:
            result=cap.probe(request(['importer.obj'],['importer.obj']),Path(directory).resolve()/'proof')
            result['value']['capabilities'][0]['probe_evidence']=[]
            result['value']['digest']=object_hash({k:v for k,v in result['value'].items() if k!='digest'})
            with self.assertRaises(RigError):cap.assert_admission(result,['importer.obj'],evidence_root=Path(directory).resolve()/'proof')

    def test_malformed_receipt_is_typed_failure(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'proof'
            result=cap.probe(request(['importer.obj'],['importer.obj']),root)
            reference=result['value']['capabilities'][0]['probe_evidence'][0]
            path=root/reference['path'];path.write_bytes(b'{}')
            reference['sha256']=hashlib.sha256(b'{}').hexdigest();reference['byte_size']=2
            # Rebind every duplicated reference consistently; malformed receipt still fails.
            result['artifacts']=[deepcopy(reference)];result['provenance_receipts']=[deepcopy(reference)]
            result['value']['digest']=object_hash({k:v for k,v in result['value'].items() if k!='digest'})
            with self.assertRaises(RigError):cap.assert_admission(result,['importer.obj'],evidence_root=root)
