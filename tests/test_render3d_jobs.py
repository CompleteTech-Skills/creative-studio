"""Real persistence, process ownership and independent image-cache checks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from render3d_jobs import JobStore
from render3d_contracts import Render3DError, validate
from render3d_snapshot import Scene3DAdapter, create_snapshot
from scene3d_example import build_example
from rig_errors import object_hash, atomic_json
from test_render3d_contracts import request, backend, file


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        build_example(self.root/'source')
        create_snapshot(self.root/'source',self.root/'snapshot',Scene3DAdapter())
        self.request=request();self.request['scene']['id']='example-scene';self.request['shot']['id']='example-shot'
        self.request['snapshot']=file(payload=(self.root/'snapshot/snapshot.json').read_bytes())
        self.request['frame_range']={'start':5,'end':7};self.request['settings']['resolution']=[8,8]
        self.store=JobStore(self.root/'jobs');self.backend=backend()

    def tearDown(self):self.tmp.cleanup()

    def submit(self,value=None):return self.store.submit(value or self.request,self.root/'snapshot',self.backend)

    def output(self,lease):
        from PIL import Image
        output=self.root/('output-'+lease['token']);output.mkdir()
        Image.new('RGBA',(8,8),(64,128,192,255)).save(output/'frame.png')
        atomic_json(output/'manifest.json',{'synthetic_fixture':True,'frame':lease['frame']})
        atomic_json(output/'verification.json',{'decoded_by_test':True})
        ref=lambda p:file(p,(output/p).read_bytes())
        req=self.store.frame_request(lease)
        value=dict(schema_version=1,kind='render3d-result',job='fixture',request_sha256=object_hash(req),
          snapshot_sha256=lease['spec']['snapshot_sha256'],backend=self.backend,status='complete',
          requested_frames=[lease['frame']],successful=[lease['frame']],failed=[],skipped=[],reused=[],
          images=[dict(frame=lease['frame'],file=ref('frame.png'),format='png',bit_depth=8,pass_name='combined',resolution=[8,8])],
          diagnostics=[],usage=dict(elapsed_seconds=0,peak_memory_bytes=None,output_bytes=None,measurement='unavailable'),
          logs=[],manifest=ref('manifest.json'),verification=dict(status='passed',verifier=dict(id='test-pillow',version='1.0.0',code_sha256='0'*64,platform='test'),evidence=ref('verification.json')))
        validate(value,'RenderResult');return output,value

    def verify(self,req,result,root):
        from PIL import Image
        self.assertEqual(result['request_sha256'],object_hash(req))
        for item in result['images']:
            payload=(root/item['file']['path']).read_bytes()
            if hashlib.sha256(payload).hexdigest()!=item['file']['sha256']:raise ValueError('hash mismatch')
            with Image.open(root/item['file']['path']) as image:
                image.load();self.assertEqual(image.size,tuple(req['settings']['resolution']))
        return {'decoded':True,'verifier':dict(id='test-pillow',version='1.0.0',code_sha256='0'*64,platform='test')}

    def test_idempotent_submission_and_destination_ownership(self):
        job=self.submit();self.assertEqual(job,self.submit())
        changed=deepcopy(self.request);changed['id']='different'
        with self.assertRaises(Render3DError):self.submit(changed)
        self.assertEqual(len(self.store.status(job)['frames']),2)

    def test_priority_capacity_and_stale_leases(self):
        low=self.submit()
        high=deepcopy(self.request);high['id']='high';high['priority']=90;high['destination']['path']='other'
        highjob=self.submit(high)
        lease=self.store.claim();self.assertEqual(lease['job'],highjob)
        self.assertIsNone(self.store.claim(low))
        self.store.fail(lease,'retry',retryable=True)
        with self.assertRaises(Render3DError):self.store.heartbeat(lease)
        again=self.store.claim(highjob);self.assertEqual(again['frame'],lease['frame'])
        self.store.fail(again,'again',retryable=True)
        self.assertEqual(self.store.status(highjob)['frames'][0]['state'],'failed')

    def test_live_pid_not_stolen_on_expired_heartbeat(self):
        job=self.submit();lease=self.store.claim(job)
        with self.store._transaction() as db:
            db.execute('UPDATE frames SET heartbeat=0 WHERE job=?',(job,))
        self.assertEqual(self.store.recover(job),[])
        self.assertTrue(self.store.heartbeat(lease) is False)

    def test_dead_process_recovery_preserves_attempts(self):
        job=self.submit()
        code="from render3d_jobs import JobStore;import sys;print(JobStore(sys.argv[1]).claim(sys.argv[2])['frame'])"
        result=subprocess.run([sys.executable,'-c',code,str(self.root/'jobs'),job],env={**__import__('os').environ,'PYTHONPATH':str(ROOT/'scripts')},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.store.recover(job),[(job,5)])
        self.assertEqual(self.store.status(job)['frames'][0]['attempts'],1)
        self.assertEqual(self.store.claim(job)['frame'],5)

    def test_cancel_revision_and_resume_preserves_completed_frame(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        with self.assertRaises(Render3DError):self.store.cancel(job,expected_revision=0)
        self.store.cancel(job,expected_revision=self.store.status(job)['revision'])
        self.assertEqual([f['state'] for f in self.store.status(job)['frames']],['complete','cancelled'])
        self.store.resume(job,verifier=self.verify)
        self.assertEqual(self.store.claim(job)['frame'],6)

    def test_independent_decode_blocks_tampered_outputs_and_caches(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        (output/'frame.png').write_bytes(b'broken')
        with self.assertRaises(ValueError):self.store.publish(lease,output,result,self.verify)
        self.assertEqual(self.store.status(job)['frames'][0]['state'],'running')
        self.assertFalse((self.root/'jobs/cache'/lease['cache_key']).exists())
        self.store.fail(lease,'bad image')

    def test_relocated_destination_reuses_original_verified_provenance(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        changed=deepcopy(self.request);changed['id']='relocated';changed['destination']['path']='elsewhere'
        other=self.submit(changed);second=self.store.claim(other)
        self.assertEqual(second['cache_key'],lease['cache_key'])
        self.assertTrue(self.store.reuse(second,self.verify))
        record=self.store.status(other)
        self.assertEqual(record['frames'][0]['state'],'complete')
        self.assertTrue(any(e['event']=='cache-reused' for e in record['events']))

    def test_source_changes_cannot_reuse_cached_pixels(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        changed=deepcopy(self.request);changed['id']='relocated';changed['destination']['path']='elsewhere'
        other=self.submit(changed);second=self.store.claim(other)
        (self.root/'snapshot/triangle.obj').write_bytes(b'corrupt')
        with self.assertRaises(Render3DError):self.store.reuse(second,self.verify)

    def test_running_cancel_prevents_publication(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.cancel(job)
        self.assertTrue(self.store.heartbeat(lease))
        with self.assertRaises(Render3DError):self.store.publish(lease,output,result,self.verify)
        self.assertFalse((self.root/'jobs/cache'/lease['cache_key']).exists())
        self.store.fail(lease,'cancelled')
        self.assertEqual(self.store.status(job)['state'],'cancelled')

    def test_corrupt_cache_is_preserved_and_repaired_after_new_verification(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        (self.root/'jobs/cache'/lease['cache_key']/'frame.png').write_bytes(b'corrupt')
        changed=deepcopy(self.request);changed['id']='repair';changed['destination']['path']='repair'
        other=self.submit(changed);second=self.store.claim(other)
        self.assertFalse(self.store.reuse(second,self.verify))
        output2,result2=self.output(second)
        self.store.publish(second,output2,result2,self.verify)
        preserved=list((self.root/'jobs/staging').glob('corrupt-*'))
        self.assertEqual(len(preserved),1)
        self.assertEqual((preserved[0]/'frame.png').read_bytes(),b'corrupt')
        self.assertEqual(self.store.status(other)['frames'][0]['state'],'complete')

    def test_corrupt_cache_repair_preserves_quota_and_prior_evidence(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        cache=self.root/'jobs/cache'/lease['cache_key']
        (cache/'frame.png').write_bytes(b'corrupt')
        changed=deepcopy(self.request);changed['id']='quota';changed['destination']['path']='quota'
        other=self.submit(changed);second=self.store.claim(other)
        output2,result2=self.output(second)
        # Existing cache must not bypass admission simply because its key exists.
        self.store.capacity['storage_bytes']=sum(p.stat().st_size for p in cache.rglob('*') if p.is_file())+1
        with self.assertRaises(Render3DError) as caught:
            self.store.publish(second,output2,result2,self.verify)
        self.assertEqual(caught.exception.report['category'],'storage-exhaustion')
        self.assertEqual((cache/'frame.png').read_bytes(),b'corrupt')
        self.assertEqual(self.store.status(other)['frames'][0]['state'],'running')

    def test_pinned_cache_cannot_be_cleaned_and_history_is_paginated(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        self.assertEqual(self.store.list_jobs()[0]['id'],job)
        self.assertEqual(len(self.store.status(job,event_limit=1)['events']),1)
        self.assertTrue(self.store.cache_inventory()[0]['pinned'])
        with self.assertRaises(Render3DError):self.store.clean_cache([lease['cache_key']],apply=True)
        self.assertTrue((self.root/'jobs/cache'/lease['cache_key']).is_dir())

    def test_complete_delivery_is_portable_and_exclusive(self):
        job=self.submit()
        for _ in range(2):
            lease=self.store.claim(job);output,result=self.output(lease)
            self.store.publish(lease,output,result,self.verify)
        destination=self.root/'delivered'
        result=self.store.materialize(job,destination,self.verify)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(result['images']),2)
        self.assertEqual([i['file']['path'] for i in result['images']],
                         ['frames/000005.png','frames/000006.png'])
        self.verify(self.request,result,destination)
        self.assertTrue((destination/'provenance/00000005.json').is_file())
        with self.assertRaises(Render3DError):self.store.materialize(job,destination,self.verify)

    def test_resume_reverifies_prior_completed_frames(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        (self.root/'jobs/cache'/lease['cache_key']/'frame.png').write_bytes(b'corrupt')
        with self.assertRaises(Render3DError):self.store.resume(job)
        self.store.resume(job,verifier=self.verify)
        self.assertEqual(self.store.status(job)['frames'][0]['state'],'queued')

    def test_refresh_policy_never_accepts_cached_frame(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        changed=deepcopy(self.request);changed['id']='refresh';changed['destination']['path']='fresh';changed['cache_policy']='refresh'
        other=self.submit(changed);second=self.store.claim(other)
        self.assertFalse(self.store.reuse(second,self.verify))
