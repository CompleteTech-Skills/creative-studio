"""Actual local inventory evidence and fail-closed provider/resource boundaries."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import director_capabilities as cap
import director_inventory as inventory
from rig_errors import RigError
from test_director_capabilities import request


class InventoryTests(unittest.TestCase):
    def receipt(self, root, result, operation):
        entry = next(x for x in result['value']['capabilities'] if x['operation'] == operation)
        return json.loads((root/entry['probe_evidence'][0]['path']).read_text())

    def test_complete_inventory_discovery_does_not_execute_or_claim_execution(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cap, '_worker', side_effect=AssertionError('worker started')):
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(inventory.OPERATIONS), root,
                               configuration={'blender': None})
            self.assertEqual(len(result['value']['capabilities']), len(inventory.OPERATIONS))
            for operation in inventory.OPERATIONS:
                receipt = self.receipt(root, result, operation)
                self.assertNotEqual(receipt['reason'], 'unsupported-operation')
                self.assertFalse(receipt['execution_verified'])
                self.assertFalse(receipt['initialized'])
                for field in ('backend', 'supported_formats', 'supported_operations',
                              'required_permissions', 'performance', 'policy_limits'):
                    self.assertIn(field, receipt)

    def test_actual_interpreter_and_destination_filesystem_are_measured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()/'evidence'
            operations = ['runtime.python', 'resource.storage']
            result = cap.probe(request(operations, operations), root)
            runtime = self.receipt(root, result, operations[0])
            storage = self.receipt(root, result, operations[1])
            self.assertTrue(runtime['execution_verified'])
            self.assertFalse(runtime['execution']['production_matrix_verified'])
            self.assertTrue(storage['execution_verified'])
            measured = storage['execution']['available_bytes']
            # Filesystem free space may change while the probe writes receipts.
            self.assertGreaterEqual(measured, 0)
            self.assertEqual(storage['execution']['total_bytes'], shutil.disk_usage(root).total)
            self.assertFalse(storage['execution']['reservation'])
            self.assertNotEqual(storage['policy_limits']['max_storage_bytes'],
                                storage['execution']['total_bytes'])

    def test_failed_memory_measurement_is_explicit_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(inventory, 'memory_observation', side_effect=OSError('not supported')):
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(['resource.memory'], ['resource.memory']), root)
            receipt = self.receipt(root, result, 'resource.memory')
            self.assertEqual(receipt['status'], 'unavailable')
            self.assertFalse(receipt['execution_verified'])
            self.assertNotIn('execution', receipt)
            self.assertEqual(receipt['failure_type'], 'OSError')

    def test_actual_resource_receipt_caps_admission_below_policy(self):
        observation = dict(available_bytes=128, total_bytes=256, source='unit-fixture',
                           scope='synthetic boundary fixture', reservation=False)
        with tempfile.TemporaryDirectory() as directory, patch.object(inventory, 'memory_observation', return_value=observation):
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(['resource.memory'], ['resource.memory']), root)
            if not (hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd):
                with self.assertRaises(RigError) as error:
                    cap.assert_admission(result, ['resource.memory'], evidence_root=root)
                self.assertEqual(error.exception.report['code'], 'DIRECTOR_PLATFORM')
                return
            cap.assert_admission(result, ['resource.memory'], evidence_root=root,
                                 resources={'max_memory_bytes': 128})
            with self.assertRaises(RigError) as error:
                cap.assert_admission(result, ['resource.memory'], evidence_root=root,
                                     resources={'max_memory_bytes': 129})
            self.assertEqual(error.exception.report['code'], 'DIRECTOR_CAPABILITY_RESOURCE')

    def test_provider_descriptors_are_public_unverified_and_never_invoked(self):
        for operation, field in inventory.PROVIDERS.items():
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory, patch.object(cap, '_worker', side_effect=AssertionError('provider called')):
                root = Path(directory).resolve()/'evidence'
                result = cap.probe(request([operation]), root,
                    configuration={field: dict(id='configured-fixture', configured=True)})
                receipt = self.receipt(root, result, operation)
                self.assertEqual(receipt['status'], 'untested')
                self.assertFalse(receipt['execution_verified'])
                self.assertEqual(receipt['price_availability'], 'unknown')
                self.assertEqual(receipt['provider_id'], 'configured-fixture')
                with self.assertRaises(RigError):
                    cap.assert_admission(result, [operation], evidence_root=root)
            for forbidden in ('endpoint', 'api_key', 'token', 'secret'):
                with tempfile.TemporaryDirectory() as directory, self.assertRaises(RigError):
                    cap.probe(request([operation]), Path(directory).resolve()/'evidence',
                        configuration={field: dict(id='fixture', configured=True, **{forbidden: 'not-public'})})

    def test_png_probe_writes_and_decodes_real_alpha_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(['format.image'], ['format.image']), root)
            receipt = self.receipt(root, result, 'format.image')
            self.assertTrue(receipt['execution_verified'])
            self.assertEqual(receipt['execution']['decoded_pixels'], 4)
            self.assertEqual(receipt['supported_formats'], ['PNG'])
            from PIL import Image
            payload = receipt['payloads'][0]
            with Image.open(root/payload['path']) as image:
                image.load()
                self.assertEqual(image.convert('RGBA').getpixel((0, 0)), (12, 34, 56, 78))


if __name__ == '__main__':
    unittest.main()
