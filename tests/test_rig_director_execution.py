"""Persistent DAG acceptance over real SQLite jobs and synthetic PNG caches.

Capability receipts and PNG masters are explicitly synthetic test inputs. They
exercise admission/persistence/failure behavior, not Blender production output.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import inspect
import unittest

from director_budget import BudgetLedger, METRICS
from director_execution import ExecutionService, retry_delay
from director_execution_drivers import RenderJobDriver
from director_schema import METHODS
from rig_errors import RigError, object_hash


class RejectingWorker:
    """Failure injection at the real native worker invocation boundary."""
    def render_frames(self, *args, **kwargs):
        raise RuntimeError('injected worker failure before producing output')


class ExecutionServiceTests(unittest.TestCase):
    def setUp(self):
        from test_render3d_jobs import JobTests
        from test_director_graph import DirectorGraphTests
        from test_rig_director_job_graph import graph_for_job
        self.fixture = JobTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.store = self.fixture.root.resolve(), self.fixture.store
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.clock_value = self.now.timestamp()
        capability = DirectorGraphTests()
        capability.setUp()
        capability.root = self.root
        self.addCleanup(capability.doCleanups)
        limits = deepcopy(capability.snapshot['capabilities'][0]['limits'])
        limits.update(max_memory_bytes=2 * 1024**3, max_storage_bytes=4 * 1024**3)
        evidence = capability.write('scheduler-cpu-proof', dict(operation='blender.cpu',
            version='test', status='available', execution_verified=True, fixture=True,
            policy_limits=limits, payloads=[]), kind='evidence')
        self.capabilities = dict(capabilities=[dict(operation='blender.cpu', version='test',
            status='available', execution_verified=True, limits=limits, probe_evidence=[evidence])],
            expires_at=(self.now + timedelta(hours=1)).isoformat())
        self.capabilities['digest'] = object_hash(self.capabilities)
        quantities = {key: 0 for key in METRICS}
        quantities.update(cpu_seconds=10000, storage_bytes=32 * 1024**3)
        self.ledger = BudgetLedger.create(self.root / 'ledger', quantities,
                                         provider_limits={'local': quantities})
        self.graph = dict(nodes=[], edges=[], critical_path_seconds=None)
        self.bindings, self.jobs = {}, {}
        for name in ('a', 'b', 'c'):
            request = deepcopy(self.fixture.request)
            request.update(id='request-' + name)
            request['destination'].update(path='output/' + name, owner_token='owner-' + name)
            job = self.store.submit(request, self.root / 'snapshot', self.fixture.backend)
            self.jobs[name] = job
            graph, native = graph_for_job(self.store, job)
            node = graph['nodes'][0]
            node.update(id=name, native_owner='RenderAdapter', gates=['blender.cpu'],
                        output_schema=METHODS['RenderAdapter.submit'][1])
            self.graph['nodes'].append(node)
            estimate = {key: 0 for key in METRICS}
            estimate.update(cpu_seconds=1, storage_bytes=1024**3)
            reservation = self.ledger.reserve(operation_id='operation-' + name, provider='local',
                estimate=estimate, retry_allowance=1, expected_revision=self.ledger.snapshot()['sha256'],
                idempotency_key='reserve-' + name, evidence=[])['reservation_id']
            self.bindings[name] = dict(native=native['render'], reservation_id=reservation,
                operation_id='operation-' + name,
                resources=dict(cpu_workers=1, gpu_workers=0, provider_workers=0,
                               memory_bytes=1024**3, storage_bytes=1024**3),
                retry=dict(max_attempts=2, base_seconds=1, maximum_seconds=30))
        pin = deepcopy(self.graph['nodes'][0]['inputs'][0])
        self.graph['edges'] = [dict(source='a', target='b', artifact=pin),
                               dict(source='a', target='c', artifact=deepcopy(pin))]
        self.driver = RenderJobDriver(self.store, RejectingWorker(), self.fixture.verify)
        self.service = self.reopen()

    def reopen(self):
        return ExecutionService(self.store, drivers={'RenderAdapter.submit': self.driver},
                                ledger=self.ledger, clock=lambda: self.clock_value)

    def submit(self):
        return self.service.submit_graph('graph', self.graph, self.bindings, self.root,
                                         capabilities=self.capabilities)

    def publish_native(self, name):
        while True:
            lease = self.store.claim(self.jobs[name])
            if lease is None:
                break
            output, result = self.fixture.output(lease)
            self.store.publish(lease, output, result, self.fixture.verify)

    def test_three_node_branch_persists_and_reopens_original_native_ids(self):
        original = self.submit()
        reopened = self.reopen().status_graph('graph')
        self.assertEqual(reopened, original)
        self.assertEqual(set(reopened['nodes']), {'a', 'b', 'c'})
        self.assertEqual(reopened['state'], 'queued')
        self.assertEqual(self.submit(), reopened)
        self.assertEqual(len(self.store.list_jobs()), 3)

    def test_cycles_and_stale_native_binding_do_not_publish_graph(self):
        self.graph['edges'].append(dict(source='c', target='a', artifact=deepcopy(self.graph['edges'][0]['artifact'])))
        with self.assertRaises(RigError):
            self.submit()
        self.graph['edges'].pop()
        self.bindings['a']['native']['request_sha256'] = '0' * 64
        with self.assertRaises(RigError):
            self.submit()
        with self.store._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM director_runs').fetchone()[0], 0)

    def test_missing_actual_backend_capability_and_understated_memory_reject(self):
        self.graph['nodes'][0]['gates'] = []
        with self.assertRaises(RigError):
            self.submit()
        self.graph['nodes'][0]['gates'] = ['blender.cpu']
        self.bindings['a']['resources']['memory_bytes'] = 1
        with self.assertRaises(RigError):
            self.submit()

    def test_invalid_boolean_resource_rejects_before_any_native_claim(self):
        self.bindings['a']['resources']['cpu_workers'] = True
        with self.assertRaises(RigError):
            self.submit()
        self.assertTrue(all(frame['attempts'] == 0 for job in self.jobs.values()
                            for frame in self.store.status(job)['frames']))

    def test_cancel_requires_exact_revision_and_resume_obeys_retry_after(self):
        submitted = self.submit()
        with self.assertRaisesRegex(RigError, 'stale'):
            self.service.cancel_graph('graph', expected_revision='0' * 64)
        self.assertFalse(any(self.store.status(job)['cancel'] for job in self.jobs.values()))
        cancelled = self.service.cancel_graph('graph', expected_revision=submitted['revision'])
        self.assertEqual(cancelled['state'], 'cancelled')
        resumed = self.service.resume_graph('graph', expected_revision=cancelled['revision'], retry_after='15')
        self.assertEqual(resumed['state'], 'queued')
        self.assertTrue(all(node['not_before'] >= self.clock_value + 15 for node in resumed['nodes'].values()))
        before = [self.store.status(job) for job in self.jobs.values()]
        self.service.dispatch_ready('graph')
        self.assertEqual(before, [self.store.status(job) for job in self.jobs.values()])

    def test_expired_capability_never_claims_native_work(self):
        self.submit()
        self.clock_value += 3601
        self.service.dispatch_ready('graph')
        self.assertTrue(all(frame['attempts'] == 0 for job in self.jobs.values()
                            for frame in self.store.status(job)['frames']))
        self.assertFalse(any(row.get('dispatches') for row in self.ledger.snapshot()['state']['reservations'].values()))

    def test_native_live_claim_is_observed_and_not_stolen(self):
        self.submit()
        lease = self.store.claim(self.jobs['a'])
        with self.store._transaction() as db:
            db.execute('UPDATE frames SET heartbeat=0 WHERE job=? AND frame=?', (lease['job'], lease['frame']))
        self.assertEqual(self.store.recover(self.jobs['a']), [])
        observed = self.service.reconcile_graph('graph')
        self.assertEqual(observed['state'], 'running')
        self.assertEqual(observed['nodes']['a']['native']['receipt']['job'], lease['job'])
        self.assertFalse(self.driver.heartbeat(self.bindings['a']['native'], lease))
        self.store.fail(lease, 'known test cleanup')

    def test_verified_native_cache_outputs_drive_completion_and_tamper_rejects(self):
        self.submit()
        for name in ('a', 'b', 'c'):
            self.publish_native(name)
        complete = self.service.reconcile_graph('graph')
        self.assertEqual(complete['state'], 'complete')
        self.assertTrue(all(node['native']['artifacts'] for node in complete['nodes'].values()))
        artifact = complete['nodes']['a']['native']['artifacts'][0]
        (self.store.root / artifact['path']).write_bytes(b'corrupted native cache')
        with self.assertRaises(ValueError):
            self.service.reconcile_graph('graph')

    def test_failed_real_native_worker_boundary_never_completes_a_dependent(self):
        self.submit()
        with self.assertRaises((RigError, RuntimeError)):
            self.service.dispatch_ready('graph')
        observed = self.service.status_graph('graph')
        self.assertNotEqual(observed['state'], 'complete')
        self.assertTrue(all(frame['attempts'] == 0 for name in ('b', 'c')
                            for frame in self.store.status(self.jobs[name])['frames']))
        self.assertFalse(observed['nodes']['b']['native']['artifacts'])


class RetryPolicyTests(unittest.TestCase):
    def test_backoff_and_retry_after_numeric_and_http_date_are_bounded(self):
        self.assertEqual(retry_delay(4, base_seconds=2, maximum_seconds=10), 10)
        self.assertEqual(retry_delay(1, retry_after='20'), 20)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        date = format_datetime(now + timedelta(seconds=30), usegmt=True)
        self.assertEqual(retry_delay(1, retry_after=date, now=now.timestamp()), 30)
        for value in ('90000', 'not a date', '-1'):
            with self.subTest(value=value), self.assertRaises(RigError):
                retry_delay(1, retry_after=value, now=now.timestamp())


if __name__ == '__main__':
    unittest.main()
