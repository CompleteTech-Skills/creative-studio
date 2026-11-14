"""Actual ledger ownership/CAS races; no mocked accounting or provider execution."""
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import tempfile
import unittest

from director_budget import BudgetLedger, METRICS
from rig_errors import RigError


def amounts(**values):
    return {name: values.get(name, 0) for name in METRICS}


def race_operation(args):
    root, reservation, revision, proof, operation = args
    ledger = BudgetLedger(root)
    try:
        if operation == 'dispatch':
            return ledger.begin_dispatch(reservation, native_owner='render3d', operation_sha256='1'*64,
                expected_revision=revision, idempotency_key='dispatch-race', evidence=proof)['state']
        return ledger.release(reservation, outcome='unstarted', expected_revision=revision,
                              idempotency_key='release-race', evidence=proof)['state']
    except RigError as error:
        return error.report['code']


class DispatchFenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.ledger = BudgetLedger.create(self.root/'ledger', amounts(provider_calls=10),
                                         provider_limits={'local': amounts(provider_calls=10)})
        path = self.root/'native-evidence.json'
        path.write_text('{"source":"actual-local-ledger-test","native_owner":"render3d"}')
        self.proof = [self.ledger.retain_evidence(path)]
        self.reservation = self.ledger.reserve(operation_id='op', provider='local',
            estimate=amounts(provider_calls=1), retry_allowance=1,
            expected_revision=self.revision(), idempotency_key='reserve', evidence=[] )['reservation_id']

    def revision(self):
        return self.ledger.snapshot()['sha256']

    def begin(self, key='dispatch'):
        return self.ledger.begin_dispatch(self.reservation, native_owner='render3d', operation_sha256='1'*64,
            expected_revision=self.revision(), idempotency_key=key, evidence=self.proof)

    def outcome(self, dispatch, outcome):
        return self.ledger.record_dispatch_outcome(self.reservation, dispatch['dispatch_id'], outcome=outcome,
            expected_revision=self.revision(), idempotency_key=outcome+'-'+dispatch['dispatch_id'], evidence=self.proof)

    def settle(self, dispatch, outcome='retry'):
        return self.ledger.settle(self.reservation, usage=amounts(provider_calls=1), outcome=outcome,
            expected_revision=self.revision(), idempotency_key='settle-'+dispatch['dispatch_id'],
            evidence=self.proof, dispatch_id=dispatch['dispatch_id'])

    def test_dispatch_release_race_has_one_winner_in_actual_processes(self):
        revision = self.revision()
        args = [(str(self.ledger.root), self.reservation, revision, self.proof, op)
                for op in ('dispatch', 'release')]
        with ProcessPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(race_operation, args))
        self.assertEqual(sum(value in ('pending', 'released') for value in outcomes), 1, outcomes)
        row = self.ledger.snapshot()['state']['reservations'][self.reservation]
        self.assertFalse(row['state']=='released' and row.get('dispatches'))

    def test_replayed_dispatch_never_grants_another_handoff(self):
        revision = self.revision()
        first = self.begin()
        replay = self.ledger.begin_dispatch(self.reservation, native_owner='render3d', operation_sha256='1'*64,
            expected_revision=revision, idempotency_key='dispatch', evidence=self.proof)
        self.assertFalse(first['replayed']); self.assertTrue(replay['replayed'])
        self.assertEqual(first['dispatch_id'], replay['dispatch_id'])
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations'][self.reservation]['dispatches']), 1)
        self.assertEqual(self.ledger.snapshot()['state']['schema_version'], 2)
        with self.assertRaises(RigError): self.begin('second-controller')

    def test_uncertain_and_unaccounted_dispatch_cannot_free_budget(self):
        dispatch = self.begin(); self.outcome(dispatch, 'uncertain')
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.ledger.release(self.reservation, outcome='canceled-known', expected_revision=self.revision(),
                                idempotency_key='release', evidence=self.proof)
        with self.assertRaises(RigError): self.settle(dispatch, 'complete')
        self.assertEqual(self.ledger.snapshot(), before)
        self.outcome(dispatch, 'failed')
        with self.assertRaises(RigError): self.begin('unaccounted-retry')
        with self.assertRaises(RigError):
            self.ledger.release(self.reservation, outcome='canceled-known', expected_revision=self.revision(),
                                idempotency_key='unaccounted-release', evidence=self.proof)
        settled = self.settle(dispatch)
        self.assertEqual(settled['spent']['provider_calls'], '1')
        second = self.begin('second-attempt'); self.outcome(second, 'failed'); self.settle(second)
        with self.assertRaises(RigError): self.begin('over-budget-attempt')

    def test_failure_before_fence_publication_does_not_admit_work(self):
        before = self.ledger.snapshot()
        publish = self.ledger._publish
        def fail(state, root=None): raise OSError('injected-before-promotion')
        self.ledger._publish = fail
        try:
            with self.assertRaises(OSError): self.begin()
        finally:
            self.ledger._publish = publish
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertFalse(self.begin()['replayed'])

    def test_known_complete_usage_is_settled_once_and_history_survives_reopen(self):
        dispatch = self.begin(); self.outcome(dispatch, 'claimed'); self.outcome(dispatch, 'complete')
        settled = self.settle(dispatch, 'complete')
        self.assertEqual(settled['spent']['provider_calls'], '1')
        reopened = BudgetLedger(self.ledger.root).snapshot()['state']
        self.assertEqual(reopened['reservations'][self.reservation]['held']['provider_calls'], '0')
        self.assertTrue(reopened['reservations'][self.reservation]['dispatches'][0]['usage_accounted'])
        self.assertEqual([event['action'] for event in reopened['events']],
                         ['reserve', 'dispatch', 'dispatch-outcome', 'dispatch-outcome', 'settle'])
