"""Actual loopback HTTP transport tests; no live model execution is asserted."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

import director_planning as dp

ROOT = Path(__file__).resolve().parents[1]


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.documents = json.loads((ROOT / 'examples/director-contracts/contracts.json').read_text())
        self.request = deepcopy(next(d for d in self.documents if d.get('method') == 'PlanningProvider.generate_structured' and d['kind'] == 'director-request'))
        self.request['context']['deadline'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        spec = dp.template('interpreter')
        self.request['input'].update(stage='brief', template_sha256=spec['sha256'], output_schema_id=spec['output_schema_id'], context_refs=[], max_repairs=1)
        self.request['context']['policy'].update(network='allow-configured', providers=['fixture'], paid='deny')
        policy=Path(self.temp.name)/'policy.json'
        policy.write_text('{}')
        self.request['context']['policy']['policy_ref']={'artifact_id':'fixture-policy','kind':'policy','schema_version':1,'semantic_version':'1.0.0','path':'policy.json','sha256':hashlib.sha256(policy.read_bytes()).hexdigest(),'byte_size':2,'media_type':'application/json','provenance_ref':None,'fixture':True,'dependencies':[]}
        self.brief = deepcopy(next(d for d in self.documents if d['kind'] == 'director-creative-brief'))
        self.calls = []

    def server(self, replies):
        calls = self.calls
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((body, self.headers['Idempotency-Key']))
                code, value = replies[min(len(calls)-1, len(replies)-1)]
                raw = json.dumps(value).encode()
                self.send_response(code)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return dp.ProviderConfig('fixture','fixture-model',f'http://127.0.0.1:{server.server_port}/v1/responses',allowed_hosts=('127.0.0.1',),paid=False,fixture_transport=True)

    def response(self, content):
        return {'status':'completed','model':'fixture-model','usage':{'input_tokens':7,'output_tokens':3},'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(content)}]}]}

    def run_request(self, config=None):
        return dp.generate_structured(self.request,self.temp.name,project_root=self.temp.name,configuration=config)

    def test_unconfigured_is_controlled(self):
        result = self.run_request()
        self.assertEqual(result['status'],'blocked_capability')
        self.assertEqual(result['usage']['provider_calls']['value'],0)

    def test_actual_http_and_immutable_replay(self):
        config = self.server([(200,self.response(self.brief))])
        result = self.run_request(config)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.run_request(config),result)
        self.assertEqual(len(self.calls),1)
        self.assertTrue(all(a['fixture'] for a in result['artifacts']))
        self.assertEqual(result['usage']['input_tokens']['value'],7)
        self.assertIn('untrusted',self.calls[0][0]['input'][0]['content'])

    def test_schema_repair_is_bounded_and_usage_accumulates(self):
        config=self.server([(200,self.response({})),(200,self.response(self.brief))])
        result=self.run_request(config)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(result['usage']['input_tokens']['value'],14)
        self.assertEqual(len(result['value']['repair_history']),1)
        self.assertNotEqual(self.calls[0][1],self.calls[1][1])

    def test_schema_exhaustion_is_controlled(self):
        result=self.run_request(self.server([(200,self.response({}))]))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(result['diagnostics'][0]['code'],'DIRECTOR_PLANNING_SCHEMA')

    def test_rate_limit_no_retry(self):
        config=self.server([(429,{})])
        result=self.run_request(config)
        self.assertEqual(result['status'],'blocked_capability')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.run_request(config),result)

    def test_paid_policy_denied_before_transport(self):
        config=self.server([(200,self.response(self.brief))])
        config=dp.ProviderConfig(**{**config.__dict__,'paid':True})
        self.assertEqual(self.run_request(config)['status'],'blocked_budget')
        self.assertEqual(self.calls,[])

    def test_endpoint_rejected_before_transport(self):
        config=dp.ProviderConfig('fixture','fixture-model','http://[broken',paid=False)
        self.assertEqual(self.run_request(config)['status'],'blocked_capability')

    def test_template_hash_mismatch_prevents_call(self):
        config=self.server([(200,self.response(self.brief))])
        self.request['input']['template_sha256']='f'*64
        self.assertEqual(self.run_request(config)['status'],'failed')
        self.assertEqual(self.calls,[])

    def test_prior_dispatch_claim_blocks_new_paid_attempt(self):
        config=self.server([(200,self.response(self.brief))])
        first=self.run_request(config)
        self.request['context']['request_id']='another-request'
        result=self.run_request(config)
        self.assertEqual(result['status'],'awaiting_external_result')
        self.assertEqual(len(self.calls),1)

    def test_malformed_response_is_controlled(self):
        result=self.run_request(self.server([(200,[])]))
        self.assertEqual(result['status'],'failed')

    def test_all_role_templates_have_valid_schema_and_distinct_hashes(self):
        values=[dp.template(role) for role in dp.ROLES]
        self.assertEqual(len({s['sha256'] for s in values}),9)
        for value in values:
            dp.Draft202012Validator.check_schema(value['schema'])

if __name__ == '__main__': unittest.main()
