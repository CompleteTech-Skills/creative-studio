"""Offline origin checks use genuine owner-journaled events and synthetic accounting."""
import unittest
from unittest.mock import patch

import test_director_budget_interop_authority as interop


class SettlementOriginTests(unittest.TestCase):
    setUp = interop.BudgetInteropAuthorityTests.setUp
    prepare = interop.BudgetInteropAuthorityTests.prepare
    bind = interop.BudgetInteropAuthorityTests.bind
    claim = interop.BudgetInteropAuthorityTests.claim
    observe = interop.BudgetInteropAuthorityTests.observe
    report = interop.BudgetInteropAuthorityTests.fail
    row = interop.BudgetInteropAuthorityTests.row
    authority = interop.BudgetInteropAuthorityTests.authority

    def test_pending_owner_event_requires_its_own_enrolled_journal(self):
        handoff = self.claim()
        token = handoff['token']
        observation = self.observe('interrupted-owner-retry', 'retry', amount='0.25')
        with patch.object(interop.BudgetLedger, 'settle', side_effect=OSError('offline interruption')):
            with self.assertRaisesRegex(OSError, 'offline interruption'):
                self.report(token, observation)
        # Use the exact genuine pending event, copied without modifying any field.
        current = interop.hatch.manifest(self.root, recovery=True)
        operation = interop.hatch.job_for(current, 'base')['budget_operation']
        event = operation['events'][-1]
        self.assertNotIn('receipt', event)
        sibling = self.prepare('sibling')
        self.bind(sibling)
        before = self.authority()
        for options in ({}, {'_native_run': sibling}):
            with self.subTest(options=list(options)):
                with self.assertRaises(interop.RigError) as refused:
                    self.ledger.settle(operation['reservation_id'], usage=event['payload']['usage'],
                        outcome=event['payload']['outcome'], idempotency_key=event['key'],
                        evidence=event['evidence'], expected_revision=before[0]['sha256'], **options)
                self.assertEqual(refused.exception.report['code'], 'DIRECTOR_BUDGET_BINDING')
                self.assertEqual(self.authority(), before)

        self.report(token, observation)
        row = self.row()
        self.assertEqual(row['spent']['amount'], '0.25')
        self.assertEqual(row['held']['amount'], '5.75')
        self.assertEqual(len(row['attempts']), 1)
        self.assertIs(row['attempts'][0]['native_owner_verified'], True)
        released = self.ledger.release(operation['reservation_id'], outcome='canceled-known',
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key='known-remainder',
            evidence=self.proof)
        self.assertEqual(released['state'], 'released')


if __name__ == '__main__':
    unittest.main()
