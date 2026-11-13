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

    def test_failed_child_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(Render3DError) as caught:
                self.run_child("raise RuntimeError('worker failure fixture')",root)
            self.assertEqual(caught.exception.report['category'],'worker-crash')
            self.assertIn('worker failure fixture',(root/'blender.log').read_text())


if __name__=='__main__':unittest.main()
