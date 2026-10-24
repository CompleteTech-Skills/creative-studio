"""Actual DIRECT-to-Blender replay plus fail-closed input admission."""
from contextlib import nullcontext
from copy import deepcopy
from io import BytesIO
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as p
from physics_rigid_render import render_direct_cache, _check_report, _unchanged, _renderer_identity, _png
from physics_backend_example import build_inputs


class ReplayAdmissionTests(unittest.TestCase):
    def test_missing_source_cannot_launch_renderer(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p.BlenderBackend, '_discover') as discover:
            with self.assertRaises(p.BackendError):
                render_direct_cache(Path(tmp)/'missing', Path(tmp)/'replay')
            discover.assert_not_called()
            self.assertFalse((Path(tmp)/'replay').exists())

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)/'replay'; destination.mkdir()
            (destination/'keep').write_bytes(b'original')
            with self.assertRaises(p.BackendError):
                render_direct_cache(Path(tmp)/'missing', destination)
            self.assertEqual((destination/'keep').read_bytes(), b'original')

    def test_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'real').mkdir(); (root/'link').symlink_to(root/'real', target_is_directory=True)
            with self.assertRaises(p.BackendError): render_direct_cache(root/'link', root/'out')


    def test_malformed_worker_reports_are_structured_rejections(self):
        binding = dict(cache_manifest_sha256='a'*64, states_sha256='b'*64)
        # These are adversarial report inputs, never solver execution receipts.
        for report in [None, [], {}, {'ok': True}, 1, 'invalid']:
            with self.subTest(report=report), self.assertRaises(p.BackendError):
                _check_report(report, 'render-replay', binding, 12, worker_sha256='c'*64, world_sha256='d'*64)

    def test_wrong_report_scalar_types_are_structured_rejections(self):
        binding = dict(cache_manifest_sha256='a'*64, states_sha256='b'*64, source_backend={'id': 'pybullet'})
        keys = {'ok', 'operation', 'backend', 'verified_frames', 'maximum_replay_error',
                'source_cache_sha256', 'states_sha256', 'previews', 'blend_sha256', 'scope',
                'artistic_review', 'worker_sha256', 'world_sha256', 'source_backend'}
        report = dict.fromkeys(keys)
        report.update(ok=True, operation='render-replay', source_cache_sha256='a'*64,
                      states_sha256='b'*64, worker_sha256='c'*64, world_sha256='d'*64,
                      source_backend=binding['source_backend'], verified_frames=12)
        for value in ['0', None, True, float('nan'), float('inf'), -1, 10**400]:
            report['maximum_replay_error'] = value
            with self.subTest(value=value), self.assertRaises(p.BackendError):
                _check_report(report, 'render-replay', binding, 12, worker_sha256='c'*64, world_sha256='d'*64)

    def test_native_identity_rejects_fabricated_platform_and_discovery(self):
        # Synthetic negative validator inputs; these are not execution evidence.
        native = dict(application_version='4.5.3 LTS', application_build=p.SUPPORTED_BLENDER_BUILD,
                      solver_name='Blender Bullet rigid-body world',
                      solver_version='not exposed separately by Blender', platform='linux', precision='mixed',
                      device='cpu', physics_gpu_support='unsupported', render_gpu_execution='not-executed',
                      render_device_discovery='not-available', render_devices=[])
        binding = dict(cache_manifest_sha256='a'*64, states_sha256='b'*64, source_backend={'id': 'pybullet'})
        report = dict(ok=True, operation='reopen-replay', backend=native, verified_frames=12,
                      maximum_replay_error=0, source_cache_sha256='a'*64, states_sha256='b'*64,
                      previews=[], blend_sha256='e'*64, scope='DIRECT solved-state replay; Blender rendering only',
                      artistic_review='not-performed', worker_sha256='c'*64, world_sha256='d'*64,
                      source_backend=binding['source_backend'])
        for key, value in [('platform', 'fabricated-os'), ('render_device_discovery', 'invented success')]:
            bad = deepcopy(report); bad['backend'][key] = value
            with self.subTest(key=key), self.assertRaises(p.BackendError):
                _check_report(bad, 'reopen-replay', binding, 12, worker_sha256='c'*64, world_sha256='d'*64)
        discovered = {key: native[key] for key in ('solver_name', 'solver_version', 'precision', 'device')}
        discovered.update(application_version='Blender 4.5.3 LTS', platform='Linux-test')
        for key, value in [('platform', 'Darwin-test'), ('application_version', 'Blender 4.5.2')]:
            bad = dict(discovered); bad[key] = value
            with self.subTest(discovery=key), self.assertRaises(p.BackendError):
                _renderer_identity(native, bad)
        changed = deepcopy(native); changed['render_device_discovery'] = 'executed-device-enumeration-only'
        with self.assertRaises(p.BackendError): _renderer_identity(changed, discovered, native)

    def test_png_header_alone_cannot_pass_native_preview_validation(self):
        from PIL import Image
        stream = BytesIO(); Image.new('RGBA', (128, 128), (0, 0, 0, 0)).save(stream, format='PNG')
        valid = stream.getvalue(); _png(valid)
        with self.assertRaises(p.BackendError): _png(valid[:24])
        with self.assertRaises(p.BackendError): _png(valid[:50]+b'corrupted')

    def test_private_binding_rejects_changed_bytes_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'worker.py').write_bytes(b'package bytes')
            bindings = {'worker.py': p.digest(b'package bytes')}
            _unchanged(root, bindings)
            (root/'worker.py').write_bytes(b'replaced')
            with self.assertRaises(p.BackendError): _unchanged(root, bindings)
            (root/'worker.py').unlink(); (root/'worker.py').symlink_to('/etc/hosts')
            with self.assertRaises(p.BackendError): _unchanged(root, bindings)


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST') == '1', 'Explicit actual solver gate required')
class NativeReplayTests(unittest.TestCase):
    def test_direct_solved_cache_render_and_independent_reopen(self):
        world, sources = build_inputs(backend='pybullet')
        world['gravity'] = [0, 0, 0]
        world['frame_range']['end'] = 25
        body = world['bodies'][0]
        body['linear_velocity'] = [.3, 0, 0]
        body['angular_velocity'] = [0, 0, .3]
        body['mass']['center_of_mass'] = [.1, 0, 0]
        body['mass']['inertia'] = [[.12, .02, 0], [.02, .14, 0], [0, 0, .15]]
        world['forces'] = [dict(id='offcenter-impulse', type='impulse', bodies=['ball'],
            vector=[.2, 0, 0], point=[0, 3.5, 0], frame_range=dict(start=0, end=1),
            strength=1, seed=0, categories=['rigid-body'], application='world-point'),
            dict(id='moderate-torque', type='torque', bodies=['ball'], vector=[0, 0, .02],
            point=[0, 0, 0], frame_range=dict(start=1, end=12), strength=1, seed=0,
            categories=['rigid-body'], application='center')]
        adapter = p.PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
        prepared = adapter.prepare(world, sources)
        try:
            solved = adapter.simulate(prepared)
            evidence = os.environ.get('HATCH_RIGID_REPLAY_EVIDENCE')
            if evidence:
                base = Path(evidence); base.mkdir(parents=True, exist_ok=True)
                retained = p._new_destination(base/self._testMethodName)
                retained.mkdir(mode=0o700)
                context = nullcontext(str(retained))
            else:
                context = tempfile.TemporaryDirectory()
            with context as tmp:
                root = Path(tmp); adapter.bake(solved, root/'cache')
                before = (root/'cache'/'states.json').read_bytes()
                report = render_direct_cache(root/'cache', root/'replay', os.environ['HATCH_BLENDER'])
                self.assertEqual(report['reports'][1]['verified_frames'], 25)
                self.assertLessEqual(report['reports'][1]['maximum_replay_error'], 1e-5)
                self.assertEqual(len(report['reports'][0]['previews']), 3)
                self.assertEqual(before, (root/'replay'/'source-cache'/'states.json').read_bytes())
                self.assertTrue((root/'replay'/'scene.blend').is_file())
                for image in report['reports'][0]['previews']:
                    payload = (root/'replay'/image['path']).read_bytes()
                    self.assertEqual(payload[:8], b'\x89PNG\r\n\x1a\n')
                    self.assertEqual(p.digest(payload), image['sha256'])
                # Retained inputs are exact solver bytes; corrupt source never starts Blender.
                shutil.copytree(root/'cache', root/'corrupt-cache')
                (root/'corrupt-cache'/'states.json').write_bytes(before+b' ')
                (root/'corrupt-cache'/'INTENTIONALLY-CORRUPT.txt').write_text(
                    'Negative admission test only. Not a verified solver cache.\n')
                with patch.object(p.BlenderBackend, '_discover') as discover:
                    with self.assertRaises(p.BackendError):
                        render_direct_cache(root/'corrupt-cache', root/'corrupt-replay', os.environ['HATCH_BLENDER'])
                    discover.assert_not_called()
        finally:
            adapter.cleanup(prepared)
