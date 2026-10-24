"""Actual DIRECT-to-Blender replay plus fail-closed input admission."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as p
from physics_rigid_render import render_direct_cache
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


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST') == '1', 'Explicit actual solver gate required')
class NativeReplayTests(unittest.TestCase):
    def test_direct_solved_cache_render_and_independent_reopen(self):
        world, sources = build_inputs(); world['solver']['backend'] = 'pybullet'
        world['frame_range']['end'] = 12
        adapter = p.PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
        prepared = adapter.prepare(world, sources)
        try:
            solved = adapter.simulate(prepared)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); adapter.bake(solved, root/'cache')
                before = (root/'cache'/'states.json').read_bytes()
                report = render_direct_cache(root/'cache', root/'replay', os.environ['HATCH_BLENDER'])
                self.assertEqual(report['reports'][1]['verified_frames'], 12)
                self.assertLessEqual(report['reports'][1]['maximum_replay_error'], 1e-5)
                self.assertEqual(len(report['reports'][0]['previews']), 3)
                self.assertEqual(before, (root/'replay'/'source-cache'/'states.json').read_bytes())
                self.assertTrue((root/'replay'/'scene.blend').is_file())
                for image in report['reports'][0]['previews']:
                    payload = (root/'replay'/image['path']).read_bytes()
                    self.assertEqual(payload[:8], b'\x89PNG\r\n\x1a\n')
                    self.assertEqual(p.digest(payload), image['sha256'])
                # Retained inputs are exact solver bytes; corrupt source never starts Blender.
                (root/'cache'/'states.json').write_bytes(before+b' ')
                with patch.object(p.BlenderBackend, '_discover') as discover:
                    with self.assertRaises(p.BackendError):
                        render_direct_cache(root/'cache', root/'corrupt-replay', os.environ['HATCH_BLENDER'])
                    discover.assert_not_called()
        finally:
            adapter.cleanup(prepared)
