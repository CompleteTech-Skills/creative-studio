"""Offline native/generic reconciliation and pinned prompt-write regressions.

The empty-resolution case was supplied by an independent nonauthor reviewer.
Namespace substitutions and accounting observations use disposable local files;
they never invoke a provider or claim measured production usage.
"""
from pathlib import Path
from contextlib import contextmanager
import os
import unittest
from unittest.mock import patch

import test_director_budget_native as fixture
import test_director_budget_publication_edges as edges


class BudgetAccountingBoundaryTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    row = fixture.NativeBudgetTests.row
    fail = fixture.NativeBudgetTests.fail
    observe = fixture.NativeBudgetTests.observe

    def generic(self, key, outcome, *, calls=0, amount='0'):
        return self.ledger.settle(self.row()['id'],
            usage=fixture.quantities(provider_calls=calls, amount=amount), outcome=outcome,
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key=key, evidence=self.proof)

    def cancel(self, key='generic-cancel'):
        return self.ledger.release(self.row()['id'], outcome='canceled-known', evidence=self.proof,
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key=key)

    def assert_refused_unchanged(self, action):
        ledger = self.ledger.snapshot()
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        with self.assertRaises((ValueError, fixture.RigError)):
            action()
        self.assertEqual(self.ledger.snapshot(), ledger)
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)

    def test_native_empty_resolution_cannot_clear_generic_unknown_usage(self):
        # Independent review oracle: native resolves=[] cannot clear direct
        # ledger uncertainty absent from the native observation journal.
        first = self.claim()
        journal_path = self.root / 'imagegen-jobs.json'
        original_journal = journal_path.read_bytes()
        self.generic('generic-unknown-provider-result', 'uncertain', calls=1, amount=None)
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(journal_path.read_bytes(), original_journal)
        self.assert_refused_unchanged(lambda: self.fail(first['token'], self.observe(
            'known-without-resolution', 'retry', calls=0, amount='0', resolves=[])))

    def test_native_uncertainty_cannot_hide_earlier_generic_unknown_usage(self):
        first = self.claim()
        self.generic('generic-unknown', 'uncertain', calls=1, amount=None)
        self.fail(first['token'], self.observe('native-unknown', 'uncertain', calls=0, amount=None))
        resolution = self.observe('native-resolution', 'retry', calls=0, amount='0',
                                  resolves=['native-unknown'])
        self.assert_refused_unchanged(lambda: self.fail(first['token'], resolution))
        self.generic('explicit-generic-reconciliation', 'retry', amount='0.4')
        self.fail(first['token'], resolution)
        self.assertEqual(self.row()['spent']['amount'], '0.4')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertNotEqual(self.claim()['token'], first['token'])

    def test_generic_unknown_between_journal_intent_and_commit_remains_authoritative(self):
        first = self.claim()
        observation = self.observe('known-native', 'retry', calls=0, amount='0')
        original = fixture.NativeBudget._save
        injected = []

        def inject_before_save(budget):
            operation = fixture.hatch.job_for(budget.data, 'base').get('budget_operation', {})
            if not injected and any(event['payload'].get('id') == observation['id']
                                    for event in operation.get('events', [])):
                injected.append(True)
                self.generic('raced-generic-unknown', 'uncertain', calls=1, amount=None)
            return original(budget)

        with patch.object(fixture.NativeBudget, '_save', inject_before_save):
            with self.assertRaises(fixture.RigError):
                self.fail(first['token'], observation)
        self.assertEqual(injected, [True])
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.row()['held']['amount'], '6')
        self.assertEqual(fixture.hatch.job_for(fixture.hatch.manifest(self.root), 'base')['token'], first['token'])
        self.generic('reconcile-raced-generic-unknown', 'retry', amount='0.5')
        self.fail(first['token'], observation)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertNotEqual(self.claim()['token'], first['token'])

    def test_cancellation_cannot_release_a_published_unmeasured_attempt(self):
        first = self.claim()
        self.assert_refused_unchanged(self.cancel)
        self.fail(first['token'], self.observe('known-native', 'retry', amount='0.25'))
        result = self.cancel()
        self.assertEqual(result['state'], 'released')
        self.assertEqual(result['spent']['amount'], '0.25')

    def test_old_measured_attempt_does_not_authorize_cancellation_of_new_publication(self):
        first = self.claim()
        self.fail(first['token'], self.observe('first-known', 'retry', amount='0.25'))
        second = self.claim()
        self.assertNotEqual(second['token'], first['token'])
        self.assert_refused_unchanged(self.cancel)
        self.fail(second['token'], self.observe('second-known', 'retry', amount='0.75'))
        self.assertEqual(self.cancel()['spent']['amount'], '1.00')

    def test_generic_known_usage_does_not_stand_in_for_current_native_token(self):
        first = self.claim()
        self.generic('known-generic-delta', 'retry', calls=1, amount='0.4')
        self.assert_refused_unchanged(self.cancel)
        self.fail(first['token'], self.observe('native-token-reconciled', 'retry', calls=0, amount='0'))
        self.assertEqual(self.cancel()['spent']['amount'], '0.4')

    def test_generic_known_reconciliation_does_not_erase_native_pending_observations(self):
        first = self.claim()
        self.fail(first['token'], self.observe('native-unknown', 'uncertain', amount=None))
        self.generic('generic-known', 'retry', amount='0.3')
        self.assert_refused_unchanged(self.cancel)
        self.fail(first['token'], self.observe('native-known', 'retry', calls=0, amount='0',
                                             resolves=['native-unknown']))
        self.assertEqual(self.cancel()['spent']['amount'], '0.3')


class BudgetPromptParentTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    production_fixture = edges.BudgetPublicationEdgeTests.production_fixture

    def exchange_at_staging(self, prompt):
        parent, retained = prompt.parent, prompt.parent.with_name('retained-prompts')
        foreign = b'Unrelated replacement directory content must remain unchanged.\n'
        real_open = os.open
        events = []

        def stage_after_exchange(path, flags, *args, **kwargs):
            if not events and Path(path).name.startswith('.hatch-bytes-'):
                try:
                    parent.rename(retained)
                except PermissionError:
                    events.append('pinned-by-windows')
                else:
                    events.append('parent-replaced')
                    parent.mkdir()
                    prompt.write_bytes(foreign)
            return real_open(path, flags, *args, **kwargs)

        @contextmanager
        def exchange():
            # The timing hook delegates every open to the real native owner.
            # Retain that owner's dir_fd capability while its identity is wrapped.
            supported = os.supports_dir_fd | ({stage_after_exchange} if real_open in os.supports_dir_fd else set())
            with patch.object(os, 'supports_dir_fd', supported), patch.object(os, 'open', stage_after_exchange):
                yield

        return exchange(), events, retained, foreign

    def test_atomic_prompt_write_does_not_modify_a_replaced_parent(self):
        prompt = self.root / 'prompts/base-pet.md'
        context, events, retained, foreign = self.exchange_at_staging(prompt)
        error = None
        with context:
            try:
                fixture.hatch.atomic_bytes(prompt, b'Owned replacement bytes.\n')
            except (ValueError, OSError):
                error = True
        self.assertEqual(len(events), 1, 'The actual staging-open boundary was not reached')
        if events == ['parent-replaced']:
            self.assertEqual(prompt.read_bytes(), foreign, 'Publication followed a replacement parent')
            self.assertEqual((retained / prompt.name).read_bytes(), b'Owned replacement bytes.\n')
            self.assertEqual(sorted(path.name for path in prompt.parent.iterdir()), [prompt.name])
        else:
            self.assertEqual(os.name, 'nt')
            self.assertIsNone(error)
            self.assertEqual(prompt.read_bytes(), b'Owned replacement bytes.\n')

    def test_production_augmentation_does_not_admit_a_replaced_prompt_parent(self):
        project = self.production_fixture()
        prompt = self.root / 'prompts/base-pet.md'
        context, events, retained, foreign = self.exchange_at_staging(prompt)
        ledger = self.ledger.snapshot()
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        handoff, error = None, None
        with context:
            try:
                handoff = fixture.workflow.claim_generation(project, 'prompt', 'base')
            except (ValueError, OSError):
                error = True
        self.assertEqual(len(events), 1, 'The real production staging-open boundary was not reached')
        if events == ['parent-replaced']:
            self.assertEqual(prompt.read_bytes(), foreign, 'Production wrote into an unrelated directory')
            self.assertIn(b'Production coverage binding SHA-256:', (retained / prompt.name).read_bytes())
            self.assertIsNone(handoff, 'A replacement prompt directory received claim authority')
            self.assertTrue(error)
            self.assertEqual(self.ledger.snapshot(), ledger)
            self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)
        else:
            self.assertEqual(os.name, 'nt')
            self.assertIsNone(error)
            self.assertIn('token', handoff)


if __name__ == '__main__':
    unittest.main()
