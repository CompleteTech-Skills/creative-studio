"""Independent offline input, directory and interrupted-publication boundaries.

Every attachment, journal, owner, ledger and replacement directory is created by
TemporaryDirectory fixtures. Hooks schedule only actual local public owner APIs;
they never forge ledger events or invoke a provider. A refused ordinary directory
rename is a real OS protection, not a simulated race or a skipped assertion.
"""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import hatch
from director_budget import NativeBudget
from rig_errors import RigError
import test_director_budget_native as fixture
import test_hatch_direct_aliases as alias_fixture


class SyntheticPublicationInterruption(OSError):
    """Publication alone is unavailable after the real claim journal is saved."""


class BudgetAdmissionIdentityTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    observe = fixture.NativeBudgetTests.observe
    report_failure = fixture.NativeBudgetTests.fail
    row = fixture.NativeBudgetTests.row

    def job(self):
        return hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')

    def journal(self):
        return (self.root / 'imagegen-jobs.json').read_bytes()

    def attach(self, relative):
        attachment = self.root / relative
        attachment.parent.mkdir(parents=True, exist_ok=True)
        payload = b'Original synthetic attachment bytes; not a generation projection.\n'
        attachment.write_bytes(payload)
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.job_for(data, 'base')['input_images'].append(dict(path=relative, role='reference'))
            hatch.save(self.root, data)
        return attachment, payload

    def test_reserved_generation_profile_attachment_refuses_or_retains_distinct_real_digest(self):
        projection = hatch.inputs(self.root, self.job())['@generation-profile']
        attachment, payload = self.attach('@generation-profile')
        real_digest = hashlib.sha256(payload).hexdigest()
        self.assertNotEqual(real_digest, projection)
        before = self.ledger.snapshot(), self.journal(), (self.owner / 'director-budget-owner.json').read_bytes()
        try:
            handoff = self.claim()
        except (ValueError, OSError, RigError):
            self.assertEqual((self.ledger.snapshot(), self.journal(),
                              (self.owner / 'director-budget-owner.json').read_bytes()), before,
                             'Reserved-name refusal must precede any authority mutation')
        else:
            hashes = handoff['receipt_template']['input_hashes']
            self.assertIn(real_digest, hashes.values(),
                          'Legal real attachment disappeared behind the synthetic projection key')
            self.assertIn(projection, hashes.values(),
                          'The real attachment must not replace the synthetic projection identity')
            self.assertTrue(any(Path(item['path']) == attachment for item in handoff['input_images']))
        self.assertEqual(attachment.read_bytes(), payload)

    def attachment_control(self, relative):
        attachment, payload = self.attach(relative)
        handoff = self.claim()
        self.assertEqual(handoff['receipt_template']['input_hashes'][relative],
                         hashlib.sha256(payload).hexdigest())
        self.assertTrue(any(Path(item['path']) == attachment for item in handoff['input_images']))
        self.assertEqual(attachment.read_bytes(), payload)

    def test_ordinary_attachment_retains_actual_input_digest_control(self):
        self.attachment_control('references/ordinary.bin')

    def test_special_looking_subdirectory_attachment_is_not_reserved_control(self):
        self.attachment_control('@generation-profile/reference.bin')

    @contextmanager
    def substituted_directory(self, replacement, retained, events):
        """Exchange actual ordinary directories, preserving both for comparison."""
        original_identity = self.root.stat().st_dev, self.root.stat().st_ino
        replacement_identity = replacement.stat().st_dev, replacement.stat().st_ino
        self.assertNotEqual(original_identity, replacement_identity)
        self.assertFalse(replacement.is_symlink())
        try:
            self.root.rename(retained)
        except PermissionError:
            events.append('OS-refused-directory-rename')
            yield False
            return
        try:
            replacement.rename(self.root)
            self.assertFalse(self.root.is_symlink())
            self.assertEqual((self.root.stat().st_dev, self.root.stat().st_ino), replacement_identity)
            events.append('ordinary-directory-substitution')
            try:
                yield True
            finally:
                self.root.rename(replacement)
        finally:
            retained.rename(self.root)
        self.assertEqual((self.root.stat().st_dev, self.root.stat().st_ino), original_identity)

    @staticmethod
    def directory_bytes(root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob('*') if path.is_file()}

    def directory_substitution(self, boundary):
        replacement = self.owner / 'unrelated-ordinary-directory'
        replacement.mkdir()
        (replacement / 'imagegen-jobs.json').write_bytes(b'Unrelated synthetic journal; preserve these bytes.\n')
        (replacement / 'ordinary-sentinel.bin').write_bytes(b'Unrelated synthetic payload.\n')
        before = self.directory_bytes(replacement)
        original_inputs = {name: payload for name, payload in self.directory_bytes(self.root).items()
                           if name != 'imagegen-jobs.json'}
        retained = self.owner / 'retained-admitted-root'
        real_admission, real_save = NativeBudget.before_claim, hatch.save
        events = []

        def attempt_while_substituted(action):
            with self.substituted_directory(replacement, retained, events):
                return action()

        def substitute_running_save(root, data):
            if Path(root) == self.root and hatch.job_for(data, 'base')['status'] == 'running' and not events:
                return attempt_while_substituted(lambda: real_save(root, data))
            return real_save(root, data)

        handoff, refusal = None, None
        if boundary == 'after-admission':
            # Enter substitution when actual public admission has returned,
            # then restore it only when the whole claim completes or refuses.
            context = None
            def substitute_after_admission(budget, *args, **kwargs):
                nonlocal context
                token = real_admission(budget, *args, **kwargs)
                context = self.substituted_directory(replacement, retained, events)
                context.__enter__()
                return token
            try:
                with patch.object(NativeBudget, 'before_claim', substitute_after_admission):
                    try:
                        handoff = self.claim()
                    except (ValueError, OSError, RigError) as error:
                        refusal = error
            finally:
                if context is not None:
                    context.__exit__(None, None, None)
        else:
            with patch.object(hatch, 'save', substitute_running_save):
                try:
                    handoff = self.claim()
                except (ValueError, OSError, RigError) as error:
                    refusal = error
        self.assertEqual(len(events), 1, 'Real claim never reached the scheduled boundary')
        with self.subTest(boundary='replacement-directory-bytes', hook=boundary):
            self.assertEqual(self.directory_bytes(replacement), before,
                             'Admitted lifecycle write overwrote an unrelated ordinary directory')
        if events == ['ordinary-directory-substitution']:
            with self.subTest(boundary='no-handoff-for-substituted-root', hook=boundary):
                self.assertTrue(handoff is None, 'Directory substitution returned dispatch authority')
                self.assertIsNotNone(refusal)
        else:
            self.assertEqual(events, ['OS-refused-directory-rename'])
            self.assertIsNotNone(handoff, 'An OS-protected unchanged root should remain usable')
        self.assertEqual({name: payload for name, payload in self.directory_bytes(self.root).items()
                          if name != 'imagegen-jobs.json'}, original_inputs,
                         'Restoring the admitted root must preserve every original input byte')

    def test_ordinary_directory_substitution_after_real_admission_cannot_authorize_handoff(self):
        self.directory_substitution('after-admission')

    def test_ordinary_directory_substitution_at_running_save_cannot_overwrite_replacement(self):
        self.directory_substitution('running-save')

    def interrupted_publication(self):
        reached = []
        def stop_publication(budget, job, token):
            persisted = self.job()
            self.assertEqual(persisted['status'], 'running')
            self.assertEqual(persisted['token'], token)
            self.assertTrue(any(item.get('event') == 'claim' and item.get('token') == token
                                for item in persisted['history']))
            reached.append(token)
            raise SyntheticPublicationInterruption('Synthetic native-claim publication interruption only')
        with patch.object(NativeBudget, 'publish_claim', stop_publication):
            with self.assertRaises(SyntheticPublicationInterruption):
                self.claim()
        self.assertEqual(len(reached), 1)
        self.assertFalse(any(event['action'] == 'native-claim'
                             for event in self.ledger.snapshot()['state']['events']))
        return reached[0]

    def test_interrupted_first_publication_unknown_remains_authoritative_after_generic_retry_cancel(self):
        token = self.interrupted_publication()
        self.report_failure(token, self.observe('interrupted-native-unknown', 'uncertain', amount=None))
        original = deepcopy(self.row())
        self.assertEqual(original['state'], 'uncertain')
        self.assertEqual(original['held']['amount'], '6')
        journal = self.journal()
        try:
            self.ledger.settle(original['id'], outcome='retry', usage=fixture.quantities(amount='0.25'),
                evidence=self.proof, expected_revision=self.ledger.snapshot()['sha256'],
                idempotency_key='generic-retry-after-interrupted-native-unknown')
        except RigError:
            pass  # Safe refusal is permitted; it cannot erase native uncertainty.
        with self.subTest(boundary='held-funds-after-generic-retry'):
            self.assertGreater(Decimal(self.row()['held']['amount']), 0)
            self.assertEqual(Decimal(self.row()['held']['amount']) + Decimal(self.row()['spent']['amount']),
                             Decimal(original['reserved']['amount']))
            self.assertEqual(self.journal(), journal)
            self.assertEqual(self.job()['token'], token)
        before_cancel = self.ledger.snapshot()
        with self.subTest(boundary='generic-cancel-cannot-discard-native-unknown'):
            with self.assertRaises(RigError):
                self.ledger.release(original['id'], outcome='canceled-known', evidence=self.proof,
                    expected_revision=before_cancel['sha256'], idempotency_key='generic-cancel-native-unknown')
            self.assertEqual(self.ledger.snapshot(), before_cancel)
        with self.subTest(boundary='unknown-still-requires-token-resolution'):
            with self.assertRaises(RigError):
                self.report_failure(token, self.observe('unresolved-native-retry', 'retry', calls=0, amount='0'))
            self.assertGreater(Decimal(self.row()['held']['amount']), 0)
            self.assertEqual(self.journal(), journal)

    def test_interrupted_first_publication_genuine_token_usage_can_settle_control(self):
        token = self.interrupted_publication()
        self.report_failure(token, self.observe('interrupted-native-unknown', 'uncertain', amount=None))
        self.report_failure(token, self.observe('interrupted-native-resolved', 'failed', calls=0,
            amount='0.75', resolves=['interrupted-native-unknown']))
        self.assertEqual(self.row()['state'], 'settled')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '0.75')
        self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
        self.assertNotIn('token', self.job())

    def test_ordinary_claim_and_direct_save_remain_valid_control(self):
        data = hatch.manifest(self.root)
        event = dict(event='synthetic-direct-save-control')
        data['history'].append(event)
        hatch.save(self.root, data)
        self.assertEqual(hatch.manifest(self.root)['history'][-1], event)
        handoff = self.claim()
        self.assertEqual(self.job()['token'], handoff['token'])
        self.report_failure(handoff['token'], self.observe('ordinary-known', 'failed'))
        self.assertEqual(self.row()['state'], 'settled')


class InitialCallerAliasControlTests(unittest.TestCase):
    setUp = alias_fixture.HatchDirectAliasTests.setUp
    assert_alias = alias_fixture.HatchDirectAliasTests.assert_alias
    assert_journal = alias_fixture.HatchDirectAliasTests.assert_journal
    artifact_hashes = alias_fixture.HatchDirectAliasTests.artifact_hashes
    exercise_direct_save = alias_fixture.HatchDirectAliasTests.exercise_direct_save

    def test_initial_caller_root_alias_allows_direct_save_and_claim_control(self):
        self.exercise_direct_save(self.caller_root)
        with hatch.ownership(self.caller_root):
            handoff = hatch.claim(self.caller_root, hatch.manifest(self.caller_root), 'base')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], handoff['token'])
        self.assert_alias()


if __name__ == '__main__':
    unittest.main()
