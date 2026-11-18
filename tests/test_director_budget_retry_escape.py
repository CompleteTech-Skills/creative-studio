"""Offline regressions for escaped retry capacity and repair after closure loss.

These use real local owner/ledger files, a scaled 128 KiB source policy, and
synthetic retained evidence. The only scheduling hook interrupts a native
closure publication; no provider or full-cap memory run is involved.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

import hatch
from director_budget import BudgetLedger, object_hash
from rig_errors import RigError
import test_director_budget_recovery_collisions as collision


class RetryEscapeTests(unittest.TestCase):
    # Reuse fixtures without inheriting their independently registered tests.
    setUp = collision.BudgetRecoveryCollisionTests.setUp
    prepare = collision.BudgetRecoveryCollisionTests.prepare
    bind = collision.BudgetRecoveryCollisionTests.bind
    claim = collision.BudgetRecoveryCollisionTests.claim
    row = collision.BudgetRecoveryCollisionTests.row
    observe = collision.BudgetRecoveryCollisionTests.observe
    job = collision.BudgetRecoveryCollisionTests.job
    journal_path = collision.BudgetRecoveryCollisionTests.journal_path
    envelope_fixture = collision.BudgetRecoveryCollisionTests.envelope_fixture
    envelope_run = collision.BudgetRecoveryCollisionTests.envelope_run
    maximal_usage = collision.BudgetRecoveryCollisionTests.maximal_usage
    scaled_source_policy = collision.BudgetRecoveryCollisionTests.scaled_source_policy
    report_job = collision.BudgetRecoveryCollisionTests.report_job

    def reconcile(self):
        with hatch.ownership(self.root):
            hatch.reconcile(self.root, hatch.manifest(self.root, recovery=True))

    def closures(self):
        operation = self.job()['budget_operation']['operation_id']
        return [event for event in self.ledger.snapshot()['state']['events']
                if event['action'] == 'native-finalize' and
                event['payload']['operation_id'] == operation]

    def cancel(self):
        with hatch.ownership(self.root):
            return hatch.release_budget(self.root, hatch.manifest(self.root, recovery=True), 'base',
                idempotency_key='retry-escape-cancel', outcome='canceled-known', evidence=self.proof)

    def assert_one_cancellation(self):
        before = self.ledger.snapshot()
        receipt = self.cancel()
        self.assertEqual(receipt['state'], 'released')
        self.assertEqual(receipt['spent']['amount'], '0.25')
        self.assertEqual(receipt['spent']['provider_calls'], '1')
        self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
        once = self.ledger.snapshot()
        self.assertEqual(once['state']['revision'], before['state']['revision'] + 1)
        self.assertEqual(len([event for event in once['state']['events']
                              if event['action'] == 'release']), 1)
        self.assertEqual(self.cancel(), receipt)
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), once,
                         'Cancellation and recovery replay must not duplicate release or spend')

    def assert_histories(self, claimed, run_history):
        data = hatch.manifest(self.root, recovery=True)
        self.assertEqual(self.job()['history'][:len(claimed)], claimed,
                         'Original decoded prompt snapshots and claim history must survive')
        self.assertEqual(data['history'][:len(run_history)], run_history)

    def retry_capacity(self, size, *, may_refuse):
        owner = self.envelope_fixture()
        with self.scaled_source_policy() as limit:
            self.envelope_run(owner, 'probe', size)
            original = deepcopy(self.job())
            run_history = deepcopy(hatch.manifest(self.root)['history'])
            before_claim = self.ledger.snapshot()
            try:
                handoff = self.claim()
            except (ValueError, RigError):
                self.assertTrue(may_refuse, 'Small prompt must admit a recoverable claim')
                self.assertEqual(self.job()['status'], 'pending')
                self.assertEqual(self.job()['history'], original['history'])
                self.assertEqual(self.job()['attempts'], 0)
                self.assertNotIn('token', self.job())
                self.assertEqual([event for event in self.ledger.snapshot()['state']['events']
                                  if event['action'] == 'native-claim'],
                                 [event for event in before_claim['state']['events']
                                  if event['action'] == 'native-claim'])
                self.assert_histories(original['history'], run_history)
                self.reconcile()
                admitted = self.ledger.snapshot()['state']['reservations']
                new_ids = set(admitted) - set(before_claim['state']['reservations'])
                self.assertLessEqual(len(new_ids), 1)
                if new_ids:
                    identifier = new_ids.pop()
                    self.assertEqual(self.job()['budget_operation']['reservation_id'], identifier)
                    self.assertEqual(admitted[identifier]['state'], 'reserved')
                    self.assertTrue(all(value == '0' for value in admitted[identifier]['spent'].values()))
                    with hatch.ownership(self.root):
                        receipt = hatch.release_budget(self.root, hatch.manifest(self.root, recovery=True),
                            'base', idempotency_key='unpublished-escape-cancel',
                            outcome='unstarted', evidence=self.proof)
                    self.assertEqual(receipt['state'], 'released')
                    self.assertTrue(all(value == '0' for value in receipt['spent'].values()))
                    self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
                    once = self.ledger.snapshot()
                    self.reconcile()
                    self.assertEqual(self.ledger.snapshot(), once)
                self.assert_histories(original['history'], run_history)
                self.assertLessEqual(self.journal_path().stat().st_size, limit)
                return
            token = handoff['token']
            claimed = deepcopy(self.job()['history'])
            observation = self.maximal_usage('x' * 128, 'retry')
            observation['measurement_source'] = '\x01' * 1024
            before = self.ledger.snapshot()
            native_events = deepcopy(self.job()['budget_operation']['events'])
            try:
                self.report_job('base', token, observation)
            except (ValueError, RigError) as error:
                self.assertTrue(may_refuse, 'Small prompt must accept maximal retry: ' + str(error))
                after = self.ledger.snapshot()
                events = deepcopy(self.job()['budget_operation']['events'])
                diagnostics = []
                # Exercise the two genuine escape routes on a partial commit.
                # A new terminal observation has zero usage because the retry
                # delta has already been accounted; it must never charge twice.
                if after != before or events != native_events:
                    terminal = self.observe('minimal-terminal', 'failed', calls=0, amount='0')
                    terminal['measurement_source'] = 's'
                    for label, usage in [('exact retry replay', observation), ('minimal terminal', terminal)]:
                        try:
                            self.report_job('base', token, usage)
                        except (ValueError, RigError) as recovery_error:
                            diagnostics.append(label + ': ' + str(recovery_error))
                        else:
                            diagnostics.append(label + ': succeeded')
                self.assertTrue(after == before and events == native_events,
                    'Retry refusal must precede every accounting delta/native event write; '
                    f"status={self.job()['status']}, spent={self.row()['spent']['amount']}, "
                    f"held={self.row()['held']['amount']}; initial error={error}; " + '; '.join(diagnostics))
                self.assert_histories(claimed, run_history)
                self.assertEqual(self.job()['status'], 'running')
                self.assertEqual(self.job()['token'], token)
                # Refusal must retain capacity for a concise real terminal
                # report of the actual quarter-dollar, one-call measurement.
                terminal = self.observe('concise-terminal', 'failed', amount='0.25')
                terminal['measurement_source'] = 's'
                self.report_job('base', token, terminal)
                self.assertEqual(self.job()['status'], 'failed')
                self.assertEqual(self.row()['state'], 'settled')
                self.assertEqual(self.row()['spent']['amount'], '0.25')
                self.assertEqual(self.row()['spent']['provider_calls'], '1')
                self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
                self.assert_histories(claimed, run_history)
                once = self.ledger.snapshot()
                self.reconcile()
                self.assertEqual(self.ledger.snapshot(), once)
                self.assertLessEqual(self.journal_path().stat().st_size, limit)
                return
            self.assertEqual(self.job()['status'], 'failed')
            self.assertNotIn('token', self.job())
            self.assert_histories(claimed, run_history)
            event = self.job()['budget_operation']['events'][-1]
            self.assertEqual(event['payload']['id'], observation['id'])
            self.assertEqual(event['payload']['measurement_source'], observation['measurement_source'])
            self.assertEqual(event['payload']['evidence'], observation['evidence'])
            self.assertEqual(event['evidence'][:-1], observation['evidence'])
            self.assertEqual(event['receipt']['state'], 'reserved')
            self.assertEqual(self.row()['spent']['amount'], '0.25')
            self.assertEqual(self.row()['spent']['provider_calls'], '1')
            self.assertEqual(self.row()['held']['amount'], '1.75')
            self.assertEqual(len(self.row()['attempts']), 1)
            self.assertEqual(len(self.closures()), 1)
            once = self.ledger.snapshot()
            with hatch.ownership(self.root):
                hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True),
                                          'base', token, observation)
            self.reconcile()
            self.assertEqual(self.ledger.snapshot(), once,
                             'Exact maximal observation replay must retain its identity without another delta')
            self.assert_one_cancellation()
            self.assert_histories(claimed, run_history)
            self.assertLessEqual(self.journal_path().stat().st_size, limit)

    def test_scaled_escaped_maximal_retry_keeps_a_durable_terminal_escape(self):
        self.retry_capacity(70205, may_refuse=True)

    def test_scaled_small_prompt_accepts_maximal_retry_and_cancels_once(self):
        self.retry_capacity(1024, may_refuse=False)

    def test_repaired_owner_recovers_original_retry_closure_without_another_delta(self):
        token = self.claim()['token']
        claimed = deepcopy(self.job()['history'])
        observation = self.observe('retry-before-repair', 'retry', amount='0.25')
        original_change = BudgetLedger._change
        interrupted = []

        def interrupt(ledger, revision, key, action, payload, mutate):
            if action == 'native-finalize':
                interrupted.append(deepcopy(payload))
                raise OSError('Synthetic interruption before native closure publication')
            return original_change(ledger, revision, key, action, payload, mutate)

        with patch.object(BudgetLedger, '_change', interrupt), self.assertRaises(OSError):
            self.report_job('base', token, observation)
        self.assertEqual(len(interrupted), 1)
        failed = deepcopy(self.job())
        self.assertEqual(failed['status'], 'failed')
        self.assertNotIn('token', failed)
        self.assertEqual(failed['history'][:len(claimed)], claimed)
        self.assertEqual((failed['history'][-1]['event'], failed['history'][-1]['token']), ('failed', token))
        self.assertEqual(self.closures(), [])
        self.assertEqual(len(self.row()['attempts']), 1)
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        before_repair = self.ledger.snapshot()
        with hatch.ownership(self.root):
            hatch.repair(self.root, hatch.manifest(self.root, recovery=True), 'base',
                         'Synthetic public repair after durable retry failure')
        repaired = deepcopy(self.job())
        self.assertEqual(repaired['status'], 'pending')
        self.assertEqual(repaired['attempts'], 0)
        self.assertEqual(repaired['history'][:len(failed['history'])], failed['history'])
        self.assertEqual(repaired['history'][-1]['event'], 'repair')
        self.assertEqual(repaired['budget_operation']['events'], failed['budget_operation']['events'])
        self.reconcile()
        closures = self.closures()
        diagnostics = []
        if not closures:
            for label, cancel in [('native canceled-known', self.cancel),
                    ('generic canceled-known', lambda: self.ledger.release(self.row()['id'],
                        outcome='canceled-known', evidence=self.proof,
                        idempotency_key='repair-generic-cancel',
                        expected_revision=self.ledger.snapshot()['sha256']))]:
                try:
                    cancel()
                except (ValueError, RigError) as error:
                    diagnostics.append(label + ': ' + str(error))
                else:
                    diagnostics.append(label + ': succeeded')
        self.assertEqual(len(closures), 1,
            'Preserved claim/failed history must recover closure after public repair changes status; '
            + '; '.join(diagnostics))
        self.assertEqual(closures[0]['payload'], interrupted[0])
        transitions = [entry for entry in failed['history'] if entry['event'] in ('claim', 'failed')]
        self.assertEqual(closures[0]['payload']['owner_history_sha256'], object_hash(transitions))
        self.assertEqual(self.job()['status'], 'pending')
        self.assertEqual(self.job()['history'], repaired['history'])
        self.assertEqual(self.row(), before_repair['state']['reservations'][self.row()['id']])
        self.assertEqual(self.ledger.snapshot()['state']['revision'], before_repair['state']['revision'] + 1)
        once = self.ledger.snapshot()
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), once)
        self.assert_one_cancellation()
        self.assertEqual(self.job()['history'], repaired['history'])


if __name__ == '__main__':
    unittest.main()
