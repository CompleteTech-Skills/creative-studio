"""Independent offline regressions for retained admission and publication edges.

All reservations, journals and path substitutions are synthetic local fixtures.
Timing hooks invoke the real native admission/preflight and local ledger APIs;
these tests never call an image provider or claim production execution evidence.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

import test_director_budget_native as fixture
from asset_contracts import generation_projection
from production_fixture import build_fixture_project


class SyntheticAdmissionStop(RuntimeError):
    pass


class BudgetPublicationEdgeTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    row = fixture.NativeBudgetTests.row

    def job(self, root=None):
        return fixture.hatch.job_for(fixture.hatch.manifest(root or self.root), 'base')

    def attempt(self, action):
        try:
            return action(), None
        except (ValueError, fixture.RigError) as error:
            return None, error

    def must_succeed(self, action):
        try:
            return action()
        except (ValueError, fixture.RigError, IndexError) as error:
            self.fail(f'Legitimate public operation was refused: {type(error).__name__}: {error}')

    def assert_unpublished(self, token=None):
        job = self.job()
        self.assertEqual(job['status'], 'pending')
        self.assertEqual(job['attempts'], 0)
        self.assertNotIn('token', job)
        self.assertFalse(any(entry.get('event') == 'claim' for entry in job['history']))
        if token is not None:
            self.assertEqual(job['budget_operation']['attempts'], [token])
        self.assertFalse(any(event['action'] == 'native-claim'
                             for event in self.ledger.snapshot()['state']['events']))

    def interrupt_admission(self):
        original = fixture.NativeBudget.before_claim
        admitted = []

        def stop_after_real_admission(budget, *args, **kwargs):
            token = original(budget, *args, **kwargs)
            admitted.append(token)
            raise SyntheticAdmissionStop('Synthetic interruption before publication')

        with patch.object(fixture.NativeBudget, 'before_claim', stop_after_real_admission):
            with self.assertRaises(SyntheticAdmissionStop):
                self.claim()
        self.assertEqual(len(admitted), 1)
        self.assert_unpublished(admitted[0])
        return admitted[0]

    def settings_file(self):
        path = self.root / 'extraction-settings.json'
        fixture.hatch.atomic(path, fixture.hatch.read(self.root / 'pet_request.json')['extraction'])
        return path, path.read_bytes()

    def mutate_settings_before_admission(self):
        path, original_bytes = self.settings_file()
        original = fixture.NativeBudget.before_claim
        calls, admitted = [], []

        def mutate_before_real_admission(budget, *args, **kwargs):
            calls.append(True)
            path.write_bytes(original_bytes + b'\n')
            token = original(budget, *args, **kwargs)
            admitted.append(token)
            return token

        with patch.object(fixture.NativeBudget, 'before_claim', mutate_before_real_admission):
            handoff, error = self.attempt(self.claim)
        self.assertEqual(calls, [True], 'Hatch did not reach real admission after capturing settings')
        self.assertIsNotNone(error, 'Changed settings returned a published handoff')
        self.assertIsNone(handoff)
        self.assert_unpublished(admitted[0] if admitted else None)
        return path, original_bytes, admitted

    def profile_without_generation_change(self):
        path = self.root / 'profile.json'
        original_bytes = path.read_bytes()
        before = fixture.hatch.read(path)
        changed = deepcopy(before)
        changed['export']['formats'].remove('gif')
        self.assertEqual(generation_projection(before, 'base'), generation_projection(changed, 'base'))
        warnings = fixture.hatch.style_warnings(self.root)
        fixture.hatch.atomic(path, changed)
        self.assertNotEqual(fixture.hatch.style_warnings(self.root), warnings,
                            'The profile mutation must change real published style warnings')
        return path, original_bytes, warnings

    def test_generic_unstarted_native_release_remains_readable_and_isolates_sibling(self):
        sibling = self.prepare('sibling')
        self.bind(sibling)
        token = self.interrupt_admission()
        identifier = self.row()['id']
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        receipt = self.ledger.release(identifier, outcome='unstarted',
            expected_revision=self.ledger.snapshot()['sha256'],
            idempotency_key='generic-unpublished-release', evidence=self.proof)
        self.assertEqual(receipt['state'], 'released')
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)
        released = self.ledger.snapshot()
        with self.subTest(boundary='released-native-status'):
            status = self.must_succeed(lambda: fixture.NativeBudget.for_run(
                self.root, fixture.hatch.manifest(self.root)).status())
            self.assertEqual(status['reservations'][identifier]['state'], 'released')
        with self.subTest(boundary='released-job-cannot-claim'):
            handoff, error = self.attempt(self.claim)
            self.assertIsNotNone(error)
            self.assertIsNone(handoff)
            self.assertEqual(self.ledger.snapshot(), released)
            self.assert_unpublished(token)
        with self.subTest(boundary='disjoint-sibling-claim'):
            handoff = self.must_succeed(lambda: self.claim(sibling))
            self.assertEqual(self.job(sibling)['token'], handoff['token'])
            self.assertNotEqual(handoff['budget_reservation_id'], identifier)
            self.assertEqual(self.ledger.snapshot()['state']['reservations'][identifier],
                             released['state']['reservations'][identifier])

    def test_native_shaped_generic_zero_reservations_remain_generic(self):
        for index, key_style in enumerate(('ordinary', 'native-looking'), start=1):
            operation = 'native-' + str(index) * 64
            key = 'generic-zero-reserve' if key_style == 'ordinary' else operation + ':reserve'
            receipt = self.ledger.reserve(operation_id=operation, provider='imagegen',
                estimate=fixture.quantities(), retry_allowance=0, evidence=[],
                expected_revision=self.ledger.snapshot()['sha256'], idempotency_key=key)
            identifier = receipt['reservation_id']
            self.assertTrue(all(value == '0' for value in receipt['reserved'].values()))
            with self.subTest(key_style=key_style, boundary='read-alongside-enrollment'):
                status = self.must_succeed(lambda: fixture.NativeBudget.for_run(
                    self.root, fixture.hatch.manifest(self.root)).status())
                self.assertEqual(status['reservations'][identifier]['operation_id'], operation)
                self.assert_unpublished()
            with self.subTest(key_style=key_style, boundary='generic-release'):
                release_key = 'generic-zero-release' if key_style == 'ordinary' else operation + ':release'
                released = self.must_succeed(lambda: self.ledger.release(identifier, outcome='unstarted',
                    expected_revision=self.ledger.snapshot()['sha256'],
                    idempotency_key=release_key, evidence=self.proof))
                self.assertEqual(released['state'], 'released')

    def test_ordinary_generic_zero_reservation_read_release_and_native_claim_control(self):
        receipt = self.ledger.reserve(operation_id='ordinary-generic-zero', provider='imagegen',
            estimate=fixture.quantities(), retry_allowance=0, evidence=[],
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key='ordinary-generic-reserve')
        identifier = receipt['reservation_id']
        status = fixture.NativeBudget.for_run(self.root, fixture.hatch.manifest(self.root)).status()
        self.assertEqual(status['reservations'][identifier]['state'], 'reserved')
        released = self.ledger.release(identifier, outcome='unstarted', evidence=self.proof,
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key='ordinary-generic-release')
        self.assertEqual(released['state'], 'released')
        handoff = self.claim()
        self.assertEqual(self.job()['token'], handoff['token'])
        self.assertNotEqual(handoff['budget_reservation_id'], identifier)

    def production_fixture(self):
        project = self.owner / 'project'
        build_fixture_project(project)
        value = fixture.hatch.read(project / 'project.json')
        value['limits']['max_generation_calls'] = 3
        fixture.hatch.atomic(project / 'project.json', value)
        self.ledger = fixture.BudgetLedger.create(project / 'ledger',
            fixture.quantities(provider_calls=6, amount=12),
            provider_limits={'imagegen': fixture.quantities(provider_calls=6, amount=12)}, currency='USD')
        self.proof = [self.ledger.retain_evidence(self.owner / 'evidence.json')]
        self.root = self.prepare('project/acquisition/prompt')
        self.bind(self.root, owner_root=project)
        fixture.workflow.bind_generation_run(project, 'prompt', self.root, ['synthetic raster requirement'])
        return project

    def test_production_prompt_append_refuses_unrelated_file_inside_same_run(self):
        project = self.production_fixture()
        target = self.root / 'unrelated-review-notes.md'
        target.write_text('Unrelated local notes must remain byte-for-byte intact.\n', encoding='utf-8')
        original = target.read_bytes()
        prompt = self.root / 'prompts/base-pet.md'
        prompt.unlink()
        if os.name == 'nt':
            prompt.mkdir()  # Exercise a real nonregular input without a platform skip.
        else:
            prompt.symlink_to(target)
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        workflow = (project / fixture.workflow.WORKFLOW_FILE).read_bytes()
        ledger = self.ledger.snapshot()
        handoff, error = self.attempt(lambda: fixture.workflow.claim_generation(project, 'prompt', 'base'))
        self.assertIsNone(handoff)
        self.assertIsNotNone(error)
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)
        self.assertEqual((project / fixture.workflow.WORKFLOW_FILE).read_bytes(), workflow)
        self.assertEqual(self.ledger.snapshot(), ledger)
        self.assertEqual(target.read_bytes(), original, 'Production augmentation followed the prompt alias')

    def test_regular_production_prompt_augmentation_control(self):
        project = self.production_fixture()
        handoff = fixture.workflow.claim_generation(project, 'prompt', 'base')
        self.assertEqual(self.job()['token'], handoff['token'])
        snapshot = self.job()['history'][-1]['prompt_snapshot']['utf8']
        self.assertIn('Production coverage binding SHA-256:', snapshot)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_existing_pending_settings_identity_rejects_changed_retry_control(self):
        self.settings_file()
        token = self.interrupt_admission()
        path, original_bytes, admitted = self.mutate_settings_before_admission()
        before = self.ledger.snapshot()
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        handoff, error = self.attempt(self.claim)
        self.assertIsNotNone(error, 'Retry bound the replacement settings to an unpublished operation')
        self.assertIsNone(handoff)
        self.assertEqual(path.read_bytes(), original_bytes + b'\n')
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assert_unpublished(token)
        path.write_bytes(original_bytes)
        self.assertEqual(self.claim()['token'], token)

    def test_original_captured_settings_restore_safe_claim_after_admission_race(self):
        path, original_bytes, admitted = self.mutate_settings_before_admission()
        path.write_bytes(original_bytes)
        handoff = self.must_succeed(self.claim)
        if admitted:
            self.assertEqual(handoff['token'], admitted[0])
        self.assertEqual(self.job()['attempts'], 1)
        self.assertEqual(self.job()['token'], handoff['token'])
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_pending_profile_change_outside_generation_projection_cannot_change_handoff(self):
        token = self.interrupt_admission()
        path, original_bytes, warnings = self.profile_without_generation_change()
        before = self.ledger.snapshot()
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        handoff, error = self.attempt(self.claim)
        self.assertIsNotNone(error,
            'Same pending token published warnings from a different full profile snapshot')
        self.assertIsNone(handoff)
        self.assertNotEqual(path.read_bytes(), original_bytes)
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assert_unpublished(token)

    def test_restored_full_profile_reuses_pending_token_and_captured_fields(self):
        identity = fixture.hatch.read(self.root / 'pet_request.json')['identity']
        token = self.interrupt_admission()
        path, original_bytes, warnings = self.profile_without_generation_change()
        path.write_bytes(original_bytes)
        handoff = self.claim()
        self.assertEqual(handoff['token'], token)
        self.assertEqual(handoff['style_warnings'], warnings)
        self.assertEqual(handoff['identity'], identity)
        self.assertEqual(self.job()['attempts'], 1)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def preflight_mutation(self, field):
        if field == 'settings':
            path, original_bytes = self.settings_file()
        else:
            path = self.root / 'prompts/base-pet.md'
            original_bytes = path.read_bytes()
        original = fixture.NativeBudget.preflight_publication
        reached = []
        checkpoints = []

        def mutate_during_real_preflight(budget, job, token, *args, **kwargs):
            reached.append(token)
            checkpoints.append(self.ledger.snapshot())
            path.write_bytes(original_bytes + b'\n')
            return original(budget, job, token, *args, **kwargs)

        with patch.object(fixture.NativeBudget, 'preflight_publication', mutate_during_real_preflight):
            handoff, error = self.attempt(self.claim)
        self.assertEqual(len(reached), 1, 'Claim did not reach its real publication preflight')
        self.assertEqual(path.read_bytes(), original_bytes + b'\n')
        self.assert_unpublished(reached[0])
        self.assertIsNotNone(error, f'Preflight {field} mutation returned a handoff')
        self.assertIsNone(handoff)
        self.assertEqual(self.ledger.snapshot(), checkpoints[0])
        path.write_bytes(original_bytes)
        restored = self.claim()
        self.assertEqual(restored['token'], reached[0])
        self.assertEqual(self.job()['attempts'], 1)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_unmutated_real_preflight_publishes_captured_input_control(self):
        self.settings_file()
        prompt = (self.root / 'prompts/base-pet.md').read_bytes()
        original = fixture.NativeBudget.preflight_publication
        reached = []

        def real_preflight(budget, job, token, *args, **kwargs):
            reached.append(token)
            return original(budget, job, token, *args, **kwargs)

        with patch.object(fixture.NativeBudget, 'preflight_publication', real_preflight):
            handoff = self.claim()
        self.assertEqual(reached, [handoff['token']])
        job = self.job()
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['history'][-1]['prompt_snapshot']['utf8'], prompt.decode('utf-8'))
        self.assertEqual(handoff['receipt_template']['input_hashes']['prompts/base-pet.md'],
                         hashlib.sha256(prompt).hexdigest())
        publications = [event for event in self.ledger.snapshot()['state']['events']
                        if event['action'] == 'native-claim']
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]['payload']['token'], handoff['token'])

    def test_prompt_mutation_during_real_publication_preflight_retains_pending_token(self):
        self.preflight_mutation('prompt')

    def test_settings_mutation_during_real_publication_preflight_retains_pending_token(self):
        self.preflight_mutation('settings')


if __name__ == '__main__':
    unittest.main()
