import sys
from pathlib import Path
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import silent_sequence_jobs as adapter

class SequenceJobContracts(unittest.TestCase):
    def test_unavailable_staging_does_not_touch_inputs(self):
        with mock.patch.object(adapter.sequence,'publication_supported',return_value=False),mock.patch.object(adapter.sequence,'_check_source',side_effect=AssertionError('no admission')):
            with self.assertRaisesRegex(ValueError,'unavailable'):adapter.create({},'.','job','out',provenance={})

    def test_batch_bounds_and_duplicates_precede_io(self):
        for roots in ([],['same','same'],['x']*17,'not-a-list'):
            with self.subTest(roots=roots),mock.patch.object(adapter,'status',side_effect=AssertionError('no IO')):
                with self.assertRaises(ValueError):adapter.run_batch(roots)

    def test_batch_failure_does_not_prevent_independent_job(self):
        inventories=[{'plan':{'destination':str(i)},'state':{'phase':'queued'}} for i in range(2)]
        with mock.patch.object(adapter,'status',side_effect=inventories),mock.patch.object(adapter,'run',side_effect=[ValueError('bad source'),{'state':{'phase':'completed'}}]):
            result=adapter.run_batch(['a','b'])
        self.assertEqual(result['concurrency'],1)
        self.assertEqual(result['jobs'][0]['phase'],'blocked')
        self.assertEqual(result['jobs'][1]['state']['phase'],'completed')

    def test_malformed_checkpoint_has_a_typed_failure(self):
        valid={'schema_version':1,'job':'a'*64,'phase':'queued','runs':0,
               'progress':{},'error':None,'receipt_sha256':None}
        for key,value in [('phase',[]),('phase',None),('schema_version',True),('runs',True),('progress',[]),('receipt_sha256','bad')]:
            with self.subTest(key=key,value=value),mock.patch.object(adapter.jobs,'_json',return_value={**valid,key:value}):
                with self.assertRaises(ValueError):adapter._state(0,{'id':'a'*64})

if __name__=='__main__':unittest.main()
