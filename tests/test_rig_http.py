import http.client
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import tempfile
import threading
import unittest
from rig_test_helpers import fixture
from rig_http import RigServer,RigHandler
from rig_errors import RigError


class RigHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.rig=fixture(self.temp.name)
        self.server=RigServer(self.temp.name,self.temp.name,token='test-fixture-token-0123456789')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
    def request(self,method,path,body=None,*,token=True,origin=None,authorization=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+self.server.token
        if authorization is not None:headers['Authorization']=authorization
        if origin:headers['Origin']=origin
        connection.request(method,path,json.dumps(body).encode() if body is not None else None,headers)
        response=connection.getresponse();value=json.loads(response.read());status=response.status;connection.close();return status,value
    def test_token_origin_and_intake_paths_are_enforced(self):
        self.assertEqual(self.request('GET','/v1/rigs/fixture',token=False)[0],401)
        self.assertEqual(self.request('GET','/v1/rigs/fixture',origin='https://evil.invalid')[0],403)
        self.assertEqual(self.request('POST','/v1/rig-jobs',{'model':'../outside.glb'})[0],400)
    def test_import_status_analyze_and_artifacts(self):
        code,data=self.request('POST','/v1/rig-jobs',{'model':'fixture.glb','id':'api-import'})
        self.assertEqual(code,200,data);self.assertTrue(data['fixture'])
        self.assertEqual(self.request('GET','/v1/rig-jobs/api-import')[0],200)
        self.assertEqual(self.request('POST','/v1/rig-jobs/api-import/analyze',{})[0],200)
        code,data=self.request('GET','/v1/rig-jobs/api-import/artifacts');self.assertEqual(code,200);self.assertTrue(data['artifacts'])
    def test_pose_ik_validation_and_export_are_real(self):
        code,pose=self.request('POST','/v1/rigs/fixture/pose',{'clip':'bend','time':.5});self.assertEqual(code,200,pose);self.assertIn('b1',pose['bones'])
        code,value=self.request('POST','/v1/rigs/fixture/solve-ik',{'chain':'arm','target':[1,1,0]});self.assertEqual(code,200,value);self.assertLess(value['report']['residual_m'],1e-5)
        code,value=self.request('POST','/v1/rig-jobs/fixture/validate',{'require_animation':True});self.assertEqual(code,200,value)
        code,value=self.request('POST','/v1/rig-jobs/fixture/export',{'format':'glb','destination':'exports/api.glb'});self.assertEqual(code,200,value);self.assertTrue((self.rig.root/'exports/api.glb').is_file())
        self.assertEqual(self.request('POST','/v1/rig-jobs/fixture/export',{'format':'glb','destination':'../outside.glb'})[0],400)
    def test_unknown_parameters_and_routes_fail(self):
        self.assertEqual(self.request('POST','/v1/rig-jobs/fixture/skin',{'arbitrary':'code'})[0],400)
        self.assertEqual(self.request('GET','/v1/rig-jobs/absent')[0],404)
        code,value=self.request('POST','/v1/rig-jobs/fixture/generate',{'template_source':'other'})
        self.assertEqual(code,400,value)
        self.assertEqual(value['error']['code'],'RIG_OPERATION_SCHEMA')

    def test_malformed_or_non_ascii_authorization_returns_structured_401(self):
        for header in ('Bearer café','Basic credentials','Bearer wrong','', 'Bearer '+'a'*4097):
            with self.subTest(header=header[:40]):
                code,value=self.request('GET','/v1/rigs/fixture',authorization=header)
                self.assertEqual(code,401,value)
                self.assertEqual(value['error']['code'],'RIG_API_AUTH')
        self.assertEqual(self.request('GET','/v1/rigs/fixture')[0],200)
        handler=object.__new__(RigHandler);handler.server=SimpleNamespace(token=self.server.token)
        handler.headers=http.client.HTTPMessage();handler.headers['Authorization']='Bearer ☃';responses=[]
        handler._respond=lambda *args:responses.append(args)
        handler._run()
        self.assertEqual(responses[0][0],401)
        self.assertEqual(responses[0][1]['error']['code'],'RIG_API_AUTH')

    def test_configured_token_validation_is_structured_and_default_remains_valid(self):
        for token in ('é'*24,'a'*4097,'a'*23,'a'*24+'\n','',True,10**400):
            with self.subTest(token_type=type(token).__name__),self.assertRaises(RigError) as failure:
                RigServer(self.temp.name,self.temp.name,token=token)
            self.assertEqual(failure.exception.report['code'],'RIG_API_TOKEN')
        server=RigServer(self.temp.name,self.temp.name)
        try:self.assertGreaterEqual(len(server.token),24);self.assertTrue(server.token.isascii())
        finally:server.server_close()

    def test_http_generate_rejects_custom_objects_paths_and_cwd_json(self):
        from rig_templates import ROOT,load_rig_template
        local=Path(self.temp.name)/'local-template.json'
        local.write_bytes((ROOT/'custom.json').read_bytes())
        original=Path.cwd()
        try:
            os.chdir(self.temp.name)
            self.assertEqual(load_rig_template(local.name)['id'],'custom')
            for template in ({'custom':'object'},[],None,False,1,'local-template.json','humanoid.json',
                             './humanoid','rig-templates/humanoid.json',str((ROOT/'humanoid.json').resolve())):
                with self.subTest(template=template):
                    code,value=self.request('POST','/v1/rig-jobs/fixture/generate',{'template':template})
                    self.assertEqual(code,400,value);self.assertEqual(value['error']['code'],'RIG_API_TEMPLATE')
            self.assertEqual(self.request('POST','/v1/rig-jobs/fixture/generate',{'template':'humanoid'})[0],200)
        finally:os.chdir(original)

    def test_dispatch_resolves_exact_installed_ids_before_native_mutation(self):
        from rig_templates import ROOT,list_templates
        # Admission records the ID. Check the historical absolute path guarantee
        # at the point where the generator actually opens the installed file.
        def observe(parameters):
            reached=RigError('RIG_TEST_GENERATOR','Observed installed execution input.')
            with patch('rig_templates.generate_skeleton',side_effect=reached) as generate:
                code,value=self.request('POST','/v1/rig-jobs/fixture/generate',{**parameters,'replace':True})
                self.assertEqual(code,400,value)
                self.assertEqual(value['error']['code'],'RIG_TEST_GENERATOR')
                return generate.call_args.args[1]

        for template in list_templates():
            with self.subTest(template=template['id']):
                self.assertEqual(observe({'template':template['id']}),str((ROOT/(template['id']+'.json')).resolve()))
        self.assertEqual(observe({}),str((ROOT/'humanoid.json').resolve()))

    def test_http_template_corrections_share_installed_id_admission(self):
        from copy import deepcopy
        from rig_errors import timestamp
        from rig_models import rig_signature
        correction=dict(schema_version=1,kind='hatch-3d-rig-correction',rig_id=self.rig.manifest['id'],
            rig_version=self.rig.manifest['version'],expected_sha256=rig_signature(self.rig.manifest),
            author='http-fixture',reason='Use installed template',created_at=timestamp(),
            edits=[dict(operation='template',target='skeleton',value={'template':'custom'})])
        for template in ({'custom':'object'},'local-template.json','custom.json','./custom',None):
            bad=deepcopy(correction);bad['edits'][0]['value']['template']=template
            with self.subTest(template=template):
                code,value=self.request('PATCH','/v1/rigs/fixture',bad)
                self.assertEqual(code,400,value);self.assertEqual(value['error']['code'],'RIG_API_TEMPLATE')
        for edits in (None,'bad',[None],[{'operation':'template','value':None}]):
            bad=deepcopy(correction);bad['edits']=edits
            with self.subTest(edits=edits):
                code,value=self.request('PATCH','/v1/rigs/fixture',bad)
                self.assertEqual(code,400,value)
                self.assertIn(value['error']['code'],('RIG_SCHEMA','RIG_API_TEMPLATE'))
        code,value=self.request('PATCH','/v1/rigs/fixture',correction)
        self.assertEqual(code,200,value)
        from rig_models import load_rig
        loaded=load_rig(self.rig.root)
        self.assertEqual([bone['role'] for bone in loaded.skeleton['bones']],['root'])

    def test_template_correction_dispatch_normalizes_only_template_values(self):
        from rig_templates import ROOT
        handler=object.__new__(RigHandler);handler.path='/v1/rigs/fixture';handler.command='PATCH'
        handler._root=lambda identifier:self.rig.root
        body={'edits':[{'operation':'template','value':{'template':'custom'}},
                       {'operation':'expression','value':{'name':'unchanged'}}]}
        handler._body=lambda:body
        # This historical wiring fixture omits envelope/target fields. Full
        # HTTP requests below exercise the unmocked native binding checks.
        with patch('rig_corrections.validate_correction_binding'), \
             patch('rig_corrections.apply_rig_corrections',return_value=(self.rig,{})) as apply:
            handler._dispatch()
            edits=apply.call_args.args[1]['edits']
            resolve=apply.call_args.kwargs['resolve_template']
            self.assertEqual(resolve(edits[0]['value']['template']),str((ROOT/'custom.json').resolve()))
            self.assertEqual(edits[1],{'operation':'expression','value':{'name':'unchanged'}})
            self.assertEqual(edits[0]['value']['template'],'custom')

    def correction(self, *edits):
        from rig_errors import timestamp
        from rig_models import load_rig,rig_signature
        manifest=load_rig(self.rig.root).manifest
        return dict(schema_version=1,kind='hatch-3d-rig-correction',rig_id=manifest['id'],
                    rig_version=manifest['version'],expected_sha256=rig_signature(manifest),
                    author='fixture',reason='Select installed template',created_at=timestamp(),edits=list(edits))

    def test_duplicate_authorization_is_refused(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        self.addCleanup(connection.close)
        connection.putrequest('GET','/v1/rigs/fixture')
        for _ in range(2):
            connection.putheader('Authorization','Bearer '+self.server.token)
        connection.endheaders()
        response=connection.getresponse()
        self.assertEqual(response.status,401)
        self.assertEqual(json.loads(response.read())['error']['code'],'RIG_API_AUTH')

    def test_patch_validates_binding_before_loading_one_catalogue(self):
        from rig_templates import installed_template_paths
        template_edit=dict(operation='template',target='skeleton',value={'template':'custom'})
        with patch('rig_templates.installed_template_paths',wraps=installed_template_paths) as catalogue:
            oversized=self.correction(*([template_edit]*1025))
            code,value=self.request('PATCH','/v1/rigs/fixture',oversized)
            self.assertEqual(code,400,value)
            self.assertEqual(value['error']['code'],'RIG_SCHEMA')
            self.assertEqual(value['error']['expected'],1024)
            self.assertEqual(value['error']['observed'],1025)
            stale=self.correction(template_edit)
            stale['expected_sha256']='0'*64
            code,value=self.request('PATCH','/v1/rigs/fixture',stale)
            self.assertEqual(code,409,value)
            self.assertEqual(value['error']['code'],'RIG_STALE_CORRECTION')
            catalogue.assert_not_called()
            expression=dict(operation='expression',target='unused',value=None)
            accepted=self.correction(template_edit,*([expression]*1022),template_edit)
            code,value=self.request('PATCH','/v1/rigs/fixture',accepted)
            self.assertEqual(code,200,value)
            code,value=self.request('PATCH','/v1/rigs/fixture',accepted)
            self.assertEqual(code,200,value)
            self.assertEqual(value['status'],'cached')
            catalogue.assert_called_once_with(['custom','custom'])

    def test_installed_template_identity_survives_status_correction_and_archive(self):
        from rig_errors import object_hash
        from rig_models import load_rig
        from rig_package import package_rig
        from rig_templates import ROOT
        from zipfile import ZipFile
        request={'template':'custom','replace':True}
        code,value=self.request('POST','/v1/rig-jobs/fixture/generate',request)
        self.assertEqual(code,200,value)
        self.assertEqual([bone['role'] for bone in load_rig(self.rig.root).skeleton['bones']],['root'])
        correction=self.correction(dict(operation='template',target='skeleton',value={'template':'custom'}))
        code,value=self.request('PATCH','/v1/rigs/fixture',correction)
        self.assertEqual(code,200,value)
        code,status=self.request('GET','/v1/rig-jobs/fixture')
        self.assertEqual(code,200,status)
        auto={**request,'template_source':'installed'}
        correct={'correction':correction,'template_source':'installed'}
        receipt='rig/corrections/'+object_hash(correction)+'.json'
        self.assertEqual(json.loads((self.rig.root/receipt).read_bytes()),correction)
        archive=Path(self.temp.name)/'rig.zip'
        package_rig(self.rig.root,archive)
        with ZipFile(archive) as package:
            archived=json.loads(package.read('job.json'))
            self.assertEqual(json.loads(package.read(receipt)),correction)
        for job in (archived,status):
            for step,parameters in [('auto',auto),('correct',correct)]:
                self.assertEqual(job['steps'][step]['parameters'],parameters)
                self.assertEqual(job['steps'][step]['parameters_sha256'],object_hash(parameters))
            self.assertNotIn(str(ROOT.resolve()),json.dumps(job))

    def test_resume_uses_recorded_template_source_for_same_spelling(self):
        from contextlib import chdir
        from rig_models import load_rig
        from rig_operations import mutate,resume
        from rig_templates import ROOT
        directory=Path(self.temp.name)
        installed=directory/'catalogue'
        installed.mkdir()
        parent=json.loads((ROOT/'custom.json').read_bytes())
        parent['id']='base'
        (installed/'base.json').write_text(json.dumps(parent))
        child={**parent,'id':'character.json','extends':['base.json'],'bones':[]}
        (installed/'installed-file.json').write_text(json.dumps(child))
        native={**parent,'id':'native-character'}
        native['bones'][0]['role']='native-root'
        (directory/'character.json').write_text(json.dumps(native))
        native_job=fixture(directory,name='native')
        params={'template':'character.json','replace':True}
        with chdir(directory),patch('rig_templates.ROOT',installed):
            with patch('rig_templates.generate_skeleton',side_effect=RigError('RIG_TEST_FAILURE','Synthetic interruption.')):
                self.assertEqual(self.request('POST','/v1/rig-jobs/fixture/generate',params)[0],400)
                with self.assertRaises(RigError):
                    mutate(native_job.root,'auto',params)
            for job,expected_id,expected_role in [(self.rig,'character.json','root'),
                                                  (native_job,'native-character','native-root')]:
                report=resume(job.root)
                self.assertEqual([item['status'] for item in report['resumed']],['succeeded'])
                loaded=load_rig(job.root)
                self.assertEqual(loaded.manifest['archetype'],expected_id)
                self.assertEqual([bone['role'] for bone in loaded.skeleton['bones']],[expected_role])
            correction=self.correction(dict(operation='template',target='skeleton',value={'template':'character.json'}))
            result=mutate(self.rig.root,'correct',{'correction':correction})
            self.assertEqual(result['status'],'succeeded')
            self.assertEqual([bone['role'] for bone in load_rig(self.rig.root).skeleton['bones']],['native-root'])
