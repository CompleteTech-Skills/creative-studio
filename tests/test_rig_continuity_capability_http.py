"""Actual existing-loopback API admission with trusted native owner injection."""
from contextlib import contextmanager
from copy import deepcopy
import http.client
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from continuity_capabilities import Admission, Policy
from continuity_capability_owners import RigOwner, pointer
from continuity_contracts import VERSION
from rig_errors import RigError
from rig_http import RigServer


class CapabilityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'intake').mkdir();(self.root/'project').mkdir()
        self.project=self.root/'project'
        (self.project/'evidence').mkdir();(self.project/'project.json').write_bytes(b'{"fixture":true}')
        shutil.copytree(ROOT/'examples/rig-3d/robotic',self.project/'rig')
        self.owner=RigOwner(self.project,'rig/rig.json','evidence','geometry-inspection')
        self.engine=Admission(self.project,policy=Policy(asset_rights=True,allowed_owners=frozenset(['hatch-rig'])),owners=[self.owner])
        self.token='capability-fixture-test-only-not-a-credential'

    def tearDown(self):self.temp.cleanup()

    @contextmanager
    def server(self,engine=True):
        server=RigServer(self.root,self.root/'intake',token=self.token,continuity=self.engine if engine else None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:yield server
        finally:server.shutdown();thread.join(timeout=2);server.server_close()

    def request(self):
        return dict(schema_version=VERSION,kind='continuity-capability-request',request_id='http-fixture',
            project_revision=pointer(self.project,self.project/'project.json',kind='project-revision'),
            checks=[dict(id='geometry',capability='geometry-inspection',schema_version=VERSION,
                inputs=[self.owner.bound],input_types=['hatch-3d-rig'],output_types=['native-inspection'],
                resources=dict(memory_bytes=256*1024*1024,disk_bytes=8*1024*1024,cpu_seconds=30,gpu_bytes=0,concurrency=1),
                required_fields=[],allow_fallback=True,gpu_required=False,needs_visual_approval=False)])

    def call(self,server,method,path,body=None,**extra):
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        headers={'Authorization':'Bearer '+self.token,'Origin':f'http://127.0.0.1:{server.server_port}'}
        headers.update(extra)
        payload=json.dumps(body).encode() if body is not None else None
        if body is not None:headers['Content-Type']='application/json'
        try:
            conn.request(method,path,body=payload,headers=headers);response=conn.getresponse()
            return response.status,json.loads(response.read())
        finally:conn.close()

    def test_actual_authorized_native_admission_and_discovery(self):
        with self.server() as server:
            status,metadata=self.call(server,'GET','/v1/continuity/capabilities')
            self.assertEqual(status,200);self.assertEqual(len(metadata['capabilities']),13)
            status,result=self.call(server,'POST','/v1/continuity/admit',self.request())
            self.assertEqual(status,200,result);self.assertEqual(result['outcome'],'partial')
            self.assertEqual(result['integrity'],'verified')
            self.assertGreater(result['checks'][0]['measurements']['geometry']['vertices'],0)
            self.assertFalse(result['visual_approval'])

    def test_unconfigured_owner_default_is_structured_closed(self):
        with self.server(engine=False) as server:
            status,result=self.call(server,'POST','/v1/continuity/admit',self.request())
            self.assertEqual(status,400);self.assertEqual(result['error']['code'],'RIG_API_CONTINUITY_UNAVAILABLE')

    def test_auth_and_origin_are_checked_before_admission(self):
        with self.server() as server:
            for headers,status in (({'Authorization':'Bearer wrong'},401),({'Origin':'https://attacker.invalid'},403)):
                observed,_=self.call(server,'POST','/v1/continuity/admit',self.request(),**headers)
                self.assertEqual(observed,status)

    def test_http_cannot_change_root_policy_owner_or_command(self):
        with self.server() as server:
            for key,value in (('root','../outside'),('policy',{'asset_rights':True}),('owner','malicious.module'),('command','sh')):
                request=self.request();request[key]=value
                status,result=self.call(server,'POST','/v1/continuity/admit',request)
                self.assertEqual(status,400);self.assertEqual(result['error']['code'],'RIG_API_INPUT')
            request=self.request();request['project_revision']['path']='../outside'
            status,_=self.call(server,'POST','/v1/continuity/admit',request);self.assertEqual(status,400)

    def test_trusted_policy_denial_stays_denied_over_http(self):
        self.engine=Admission(self.project,policy=Policy(),owners=[self.owner])
        with self.server() as server:
            status,result=self.call(server,'POST','/v1/continuity/admit',self.request())
            self.assertEqual(status,200);self.assertEqual(result['outcome'],'failed')
            self.assertIn('permission',result['checks'][0]['unsatisfied_gates'])

    def test_worker_budget_and_unknown_queries_preserved(self):
        with self.server() as server:
            server.budget.acquire();server.budget.acquire()
            try:
                status,_=self.call(server,'GET','/v1/continuity/capabilities');self.assertEqual(status,429)
            finally:server.budget.release();server.budget.release()
            status,_=self.call(server,'GET','/v1/continuity/capabilities?root=outside');self.assertEqual(status,400)

    def test_outside_configured_root_and_changed_root_binding_rejected(self):
        outside=self.root.parent
        invalid=Admission(outside,policy=Policy())
        with self.assertRaises(RigError):RigServer(self.root,self.root/'intake',token=self.token,continuity=invalid)
        with self.server() as server:
            self.engine.root=outside
            status,result=self.call(server,'POST','/v1/continuity/admit',self.request())
            self.assertEqual(status,400);self.assertEqual(result['error']['code'],'RIG_API_CONTINUITY_ROOT')

    def test_missing_backend_is_exactly_blocked_over_http(self):
        request=self.request();request['checks'][0].update(capability='physics-continuation',id='physics')
        with self.server() as server:
            status,result=self.call(server,'POST','/v1/continuity/admit',request)
            self.assertEqual(status,200);self.assertEqual(result['outcome'],'blocked_missing_capability')
            self.assertIn('exact-continuation',result['checks'][0]['missing_fields'])
