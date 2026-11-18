"""Offline interleavings between durable native intents and generic ledger writers.

The interruption hooks only stop before a ledger mutation. The native journal,
evidence objects, ledger revisions, accounting replay, and byte-limit probes
use real files under the fixture's temporary directory.
"""
from copy import deepcopy
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

import hatch
from director_budget import BudgetLedger
from rig_errors import RigError
import test_director_budget_native as fixture


class InterruptedMutation(Exception):
    pass


class NativeTerminalInterleavingTests(unittest.TestCase):
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
            return hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))

    def record(self, token, observation):
        with hatch.ownership(self.root):
            return hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True),
                                            'base', token, observation)

    def terminal_interleaving(self, native_outcome, generic_outcome, *, prior_retry=False):
        token = self.claim()['token']
        prior_amount = Decimal(0)
        if prior_retry:
            self.record(token, self.observe('prior-native-meter', 'retry', calls=0, amount='0.10'))
            prior_amount = Decimal('0.10')
            self.assertEqual(self.job()['token'], token)
            self.assertEqual(self.row()['state'], 'reserved')

        unknown = native_outcome == 'uncertain'
        measured = self.observe('interrupted-native-meter', native_outcome,
                                calls=1, amount=None if unknown else '0.75')
        existing_keys = {event['idempotency_key'] for event in self.ledger.snapshot()['state']['events']}
        original_settle = BudgetLedger.settle

        def stop_new_settlement(ledger, reservation_id, **options):
            if options['idempotency_key'] not in existing_keys:
                raise InterruptedMutation
            return original_settle(ledger, reservation_id, **options)

        with patch.object(BudgetLedger, 'settle', stop_new_settlement):
            with self.assertRaises(InterruptedMutation):
                self.record(token, measured)
        pending = self.job()['budget_operation']['events'][-1]
        self.assertEqual(pending['payload']['id'], measured['id'])
        self.assertNotIn('receipt', pending)
        self.assertEqual(self.row()['spent']['provider_calls'], '0')

        # These are two independent deltas backed by separate observations.
        # A generic refusal or a safely retained adjustment are both acceptable.
        identifier = self.row()['id']
        try:
            self.ledger.settle(identifier, outcome=generic_outcome,
                              usage=fixture.quantities(amount='0.20'), evidence=self.proof,
                              expected_revision=self.ledger.snapshot()['sha256'],
                              idempotency_key='independent-generic-meter')
        except RigError:
            pass
        generic_amount = Decimal(self.row()['spent']['amount']) - prior_amount
        self.assertIn(generic_amount, (Decimal(0), Decimal('0.20')))
        try:
            self.reconcile()
        except (ValueError, RigError) as exc:
            self.fail(f'Durable measured usage cannot replay after generic {generic_outcome}: {exc}')
        acknowledged = self.job()['budget_operation']['events'][-1]
        self.assertEqual(acknowledged['payload'], pending['payload'])
        self.assertEqual(acknowledged['evidence'], pending['evidence'])
        self.assertEqual(acknowledged['receipt']['reservation_id'], identifier)
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.job()['token'], token)
        if unknown:
            self.assertEqual(self.row()['state'], 'uncertain')
            self.assertGreater(Decimal(self.row()['held']['amount']), 0)
            measured = self.observe('native-resolution', 'failed', calls=0, amount='0.75',
                                    resolves=['interrupted-native-meter'])
        self.report_failure(token, measured)
        self.assertEqual(self.row()['state'], 'settled')
        self.assertEqual(Decimal(self.row()['spent']['amount']), prior_amount + generic_amount + Decimal('0.75'))
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertTrue(all(Decimal(value) == 0 for value in self.row()['held'].values()))
        self.assertNotIn('token', self.job())
        once = self.ledger.snapshot()
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), once)

    def test_pending_unknown_survives_generic_complete(self):
        self.terminal_interleaving('uncertain', 'complete')

    def test_pending_unknown_survives_generic_failed(self):
        self.terminal_interleaving('uncertain', 'failed')

    def test_pending_known_survives_generic_complete(self):
        self.terminal_interleaving('failed', 'complete')

    def test_pending_known_survives_generic_failed(self):
        self.terminal_interleaving('failed', 'failed')

    def test_pending_unknown_after_accounted_retry_survives_generic_complete(self):
        self.terminal_interleaving('uncertain', 'complete', prior_retry=True)

    def test_pending_unknown_after_accounted_retry_survives_generic_failed(self):
        self.terminal_interleaving('uncertain', 'failed', prior_retry=True)

    def test_pending_known_after_accounted_retry_survives_generic_complete(self):
        self.terminal_interleaving('failed', 'complete', prior_retry=True)

    def test_pending_known_after_accounted_retry_survives_generic_failed(self):
        self.terminal_interleaving('failed', 'failed', prior_retry=True)

    def test_unrelated_generic_terminals_and_native_terminal_control(self):
        token = self.claim()['token']
        for outcome in ('complete', 'failed'):
            with self.subTest(outcome=outcome):
                reserve = self.ledger.reserve(operation_id='independent-' + outcome,
                    provider='imagegen', estimate=fixture.quantities(provider_calls=1, amount=1),
                    retry_allowance=0, evidence=self.proof,
                    expected_revision=self.ledger.snapshot()['sha256'], idempotency_key='reserve-' + outcome)
                final = self.ledger.settle(reserve['reservation_id'], outcome=outcome,
                    usage=fixture.quantities(provider_calls=1, amount='0.3'), evidence=self.proof,
                    expected_revision=self.ledger.snapshot()['sha256'], idempotency_key='meter-' + outcome)
                self.assertEqual(final['state'], 'settled')
                self.assertEqual(final['spent']['provider_calls'], '1')
                self.assertEqual(final['spent']['amount'], '0.3')
        self.report_failure(token, self.observe('ordinary-native-terminal', 'failed', amount='0.7'))
        self.assertEqual(self.row()['state'], 'settled')
        self.assertEqual(self.row()['spent']['amount'], '0.7')
        self.assertEqual(self.job()['status'], 'failed')

    def test_exactly_sized_pending_intent_rejects_before_reserve_or_recovers_and_cancels(self):
        cap = 128 * 1024
        journal = self.root / 'imagegen-jobs.json'
        with patch.object(hatch, 'MAX_SOURCE_BYTES', cap):
            start = hatch.manifest(self.root)
            start['history'].append(dict(event='fixture-review-note', note=''))
            hatch.save(self.root, start)
            original = journal.read_bytes()

            # Determine the encoded size of this run's exact reservation
            # intent before padding. The sizing pass never mutates the ledger.
            with patch.object(BudgetLedger, 'reserve', side_effect=InterruptedMutation):
                with self.assertRaises(InterruptedMutation):
                    self.claim()
            intent = hatch.manifest(self.root, recovery=True)
            compact = (json.dumps(intent, ensure_ascii=False, allow_nan=False,
                                  separators=(',', ':')) + '\n').encode('utf-8')
            self.assertFalse(self.ledger.snapshot()['state']['reservations'])
            journal.write_bytes(original)
            start['history'][-1]['note'] = 'a' * (cap - len(compact) - 4)
            hatch.save(self.root, start)
            original_history = deepcopy(start['history'])
            job_histories = {job['id']: deepcopy(job['history']) for job in start['jobs']}

            try:
                self.claim()
            except (ValueError, RigError):
                pass
            self.assertLessEqual(journal.stat().st_size, cap)
            self.assertNotIn('token', self.job())
            snapshot = self.ledger.snapshot()['state']
            self.assertFalse(any(event['action'] == 'native-claim' for event in snapshot['events']))
            if snapshot['reservations']:
                self.assertEqual(len(snapshot['reservations']), 1)
                identifier = next(iter(snapshot['reservations']))
                try:
                    self.reconcile()
                    self.assertEqual(self.job()['budget_operation']['reservation_id'], identifier)
                    with hatch.ownership(self.root):
                        hatch.release_budget(self.root, hatch.manifest(self.root, recovery=True), 'base',
                                             idempotency_key='cancel', outcome='unstarted', evidence=self.proof)
                except (ValueError, RigError) as exc:
                    self.fail(f'Committed pending reservation cannot be acknowledged and canceled: {exc}')
                row = self.ledger.snapshot()['state']['reservations'][identifier]
                self.assertEqual(row['state'], 'released')
                self.assertTrue(all(Decimal(value) == 0 for value in row['held'].values()))
            self.assertLessEqual(journal.stat().st_size, cap)
            reopened = hatch.manifest(self.root, recovery=True)
            self.assertEqual(reopened['history'][:len(original_history)], original_history)
            for job in reopened['jobs']:
                previous = job_histories[job['id']]
                self.assertEqual(job['history'][:len(previous)], previous)
            once = self.ledger.snapshot()
            self.reconcile()
            self.assertEqual(self.ledger.snapshot(), once)
