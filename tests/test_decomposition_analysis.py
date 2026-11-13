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
                def interrupted(path, value):
                    if Path(path) == folder/'state.json':
                        if after_write:
                            real_atomic(path,value)
                        raise RuntimeError('Injected controller crash')
                    return real_atomic(path,value)
                with mock.patch.object(analysis,'atomic_json',side_effect=interrupted):
                    with self.assertRaisesRegex(RuntimeError,'Injected'):
                        analysis.retry(self.root,job['job'])
                self.assertTrue((folder/'retry.json').is_file())
                self.assertEqual((folder/'attempt-1/response.json').read_bytes(),original_response)
                recovered=analysis.status(self.root,job['job'])
                self.assertEqual(recovered['status'],'awaiting-external')
                self.assertFalse((folder/'retry.json').exists())
                self.assertFalse(recovered['response_available'])
                self.assertEqual(self.ingest(job)['attempts'],2)
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
        real_open=os.open
        with mock.patch.object(analysis,'_WINDOWS_RECORD_IO',True), mock.patch.object(
                analysis,'_windows_record_descriptor',side_effect=lambda path: real_open(path,os.O_RDONLY)) as windows:
            # This tests dispatch, not actual Windows kernel behavior. Native
            # handle validation runs in the Windows CI matrix separately.
            with mock.patch.dict(os.__dict__):
                os.__dict__.pop('O_NOFOLLOW',None);os.__dict__.pop('O_NONBLOCK',None)
                self.assertEqual(analysis._record_json(record),{'kind':'fixture'})
                job=self.prepare()
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


if __name__=='__main__':unittest.main()
