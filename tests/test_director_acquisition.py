"""Exercise the existing native raster claim lifecycle without provider calls."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

import hatch
from director_acquisition import NativeAcquisitionGateway
from fixture_workflow import small_profile
from rig_errors import RigError


class DirectorAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name);self.root=self.parent/'run'
        profile=self.parent/'profile.json';hatch.atomic(profile,small_profile())
        hatch.prepare(self.root,argparse.Namespace(replace_inputs=False,fixture_mode=True,
            profile=str(profile),pet_name='Gateway fixture',pet_notes='Explicit synthetic lifecycle test',
            reference=[],avoid=[],require=[],brand_source=[],style_preset='clay',style_notes='fixture only'))
        self.gateway=NativeAcquisitionGateway(self.root,asset_kind='raster')
        self.source=self.parent/'source.png'
        Image.new('RGBA',(96,96),(30,70,160,255)).save(self.source)
        self.sha=hashlib.sha256(self.source.read_bytes()).hexdigest()

    def test_prepare_is_idempotent_and_existing_owner_is_preserved(self):
        before=(self.root/'imagegen-jobs.json').read_bytes()
        a=self.gateway.prepare();b=NativeAcquisitionGateway(self.root,asset_kind='raster',binding=a['binding']).prepare()
        self.assertEqual(a,b)
        self.assertEqual(before,(self.root/'imagegen-jobs.json').read_bytes())
        self.assertFalse(a['provider_called'])

    def test_duplicate_concurrent_claims_reuse_one_native_token(self):
        def claim(_):return self.gateway.claim('base')
        with ThreadPoolExecutor(max_workers=4) as workers:results=list(workers.map(claim,range(4)))
        self.assertEqual(len({r['claim_id'] for r in results}),1)
        self.assertEqual(sum(not r['reused_active_claim'] for r in results),1)
        data=hatch.manifest(self.root);job=hatch.job_for(data,'base')
        self.assertEqual(job['attempts'],1)
        self.assertEqual(sum(x['event']=='claim' for x in job['history']),1)

    def test_uncertain_failure_and_stale_input_preserve_original_claim(self):
        result=self.gateway.claim('base');token=result['claim_id']
        uncertain=self.gateway.fail('base',token,'Provider outcome unknown',outcome='uncertain')
        self.assertEqual(uncertain['claim_id'],token)
        self.assertEqual(uncertain['outcome'],'uncertain')
        prompt=self.root/'prompts/base-pet.md';prompt.write_text(prompt.read_text()+'\nchanged')
        reconciled=self.gateway.reconcile('base')
        self.assertEqual(reconciled['claim_id'],token);self.assertTrue(reconciled['stale'])
        self.assertEqual(self.gateway.claim('base')['claim_id'],token)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['attempts'],1)

    def test_native_ingest_preserves_exact_source_and_fixture_origin(self):
        result=self.gateway.claim('base');token=result['claim_id']
        receipt=result['handoff']['receipt_template']
        ingested=self.gateway.ingest('base',token,self.source,source_sha256=self.sha,
                                     receipt=receipt,note='Synthetic raster acceptance only')
        self.assertEqual(ingested['native_state'],'generated')
        self.assertEqual((self.root/ingested['preserved_source']).read_bytes(),self.source.read_bytes())
        data=hatch.manifest(self.root);job=hatch.job_for(data,'base')
        self.assertEqual(job['source_origin'],'fixture')
        self.assertTrue(ingested['native_binding']['fixture'])
        self.assertEqual(job['attempts'],1)
        self.assertIsNone(ingested['claim_id'])
        self.source.unlink()
        self.assertTrue((self.root/job['output_path']).is_file())
        self.assertTrue(Path(job['source_path']).is_file())

    def test_wrong_hash_receipt_and_token_never_promote(self):
        result=self.gateway.claim('base');token=result['claim_id'];receipt=result['handoff']['receipt_template']
        wrong=deepcopy(receipt);wrong['generation_skill']='$imagegen'
        for sha,claim,used in [('0'*64,token,receipt),(self.sha,'other',receipt),(self.sha,token,wrong)]:
            with self.subTest(sha=sha,token=claim),self.assertRaises(RigError):
                self.gateway.ingest('base',claim,self.source,source_sha256=sha,receipt=used,note='fixture')
        self.assertFalse((self.root/'decoded/base.png').exists())
        self.assertEqual(self.gateway.reconcile('base')['claim_id'],token)

    def test_known_failure_releases_only_original_token(self):
        token=self.gateway.claim('base')['claim_id']
        with self.assertRaises(RigError):self.gateway.fail('base','other','known failure')
        failed=self.gateway.fail('base',token,'Known provider failure',outcome='known')
        self.assertEqual(failed['native_state'],'failed');self.assertIsNone(failed['claim_id'])
        self.assertEqual(failed['history'][-1]['token'],token)

    def test_missing_source_changed_binding_and_3d_are_explicit_errors(self):
        with self.assertRaises(RigError):NativeAcquisitionGateway(self.root,asset_kind='mesh')
        with self.assertRaises(RigError):NativeAcquisitionGateway(self.root,asset_kind='raster',binding={})
        binding=self.gateway.prepare()['binding'];request=self.root/'pet_request.json'
        value=hatch.read(request);value['pet_notes']='changed request';hatch.atomic(request,value)
        with self.assertRaises(RigError):NativeAcquisitionGateway(self.root,asset_kind='raster',binding=binding)

    def test_adapter_import_does_not_require_optional_3d(self):
        code='''import sys
class Block:
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in {'numpy','pygltflib','bpy','scipy'}:raise RuntimeError(fullname)
sys.meta_path.insert(0,Block())
import director_acquisition
'''
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class DirectorAcquisitionEnvelopeTests(unittest.TestCase):
    def setUp(self):
        DirectorAcquisitionTests.setUp(self)
        from director_acquisition import AcquisitionGateway
        self.adapter=AcquisitionGateway(self.gateway,self.root,'base')
        repo=Path(__file__).resolve().parents[1]
        self.templates=json.loads((repo/'examples/director-contracts/contracts.json').read_text())
        spec=json.loads((repo/'examples/director-contracts/asset-spec-fixture.json').read_text())
        spec.update(logical_id='base',asset_type='raster',formats=['png'])
        self.spec=self.ref('spec.json',json.dumps(spec).encode(),spec['id'],'director-asset-spec')

    def ref(self,path,payload,identity,kind,media='application/json'):
        target=self.root/path;target.write_bytes(payload)
        return dict(artifact_id=identity,kind=kind,schema_version=1,semantic_version='1.0.0',
                    path=path,sha256=hashlib.sha256(payload).hexdigest(),byte_size=len(payload),
                    media_type=media,provenance_ref=None,fixture=True,dependencies=[])

    def request(self,name,state='pending',claim=None,candidates=None,diagnostics=None):
        method='AcquisitionGateway.'+name
        value=deepcopy(next(x for x in self.templates if x.get('method')==method and x['kind']=='director-request'))
        value['input']=dict(spec=self.spec,input_sha256=self.adapter.input_sha256(),
            binding_id=self.gateway.binding['binding_id'],claim_id=claim,expected_state=state,
            provider_request_id=None,candidates=candidates or [],diagnostics=diagnostics or [])
        return value

    def test_canonical_claim_replay_and_immutable_evidence(self):
        from director_contracts import _read_artifact, validate_method_result
        prepared=self.adapter.prepare(self.request('prepare'))
        initial=prepared['value']['binding'];initial_bytes=_read_artifact(self.root,initial)
        request=self.request('claim')
        claim=self.adapter.claim(request)
        replay=self.adapter.claim(request)
        self.assertEqual(claim['value']['claim_id'],replay['value']['claim_id'])
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['attempts'],1)
        self.assertEqual(_read_artifact(self.root,initial),initial_bytes)
        validate_method_result('AcquisitionGateway.claim',replay,request)

    def test_canonical_ingest_exact_source_and_receipt(self):
        token=self.adapter.claim(self.request('claim'))['value']['claim_id']
        job=hatch.job_for(hatch.manifest(self.root),'base')
        receipt=dict(generation_skill='$fixture',job='base',token=token,input_hashes=job['input_hashes'])
        refs=[self.ref('candidate.png',self.source.read_bytes(),'candidate','acquisition-source','image/png'),
              self.ref('receipt.json',json.dumps(receipt).encode(),'receipt','acquisition-receipt')]
        result=self.adapter.ingest(self.request('ingest','running',token,refs))
        self.assertEqual(result['value']['native_state'],'generated')
        self.assertEqual(result['value']['claim_id'],token)
        self.assertTrue(result['value']['receipt']['fixture'])
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['source_origin'],'fixture')

    def test_canonical_uncertain_reason_survives_reopen(self):
        token=self.adapter.claim(self.request('claim'))['value']['claim_id']
        reason='Provider disconnected after submission; keep original ownership.'
        diagnostic=dict(code='NATIVE_ACQUISITION_OUTCOME',severity='warning',stage='acquisition',
            entity_id='base',field_path='outcome',expected=None,observed='uncertain',retryable=False,
            recommended_action=reason,evidence=[])
        result=self.adapter.fail(self.request('fail','running',token,diagnostics=[diagnostic]))
        self.assertEqual(result['value']['outcome'],'uncertain')
        reopened=NativeAcquisitionGateway(self.root,asset_kind='raster',binding=self.gateway.binding)
        current=reopened.reconcile('base')
        self.assertEqual(current['claim_id'],token)
        self.assertEqual(current['history'][-1]['reason'],reason)
        self.assertEqual(current['history'][-1]['event'],'outcome-uncertain')

    def test_canonical_rejects_stale_state_hash_and_wrong_spec_before_claim(self):
        for field,value in [('expected_state','generated'),('input_sha256','0'*64),('binding_id','wrong')]:
            request=self.request('claim');request['input'][field]=value
            with self.subTest(field=field),self.assertRaises(RigError):self.adapter.claim(request)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['attempts'],0)
        bad=deepcopy(self.spec);bad['sha256']='0'*64
        request=self.request('claim');request['input']['spec']=bad
        with self.assertRaises(RigError):self.adapter.claim(request)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['attempts'],0)

    def test_missing_source_does_not_release_claim(self):
        result=self.gateway.claim('base');token=result['claim_id']
        with self.assertRaises(RigError):
            self.gateway.ingest('base',token,self.parent/'missing.png',source_sha256=self.sha,
                                receipt=result['handoff']['receipt_template'],note='missing source')
        self.assertEqual(self.gateway.reconcile('base')['claim_id'],token)

    def test_production_claim_requires_both_explicit_policies_and_native_limits(self):
        from director_acquisition import AcquisitionGateway
        # Prepare a real native non-fixture run, but do not invoke any provider.
        root=self.parent/'production'
        hatch.prepare(root,argparse.Namespace(replace_inputs=False,fixture_mode=False,
            profile=str(self.parent/'profile.json'),pet_name='Production policy test',pet_notes='No provider execution',
            reference=[],avoid=[],require=[],brand_source=[],style_preset='clay',style_notes='policy only'))
        self.root=root
        self.gateway=NativeAcquisitionGateway(root,asset_kind='raster')
        self.adapter=AcquisitionGateway(self.gateway,root,'base')
        spec=json.loads((Path(__file__).resolve().parents[1]/'examples/director-contracts/asset-spec-fixture.json').read_text())
        spec.update(logical_id='base',asset_type='raster',formats=['png'])
        policy_ref=self.ref('policy.txt',b'Explicit bounded imagegen policy.','policy','policy','text/plain')
        admitted=dict(network='allow-configured',providers=['imagegen'],paid='bounded',policy_ref=policy_ref)
        spec['source_policy']=admitted
        self.spec=self.ref('spec.json',json.dumps(spec).encode(),spec['id'],'director-asset-spec')
        request=self.request('claim')
        with self.assertRaises(RigError):self.adapter.claim(request)
        request['context']['policy']=admitted
        with self.assertRaises(RigError):self.adapter.claim(request)
        request['context']['budget_reservation_id']='reservation-1'
        with self.assertRaises(RigError):self.adapter.claim(request)
        data=hatch.manifest(root)
        data['limits'].update(generation_calls=1,unit_budget=5,estimated_units_per_call=5)
        hatch.save(root,data)
        result=self.adapter.claim(request)
        self.assertEqual(result['value']['native_state'],'running')
        self.assertFalse(result['value']['binding']['fixture'])
        self.assertEqual(result['usage']['provider_calls']['value'],0)

    def test_deadline_rejected_before_native_claim(self):
        request=self.request('claim');request['context']['deadline']='2000-01-01T00:00:00Z'
        with self.assertRaises(RigError):self.adapter.claim(request)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['attempts'],0)

    def test_changed_exact_spec_cannot_reuse_native_acquisition(self):
        request=self.request('claim')
        first=self.adapter.claim(request)
        spec=json.loads((self.root/'spec.json').read_text())
        spec.update(visual_characteristics=['Different requested design'],licensing='INCOMPATIBLE-LICENSE')
        self.spec=self.ref('different-spec.json',json.dumps(spec).encode(),spec['id'],'director-asset-spec')
        with self.assertRaisesRegex(RigError,'different exact Director specification'):
            self.adapter.claim(self.request('claim','running',first['value']['claim_id']))
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['attempts'],1)

    def test_deadline_checked_again_after_native_lock_wait(self):
        from contextlib import contextmanager
        from datetime import datetime,timedelta,timezone
        from unittest.mock import patch
        import time
        original=self.gateway._locked
        @contextmanager
        def delayed():
            time.sleep(.08)
            with original() as data:yield data
        request=self.request('claim')
        request['context']['deadline']=(datetime.now(timezone.utc)+timedelta(seconds=.05)).isoformat()
        with patch.object(self.gateway,'_locked',delayed):
            with self.assertRaisesRegex(RigError,'waiting for native ownership'):
                self.adapter.claim(request)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['attempts'],0)

    def test_missing_inputs_allow_token_bound_recovery_and_known_failure(self):
        claim=self.adapter.claim(self.request('claim'))['value']['claim_id']
        reconcile=self.request('reconcile','running',claim)
        diagnostic=dict(code='NATIVE_ACQUISITION_OUTCOME',severity='warning',stage='acquisition',
            entity_id='base',field_path='outcome',expected=None,observed='known',retryable=False,
            recommended_action='Provider confirms no output; preserve failure before repair.',evidence=[])
        fail=self.request('fail','running',claim,diagnostics=[diagnostic])
        (self.root/'prompts/base-pet.md').unlink()
        # Reopening the API can still compute the original recovery binding.
        self.assertEqual(self.adapter.input_sha256(),reconcile['input']['input_sha256'])
        result=self.adapter.reconcile(reconcile)
        self.assertEqual(result['value']['claim_id'],claim)
        self.assertEqual(result['value']['outcome'],'uncertain')
        self.assertEqual(result['diagnostics'][0]['code'],'NATIVE_INPUTS_STALE')
        result=self.adapter.fail(fail)
        self.assertEqual(result['value']['native_state'],'failed')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root),'base')['status'],'failed')
