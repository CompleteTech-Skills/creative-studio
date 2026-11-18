"""Native finalization uses real local journals and immutable accounting events."""
from decimal import Decimal
import unittest
from unittest.mock import patch

import hatch
from director_budget import BudgetLedger
from rig_errors import RigError
import test_director_budget_native as fixture


class NativeFinalizationAuthorityTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    observe = fixture.NativeBudgetTests.observe
    row = fixture.NativeBudgetTests.row
    report_failure = fixture.NativeBudgetTests.fail

    def job(self):
        return hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')

    def reconcile(self):
        with hatch.ownership(self.root):
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))

    def direct_cancel(self):
        return self.ledger.release(self.row()['id'], outcome='canceled-known', evidence=self.proof,
                                   idempotency_key='after-owner-finalization',
                                   expected_revision=self.ledger.snapshot()['sha256'])

    def closures(self):
        return [event for event in self.ledger.snapshot()['state']['events']
                if event['action'] == 'native-finalize']

    def test_saved_owner_recovers_interrupted_closure_without_another_usage_delta(self):
        token = self.claim()['token']
        change = BudgetLedger._change

        def interrupt(ledger, revision, key, action, payload, mutate):
            if action == 'native-finalize':
                raise OSError('Synthetic interruption before closure publication')
            return change(ledger, revision, key, action, payload, mutate)

        with patch.object(BudgetLedger, '_change', interrupt), self.assertRaises(OSError):
            self.report_failure(token, self.observe('measured-retry', 'retry', amount='0.25'))
        self.assertEqual(self.job()['status'], 'failed')
        self.assertNotIn('token', self.job())
        self.assertEqual(self.closures(), [])
        self.assertEqual(len(self.row()['attempts']), 1)
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.reconcile()
        self.assertEqual(len(self.closures()), 1)
        self.assertEqual(len(self.row()['attempts']), 1)
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        once = self.ledger.snapshot()
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), once)
        self.assertEqual(self.direct_cancel()['state'], 'released')

    def test_generic_measurement_during_closure_preserves_spend_and_recovers(self):
        token = self.claim()['token']
        change = BudgetLedger._change
        observed = []

        def record_before_closure(ledger, revision, key, action, payload, mutate):
            if action == 'native-finalize' and not observed:
                observed.append(ledger.settle(self.row()['id'], outcome='failed',
                    usage=fixture.quantities(amount='0.20'), evidence=self.proof,
                    expected_revision=ledger.snapshot()['sha256'], idempotency_key='independent-meter'))
            return change(ledger, revision, key, action, payload, mutate)

        with patch.object(BudgetLedger, '_change', record_before_closure):
            self.report_failure(token, self.observe('measured-retry', 'retry', amount='0.25'))
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['state'], 'reserved')
        self.assertEqual(self.job()['status'], 'failed')
        self.assertEqual(len(self.closures()), 1)
        self.assertEqual(len(self.row()['attempts']), 2)
        self.assertEqual(Decimal(self.row()['spent']['amount']), Decimal('0.45'))
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(Decimal(self.direct_cancel()['spent']['amount']), Decimal('0.45'))
        once = self.ledger.snapshot()
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), once)

    def test_retained_closure_rejects_rollback_to_running_owner(self):
        token = self.claim()['token']
        observation = self.observe('measured-retry', 'retry', amount='0.25')
        with hatch.ownership(self.root):
            hatch.record_budget_usage(self.root, hatch.manifest(self.root), 'base', token, observation)
        journal = self.root / 'imagegen-jobs.json'
        running = journal.read_bytes()
        self.report_failure(token, observation)
        finalized = journal.read_bytes()
        before = self.ledger.snapshot()
        journal.write_bytes(running)
        with self.assertRaises(RigError):
            self.report_failure(token, self.observe('later-unknown', 'uncertain', calls=0, amount=None))
        self.assertEqual(journal.read_bytes(), running)
        self.assertEqual(self.ledger.snapshot(), before)
        journal.write_bytes(finalized)
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.direct_cancel()['state'], 'released')

    def test_retained_closure_rejects_changed_failure_history(self):
        token = self.claim()['token']
        self.report_failure(token, self.observe('measured-retry', 'retry', amount='0.25'))
        journal = self.root / 'imagegen-jobs.json'
        original = journal.read_bytes()
        data = hatch.manifest(self.root, recovery=True)
        job = hatch.job_for(data, 'base')
        job['history'][-1]['reason'] += ' changed after closure'
        hatch.save(self.root, data)
        edited = journal.read_bytes()
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.reconcile()
        self.assertEqual(journal.read_bytes(), edited)
        self.assertEqual(self.ledger.snapshot(), before)
        journal.write_bytes(original)
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.direct_cancel()['state'], 'released')
