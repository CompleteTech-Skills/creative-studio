"""Independent offline regressions for generic/native accounting interoperability.

The legacy fixture below transcribes the public reserve/claim/fail/claim journal
format at 165d57101e667c58a5af217d306422ecd32d87e6. It deliberately has no
publication protocol or native-claim ledger events. No provider is invoked.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

import test_director_budget_native as fixture

hatch = fixture.hatch
BudgetLedger = fixture.BudgetLedger
NativeBudget = fixture.NativeBudget
RigError = fixture.RigError
quantities = fixture.quantities


class BudgetInteropAuthorityTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    row = fixture.NativeBudgetTests.row
    observe = fixture.NativeBudgetTests.observe
    fail = fixture.NativeBudgetTests.fail

    def authority(self):
        return (self.ledger.snapshot(), (self.root / 'imagegen-jobs.json').read_bytes(),
                (self.owner / 'director-budget-owner.json').read_bytes())

    def source_bytes(self):
        paths = ['pet_request.json', 'profile.json', 'prompts/base-pet.md']
        return {path: (self.root / path).read_bytes() for path in paths}

    def retain_json(self, value):
        path = self.owner / 'synthetic-accounting-proof.json'
        path.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding='utf-8')
        return self.ledger.retain_evidence(path)

    def generic_reserve(self, operation='ordinary-offline-operation', evidence=None, key=None):
        return self.ledger.reserve(operation_id=operation, provider='imagegen',
            estimate=quantities(provider_calls=1, amount=1), retry_allowance=0,
            expected_revision=self.ledger.snapshot()['sha256'],
            idempotency_key=key or operation + ':reserve', evidence=evidence or self.proof)

    def generic_settle(self, identifier, outcome, key, *, usage=None):
        return self.ledger.settle(identifier, usage=quantities() if usage is None else usage, outcome=outcome,
            expected_revision=self.ledger.snapshot()['sha256'],
            idempotency_key=key, evidence=self.proof)

    def assert_generic_cannot_finalize_native_unknown(self, outcome):
        handoff = self.claim()
        self.fail(handoff['token'], self.observe('native-disconnected', 'uncertain', amount=None))
        before = self.authority()
        sources = self.source_bytes()
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.row()['held']['amount'], '6')
        with self.assertRaises((ValueError, RigError)):
            self.generic_settle(handoff['budget_reservation_id'], outcome,
                                'generic-cannot-clear-native-' + outcome)
        self.assertEqual(self.authority(), before,
                         'Refused generic settlement changed ledger or native authority')
        self.assertEqual(self.source_bytes(), sources)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], handoff['token'])
        # Actual usage is a delta; the previously observed call must not be lost
        # or charged twice when the original native token resolves its unknown.
        resolved = self.observe('native-actual-delta', 'failed', calls=0, amount='0.75',
                                resolves=['native-disconnected'])
        self.fail(handoff['token'], resolved)
        self.assertEqual(self.row()['state'], 'settled')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '0.75')
        after = self.authority()
        # Finalization consumes the active native token. Accounting replay uses
        # the public usage endpoint; it must return the original receipt without
        # another native transition or another charge.
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            job = hatch.job_for(data, 'base')
            self.assertEqual(job['status'], 'failed')
            self.assertNotIn('token', job)
            expected_receipt = job['budget_operation']['events'][-1]['receipt']
            replayed = hatch.record_budget_usage(self.root, data, 'base', handoff['token'], resolved)
            self.assertEqual(replayed, expected_receipt)
        with self.assertRaisesRegex(ValueError, 'active token required'):
            self.fail(handoff['token'], resolved)
        self.assertEqual(self.authority(), after)

    def test_generic_complete_cannot_erase_token_bound_native_unknown(self):
        self.assert_generic_cannot_finalize_native_unknown('complete')

    def test_generic_failed_cannot_erase_token_bound_native_unknown(self):
        self.assert_generic_cannot_finalize_native_unknown('failed')

    def test_ordinary_generic_terminal_settlement_remains_valid(self):
        sources = self.source_bytes()
        journal = (self.root / 'imagegen-jobs.json').read_bytes()
        for outcome in ('complete', 'failed'):
            with self.subTest(outcome=outcome):
                reserved = self.generic_reserve('ordinary-' + outcome)
                self.generic_settle(reserved['reservation_id'], 'uncertain', 'unknown-' + outcome,
                                    usage=quantities(provider_calls=1, amount=None))
                receipt = self.generic_settle(reserved['reservation_id'], outcome, 'finish-' + outcome,
                                              usage=quantities(amount='0.25'))
                row = self.ledger.snapshot()['state']['reservations'][reserved['reservation_id']]
                self.assertEqual(receipt['state'], 'settled')
                self.assertEqual(row['spent']['provider_calls'], '1')
                self.assertEqual(row['spent']['amount'], '0.25')
                self.assertTrue(all(value == '0' for value in row['held'].values()))
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), journal)
        self.assertEqual(self.source_bytes(), sources)

    def legacy_retry_fixture(self, *, second_running):
        """Reconstitute main165's authentic old wire format using public ledger APIs.

        The policy and original input hashes come from a fresh public enrollment.
        Both claim records, the failed transition, and the token-bound measured
        retry match main165 public Hatch output; no modern event is removed or
        rewritten. Only the first token has a usage observation in the ledger.
        """
        data = hatch.manifest(self.root)
        job = hatch.job_for(data, 'base')
        policy = deepcopy(data['director_budget'])
        hashes = hatch.inputs(self.root, job)
        operation_id = 'native-' + hatch.object_hash(dict(run_id=data['run_id'], job='base',
            inputs=hashes, fixture=data['fixture_mode'], policy=policy))
        intent = dict(kind='native-budget-intent', run_id=data['run_id'], job='base',
            fixture=data['fixture_mode'], operation_id=operation_id,
            input_hashes=hashes, policy=policy)
        request = {key: deepcopy(policy[key]) for key in ('provider', 'estimate', 'retry_allowance',
                   'unknown_price', 'unknown_price_cap')}
        request.update(operation_id=operation_id, idempotency_key=operation_id + ':reserve',
                       evidence=self.proof + [self.retain_json(intent)])
        reservation = self.ledger.reserve(**request, expected_revision=self.ledger.snapshot()['sha256'])
        first, second = '1' * 32, '2' * 32
        payload = dict(token=first, **self.observe('legacy-measured-retry', 'retry', amount='0.25'))
        payload['usage'] = {key: str(value) for key, value in payload['usage'].items()}
        key = 'native-observation-' + hatch.object_hash([operation_id, payload['id']])
        retained = self.proof + [self.retain_json(dict(kind='native-budget-usage',
            fixture=data['fixture_mode'], operation_id=operation_id, **payload))]
        receipt = self.ledger.settle(reservation['reservation_id'], usage=payload['usage'],
            outcome='retry', evidence=retained, idempotency_key=key,
            expected_revision=self.ledger.snapshot()['sha256'])
        operation = dict(operation_id=operation_id, input_hashes=hashes,
            reservation_request=request, reservation_id=reservation['reservation_id'],
            attempts=[first, second] if second_running else [first],
            events=[dict(key=key, payload=payload, evidence=retained, receipt=receipt)])
        request_value = hatch.read(self.root / 'pet_request.json')
        prompt_bytes = (self.root / job['prompt_file']).read_bytes()

        def historical_claim(token, timestamp):
            return dict(event='claim', time=timestamp, token=token, input_hashes=hashes,
                prompt_snapshot=dict(path=job['prompt_file'],
                    sha256=hashlib.sha256(prompt_bytes).hexdigest(), utf8=prompt_bytes.decode('utf-8')),
                input_images=deepcopy(job['input_images']), identity=request_value.get('identity', {}),
                resolved_style=request_value.get('resolved_style'))

        job['history'] += [historical_claim(first, 1.0),
            dict(event='failed', time=2.0, token=first, reason='Synthetic provider result')]
        if second_running:
            job['history'].append(historical_claim(second, 3.0))
        job.update(budget_operation=operation, input_hashes=hashes,
                   status='running' if second_running else 'failed',
                   attempts=2 if second_running else 1, started_at=3.0 if second_running else 1.0)
        if second_running:
            job.update(token=second, started_at=3.0)
        hatch.save(self.root, data)
        # Validate this as a real recoverable legacy owner before exercising the
        # generic API, so a malformed fixture cannot masquerade as a refusal.
        with hatch.ownership(self.root):
            current = hatch.manifest(self.root)
            NativeBudget.for_run(self.root, current, recovery=True).reconcile()
        events = self.ledger.snapshot()['state']['events']
        self.assertFalse(any(event['action'] == 'native-claim' for event in events))
        self.assertNotIn('publication_protocol', intent)
        return reservation['reservation_id'], first, second

    def test_legacy_measured_retry_does_not_prove_latest_running_token_is_known(self):
        identifier, first, second = self.legacy_retry_fixture(second_running=True)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['token'], second)
        self.assertEqual(job['budget_operation']['events'][-1]['payload']['token'], first)
        before, sources = self.authority(), self.source_bytes()
        with self.assertRaises((ValueError, RigError)):
            self.ledger.release(identifier, outcome='canceled-known',
                expected_revision=self.ledger.snapshot()['sha256'],
                idempotency_key='legacy-newer-running-refusal', evidence=self.proof)
        self.assertEqual(self.authority(), before)
        self.assertEqual(self.source_bytes(), sources)
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        self.assertEqual(self.row()['held']['amount'], '5.75')

    def test_legacy_unknown_publication_can_still_receive_actual_usage(self):
        identifier, _, second = self.legacy_retry_fixture(second_running=True)
        self.fail(second, self.observe('legacy-latest-measured', 'failed', amount='0.75'))
        row = self.ledger.snapshot()['state']['reservations'][identifier]
        self.assertEqual(row['state'], 'settled')
        self.assertEqual(row['spent']['provider_calls'], '2')
        self.assertEqual(row['spent']['amount'], '1.00')

    def assert_retained_other_intent_is_ordinary_provenance(self, canonical_key):
        first = self.claim()
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        retained_intent = job['budget_operation']['reservation_request']['evidence'][-1]
        operation_id = 'native-' + 'f' * 64
        self.assertNotEqual(operation_id, first['budget_operation_id'])
        key = operation_id + ':reserve' if canonical_key else 'ordinary-audit-provenance'
        generic = self.generic_reserve(operation_id, self.proof + [retained_intent], key)
        before, sources = self.authority(), self.source_bytes()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            NativeBudget.for_run(self.root, data, recovery=True).reconcile()
            self.assertEqual(hatch.status(self.root, data)['status'], 'running')
        self.assertEqual(self.authority(), before)
        self.assertEqual(self.source_bytes(), sources)
        receipt = self.generic_settle(generic['reservation_id'], 'complete', 'ordinary-provenance-finish')
        self.assertEqual(receipt['state'], 'settled')
        # A second native enrollment and claim also scan the retained ledger.
        other = self.prepare('unrelated-native')
        self.bind(other)
        handoff = self.claim(other)
        self.assertNotEqual(handoff['token'], first['token'])
        self.assertEqual(self.row()['spent']['amount'], '0')

    def test_other_native_intent_is_generic_provenance_with_arbitrary_key(self):
        self.assert_retained_other_intent_is_ordinary_provenance(False)

    def test_other_native_intent_is_generic_provenance_with_native_shaped_key(self):
        self.assert_retained_other_intent_is_ordinary_provenance(True)

    def test_native_shaped_generic_operation_with_plain_proof_keeps_scans_valid(self):
        self.generic_reserve('native-' + 'e' * 64)
        handoff = self.claim()
        self.assertEqual(handoff['token'], hatch.job_for(hatch.manifest(self.root), 'base')['token'])
        self.assertEqual(self.row()['state'], 'reserved')

    def attach_fixture(self):
        original = self.root / 'references/original-offline.bin'
        alternate = self.root / 'references/alternate-offline.bin'
        original.write_bytes(b'Immutable original synthetic attachment.\n')
        alternate.write_bytes(b'Different synthetic attachment, never authorized.\n')
        data = hatch.manifest(self.root)
        hatch.job_for(data, 'base')['input_images'].append(dict(path=original.relative_to(self.root).as_posix(),
                                                              role='reference'))
        hatch.save(self.root, data)
        return original, alternate

    def test_ordinary_attachment_handoff_matches_captured_bytes(self):
        original, alternate = self.attach_fixture()
        sources = {path: path.read_bytes() for path in (original, alternate)}
        handoff = self.claim()
        attached = next(item for item in handoff['input_images'] if item['role'] == 'reference')
        self.assertEqual(Path(attached['path']), original)
        expected = hashlib.sha256(sources[original]).hexdigest()
        self.assertEqual(handoff['receipt_template']['input_hashes'][original.relative_to(self.root).as_posix()], expected)
        self.assertEqual({path: path.read_bytes() for path in sources}, sources)

    @unittest.skipIf(os.name == 'nt', 'POSIX temporary symlink exchange')
    def test_transient_in_run_alias_cannot_export_another_attachment(self):
        original, alternate = self.attach_fixture()
        backup = original.with_suffix('.held')
        original_bytes, alternate_bytes = original.read_bytes(), alternate.read_bytes()
        before, sources = self.authority(), self.source_bytes()
        real_warnings, real_admission = hatch.style_warnings, NativeBudget.before_claim
        reached = []

        def restore():
            if backup.exists():
                original.unlink()
                backup.rename(original)

        def exchange_after_capture(*args, **kwargs):
            result = real_warnings(*args, **kwargs)
            original.rename(backup)
            original.symlink_to(alternate)
            reached.append('alias-visible-at-handoff-resolution')
            return result

        def restore_before_admission(budget, *args, **kwargs):
            restore()
            reached.append('original-restored-before-admission-and-final-input-recheck')
            return real_admission(budget, *args, **kwargs)

        handoff = None
        try:
            with patch.object(hatch, 'style_warnings', exchange_after_capture), \
                    patch.object(NativeBudget, 'before_claim', restore_before_admission):
                try:
                    handoff = self.claim()
                except (ValueError, RigError):
                    pass
        finally:
            restore()
        self.assertIn('alias-visible-at-handoff-resolution', reached)
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(alternate.read_bytes(), alternate_bytes)
        self.assertEqual(self.source_bytes(), sources)
        if handoff is None:
            self.assertEqual(self.authority(), before,
                             'Refused attachment resolution changed ledger or native authority')
        else:
            self.assertIn('original-restored-before-admission-and-final-input-recheck', reached)
            attached = next(item for item in handoff['input_images'] if item['role'] == 'reference')
            expected = hashlib.sha256(original_bytes).hexdigest()
            relative = original.relative_to(self.root).as_posix()
            self.assertEqual(handoff['receipt_template']['input_hashes'][relative], expected)
            job = hatch.job_for(hatch.manifest(self.root), 'base')
            self.assertEqual(hatch.inputs(self.root, job), job['input_hashes'])
            self.assertEqual(Path(attached['path']), original,
                             'Claim exported another file after the original input hashes passed')
            self.assertEqual(Path(attached['path']).read_bytes(), original_bytes)

    @unittest.skipIf(os.name == 'nt', 'POSIX FIFO input boundary')
    def test_special_attachment_is_refused_without_blocking_or_mutating_authority(self):
        original, alternate = self.attach_fixture()
        original_bytes, alternate_bytes = original.read_bytes(), alternate.read_bytes()
        before, sources = self.authority(), self.source_bytes()
        backup = original.with_suffix('.held')
        original.rename(backup)
        os.mkfifo(original)
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                '--special-attachment-probe', str(self.root)], capture_output=True,
                text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), 'refused')
            self.assertEqual(self.authority(), before)
            self.assertEqual(self.source_bytes(), sources)
            self.assertEqual(alternate.read_bytes(), alternate_bytes)
        finally:
            original.unlink()
            backup.rename(original)
        self.assertEqual(original.read_bytes(), original_bytes)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--special-attachment-probe':
        root = Path(sys.argv[2])
        try:
            with hatch.ownership(root):
                hatch.claim(root, hatch.manifest(root), 'base')
        except (ValueError, OSError, RigError):
            print('refused')
        else:
            print('accepted')
    else:
        unittest.main()
