"""Original native observations survive the first publication interruption.

All journal/ledger operations use real disposable fixtures. Only interruption
timing is mocked; no provider or real accounting service is called.
"""
from decimal import Decimal
import unittest
from unittest.mock import patch

import hatch
from director_budget import NativeBudget
from rig_errors import RigError
import test_director_budget_native as fixture


class UnpublishedOwnerTests(unittest.TestCase):
    def interrupted_owner(self, outcome):
        owner = fixture.NativeBudgetTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        with patch.object(NativeBudget, 'publish_claim',
                          side_effect=OSError('synthetic publication interruption')):
            with self.assertRaises(OSError):
                owner.claim()
        job = hatch.job_for(hatch.manifest(owner.root, recovery=True), 'base')
        token = job['token']
        observation = owner.observe('pending-original-owner', outcome, calls=1,
                                    amount='.2' if outcome == 'retry' else None)
        with hatch.ownership(owner.root), patch.object(NativeBudget, '_apply',
                side_effect=OSError('synthetic accounting interruption')):
            with self.assertRaises(OSError):
                hatch.record_budget_usage(owner.root, hatch.manifest(owner.root, recovery=True),
                                          'base', token, observation)
        self.assertEqual(owner.row()['attempts'], [])
        self.assertFalse(any(event['action'] == 'native-claim'
                             for event in owner.ledger.snapshot()['state']['events']))
        return owner

    def replay(self, owner):
        with hatch.ownership(owner.root):
            hatch.reconcile(owner.root, hatch.manifest(owner.root, recovery=True))

    def test_terminal_generic_measurements_preserve_durable_native_usage(self):
        for terminal in ('complete', 'failed'):
            for pending in ('retry', 'uncertain'):
                with self.subTest(terminal=terminal, pending=pending):
                    owner = self.interrupted_owner(pending)
                    journal = (owner.root / 'imagegen-jobs.json').read_bytes()
                    result = owner.ledger.settle(owner.row()['id'],
                        usage=fixture.quantities(amount='.1'), outcome=terminal,
                        expected_revision=owner.ledger.snapshot()['sha256'],
                        idempotency_key='independent-measurement', evidence=owner.proof)
                    self.assertEqual(result['state'], 'reserved')
                    self.assertEqual(journal, (owner.root / 'imagegen-jobs.json').read_bytes())
                    self.replay(owner)
                    row = owner.row()
                    self.assertEqual(Decimal(row['spent']['provider_calls']), 1)
                    self.assertEqual(Decimal(row['spent']['amount']),
                                     Decimal('.3') if pending == 'retry' else Decimal('.1'))
                    self.assertEqual(row['state'], 'reserved' if pending == 'retry' else 'uncertain')
                    self.assertGreater(Decimal(row['held']['amount']), 0)
                    head = owner.ledger.snapshot()['sha256']
                    self.replay(owner)
                    self.assertEqual(owner.ledger.snapshot()['sha256'], head)

    def test_generic_release_cannot_strand_uncharged_native_unknown(self):
        for outcome in ('unstarted', 'canceled-known'):
            with self.subTest(outcome=outcome):
                owner = self.interrupted_owner('uncertain')
                before = owner.ledger.snapshot()
                journal = (owner.root / 'imagegen-jobs.json').read_bytes()
                with self.assertRaises(RigError):
                    owner.ledger.release(owner.row()['id'], outcome=outcome,
                        expected_revision=before['sha256'],
                        idempotency_key='independent-release', evidence=owner.proof)
                self.assertEqual(owner.ledger.snapshot(), before)
                self.assertEqual((owner.root / 'imagegen-jobs.json').read_bytes(), journal)
                self.replay(owner)
                self.assertEqual(owner.row()['spent']['provider_calls'], '1')
                self.assertEqual(owner.row()['state'], 'uncertain')
                self.assertGreater(Decimal(owner.row()['held']['amount']), 0)

    def test_non_native_reservation_keeps_generic_terminal_and_release_behavior(self):
        owner = fixture.NativeBudgetTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        for outcome in ('complete', 'failed', 'unstarted', 'canceled-known'):
            with self.subTest(outcome=outcome):
                reserved = owner.ledger.reserve(operation_id='standalone-' + outcome,
                    provider='imagegen', estimate=fixture.quantities(provider_calls=1, amount='.1'),
                    retry_allowance=0, evidence=owner.proof,
                    expected_revision=owner.ledger.snapshot()['sha256'],
                    idempotency_key='reserve-' + outcome)
                options = dict(outcome=outcome,
                    expected_revision=owner.ledger.snapshot()['sha256'],
                    idempotency_key='finish-' + outcome, evidence=owner.proof)
                if outcome in ('complete', 'failed'):
                    result = owner.ledger.settle(reserved['reservation_id'],
                                                 usage=fixture.quantities(amount='.01'), **options)
                    self.assertEqual(result['state'], 'settled')
                else:
                    result = owner.ledger.release(reserved['reservation_id'], **options)
                    self.assertEqual(result['state'], 'released')


if __name__ == '__main__':
    unittest.main()
