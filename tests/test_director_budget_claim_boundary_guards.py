"""Independent offline guards for native accounting and captured claim inputs.

All bytes and namespace substitutions belong to TemporaryDirectory fixtures.
Hooks schedule real owner calls; they do not replace accounting or file reads.
If local symlinks are unavailable, explicitly labeled Path.resolve doubles model
only that alias lookup while directory renames and journal writes remain real.
No test is provider execution, production evidence, or an optional skip.
"""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import hatch
from director_budget import NativeBudget
from rig_errors import RigError
import test_director_budget_native as fixture
import test_director_budget_identity_recovery_edges as identity_fixture


class LegacySettingsReadReached(AssertionError):
    """An ordinary legacy settings read escaped the bounded captured lane."""


class BudgetClaimBoundaryGuards(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    observe = fixture.NativeBudgetTests.observe
    report_failure = fixture.NativeBudgetTests.fail
    row = fixture.NativeBudgetTests.row
    prepare_references = identity_fixture.BudgetIdentityRecoveryEdgeTests.prepare_references

    def job(self):
        return hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')

    def generic(self, outcome, key, *, amount='0.25'):
        return self.ledger.settle(self.row()['id'], outcome=outcome,
            usage=fixture.quantities(amount=amount), evidence=self.proof,
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key=key)

    def terminal_over_native_unknown(self, outcome):
        token = self.claim()['token']
        self.report_failure(token, self.observe('native-unknown', 'uncertain', amount=None))
        original = deepcopy(self.row())
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        # A refusal or a retained generic measured delta may be valid. Neither
        # can settle a native token whose unknown observation is unresolved.
        try:
            self.generic(outcome, 'generic-terminal')
        except RigError:
            pass
        generic_spent = Decimal(self.row()['spent']['amount'])
        with self.subTest(boundary='hold-and-native-token'):
            self.assertEqual(self.row()['state'], 'uncertain')
            self.assertGreater(Decimal(self.row()['held']['amount']), 0)
            self.assertEqual(Decimal(self.row()['held']['amount']) + Decimal(self.row()['spent']['amount']),
                             Decimal(original['reserved']['amount']))
            self.assertEqual(self.job()['token'], token)
            self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)
        with self.subTest(boundary='explicit-measured-token-reconciliation'):
            self.report_failure(token, self.observe('native-known', 'failed', calls=0,
                amount='0.75', resolves=['native-unknown']))
            self.assertEqual(self.row()['state'], 'settled')
            self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
            self.assertEqual(self.row()['spent']['provider_calls'], '1')
            self.assertEqual(Decimal(self.row()['spent']['amount']), generic_spent + Decimal('0.75'))
            self.assertNotIn('token', self.job())

    def test_generic_complete_cannot_discard_authenticated_native_uncertainty(self):
        self.terminal_over_native_unknown('complete')

    def test_generic_failed_cannot_discard_authenticated_native_uncertainty(self):
        self.terminal_over_native_unknown('failed')

    def test_generic_retry_preserves_native_unknown_until_token_resolution_control(self):
        token = self.claim()['token']
        self.report_failure(token, self.observe('native-unknown', 'uncertain', amount=None))
        held = deepcopy(self.row()['held'])
        self.generic('retry', 'generic-measured-retry')
        self.assertIn(self.row()['state'], ('reserved', 'uncertain'))
        self.assertEqual(Decimal(self.row()['held']['amount']) + Decimal(self.row()['spent']['amount']),
                         Decimal(held['amount']))
        self.assertEqual(self.job()['token'], token)
        with self.assertRaises(RigError):
            self.ledger.release(self.row()['id'], outcome='canceled-known', evidence=self.proof,
                expected_revision=self.ledger.snapshot()['sha256'], idempotency_key='unknown-cancel')
        with self.assertRaises(RigError):
            self.report_failure(token, self.observe('missing-resolution', 'retry', calls=0, amount='0'))
        self.report_failure(token, self.observe('native-known', 'retry', calls=0,
            amount='0', resolves=['native-unknown']))
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertNotEqual(self.claim()['token'], token)

    def test_generic_only_uncertainty_still_terminal_settles_control(self):
        for outcome in ('complete', 'failed'):
            with self.subTest(outcome=outcome):
                receipt = self.ledger.reserve(operation_id='generic-' + outcome,
                    provider='imagegen', estimate=fixture.quantities(provider_calls=1, amount=2),
                    retry_allowance=0, evidence=self.proof,
                    expected_revision=self.ledger.snapshot()['sha256'],
                    idempotency_key='generic-reserve-' + outcome)
                identifier = receipt['reservation_id']
                self.ledger.settle(identifier, outcome='uncertain',
                    usage=fixture.quantities(provider_calls=1, amount=None), evidence=self.proof,
                    expected_revision=self.ledger.snapshot()['sha256'],
                    idempotency_key='generic-unknown-' + outcome)
                final = self.ledger.settle(identifier, outcome=outcome,
                    usage=fixture.quantities(amount='0.5'), evidence=self.proof,
                    expected_revision=self.ledger.snapshot()['sha256'],
                    idempotency_key='generic-known-' + outcome)
                self.assertEqual(final['state'], 'settled')
                self.assertEqual(final['spent']['provider_calls'], '1')
                self.assertEqual(final['spent']['amount'], '0.5')
                self.assertTrue(all(value == '0' for value in final['held'].values()))

    @contextmanager
    def local_alias(self, path, target, retained, events):
        """Swap real local names; identify an injected fallback explicitly."""
        try:
            path.rename(retained)
        except PermissionError:
            # Native directory pins may prevent the namespace substitution.
            # This is an observed OS refusal, not a skipped alias assertion.
            events.append('OS-refused-local-rename')
            yield
            return
        try:
            try:
                path.symlink_to(target, target_is_directory=target.is_dir())
            except (OSError, NotImplementedError):
                # Symlink permission is optional on Windows. The fallback is a
                # path-method double, not evidence of an actual OS symlink race.
                events.append('injected-Path.resolve-alias')
                real_resolve = Path.resolve

                def alias_resolve(candidate, *args, **kwargs):
                    candidate = Path(candidate)
                    if candidate == path or path in candidate.parents:
                        return real_resolve(target / candidate.relative_to(path), *args, **kwargs)
                    return real_resolve(candidate, *args, **kwargs)

                with patch.object(Path, 'resolve', alias_resolve):
                    yield
            else:
                events.append('real-local-symlink')
                try:
                    yield
                finally:
                    path.unlink()
        finally:
            retained.rename(path)

    def assert_handoff_attachment_identity(self, handoff, descriptors, originals):
        self.assertEqual(handoff['input_images'],
            [{**item, 'path': str(self.root / item['path'])} for item in descriptors])
        hashes = handoff['receipt_template']['input_hashes']
        for item in handoff['input_images']:
            relative = Path(item['path']).relative_to(self.root).as_posix()
            self.assertEqual(Path(item['path']).read_bytes(), originals[relative])
            self.assertEqual(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest(), hashes[relative])

    def test_transient_attachment_alias_cannot_return_another_ordinary_file(self):
        self.prepare_references()
        descriptors = deepcopy(self.job()['input_images'])
        originals = {item['path']: (self.root / item['path']).read_bytes() for item in descriptors}
        attachment = self.root / descriptors[0]['path']
        other = self.root / 'ordinary-unadmitted.png'
        other.write_bytes(b'Unrelated ordinary local file, never admitted as an attachment.\n')
        retained = attachment.with_name('retained-original.png')
        real_inside, real_warnings = hatch.inside, hatch.style_warnings
        ready, events = [], []

        def after_real_warnings(*args, **kwargs):
            value = real_warnings(*args, **kwargs)
            ready.append(True)  # Scheduling hook: initial input hashing is done.
            return value

        def transient_inside(root, relative):
            if ready and not events and Path(root) == self.root and relative == descriptors[0]['path']:
                with self.local_alias(attachment, other, retained, events):
                    return real_inside(root, relative)
            return real_inside(root, relative)

        handoff, error = None, None
        with patch.object(hatch, 'style_warnings', after_real_warnings), \
                patch.object(hatch, 'inside', transient_inside):
            try:
                handoff = self.claim()
            except (ValueError, OSError, RigError) as exc:
                error = exc
        self.assertEqual(len(events), 1, 'No transient resolution boundary was exercised')
        self.assertEqual(attachment.read_bytes(), originals[descriptors[0]['path']])
        self.assertEqual(other.read_bytes(), b'Unrelated ordinary local file, never admitted as an attachment.\n')
        if handoff is not None:
            with self.subTest(alias_backend=events[0]):
                self.assert_handoff_attachment_identity(handoff, descriptors, originals)
        else:
            self.assertIsNotNone(error)
            if self.job()['status'] == 'running':
                self.report_failure(self.job()['token'], self.observe('recovered-attachment', 'failed'))
                self.assertEqual(self.row()['state'], 'settled')
            else:
                self.assertEqual(self.claim()['attempt'], 1, 'Restored original inputs must remain claimable')

    def test_regular_attachment_paths_identify_admitted_bytes_control(self):
        self.prepare_references()
        descriptors = deepcopy(self.job()['input_images'])
        originals = {item['path']: (self.root / item['path']).read_bytes() for item in descriptors}
        self.assert_handoff_attachment_identity(self.claim(), descriptors, originals)

    def test_run_root_alias_at_journal_save_cannot_redirect_publication(self):
        alternate = self.owner / 'unrelated-directory'
        alternate.mkdir()
        sentinel = alternate / 'imagegen-jobs.json'
        sentinel.write_bytes(b'Ordinary unrelated file must survive exactly.\n')
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in alternate.iterdir()}
        retained = self.owner / 'retained-original-run'
        real_save, events = hatch.save, []

        def alias_at_running_save(root, data):
            job = hatch.job_for(data, 'base')
            if Path(root) == self.root and job['status'] == 'running' and not events:
                with self.local_alias(self.root, alternate, retained, events):
                    return real_save(root, data)
            return real_save(root, data)

        handoff, error = None, None
        with patch.object(hatch, 'save', alias_at_running_save):
            try:
                handoff = self.claim()
            except (ValueError, OSError, RigError) as exc:
                error = exc
        self.assertEqual(len(events), 1, 'No running-claim journal save was exercised')
        with self.subTest(boundary='alternate-directory-untouched', alias_backend=events[0]):
            self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in alternate.iterdir()}, before)
        with self.subTest(boundary='original-journal-recovery', alias_backend=events[0]):
            with hatch.ownership(self.root):
                data = hatch.manifest(self.root, recovery=True)
                budget = NativeBudget.for_run(self.root, data, recovery=True)
                self.assertIn(self.row()['id'], budget.status()['reservations'])
            if handoff is not None:
                self.assertEqual(self.job()['token'], handoff['token'])
            else:
                self.assertIsNotNone(error)
            if self.job()['status'] == 'running':
                self.report_failure(self.job()['token'], self.observe('recovered-known', 'failed'))
                self.assertEqual(self.row()['state'], 'settled')
            else:
                self.assertEqual(self.claim()['attempt'], 1)

    def test_ordinary_journal_save_retains_recoverable_published_token_control(self):
        handoff = self.claim()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root, recovery=True)
            status = NativeBudget.for_run(self.root, data, recovery=True).status()
        self.assertIn(handoff['budget_reservation_id'], status['reservations'])
        self.assertEqual(self.job()['token'], handoff['token'])
        self.report_failure(handoff['token'], self.observe('control-known', 'failed'))
        self.assertEqual(self.row()['state'], 'settled')

    def settings_attempt(self, payload):
        path = self.root / 'extraction-settings.json'
        if payload is not None:
            path.write_bytes(payload)
        before = self.ledger.snapshot(), (self.root / 'imagegen-jobs.json').read_bytes()
        legacy, admitted = [], []
        real_read, real_admission = hatch.read, NativeBudget.before_claim

        def guard_legacy_reader(candidate):
            if Path(candidate) == path:
                legacy.append(True)
                raise LegacySettingsReadReached('Captured settings reached the ordinary legacy JSON reader')
            return real_read(candidate)

        def note_admission(*args, **kwargs):
            admitted.append(True)
            return real_admission(*args, **kwargs)

        handoff, error = None, None
        with patch.object(hatch, 'read', guard_legacy_reader), \
                patch.object(NativeBudget, 'before_claim', note_admission):
            try:
                handoff = self.claim()
            except Exception as exc:
                error = exc
        return handoff, error, legacy, admitted, before

    def reject_settings(self, payload):
        handoff, error, legacy, admitted, before = self.settings_attempt(payload)
        self.assertEqual(legacy, [], 'Malformed captured settings triggered an unbounded legacy reread')
        self.assertIsNone(handoff)
        self.assertIsNotNone(error, 'Nonobject settings must reject before admission')
        self.assertEqual(admitted, [], 'Invalid settings reached accounting admission')
        self.assertEqual((self.ledger.snapshot(), (self.root / 'imagegen-jobs.json').read_bytes()), before)

    def test_captured_null_settings_reject_before_legacy_reread_or_admission(self):
        self.reject_settings(b'null\n')

    def test_captured_nonobject_settings_reject_before_admission(self):
        # Some published versions reject these through AttributeError rather
        # than a typed native diagnostic. That still proves early refusal; it
        # is not evidence of the null-only fallback vulnerability.
        for payload in (b'[]\n', b'"text"\n', b'0\n', b'false\n'):
            with self.subTest(payload=payload):
                self.reject_settings(payload)

    def test_absent_settings_use_captured_request_extraction_control(self):
        self.assertFalse((self.root / 'extraction-settings.json').exists())
        handoff, error, legacy, admitted, _ = self.settings_attempt(None)
        self.assertIsNone(error)
        self.assertEqual(legacy, [])
        self.assertEqual(admitted, [True])
        self.assertEqual(handoff['tool_arguments']['transparent_background'],
                         hatch.read(self.root / 'pet_request.json')['extraction']['background'] != 'chroma')

    def test_valid_captured_settings_avoid_legacy_reader_control(self):
        settings = hatch.read(self.root / 'pet_request.json')['extraction']
        handoff, error, legacy, admitted, _ = self.settings_attempt(json.dumps(settings).encode('utf-8'))
        self.assertIsNone(error)
        self.assertEqual(legacy, [])
        self.assertEqual(admitted, [True])
        self.assertIn('token', handoff)


if __name__ == '__main__':
    unittest.main()
