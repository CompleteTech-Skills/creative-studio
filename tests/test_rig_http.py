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

    def test_malformed_or_non_ascii_authorization_returns_structured_401(self):
        for header in ('Bearer café','Basic credentials','Bearer wrong','', 'Bearer '+'a'*4097):
            with self.subTest(header=header[:40]):
                code,value=self.request('GET','/v1/rigs/fixture',authorization=header)
                self.assertEqual(code,401,value)
                self.assertEqual(value['error']['code'],'RIG_API_AUTH')
        self.assertEqual(self.request('GET','/v1/rigs/fixture')[0],200)
        handler=object.__new__(RigHandler);handler.server=SimpleNamespace(token=self.server.token)
        handler.headers={'Authorization':'Bearer ☃'};responses=[]
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
        handler=object.__new__(RigHandler);handler.path='/v1/rig-jobs/fixture/generate';handler.command='POST'
        handler._root=lambda identifier:identifier
        for template in list_templates():
            handler._body=lambda template=template:{'template':template['id']}
            with patch('rig_models.load_rig',return_value=SimpleNamespace()),patch('rig_operations.mutate') as mutate:
                handler._dispatch()
                parameters=mutate.call_args.args[2]
                self.assertEqual(parameters['template'],str((ROOT/(template['id']+'.json')).resolve()))
        handler._body=lambda:{}
        with patch('rig_models.load_rig',return_value=SimpleNamespace()),patch('rig_operations.mutate') as mutate:
            handler._dispatch()
            self.assertEqual(mutate.call_args.args[2]['template'],str((ROOT/'humanoid.json').resolve()))

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
        handler._root=lambda identifier:identifier
        body={'edits':[{'operation':'template','value':{'template':'custom'}},
                       {'operation':'expression','value':{'name':'unchanged'}}]}
        handler._body=lambda:body
        with patch('rig_models.load_rig',return_value=SimpleNamespace()),patch('rig_operations.mutate') as mutate:
            handler._dispatch()
            edits=mutate.call_args.args[2]['correction']['edits']
            self.assertEqual(edits[0]['value']['template'],str((ROOT/'custom.json').resolve()))
            self.assertEqual(edits[1],{'operation':'expression','value':{'name':'unchanged'}})
