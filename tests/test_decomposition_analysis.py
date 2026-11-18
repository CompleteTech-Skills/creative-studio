"""Mechanics/security fixtures are separate from optional real detector evidence."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import decomposition_analysis as analysis
from asset_contracts import hash_object, sha
from character_decomposition import create_decomposition, load_decomposition, rebuild_from_annotation
from decomposition_spec import validate_structure
from extract_strip_frames import atomic_json


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / 'run'
        self.source = self.home / 'synthetic.png'
        Image.new('RGBA', (32,32), '#78b8ee').save(self.source)
        create_decomposition(self.source, self.root, background='keep', fixture=True,
                             origin='Synthetic test square; no semantic inference or approval')
        self.original = (self.root/'decomposition.json').read_bytes()

    def prepare(self, **kwargs):
        return analysis.prepare(self.root, 'external', enabled=True, taxonomy=['face/region'], **kwargs)

    def response(self, job):
        return {'schema_version':1,'kind':'decomposition-analysis-response',
            'request_sha256':job['job'],'source_sha256':job['request']['source']['sha256'],
            'status':'proposed','model':{'provider':'synthetic-fixture','name':'none','version':'fixture-v1',
                'files':[], 'identity_evidence':'Fixture only; no model executed'},
            'annotation':{'schema_version':1,'source_sha256':job['request']['source']['sha256'],
                'author':'Synthetic fixture', 'note':'Test mechanics, not detected semantics',
                'components':[{'id':'region','name':'Synthetic region','category':'face/region',
                    'mask':{'polygons':[[[2,2],[20,2],[20,20],[2,20]]]},'confidence':.5}]},
            'evidence':{'execution':'external-assertion','call_id':None,
                'unavailable_reason':'Synthetic fixture; no call happened','elapsed_seconds':None,'cost':None,
                'note':'No provider execution claimed'},'errors':[]}

    def ingest(self, job, response=None):
        p=self.home/'response.json';atomic_json(p,response or self.response(job))
        return analysis.ingest(self.root,job['job'],p)

    def test_source_reader_rejects_replaced_nonregular_leaf_without_waiting(self):
        import rig_errors
        if os.name == 'nt' or not hasattr(os, 'O_NOFOLLOW'):
            # Native Windows handles have dedicated actual CI; exercise the
            # portable same-byte source route rather than introduce a skip.
            self.assertEqual(analysis._source_image(self.root, self.prepare()['request']).size, (32,32))
            return
        for replacement in ('fifo', 'symlink'):
            with self.subTest(replacement=replacement):
                ref={'path': 'adversarial.bin', 'sha256': '0' * 64}
                target=self.root/ref['path']; target.write_bytes(b'original')
                outside=self.home/'outside.bin'; outside.write_bytes(b'foreign')
                original_safe_path=rig_errors.safe_path; changed=False
                def replace_after_path_admission(root, name, **kwargs):
                    nonlocal changed
                    target_path=original_safe_path(root,name,**kwargs)
                    if not changed and name==target.name:
                        changed=True; target.unlink()
                        if replacement=='fifo': os.mkfifo(target)
                        else: target.symlink_to(outside)
                    return target_path
                try:
                    with mock.patch.object(rig_errors, 'safe_path', side_effect=replace_after_path_admission):
                        with self.assertRaises(ValueError): analysis._source_bytes(self.root, ref)
                    self.assertTrue(changed)
                    self.assertEqual((self.root/'decomposition.json').read_bytes(), self.original)
                    self.assertEqual(outside.read_bytes(), b'foreign')
                finally:
                    target.unlink(missing_ok=True)

    def test_source_reader_preserves_source_capacity_and_rejects_oversize(self):
        from asset_contracts import MAX_SOURCE_BYTES
        target=self.root/'bounded.bin'; data=b'x'*(analysis.MAX_RESPONSE_BYTES+1); target.write_bytes(data)
        ref={'path': target.name, 'sha256': sha(target)}
        self.assertEqual(analysis._source_bytes(self.root, ref), data)
        with target.open('wb') as stream: stream.truncate(MAX_SOURCE_BYTES+1)
        with self.assertRaisesRegex(ValueError, 'byte quota'):
            analysis._source_bytes(self.root, ref)

    def test_source_decode_uses_admitted_hash_verified_bytes(self):
        request=self.prepare()['request']; target=self.root/request['source']['path']
        original_bytes=target.read_bytes(); admitted=analysis._source_bytes
        def replace_after_read(root, reference):
            payload=admitted(root,reference)
            Image.new('RGBA',(32,32),'#f01020').save(target)
            return payload
        try:
            with mock.patch.object(analysis,'_source_bytes',side_effect=replace_after_read):
                image=analysis._source_image(self.root,request)
            self.assertEqual(image.getpixel((0,0)),(0x78,0xb8,0xee))
            with self.assertRaisesRegex(ValueError,'bytes changed'):
                analysis._source_image(self.root,request)
        finally:
            target.write_bytes(original_bytes)

    def test_response_normalization_rejects_source_replaced_after_admission(self):
        job=self.prepare(); target=self.root/job['request']['source']['path']
        original=target.read_bytes(); source=analysis._source
        def replace_after_source(root,request):
            manifest=source(root,request)
            Image.new('RGBA',(64,64),'#f01020').save(target)
            return manifest
        try:
            with mock.patch.object(analysis,'_source',side_effect=replace_after_source):
                with self.assertRaisesRegex(ValueError,'bytes changed'):
                    analysis._validated_response(self.root,job['job'],self.response(job))
            self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)
        finally:
            target.write_bytes(original)

    def test_source_normalization_retains_verified_alpha(self):
        request=self.prepare()['request']
        rgba=analysis._source_image(self.root,request,mode='RGBA')
        self.assertEqual(rgba.mode,'RGBA')
        self.assertEqual(rgba.getpixel((0,0)),(0x78,0xb8,0xee,255))

    def test_regular_record_capacity_is_explicit_and_bounded(self):
        path=self.home/'record.bin'; data=b'x'*(analysis.MAX_RESPONSE_BYTES+1); path.write_bytes(data)
        with self.assertRaisesRegex(ValueError,'bounded'):
            analysis._record_bytes(path)
        self.assertEqual(analysis._record_bytes(path,len(data)),data)
        for bound in (True,0,-1,200_000_001,1.5):
            with self.subTest(bound=bound):
                with self.assertRaisesRegex(ValueError,'bound'): analysis._record_bytes(path,bound)

    def test_missing_in_memory_model_support_is_explicit_and_failure_isolated(self):
        import types
        with mock.patch.dict(sys.modules, {'cv2': types.SimpleNamespace()}):
            result=analysis.capabilities()
            self.assertFalse(result['opencv-haar']['available'])
            self.assertIn('in-memory',result['opencv-haar']['unavailable_reason'])
            self.assertTrue(result['external']['available'])
        self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)

    def test_classifier_requires_bounded_immutable_model_bytes(self):
        for payload in (None,bytearray(b'xml'),b'',b'x'*(analysis.MAX_RESPONSE_BYTES+1)):
            with self.subTest(kind=type(payload).__name__,size=len(payload) if payload is not None else 0):
                with self.assertRaisesRegex(ValueError,'bounded immutable'):
                    analysis._classifier_from_xml(None,payload,'synthetic.xml')

    def test_model_capture_checks_size_before_reading_or_parsing(self):
        import types
        models=self.home/'models';models.mkdir()
        with (models/analysis.MODEL_FILES[0]).open('wb') as stream:
            stream.truncate(analysis.MAX_RESPONSE_BYTES+1)
        class UnsupportedFixtureCascade:
            def read(self,node): raise AssertionError('no parsing oversized fixture')
        fixture=types.SimpleNamespace(FileStorage=lambda *args: None,
            CascadeClassifier=UnsupportedFixtureCascade,FILE_STORAGE_READ=0,FILE_STORAGE_MEMORY=4,
            data=types.SimpleNamespace(haarcascades=str(models)),__version__='synthetic-fixture')
        with mock.patch.dict(sys.modules,{'cv2':fixture}):
            with self.assertRaisesRegex(ValueError,'8 MiB'):
                analysis._capture_model()
        self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)

    def test_disabled_requires_permission_and_is_failure_isolated(self):
        with self.assertRaisesRegex(ValueError,'disabled'):
            analysis.prepare(self.root,'external')
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_request_is_idempotent_strict_and_bound(self):
        job=self.prepare()
        self.assertEqual(job,self.prepare())
        self.assertEqual(job['job'],hash_object(job['request']))
        value=copy.deepcopy(job['request']);value['provider_command']='sh'
        with self.assertRaises(ValueError):validate_structure(value,analysis.REQUEST)
        with self.assertRaises(ValueError):self.prepare(max_seconds=121)
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_ingest_apply_preserves_original_pixels_and_requires_review(self):
        job=self.prepare(); self.ingest(job)
        original_source=(self.root/'source'/'original.png').read_bytes()
        applied=analysis.apply(self.root,job['job'])
        self.assertEqual(applied['status'],'applied')
        current=load_decomposition(self.root)
        self.assertEqual(current['review']['status'],'pending')
        self.assertEqual(current['validation']['max_channel_error'] if 'max_channel_error' in current['validation'] else current['validation']['reconstruction_error']['max_channel_error'],0)
        self.assertEqual(original_source,(self.root/'source'/'original.png').read_bytes())
        self.assertEqual(analysis.apply(self.root,job['job'])['applied_revision'],current['revision'])
        self.assertEqual(self.prepare()['job'],job['job'])
        self.assertFalse(current['analysis']['semantic_model_called'])
        self.assertIn('decomposition_analysis',json.loads((self.root/'annotations/current.json').read_text())['extensions'])

    def test_crash_after_apply_is_acknowledged_without_duplicate_revision(self):
        job=self.prepare();self.ingest(job);analysis.apply(self.root,job['job'])
        state_path=self.root/'analysis-jobs'/job['job']/'state.json'
        state=json.loads(state_path.read_text());state.update(status='proposed',applied_revision=None);atomic_json(state_path,state)
        revision=load_decomposition(self.root)['revision']
        self.assertEqual(analysis.apply(self.root,job['job'])['applied_revision'],revision)

    def test_stale_manual_edit_cannot_be_overwritten(self):
        job=self.prepare();self.ingest(job)
        annotation=json.loads((self.root/'annotations/current.json').read_text())
        rebuild_from_annotation(self.root,annotation,reason='Manual work after request',author='Test')
        current=(self.root/'decomposition.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'changed since'):
            analysis.apply(self.root,job['job'])
        self.assertEqual(current,(self.root/'decomposition.json').read_bytes())

    def test_malformed_external_results_leave_original_unchanged(self):
        job=self.prepare()
        mutations=[lambda r:r.update(extra=1),
            lambda r:r.update(source_sha256='0'*64),
            lambda r:r['annotation']['components'][0].update(confidence=float('nan')),
            lambda r:r['annotation']['components'][0].update(mask={'path':'../../outside.png'}),
            lambda r:r['annotation']['components'][0].update(mask={'polygons':[[[0,0],[100,0],[0,100]]]}),
            lambda r:r['annotation']['components'][0].update(category='unrequested'),
            lambda r:r['evidence'].update(execution='local'),
            lambda r:r['evidence'].update(unavailable_reason='')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                response=self.response(job);mutate(response)
                p=self.home/'bad.json';p.write_text(json.dumps(response))
                with self.assertRaises(ValueError):analysis.ingest(self.root,job['job'],p)
                self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_immutable_response_and_hash_checked_before_apply(self):
        job=self.prepare();self.ingest(job)
        changed=self.response(job);changed['annotation']['note']='Different'
        with self.assertRaisesRegex(ValueError,'immutable'):self.ingest(job,changed)
        response=self.root/'analysis-jobs'/job['job']/'response.json'
        atomic_json(response,changed)
        with self.assertRaisesRegex(ValueError,'response changed'):analysis.apply(self.root,job['job'])

    def test_cancel_prevents_ingest_and_apply_without_corruption(self):
        job=self.prepare();analysis.cancel(self.root,job['job'])
        self.assertTrue(analysis.cancel(self.root,job['job'])['cancellation_requested'])
        with self.assertRaisesRegex(ValueError,'cancelled'):self.ingest(job)
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_no_backend_fails_explicitly_without_importing_optional_packages(self):
        with mock.patch.dict(sys.modules,{'cv2':None}):
            self.assertFalse(analysis.capabilities()['opencv-haar']['available'])
            with self.assertRaisesRegex(ValueError,'unavailable'):analysis.prepare(self.root,'opencv-haar',enabled=True)
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_stale_source_rejected(self):
        job=self.prepare()
        (self.root/'source/original.png').write_bytes(b'changed')
        with self.assertRaises(ValueError):self.ingest(job)

    def test_symlink_metadata_and_bad_job_paths_rejected(self):
        job=self.prepare()
        with self.assertRaises(ValueError):analysis.status(self.root,'../escape')
        link=self.root/'analysis-jobs'/job['job']/'response.json'
        try:link.symlink_to(self.home/'outside.json')
        except (OSError,NotImplementedError):self.skipTest('OS does not permit symlink creation')
        with self.assertRaisesRegex(ValueError,'symbolic'):analysis.status(self.root,job['job'])

    def test_quota_and_failed_attempts_are_preserved(self):
        job=self.prepare()
        path=self.root/'analysis-jobs'/job['job']/'state.json'
        state=json.loads(path.read_text());state.update(status='failed',attempts=1);atomic_json(path,state)
        retried=analysis.retry(self.root,job['job'])
        self.assertEqual(retried['status'],'awaiting-external')
        self.assertTrue(path.parent.joinpath('attempt-1/state.json').is_file())
        state=json.loads(path.read_text());state.update(status='failed',attempts=3);atomic_json(path,state)
        with self.assertRaisesRegex(ValueError,'quota'):analysis.retry(self.root,job['job'])
        with mock.patch.object(analysis,'MAX_JOBS',1):
            with self.assertRaisesRegex(ValueError,'quota'):self.prepare(prompt='other request')

    def test_arbitrary_extension_cannot_claim_local_model_execution(self):
        annotation=json.loads((self.root/'annotations/current.json').read_text())
        annotation['extensions']['decomposition_analysis']={'evidence':{'execution':'local'}}
        with self.assertRaises(ValueError):
            rebuild_from_annotation(self.root,annotation,reason='Forgery fixture',author='Test')
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_modified_lineage_response_invalidates_loaded_decomposition(self):
        job=self.prepare();self.ingest(job);analysis.apply(self.root,job['job'])
        path=self.root/'analysis-jobs'/job['job']/'response.json'
        response=json.loads(path.read_text());response['model']['version']='forged'
        atomic_json(path,response)
        with self.assertRaisesRegex(ValueError,'lineage'):
            load_decomposition(self.root)

    def test_external_cancel_can_be_explicitly_retried(self):
        job=self.prepare()
        analysis.cancel(self.root,job['job'])
        self.assertEqual(analysis.status(self.root,job['job'])['status'],'cancelled')
        self.assertEqual(analysis.retry(self.root,job['job'])['status'],'awaiting-external')
        self.assertEqual(self.ingest(job)['status'],'proposed')

    def test_cancelled_or_failed_completed_worker_can_be_explicitly_retried(self):
        for status in ('cancelled','failed'):
            with self.subTest(status=status):
                model={'provider':'synthetic-fixture','name':'none','version':status,'files':[],
                       'identity_evidence':'Synthetic recovery fixture; no model execution'}
                with mock.patch.object(analysis,'_model',return_value=model):
                    job=analysis.prepare(self.root,'opencv-haar',enabled=True)
                response=self.response(job);response['model']=model
                response['evidence']['execution']='local'
                folder=self.root/'analysis-jobs'/job['job']
                atomic_json(folder/'worker-response.json',response)
                state=json.loads((folder/'state.json').read_text())
                state.update(status=status,attempts=1);atomic_json(folder/'state.json',state)
                self.assertEqual(analysis.retry(self.root,job['job'])['status'],'ready')
                self.assertFalse((folder/'worker-response.json').exists())
                self.assertTrue((folder/'attempt-1/worker-response.json').is_file())

    def test_external_parameters_and_selected_model_are_bound(self):
        model={'provider':'synthetic-fixture','name':'none','version':'fixture-v1','files':[],
               'identity_evidence':'Fixture only; no model executed'}
        job=analysis.prepare(self.root,'external',enabled=True,taxonomy=['face/region'],
                             parameters={'threshold':0.7,'labels':['face/region']},expected_model=model)
        self.assertEqual(job['request']['parameters']['threshold'],.7)
        wrong=self.response(job);wrong['model']['version']='other'
        with self.assertRaisesRegex(ValueError,'selected model'):self.ingest(job,wrong)
        self.ingest(job)

    def test_external_failure_can_be_explicitly_retried_with_retained_response(self):
        job=self.prepare()
        failed=self.response(job);failed.update(status='failed',errors=['Synthetic provider failure'])
        failed['annotation']['components']=[]
        self.ingest(job,failed)
        analysis.retry(self.root,job['job'])
        retried=self.ingest(job)
        self.assertEqual(retried['status'],'proposed')
        self.assertEqual(retried['attempts'],2)
        self.assertEqual(json.loads((self.root/'analysis-jobs'/job['job']/'attempt-1/response.json').read_text())['status'],'failed')

    def test_uncertain_resume_never_repeats_call(self):
        model={'provider':'synthetic-fixture','name':'none','version':'test','files':[], 'identity_evidence':'No actual detector call'}
        with mock.patch.object(analysis,'_model',return_value=model):
            job=analysis.prepare(self.root,'opencv-haar',enabled=True)
        path=self.root/'analysis-jobs'/job['job']/'state.json'
        state=json.loads(path.read_text());state.update(status='running',attempts=1);atomic_json(path,state)
        with mock.patch.object(analysis.subprocess,'Popen',side_effect=AssertionError('must not dispatch')):
            result=analysis.run(self.root,job['job'])
            self.assertEqual(result['status'],'interrupted')
            self.assertEqual(analysis.run(self.root,job['job'])['status'],'interrupted')

    def test_late_completed_worker_is_recovered_without_redispatch(self):
        model={'provider':'synthetic-fixture','name':'none','version':'late','files':[],
               'identity_evidence':'Synthetic recovery fixture; no model execution'}
        with mock.patch.object(analysis,'_model',return_value=model):
            job=analysis.prepare(self.root,'opencv-haar',enabled=True)
        folder=self.root/'analysis-jobs'/job['job']
        state=json.loads((folder/'state.json').read_text())
        state.update(status='running',attempts=1);atomic_json(folder/'state.json',state)
        with mock.patch.object(analysis.subprocess,'Popen',side_effect=AssertionError('must not dispatch')):
            self.assertEqual(analysis.run(self.root,job['job'])['status'],'interrupted')
            response=self.response(job);response['model']=model;response['evidence']['execution']='local'
            atomic_json(folder/'worker-response.json',response)
            recovered=analysis.run(self.root,job['job'])
            self.assertEqual(recovered['status'],'proposed')
            self.assertEqual(recovered['attempts'],1)
            self.assertEqual(analysis.run(self.root,job['job'])['status'],'proposed')

    def test_retry_crash_recovers_before_and_after_final_state_write(self):
        for after_write in (False, True):
            with self.subTest(after_write=after_write):
                job=self.prepare(prompt=str(after_write))
                failed=self.response(job);failed.update(status='failed',errors=['Fixture failure'])
                failed['annotation']['components']=[]
                self.ingest(job,failed)
                folder=self.root/'analysis-jobs'/job['job']
                original_response=(folder/'response.json').read_bytes()
                real_atomic=analysis.atomic_json
                state_path=analysis._job(self.root,job['job'])/'state.json'
                writes=[]
                def interrupted(path, value):
                    if Path(path) == state_path:
                        writes.append(value.copy())
                        if after_write:
                            real_atomic(path,value)
                        raise RuntimeError('Injected controller crash')
                    return real_atomic(path,value)
                with mock.patch.object(analysis,'atomic_json',side_effect=interrupted):
                    with self.assertRaisesRegex(RuntimeError,'Injected'):
                        analysis.retry(self.root,job['job'])
                self.assertEqual(len(writes),1)
                self.assertEqual(writes[0]['status'],'awaiting-external')
                self.assertTrue((folder/'retry.json').is_file())
                self.assertEqual((folder/'attempt-1/response.json').read_bytes(),original_response)
                recovered=analysis.status(self.root,job['job'])
                self.assertEqual(recovered['status'],'awaiting-external')
                self.assertFalse((folder/'retry.json').exists())
                self.assertFalse(recovered['response_available'])
                self.assertEqual(self.ingest(job)['attempts'],2)
                self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_retry_crash_hook_with_root_alias(self):
        alias=self.home/'run-alias'
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('directory symlinks unavailable')
        self.root=alias
        self.assertNotEqual(self.root,analysis._job(self.root,'0'*64).parent.parent)
        self.test_retry_crash_recovers_before_and_after_final_state_write()

    def test_retry_flushes_history_records_before_publication(self):
        import atomic_publish
        job=self.prepare()
        failed=self.response(job);failed.update(status='failed',errors=['Fixture failure'])
        failed['annotation']['components']=[]
        self.ingest(job,failed)
        synced=set()
        real_fsync=os.fsync
        real_publish=atomic_publish._rename_no_replace
        published=[]
        def record_sync(fd):
            info=os.fstat(fd)
            synced.add((info.st_dev,info.st_ino))
            return real_fsync(fd)
        def check_publish(source,destination,**kwargs):
            info=Path(source).stat()
            self.assertIn((info.st_dev,info.st_ino),synced)
            published.append(Path(destination).name)
            return real_publish(source,destination,**kwargs)
        with mock.patch.object(analysis.os,'fsync',side_effect=record_sync), \
             mock.patch.object(atomic_publish,'_rename_no_replace',side_effect=check_publish):
            result=analysis.retry(self.root,job['job'])
        self.assertEqual(result['status'],'awaiting-external')
        self.assertIn('state.json',published)
        self.assertIn('response.json',published)
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_retry_rejects_nonregular_history_records_without_blocking(self):
        for name in ('state.json','worker.log','cancel','response.json','worker-response.json'):
            with self.subTest(name=name):
                job=self.prepare(prompt=name)
                failed=self.response(job);failed.update(status='failed',errors=['Fixture failure'])
                failed['annotation']['components']=[]
                self.ingest(job,failed)
                folder=self.root/'analysis-jobs'/job['job']
                (folder/'worker.log').write_bytes(b'fixture log')
                (folder/'worker-response.json').write_bytes(b'{}')
                (folder/'cancel').write_bytes(b'cancel\n')
                history=folder/'attempt-1';history.mkdir()
                if hasattr(os,'mkfifo'):
                    os.mkfifo(history/name)
                else:
                    (history/name).mkdir()
                result=subprocess.run([sys.executable,analysis.__file__,'retry','--root',str(self.root),
                    '--job',job['job']],capture_output=True,text=True,timeout=3)
                self.assertEqual(result.returncode,2,result.stdout)
                self.assertRegex(result.stderr,'regular file|opened safely')
                self.assertTrue((folder/'response.json').is_file())
                self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_retry_recovery_rejects_nonregular_history_state_without_blocking(self):
        job=self.prepare()
        folder=self.root/'analysis-jobs'/job['job']
        state=json.loads((folder/'state.json').read_text());state.update(status='failed',attempts=1)
        atomic_json(folder/'state.json',state)
        real_recovery=analysis._recover_retry
        calls=[]
        def interrupt(root, identifier):
            calls.append(identifier)
            if len(calls)>1:
                raise RuntimeError('crash')
            return real_recovery(root,identifier)
        with mock.patch.object(analysis,'_recover_retry',side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError,'crash'):
                analysis.retry(self.root,job['job'])
        retained=folder/'attempt-1/state.json';retained.unlink()
        if hasattr(os,'mkfifo'):
            os.mkfifo(retained)
        else:
            retained.mkdir()
        result=subprocess.run([sys.executable,analysis.__file__,'status','--root',str(self.root),
            '--job',job['job']],capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,2)
        self.assertRegex(result.stderr,'regular file|opened safely')
        self.assertEqual(json.loads((folder/'state.json').read_text()),state)
        self.assertTrue((folder/'retry.json').is_file())

    def test_missing_posix_flags_dispatches_to_windows_safe_descriptor(self):
        record=self.home/'portable.json';record.write_bytes(b'{"kind":"fixture"}')
        # Build with the host's real publication authority; the context below
        # emulates Windows record-reader dispatch, not Windows directory handles.
        job=self.prepare()
        real_open=os.open
        with mock.patch.object(analysis,'_WINDOWS_RECORD_IO',True), mock.patch.object(
                analysis,'_windows_record_descriptor',side_effect=lambda path: real_open(path,os.O_RDONLY)) as windows:
            # This tests dispatch, not actual Windows kernel behavior. Native
            # handle validation runs in the Windows CI matrix separately.
            with mock.patch.dict(os.__dict__):
                os.__dict__.pop('O_NOFOLLOW',None);os.__dict__.pop('O_NONBLOCK',None)
                self.assertEqual(analysis._record_json(record),{'kind':'fixture'})
                self.assertEqual(analysis.status(self.root,job['job'])['status'],'awaiting-external')
                self.assertEqual(self.ingest(job)['status'],'proposed')
        self.assertTrue(windows.called)

    def test_platform_reader_never_opens_without_available_safety(self):
        if sys.platform=='win32':
            self.assertTrue(analysis._WINDOWS_RECORD_IO)
            self.assertEqual(analysis._record_bytes(self.source),self.source.read_bytes())
            return
        with mock.patch.dict(os.__dict__):
            os.__dict__.pop('O_NOFOLLOW',None)
            with mock.patch.object(analysis.os,'open',side_effect=AssertionError('must not open')):
                with self.assertRaisesRegex(ValueError,'platform support'):
                    analysis._record_bytes(self.source)

    def test_runtime_timeout_kills_real_subprocess_and_keeps_manual_route(self):
        model={'provider':'synthetic-fixture','name':'none','version':'test','files':[], 'identity_evidence':'No detector used by timeout test'}
        with mock.patch.object(analysis,'_model',return_value=model):
            job=analysis.prepare(self.root,'opencv-haar',enabled=True,max_seconds=1)
        actual_popen=subprocess.Popen
        children=[]
        def slow_child(*args,**kwargs):
            child=actual_popen([sys.executable,'-c','import time; time.sleep(60)'],**kwargs)
            children.append(child)
            return child
        with mock.patch.object(analysis,'_model',return_value=model), mock.patch.object(analysis.subprocess,'Popen',side_effect=slow_child):
            self.assertEqual(analysis.run(self.root,job['job'])['status'],'failed')
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())
        self.assertTrue(load_decomposition(self.root)['validation']['ok'])

    def test_cancellation_marker_stops_running_real_subprocess(self):
        model={'provider':'synthetic-fixture','name':'none','version':'test','files':[], 'identity_evidence':'No detector used by cancellation test'}
        with mock.patch.object(analysis,'_model',return_value=model):
            job=analysis.prepare(self.root,'opencv-haar',enabled=True)
        actual_popen=subprocess.Popen
        children=[]
        def cancelling_child(*args,**kwargs):
            child=actual_popen([sys.executable,'-c','import time; time.sleep(60)'],**kwargs)
            children.append(child)
            analysis.cancel(self.root,job['job'])
            return child
        with mock.patch.object(analysis,'_model',return_value=model), mock.patch.object(analysis.subprocess,'Popen',side_effect=cancelling_child):
            self.assertEqual(analysis.run(self.root,job['job'])['status'],'cancelled')
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.original,(self.root/'decomposition.json').read_bytes())

    def test_cli_retains_legacy_handoff_and_explicit_enablement(self):
        cli=Path(analysis.__file__).with_name('hatch.py')
        result=subprocess.run([sys.executable,str(cli),'decompose-analyze','--run-dir',str(self.root)],capture_output=True,text=True,check=True)
        self.assertFalse(json.loads(result.stdout)['semantic_model_called'])
        result=subprocess.run([sys.executable,str(cli),'decompose-analyze','--run-dir',str(self.root),'--analysis-backend','external','--enable-analysis'],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['status'],'awaiting-external')

    def publication_hook(self, action):
        """Interleave real namespace operations at the canonical publish boundary."""
        if os.name == 'nt':
            from atomic_windows import PrivateStage
            original=PrivateStage.publish_to
            def publish(stage,destination):
                action(stage.path.parent,Path(destination))
                return original(stage,destination)
            return mock.patch.object(PrivateStage,'publish_to',new=publish)
        import atomic_publish
        original=atomic_publish.rename_no_replace_at
        def publish(stage,name,parent,destination):
            identity=os.fstat(stage)
            wrapper=next(p for p in (self.root/'.analysis-staging').iterdir()
                         if (p.stat().st_dev,p.stat().st_ino)==(identity.st_dev,identity.st_ino))
            action(wrapper,self.root/'analysis-jobs'/destination)
            return original(stage,name,parent,destination)
        return mock.patch.object(atomic_publish,'rename_no_replace_at',side_effect=publish)

    def test_prepare_outer_stage_replacement_never_deletes_foreign_directory(self):
        paths=[]
        def interleave(wrapper,destination):
            displaced=wrapper.with_name(wrapper.name+'-preserved')
            if os.name=='nt':
                with self.assertRaises(OSError):wrapper.rename(displaced)
                foreign=self.home/'foreign';foreign.mkdir();(foreign/'sentinel').write_bytes(b'foreign')
                paths.append((foreign,None))
            else:
                wrapper.rename(displaced);wrapper.mkdir();(wrapper/'sentinel').write_bytes(b'foreign')
                paths.append((wrapper,displaced))
            raise RuntimeError('publication interrupted after namespace attempt')
        with self.publication_hook(interleave),self.assertRaisesRegex(RuntimeError,'publication interrupted'):
            self.prepare()
        foreign,displaced=paths[0]
        self.assertEqual((foreign/'sentinel').read_bytes(),b'foreign')
        if displaced is not None:self.assertEqual(list(displaced.iterdir()),[])
        self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)
        self.assertEqual(list((self.root/'analysis-jobs').iterdir()),[])

    def test_prepare_root_namespace_replacement_preserves_foreign_and_never_reports_success(self):
        displaced=self.root.with_name('preserved-run')
        def interleave(wrapper,destination):
            if os.name=='nt':
                with self.assertRaises(OSError):self.root.rename(displaced)
                raise RuntimeError('Windows denied parent namespace replacement')
            self.root.rename(displaced);self.root.mkdir();(self.root/'foreign').write_bytes(b'foreign')
        with self.publication_hook(interleave),self.assertRaises((ValueError,RuntimeError)):
            self.prepare()
        if os.name=='nt':
            self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)
        else:
            self.assertEqual([p.name for p in self.root.iterdir()],['foreign'])
            self.assertEqual((self.root/'foreign').read_bytes(),b'foreign')
            self.assertEqual((displaced/'decomposition.json').read_bytes(),self.original)

    def test_prepare_jobs_parent_replacement_never_writes_foreign_namespace(self):
        displaced=self.root/'preserved-jobs'
        def interleave(wrapper,destination):
            jobs=destination.parent
            if os.name=='nt':
                with self.assertRaises(OSError):jobs.rename(displaced)
                raise RuntimeError('Windows denied jobs namespace replacement')
            jobs.rename(displaced);jobs.mkdir();(jobs/'foreign').write_bytes(b'foreign')
        with self.publication_hook(interleave),self.assertRaises((ValueError,RuntimeError)):
            self.prepare()
        jobs=self.root/'analysis-jobs'
        if os.name!='nt':
            self.assertEqual([p.name for p in jobs.iterdir()],['foreign'])
            self.assertEqual((jobs/'foreign').read_bytes(),b'foreign')
        self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)

    def test_prepare_outer_stage_replacement_before_writes_uses_owned_payload(self):
        if os.name=='nt':
            # Windows pins the wrapper before yielding, preventing the rename.
            def denied(wrapper,destination):
                with self.assertRaises(OSError):wrapper.rename(wrapper.with_name('moved-wrapper'))
            with self.publication_hook(denied):job=self.prepare()
            self.assertEqual(job['status'],'awaiting-external')
            return
        import atomic_publish
        from contextlib import contextmanager
        original=atomic_publish.private_stage_fd
        paths=[]
        @contextmanager
        def swapped(parent,**kwargs):
            with original(parent,**kwargs) as fd:
                wrapper=next((self.root/'.analysis-staging').iterdir())
                displaced=wrapper.with_name(wrapper.name+'-preserved');wrapper.rename(displaced)
                wrapper.mkdir();(wrapper/'foreign').write_bytes(b'foreign')
                paths.append((wrapper,displaced));yield fd
        with mock.patch.object(atomic_publish,'private_stage_fd',new=swapped):job=self.prepare()
        foreign,displaced=paths[0]
        self.assertEqual([p.name for p in foreign.iterdir()],['foreign'])
        self.assertEqual((foreign/'foreign').read_bytes(),b'foreign')
        self.assertEqual(list(displaced.iterdir()),[])
        self.assertEqual(analysis.status(self.root,job['job'])['status'],'awaiting-external')

    def test_prepare_racing_destination_is_never_replaced(self):
        destinations=[]
        def interleave(wrapper,destination):
            destination.mkdir();(destination/'foreign').write_bytes(b'existing destination')
            destinations.append(destination)
        with self.publication_hook(interleave),self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual((destinations[0]/'foreign').read_bytes(),b'existing destination')
        self.assertEqual([p.name for p in destinations[0].iterdir()],['foreign'])
        self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)

    def test_prepare_publication_interruption_cleans_only_owned_payload_and_can_retry(self):
        def interrupt(wrapper,destination):raise RuntimeError('controlled publication interruption')
        with self.publication_hook(interrupt),self.assertRaisesRegex(RuntimeError,'controlled publication'):
            self.prepare()
        self.assertEqual(list((self.root/'analysis-jobs').iterdir()),[])
        self.assertTrue(all(not list(p.iterdir()) for p in (self.root/'.analysis-staging').iterdir()))
        job=self.prepare()
        self.assertEqual(job['status'],'awaiting-external')
        self.assertEqual(self.prepare()['job'],job['job'])
        self.assertEqual((self.root/'decomposition.json').read_bytes(),self.original)

    def test_prepare_staging_wrappers_do_not_reduce_job_quota_and_are_separately_bounded(self):
        with mock.patch.object(analysis,'MAX_JOBS',2):
            self.prepare(prompt='first');self.prepare(prompt='second')
            with self.assertRaisesRegex(ValueError,'job quota'):self.prepare(prompt='third')
        staging=self.root/'.analysis-staging'
        for i in range(8): (staging/('retained-'+str(i))).mkdir()
        with mock.patch.object(analysis,'MAX_JOBS',2):
            # A separate run leaves room under the job quota while exhausting stages.
            import shutil
            for p in (self.root/'analysis-jobs').iterdir():shutil.rmtree(p)
            with self.assertRaisesRegex(ValueError,'staging quota'):self.prepare(prompt='new')

    def test_prepare_rejects_replaced_selected_manifest_before_creating_job_directories(self):
        original=analysis._publish_prepared_job
        def changed(root,identifier,request,state):
            annotation=json.loads((self.root/'annotations/current.json').read_text())
            rebuild_from_annotation(self.root,annotation,reason='Manual change during preparation',author='Test')
            return original(root,identifier,request,state)
        with mock.patch.object(analysis,'_publish_prepared_job',side_effect=changed),self.assertRaisesRegex(ValueError,'changed before'):
            self.prepare()
        self.assertFalse((self.root/'analysis-jobs').exists())
        self.assertTrue(load_decomposition(self.root)['validation']['ok'])


if __name__=='__main__':unittest.main()
