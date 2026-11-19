"""Disposable native journals refuse earlier saved snapshots before mutation.

Claims and accounting writes are real local execution. Usage is synthetic and
interruptions are injected at publication/settlement boundaries; no provider runs.
"""
from argparse import Namespace
from hashlib import sha256
import unittest
from unittest.mock import patch

import hatch
from director_budget import NativeBudget
from rig_errors import RigError, job_lock
import test_director_budget_stale_native_owner as controls


class StaleOwnerRefusalTests(unittest.TestCase):
    setUp = controls.StaleNativeOwnerTests.setUp
    prepare = controls.StaleNativeOwnerTests.prepare
    bind = controls.StaleNativeOwnerTests.bind
    claim = controls.StaleNativeOwnerTests.claim
    observe = controls.StaleNativeOwnerTests.observe
    row = controls.StaleNativeOwnerTests.row
    owned_inventory = controls.StaleNativeOwnerTests.owned_inventory
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
        with self.owned_inventory() as inventory:
            before = inventory()
            error = None
            try:
                budget = NativeBudget.for_run(self.root, stale, recovery=True)
                action(budget, hatch.job_for(stale, 'base'))
            except (RigError, ValueError) as caught:
                error = caught
            after = inventory()
            changed = sorted(name for name in set(before) | set(after)
                             if before.get(name) != after.get(name))
            self.assertTrue(error is not None and before == after,
                            f'Stale snapshot must refuse without writing: '
                            f'refused={error is not None}, changed={changed}')
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
            recovered = inventory()
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
            self.assertEqual(inventory(), recovered)
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

    def check_limits_with_pending_usage(self, *, stale, write=None, before_claim=None,
                                        snapshot_before_claim=False):
        if before_claim is not None:
            with hatch.ownership(self.root):
                before_claim(hatch.manifest(self.root))
        if snapshot_before_claim:
            with hatch.ownership(self.root):
                earlier = hatch.manifest(self.root)
        token = self.claim()['token']
        if not snapshot_before_claim:
            with hatch.ownership(self.root):
                earlier = hatch.manifest(self.root)
        usage = self.observe('interrupt-meter', 'uncertain', amount=None)
        with hatch.ownership(self.root), job_lock(self.ledger.root):
            with self.assertRaises(RigError) as interrupted:
                hatch.record_budget_usage(self.root, hatch.manifest(self.root), 'base', token, usage)
        self.assertEqual(interrupted.exception.report['code'], 'RIG_BUSY')
        self.assertEqual(self.row()['attempts'], [])

        journal = (self.root / 'imagegen-jobs.json').relative_to(self.owner).as_posix()
        with self.owned_inventory() as inventory:
            def digests():
                return {path: sha256(content).hexdigest() for path, content in inventory().items()}

            current = hatch.manifest(self.root)
            current_job = hatch.job_for(current, 'base')
            pending, = current_job['budget_operation']['events']
            self.assertEqual(pending['payload']['id'], usage['id'])
            self.assertNotIn('receipt', pending)
            original = digests()
            if stale:
                with self.assertRaises((RigError, ValueError)):
                    if write is None:
                        hatch.configure_limits(self.root, earlier, Namespace(max_calls=1))
                    else:
                        write(earlier)
                self.assertEqual(digests(), original)
            elif write is None:
                result = hatch.configure_limits(self.root, current, Namespace(max_calls=1))
                self.assertEqual(result['limits']['generation_calls'], 1)
                self.assertEqual(hatch.manifest(self.root)['limits']['generation_calls'], 1)
            else:
                write(current)
                # A current repair legitimately appends its note to prompts.
                original = digests()

            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
            recovered = digests()
            self.assertEqual({path: recovered.get(path) for path in original if path != journal},
                             {path: digest for path, digest in original.items() if path != journal})
            job = hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')
            if not snapshot_before_claim:
                self.assertEqual(job['history'], hatch.job_for(earlier, 'base')['history'])
            self.assertEqual(job['token'], token)
            event, = job['budget_operation']['events']
            self.assertEqual(event['payload'], pending['payload'])
            self.assertEqual(event['receipt']['state'], 'uncertain')
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))
            self.assertEqual(digests(), recovered)
        row = self.row()
        self.assertEqual((row['state'], row['spent']['provider_calls'], row['held']['provider_calls']),
                         ('uncertain', '1', '2'))
        self.assertEqual(len(row['attempts']), 1)

    def test_stale_limits_update_preserves_pending_native_usage(self):
        self.check_limits_with_pending_usage(stale=True)

    def test_current_limits_update_preserves_pending_native_usage(self):
        self.check_limits_with_pending_usage(stale=False)

    def repair_unrelated_state(self, data):
        other = next(job['id'] for job in data['jobs'] if job['id'] != 'base'
                     and 'base' not in hatch.descendants(data, job['id']))
        result = hatch.repair(self.root, data, other, 'stale snapshot probe')
        self.assertNotIn('base', result['pending'])

    def test_stale_repair_preserves_pending_native_usage(self):
        # Repairing a different state is allowed while base runs, but a stale
        # snapshot must not replace the newer pending usage on disk.
        self.check_limits_with_pending_usage(stale=True, write=self.repair_unrelated_state)

    def test_current_repair_preserves_pending_native_usage(self):
        self.check_limits_with_pending_usage(stale=False, write=self.repair_unrelated_state)


    def crashed_operation(self, data):
        data['operation'] = {'id': 'crashed-fixture', 'kind': 'validate', 'status': 'running'}
        hatch.save(self.root, data)

    def test_stale_resume_preserves_pending_native_usage(self):
        # resume saves its recovered operation before any reconciliation.
        self.check_limits_with_pending_usage(
            stale=True, before_claim=self.crashed_operation,
            write=lambda data: hatch.resume(self.root, data))

    def test_current_resume_preserves_pending_native_usage(self):
        self.check_limits_with_pending_usage(
            stale=False, before_claim=self.crashed_operation,
            write=lambda data: hatch.resume(self.root, data))

    def test_profile_revision_from_a_pre_claim_snapshot_preserves_the_claim(self):
        # The snapshot has no running row, so its own running-claim check passes.
        profile = self.root / 'profile.json'
        revision = profile.with_name('profile-revision.json')
        revision.write_bytes(profile.read_bytes())
        self.check_limits_with_pending_usage(
            stale=True, snapshot_before_claim=True,
            write=lambda data: hatch.revise_profile(self.root, data, revision))

if __name__ == '__main__':
    unittest.main()
