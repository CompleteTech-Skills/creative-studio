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

    def test_artifact_replaced_with_fifo_does_not_block_copy(self):
        import os
        if not hasattr(os,'mkfifo'):self.skipTest('FIFO race applies to POSIX')
        source=self.root/'fifo';os.mkfifo(source)
        code="""import sys
sys.path.insert(0,sys.argv[1])
from render3d_jobs import _copy_artifact
from render3d_contracts import Render3DError
try:_copy_artifact(sys.argv[2],__import__('pathlib').Path(sys.argv[3]),dict(bytes=1,sha256='0'*64))
except Render3DError as error:
 assert error.report['category']=='invalid-input'
else:raise AssertionError('FIFO input accepted')
"""
        child=subprocess.run([sys.executable,'-c',code,str(ROOT/'scripts'),str(source),
                              str(self.root/'copy')],capture_output=True,text=True,timeout=20)
        self.assertEqual(child.returncode,0,child.stderr)

    def test_metadata_fifo_replacements_reject_without_blocking(self):
        import os
        if not hasattr(os,'mkfifo'):self.skipTest('FIFO races apply to POSIX')
        code="""import sys,os
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from unittest.mock import patch
from test_render3d_jobs import JobTests
import render3d_jobs as jobs
from render3d_contracts import Render3DError
case=JobTests();case.setUp()
try:
 name=sys.argv[3]
 if name=='cache.json':
  job=case.submit();lease=case.store.claim(job);output,result=case.output(lease)
  case.store.publish(lease,output,result,case.verify)
 original=jobs.safe_path
 def replace(root,name_,*a,**k):
  path=original(root,name_,*a,**k)
  if name_==name:path.unlink();os.mkfifo(path)
  return path
 with patch.object(jobs,'safe_path',side_effect=replace):
  try:
   if name=='store.json':jobs.JobStore(case.root/'jobs')
   else:case.store._verify_cache(lease,case.verify)
  except Render3DError:pass
  else:raise AssertionError('FIFO metadata accepted')
finally:case.tearDown()
"""
        for name in ('store.json','cache.json'):
            with self.subTest(name=name):
                child=subprocess.run([sys.executable,'-c',code,str(ROOT/'scripts'),str(ROOT/'tests'),name],
                                     capture_output=True,text=True,timeout=20)
                self.assertEqual(child.returncode,0,child.stderr)

    def test_failed_and_crash_leftover_outputs_reserve_capacity(self):
        self.request['settings']['resources']['storage_bytes']=128
        self.store=JobStore(self.root/'tiny-jobs',storage_bytes=128)
        job=self.submit()
        class Failing:
            calls=0
            def render_frames(worker,request,snapshot,output,**kwargs):
                worker.calls+=1;output.mkdir();(output/'retained.log').write_bytes(b'x'*128)
                raise Render3DError('worker-crash','bounded failed attempt')
        worker=Failing()
        with self.assertRaises(Render3DError):self.store.run_one(worker,self.verify,job=job)
        self.assertIsNone(self.store.run_one(worker,self.verify,job=job))
        self.assertEqual(worker.calls,1)
        self.assertEqual(self.store._retained_cache_bytes(),128)
        self.assertEqual(self.store.status(job)['frames'][0]['attempts'],1)
        # Reopening retains accounting even when no live process owns the files.
        reopened=JobStore(self.root/'tiny-jobs',storage_bytes=128)
        self.assertIsNone(reopened.claim(job))
        self.assertEqual(reopened._retained_cache_bytes(),128)
        orphan=JobStore(self.root/'crash-leftover',storage_bytes=128)
        orphan_job=orphan.submit(self.request,self.root/'snapshot',self.backend)
        leftover=orphan.root/'staging/run-dead';leftover.mkdir()
        (leftover/'partial').write_bytes(b'x'*128)
        self.assertIsNone(orphan.claim(orphan_job))

    def test_running_output_is_not_double_counted_against_its_reservation(self):
        self.request['settings']['resources']['storage_bytes']=128
        self.store=JobStore(self.root/'parallel',workers=2,storage_bytes=256)
        job=self.submit();first=self.store.claim(job)
        output=self.store.root/'staging'/('run-'+first['token']);output.mkdir()
        (output/'active.log').write_bytes(b'x'*128)
        self.assertIsNotNone(self.store.claim(job))

    def test_delivery_metadata_budget_failure_preserves_destination(self):
        self.request['settings']['resources']['storage_bytes']=512
        job=self.submit()
        for _ in range(2):
            lease=self.store.claim(job);output,result=self.output(lease)
            self.store.publish(lease,output,result,self.verify)
        destination=self.root/'too-small'
        with self.assertRaises(Render3DError) as caught:
            self.store.materialize(job,destination,self.verify)
        self.assertEqual(caught.exception.report['category'],'storage-exhaustion')
        self.assertFalse(destination.exists())
        self.assertEqual(self.store.status(job)['state'],'complete')
        self.assertFalse(list(self.root.glob('.render3d-delivery-*')))

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

    def test_wrong_shape_cache_metadata_rejects_and_repairs(self):
        job=self.submit();lease=self.store.claim(job);output,result=self.output(lease)
        self.store.publish(lease,output,result,self.verify)
        metadata=self.store.root/'cache'/lease['cache_key']/'cache.json'
        original=json.loads(metadata.read_text())
        malformed=[[],None,"invalid",False,17,{},dict(original,spec=[]),dict(original,backend=None),
                   dict(original,request=None),dict(original,result=[]),
                   dict(original,report=[]),dict(original,version=2)]
        for index,value in enumerate(malformed):
            with self.subTest(index=index):
                atomic_json(metadata,value)
                req=deepcopy(self.request);req['id']='repair-'+str(index)
                req['destination']['path']='repair-'+str(index)
                other=self.submit(req);second=self.store.claim(other)
                self.assertFalse(self.store.reuse(second,self.verify))
                output2,result2=self.output(second)
                self.store.publish(second,output2,result2,self.verify)
                self.assertEqual(self.store.status(other)['frames'][0]['state'],'complete')
        # Resume must invalidate malformed metadata without a raw Python error.
        atomic_json(metadata,[])
        self.store.resume(job,verifier=self.verify)
        self.assertEqual(self.store.status(job)['frames'][0]['state'],'queued')
        self.assertEqual(len(list((self.store.root/'staging').glob('corrupt-*'))),len(malformed))

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
        actual=sum(p.stat().st_size for p in destination.rglob('*') if p.is_file())
        self.assertEqual(result['usage']['output_bytes'],actual)
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


