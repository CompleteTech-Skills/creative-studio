"""Current-owner recovery across durable native claim and usage interruptions."""
from contextlib import contextmanager
import os
import unittest
from unittest.mock import patch

import hatch
from director_budget import BudgetLedger, NativeBudget
from rig_errors import RigError
import test_director_budget_native as native_fixtures


class StaleNativeOwnerTests(unittest.TestCase):
    # Reuse the original disposable native setup and synthetic measurement source
    # without inheriting (and rediscovering) that module's complete test suite.
    setUp = native_fixtures.NativeBudgetTests.setUp
    prepare = native_fixtures.NativeBudgetTests.prepare
    bind = native_fixtures.NativeBudgetTests.bind
    claim = native_fixtures.NativeBudgetTests.claim
    observe = native_fixtures.NativeBudgetTests.observe
    row = native_fixtures.NativeBudgetTests.row

    @contextmanager
    def owned_inventory(self):
        """Read every file, bracketing Windows' mandatory lock on the sentinel byte."""
        lock = self.root.with_name(f'.{self.root.name}.hatch.lock')
        original = lock.stat()
        sentinel = lock.read_bytes()
        self.assertEqual(sentinel, b'0')

        def check_lock():
            current = lock.stat()
            self.assertTrue(os.path.samestat(original, current))
            self.assertEqual(current.st_size, len(sentinel))

        def inventory():
            check_lock()
            return {path.relative_to(self.owner).as_posix():
                    sentinel if os.name == 'nt' and path == lock else path.read_bytes()
                    for path in self.owner.rglob('*') if path.is_file()}

        try:
            with hatch.ownership(self.root):
                yield inventory
                check_lock()
        finally:
            check_lock()
            self.assertEqual(lock.read_bytes(), sentinel)

    def interrupted_publication(self):
        with patch.object(NativeBudget, 'publish_claim',
                          side_effect=RuntimeError('scheduled publication interruption')):
            with self.assertRaisesRegex(RuntimeError, 'scheduled publication interruption'):
                self.claim()
        job = hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['budget_operation']['attempts'], [job['token']])
        return job['token']

    def interrupt_observation(self, token, observation, *, after_settlement=False):
        """Interrupt the real settlement boundary, before or after its commit."""
        original_settle = BudgetLedger.settle

        committed = {entry['idempotency_key'] for entry in self.row()['attempts']}

        def interrupted(ledger, *args, **kwargs):
            if kwargs['idempotency_key'] in committed:
                return original_settle(ledger, *args, **kwargs)
            if after_settlement:
                original_settle(ledger, *args, **kwargs)
            raise RuntimeError('scheduled settlement interruption')

        with hatch.ownership(self.root), patch.object(BudgetLedger, 'settle', interrupted):
            with self.assertRaisesRegex(RuntimeError, 'settlement interruption'):
                hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True),
                                          'base', token, observation)
        current = hatch.manifest(self.root, recovery=True)
        events = hatch.job_for(current, 'base')['budget_operation']['events']
        self.assertEqual(events[-1]['payload']['id'], observation['id'])
        self.assertNotIn('receipt', events[-1])
        return current

    def pending_usage(self):
        token = self.interrupted_publication()
        unknown = self.observe('durable-unknown', 'uncertain', amount=None)
        self.interrupt_observation(token, unknown)
        self.assertEqual(self.row()['attempts'], [])
        self.assertEqual(self.row()['held']['provider_calls'], '3')
        self.assertEqual(self.row()['held']['amount'], '6')
        return token, unknown

    def pending_receipt_and_newer_observation(self):
        token = self.interrupted_publication()
        earlier = self.observe('earlier-measured', 'uncertain', amount='0.25')
        # The earlier observation really commits to the ledger. Its journal
        # acknowledgment does not run because the settlement call is interrupted.
        self.interrupt_observation(token, earlier, after_settlement=True)
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        with hatch.ownership(self.root):
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        unknown = self.observe('newer-unknown', 'uncertain', amount=None)
        current = self.interrupt_observation(token, unknown)
        events = hatch.job_for(current, 'base')['budget_operation']['events']
        self.assertEqual([event['payload']['id'] for event in events],
                         ['earlier-measured', 'newer-unknown'])
        self.assertIn('receipt', events[0])
        self.assertEqual(len(self.row()['attempts']), 1)
        return token, unknown

    def test_fresh_owner_recovery_replays_once_and_preserves_uncertainty(self):
        token, unknown = self.pending_receipt_and_newer_observation()
        with hatch.ownership(self.root):
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
        row = self.row()
        self.assertEqual(row['state'], 'uncertain')
        self.assertEqual(row['spent']['provider_calls'], '2')
        self.assertEqual(row['spent']['amount'], '0.25')
        self.assertEqual(row['held']['provider_calls'], '1')
        self.assertEqual(row['held']['amount'], '5.75')
        self.assertEqual(len(row['attempts']), 2)
        self.assertEqual(len({attempt['idempotency_key'] for attempt in row['attempts']}), 2)
        job = hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')
        self.assertEqual((job['status'], job['token'], job['attempts']), ('running', token, 1))
        self.assertEqual([event['payload']['id'] for event in job['budget_operation']['events']],
                         ['earlier-measured', 'newer-unknown'])
        self.assertTrue(all('receipt' in event for event in job['budget_operation']['events']))
        with self.owned_inventory() as inventory:
            before = inventory()
            for _ in range(2):
                hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
                hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True),
                                          'base', token, unknown)
            self.assertEqual(inventory(), before)
            with self.assertRaises((RigError, ValueError)):
                hatch.release_budget(self.root, hatch.manifest(self.root, recovery=True), 'base',
                                     idempotency_key='active-unknown', outcome='canceled-known',
                                     evidence=self.proof)
            self.assertEqual(inventory(), before)

    def test_fresh_record_preserves_earlier_pending_observation(self):
        token, unknown = self.pending_usage()
        later = self.observe('later-measured', 'uncertain', calls=0, amount='0.3')
        with hatch.ownership(self.root):
            hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True),
                                      'base', token, later)
        row = self.row()
        self.assertEqual(row['state'], 'uncertain')
        self.assertEqual(row['spent']['provider_calls'], '1')
        self.assertEqual(row['spent']['amount'], '0.3')
        self.assertEqual(len(row['attempts']), 2)
        job = hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')
        self.assertEqual([event['payload']['id'] for event in job['budget_operation']['events']],
                         [unknown['id'], later['id']])
        self.assertEqual(job['token'], token)
        with self.owned_inventory() as inventory:
            before = inventory()
            hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True),
                                      'base', token, later)
            self.assertEqual(inventory(), before)

    def test_original_pending_claim_recovery_reuses_token_and_reservation(self):
        original_before_claim = NativeBudget.before_claim
        pending_tokens = []

        def interrupted(budget, job, hashes):
            pending_tokens.append(original_before_claim(budget, job, hashes))
            raise RuntimeError('scheduled interruption before running publication')

        with patch.object(NativeBudget, 'before_claim', interrupted):
            with self.assertRaisesRegex(RuntimeError, 'before running publication'):
                self.claim()
        pending = hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')
        self.assertEqual((pending['status'], pending['attempts']), ('pending', 0))
        self.assertFalse(any(item.get('event') == 'claim' for item in pending['history']))
        reservation = pending['budget_operation']['reservation_id']
        self.assertEqual(pending['budget_operation']['attempts'], pending_tokens)
        result = self.claim()
        self.assertEqual(result['token'], pending_tokens[0])
        self.assertEqual(result['budget_reservation_id'], reservation)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        self.assertEqual(self.row()['spent']['provider_calls'], '0')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 1)


if __name__ == '__main__':
    unittest.main()
