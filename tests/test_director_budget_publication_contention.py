"""Offline publication contention; hooks inject local ledger scheduling only."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_director_budget_native as fixture


class BudgetPublicationContentionTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    row = fixture.NativeBudgetTests.row

    def test_transient_publication_contention_reuses_one_native_intent(self):
        original = fixture.BudgetLedger._change
        seen = []

        def contend(ledger, revision, key, action, payload, mutate):
            if action == 'native-claim':
                seen.append((key, deepcopy(payload)))
                if len(seen) <= 2:
                    code = 'RIG_BUSY' if len(seen) == 1 else 'DIRECTOR_BUDGET_STALE'
                    raise fixture.RigError(code, 'Synthetic local ledger contention', retryable=True)
            return original(ledger, revision, key, action, payload, mutate)

        with patch.object(fixture.BudgetLedger, '_change', contend):
            handoff = self.claim()
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(item == seen[0] for item in seen))
        job = fixture.hatch.job_for(fixture.hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['budget_operation']['attempts'], [handoff['token']])
        state = self.ledger.snapshot()['state']
        self.assertEqual(len(state['reservations']), 1)
        self.assertEqual(len([event for event in state['events'] if event['action'] == 'native-claim']), 1)

    def test_persistent_publication_contention_returns_no_handoff_and_stays_bounded(self):
        original = fixture.BudgetLedger._change
        seen = []

        def busy(ledger, revision, key, action, payload, mutate):
            if action == 'native-claim':
                seen.append(key)
                raise fixture.RigError('RIG_BUSY', 'Synthetic persistent contention', retryable=True)
            return original(ledger, revision, key, action, payload, mutate)

        with patch.object(fixture.BudgetLedger, '_change', busy), self.assertRaises(fixture.RigError):
            self.claim()
        self.assertGreater(len(seen), 0)
        self.assertLessEqual(len(seen), 16)
        self.assertEqual(len(set(seen)), 1)
        job = fixture.hatch.job_for(fixture.hatch.manifest(self.root), 'base')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(len(job['budget_operation']['attempts']), 1)
        state = self.ledger.snapshot()['state']
        self.assertEqual(len(state['reservations']), 1)
        self.assertFalse(any(event['action'] == 'native-claim' for event in state['events']))
        with self.assertRaises(ValueError):
            self.claim()

    def test_publication_rechecks_real_overrun_after_successful_preflight(self):
        other = self.prepare('other-publication-owner')
        self.bind(other)
        earlier = self.claim(other)
        original = fixture.BudgetLedger._change
        observed = []

        def settle_before_publication(ledger, revision, key, action, payload, mutate):
            if action == 'native-claim' and not observed:
                observed.append(ledger.settle(earlier['budget_reservation_id'],
                    usage=fixture.quantities(provider_calls=7, amount=13), outcome='failed',
                    expected_revision=ledger.snapshot()['sha256'],
                    idempotency_key='observed-before-publication', evidence=self.proof))
            return original(ledger, revision, key, action, payload, mutate)

        with patch.object(fixture.BudgetLedger, '_change', settle_before_publication), \
                self.assertRaises(fixture.RigError) as refused:
            self.claim()
        self.assertEqual(refused.exception.report['code'], 'DIRECTOR_BUDGET_EXCEEDED')
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0]['over_budget'])
        job = fixture.hatch.job_for(fixture.hatch.manifest(self.root), 'base')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['attempts'], 1)
        state = self.ledger.snapshot()['state']
        self.assertEqual(state['reservations'][earlier['budget_reservation_id']]['spent']['provider_calls'], '7')
        self.assertFalse(any(event['action'] == 'native-claim' and event['payload']['token'] == job['token']
                             for event in state['events']))


if __name__ == '__main__':
    unittest.main()
