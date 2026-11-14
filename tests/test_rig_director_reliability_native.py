"""Explicit Linux Blender cancellation/resume evidence; run under native flock."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from director_job_graph import RenderGraphBindings
from test_rig_director_job_graph import graph_for_job
from render3d_jobs import JobStore
from render3d_blender import BlenderBackend
from render3d_verify import RenderArtifactVerifier
from render3d_snapshot import NativeRigAdapter, Scene3DAdapter, create_snapshot
from rig_errors import atomic_json
from rig_test_helpers import fixture
from rig_jobs import save_rig
from test_render3d_contracts import request, file


def descendants(pid):
    found = []
    pending = [pid]
    while pending:
        parent = pending.pop()
        try:
            children = Path('/proc/'+str(parent)+'/task/'+str(parent)+'/children').read_text().split()
        except FileNotFoundError:
            continue
        for child in children:
            child = int(child)
            found.append(child)
            pending.append(child)
    return found


@unittest.skipUnless(os.environ.get('HATCH_DIRECTOR31_NATIVE') == '1' and sys.platform == 'linux',
                     'Explicit Linux Blender acceptance requires HATCH_DIRECTOR31_NATIVE=1 and native queue lock')
class NativeReliabilityTests(unittest.TestCase):
    def test_real_worker_cancel_resume_and_tampered_cache_repair(self):
        evidence = os.environ.get('HATCH_DIRECTOR31_EVIDENCE')
        temporary = None if evidence else tempfile.TemporaryDirectory()
        self.addCleanup(lambda: temporary.cleanup() if temporary else None)
        root = Path(evidence or temporary.name)
        root.mkdir(parents=True, exist_ok=True)
        source = os.environ.get('HATCH_DIRECTOR31_SCENE_PROJECT')
        count = int(os.environ.get('HATCH_DIRECTOR31_FRAMES', '3'))
        self.assertGreaterEqual(count, 3)
        self.assertLessEqual(count, 60)
        if source:
            from scene3d_contracts import load_project
            bundle = load_project(source)
            project = bundle['project']
            shot_ref = next(item for item in project['shots'] if item['id']=='establish')
            shot = bundle['documents'][shot_ref['path']]
            create_snapshot(source, root/'snapshot', Scene3DAdapter())
        else:
            (root/'native').mkdir()
            rig = fixture(root/'native')
            del rig.clips['neutral']
            save_rig(rig, operation='director-reliability-native-evidence')
            create_snapshot(rig.root, root/'snapshot', NativeRigAdapter())
        req = request()
        req['snapshot'] = file(payload=(root/'snapshot/snapshot.json').read_bytes())
        req['id'] = 'director-reliability'
        req['frame_range'] = dict(start=0,end=count)
        if source:
            req['project'] = dict(id=project['id'],version=project['version'])
            req['scene'] = {key:shot['scene'][key] for key in ('id','version')}
            req['shot'] = {key:shot[key] for key in ('id','version')}
        req['source_start'] = dict(numerator=0,denominator=1)
        req['fps'] = dict(numerator=24,denominator=1)
        req['playback_rate'] = dict(numerator=1,denominator=1)
        req['camera'] = shot['camera'] if source else 'front'
        req['max_attempts'] = 4
        req['settings']['resolution'] = [640,360] if source else [64,64]
        req['settings']['samples'] = 16 if source else 64
        req['settings']['resources']['max_frames'] = count
        if source:
            req['settings']['seed'] = 0
            req['settings']['output']['alpha'] = 'opaque'
            req['settings']['resources']['timeout_seconds'] = 300
        req['settings']['resources']['memory_bytes'] = 2*1024**3
        backend = BlenderBackend()
        identity = backend.inspect_version()
        verify = RenderArtifactVerifier(root/'snapshot',identity).verify
        store = JobStore(root/'jobs')
        job = store.submit(req, root/'snapshot', identity)
        graph, bindings = graph_for_job(store,job)
        discovery_job = discovery_request = None
        if source:
            discovery_request = deepcopy(req)
            discovery_ref = next(item for item in project['shots'] if item['id']=='discovery')
            discovery = bundle['documents'][discovery_ref['path']]
            discovery_request.update(id='director-reliability-discovery',
                shot={key:discovery[key] for key in ('id','version')},camera=discovery['camera'],
                frame_range=dict(start=count,end=count*2))
            discovery_request['destination']['path']='render/discovery'
            discovery_job=store.submit(discovery_request,root/'snapshot',identity)
            other_graph,other_bindings=graph_for_job(store,discovery_job)
            other_graph['nodes'][0]['id']='render-discovery'
            graph['nodes'].extend(other_graph['nodes'])
            bindings['render-discovery']=other_bindings['render']
        if source:
            project_path=root/'snapshot/project.json'
            from rig_errors import sha256
            project_input=dict(artifact_id='director-project-revision',kind='scene3d-project',schema_version=1,
                semantic_version=project['version'],path='snapshot/project.json',sha256=sha256(project_path),
                byte_size=project_path.stat().st_size,media_type='application/json',provenance_ref=None,fixture=True,dependencies=[])
            for node in graph['nodes']:node['inputs'].append(deepcopy(project_input))
        graphs = RenderGraphBindings(store)
        graph_receipt = graphs.register('director-mvp',graph,bindings,root)
        self.assertEqual(RenderGraphBindings(JobStore(root/'jobs')).status('director-mvp')['state'],'queued')
        first = store.run_one(backend, verify, job=job)
        self.assertEqual(first['lease']['frame'], 0)
        code = '''import sys
from render3d_jobs import JobStore
from render3d_blender import BlenderBackend
from render3d_verify import RenderArtifactVerifier
from render3d_contracts import Render3DError
store=JobStore(sys.argv[1]);backend=BlenderBackend()
try:store.run_one(backend,RenderArtifactVerifier(sys.argv[2],backend.inspect_version()).verify,job=sys.argv[3])
except Render3DError as error:
 print(error.report['category'],flush=True)
 sys.exit(0 if error.report['category']=='cancelled' else 2)
raise AssertionError('Actual worker completed before cancellation')
'''
        process = subprocess.Popen([sys.executable,'-c',code,str(root/'jobs'),str(root/'snapshot'),job],
                                   stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic()+60
        native_pids = []
        while process.poll() is None and time.monotonic()<deadline:
            for pid in descendants(process.pid):
                try:
                    executable = os.readlink('/proc/'+str(pid)+'/exe')
                except FileNotFoundError:
                    continue
                try:
                    command = Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
                except FileNotFoundError:
                    continue
                if Path(executable).name.startswith('blender') and any(b'render3d_blender_worker.py' in part for part in command):
                    native_pids.append(pid)
            if native_pids:
                break
            time.sleep(.01)
        self.assertTrue(native_pids, 'No real Blender descendant observed; cancellation not proven')
        state = store.status(job)
        running_graph = graphs.status('director-mvp',expected_sha256=graph_receipt['graph_sha256'])
        self.assertEqual(running_graph['state'],'running')
        self.assertTrue(any(frame['pid']==process.pid for frame in running_graph['nodes']['render']['frames']))
        store.cancel(job,expected_revision=state['revision'])
        log,_ = process.communicate(timeout=60)
        (root/'cancel-worker.log').write_text(log)
        self.assertEqual(process.returncode,0,log)
        self.assertIn('cancelled',log)
        self.assertTrue(all(not Path('/proc/'+str(pid)).exists() for pid in native_pids), 'Cancelled native worker survived')
        state = store.status(job)
        self.assertEqual(state['state'],'cancelled')
        receipt = graphs.resume_node('director-mvp','render',verify,graph_revision=graph_receipt['revision'],
            graph_sha256=graph_receipt['graph_sha256'],job_revision=state['revision'])
        self.assertEqual(receipt['verified_preserved_frames'],[0])
        self.assertEqual(receipt['pending_frames'],list(range(1,count)))
        while store.run_one(backend,verify,job=job) is not None:
            pass
        frame = store.status(job)['frames'][0]
        cache = root/'jobs/cache'/frame['cache_key']
        record = json.loads((cache/'cache.json').read_text())
        (cache/record['result']['images'][0]['file']['path']).write_bytes(b'corrupt cached frame')
        missing_frame = store.status(job)['frames'][1]
        missing_cache = root/'jobs/cache'/missing_frame['cache_key']
        missing_record = json.loads((missing_cache/'cache.json').read_text())
        (missing_cache/missing_record['result']['images'][0]['file']['path']).unlink()
        state = store.status(job)
        repaired = graphs.resume_node('director-mvp','render',verify,graph_revision=graph_receipt['revision'],
            graph_sha256=graph_receipt['graph_sha256'],job_revision=state['revision'])
        self.assertEqual(repaired['verified_preserved_frames'],list(range(2,count)))
        self.assertEqual(repaired['pending_frames'],[0,1])
        rerendered = []
        while (run := store.run_one(backend,verify,job=job)) is not None:
            rerendered.append(run['lease']['frame'])
        self.assertEqual(rerendered,[0,1])
        delivered = store.materialize(job,root/'delivery',verify)
        decoded = verify(req,delivered,root/'delivery')
        self.assertEqual(decoded['decoded_frames'],count)
        discovery_verification = None
        if discovery_job:
            while store.run_one(backend,verify,job=discovery_job) is not None:
                pass
            discovery_result=store.materialize(discovery_job,root/'discovery-delivery',verify)
            discovery_verification=verify(discovery_request,discovery_result,root/'discovery-delivery')
            self.assertEqual(discovery_verification['decoded_frames'],count)
            self.assertEqual(graphs.status('director-mvp')['state'],'complete')
            self.assertEqual(len(graphs.status('director-mvp')['nodes']),2)
        atomic_json(root/'director-reliability-evidence.json', dict(schema_version=1,
            kind='director-native-reliability-proof',job=job,native_worker_pids=native_pids,
            source_scene_project=source,frame_count=count+(count if discovery_job else 0),graph=graphs.status('director-mvp'),
            discovery_verification=discovery_verification,
            interrupted_graph=running_graph,
            cancellation_log='cancel-worker.log',resume=receipt,tamper_repair=repaired,
            rerendered_frames=rerendered,corrupt_frames=[0],missing_frames=[1],verification=decoded,fixture=True,visual_review='not-performed'))
