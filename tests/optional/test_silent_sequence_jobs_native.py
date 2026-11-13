"""Actual sequence controller/filesystem/decode gate; no mocked media output."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import silent_sequence_jobs as adapter

class ActualSequenceJobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('HATCH_SEQUENCE_JOBS_TEST')!='1':raise RuntimeError('Explicit actual sequence gate requires HATCH_SEQUENCE_JOBS_TEST=1')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source_root=self.root/'source';self.source_root.mkdir()
        paths=[]
        for i in range(3):
            name=f'{i}.png';Image.new('RGBA',(16,16),(i*40,90,100,180)).save(self.source_root/name);paths.append(name)
        self.source=adapter.sequence.build_source(self.source_root,paths,fps={'numerator':24,'denominator':1},
            output={'format':'png','bit_depth':8,'passes':['combined'],'alpha':'straight','color_space':'srgb'},
            identity={'resolution':[16,16],'fixture':'actual synthetic pixels; no render claim'})

    def create(self,label='job'):
        return adapter.create(self.source,self.source_root,self.root/label,self.root/(label+'-out'),provenance={'fixture':'Actual PNG sequence controller'})

    def test_actual_batch_and_complete_hash_inventory(self):
        self.create('a');self.create('b')
        result=adapter.run_batch([self.root/'a',self.root/'b'])
        self.assertEqual([r['state']['phase'] for r in result['jobs']],['completed','completed'],result)
        for label in ('a','b'):
            report=adapter.sequence.verify_sequence(self.root/(label+'-out'))
            self.assertEqual(len(report['decoded']),3)
            for frame in report['source']['frames']:
                self.assertEqual(hashlib.sha256((self.root/(label+'-out')/frame['path']).read_bytes()).hexdigest(),frame['sha256'])

    def test_retained_png16_exr_half_and_float_batches_preserve_precision(self):
        project=Path(__file__).resolve().parents[2]
        roots=[]
        for kind in ('png16','exr16','exr32'):
            original=project/'examples/silent-sequence235'/kind
            raw=(original/'sequence.json').read_bytes();prior=json.loads(raw)
            source=adapter.sequence.build_source(original,[f['path'] for f in prior['source']['frames']],
                fps=prior['source']['fps'],output=prior['source']['output'],
                identity={'resolution':prior['source']['resolution'],'retained_fixture_receipt_sha256':hashlib.sha256(raw).hexdigest()})
            job=self.root/kind;roots.append(job)
            adapter.create(source,original,job,self.root/(kind+'-out'),
                provenance={'fixture':'Explicit reimport of retained engineering master; no new render claim',
                            'original_receipt_sha256':hashlib.sha256(raw).hexdigest()})
        result=adapter.run_batch(roots)
        self.assertEqual([r['state']['phase'] for r in result['jobs']],['completed']*3,result)
        for kind in ('png16','exr16','exr32'):
            report=adapter.sequence.verify_sequence(self.root/(kind+'-out'))
            expected=32 if kind=='exr32' else 16
            self.assertTrue(all(d['bit_depth']==expected for d in report['decoded']))

    def test_actual_cancellation_retains_cache_and_retry_reuses_exact_bytes(self):
        self.create();calls=[]
        def cancel_after_one(state):
            calls.append(state['progress']['checkpoint'])
            if calls[-1]==3:adapter.cancel(self.root/'job')
        result=adapter.run(self.root/'job',progress=cancel_after_one)
        self.assertEqual(result['state']['phase'],'cancelled',result)
        self.assertFalse((self.root/'job-out').exists())
        stage=self.root/('.sequence-job-'+result['plan']['id'][:32]);first=stage/'frames/000000.png'
        before=(first.stat().st_ino,first.stat().st_mtime_ns,first.read_bytes())
        result=adapter.run(self.root/'job',retry=True)
        self.assertEqual(result['state']['phase'],'completed',result)
        self.assertEqual(result['state']['progress']['cached_reused'],1)
        self.assertEqual(result['state']['progress']['cached_created'],2)
        # Canonical final publication copies the private verified cache; the
        # persistent cache itself remains available for exact retry evidence.
        self.assertEqual((first.stat().st_ino,first.stat().st_mtime_ns,first.read_bytes()),before)

    def test_active_owner_and_corrupt_completed_output(self):
        self.create()
        with adapter._locked(self.root/'job') as (fd,plan):
            state=adapter._state(fd,plan);adapter._save(fd,state,'preparing')
            self.assertTrue(adapter.status(self.root/'job')['owned'])
            with self.assertRaisesRegex(ValueError,'owned'):adapter.run(self.root/'job')
        self.assertEqual(adapter.status(self.root/'job')['state']['phase'],'interrupted')
        self.assertEqual(adapter.run(self.root/'job',resume=True)['state']['phase'],'completed')
        (self.root/'job-out/frames/000000.png').write_bytes(b'corrupt')
        self.assertEqual(adapter.status(self.root/'job')['state']['phase'],'stale')

    def test_source_and_implementation_mismatch_preserve_outputs(self):
        self.create();(self.source_root/'0.png').write_bytes(b'changed')
        self.assertEqual(adapter.run(self.root/'job')['state']['phase'],'failed')
        self.assertFalse((self.root/'job-out').exists())
        with mock.patch.object(adapter,'_implementation',return_value={}):
            with self.assertRaisesRegex(ValueError,'stale'):adapter.run(self.root/'job',retry=True)

    def test_declared_memory_and_storage_budgets_fail_before_job_creation(self):
        for key in ('max_memory_bytes','max_work_bytes'):
            with self.subTest(key=key):
                budget={**adapter.DEFAULT_BUDGET,key:1024**2}
                with self.assertRaisesRegex(ValueError,'budget'):
                    adapter.create(self.source,self.source_root,self.root/'job',self.root/'out',
                        provenance={'fixture':'budget rejection'},budget=budget)
                self.assertFalse((self.root/'job').exists())

if __name__=='__main__':unittest.main()
