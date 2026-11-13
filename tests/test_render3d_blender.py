"""Real process supervision and native source preparation for Render3D."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from render3d_blender import _supervise, _prepare_native, MAX_LOG_BYTES
from render3d_contracts import Render3DError


class Supervision(unittest.TestCase):
    def run_child(self,code,root,**overrides):
        return _supervise([sys.executable,'-c',code],cwd=root,timeout=10,
                          memory_bytes=1024**3,storage_bytes=16*1024**2,**overrides)

    def test_success_and_bounded_real_child_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            payload,usage=self.run_child("import sys;sys.stdout.write('x'*(3*1024*1024))",root)
            self.assertEqual(len(payload),MAX_LOG_BYTES)
            self.assertEqual((root/'blender.log').read_bytes(),payload)
            self.assertGreater(usage['elapsed_seconds'],0)

    def test_cancel_terminates_real_child_and_retains_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(Render3DError) as caught:
                self.run_child("import time;print('started',flush=True);time.sleep(30)",root,cancelled=lambda:True)
            self.assertEqual(caught.exception.report['category'],'cancelled')
            self.assertTrue((root/'blender.log').exists())

    def test_timeout_terminates_child_and_retains_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(Render3DError) as caught:
                _supervise([sys.executable,'-c',"import time;print('waiting',flush=True);time.sleep(30)"],
                           cwd=root,timeout=.25,memory_bytes=1024**3,storage_bytes=16*1024**2)
            self.assertEqual(caught.exception.report['category'],'timeout')
            self.assertIn('waiting',(root/'blender.log').read_text())

    def test_storage_budget_terminates_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(Render3DError) as caught:
                _supervise([sys.executable,'-c',"from pathlib import Path;import time;Path('output').write_bytes(b'x'*4096);time.sleep(30)"],
                           cwd=root,timeout=10,memory_bytes=1024**3,storage_bytes=1024)
            self.assertEqual(caught.exception.report['category'],'storage-exhaustion')
            self.assertTrue((root/'blender.log').exists())

    @unittest.skipUnless(Path('/proc').is_dir(),'Process-tree RSS measurement requires procfs')
    def test_memory_budget_terminates_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(Render3DError) as caught:
                _supervise([sys.executable,'-c',"import time;payload=bytearray(32*1024**2);time.sleep(30)"],
                           cwd=root,timeout=10,memory_bytes=8*1024**2,storage_bytes=1024**2)
            self.assertEqual(caught.exception.report['category'],'system-memory')
            self.assertTrue((root/'blender.log').exists())

    def test_failed_child_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(Render3DError) as caught:
                self.run_child("raise RuntimeError('worker failure fixture')",root)
            self.assertEqual(caught.exception.report['category'],'worker-crash')
            self.assertIn('worker failure fixture',(root/'blender.log').read_text())


if __name__=='__main__':unittest.main()
