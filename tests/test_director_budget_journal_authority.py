"""Offline rollback admission contracts; synthetic accounting is not provider execution.

Only imagegen-jobs.json is restored. The current enrollment, owner and ledger
remain in place, so an older native journal cannot grant another handoff.
"""
from copy import deepcopy
import unittest
import uuid
from unittest.mock import patch

import hatch
from director_budget import NativeBudget, object_hash
from rig_errors import RigError
import test_director_budget_native as native_fixture


class BudgetJournalAuthorityTests(unittest.TestCase):
    # Reuse synthetic builders without collecting the original TestCase again.
    setUp = native_fixture.NativeBudgetTests.setUp
    prepare = native_fixture.NativeBudgetTests.prepare
    bind = native_fixture.NativeBudgetTests.bind
    claim = native_fixture.NativeBudgetTests.claim
    observe = native_fixture.NativeBudgetTests.observe
    report_failure = native_fixture.NativeBudgetTests.fail

    def new_run(self, name, *, retries=0):
        self.root = self.prepare(name)
        self.bind(self.root, retry_allowance=retries)

    def journal_path(self):
        return self.root / 'imagegen-jobs.json'

    def change_prompt(self):
        data = hatch.manifest(self.root)
        prompt = self.root / hatch.job_for(data, 'base')['prompt_file']
        with prompt.open('a', encoding='utf-8') as handle:
            handle.write('\nExplicit synthetic alternate prompt for rollback admission.\n')

    def pending_token(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            job = hatch.job_for(data, 'base')
            budget = NativeBudget.for_run(self.root, data)
            return budget.before_claim(job, hatch.inputs(self.root, job))

    def assert_rejected_without_mutation(self, action):
        before_ledger = self.ledger.snapshot()
        before_journal = self.journal_path().read_bytes()
        owner_path = self.owner / 'director-budget-owner.json'
        before_owner = owner_path.read_bytes()
        error = None
        returned = None
        try:
            returned = action()
        except (ValueError, OSError, RigError) as exc:
            error = exc
        # Check every persistence boundary even if a handoff was returned.
        after_ledger = self.ledger.snapshot()
        after_journal = self.journal_path().read_bytes()
        after_owner = owner_path.read_bytes()
        with self.subTest(boundary='ledger'):
            self.assertEqual(after_ledger, before_ledger,
                             'rejected stale admission must not change accounting')
        with self.subTest(boundary='native journal'):
            self.assertEqual(after_journal, before_journal,
                             'rejected stale admission must not rewrite the journal')
        with self.subTest(boundary='retained owner'):
            self.assertEqual(after_owner, before_owner)
        self.assertIsNotNone(error,
            'retained authority must reject the stale native journal action; '
            'returned a result: ' + repr(returned is not None))

    def assert_claim_rejected_without_mutation(self):
        self.assert_rejected_without_mutation(self.claim)

    def test_enrollment_only_journal_cannot_manufacture_another_claim(self):
        for changed_prompt in (False, True):
            with self.subTest(changed_prompt=changed_prompt):
                self.new_run('enrollment-' + str(changed_prompt))
                enrolled = self.journal_path().read_bytes()
                published = self.claim()
                self.assertTrue(published['token'])
                self.journal_path().write_bytes(enrolled)
                if changed_prompt:
                    self.change_prompt()
                self.assert_claim_rejected_without_mutation()

    def test_prepublication_journal_cannot_return_a_published_pending_token(self):
        for changed_prompt in (False, True):
            with self.subTest(changed_prompt=changed_prompt):
                self.new_run('pending-' + str(changed_prompt))
                pending = self.pending_token()
                saved_pending = self.journal_path().read_bytes()
                published = self.claim()
                self.assertEqual(published['token'], pending)
                # This exact pending token was already handed to the caller.
                self.journal_path().write_bytes(saved_pending)
                if changed_prompt:
                    self.change_prompt()
                self.assert_claim_rejected_without_mutation()

    def test_earlier_retry_journal_cannot_erase_later_claim_and_usage(self):
        for changed_prompt in (False, True):
            with self.subTest(changed_prompt=changed_prompt):
                self.new_run('retry-' + str(changed_prompt), retries=2)
                first = self.claim()
                self.report_failure(first['token'], self.observe('first-retry', 'retry'))
                earlier_retry = self.journal_path().read_bytes()
                second = self.claim()
                self.assertNotEqual(second['token'], first['token'])
                self.report_failure(second['token'], self.observe('second-retry', 'retry'))
                rows = self.ledger.snapshot()['state']['reservations'].values()
                row = next(row for row in rows if row['id'] == second['budget_reservation_id'])
                self.assertEqual(row['spent']['provider_calls'], '2')
                self.assertEqual(row['spent']['amount'], '1.0')
                self.journal_path().write_bytes(earlier_retry)
                if changed_prompt:
                    self.change_prompt()
                self.assert_claim_rejected_without_mutation()

    def test_unpublished_pending_token_recovers_exactly_once(self):
        self.new_run('interrupted-before-publication')
        pending = self.pending_token()
        before = self.ledger.snapshot()
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['status'], 'pending')
        self.assertNotIn('token', job)
        published = self.claim()
        self.assertEqual(published['token'], pending)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        self.assertEqual(self.ledger.snapshot()['state']['reservations'].keys(),
                         before['state']['reservations'].keys())
        self.assert_claim_rejected_without_mutation()

    def test_failed_native_claim_publication_recovers_original_pending_token(self):
        self.new_run('failed-native-publication')
        pending = self.pending_token()
        pending_journal = self.journal_path().read_bytes()
        original_save = hatch.save

        def stop_before_running_save(root, data):
            if hatch.job_for(data, 'base')['status'] == 'running':
                raise RuntimeError('synthetic stop before native claim publication')
            return original_save(root, data)

        with patch.object(hatch, 'save', side_effect=stop_before_running_save):
            with self.assertRaisesRegex(RuntimeError, 'synthetic stop before native claim publication'):
                self.claim()
        self.assertEqual(self.journal_path().read_bytes(), pending_journal)
        reopened = self.claim()
        self.assertEqual(reopened['token'], pending)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        self.assert_claim_rejected_without_mutation()

    def test_old_same_token_journal_cannot_erase_retained_uncertainty(self):
        for outcome in ('retry', 'complete'):
            with self.subTest(outcome=outcome):
                self.new_run('uncertainty-' + outcome)
                claim = self.claim()
                before_uncertainty = self.journal_path().read_bytes()
                self.report_failure(claim['token'],
                                    self.observe('authentic-unknown', 'uncertain', amount=None))
                row = self.ledger.snapshot()['state']['reservations'][
                    claim['budget_reservation_id']]
                self.assertEqual(row['state'], 'uncertain')
                self.journal_path().write_bytes(before_uncertainty)
                known = self.observe('known-without-resolution', outcome, calls=0, amount='0.75')

                def report_known():
                    with hatch.ownership(self.root):
                        return hatch.record_budget_usage(self.root, hatch.manifest(self.root),
                                                         'base', claim['token'], known)

                self.assert_rejected_without_mutation(report_known)

    def test_current_retry_journal_preserves_input_binding_and_allows_valid_retry(self):
        self.new_run('current-retry', retries=1)
        first = self.claim()
        self.report_failure(first['token'], self.observe('authentic-retry', 'retry'))
        data = hatch.manifest(self.root)
        prompt = self.root / hatch.job_for(data, 'base')['prompt_file']
        original_prompt = prompt.read_bytes()
        self.change_prompt()
        self.assert_claim_rejected_without_mutation()
        prompt.write_bytes(original_prompt)
        second = self.claim()
        self.assertNotEqual(second['token'], first['token'])
        rows = self.ledger.snapshot()['state']['reservations'].values()
        self.assertLessEqual(sum(int(row['reserved']['provider_calls']) for row in rows), 2)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 2)

    def test_historical_pending_intent_without_publication_protocol_cannot_issue_handoff(self):
        """Construct the historical 3358f0d5 intent using public offline APIs.

        A historical pending journal cannot establish whether its handoff was
        already published. Its retained reservation must remain unchanged.
        """
        self.new_run('historical-pending-intent')
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            job = hatch.job_for(data, 'base')
            policy = deepcopy(data['director_budget'])
            hashes = hatch.inputs(self.root, job)
            identifier = 'native-' + object_hash({
                'run_id': data['run_id'], 'job': job['id'], 'inputs': hashes,
                'fixture': data['fixture_mode'], 'policy': policy})
            request = {key: deepcopy(policy[key]) for key in
                       ('provider', 'estimate', 'retry_allowance', 'unknown_price', 'unknown_price_cap')}
            request.update(operation_id=identifier, idempotency_key=identifier + ':reserve',
                           evidence=deepcopy(policy['evidence']))
            # This is the complete old intent format, deliberately constructed
            # without invoking the runtime NativeBudget.before_claim method.
            intent = {
                'kind': 'native-budget-intent', 'run_id': data['run_id'], 'job': job['id'],
                'fixture': data['fixture_mode'], 'operation_id': identifier,
                'input_hashes': hashes, 'policy': policy}
            self.assertNotIn('publication_protocol', intent)
            path = self.owner / 'historical-native-intent.json'
            hatch.atomic(path, intent)
            request['evidence'].append(self.ledger.retain_evidence(path))
            receipt = self.ledger.reserve(**request,
                                          expected_revision=self.ledger.snapshot()['sha256'])
            job['budget_operation'] = {
                'operation_id': identifier, 'input_hashes': deepcopy(hashes),
                'reservation_request': request, 'attempts': [uuid.uuid4().hex],
                'events': [], 'reservation_id': receipt['reservation_id']}
            hatch.save(self.root, data)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'pending')
        self.assert_claim_rejected_without_mutation()


if __name__ == '__main__':
    unittest.main()
