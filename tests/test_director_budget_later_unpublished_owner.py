"""Later unpublished native attempts retain recovery and exact measured deltas.

Real disposable journals and accounting files; only interruption timing is mocked.
No provider is called. The independent six-case predecessor observation is retained
separately, and these assertions exercise its public recovery requirement.
"""
from decimal import Decimal
import unittest
from unittest.mock import patch

import hatch
from director_budget import NativeBudget
from rig_errors import RigError
import test_director_budget_native as fixture


class LaterUnpublishedOwnerTests(unittest.TestCase):
    def pending_retry_owner(self, outcome):
        owner = fixture.NativeBudgetTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        first = owner.claim()['token']
        owner.fail(first, owner.observe('first-finalized-retry', 'retry', calls=1, amount='.2'))
        with patch.object(NativeBudget, 'publish_claim', side_effect=OSError('synthetic second publication stop')):
            with self.assertRaises(OSError):
                owner.claim()
        job = hatch.job_for(hatch.manifest(owner.root, recovery=True), 'base')
        token = job['token']
        self.assertNotEqual(token, first)
        observation = owner.observe('second-durable-observation', outcome, calls=1,
                                    amount='.3' if outcome == 'retry' else None)
        original_apply = NativeBudget._apply
        def stop_second_observation(budget, current_job, event):
            if event['payload'].get('id') == observation['id']:
                raise OSError('synthetic second observation stop')
            return original_apply(budget, current_job, event)
        with hatch.ownership(owner.root), patch.object(NativeBudget, '_apply',
                new=stop_second_observation):
            with self.assertRaises(OSError):
                hatch.record_budget_usage(owner.root, hatch.manifest(owner.root, recovery=True),
                                          'base', token, observation)
        saved = hatch.job_for(hatch.manifest(owner.root, recovery=True), 'base')
        retained = [event for event in saved['budget_operation']['events']
                    if event['payload'].get('id') == observation['id']]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]['payload']['token'], token)
        self.assertNotIn('receipt', retained[0])
        state = owner.ledger.snapshot()['state']
        publications = [e for e in state['events'] if e['action'] == 'native-claim']
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]['payload']['token'], first)
        self.assertEqual(owner.row()['spent']['provider_calls'], '1')
        return owner

    def replay(self, owner):
        with hatch.ownership(owner.root):
            hatch.reconcile(owner.root, hatch.manifest(owner.root, recovery=True))

    def test_generic_terminal_measurements_keep_later_native_recovery(self):
        for terminal in ('complete', 'failed'):
            for outcome in ('retry', 'uncertain'):
                with self.subTest(terminal=terminal, native=outcome):
                    owner = self.pending_retry_owner(outcome)
                    before = (owner.root / 'imagegen-jobs.json').read_bytes()
                    result = owner.ledger.settle(owner.row()['id'], outcome=terminal,
                        usage=fixture.quantities(amount='.1'), evidence=owner.proof,
                        expected_revision=owner.ledger.snapshot()['sha256'], idempotency_key='direct-meter')
                    self.assertEqual(result['state'], 'reserved')
                    self.assertGreater(Decimal(result['held']['amount']), 0)
                    self.assertEqual((owner.root / 'imagegen-jobs.json').read_bytes(), before)
                    self.replay(owner)
                    row = owner.row()
                    self.assertEqual(Decimal(row['spent']['provider_calls']), 2)
                    self.assertEqual(Decimal(row['spent']['amount']),
                                     Decimal('.6') if outcome == 'retry' else Decimal('.3'))
                    self.assertEqual(row['state'], 'reserved' if outcome == 'retry' else 'uncertain')
                    self.assertGreater(Decimal(row['held']['amount']), 0)
                    after = owner.ledger.snapshot()
                    self.replay(owner)
                    self.assertEqual(owner.ledger.snapshot(), after)

    def test_generic_cancel_refuses_later_unpublished_owner_without_mutation(self):
        for outcome in ('retry', 'uncertain'):
            with self.subTest(native=outcome):
                owner = self.pending_retry_owner(outcome)
                before = owner.ledger.snapshot()
                journal = (owner.root / 'imagegen-jobs.json').read_bytes()
                with self.assertRaises(RigError):
                    owner.ledger.release(owner.row()['id'], outcome='canceled-known', evidence=owner.proof,
                        expected_revision=before['sha256'], idempotency_key='direct-cancel')
                self.assertEqual(owner.ledger.snapshot(), before)
                self.assertEqual((owner.root / 'imagegen-jobs.json').read_bytes(), journal)
                self.replay(owner)
                self.assertEqual(owner.row()['spent']['provider_calls'], '2')
                self.assertGreater(Decimal(owner.row()['held']['amount']), 0)


if __name__ == '__main__':
    unittest.main()
