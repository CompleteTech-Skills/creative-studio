"""Actual local inventory evidence and fail-closed provider/resource boundaries."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import director_capabilities as cap
import director_inventory as inventory
from rig_errors import RigError, object_hash
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

    def test_unsupported_posix_memory_api_returns_unavailable(self):
        # Dispatch uses sys.platform only, so this also runs on Windows, where
        # os.sysconf does not exist (create=True) and os.name stays 'nt'.
        with patch.object(inventory.sys, 'platform', 'darwin'), \
                patch.object(inventory.os, 'sysconf', side_effect=KeyError('unsupported'), create=True):
            with self.assertRaises(ValueError):
                inventory.memory_observation()
        with patch.object(inventory.sys, 'platform', 'darwin'), \
                patch.object(inventory.os, 'sysconf', None, create=True):
            with self.assertRaises(ValueError):
                inventory.memory_observation()
        pages = {'SC_PAGE_SIZE': 4096, 'SC_AVPHYS_PAGES': 3, 'SC_PHYS_PAGES': 8}
        self.assertEqual(inventory.sysconf_memory(pages.__getitem__), (3*4096, 8*4096))

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
            self.assertIn('python', receipt['dependencies'])
            self.assertIn('Pillow', receipt['dependencies'])
            from PIL import Image
            payload = receipt['payloads'][0]
            with Image.open(root/payload['path']) as image:
                image.load()
                self.assertEqual(list(image.convert('RGBA').getdata()), [(12, 34, 56, 78)] * 4)

    def test_every_remote_family_requires_network_reservation_allowlist_and_price(self):
        # Exact-byte synthetic executed receipts exercise policy only, not a
        # claim that any real provider ran.
        for operation in inventory.PROVIDERS:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                limits = {**cap.LIMITS, 'max_provider_calls': 1, 'currency': 'USD', 'max_amount': 1}
                receipt = dict(operation=operation, status='available', execution_verified=True,
                    version='engineering-fixture', policy_limits=limits, payloads=[],
                    provider_id='fixture-provider', price_availability='known', price_per_call=.25,
                    currency='USD', fixture=True)
                path = root/'receipt.json'
                path.write_text(json.dumps(receipt))
                reference = cap._reference(path, root)
                snapshot = dict(capabilities=[dict(operation=operation, status='available',
                    execution_verified=True, version='engineering-fixture', limits=limits,
                    probe_evidence=[reference])],
                    expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
                snapshot['digest'] = object_hash(snapshot)
                if not (hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd):
                    with self.assertRaises(RigError) as error:
                        cap.assert_admission(snapshot, [operation], evidence_root=root)
                    self.assertEqual(error.exception.report['code'], 'DIRECTOR_PLATFORM')
                    continue
                authority = dict(evidence_root=root, network_policy='allow-configured',
                    budget_reservation_id='fixture-reservation', budget_currency='USD',
                    budget_amount=.25, paid_policy='bounded', allowed_providers=['fixture-provider'],
                    resources={'max_provider_calls': 1})
                cap.assert_admission(snapshot, [operation], **authority)
                for delta in ({'network_policy': 'deny'}, {'budget_reservation_id': None},
                              {'allowed_providers': []}, {'paid_policy': 'deny'},
                              {'budget_amount': .24}, {'budget_currency': 'EUR'}):
                    with self.subTest(delta=delta), self.assertRaises(RigError):
                        cap.assert_admission(snapshot, [operation], **{**authority, **delta})


def _pinned_publication_supported():
    import atomic_publish
    if os.name == 'nt':
        return atomic_publish.windows_private_stage_supported()
    return atomic_publish.private_stage_supported() or atomic_publish.private_stage_fd_supported()


def _evidence_bytes(root):
    return b''.join(path.read_bytes() for path in sorted(Path(root).rglob('*')) if path.is_file())


class PublicationTests(unittest.TestCase):
    def setUp(self):
        if not _pinned_publication_supported():
            self.skipTest('pinned evidence publication is unavailable on this host; probe fails closed')

    def test_parent_swap_between_stage_and_publish_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            parent, moved = base/'parent', base/'moved'
            parent.mkdir()
            publish = cap._rename_no_replace
            swapped = []

            def swap(stage, target):
                # The stage already holds every receipt; replace its parent now.
                if os.name == 'nt':
                    # Handle pins hold ancestors without delete sharing.
                    with self.assertRaises(OSError):
                        os.rename(parent, moved)
                else:
                    os.rename(parent, moved)
                    parent.mkdir()
                    swapped.append(True)
                return publish(stage, target)

            with patch.object(cap, '_rename_no_replace', side_effect=swap):
                if os.name == 'nt':
                    cap.probe(request(['runtime.python']), parent/'proof')
                    self.assertTrue((parent/'proof'/'result.json').is_file())
                    return
                with self.assertRaises(RigError) as error:
                    cap.probe(request(['runtime.python']), parent/'proof')
            self.assertEqual(swapped, [True])
            self.assertEqual(error.exception.report['code'], 'DIRECTOR_CAPABILITY_DESTINATION')
            self.assertFalse((parent/'proof').exists())
            self.assertFalse((moved/'proof').exists())
            # Only empty private wrappers may remain in the original parent.
            for leftover in moved.iterdir():
                self.assertTrue(leftover.name.startswith('.director-probe-'))
                self.assertEqual([p for p in leftover.rglob('*') if p.is_file()], [])

    def test_publication_lands_in_pinned_parent_without_worker_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()/'proof'
            result = cap.probe(request(['importer.obj'], ['importer.obj']), root)
            self.assertTrue(result['value']['capabilities'][0]['execution_verified'], result['diagnostics'])
            names = {p.relative_to(root).parts[0] for p in root.rglob('*')}
            self.assertEqual(names, {'payloads', 'receipts', 'result.json'})


class InitializationTests(unittest.TestCase):
    def setUp(self):
        if not _pinned_publication_supported():
            self.skipTest('pinned evidence publication is unavailable on this host; probe fails closed')

    def receipts(self, root, result):
        return {entry['operation']: json.loads((root/entry['probe_evidence'][0]['path']).read_text())
                for entry in result['value']['capabilities']}

    def test_modules_initialize_in_subprocess_but_never_claim_execution(self):
        operations = sorted(inventory.INITIALIZED)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(operations, operations), root)
            receipts = self.receipts(root, result)
            for operation in operations:
                with self.subTest(operation=operation):
                    receipt = receipts[operation]
                    self.assertTrue(receipt['discovered'])
                    self.assertTrue(receipt['initialized'], receipt.get('execution'))
                    self.assertFalse(receipt['execution_verified'])
                    self.assertEqual(receipt['status'], 'untested')
                    self.assertEqual(receipt['reason'], 'initialized-not-executed')
                    self.assertTrue(receipt['execution']['initialization_only'])
                    self.assertFalse(receipt['execution']['executed'])
                    self.assertEqual(receipt['payloads'], [])
                    self.assertIn('native_module', receipt['dependencies'])
            self.assertEqual(receipts['native.acquisition']['execution']['call'], 'asset_contracts.versions')
            self.assertIsNone(receipts['native.acquisition']['execution']['model_call'])
            self.assertGreater(receipts['module.scene3d']['execution']['schemas_checked'], 0)
            self.assertEqual({d['code'] for d in result['diagnostics']}, {'CAPABILITY_INITIALIZED_NOT_EXECUTED'})
            for operation in operations:
                with self.assertRaises(RigError) as error:
                    cap.assert_admission(result, [operation], evidence_root=root)
                self.assertEqual(error.exception.report['code'], 'DIRECTOR_CAPABILITY_UNAVAILABLE')

    def test_discovery_without_probe_request_is_not_initialization(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cap, '_worker', side_effect=AssertionError('worker started')):
            root = Path(directory).resolve()/'evidence'
            receipts = self.receipts(root, cap.probe(request(['module.render3d']), root))
            self.assertEqual(receipts['module.render3d']['reason'], 'initialization-not-requested')
            self.assertFalse(receipts['module.render3d']['initialized'])

    def test_missing_module_has_precise_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(cap.MODULES, {'module.editor3d': 'absent_editor3d_owner'}), \
                patch.object(cap, '_worker', side_effect=AssertionError('worker started')):
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(['module.editor3d'], ['module.editor3d']), root)
            receipt = self.receipts(root, result)['module.editor3d']
            self.assertEqual((receipt['discovered'], receipt['initialized'], receipt['status']),
                             (False, False, 'unavailable'))
            self.assertEqual(receipt['reason'], 'missing-native-module')
            self.assertNotIn('native_module', receipt['dependencies'])
            self.assertEqual([d['code'] for d in result['diagnostics']], ['CAPABILITY_MISSING_NATIVE_MODULE'])

    def test_failed_initialization_is_unavailable_without_raw_output(self):
        failing = 'import sys\nprint("token-should-not-be-retained")\nraise SystemExit(3)\n'
        with tempfile.TemporaryDirectory() as directory, patch.object(inventory, '_CONTRACT_INIT', failing + '#%r%r'):
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(['module.camera3d'], ['module.camera3d']), root)
            receipt = self.receipts(root, result)['module.camera3d']
            self.assertEqual((receipt['initialized'], receipt['status'], receipt['reason']),
                             (False, 'unavailable', 'initialization-failed'))
            self.assertEqual(receipt['execution']['exit_code'], 3)
            self.assertEqual([d['code'] for d in result['diagnostics']], ['CAPABILITY_INITIALIZATION_FAILED'])
            self.assertNotIn(b'token-should-not-be-retained', _evidence_bytes(root))


class MissingDependencyTests(unittest.TestCase):
    """Mocked host states: none of these start Blender, FFmpeg or a provider."""
    def setUp(self):
        if not _pinned_publication_supported():
            self.skipTest('pinned evidence publication is unavailable on this host; probe fails closed')

    def receipts(self, root, result):
        return {entry['operation']: json.loads((root/entry['probe_evidence'][0]['path']).read_text())
                for entry in result['value']['capabilities']}

    def test_missing_tools_credentials_and_pricing_are_precise_stable_and_secret_free(self):
        operations = ['blender.cpu', 'blender.gpu', 'blender.engines', 'importer.blend',
                      'codec.h264', 'codec.vp9', 'codec.av1', 'planning.provider', 'geometry.provider']
        secret = 'sk-director-fixture-' + 'f' * 24
        config = {'blender': None, 'ffmpeg': None, 'ffprobe': None,
                  'geometry_provider': {'id': 'geometry-fixture', 'configured': True}}
        fingerprints = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'DIRECTOR_FIXTURE_API_KEY': secret, 'HATCH_BLENDER': secret}), \
                patch.object(cap, '_worker', side_effect=AssertionError('worker started')):
            base = Path(directory).resolve()
            for name in ('first', 'second'):
                root = base/name
                # Local probes are requested for every native tool; providers never run.
                result = cap.probe(request(operations, operations[:7]), root, configuration=config)
                receipts = self.receipts(root, result)
                fingerprints.append({op: r['dependency_fingerprint'] for op, r in receipts.items()})
                for operation in operations[:4]:
                    self.assertEqual(receipts[operation]['reason'], 'missing-blender')
                for operation in operations[4:7]:
                    self.assertEqual(receipts[operation]['reason'], 'missing-ffmpeg-or-ffprobe')
                self.assertEqual(receipts['planning.provider']['reason'], 'missing-planning-credentials')
                self.assertEqual(receipts['geometry.provider']['reason'], 'remote-execution-not-probed')
                self.assertEqual(receipts['geometry.provider']['price_availability'], 'unknown')
                self.assertEqual(receipts['blender.gpu']['fallback'],
                                 'blender.cpu-requires-separate-probe-and-explicit-policy')
                codes = [(d['entity_id'], d['code']) for d in result['diagnostics']]
                for operation in operations[:4]:
                    self.assertIn((operation, 'CAPABILITY_MISSING_BLENDER'), codes)
                for operation in operations[4:7]:
                    self.assertIn((operation, 'CAPABILITY_MISSING_FFMPEG_OR_FFPROBE'), codes)
                self.assertIn(('planning.provider', 'CAPABILITY_MISSING_PLANNING_CREDENTIALS'), codes)
                self.assertIn(('planning.provider', 'CAPABILITY_PRICE_UNKNOWN'), codes)
                self.assertIn(('geometry.provider', 'CAPABILITY_PRICE_UNKNOWN'), codes)
                for receipt in receipts.values():
                    self.assertEqual(receipt['dependency_fingerprint'], object_hash(receipt['dependencies']))
                evidence = _evidence_bytes(root)
                self.assertNotIn(secret.encode(), evidence)
                self.assertNotIn(str(base).encode(), evidence)
            self.assertEqual(fingerprints[0], fingerprints[1])

    def test_missing_gpu_is_a_precise_diagnostic_with_explicit_fallback(self):
        import sys

        def no_gpu(argv, directory, timeout):
            # Exactly what the fixed Blender GPU script writes before failing.
            (Path(directory)/'failure.json').write_text(json.dumps({'reason': 'gpu-unavailable'}))
            return {'success': False, 'reason': 'worker-failed', 'exit_code': 1}, ''

        with tempfile.TemporaryDirectory() as directory, patch.object(cap, '_worker', side_effect=no_gpu):
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(['blender.gpu'], ['blender.gpu']), root,
                               configuration={'blender': sys.executable})
            receipt = self.receipts(root, result)['blender.gpu']
            self.assertEqual((receipt['discovered'], receipt['status'], receipt['reason']),
                             (True, 'unavailable', 'gpu-unavailable'))
            self.assertEqual(receipt['fallback'], 'blender.cpu-requires-separate-probe-and-explicit-policy')
            self.assertEqual([d['code'] for d in result['diagnostics']], ['CAPABILITY_GPU_UNAVAILABLE'])

    def test_blend_import_runs_blender_author_then_native_importer(self):
        import sys
        calls = []

        def worker(argv, directory, timeout):
            calls.append([Path(str(a)).name for a in argv])
            if len(calls) == 1:
                (Path(directory)/'triangle.blend').write_bytes(b'BLENDER-v405 synthetic fixture bytes')
                return {'success': True, 'reason': None, 'exit_code': 0}, 'DIRECTOR_PROBE={"version": "4.5.3 LTS"}\n'
            source = (Path(directory)/'probe.py').read_text()
            self.assertIn('load_character_model', source)
            self.assertIn(repr(str(Path(sys.executable).resolve())), source)
            return {'success': True, 'reason': None, 'exit_code': 0}, 'DIRECTOR_PROBE={"vertices": 3, "triangles": 1}\n'

        with tempfile.TemporaryDirectory() as directory, patch.object(cap, '_worker', side_effect=worker):
            root = Path(directory).resolve()/'evidence'
            result = cap.probe(request(['importer.blend'], ['importer.blend']), root,
                               configuration={'blender': sys.executable})
            receipt = self.receipts(root, result)['importer.blend']
            self.assertTrue(receipt['execution_verified'], result['diagnostics'])
            self.assertEqual(receipt['version'], '4.5.3 LTS')
            self.assertEqual(receipt['supported_formats'], ['BLEND'])
            self.assertEqual([p['media_type'] for p in receipt['payloads']], ['application/x-blender'])
            self.assertIn('rig_blender_worker.py', receipt['dependencies'])
            self.assertEqual(calls[0][-1], 'author.py')
            self.assertIn('--disable-autoexec', calls[0])
            self.assertEqual(calls[1][-1], 'probe.py')

    def test_blend_import_failures_are_typed(self):
        import sys
        for failing, reason in ((1, 'blend-authoring-failed'), (2, 'native-blend-import-failed')):
            calls = []

            def worker(argv, directory, timeout):
                calls.append(argv)
                if len(calls) == failing:
                    return {'success': False, 'reason': 'worker-failed', 'exit_code': 1}, ''
                (Path(directory)/'triangle.blend').write_bytes(b'BLENDER fixture')
                return {'success': True, 'reason': None, 'exit_code': 0}, 'DIRECTOR_PROBE={"version": "4.5.3"}\n'

            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory, \
                    patch.object(cap, '_worker', side_effect=worker):
                root = Path(directory).resolve()/'evidence'
                result = cap.probe(request(['importer.blend'], ['importer.blend']), root,
                                   configuration={'blender': sys.executable})
                receipt = self.receipts(root, result)['importer.blend']
                self.assertEqual((receipt['status'], receipt['reason']), ('unavailable', reason))
                self.assertEqual(receipt['payloads'], [])


if __name__ == '__main__':
    unittest.main()
