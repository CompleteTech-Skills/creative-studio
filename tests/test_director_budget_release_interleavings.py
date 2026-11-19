"""Synthetic cancellation interleavings on actual local native journals and ledgers."""
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

import hatch
from director_budget import BudgetLedger
from rig_errors import RigError
import test_director_budget_native as fixture


class InterruptedLedgerWrite(Exception):
    pass


class NativeReleaseInterleavingTests(unittest.TestCase):
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

    def record(self, token, observation):
        with hatch.ownership(self.root):
            return hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True),
                                             'base', token, observation)

    def direct_cancel(self):
        return self.ledger.release(self.row()['id'], outcome='canceled-known', evidence=self.proof,
                                   idempotency_key='direct-cancel', expected_revision=self.ledger.snapshot()['sha256'])

    def native_cancel(self):
        with hatch.ownership(self.root):
            return hatch.release_budget(self.root, hatch.manifest(self.root, recovery=True), 'base',
                                        idempotency_key='native-cancel', outcome='canceled-known', evidence=self.proof)

    def running_observation_survives_cancel(self, outcome):
        token = self.claim()['token']
        prior = self.record(token, self.observe('first-retry-reading', 'retry', calls=0, amount='0.10'))
        self.assertEqual(self.job()['token'], token)
        self.assertEqual(self.row()['state'], 'reserved')
        unknown = outcome == 'uncertain'
        observation = self.observe('next-measured-reading', outcome, calls=1, amount=None if unknown else '0.75')
        original_settle = BudgetLedger.settle

        def stop_before_new_measurement(ledger, reservation_id, **options):
            if options['idempotency_key'] != prior['idempotency_key']:
                raise InterruptedLedgerWrite
            return original_settle(ledger, reservation_id, **options)

        with patch.object(BudgetLedger, 'settle', stop_before_new_measurement):
            with self.assertRaises(InterruptedLedgerWrite):
                self.record(token, observation)
        retained = deepcopy(self.job()['budget_operation']['events'][-1])
        self.assertEqual(retained['payload']['id'], observation['id'])
        self.assertEqual(retained['payload']['token'], token)
        self.assertNotIn('receipt', retained)
        self.assertEqual(self.row()['spent']['provider_calls'], '0')
        try:
            self.direct_cancel()
        except RigError:
            pass
        try:
            self.reconcile()
        except (ValueError, RigError) as exc:
            self.fail(f'Pending measured usage cannot replay after direct cancellation: {exc}')
        acknowledged = self.job()['budget_operation']['events'][-1]
        self.assertEqual(acknowledged['payload'], retained['payload'])
        self.assertEqual(acknowledged['evidence'], retained['evidence'])
        self.assertEqual(acknowledged['receipt']['reservation_id'], self.row()['id'])
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        if unknown:
            self.assertEqual(self.row()['state'], 'uncertain')
            self.assertGreater(Decimal(self.row()['held']['amount']), 0)
            observation = self.observe('later-native-resolution', 'failed', calls=0,
                                       amount='0.75', resolves=['next-measured-reading'])
        self.report_failure(token, observation)
        self.assertEqual(self.row()['state'], 'settled')
        self.assertEqual(Decimal(self.row()['spent']['amount']), Decimal('0.85'))
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertNotIn('token', self.job())
        final = self.ledger.snapshot()
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), final)

    def test_pending_unknown_after_still_running_retry_survives_direct_cancel(self):
        self.running_observation_survives_cancel('uncertain')

    def test_pending_known_after_still_running_retry_survives_direct_cancel(self):
        self.running_observation_survives_cancel('failed')

    def finalize_retry(self):
        token = self.claim()['token']
        self.report_failure(token, self.observe('fully-finalized-retry', 'retry', calls=1, amount='0.25'))
        self.assertEqual(self.job()['status'], 'failed')
        self.assertNotIn('token', self.job())
        self.assertEqual(self.row()['state'], 'reserved')

    def assert_canceled_once(self):
        row = self.row()
        self.assertEqual(row['state'], 'released')
        self.assertEqual(row['spent']['provider_calls'], '1')
        self.assertEqual(Decimal(row['spent']['amount']), Decimal('0.25'))
        self.assertTrue(all(Decimal(value) == 0 for value in row['held'].values()))
        final = self.ledger.snapshot()
        releases = [event for event in final['state']['events'] if event['action'] == 'release']
        self.assertEqual(len(releases), 1)
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), final)

    def test_finalized_native_retry_allows_native_cancel(self):
        self.finalize_retry()
        self.assertEqual(self.native_cancel()['state'], 'released')
        self.assert_canceled_once()

    def test_finalized_native_retry_allows_direct_cancel(self):
        self.finalize_retry()
        self.assertEqual(self.direct_cancel()['state'], 'released')
        self.assert_canceled_once()

    def test_matching_direct_cancel_acknowledges_durable_native_cancel(self):
        self.finalize_retry()
        with patch.object(BudgetLedger, 'release', side_effect=InterruptedLedgerWrite):
            with self.assertRaises(InterruptedLedgerWrite):
                self.native_cancel()
        pending = deepcopy(self.job()['budget_operation']['events'][-1])
        self.assertIsNone(pending['payload']['token'])
        self.assertEqual(pending['payload']['id'], 'native-cancel')
        self.assertEqual(pending['payload']['outcome'], 'canceled-known')
        self.assertNotIn('receipt', pending)
        receipt = self.direct_cancel()
        before = self.ledger.snapshot()
        self.reconcile()
        acknowledged = self.job()['budget_operation']['events'][-1]
        self.assertEqual(acknowledged, {**pending, 'receipt': receipt})
        self.assertEqual(self.ledger.snapshot(), before)
        self.assert_canceled_once()
