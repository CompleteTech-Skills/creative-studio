"""Strict envelopes over real SQLite jobs and decoded synthetic PNG fixtures.

No Blender, provider, production image or artistic approval is implied by these
tests. The actual immutable recorder requires the canonical publication helper.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import unittest

import director_contracts as dc
from director_execution_facade import ExecutionServiceFacade, ImmutableExecutionRecorder
from rig_errors import object_hash

ROOT = Path(__file__).resolve().parents[1]


class ExecutionFacadeTests(unittest.TestCase):
    def setUp(self):
        from test_rig_director_execution import ExecutionServiceTests
        self.native = ExecutionServiceTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        self.trusted = dict(project_id='native-project', project_revision_sha256='1' * 64)
        self.graph_ref = self.reference('graph.json', self.native.graph, kind='execution-graph')
        self.facade = self.configured()

    def reference(self, name, document, *, kind='test-fixture'):
        payload = (json.dumps(document, sort_keys=True) + '\n').encode()
        (self.root / name).write_bytes(payload)
        return dict(artifact_id='fixture-' + hashlib.sha256(payload).hexdigest(), kind=kind,
            schema_version=1, semantic_version='1.0.0', path=name,
            sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload),
            media_type='application/json', provenance_ref=None, fixture=True, dependencies=[])

    def configured(self, recorder=None):
        def resolve(graph, root):
            self.assertEqual(graph, self.native.graph)
            self.assertEqual(root, self.root)
            return deepcopy(self.native.bindings)
        return ExecutionServiceFacade(self.native.service, binding_resolver=resolve,
            capabilities=self.native.capabilities, input_root=self.root,
            project_context=lambda: deepcopy(self.trusted), artifact_recorder=recorder,
            clock=lambda: self.native.clock_value)

    def request(self, operation, snapshot=None):
        value = deepcopy(next(document for document in json.loads((ROOT / 'examples/director-contracts/contracts.json').read_text())
            if document.get('kind') == 'director-request' and document.get('method') == 'ExecutionService.' + operation))
        context = value['context']
        context.update(self.trusted, request_id='request-' + operation, idempotency_key='key-' + operation,
                       cancellation_token='native-owner-token', capability_snapshot_sha256=self.native.capabilities['digest'],
                       deadline=(self.native.now + timedelta(hours=1)).isoformat())
        if operation == 'submit':
            value['input']['graph'] = deepcopy(self.graph_ref)
        else:
            value['input'].update(job_id=snapshot['job_id'], expected_revision=snapshot['revision'], request_id='request-submit')
        return value

    def submitted(self):
        result = self.facade.submit(self.request('submit'), self.root)
        self.assertEqual(result['status'], 'succeeded', result['diagnostics'])
        dc.validate_method_result('ExecutionService.submit', result, self.request('submit'))
        return result['value']

    def test_real_submission_replay_and_fresh_status_operation_identity(self):
        first = self.submitted()
        replay = self.facade.submit(self.request('submit'), self.root)
        self.assertEqual(replay['value']['job_id'], first['job_id'])
        self.assertEqual(len(self.native.store.list_jobs()), 3)
        query = self.request('status', first)
        query['context']['request_id'] = 'new-read-operation'
        result = self.facade.status(query, self.root)
        self.assertEqual(result['status'], 'succeeded', result['diagnostics'])
        self.assertEqual(result['operation_id'], 'new-read-operation')
        self.assertEqual(result['value']['job_id'], first['job_id'])
        self.assertEqual(result['input_digest'], object_hash(query))

    def test_expired_deadline_never_publishes_a_graph(self):
        request = self.request('submit'); request['context']['deadline'] = '2000-01-01T00:00:00Z'
        result = self.facade.submit(request, self.root)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['value'])
        with self.native.store._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM director_runs').fetchone()[0], 0)

    def test_wrong_project_policy_capability_and_root_do_not_admit(self):
        for field, value in (('project_id', 'other-project'), ('project_revision_sha256', '2' * 64),
                             ('capability_snapshot_sha256', '3' * 64)):
            with self.subTest(field=field):
                request = self.request('submit'); request['context'][field] = value
                result = self.facade.submit(request, self.root)
                self.assertNotEqual(result['status'], 'succeeded')
                self.assertIsNone(result['value'])
        request = self.request('submit')
        request['context']['policy']['providers'] = ['not-authorized']
        self.assertEqual(self.facade.submit(request, self.root)['status'], 'failed')
        self.assertEqual(self.facade.submit(self.request('submit'), self.root / 'source')['status'], 'failed')
        with self.native.store._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM director_runs').fetchone()[0], 0)

    def test_cancellation_requires_original_token_original_request_and_current_revision(self):
        submitted = self.submitted()
        original = self.request('cancel', submitted)
        for mutation in ('token', 'request', 'revision'):
            request = deepcopy(original)
            if mutation == 'token': request['context']['cancellation_token'] = 'foreign-token'
            elif mutation == 'request': request['input']['request_id'] = 'unrelated-submit'
            else: request['input']['expected_revision'] = '0' * 64
            result = self.facade.cancel(request, self.root)
            self.assertEqual(result['status'], 'failed')
            if mutation == 'revision':
                self.assertEqual(result['diagnostics'][0]['expected'], '0' * 64)
                self.assertEqual(result['diagnostics'][0]['observed'], submitted['revision'])
            self.assertFalse(any(self.native.store.status(job)['cancel'] for job in self.native.jobs.values()))
        cancelled = self.facade.cancel(original, self.root)
        self.assertEqual(cancelled['status'], 'succeeded', cancelled['diagnostics'])
        self.assertEqual(cancelled['value']['state'], 'canceled')
        self.assertTrue(all(self.native.store.status(job)['cancel'] for job in self.native.jobs.values()))

    def test_native_completed_bytes_without_recorder_never_become_canonical_completion(self):
        submitted = self.submitted()
        for node in self.native.jobs: self.native.publish_native(node)
        result = self.facade.status(self.request('status', submitted), self.root)
        self.assertEqual(result['status'], 'blocked_capability', result['diagnostics'])
        self.assertIsNone(result['value'])
        self.assertIn('recorder', result['diagnostics'][0]['recommended_action'])

    def test_forged_worker_receipt_is_rejected_even_when_its_hash_is_valid(self):
        submitted = self.submitted()
        for node in self.native.jobs: self.native.publish_native(node)
        forged = self.reference('forged-worker.json', dict(node='a', native_receipt={'status': 'complete'}), kind='native-worker-receipt')
        facade = self.configured(lambda snapshot, root: dict(nodes=[], native_workers=[forged], claims=[], artifacts=[]))
        result = facade.status(self.request('status', submitted), self.root)
        self.assertEqual(result['status'], 'blocked_capability')
        self.assertIsNone(result['value'])
        self.assertIn('actual owner', result['diagnostics'][0]['recommended_action'])

    def test_changed_pinned_graph_is_rejected_before_submission(self):
        path = self.root / self.graph_ref['path']; path.write_bytes(path.read_bytes() + b' ')
        result = self.facade.submit(self.request('submit'), self.root)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['value'])
        with self.native.store._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM director_runs').fetchone()[0], 0)

    def test_resume_requires_exact_graph_and_current_native_ownership_evidence(self):
        submitted = self.submitted()
        cancelled = self.facade.cancel(self.request('cancel', submitted), self.root)['value']
        request = self.request('resume', cancelled)
        request['input'].update(dependency_snapshot=deepcopy(self.graph_ref), ownership_evidence=[])
        rejected = self.facade.resume(request, self.root)
        self.assertEqual(rejected['status'], 'failed')
        native = self.native.service.status_graph(cancelled['job_id'])
        request['input']['ownership_evidence'] = [self.reference('owner-' + node + '.json',
            dict(node=node, native_receipt=state['native']['receipt']), kind='native-worker-receipt')
            for node, state in native['nodes'].items()]
        result = self.facade.resume(request, self.root)
        self.assertEqual(result['status'], 'succeeded', result['diagnostics'])
        self.assertEqual(result['value']['state'], 'queued')

    def test_actual_canonical_recorder_exports_verified_native_bytes_and_runtime_metadata(self):
        # This is a real required dependency gate. If the shared canonical
        # publication helper is absent, fail clearly rather than skip or fall
        # back to an unsafe Path publisher.
        submitted = self.submitted()
        for node in self.native.jobs: self.native.publish_native(node)
        recorder = ImmutableExecutionRecorder(self.native.service)
        facade = self.configured(recorder)
        request = self.request('status', submitted)
        result = facade.status(request, self.root)
        self.assertEqual(result['status'], 'succeeded', result['diagnostics'])
        self.assertEqual(result['value']['state'], 'completed')
        self.assertEqual(len(result['value']['artifacts']), 6)
        self.assertEqual(len(result['value']['native_workers']), 3)
        for reference in result['value']['nodes']:
            observation = json.loads(dc._read_artifact(self.root, reference))
            metadata = dc.validate_execution_metadata(observation['execution_metadata'], planned=False)
            self.assertEqual(metadata['execution_status'], 'validated')
            self.assertEqual(metadata['actual_usage']['classification'], 'unavailable')
            self.assertTrue(all(metadata['actual_usage'][name]['value'] is None for name in
                ('provider_calls', 'input_tokens', 'output_tokens', 'cpu_seconds', 'gpu_seconds', 'storage_bytes', 'amount')))
        again = facade.status(request, self.root)
        self.assertEqual(again['value'], result['value'])
        replay = facade.submit(self.request('submit'), self.root)
        self.assertEqual(replay['status'], 'succeeded', replay['diagnostics'])
        self.assertEqual(replay['value']['state'], 'queued')
        self.assertIn('cache reuse', replay['diagnostics'][0]['recommended_action'])


if __name__ == '__main__':
    unittest.main()
