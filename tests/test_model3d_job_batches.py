"""Actual fixed subprocess/tree limits and persistent native batch recovery."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import model3d_job_batches as batches
import model3d_job_worker as worker
from model3d_jobs import create_job,status as job_status
from rig_errors import RigError,atomic_json,object_hash,read_json,sha256


def request(kind='box'):
    return dict(engine='native-primitive',parameters=dict(kind=kind,parameters={},name='fixture',normal_mode='smooth',format='glb'))


@unittest.skipUnless(sys.platform.startswith('linux'),'hard workers fail explicitly on unsupported platforms')
class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def wait_worker(self,root,timeout=60):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            state=worker.state(root)
            if state['status'] not in ('queued','running') and not worker.alive(root):return state
            time.sleep(.03)
        self.fail('owned supervisor did not finish within bounded test runtime')

    def make_worker(self,diagnostic,**limits):
        root=self.root/diagnostic;job=self.root/(diagnostic+'-job');create_job(job,request())
        worker.launch(job,root,diagnostic=diagnostic,resource_limits=dict(runtime_seconds=20,**limits))
        return root,job

    def test_batch_failure_continuation_and_verified_reuse(self):
        root=self.root/'batch';source=self.root/'source';source.write_bytes(b'original')
        batches.create_batch(root,[dict(id='gooda',request=request(),inputs={'source.bin':source}),
                                   dict(id='bad',request=request('unsupported')),
                                   dict(id='goodb',request=request('sphere'))],concurrency=2,
                             resource_limits=dict(runtime_seconds=30,cpu_seconds=20))
        immutable=sha256(root/'batch-request.json');source.write_bytes(b'changed original')
        result=batches.run_batch(root)['journal']
        self.assertEqual(result['status'],'finished-with-errors')
        self.assertEqual([result['items'][n]['status'] for n in ('gooda','bad','goodb')],['completed','failed','completed'])
        for name in ('gooda','goodb'):
            item=result['items'][name];self.assertEqual(item['checkpoint']['attempts'],1)
            self.assertTrue(item['checkpoint']['output_files']);self.assertEqual(len(item['workers']),1)
            self.assertEqual(item['workers'][0]['outcome']['enforced']['gpu_quota'],None)
            self.assertEqual(len(item['workers'][0]['outcome']['enforced']['cpu_affinity']),1)
        intervals=[item['workers'][0]['outcome'] for item in result['items'].values()]
        for event in intervals:
            peak=sum(other['started_epoch']<=event['started_epoch']<other['finished_epoch'] for other in intervals)
            self.assertLessEqual(peak,2)
        again=batches.run_batch(root)['journal']
        self.assertEqual([len(again['items'][n]['workers']) for n in ('gooda','bad','goodb')],[1,1,1])
        self.assertEqual(sha256(root/'batch-request.json'),immutable)
        self.assertEqual((root/'jobs/gooda/inputs/source.bin').read_bytes(),b'original')

    def test_scheduler_process_exit_retains_owned_worker_and_checkpoint(self):
        root=self.root/'batch';batches.create_batch(root,[dict(id='first',request=request()),dict(id='second',request=request())])
        script=Path(batches.__file__).resolve()
        with (self.root/'scheduler.log').open('wb') as out:
            process=subprocess.Popen([sys.executable,'-I',str(script),'run','--root',str(root)],
                        cwd=str(script.parents[1]),stdout=out,stderr=subprocess.STDOUT)
            self.addCleanup(lambda:process.kill() if process.poll() is None else None)
            deadline=time.monotonic()+30;owned=None
            while time.monotonic()<deadline:
                value=batches.status(root)['journal']['items']['first'];owned=value['active_worker']
                if owned is not None and (root/owned['relative_root']/'state.json').exists():break
                if process.poll() is not None:self.fail((self.root/'scheduler.log').read_text())
                time.sleep(.01)
            self.assertIsNotNone(owned);process.kill();process.wait(timeout=5)
        self.wait_worker(root/owned['relative_root'])
        completed=batches.run_batch(root)['journal']
        self.assertEqual(completed['status'],'completed')
        self.assertEqual(len(completed['items']['first']['workers']),1)
        self.assertEqual(completed['items']['first']['checkpoint']['attempts'],1)
        self.assertEqual(completed['items']['second']['checkpoint']['attempts'],1)

    def test_hard_wall_runtime_stops_real_owned_worker(self):
        job=self.root/'job';root=self.root/'worker';create_job(job,request())
        worker.launch(job,root,diagnostic='sleep',resource_limits={'runtime_seconds':.3})
        state=self.wait_worker(root)
        self.assertEqual(state['status'],'timed-out');self.assertIsNone(job_status(job)['current_revision'])
        self.assertLess(state['wall_seconds'],10)

    def test_hard_memory_cpu_and_file_limits_are_reported(self):
        memory,_=self.make_worker('memory',memory_bytes=64*1024**2)
        state=self.wait_worker(memory);self.assertEqual(state['status'],'resource-exhausted',state)
        self.assertEqual(state['error']['code'],'MODEL3D_WORKER_MEMORY')
        cpu,_=self.make_worker('cpu',cpu_seconds=1)
        state=self.wait_worker(cpu);self.assertEqual(state['status'],'resource-exhausted',state)
        self.assertGreaterEqual(state['waited_cpu_seconds'],.8)
        file_,_=self.make_worker('file',file_bytes=65536)
        state=self.wait_worker(file_);self.assertEqual(state['status'],'resource-exhausted',state)
        self.assertLessEqual((file_/'diagnostic-large.bin').stat().st_size,65536)

    def test_cancellation_kills_owned_descendant_without_pid_guessing(self):
        root,job=self.make_worker('tree')
        deadline=time.monotonic()+30
        while not (root/'diagnostic-child.json').exists() and time.monotonic()<deadline:time.sleep(.02)
        child=read_json(root/'diagnostic-child.json')['pid'];identity=worker._identity(child)
        self.assertIsNotNone(identity);worker.cancel(root)
        state=self.wait_worker(root);self.assertEqual(state['status'],'cancelled')
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            actual=worker._identity(child)
            if actual is None:break
            text=(Path('/proc')/str(child)/'stat').read_text();state_=text[text.rfind(')')+2:].split()[0]
            if state_=='Z':break
            time.sleep(.02)
        else:self.fail('owned descendant still executing after cancellation')
        self.assertIsNone(job_status(job)['current_revision'])

    def test_unknown_quotas_and_unsafe_requests_fail_before_publication(self):
        with self.assertRaises(RigError):batches.create_batch(self.root/'bad',[dict(id='../bad',request=request())])
        with self.assertRaises(RigError):worker.limits({'gpu_memory_bytes':1})
        self.assertFalse((self.root/'bad').exists())
        with self.assertRaises(RigError):batches.create_batch(self.root/'bad',[],concurrency=100)

    def test_source_tampering_fails_only_affected_item(self):
        source=self.root/'source';source.write_bytes(b'original');root=self.root/'batch'
        batches.create_batch(root,[dict(id='changed',request=request(),inputs={'source.bin':source}),dict(id='valid',request=request())])
        (root/'jobs/changed/inputs/source.bin').write_bytes(b'tampered')
        result=batches.run_batch(root)['journal']
        self.assertEqual(result['status'],'finished-with-errors')
        self.assertEqual(result['items']['changed']['status'],'failed')
        self.assertEqual(result['items']['changed']['workers'],[])
        self.assertEqual(result['items']['valid']['status'],'completed')

    def test_birth_record_alone_cannot_signal_a_foreign_command(self):
        process=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)'],
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        try:
            identity=worker._identity(process.pid)
            self.assertIsNotNone(identity)
            self.assertFalse(worker.owned_identity(identity,self.root/'worker','execute'))
            with self.assertRaises(RigError) as caught:worker._stop(process,identity,self.root/'worker')
            self.assertEqual(caught.exception.report['code'],'MODEL3D_WORKER_OWNERSHIP')
            self.assertIsNone(process.poll())
        finally:
            if process.poll() is None:process.terminate()
            process.wait(timeout=5)

    def test_fixed_worker_rejects_request_replacement_before_engine_execution(self):
        original=self.root/'original';foreign=self.root/'foreign';root=self.root/'worker'
        create_job(original,request());create_job(foreign,request());root.mkdir()
        _,version=worker.engine_api()
        value=dict(schema_version=1,kind='model3d-owned-worker',token=uuid.uuid4().hex,
                   job_root=str(original),limits=worker.limits(),handler_version=version,retry=False,diagnostic=None)
        digest=object_hash(value)
        atomic_json(root/'request.json',dict(value,job_root=str(foreign)))
        process=subprocess.run([sys.executable,'-I',str(Path(worker.__file__).resolve()),'guard',
                    '--root',str(root),'--request-hash',digest],cwd=str(Path(worker.__file__).resolve().parents[1]),
                    capture_output=True,text=True,timeout=15)
        self.assertEqual(process.returncode,2)
        self.assertEqual(read_json(root/'state.json')['error']['code'],'MODEL3D_WORKER_CHANGED')
        self.assertEqual(job_status(original)['attempts'],0)
        self.assertEqual(job_status(foreign)['attempts'],0)


if __name__=='__main__':unittest.main()
