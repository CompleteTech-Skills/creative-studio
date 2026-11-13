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


class JobFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not jobs.publication_supported():raise RuntimeError('Explicit Linux job mechanics suite requires shared private staging')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir()
        paths=[]
        for i in range(3):
            path=self.source/f'{i}.png';Image.new('RGB',(16,16),(30+i*40,80,150)).save(path);paths.append(path.name)
        self.manifest=media.build_source(self.source,paths,{'numerator':24,'denominator':1},origin='synthetic')
        self.request=media.make_request(self.manifest)
        executable=str(Path(sys.executable).resolve());identity=jobs._sha_path(executable,jobs.MAX_TOOL_BYTES)
        self.synthetic_tools={name:{'path':executable,**identity,'version_stdout':'Explicit synthetic mechanics fixture; not FFmpeg execution'} for name in ('ffmpeg','ffprobe')}

    def create(self,name='job',**options):
        with mock.patch.object(jobs,'_tools',return_value=self.synthetic_tools),mock.patch.object(media,'capabilities',return_value={'status':'available','profiles':{p:{'status':'supported'} for p in media.PROFILES}}):
            return jobs.create(self.request,self.source,self.root/name,self.root/(name+'-delivery'),context={'fixture':'Synthetic mechanics only'},**options)

    def test_create_strict_identity_and_checkpoint(self):
        result=self.create();self.assertEqual(result['state']['phase'],'queued')
        self.assertEqual(jobs.status(self.root/'job')['plan'],result['plan'])
        plan=result['plan'];self.assertEqual(plan['id'],media._digest({k:v for k,v in plan.items() if k!='id'}))
        self.assertEqual(plan['context']['fixture'],'Synthetic mechanics only')

    def test_changed_request_or_state_never_reuses_identity(self):
        self.create();path=self.root/'job/plan.json';plan=json.loads(path.read_text());plan['request']['profile']='vp9-webm';path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError,'identity'):jobs.status(self.root/'job')

    def test_duplicate_job_owner_and_destination_no_overwrite(self):
        self.create()
        with jobs._locked(self.root/'job'):
            with self.assertRaisesRegex(ValueError,'owned'):
                with jobs._locked(self.root/'job'):pass
            self.assertTrue(jobs.status(self.root/'job')['owned'])
        with self.assertRaises(ValueError):self.create()
        self.assertFalse((self.root/'job-delivery').exists())

    def test_lost_active_owner_becomes_interrupted_without_dispatch(self):
        self.create()
        with jobs._locked(self.root/'job') as (fd,_,plan):
            state=jobs._state(fd,plan);jobs._save(fd,state,'encoding')
        with mock.patch.object(jobs.subprocess,'Popen',side_effect=AssertionError('status never starts encoding')):
            self.assertEqual(jobs.status(self.root/'job')['state']['phase'],'interrupted')

    def test_exact_cache_reuse_and_stale_or_corrupt_rejection(self):
        self.create()
        with jobs._locked(self.root/'job') as (fd,_,plan):
            state=jobs._state(fd,plan);jobs._cache(fd,plan,state)
            self.assertEqual(state['cached_created'],3)
            (self.source/'0.png').unlink()
            jobs._cache(fd,plan,state);self.assertEqual(state['cached_reused'],3)
            jobs._input_view(fd,plan)
            (self.root/'job/cache/000001.png').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'stale'):jobs._cache(fd,plan,state)
        self.assertFalse((self.root/'job-delivery').exists())

    def test_stale_original_before_snapshot_fails_without_final_output(self):
        self.create();(self.source/'0.png').write_bytes(b'changed')
        with jobs._locked(self.root/'job') as (fd,_,plan):
            with self.assertRaises(ValueError):jobs._cache(fd,plan,jobs._state(fd,plan))
        self.assertFalse((self.root/'job-delivery').exists())

    def test_unsafe_job_records_and_cancel_fifo_fail_without_blocking(self):
        self.create();root=self.root/'job';os.mkfifo(root/'cancel')
        with self.assertRaises(ValueError):jobs.cancel(root)
        (root/'cancel').unlink();(root/'state.json').unlink();os.mkfifo(root/'state.json')
        with self.assertRaises(ValueError):jobs.status(root)

    def test_invalid_resource_and_no_disk_admission(self):
        budget={**jobs.DEFAULT_BUDGET,'max_work_bytes':1024**2}
        with self.assertRaisesRegex(ValueError,'budget'):self.create(budget=budget)
        self.assertFalse((self.root/'job').exists())
        memory_budget={**jobs.DEFAULT_BUDGET,'max_memory_bytes':1024**2}
        with mock.patch.object(jobs,'_tools',side_effect=AssertionError('memory admission precedes tools')):
            with self.assertRaisesRegex(ValueError,'memory budget'):self.create(budget=memory_budget)
        self.assertFalse((self.root/'job').exists())
        with mock.patch.object(jobs.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()):
            with self.assertRaisesRegex(ValueError,'storage'):self.create()
        self.assertFalse((self.root/'job').exists())

    def test_foreign_root_and_unowned_partial_are_preserved(self):
        foreign=self.root/'foreign';foreign.mkdir();(foreign/'user.txt').write_text('keep')
        with self.assertRaises((ValueError,OSError)):jobs.cancel(foreign)
        self.assertEqual(sorted(p.name for p in foreign.iterdir()),['user.txt'])
        self.create();partial=self.root/'job/unowned.partial.mp4';partial.write_bytes(b'unknown')
        with jobs._locked(self.root/'job') as (fd,_,_):
            with self.assertRaisesRegex(ValueError,'unowned'):jobs._workspace_shape(fd)
        self.assertEqual(partial.read_bytes(),b'unknown')

    def test_changed_controller_or_encoder_identity_requires_new_job(self):
        result=self.create();changed=copy.deepcopy(result['plan']['implementation']);changed['controller']='0'*64
        with mock.patch.object(jobs,'_implementation',return_value=changed):
            with self.assertRaisesRegex(ValueError,'stale'):jobs.run(self.root/'job')
        self.assertEqual(jobs.status(self.root/'job')['state']['runs'],0)

    def test_malformed_checkpoint_references_fail_before_artifact_access(self):
        self.create()
        path=self.root/'job/state.json';original=json.loads(path.read_text())
        cases=[{'phase':[]},{'phase':'completed'},
               {'artifact':{}},{'receipt':{'bytes':1,'sha256':'bad'}},
               {'artifact':{'path':'../foreign','bytes':1,'sha256':'0'*64}}]
        for change in cases:
            with self.subTest(change=change):
                path.write_text(json.dumps({**original,**change}))
                with self.assertRaises(ValueError):jobs.status(self.root/'job')
        self.assertFalse((self.root/'job-delivery').exists())

    def test_storage_measurement_tolerates_owned_checkpoint_rename_only(self):
        self.create()
        with jobs._locked(self.root/'job') as (fd,_,_):
            expected=jobs._work_bytes(fd)
            jobs._write(fd,'.job-write-race',b'temporary checkpoint bytes')
            original=os.stat;removed=[]
            def remove_checkpoint(path,*args,**kwargs):
                if path=='.job-write-race' and not removed:
                    os.unlink(path,dir_fd=kwargs['dir_fd']);removed.append(True)
                return original(path,*args,**kwargs)
            with mock.patch.object(jobs.os,'stat',side_effect=remove_checkpoint):
                self.assertEqual(jobs._work_bytes(fd),expected)
            self.assertTrue(removed)
            outside=self.root/'foreign';outside.write_bytes(b'foreign')
            os.symlink(outside,self.root/'job/escape')
            with self.assertRaisesRegex(ValueError,'unsafe'):jobs._work_bytes(fd)

    def test_extreme_request_and_budget_numbers_fail_before_creation(self):
        huge=10**400
        for key in jobs.DEFAULT_BUDGET:
            with self.subTest(budget=key),mock.patch.object(jobs,'_tools',side_effect=AssertionError('admission precedes tools')):
                with self.assertRaises(ValueError):self.create(budget={**jobs.DEFAULT_BUDGET,key:huge})
        for section,key in [('limits','max_output_bytes'),('source','width')]:
            request=copy.deepcopy(self.request);request[section][key]=huge
            with self.subTest(request=(section,key)),mock.patch.object(jobs,'_tools',side_effect=AssertionError('admission precedes tools')):
                with self.assertRaises(ValueError):
                    jobs.create(request,self.source,self.root/'job',self.root/'job-delivery')
        self.assertFalse((self.root/'job').exists())

    def test_hidden_worker_cannot_signal_an_unowned_process_group(self):
        result=subprocess.run([sys.executable,str(Path(jobs.__file__)),'_worker'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,2);self.assertIn('owned session',result.stderr)

    def test_batch_duplicate_destination_rejected(self):
        self.create();other=self.create('other')
        path=self.root/'other/plan.json';plan=json.loads(path.read_text());plan['destination']=str(self.root/'job-delivery')
        plan['id']=media._digest({k:v for k,v in plan.items() if k!='id'});path.write_text(json.dumps(plan))
        state_path=self.root/'other/state.json';state=json.loads(state_path.read_text());state['job']=plan['id'];state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError,'destinations'):jobs.run_batch([self.root/'job',self.root/'other'])



if __name__=='__main__':unittest.main()
