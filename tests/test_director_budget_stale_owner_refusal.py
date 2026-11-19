"""Disposable native journals refuse earlier saved snapshots before mutation.

Claims and accounting writes are real local execution. Usage is synthetic and
interruptions are injected at publication/settlement boundaries; no provider runs.
"""
import unittest
from unittest.mock import patch

import hatch
from director_budget import NativeBudget
from rig_errors import RigError
import test_director_budget_stale_native_owner as controls


class StaleOwnerRefusalTests(unittest.TestCase):
    setUp = controls.StaleNativeOwnerTests.setUp
    prepare = controls.StaleNativeOwnerTests.prepare
    bind = controls.StaleNativeOwnerTests.bind
    claim = controls.StaleNativeOwnerTests.claim
    observe = controls.StaleNativeOwnerTests.observe
    row = controls.StaleNativeOwnerTests.row
    inventory = controls.StaleNativeOwnerTests.inventory
    interrupt_observation = controls.StaleNativeOwnerTests.interrupt_observation

    def saved_claim_revisions(self):
        revisions = []
        original_save = hatch.save

        def capture(root, data):
            result = original_save(root, data)
            revisions.append(hatch.manifest(self.root, recovery=True))
            return result

        with patch.object(hatch, 'save', side_effect=capture), patch.object(
                NativeBudget, 'publish_claim',
                side_effect=RuntimeError('scheduled publication interruption')):
            with self.assertRaisesRegex(RuntimeError, 'scheduled publication interruption'):
                self.claim()
        pending = []
        for revision in revisions:
            job = hatch.job_for(revision, 'base')
            operation = job.get('budget_operation', {})
            if (job['status'] == 'pending' and operation.get('reservation_id') and
                    len(operation['attempts']) == 1 and
                    not any(item.get('event') == 'claim' for item in job['history'])):
                pending.append(revision)
        self.assertTrue(pending, 'Capture a genuinely saved pending reservation and token.')
        running = hatch.manifest(self.root, recovery=True)
        job = hatch.job_for(running, 'base')
        self.assertEqual((job['status'], job['attempts']), ('running', 1))
        self.assertEqual(job['budget_operation']['attempts'], [job['token']])
        self.assertEqual([item['action'] for item in self.ledger.snapshot()['state']['events']],
                         ['reserve'])
        return pending[-1], running, job['token']

    def pending_usage(self):
        pending, running, token = self.saved_claim_revisions()
        self.interrupt_observation(token, self.observe('durable-unknown', 'uncertain', amount=None))
        self.assertEqual(self.row()['attempts'], [])
        return pending, running, token

    def assert_refusal_and_recovery(self, stale, action, *, calls, amount):
        with hatch.ownership(self.root):
            before = self.inventory()
            error = None
            try:
                budget = NativeBudget.for_run(self.root, stale, recovery=True)
                action(budget, hatch.job_for(stale, 'base'))
            except (RigError, ValueError) as caught:
                error = caught
            after = self.inventory()
            changed = sorted(name for name in set(before) | set(after)
                             if before.get(name) != after.get(name))
            self.assertTrue(error is not None and before == after,
                            f'Stale snapshot must refuse without writing: '
                            f'refused={error is not None}, changed={changed}')
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
            recovered = self.inventory()
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
            self.assertEqual(self.inventory(), recovered)
        row = self.row()
        self.assertEqual(row['state'], 'uncertain')
        self.assertEqual(row['spent']['provider_calls'], str(calls))
        self.assertEqual(row['spent']['amount'], amount)
        self.assertEqual(row['held']['provider_calls'], str(3 - calls))
        self.assertEqual(len(row['attempts']), calls)

    def test_stale_pending_unstarted_release_preserves_current_owner(self):
        pending, _, _ = self.pending_usage()
        self.assert_refusal_and_recovery(pending, lambda budget, job: budget.release(
            job, idempotency_key='stale-unstarted', outcome='unstarted', evidence=self.proof),
            calls=1, amount='0')

    def test_stale_pending_cancellation_preserves_current_owner(self):
        pending, _, _ = self.pending_usage()
        self.assert_refusal_and_recovery(pending, lambda budget, job: budget.release(
            job, idempotency_key='stale-canceled', outcome='canceled-known', evidence=self.proof),
            calls=1, amount='0')

    def test_stale_running_observation_preserves_current_owner(self):
        _, running, token = self.pending_usage()
        self.assert_refusal_and_recovery(running, lambda budget, job: budget.record(
            job, token, self.observe('later-unknown', 'uncertain', calls=0, amount=None)),
            calls=1, amount='0')

    def test_stale_reconciliation_preserves_newer_durable_observation(self):
        _, _, token = self.saved_claim_revisions()
        stale = self.interrupt_observation(
            token, self.observe('earlier-measured', 'uncertain', amount='0.25'),
            after_settlement=True)
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        current = self.interrupt_observation(
            token, self.observe('newer-unknown', 'uncertain', amount=None))
        events = hatch.job_for(current, 'base')['budget_operation']['events']
        self.assertEqual([event['payload']['id'] for event in events],
                         ['earlier-measured', 'newer-unknown'])
        self.assertIn('receipt', events[0])
        self.assertNotIn('receipt', events[1])
        self.assert_refusal_and_recovery(stale, lambda budget, job: budget.reconcile(),
                                        calls=2, amount='0.25')


if __name__ == '__main__':
    unittest.main()
