"""Real CPU rendering acceptance. The dedicated render gate enables this suite.

Fixture geometry is synthetic; successful execution is not production-art review.
"""
from copy import deepcopy
import hashlib
import os
import shutil
from pathlib import Path
import tempfile
import unittest

from rig_test_helpers import fixture
from render3d_contracts import Render3DError
from render3d_snapshot import NativeRigAdapter, create_snapshot


@unittest.skipUnless(os.environ.get('HATCH_RENDER3D_REAL_TESTS') == '1',
                     'Dedicated real-render gate requires HATCH_RENDER3D_REAL_TESTS=1')
class RealRenderAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from render3d_blender import BlenderBackend
        cls.backend=BlenderBackend()

    def setUp(self):
        evidence=os.environ.get('HATCH_RENDER3D_ACCEPTANCE_DIR')
        if evidence:
            parent=Path(evidence).absolute();parent.mkdir(parents=True,exist_ok=True)
            self.root=Path(tempfile.mkdtemp(prefix=self._testMethodName+'-',dir=parent))
        else:
            self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
            self.root=Path(self.temporary.name)
        (self.root/'native').mkdir()
        self.rig=fixture(self.root/'native')
        del self.rig.clips['neutral']
        from rig_jobs import save_rig
        save_rig(self.rig,operation='single-clip-render-acceptance')
        self.snapshot=self.root/'snapshot'
        create_snapshot(self.rig.root,self.snapshot,NativeRigAdapter())
        payload=(self.snapshot/'snapshot.json').read_bytes()
        self.request=dict(schema_version=1,kind='render3d-request',id='acceptance',
            project=dict(id='fixture',version='1.0.0'),scene=dict(id='fixture',version='1.0.0'),
            shot=dict(id='motion',version='1.0.0'),mode='3d',
            snapshot=dict(path='snapshot.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()),
            fps=dict(numerator=4,denominator=1),frame_range=dict(start=0,end=3),
            source_start=dict(numerator=0,denominator=1),playback_rate=dict(numerator=1,denominator=1),
            camera='front',priority=0,max_attempts=1,cache_policy='verify-reuse',
            destination=dict(path='render',owner_token='acceptance-owner'),
            settings=dict(engine='cycles',device_policy='cpu',device='cpu',allow_cpu_fallback=False,
                resolution=[64,64],samples=2,seed=4,denoise=False,
                output=dict(format='png',bit_depth=8,passes=['combined'],alpha='straight',color_space='srgb'),
                exposure=0,view_transform='standard',resources=dict(memory_bytes=2*1024**3,
                    storage_bytes=128*1024**2,timeout_seconds=120,workers=1,max_frames=10)))

    def test_actual_cpu_sequence_reinspection_and_corruption_rejection(self):
        from render3d_acceptance import inspect_render
        from render3d_verify import verify_result
        destination=self.root/'render'
        result=self.backend.render_frames(self.request,self.snapshot,destination)
        self.assertEqual(result['backend']['engine'],'cycles')
        self.assertEqual(result['backend']['device'],'cpu')
        self.assertEqual(result['verification']['status'],'passed')
        from rig_errors import read_json
        worker=read_json(destination/'worker-report.json')
        self.assertGreater(worker['geometry_vertices'],0)
        self.assertGreater(worker['armatures'],0)
        self.assertEqual(len(worker['samples']),3)
        import numpy as np
        first=worker['samples'][0]['world_vertices'];last=worker['samples'][-1]['world_vertices']
        self.assertEqual(set(first),set(last))
        displacement=max(float(np.max(np.abs(np.asarray(first[name])-np.asarray(last[name])))) for name in first)
        self.assertGreater(displacement,1e-5,'Actual Blender-evaluated vertices must deform')
        report=inspect_render(self.request,result,destination,self.snapshot,self.root/'evidence',
            expected_backend=self.backend.inspect_version())
        self.assertEqual(report['verification']['decoded_frames'],3)
        self.assertTrue(report['sampled_pixel_change'],'Articulated fixture must change real rendered pixels')
        self.assertEqual(len(report['sampled']),3)
        self.assertTrue((self.root/'evidence'/'contact-sheet.png').is_file())
        tampered=self.root/'tampered';shutil.copytree(destination,tampered)
        frame=tampered/result['images'][1]['file']['path']
        original=frame.read_bytes();frame.write_bytes(original[:-10])
        with self.assertRaises(Render3DError):
            verify_result(self.request,result,tampered,snapshot_root=self.snapshot,
                          expected_backend=self.backend.inspect_version())
        frame.write_bytes(original)
        missing=tampered/result['images'][0]['file']['path'];missing.unlink()
        with self.assertRaises(Render3DError):
            verify_result(self.request,result,tampered,snapshot_root=self.snapshot,
                          expected_backend=self.backend.inspect_version())

    def test_existing_output_and_resource_admission_preserve_user_content(self):
        destination=self.root/'existing';destination.mkdir();sentinel=destination/'user.txt';sentinel.write_text('preserve')
        with self.assertRaises(Render3DError):self.backend.render_frames(self.request,self.snapshot,destination)
        self.assertEqual(sentinel.read_text(),'preserve')
        bad=deepcopy(self.request);bad['settings']['resources']['memory_bytes']=1024
        with self.assertRaises(Render3DError):self.backend.render_frames(bad,self.snapshot,self.root/'limited')
        self.assertFalse((self.root/'limited').exists())

    def test_real_worker_cancellation_never_publishes_partial_output(self):
        with self.assertRaises(Render3DError) as caught:
            self.backend.render_frames(self.request,self.snapshot,self.root/'cancelled',cancelled=lambda:True)
        self.assertEqual(caught.exception.report['category'],'cancelled')
        self.assertFalse((self.root/'cancelled').exists())
