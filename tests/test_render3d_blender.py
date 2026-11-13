"""Real process supervision and native source preparation for Render3D."""
from pathlib import Path
from copy import deepcopy
import hashlib
import os
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


@unittest.skipUnless(os.environ.get('HATCH_RENDER3D_REAL_TESTS') == '1',
                     'Actual Blender gate requires HATCH_RENDER3D_REAL_TESTS=1')
class NativeRendering(unittest.TestCase):
    def test_native_deformation_and_actual_png_exr_precision(self):
        from rig_test_helpers import fixture
        from rig_jobs import save_rig
        from rig_errors import read_json
        from render3d_blender import BlenderBackend
        from render3d_snapshot import create_snapshot, NativeRigAdapter
        from render3d_verify import decode_image
        import numpy as np
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'native').mkdir()
            rig=fixture(root/'native');del rig.clips['neutral']
            save_rig(rig,operation='native-render-regression')
            create_snapshot(rig.root,root/'snapshot',NativeRigAdapter())
            payload=(root/'snapshot'/'snapshot.json').read_bytes()
            request=dict(schema_version=1,kind='render3d-request',id='native-regression',
                project=dict(id='fixture',version='1.0.0'),scene=dict(id='fixture',version='1.0.0'),
                shot=dict(id='motion',version='1.0.0'),mode='3d',
                snapshot=dict(path='snapshot.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()),
                fps=dict(numerator=4,denominator=1),frame_range=dict(start=0,end=3),
                source_start=dict(numerator=0,denominator=1),playback_rate=dict(numerator=1,denominator=1),
                camera='front',priority=0,max_attempts=1,cache_policy='verify-reuse',
                destination=dict(path='render',owner_token='native-regression-owner'),
                settings=dict(engine='cycles',device_policy='cpu',device='cpu',allow_cpu_fallback=False,
                    resolution=[64,64],samples=2,seed=4,denoise=False,
                    output=dict(format='png',bit_depth=8,passes=['combined'],alpha='straight',color_space='srgb'),
                    exposure=0,view_transform='standard',resources=dict(memory_bytes=2*1024**3,
                        storage_bytes=128*1024**2,timeout_seconds=120,workers=1,max_frames=10)))
            backend=BlenderBackend()
            result=backend.render_frames(request,root/'snapshot',root/'render')
            self.assertEqual(result['verification']['status'],'passed')
            worker=read_json(root/'render/worker-report.json')
            self.assertGreater(worker['armatures'],0)
            first=worker['samples'][0]['world_vertices'];last=worker['samples'][-1]['world_vertices']
            self.assertGreater(max(float(np.max(np.abs(np.array(first[k])-np.array(last[k])))) for k in first),.01)
            decoded=[]
            for frame in (0,2):
                _,pixels=decode_image(root/f'render/frames/{frame:06d}.png',output=request['settings']['output'],resolution=[64,64])
                decoded.append(pixels)
            self.assertTrue(any(np.any(decoded[0][k] != decoded[1][k]) for k in decoded[0]))
            for name,fmt,depth,color in [('png16','png',16,'srgb'),('exr16','exr',16,'linear'),('exr32','exr',32,'linear')]:
                with self.subTest(format=name):
                    variant=deepcopy(request);variant['id']=name;variant['frame_range']['end']=1
                    variant['settings']['output'].update(format=fmt,bit_depth=depth,color_space=color)
                    variant['destination']['path']=name
                    output=backend.render_frames(variant,root/'snapshot',root/name)
                    self.assertEqual(output['verification']['status'],'passed')
                    metadata,_=decode_image(root/name/output['images'][0]['file']['path'],output=variant['settings']['output'],resolution=[64,64])
                    self.assertEqual(metadata['bit_depth'],depth)
                    self.assertEqual(set(metadata['channels']),set('RGBA'))


if __name__=='__main__':unittest.main()
