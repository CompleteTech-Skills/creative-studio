"""Independent dispatch-authority regressions with offline synthetic native fixtures.

These cases use the landed public APIs and existing fixture helpers. No provider
is invoked, and no implementation-specific corrective method is assumed.
"""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_director_budget_native as fixture


class BudgetDispatchGuardTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    row = fixture.NativeBudgetTests.row

    def pending_job(self):
        return fixture.hatch.job_for(fixture.hatch.manifest(self.root), 'base')

    def settings_fixture(self):
        request = fixture.hatch.read(self.root / 'pet_request.json')
        return request['extraction']

    def assert_no_published_handoff(self, action):
        returned = []
        refused = False
        try:
            returned.append(action())
        except (ValueError, fixture.RigError):
            refused = True
        self.assertTrue(refused, 'Native claim returned authority after dispatch admission changed')
        self.assertEqual(returned, [])
        job = self.pending_job()
        self.assertNotEqual(job['status'], 'running')
        self.assertNotIn('token', job)

    def test_generic_unstarted_release_cannot_erase_a_published_native_handoff(self):
        handoff = self.claim()
        self.assertEqual(self.pending_job()['token'], handoff['token'])
        before = self.ledger.snapshot()
        journal_before = (self.root / 'imagegen-jobs.json').read_bytes()
        with self.assertRaises((ValueError, fixture.RigError)):
            self.ledger.release(handoff['budget_reservation_id'], outcome='unstarted',
                                expected_revision=before['sha256'],
                                idempotency_key='published-unstarted-refusal', evidence=self.proof)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal_before)

    def test_legitimate_unstarted_reservation_can_cancel_before_publication(self):
        with fixture.hatch.ownership(self.root):
            data = fixture.hatch.manifest(self.root)
            job = fixture.hatch.job_for(data, 'base')
            budget = fixture.NativeBudget.for_run(self.root, data)
            budget.before_claim(job, fixture.hatch.inputs(self.root, job))
        self.assertEqual(self.pending_job()['status'], 'pending')
        self.assertEqual(self.pending_job()['attempts'], 0)
        self.assertNotIn('token', self.pending_job())
        identifier = self.row()['id']
        receipt = self.ledger.release(identifier, outcome='unstarted',
                                      expected_revision=self.ledger.snapshot()['sha256'],
                                      idempotency_key='unpublished-unstarted-control', evidence=self.proof)
        self.assertEqual(receipt['state'], 'released')
        row = self.ledger.snapshot()['state']['reservations'][identifier]
        self.assertTrue(all(value == '0' for value in row['spent'].values()))
        self.assertTrue(all(value == '0' for value in row['held'].values()))

    def test_other_reservation_overrun_between_admission_and_publication_blocks_handoff(self):
        other = self.prepare('other-observed-work')
        self.bind(other)
        other_handoff = self.claim(other)
        identifier = other_handoff['budget_reservation_id']
        original = fixture.NativeBudget.before_claim
        observed = []

        def inject_after_admission(budget, *args, **kwargs):
            token = original(budget, *args, **kwargs)
            def commit_other_observation():
                ledger = fixture.BudgetLedger(self.ledger.root)
                receipt = ledger.settle(identifier,
                    usage=fixture.quantities(provider_calls=7, amount=13), outcome='failed',
                    expected_revision=ledger.snapshot()['sha256'],
                    idempotency_key='concurrent-observed-overrun', evidence=self.proof)
                return receipt, ledger.snapshot()

            writer = ThreadPoolExecutor(max_workers=1)
            try:
                receipt, snapshot = writer.submit(commit_other_observation).result(timeout=10)
            finally:
                writer.shutdown(wait=False, cancel_futures=True)
            self.assertTrue(receipt['over_budget'])
            observed.append(snapshot)
            return token

        with patch.object(fixture.NativeBudget, 'before_claim', inject_after_admission):
            self.assert_no_published_handoff(self.claim)
        self.assertEqual(len(observed), 1)
        row = self.ledger.snapshot()['state']['reservations'][identifier]
        self.assertEqual(row['spent']['provider_calls'], '7')
        self.assertEqual(row['spent']['amount'], '13')
        self.assertEqual(row, observed[0]['state']['reservations'][identifier])

    def test_settings_mutation_after_admission_cannot_publish_handoff(self):
        path = self.root / 'extraction-settings.json'
        fixture.hatch.atomic(path, self.settings_fixture())
        before = path.read_bytes()
        original = fixture.NativeBudget.before_claim
        captured = []

        def mutate_after_admission(budget, *args, **kwargs):
            token = original(budget, *args, **kwargs)
            captured.append(token)
            path.write_bytes(before + b'\n')  # Same JSON value, different captured bytes.
            return token

        with patch.object(fixture.NativeBudget, 'before_claim', mutate_after_admission):
            self.assert_no_published_handoff(self.claim)
        self.assertEqual(len(captured), 1)
        self.assertNotEqual(path.read_bytes(), before)

    def interrupted_settings_change(self):
        path = self.root / 'extraction-settings.json'
        fixture.hatch.atomic(path, self.settings_fixture())
        before = path.read_bytes()
        original = fixture.NativeBudget.before_claim
        captured = []

        def stop_after_mutation(budget, *args, **kwargs):
            token = original(budget, *args, **kwargs)
            captured.append(token)
            path.write_bytes(before + b'\n')
            raise ValueError('Synthetic interruption after admission; no handoff returned')

        with patch.object(fixture.NativeBudget, 'before_claim', stop_after_mutation):
            with self.assertRaisesRegex(ValueError, 'Synthetic interruption'):
                self.claim()
        self.assertEqual(len(captured), 1)
        self.assertEqual(self.pending_job()['status'], 'pending')
        self.assertNotIn('token', self.pending_job())
        return path, before, captured[0]

    def test_retry_after_settings_refusal_cannot_rebase_on_changed_bytes(self):
        path, original_bytes, _ = self.interrupted_settings_change()
        self.assertNotEqual(path.read_bytes(), original_bytes)
        before_retry = self.ledger.snapshot()
        self.assert_no_published_handoff(self.claim)
        self.assertEqual(self.ledger.snapshot()['state']['reservations'],
                         before_retry['state']['reservations'])
        self.assertNotEqual(path.read_bytes(), original_bytes)

    def test_retry_with_original_settings_bytes_restored_reuses_pending_admission(self):
        path, original_bytes, admitted_token = self.interrupted_settings_change()
        path.write_bytes(original_bytes)
        handoff = self.claim()
        self.assertEqual(handoff['token'], admitted_token)
        self.assertEqual(self.pending_job()['attempts'], 1)
        self.assertEqual(self.pending_job()['token'], admitted_token)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_settings_absence_is_bound_before_new_file_appears_during_admission(self):
        path = self.root / 'extraction-settings.json'
        self.assertFalse(path.exists())
        settings = self.settings_fixture()
        original = fixture.NativeBudget.before_claim
        captured = []

        def create_after_admission(budget, *args, **kwargs):
            token = original(budget, *args, **kwargs)
            captured.append(token)
            fixture.hatch.atomic(path, settings)
            return token

        with patch.object(fixture.NativeBudget, 'before_claim', create_after_admission):
            self.assert_no_published_handoff(self.claim)
        self.assertEqual(len(captured), 1)
        self.assertTrue(path.is_file())
        self.assert_no_published_handoff(self.claim)


if __name__ == '__main__':
    unittest.main()
