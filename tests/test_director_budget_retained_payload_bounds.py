"""Independent offline retained-payload, path, approval and journal-size contracts.

All bytes and timing exchanges are synthetic local fixtures. Size cases lower
both public source-policy bindings to 128 KiB and exercise real serialization,
publication and bounded reopening; they are not full 64 MiB execution evidence.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from PIL import Image
import asset_contracts
import hatch
from director_budget import BudgetLedger, NativeBudget
from rig_errors import RigError
import test_director_budget_native as fixture
import test_director_budget_publication_edges as edges


class RetainedPayloadBoundsTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    observe = fixture.NativeBudgetTests.observe
    report_failure = fixture.NativeBudgetTests.fail

    def journal_path(self):
        return self.root / 'imagegen-jobs.json'

    def job(self, name='base'):
        return hatch.job_for(hatch.manifest(self.root, recovery=True), name)

    def reconcile(self):
        with hatch.ownership(self.root):
            return NativeBudget.for_run(self.root, hatch.manifest(self.root, recovery=True),
                                        recovery=True).reconcile()

    def assert_refused_unchanged(self, action, *, untouched=None):
        before_ledger = self.ledger.snapshot()
        before_journal = self.journal_path().read_bytes()
        owner_records = {path: path.read_bytes() for path in
                         self.owner.rglob('director-budget-owner.json')}
        untouched_bytes = untouched.read_bytes() if untouched else None
        error, returned = None, None
        try:
            returned = action()
        except (ValueError, OSError, RigError) as exc:
            error = exc
        # Independent subtests retain the persistence evidence even when refusal
        # itself is absent. Never mask TestCase.fail with a fixture helper.
        with self.subTest(boundary='ledger'):
            self.assertEqual(self.ledger.snapshot(), before_ledger)
        with self.subTest(boundary='journal'):
            self.assertEqual(self.journal_path().read_bytes(), before_journal)
        with self.subTest(boundary='owner'):
            self.assertEqual({path: path.read_bytes() for path in
                              self.owner.rglob('director-budget-owner.json')}, owner_records)
        if untouched:
            with self.subTest(boundary='unrelated regular file'):
                self.assertEqual(untouched.read_bytes(), untouched_bytes,
                                 'An unrelated ordinary in-run file was overwritten')
        self.assertIsNotNone(error, 'Expected fail-closed refusal; returned result type ' + type(returned).__name__)

    def interrupted_observation(self, token, observation):
        def stop_before_settlement(*args, **kwargs):
            raise RuntimeError('synthetic interruption before usage settlement')
        with patch.object(BudgetLedger, 'settle', side_effect=stop_before_settlement):
            with self.assertRaisesRegex(RuntimeError, 'synthetic interruption before usage settlement'):
                self.report_failure(token, observation)
        event = self.job()['budget_operation']['events'][-1]
        self.assertEqual(event['payload']['id'], observation['id'])
        self.assertTrue(event['evidence'], 'Pending usage must already retain its proof')
        self.assertNotIn('receipt', event)

    def mutate_retained_field(self, field, pending):
        self.root = self.prepare('payload-' + field + '-' + str(pending))
        self.bind(self.root)
        token = self.claim()['token']
        unknown = self.observe('original-unknown', 'uncertain', amount=None)
        if pending:
            self.interrupted_observation(token, unknown)
        else:
            self.report_failure(token, unknown)
            self.assertEqual(self.job()['budget_operation']['events'][-1]['receipt']['state'], 'uncertain')
        original = self.journal_path().read_bytes()
        data = hatch.manifest(self.root, recovery=True)
        event = hatch.job_for(data, 'base')['budget_operation']['events'][-1]
        values = {'id': 'forged-unknown', 'token': '0' * 32,
                  'resolves': ['invented-resolution'],
                  'measurement_source': 'Forged source substituted after evidence retention'}
        event['payload'][field] = values[field]
        hatch.atomic(self.journal_path(), data)
        self.assert_refused_unchanged(self.reconcile)
        # Restore original bytes and prove the same pending/acknowledged event
        # still recovers once and uncertainty requires its real ID to resolve.
        self.journal_path().write_bytes(original)
        self.reconcile()
        self.report_failure(token, self.observe('real-resolution', 'retry', calls=0,
                                               amount='0.75', resolves=['original-unknown']))
        row = self.ledger.snapshot()['state']['reservations'][self.job()['budget_operation']['reservation_id']]
        self.assertEqual(row['spent']['provider_calls'], '1')
        self.assertEqual(row['spent']['amount'], '0.75')
        self.assertNotEqual(self.claim()['token'], token)

    def test_retained_usage_observation_id_is_authenticated_before_replay(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.mutate_retained_field('id', pending)

    def test_retained_usage_token_is_authenticated_before_replay(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.mutate_retained_field('token', pending)

    def test_retained_usage_resolution_list_is_authenticated_before_replay(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.mutate_retained_field('resolves', pending)

    def test_retained_usage_measurement_source_is_authenticated_before_replay(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.mutate_retained_field('measurement_source', pending)

    def test_forged_unknown_id_cannot_reduce_retained_uncertainty(self):
        token = self.claim()['token']
        self.report_failure(token, self.observe('real-unknown', 'uncertain', amount=None))
        data = hatch.manifest(self.root, recovery=True)
        hatch.job_for(data, 'base')['budget_operation']['events'][-1]['payload']['id'] = 'forged-unknown'
        hatch.atomic(self.journal_path(), data)
        known = self.observe('forged-resolution', 'retry', calls=0, amount='0.5',
                             resolves=['forged-unknown'])
        self.assert_refused_unchanged(lambda: self.report_failure(token, known))

    def test_unmodified_pending_and_acknowledged_usage_recover_once(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.root = self.prepare('usage-control-' + str(pending))
                self.bind(self.root)
                token = self.claim()['token']
                observation = self.observe('control-unknown', 'uncertain', amount=None)
                if pending:
                    self.interrupted_observation(token, observation)
                else:
                    self.report_failure(token, observation)
                self.reconcile()
                once = self.ledger.snapshot()
                self.reconcile()
                self.assertEqual(self.ledger.snapshot(), once)
                self.assertEqual(self.job()['token'], token)
                self.report_failure(token, self.observe('control-known', 'retry', calls=0,
                                                       amount='0.5', resolves=['control-unknown']))
                self.assertNotEqual(self.claim()['token'], token)

    def approve_base(self):
        claim = self.claim()
        source = self.owner / 'synthetic-base.png'
        Image.new('RGBA', (96, 96), (30, 70, 160, 255)).save(source)
        receipt = deepcopy(claim['receipt_template'])
        receipt['budget_usage'] = self.observe('base-complete', 'complete')
        receipt_path = self.owner / 'synthetic-base-receipt.json'
        hatch.atomic(receipt_path, receipt)
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.ingest(self.root, data, 'base', source, claim['token'],
                         'Synthetic base only; no provider call', receipt_path, fixture=True)
            hatch.record_base_review(self.root, data, 'Synthetic local base approval', True)
        self.assertTrue(hatch.base_review_current(self.root, hatch.manifest(self.root)))

    def claim_state(self, name='rest'):
        with hatch.ownership(self.root):
            return hatch.claim(self.root, hatch.manifest(self.root), name)

    def test_retry_prompt_path_cannot_select_unrelated_regular_run_file(self):
        self.approve_base()
        target = self.root / 'ordinary-review-notes.md'
        target.write_bytes(b'Unrelated ordinary in-run notes must survive exactly.\n')
        data = hatch.manifest(self.root)
        row = hatch.job_for(data, 'rest')
        canonical = row['retry_prompt_file']
        row['retry_prompt_file'] = target.relative_to(self.root).as_posix()
        hatch.atomic(self.journal_path(), data)
        self.assert_refused_unchanged(self.claim_state, untouched=target)
        data = hatch.manifest(self.root, recovery=True)
        hatch.job_for(data, 'rest')['retry_prompt_file'] = canonical
        hatch.atomic(self.journal_path(), data)
        handoff = self.claim_state()
        self.assertEqual(self.job('rest')['token'], handoff['token'])
        self.assertEqual(target.read_bytes(), b'Unrelated ordinary in-run notes must survive exactly.\n')

    def test_production_retry_prompt_binding_never_overwrites_unrelated_regular_file(self):
        project = edges.BudgetPublicationEdgeTests.production_fixture(self)
        self.approve_base()
        target = self.root / 'ordinary-production-notes.md'
        target.write_bytes(b'Ordinary in-run production notes must remain intact.\n')
        data = hatch.manifest(self.root)
        hatch.job_for(data, 'rest')['retry_prompt_file'] = target.relative_to(self.root).as_posix()
        hatch.atomic(self.journal_path(), data)
        self.assert_refused_unchanged(
            lambda: fixture.workflow.claim_generation(project, 'prompt', 'rest'), untouched=target)

    def test_production_canonical_retry_prompt_binding_control(self):
        project = edges.BudgetPublicationEdgeTests.production_fixture(self)
        self.approve_base()
        handoff = fixture.workflow.claim_generation(project, 'prompt', 'rest')
        self.assertEqual(self.job('rest')['token'], handoff['token'])
        retry = self.root / 'prompts/row-retries/rest.md'
        self.assertIn(b'Production coverage binding SHA-256:', retry.read_bytes())

    def test_unmodified_canonical_retry_prompt_allows_state_handoff(self):
        self.approve_base()
        self.assertEqual(self.job('rest')['retry_prompt_file'], 'prompts/row-retries/rest.md')
        handoff = self.claim_state()
        self.assertEqual(self.job('rest')['token'], handoff['token'])

    def test_captured_base_identity_must_equal_the_reviewed_image_after_validation(self):
        self.approve_base()
        canonical = self.root / 'references/canonical-base.png'
        approved = canonical.read_bytes()
        alternate = self.owner / 'synthetic-alternate.png'
        Image.new('RGBA', (96, 96), (160, 70, 30, 255)).save(alternate)
        replacement = alternate.read_bytes()
        self.assertNotEqual(hashlib.sha256(approved).hexdigest(), hashlib.sha256(replacement).hexdigest())
        real_review = hatch.base_review_current
        reached = []

        def replace_after_real_review(*args, **kwargs):
            valid = real_review(*args, **kwargs)
            if valid and not reached:
                reached.append(True)
                canonical.write_bytes(replacement)
            return valid

        with patch.object(hatch, 'base_review_current', side_effect=replace_after_real_review):
            self.assert_refused_unchanged(self.claim_state)
        self.assertEqual(reached, [True], 'The real approval-validation boundary must execute')
        self.assertEqual(canonical.read_bytes(), replacement)
        canonical.write_bytes(approved)
        handoff = self.claim_state()
        self.assertEqual(handoff['receipt_template']['input_hashes']['references/canonical-base.png'],
                         hashlib.sha256(approved).hexdigest())

    @contextmanager
    def scaled_source_policy(self):
        # Both exposed bindings must agree. Actual file reads/JSON encode/save
        # are untouched; no mocked stat size or synthetic reader result is used.
        limit = 128 * 1024
        with patch.object(asset_contracts, 'MAX_SOURCE_BYTES', limit), \
                patch.object(hatch, 'MAX_SOURCE_BYTES', limit):
            yield limit

    def assert_action_keeps_reopenable_journal(self, action):
        error = None
        try:
            result = action()
        except (ValueError, OSError, RigError) as exc:
            error, result = exc, None
        with self.subTest(boundary='encoded journal size'):
            self.assertLessEqual(self.journal_path().stat().st_size, hatch.MAX_SOURCE_BYTES,
                                 'Native mutation published JSON its own bounded reader cannot reopen')
        # Exercise the real bounded reader even when the size assertion fails.
        reopened, reopen_error = None, None
        try:
            reopened = hatch.manifest(self.root, recovery=True)
        except (ValueError, OSError, RigError) as exc:
            reopen_error = exc
        self.assertIsNone(reopen_error, 'Actual bounded reopening refused the published journal: ' +
                          str(reopen_error))
        self.assertIsInstance(reopened, dict)
        if error is not None:
            self.assertIsNone(result)
        return result

    def test_scaled_prompt_json_escaping_cannot_publish_unreadable_journal(self):
        with self.scaled_source_policy() as limit:
            prompt = self.root / 'prompts/base-pet.md'
            prompt.write_bytes(b'\x00' * (limit // 4))
            self.assertLess(prompt.stat().st_size, limit)
            self.assert_action_keeps_reopenable_journal(self.claim)

    def test_scaled_prompt_with_json_overhead_cannot_publish_unreadable_journal(self):
        with self.scaled_source_policy() as limit:
            prompt = self.root / 'prompts/base-pet.md'
            prompt.write_bytes(b'P' * (limit - 4096))
            self.assertLess(prompt.stat().st_size, limit)
            self.assert_action_keeps_reopenable_journal(self.claim)

    def test_scaled_cumulative_prompt_history_cannot_outgrow_journal_reader(self):
        with self.scaled_source_policy() as limit:
            prompt = self.root / 'prompts/base-pet.md'
            prompt.write_bytes(b'History payload\n' + b'P' * (limit // 3))
            for attempt in range(3):
                handoff = self.assert_action_keeps_reopenable_journal(self.claim)
                if handoff is None:
                    break
                self.assertEqual(self.job()['attempts'], attempt + 1)
                result = self.assert_action_keeps_reopenable_journal(lambda: self.report_failure(
                    handoff['token'], self.observe('history-' + str(attempt), 'retry')))
                if result is None and self.job()['status'] == 'running':
                    break

    def test_scaled_small_prompt_and_retry_history_reopen_normally(self):
        with self.scaled_source_policy():
            for attempt in range(3):
                handoff = self.assert_action_keeps_reopenable_journal(self.claim)
                self.assertIsNotNone(handoff)
                self.assert_action_keeps_reopenable_journal(lambda: self.report_failure(
                    handoff['token'], self.observe('small-' + str(attempt), 'retry')))
            self.assertEqual(self.job()['attempts'], 3)


if __name__ == '__main__':
    unittest.main()
