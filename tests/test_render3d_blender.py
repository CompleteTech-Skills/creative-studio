"""Real process supervision and native source preparation for Render3D."""
from pathlib import Path
from copy import deepcopy
import hashlib
import os
import signal
import subprocess
import json
import time
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

    @unittest.skipUnless(os.name == 'posix' and Path('/proc').is_dir(),
                         'Owned process-group observation requires Linux procfs')
    def test_exited_leader_cannot_leave_descendant_or_output_pipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);pid_path=root/'child.pid'
            code=("import subprocess,sys;from pathlib import Path;"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                  "Path('child.pid').write_text(str(p.pid));print('leader finished',flush=True)")
            started=time.monotonic()
            try:
                payload,_=self.run_child(code,root)
                self.assertIn(b'leader finished',payload)
                self.assertLess(time.monotonic()-started,5,
                                'A descendant-held stdout pipe must not stall completed workers')
                pid=int(pid_path.read_text())
                status=Path('/proc',str(pid),'stat')
                # A killed orphan can briefly remain a zombie pending reaping.
                if status.exists():self.assertEqual(status.read_text().split()[2],'Z')
            finally:
                if pid_path.exists():
                    try:os.kill(int(pid_path.read_text()),signal.SIGKILL)
                    except ProcessLookupError:pass

    def test_failed_child_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(Render3DError) as caught:
                self.run_child("raise RuntimeError('worker failure fixture')",root)
            self.assertEqual(caught.exception.report['category'],'worker-crash')
            self.assertIn('worker failure fixture',(root/'blender.log').read_text())


@unittest.skipUnless(hasattr(os,'mkfifo'),'FIFO boundary tests require POSIX')
class SnapshotMetadataAdmission(unittest.TestCase):
    def run_boundary_child(self,root,second_read=False):
        code = """
import sys,json,os
from pathlib import Path
from unittest.mock import patch
from render3d_blender import BlenderBackend
import render3d_blender as module
from render3d_contracts import Render3DError
root=Path(sys.argv[1]);spec=json.loads((root/'request.json').read_text())
backend=object.__new__(BlenderBackend)
original=module.create_snapshot
def substituted(*args,**kwargs):
    result=original(*args,**kwargs)
    metadata=root/'snapshot/snapshot.json';metadata.unlink();os.mkfifo(metadata)
    return result
try:
    with patch.object(module,'create_snapshot',substituted if sys.argv[2]=='second' else original):
        backend.render_frames(spec,root/'snapshot',root/'output')
except Render3DError as error:
    assert error.report['category'] in ('missing-asset','stale-asset','invalid-input'),error.report
    assert not (root/'output').exists()
    print('typed rejection',error.report['category'])
else:raise AssertionError('Nonregular metadata must be rejected before worker execution')
"""
        environment=dict(os.environ,PYTHONPATH=str(ROOT/'scripts'))
        result=subprocess.run([sys.executable,'-c',code,str(root),'second' if second_read else 'initial'],
                              capture_output=True,text=True,timeout=20,env=environment)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('typed rejection',result.stdout)

    def test_initial_fifo_snapshot_is_bounded_public_admission(self):
        from test_render3d_contracts import request
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'snapshot').mkdir();os.mkfifo(root/'snapshot/snapshot.json')
            (root/'request.json').write_text(json.dumps(request()))
            self.run_boundary_child(root)

    def test_fifo_substitution_after_capture_rejects_without_publication(self):
        from rig_test_helpers import fixture
        from render3d_snapshot import create_snapshot,NativeRigAdapter
        from test_render3d_contracts import request,file
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'native').mkdir()
            rig=fixture(root/'native',animated=False)
            create_snapshot(rig.root,root/'snapshot',NativeRigAdapter())
            spec=request();spec['snapshot']=file(payload=(root/'snapshot/snapshot.json').read_bytes())
            (root/'request.json').write_text(json.dumps(spec))
            self.run_boundary_child(root,second_read=True)