@unittest.skipUnless(__import__('os').environ.get('HATCH_RENDER3D_REAL_TESTS') == '1',
                     'Dedicated real-render gate requires HATCH_RENDER3D_REAL_TESTS=1')
class RealJobWorkflow(unittest.TestCase):
    def test_native_render_delivery_cache_repair_and_worker_cancellation(self):
        from rig_test_helpers import fixture
        from rig_jobs import save_rig
        from render3d_snapshot import NativeRigAdapter
        from render3d_blender import BlenderBackend
        from render3d_verify import RenderArtifactVerifier
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'native').mkdir()
            rig=fixture(root/'native');del rig.clips['neutral']
            save_rig(rig,operation='single-clip-jobs-native-test')
            create_snapshot(rig.root,root/'snapshot',NativeRigAdapter())
            req=request();req['snapshot']=file(payload=(root/'snapshot/snapshot.json').read_bytes())
            req['frame_range']=dict(start=0,end=2);req['source_start']=dict(numerator=0,denominator=1)
            req['fps']=dict(numerator=4,denominator=1);req['playback_rate']=dict(numerator=1,denominator=1)
            req['camera']='front';req['max_attempts']=3
            req['settings']['resolution']=[32,32];req['settings']['samples']=1
            req['settings']['resources']['memory_bytes']=2*1024**3
            backend_adapter=BlenderBackend();identity=backend_adapter.inspect_version()
            verify=RenderArtifactVerifier(root/'snapshot',identity).verify
            store=JobStore(root/'jobs');job=store.submit(req,root/'snapshot',identity)
            while store.run_one(backend_adapter,verify,job=job) is not None:pass
            delivered=store.materialize(job,root/'delivery',verify)
            self.assertEqual(verify(req,delivered,root/'delivery')['decoded_frames'],2)
            reused=deepcopy(req);reused['id']='reused';reused['destination']['path']='reused'
            reuse_job=store.submit(reused,root/'snapshot',identity)
            class NoRender:
                def render_frames(self,*args,**kwargs):
                    raise AssertionError('Valid independently decoded cache should be reused')
            while store.run_one(NoRender(),verify,job=reuse_job) is not None:pass
            self.assertEqual(store.status(reuse_job)['state'],'complete')
            frame=store.status(job)['frames'][0]
            cache=root/'jobs/cache'/frame['cache_key']
            record=json.loads((cache/'cache.json').read_text())
            (cache/record['result']['images'][0]['file']['path']).write_bytes(b'corrupt-cache')
            store.resume(job,verifier=verify)
            self.assertEqual([item['state'] for item in store.status(job)['frames']],['queued','complete'])
            while store.run_one(backend_adapter,verify,job=job) is not None:pass
            self.assertEqual(store.status(job)['state'],'complete')
            self.assertEqual(len(list((root/'jobs/staging').glob('corrupt-*'))),1)
            cancelled=deepcopy(req);cancelled['id']='cancelled';cancelled['destination']['path']='cancelled'
            cancelled['cache_policy']='refresh';cancel_job=store.submit(cancelled,root/'snapshot',identity)
            class CancelRender:
                def render_frames(self,*args,**kwargs):
                    store.cancel(cancel_job)
                    return backend_adapter.render_frames(*args,**kwargs)
            with self.assertRaises(Render3DError) as caught:
                store.run_one(CancelRender(),verify,job=cancel_job)
            self.assertEqual(caught.exception.report['category'],'cancelled')
            self.assertEqual(store.status(cancel_job)['state'],'cancelled')
