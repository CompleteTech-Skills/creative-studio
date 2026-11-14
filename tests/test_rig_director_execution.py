"""Persistent DAG acceptance over real SQLite jobs and synthetic PNG caches.

Capability receipts and PNG masters are explicitly synthetic test inputs. They
exercise admission/persistence/failure behavior, not Blender production output.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import inspect
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
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

    @staticmethod
    def resources(**values):
        result = dict(cpu_workers=0, gpu_workers=0, provider_workers=0, memory_bytes=0, storage_bytes=0)
        result.update(values)
        return result

    def process_environment(self):
        root = Path(__file__).resolve().parents[1]
        return dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / 'scripts'), str(root / 'tests'))))

    def resource_child(self, operation, resources, *, hold=False):
        code = '''import json,sys
from render3d_jobs import JobStore
store=JobStore(sys.argv[1])
lease=store.reserve_resources(sys.argv[2],json.loads(sys.argv[3]))
assert lease is not None
print(json.dumps(lease),flush=True)
if sys.argv[4]=='hold':
 sys.stdin.read(1)
 store.release_resources(lease)
'''
        command = [sys.executable, '-c', code, str(self.store.root), operation, json.dumps(resources),
                   'hold' if hold else 'exit']
        if not hold:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                    env=self.process_environment())
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=self.process_environment())
        def stop():
            if child.poll() is None:
                try:
                    child.communicate('\n', timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill(); child.communicate(timeout=5)
        self.addCleanup(stop)
        output = queue.Queue()
        threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True).start()
        line = output.get(timeout=20)
        self.assertTrue(line, 'The owned resource child did not publish its lease.')
        return child, json.loads(line)

    def test_three_node_branch_persists_and_reopens_original_native_ids(self):
        original = self.submit()
        reopened = self.reopen().status_graph('graph')
        self.assertEqual(reopened, original)
        self.assertEqual(set(reopened['nodes']), {'a', 'b', 'c'})
        self.assertEqual(reopened['state'], 'queued')
        self.assertEqual(self.submit(), reopened)
        self.assertEqual(len(self.store.list_jobs()), 3)

    def test_context_binding_is_immutable_across_reopen_and_cross_project_reuse(self):
        context = dict(project_id='project', project_revision_sha256='1' * 64,
                       cancellation_token='cancel-a', request_id='request-a', idempotency_key='submit-a')
        original = self.service.submit_graph('graph', self.graph, self.bindings, self.root,
            capabilities=self.capabilities, context_binding=context)
        self.assertEqual(self.reopen().get_context_binding('graph'), context)
        for field in context:
            altered = dict(context)
            altered[field] = '2' * 64 if field.endswith('sha256') else 'another-context'
            with self.subTest(field=field), self.assertRaises(RigError):
                self.service.submit_graph('graph', self.graph, self.bindings, self.root,
                    capabilities=self.capabilities, context_binding=altered)
        self.assertEqual(self.service.status_graph('graph'), original)

    def test_idempotent_reopen_after_expiry_and_settlement_does_not_readmit_execution(self):
        original = self.submit()
        proof_path = self.root / 'known-unstarted-budget.json'
        proof_path.write_text('{"fixture":true,"native_frame_attempts":0}')
        proof = [self.ledger.retain_evidence(proof_path)]
        for name, binding in self.bindings.items():
            self.ledger.settle(binding['reservation_id'], usage={metric: 0 for metric in METRICS},
                outcome='complete', expected_revision=self.ledger.snapshot()['sha256'],
                idempotency_key='settle-unstarted-' + name, evidence=proof)
        self.assertEqual(self.submit(), original)
        with self.assertRaises(RigError):
            self.service.dispatch_ready('graph')
        reconciled = self.service.status_graph('graph')
        self.assertEqual(reconciled['id'], original['id'])
        self.assertEqual(reconciled['graph_sha256'], original['graph_sha256'])
        self.clock_value += 3601
        self.assertEqual(self.submit(), reconciled)
        self.service.dispatch_ready('graph')
        self.assertTrue(all(frame['attempts'] == 0 for job in self.jobs.values()
                            for frame in self.store.status(job)['frames']))

    def test_atomic_admission_denial_and_exception_roll_back_both_native_and_dag_writes(self):
        self.submit()
        def deny(db, record):
            db.execute('UPDATE director_nodes SET phase="complete" WHERE run="graph" AND node="a"')
            return False
        self.assertIsNone(self.store.claim(self.jobs['a'], admit=deny))
        self.assertIsNone(self.store.reserve_resources('denied-external', self.resources(cpu_workers=1), admit=deny))
        def invalid(db, record):
            db.execute('UPDATE director_nodes SET phase="complete" WHERE run="graph" AND node="a"')
            return 1
        def interrupted(db, record):
            db.execute('UPDATE director_nodes SET phase="complete" WHERE run="graph" AND node="a"')
            raise RuntimeError('injected exception inside native admission')
        for callback, error in ((invalid, ValueError), (interrupted, RuntimeError)):
            with self.subTest(callback=callback.__name__), self.assertRaises(error):
                self.store.claim(self.jobs['a'], admit=callback)
        with self.store._transaction() as db:
            self.assertEqual(db.execute('SELECT phase FROM director_nodes WHERE run="graph" AND node="a"').fetchone()[0], 'queued')
        self.assertEqual(self.store.resources_status()['reservations'], [])
        self.assertTrue(all(frame['attempts'] == 0 for frame in self.store.status(self.jobs['a'])['frames']))

    def test_external_cpu_capacity_and_plain_native_render_claim_share_one_limit(self):
        lease = self.store.reserve_resources('external-cpu', self.resources(cpu_workers=1))
        self.assertIsNotNone(lease)
        self.assertIsNone(self.store.claim(self.jobs['a']))
        self.store.release_resources(lease)
        native = self.store.claim(self.jobs['a'])
        self.assertIsNotNone(native)
        self.assertIsNone(self.store.reserve_resources('second-cpu', self.resources(cpu_workers=1)))
        self.store.fail(native, 'known fixture worker stopped')

    def test_external_gpu_and_provider_capacity_are_separate_from_cpu_admission(self):
        from render3d_jobs import JobStore
        store = JobStore(self.root / 'gpu-jobs', workers=1, gpu_workers=1, provider_workers=1)
        request = deepcopy(self.fixture.request)
        request['id'] = 'gpu-accounting-fixture'
        request['settings']['device_policy'] = 'gpu-required'
        backend = dict(self.fixture.backend, device='gpu-accounting-fixture')
        gpu_job = store.submit(request, self.root / 'snapshot', backend)
        gpu = store.reserve_resources('external-gpu', self.resources(gpu_workers=1))
        provider = store.reserve_resources('external-provider', self.resources(provider_workers=1))
        self.assertIsNotNone(gpu); self.assertIsNotNone(provider)
        self.assertIsNone(store.claim(gpu_job))
        self.assertIsNone(store.reserve_resources('another-provider', self.resources(provider_workers=1)))
        cpu = store.reserve_resources('independent-cpu', self.resources(cpu_workers=1))
        self.assertIsNotNone(cpu)
        store.release_resources(cpu); store.release_resources(gpu)
        native = store.claim(gpu_job)
        self.assertIsNotNone(native)
        store.fail(native, 'known accounting fixture cleanup')
        store.release_resources(provider)

    def test_live_child_resource_owner_is_not_stolen_despite_zero_heartbeat(self):
        child, lease = self.resource_child('live-cpu-owner', self.resources(cpu_workers=1), hold=True)
        with self.store._transaction() as db:
            db.execute('UPDATE resource_reservations SET heartbeat=0 WHERE operation_id=?', (lease['operation_id'],))
        self.assertIsNone(child.poll())
        self.assertEqual(self.store.recover_resources(admit=lambda db, record: True), [])
        self.assertIsNone(self.store.claim(self.jobs['a']))
        child.communicate('\n', timeout=10)
        self.assertEqual(child.returncode, 0)
        native = self.store.claim(self.jobs['a'])
        self.assertIsNotNone(native)
        self.store.fail(native, 'known fixture cleanup')

    def test_dead_child_cpu_capacity_recovers_without_reusing_its_ownership_token(self):
        original = self.resource_child('dead-cpu-owner', self.resources(cpu_workers=1))
        self.assertEqual(self.store.resources_status()['used']['cpu_workers'], 1)
        self.assertEqual(self.store.recover_resources(), ['dead-cpu-owner'])
        recovered = self.store.reserve_resources('dead-cpu-owner', self.resources(cpu_workers=1))
        self.assertIsNotNone(recovered)
        self.assertNotEqual(recovered['token'], original['token'])
        self.store.release_resources(recovered)

    def test_dead_provider_capacity_requires_explicit_known_terminal_sql_approval(self):
        lease = self.resource_child('external-provider', self.resources(provider_workers=1))
        self.assertEqual(self.store.recover_resources(), [])
        # Explicit synthetic native-outcome metadata tests the recovery hook;
        # this is capacity accounting, not a provider execution receipt.
        with self.store._transaction() as db:
            db.execute('CREATE TABLE test_external_outcomes(operation_id TEXT PRIMARY KEY,state TEXT NOT NULL)')
            db.execute('INSERT INTO test_external_outcomes VALUES(?,?)', (lease['operation_id'], 'uncertain'))
        def known_terminal(db, record):
            state = db.execute('SELECT state FROM test_external_outcomes WHERE operation_id=?',
                               (record['operation_id'],)).fetchone()
            return state is not None and state['state'] == 'known-failed'
        self.assertEqual(self.store.recover_resources(admit=known_terminal), [])
        self.assertEqual(self.store.resources_status()['used']['provider_workers'], 1)
        with self.store._transaction() as db:
            db.execute('UPDATE test_external_outcomes SET state="known-failed" WHERE operation_id=?', (lease['operation_id'],))
        self.assertEqual(self.store.recover_resources(admit=known_terminal), ['external-provider'])
        self.assertEqual(self.store.resources_status()['used']['provider_workers'], 0)

    def test_two_director_processes_observe_one_live_native_frame_claim(self):
        self.submit()
        started, release = self.root / 'worker-started', self.root / 'worker-release'
        code = '''import json,sys,time
from pathlib import Path
from director_execution import ExecutionService
from director_execution_drivers import RenderJobDriver
from director_budget import BudgetLedger
from render3d_jobs import JobStore
from render3d_verify import RenderArtifactVerifier
store=JobStore(sys.argv[1]);ledger=BudgetLedger(sys.argv[2])
started,release=Path(sys.argv[3]),Path(sys.argv[4])
job=store.status(store.list_jobs()[0]['id'])
class ControlledFailure:
 def render_frames(self,*args,**kwargs):
  started.write_text(str(__import__('os').getpid()))
  deadline=time.monotonic()+15
  while not release.exists():
   if time.monotonic()>=deadline:raise RuntimeError('owned controller barrier timed out')
   time.sleep(.01)
  raise RuntimeError('injected native worker failure without output')
driver=RenderJobDriver(store,ControlledFailure(),RenderArtifactVerifier(job['snapshot'],job['backend']).verify)
service=ExecutionService(store,drivers={'RenderAdapter.submit':driver},ledger=ledger,clock=lambda:float(sys.argv[5]))
try:result=service.dispatch_ready('graph')
except RuntimeError:result=service.status_graph('graph')
print(json.dumps({'state':result['state']}),flush=True)
'''
        command = [sys.executable, '-c', code, str(self.store.root), str(self.ledger.root),
                   str(started), str(release), str(self.clock_value)]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=self.process_environment())
        def stop():
            release.write_text('release owned fixture worker')
            if child.poll() is None:
                try:
                    child.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill(); child.communicate(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        while not started.exists():
            if child.poll() is not None:
                stdout, stderr = child.communicate()
                self.fail('First actual controller exited before its worker boundary: ' + stdout + stderr)
            if time.monotonic() >= deadline:
                self.fail('First actual controller did not reach the owned worker barrier.')
            time.sleep(.01)
        peer = subprocess.run(command, capture_output=True, text=True, timeout=10,
                              env=self.process_environment())
        self.assertEqual(peer.returncode, 0, peer.stdout + peer.stderr)
        self.assertEqual(json.loads(peer.stdout)['state'], 'running')
        active = self.store.status(self.jobs['a'])
        self.assertEqual([frame['attempts'] for frame in active['frames']], [1, 0])
        self.assertEqual(active['frames'][0]['pid'], child.pid)
        self.assertEqual(sum(event['event'] == 'claimed' for event in active['events']), 1)
        self.assertTrue(all(frame['attempts'] == 0 for name in ('b', 'c')
                            for frame in self.store.status(self.jobs[name])['frames']))
        release.write_text('known injected worker stop')
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stdout + stderr)
        self.assertNotEqual(json.loads(stdout)['state'], 'complete')

    def test_v2_planned_metadata_reopens_without_becoming_native_execution_proof(self):
        from test_director_graph import DirectorGraphTests
        from types import SimpleNamespace
        self.graph['schema_version'] = 2
        for node in self.graph['nodes']:
            node['execution_metadata'] = DirectorGraphTests.planned_metadata(
                SimpleNamespace(input=deepcopy(node['inputs'][0])), node['id'])
        declared = deepcopy(self.graph)
        self.submit()
        lease = self.store.claim(self.jobs['a'])
        observed = self.service.reconcile_graph('graph')
        self.assertEqual(observed['nodes']['a']['native']['state'], 'running')
        reopened = self.reopen().status_graph('graph')
        self.assertEqual(reopened['nodes']['a']['native']['receipt']['job'], lease['job'])
        with self.store._transaction() as db:
            import json
            retained = json.loads(db.execute('SELECT graph FROM director_runs WHERE id=?', ('graph',)).fetchone()[0])
        self.assertEqual(retained, declared)
        self.assertEqual(retained['nodes'][0]['execution_metadata']['execution_status'], 'planned')
        self.assertIsNone(retained['nodes'][0]['execution_metadata']['native_job_ref'])
        self.store.fail(lease, 'known cleanup after observing actual native lease')
        for updates in (dict(execution_status='running', native_job_ref=declared['nodes'][0]['inputs'][0]),
                        dict(output_artifacts=[declared['nodes'][0]['inputs'][0]])):
            self.graph = deepcopy(declared)
            self.graph['nodes'][0]['execution_metadata'].update(updates)
            with self.subTest(updates=updates), self.assertRaises(RigError):
                self.submit()

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
