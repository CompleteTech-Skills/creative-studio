"""Mechanical job fixtures and separate real FFmpeg failure-injection gate."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import silent_jobs as jobs
import silent_video as media


class ActualMediaJobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('HATCH_MEDIA_JOBS_TEST')!='1':raise RuntimeError('Explicit actual FFmpeg job suite requires HATCH_MEDIA_JOBS_TEST=1')
        jobs._worker_boundary()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir();paths=[]
        for i in range(24):
            path=self.source/f'{i}.png';Image.new('RGB',(32,32),(20+i*3,90,150)).save(path);paths.append(path.name)
        self.manifest=media.build_source(self.source,paths,{'numerator':24,'denominator':1},origin='synthetic')

    def create(self,name='job',profile='h264-mp4',**options):
        return jobs.create(media.make_request(self.manifest,profile),self.source,self.root/name,self.root/(name+'-delivery'),context={'fixture':'Actual FFmpeg with explicitly synthetic RGB frames; no renderer'},**options)

    def test_real_job_complete_and_portable_receipt(self):
        self.create();result=jobs.run(self.root/'job')
        self.assertEqual(result['state']['phase'],'completed',result)
        self.assertEqual(result['state']['cached_created'],24)
        receipt=json.loads((self.root/'job-delivery/receipt.json').read_text())
        self.assertEqual(receipt['media']['audio_stream_count'],0);self.assertEqual(receipt['media']['video_stream_count'],1)
        self.assertEqual(receipt['media']['frame_count'],24)
        self.assertEqual(jobs.status(self.root/'job')['state']['phase'],'completed')
        (self.root/'job-delivery/video.mp4').write_bytes(b'corruption')
        self.assertEqual(jobs.status(self.root/'job')['state']['phase'],'stale')

    def test_real_sequential_mp4_webm_batch_and_no_repeat(self):
        self.create('mp4');self.create('webm','vp9-webm')
        report=jobs.run_batch([self.root/'mp4',self.root/'webm'])
        self.assertEqual([r['state']['phase'] for r in report['jobs']],['completed','completed'],report)
        again=jobs.run_batch([self.root/'mp4',self.root/'webm'])
        self.assertEqual([r['state']['attempts'] for r in again['jobs']],[1,1])
        for label in ('mp4','webm'):
            receipt=json.loads((self.root/(label+'-delivery')/'receipt.json').read_text())
            self.assertEqual(receipt['media']['audio_stream_count'],0)

    def _crash_worker_after(self,phase,cancel_before_resume=False,expected_attempts=1):
        self.create()
        with jobs._locked(self.root/'job') as (fd,lock,plan):
            state=jobs._state(fd,plan);state['runs']=1;jobs._save(fd,state,'validating')
            read_fd,write_fd=os.pipe();pid=os.fork()
            if pid==0:
                os.close(write_fd);os.setsid();real_save=jobs._save
                if phase=='independent-probe':
                    def interrupted_probe(path,*args,**kwargs):
                        if not Path(path).is_file() or Path(path).stat().st_size==0:os._exit(75)
                        (self.root/'probe-interrupted.txt').write_text('actual encoded artifact reached independent probe')
                        os._exit(74)
                    media.verify_export=interrupted_probe
                def interrupted(descriptor,checkpoint,next_phase=None,**values):
                    if next_phase==phase:
                        if phase=='completed':os._exit(74)
                        real_save(descriptor,checkpoint,next_phase,**values);os._exit(74)
                    return real_save(descriptor,checkpoint,next_phase,**values)
                jobs._save=interrupted
                os._exit(jobs._worker(fd,read_fd,lock,False))
            os.close(read_fd)
            try:self.assertEqual(os.waitpid(pid,0)[1]>>8,74)
            finally:os.close(write_fd)
        self.assertEqual(jobs.status(self.root/'job')['state']['phase'],'interrupted')
        if cancel_before_resume:
            jobs.cancel(self.root/'job');result=jobs.retry(self.root/'job')
        else:result=jobs.resume(self.root/'job')
        self.assertEqual(result['state']['phase'],'completed',result)
        self.assertEqual(result['state']['attempts'],expected_attempts)
        self.assertEqual(result['state']['cached_reused']+result['state']['cached_created'],24)
        if phase not in ('validating','preparing'):self.assertEqual(result['state']['cached_reused'],24)
        return result

    def test_actual_encode_survives_crash_before_finalization(self):self._crash_worker_after('finalizing')
    def test_actual_delivery_survives_crash_before_checkpoint_ack(self):self._crash_worker_after('completed')
    def test_preflight_interruption_resumes(self):self._crash_worker_after('validating')
    def test_encoding_interruption_restarts_complete_attempt(self):self._crash_worker_after('encoding',expected_attempts=2)
    def test_independent_probe_interruption_restarts_complete_attempt(self):
        self._crash_worker_after('independent-probe',expected_attempts=2)
        self.assertTrue((self.root/'probe-interrupted.txt').is_file())

    def test_preparation_interrupt_cancellation_retry_and_cache_reuse(self):
        self._crash_worker_after('preparing',cancel_before_resume=True)

    def test_cancel_after_actual_encoder_progress_then_retry(self):
        paths=[]
        for i in range(120):
            path=self.source/f'noise-{i}.png'
            Image.effect_noise((128,128),100).convert('RGB').save(path);paths.append(path.name)
        self.manifest=media.build_source(self.source,paths,{'numerator':24,'denominator':1},origin='synthetic')
        encoding={'threads':1,'gop':24,'speed':'veryslow','rate_control':{'mode':'quality','value':18}}
        request=media.make_request(self.manifest,encoding=encoding)
        jobs.create(request,self.source,self.root/'job',self.root/'job-delivery',context={'fixture':'Actual cancellation of FFmpeg on synthetic noise'})
        cancelled=[False]
        def on_progress(state):
            event=state['progress']
            if event.get('phase')=='encoding' and event.get('frame',0)>0 and not cancelled[0]:
                cancelled[0]=True;jobs.cancel(self.root/'job')
        result=jobs.run(self.root/'job',progress=on_progress)
        self.assertTrue(cancelled[0],'actual encoder progress was required to inject cancellation')
        self.assertEqual(result['state']['phase'],'cancelled',result)
        self.assertFalse((self.root/'job-delivery').exists())
        retried=jobs.retry(self.root/'job')
        self.assertEqual(retried['state']['phase'],'completed',retried)
        self.assertEqual(retried['state']['cached_reused'],120)
        self.assertEqual(retried['state']['attempts'],2)

    def test_parent_death_stops_owned_worker_and_actual_ffmpeg(self):
        paths=[]
        for i in range(120):
            path=self.source/f'parent-noise-{i}.png'
            Image.effect_noise((128,128),100).convert('RGB').save(path);paths.append(path.name)
        source=media.build_source(self.source,paths,{'numerator':24,'denominator':1},origin='synthetic')
        request=media.make_request(source,encoding={'threads':1,'gop':24,'speed':'veryslow',
            'rate_control':{'mode':'quality','value':18}})
        jobs.create(request,self.source,self.root/'job',self.root/'job-delivery')
        controller=subprocess.Popen([sys.executable,str(Path(jobs.__file__)),'run','--job-root',str(self.root/'job')],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        owned=[]
        def children(pid):
            try:return [int(value) for value in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
            except FileNotFoundError:return []
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline and controller.poll() is None:
                workers=children(controller.pid)
                for worker in workers:
                    for child in children(worker):
                        try:command=Path(f'/proc/{child}/cmdline').read_bytes()
                        except FileNotFoundError:continue
                        if b'ffmpeg' in command and b'-progress' in command:
                            owned=[worker,child];break
                    if owned:break
                if owned:break
                time.sleep(.01)
            self.assertTrue(owned,'actual owned FFmpeg process must execute before parent-death injection')
            controller.kill();controller.wait(timeout=5)
            deadline=time.monotonic()+5
            def executing(pid):
                try:return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0]!='Z'
                except FileNotFoundError:return False
            while time.monotonic()<deadline and any(executing(pid) for pid in owned):time.sleep(.01)
            self.assertFalse(any(executing(pid) for pid in owned),'lifetime guardian must stop its complete owned process group')
            self.assertEqual(jobs.status(self.root/'job')['state']['phase'],'interrupted')
            self.assertFalse((self.root/'job-delivery').exists())
        finally:
            if controller.poll() is None:controller.kill();controller.wait(timeout=5)
            if owned:
                try:
                    command=Path(f'/proc/{owned[0]}/cmdline').read_bytes()
                    if (str(Path(jobs.__file__)).encode() in command and b'_worker' in command
                            and os.getpgid(owned[0])==owned[0]):
                        os.killpg(owned[0],signal.SIGKILL)
                except ProcessLookupError:pass
                except FileNotFoundError:pass


if __name__=='__main__':unittest.main()
