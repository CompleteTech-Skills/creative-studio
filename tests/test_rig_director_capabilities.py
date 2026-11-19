"""Native importer capability proofs; optional 3D gate only."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
import director_capabilities as cap
import director_contracts
from rig_errors import RigError,object_hash
from test_director_capabilities import request


class NativeCapabilityTests(unittest.TestCase):
    def assert_native_admission(self, result, operations, root, **kwargs):
        if director_contracts.artifact_reads_supported():
            cap.assert_admission(result, operations, evidence_root=root, **kwargs)
        else:
            with self.assertRaises(RigError) as error:
                cap.assert_admission(result, operations, evidence_root=root, **kwargs)
            self.assertEqual(error.exception.report['code'], 'DIRECTOR_PLATFORM')

    def test_procedural_robot_probe_retains_real_roundtrip_geometry(self):
        from rig_import import load_character_model
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()/'proof'
            result = cap.probe(request(['procedural.native'], ['procedural.native']), root)
            self.assertTrue(result['value']['capabilities'][0]['execution_verified'], result['diagnostics'])
            receipt = json.loads((root/result['artifacts'][0]['path']).read_text())
            self.assertEqual(receipt['execution']['scope'], 'procedural-robotic-fixture-only')
            self.assertGreater(receipt['execution']['vertices'], 100)
            self.assertGreater(receipt['execution']['triangles'], 100)
            geometry = load_character_model(root/receipt['payloads'][0]['path']).geometry
            self.assertEqual(geometry.vertex_count, receipt['execution']['vertices'])
            self.assertEqual(geometry.triangle_count, receipt['execution']['triangles'])

    def test_real_blender_capabilities(self):
        required = os.environ.get('HATCH_DIRECTOR_BLENDER_TEST') == '1'
        if required:
            self.assertTrue(os.environ.get('HATCH_BLENDER'),
                            'The required gate must configure the trusted Blender executable')
        if cap._executable('blender', {}) is None:
            if required:
                self.fail('The configured Blender executable is unavailable')
            self.skipTest('Blender is unavailable; worker orchestration has synthetic coverage')
        from PIL import Image
        from rig_import import load_character_model

        def verify(root):
            operations = ['blender.cpu', 'blender.engines', 'importer.blend']
            result = cap.probe(request(operations, operations), root,
                               configuration={'timeout_seconds': 120})
            self.assertTrue(all(entry['execution_verified'] for entry in
                                result['value']['capabilities']), result['diagnostics'])
            self.assert_native_admission(result, operations, root)
            receipts = {entry['operation']: json.loads((root/entry['probe_evidence'][0]['path']).read_bytes())
                        for entry in result['value']['capabilities']}

            render = receipts['blender.cpu']
            self.assertEqual(render['execution']['device'], 'CPU')
            self.assertEqual(render['version_scope'], 'reported-during-probe')
            with Image.open(root/render['payloads'][0]['path']) as image:
                image.load()
                self.assertEqual(image.size, (8, 8))

            engines = receipts['blender.engines']
            self.assertIn('CYCLES', engines['execution']['engines'])
            self.assertIs(engines['execution']['rendered'], False)
            self.assertEqual(engines['version'], render['version'])

            blend = receipts['importer.blend']
            self.assertEqual((blend['execution']['vertices'], blend['execution']['triangles']), (3, 1))
            self.assertEqual(blend['execution']['authoring_blender_version'], render['version'])
            self.assertEqual(blend['execution']['importing_blender_version'], render['version'])
            self.assertIsNone(blend['version'])
            payload = blend['payloads'][0]
            self.assertEqual(payload['media_type'], 'application/x-blender')
            geometry = load_character_model(root/payload['path']).geometry
            self.assertEqual((geometry.vertex_count, geometry.triangle_count), (3, 1))

        retained = os.environ.get('HATCH_DIRECTOR_EVIDENCE') if required else None
        if retained:
            verify(Path(retained).absolute())
        else:
            with tempfile.TemporaryDirectory() as directory:
                verify(Path(directory).resolve()/'proof')

    def test_three_real_native_importers_and_exact_evidence(self):
        operations=['importer.obj','importer.glb','importer.gltf']
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'proof';result=cap.probe(request(operations,operations),path)
            self.assertTrue(all(x['execution_verified'] for x in result['value']['capabilities']),result['diagnostics'])
            self.assert_native_admission(result,operations,path,resources={'max_provider_calls':0})
            for reference in result['artifacts']:
                evidence=json.loads((path/reference['path']).read_text())
                self.assertEqual(evidence['execution']['vertices'],3)
                self.assertEqual(evidence['execution']['triangles'],1)
            stripped=deepcopy(result['value'])
            stripped['capabilities'][0]['probe_evidence'][0]['dependencies']=[]
            stripped['digest']=object_hash({k:v for k,v in stripped.items() if k!='digest'})
            if director_contracts.artifact_reads_supported():
                with self.assertRaisesRegex(RigError,'receipt payloads differ'):
                    cap.assert_admission(stripped,operations,evidence_root=path)
            else:
                self.assert_native_admission(stripped,operations,path)
                from unittest.mock import patch
                with patch.object(director_contracts,'_read_artifact',
                                  side_effect=lambda root,ref:(root/ref['path']).read_bytes()):
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