class ScenePrefabPreparation(unittest.TestCase):
    def test_nested_prefabs_plan_exact_effective_nodes_and_geometry(self):
        from scene3d_example import build_example, node
        from scene3d_graph import SceneGraph
        from scene3d_prefabs import PrefabLibrary
        from scene3d_snapshot import snapshot_project
        from scene3d_contracts import load_project
        from scene3d_evaluate import evaluate_scene
        from render3d_snapshot import create_snapshot, Scene3DAdapter
        from render3d_blender import _prepare_scene
        from test_render3d_contracts import request
        import numpy as np
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';bundle=build_example(source)
            library=PrefabLibrary.from_files(source,bundle)
            outer=deepcopy(bundle['documents']['example-prefab.json'])
            outer.update(id='outer',nodes=outer['nodes'][:1],dependencies=[])
            child=node('inner','prefab_instance','prefab-root')
            child['prefab']=deepcopy(bundle['project']['prefabs'][0])
            child['prefab_overrides']=dict(parameters={},materials={},nodes=[])
            outer['nodes'].append(child)
            reference=library.register(outer)
            graph=SceneGraph.from_bundle(bundle,'example-scene.json',source_root=source)
            library.instantiate(graph,reference,'outer-instance')
            saved=snapshot_project(source,root/'persisted',graph,scene_path='example-scene.json',
                                   derived_documents={reference['path']:outer})
            persisted=load_project(root/'persisted')
            scene_ref=saved['active_scene'];shot_ref=persisted['project']['shots'][-1]
            create_snapshot(root/'persisted',root/'snapshot',Scene3DAdapter())
            payload=(root/'snapshot/snapshot.json').read_bytes()
            spec=request();spec.update(project={k:persisted['project'][k] for k in ('id','version')},
                scene={k:scene_ref[k] for k in ('id','version')},shot={k:shot_ref[k] for k in ('id','version')},
                fps=dict(numerator=24,denominator=1),frame_range=dict(start=0,end=2),
                source_start=dict(numerator=0,denominator=1),playback_rate=dict(numerator=1,denominator=1),
                snapshot=dict(path='snapshot.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()))
            workspace=root/'workspace';workspace.mkdir()
            result=_prepare_scene(spec,root/'snapshot',workspace)
            expected=evaluate_scene(root/'snapshot',scene_ref['path'],shot_ref['path'],0)
            self.assertEqual({n['id'] for n in result['nodes']},set(expected['nodes']))
            self.assertEqual(len([n for n in result['nodes'] if 'asset' in n]),2)
            self.assertGreater(result['geometry_vertices'],0)
            self.assertGreater(result['geometry_triangles'],0)
            self.assertTrue(all(len(n['frames'])==2 for n in result['nodes']))
            from scene3d_math import to_blender
            for entry in result['nodes']:
                if 'asset' in entry:
                    np.testing.assert_allclose(entry['frames'][0]['matrix'],
                        to_blender(np.asarray(expected['nodes'][entry['id']]['world_matrix'])))
            self.assertEqual(spec['scene'],{k:scene_ref[k] for k in ('id','version')})
            self.assertEqual(load_project(root/'persisted')['inventory'],persisted['inventory'])
            # A newly supported evaluator Pose must not silently render rest state.
            from unittest.mock import patch
            def bound_pose(*args,**kwargs):
                value=evaluate_scene(*args,**kwargs)
                next(iter(value['nodes'].values()))['native_pose']={'pose':'exact-bound-pose'}
                return value
            posed_workspace=root/'posed-workspace';posed_workspace.mkdir()
            with patch('scene3d_evaluate.evaluate_scene',side_effect=bound_pose), self.assertRaises(Render3DError) as caught:
                _prepare_scene(spec,root/'snapshot',posed_workspace)
            self.assertEqual(caught.exception.report['category'],'unsupported-feature')



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
