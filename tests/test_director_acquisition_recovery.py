"""Offline native recovery regressions; no provider execution or paid calls."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from PIL import Image

import hatch
from director_acquisition import AcquisitionGateway, NativeAcquisitionGateway
from director_budget import BudgetLedger, METRICS, NativeBudget, bind_native_budget
from rig_errors import RigError
import test_director_acquisition as acquisition_fixtures

secure_artifact_test = acquisition_fixtures.secure_artifact_test


class DirectorAcquisitionRecoveryTests(unittest.TestCase):
    # Reuse fixture builders, not inherited test methods or simulated owners.
    setUp = acquisition_fixtures.DirectorAcquisitionEnvelopeTests.setUp
    ref = acquisition_fixtures.DirectorAcquisitionEnvelopeTests.ref
    request = acquisition_fixtures.DirectorAcquisitionEnvelopeTests.request

    def reserve(self):
        limits = {metric: 100 for metric in METRICS}
        self.ledger = BudgetLedger.create(self.parent/'budget', limits,
            provider_limits={'imagegen': limits}, currency='USD')
        self.estimate = {metric: 0 for metric in METRICS}
        self.estimate.update(provider_calls=1, amount=1)
        proof_path = self.parent/'proof.txt'
        proof_path.write_text('Synthetic fixture accounting only; no provider call.')
        self.proof = self.ledger.retain_evidence(proof_path)
        bind_native_budget(self.root, self.ledger.root, owner_root=self.parent,
            provider='imagegen', estimate=self.estimate, retry_allowance=0,
            evidence=[self.proof])
        self.reservation = self.gateway.reserve_budget('base')['reservation_id']

    def enroll(self):
        self.reserve()
        request = self.request('claim')
        request['context']['budget_reservation_id'] = self.reservation
        self.token = self.adapter.claim(request)['value']['claim_id']
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.hashes = deepcopy(job['input_hashes'])
        self.observation = dict(id='complete-fixture', outcome='complete', usage=self.estimate,
            evidence=[self.proof], measurement_source='Synthetic native fixture deltas', resolves=[])
        self.receipt = dict(generation_skill='$fixture', job='base', token=self.token,
            input_hashes=self.hashes, budget_usage=self.observation)
        source = self.ref('candidate.png', self.source.read_bytes(),
            'candidate', 'acquisition-source', 'image/png')
        receipt = self.ref('receipt.json', json.dumps(self.receipt).encode(),
            'receipt', 'acquisition-receipt')
        self.ingestion = self.request('ingest', 'running', self.token, [source, receipt])
        self.ingestion['context']['budget_reservation_id'] = self.reservation

    def row(self):
        return self.ledger.snapshot()['state']['reservations'][self.reservation]

    def assert_accounted_without_promotion(self):
        row = self.row()
        self.assertEqual(row['spent']['provider_calls'], '1')
        self.assertEqual(row['spent']['amount'], '1')
        self.assertEqual(len(row['attempts']), 1)
        job = hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')
        self.assertEqual(job['token'], self.token)
        self.assertEqual(job['status'], 'running')
        self.assertFalse((self.root/'decoded/base.png').exists())

    def bounded_reopened_ingest(self):
        # A subprocess timeout turns a FIFO regression into a test failure
        # without hanging the test runner or holding its native owner lock.
        code = """import json, sys
sys.path.insert(0, sys.argv[1])
from director_acquisition import AcquisitionGateway, NativeAcquisitionGateway
from rig_errors import RigError
native = NativeAcquisitionGateway(sys.argv[2], asset_kind='raster', binding=json.loads(sys.argv[3]))
adapter = AcquisitionGateway(native, sys.argv[2], 'base')
try:
    adapter.ingest(json.loads(sys.argv[4]))
except RigError as error:
    print(error.report['code'])
else:
    raise AssertionError('unsafe input unexpectedly promoted')
"""
        result = subprocess.run([sys.executable, '-c', code,
            str(Path(__file__).resolve().parents[1]/'scripts'), str(self.root),
            json.dumps(self.gateway.binding), json.dumps(self.ingestion)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('DIRECTOR_ACQUISITION_TOKEN', result.stdout)
        self.assert_accounted_without_promotion()

    @secure_artifact_test
    def test_missing_prompt_is_accounted_before_rejected_ingestion(self):
        self.enroll()
        (self.root/'prompts/base-pet.md').unlink()
        for _ in range(2):
            with self.assertRaises(RigError):
                self.adapter.ingest(self.ingestion)
        self.assert_accounted_without_promotion()

    @secure_artifact_test
    def test_current_input_validation_observes_durable_usage(self):
        self.enroll()
        original = hatch.inputs
        observed = []
        def inspect(root, job):
            observed.append(job['id'])
            self.assertEqual(self.row()['spent']['provider_calls'], '1')
            return original(root, job)
        with patch.object(hatch, 'inputs', side_effect=inspect):
            result = self.adapter.ingest(self.ingestion)
        self.assertTrue(observed)
        self.assertEqual(result['value']['native_state'], 'generated')

    @secure_artifact_test
    def test_fifo_prompt_is_bounded_after_accounting(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO adversarial fixture requires POSIX mkfifo')
        self.enroll()
        path = self.root/'prompts/base-pet.md'
        path.unlink()
        os.mkfifo(path)
        self.bounded_reopened_ingest()

    @secure_artifact_test
    def test_fifo_request_reopens_original_binding_and_retains_usage(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO adversarial fixture requires POSIX mkfifo')
        self.enroll()
        path = self.root/'pet_request.json'
        path.unlink()
        os.mkfifo(path)
        self.bounded_reopened_ingest()

    @secure_artifact_test
    def test_fifo_profile_is_bounded_after_accounting(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO adversarial fixture requires POSIX mkfifo')
        self.enroll()
        path = self.root/'profile.json'
        path.unlink()
        os.mkfifo(path)
        self.bounded_reopened_ingest()

    @secure_artifact_test
    def test_oversized_prompt_is_bounded_after_accounting(self):
        from asset_contracts import MAX_SOURCE_BYTES
        self.enroll()
        with (self.root/'prompts/base-pet.md').open('wb') as stream:
            stream.truncate(MAX_SOURCE_BYTES+1)
        self.bounded_reopened_ingest()

    @secure_artifact_test
    def test_directory_prompt_retains_authenticated_usage(self):
        self.enroll()
        path = self.root/'prompts/base-pet.md'
        path.unlink()
        path.mkdir()
        with self.assertRaises(RigError):
            self.adapter.ingest(self.ingestion)
        self.assert_accounted_without_promotion()

    @secure_artifact_test
    def test_wrong_receipt_claim_reservation_and_input_charge_nothing(self):
        self.enroll()
        forged = deepcopy(self.receipt)
        forged['token'] = 'foreign-token'
        forged_ref = self.ref('forged.json', json.dumps(forged).encode(),
            'forged', 'acquisition-receipt')
        variants = []
        wrong = deepcopy(self.ingestion)
        wrong['input']['candidates'][1] = forged_ref
        variants.append(wrong)
        wrong = deepcopy(self.ingestion)
        wrong['input']['claim_id'] = 'foreign-token'
        variants.append(wrong)
        wrong = deepcopy(self.ingestion)
        wrong['context']['budget_reservation_id'] = 'foreign-reservation'
        variants.append(wrong)
        wrong = deepcopy(self.ingestion)
        wrong['input']['input_sha256'] = '0'*64
        variants.append(wrong)
        (self.root/'prompts/base-pet.md').unlink()
        before = self.ledger.snapshot()
        for request in variants:
            with self.subTest(request=request['input']['claim_id']):
                with self.assertRaises(RigError):
                    self.adapter.ingest(request)
                self.assertEqual(self.ledger.snapshot(), before)
        self.assertFalse((self.root/'decoded/base.png').exists())

    def failure_request(self, outcome='known'):
        evidence = self.ref('observation.json', json.dumps(self.observation).encode(),
            'observation', 'acquisition-budget-observation')
        diagnostic = dict(code='NATIVE_ACQUISITION_OUTCOME', severity='warning',
            stage='acquisition', entity_id='base', field_path='outcome',
            expected=None, observed=outcome, retryable=False,
            recommended_action='Synthetic completion; retain exact native history.', evidence=[evidence])
        request = self.request('fail', 'running', self.token, diagnostics=[diagnostic])
        request['context']['budget_reservation_id'] = self.reservation
        return request

    @secure_artifact_test
    def test_terminal_generated_reconcile_uses_original_claim_and_reservation(self):
        self.enroll()
        self.adapter.ingest(self.ingestion)
        request = self.request('reconcile', 'generated', self.token)
        request['context']['budget_reservation_id'] = self.reservation
        before = self.ledger.snapshot()
        for _ in range(2):
            result = self.adapter.reconcile(request)
            self.assertEqual(result['value']['claim_id'], self.token)
            self.assertEqual(result['value']['native_state'], 'generated')
            self.assertEqual(result['value']['outcome'], 'known')
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 1)

    @secure_artifact_test
    def test_terminal_failed_reconcile_uses_original_claim_with_missing_inputs(self):
        self.enroll()
        failure = self.failure_request()
        request = self.request('reconcile', 'running', self.token)
        request['input']['expected_state'] = 'failed'
        request['context']['budget_reservation_id'] = self.reservation
        (self.root/'prompts/base-pet.md').unlink()
        self.adapter.fail(failure)
        before = self.ledger.snapshot()
        result = self.adapter.reconcile(request)
        self.assertEqual(result['value']['claim_id'], self.token)
        self.assertEqual(result['value']['native_state'], 'failed')
        self.assertEqual(self.ledger.snapshot(), before)

    @secure_artifact_test
    def test_terminal_reconcile_rejects_foreign_identity_and_changed_exact_spec(self):
        self.enroll()
        self.adapter.fail(self.failure_request())
        request = self.request('reconcile', 'failed', self.token)
        request['context']['budget_reservation_id'] = self.reservation
        variants = []
        for field, value in [('claim_id', 'foreign-token'), ('input_sha256', '0'*64)]:
            wrong = deepcopy(request)
            wrong['input'][field] = value
            variants.append(wrong)
        wrong = deepcopy(request)
        wrong['context']['budget_reservation_id'] = 'foreign-reservation'
        variants.append(wrong)
        spec = json.loads((self.root/'spec.json').read_text())
        spec['licensing'] = 'Different fixture license'
        wrong = deepcopy(request)
        wrong['input']['spec'] = self.ref('changed-spec.json', json.dumps(spec).encode(),
            spec['id'], 'director-asset-spec')
        variants.append(wrong)
        before = self.ledger.snapshot()
        for wrong in variants:
            with self.assertRaises(RigError):
                self.adapter.reconcile(wrong)
            self.assertEqual(self.ledger.snapshot(), before)

    @secure_artifact_test
    def test_old_historical_claim_cannot_reconcile_a_later_native_attempt(self):
        first = self.adapter.claim(self.request('claim'))['value']['claim_id']
        self.gateway.fail('base', first, 'Synthetic first attempt failed.')
        second = self.adapter.claim(self.request('claim', 'failed'))['value']['claim_id']
        request = self.request('reconcile', 'running', first)
        with self.assertRaises(RigError):
            self.adapter.reconcile(request)
        self.gateway.fail('base', second, 'Synthetic second attempt failed.')
        request = self.request('reconcile', 'failed', first)
        with self.assertRaises(RigError):
            self.adapter.reconcile(request)
        request['input']['claim_id'] = second
        result = self.adapter.reconcile(request)
        self.assertEqual(result['value']['claim_id'], second)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 2)

    @secure_artifact_test
    def test_preexisting_candidate_hardlink_is_never_adopted(self):
        if not hasattr(os, 'link'):
            self.skipTest('Hardlink adversarial fixture requires os.link')
        self.enroll()
        original = self.source.read_bytes()
        folder = self.root/'director-inputs'
        folder.mkdir()
        target = folder/(hashlib.sha256(original).hexdigest()+'.png')
        os.link(self.source, target)
        result = self.adapter.ingest(self.ingestion)
        self.assertEqual(result['value']['native_state'], 'generated')
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        preserved = Path(job['source_path'])
        self.assertFalse(os.path.samefile(preserved, self.source))
        self.assertEqual(preserved.stat().st_nlink, 1)
        Image.new('RGBA', (96, 96), (220, 10, 10, 255)).save(self.source)
        self.assertEqual(preserved.read_bytes(), original)
        self.assertEqual(job['source_hash'], hashlib.sha256(original).hexdigest())
        with Image.open(self.root/'decoded/base.png') as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (30, 70, 160, 255))

    @secure_artifact_test
    def test_candidate_substitution_at_native_handoff_cannot_change_consumed_bytes(self):
        self.enroll()
        original = self.source.read_bytes()
        native_ingest = hatch.ingest
        def substitute(*args, **kwargs):
            # Inject a boundary race, then execute the real native owner.
            source = Path(args[3])
            Image.new('RGBA', (96, 96), (220, 10, 10, 255)).save(source)
            return native_ingest(*args, **kwargs)
        with patch.object(hatch, 'ingest', side_effect=substitute):
            result = self.adapter.ingest(self.ingestion)
        self.assertEqual(result['value']['native_state'], 'generated')
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(Path(job['source_path']).read_bytes(), original)
        self.assertEqual(job['source_hash'], hashlib.sha256(original).hexdigest())
        with Image.open(self.root/'decoded/base.png') as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (30, 70, 160, 255))

    @contextmanager
    def deadline_clock(self, request):
        request['context']['deadline'] = '2030-01-01T00:00:00Z'
        current = [datetime(2029, 1, 1, tzinfo=timezone.utc)]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return current[0]
        def expire_after(operation):
            def execute(*args, **kwargs):
                result = operation(*args, **kwargs)
                current[0] = datetime(2031, 1, 1, tzinfo=timezone.utc)
                return result
            return execute
        with patch('datetime.datetime', Clock):
            yield expire_after

    def assert_unclaimed_unbound(self):
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 0)
        self.assertEqual(job['status'], 'pending')
        self.assertNotIn('token', job)
        self.assertNotIn('director_spec_binding', job)

    @secure_artifact_test
    def test_deadline_after_generation_validation_cannot_bind_or_claim(self):
        for method in ('prepare', 'claim'):
            request = self.request(method)
            before = (self.root/'imagegen-jobs.json').read_bytes()
            with self.subTest(method=method), self.deadline_clock(request) as expire_after:
                with patch.object(hatch, 'generation_inputs', side_effect=expire_after(hatch.generation_inputs)), \
                     patch.object(hatch, 'claim', wraps=hatch.claim) as claimed:
                    with self.assertRaisesRegex(RigError, 'deadline'):
                        getattr(self.adapter, method)(request)
                    claimed.assert_not_called()
            self.assert_unclaimed_unbound()
            self.assertEqual((self.root/'imagegen-jobs.json').read_bytes(), before)

    @secure_artifact_test
    def test_deadline_after_reservation_validation_cannot_bind_or_claim(self):
        self.reserve()
        request = self.request('claim')
        request['context']['budget_reservation_id'] = self.reservation
        before = self.ledger.snapshot()
        with self.deadline_clock(request) as expire_after:
            with patch.object(NativeBudget, 'verify_reservation', autospec=True,
                              side_effect=expire_after(NativeBudget.verify_reservation)) as verified:
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.claim(request)
                self.assertEqual(verified.call_count, 1)
        self.assert_unclaimed_unbound()
        self.assertEqual(self.ledger.snapshot(), before)

    @secure_artifact_test
    def test_deadline_after_policy_read_cannot_bind_or_claim(self):
        import director_contracts
        self.reserve()
        policy_ref = self.ref('policy.json', b'{}', 'policy', 'policy')
        spec = json.loads((self.root/'spec.json').read_text())
        policy = dict(spec['source_policy'], network='allow-configured',
                      providers=['imagegen'], paid='bounded', policy_ref=policy_ref)
        spec['source_policy'] = policy
        self.spec = self.ref('spec.json', json.dumps(spec).encode(), spec['id'], 'director-asset-spec')
        request = self.request('claim')
        request['context'].update(policy=policy, budget_reservation_id=self.reservation)
        original = director_contracts._read_artifact
        before = self.ledger.snapshot()
        with self.deadline_clock(request) as expire_after:
            def read(root, ref):
                return (expire_after(original) if ref == policy_ref else original)(root, ref)
            with patch.object(director_contracts, '_read_artifact', side_effect=read):
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.claim(request)
        self.assert_unclaimed_unbound()
        self.assertEqual(self.ledger.snapshot(), before)

    @secure_artifact_test
    def test_deadline_after_native_reconcile_cannot_bind_or_claim(self):
        request = self.request('claim')
        with self.deadline_clock(request) as expire_after:
            with patch.object(hatch, 'reconcile', side_effect=expire_after(hatch.reconcile)), \
                 patch.object(hatch, 'claim', wraps=hatch.claim) as claimed:
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.claim(request)
                claimed.assert_not_called()
        self.assert_unclaimed_unbound()

    @secure_artifact_test
    def test_deadline_after_binding_commit_still_prevents_native_claim(self):
        request = self.request('claim')
        original = hatch.save
        with self.deadline_clock(request) as expire_after:
            def save(root, data):
                operation = expire_after(original) if hatch.job_for(data, 'base').get('director_spec_binding') else original
                return operation(root, data)
            with patch.object(hatch, 'save', side_effect=save), \
                 patch.object(hatch, 'claim', wraps=hatch.claim) as claimed:
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.claim(request)
                claimed.assert_not_called()
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 0)
        self.assertNotIn('token', job)
        # The completed specification write is evidence, not a paid attempt.
        self.assertEqual(job['director_spec_binding'], self.spec)

    @secure_artifact_test
    def test_deadline_after_usage_accounting_retains_original_recoverable_claim(self):
        self.enroll()
        with self.deadline_clock(self.ingestion) as expire_after:
            with patch.object(hatch, 'record_budget_usage', side_effect=expire_after(hatch.record_budget_usage)), \
                 patch.object(hatch, 'inputs', wraps=hatch.inputs) as inputs:
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.ingest(self.ingestion)
                inputs.assert_not_called()
        self.assert_accounted_without_promotion()
        result = self.adapter.ingest(self.ingestion)
        self.assertEqual(result['value']['native_state'], 'generated')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(len(self.row()['attempts']), 1)

    @secure_artifact_test
    def test_deadline_after_current_input_validation_keeps_usage_without_promotion(self):
        self.enroll()
        with self.deadline_clock(self.ingestion) as expire_after:
            with patch.object(hatch, 'inputs', side_effect=expire_after(hatch.inputs)), \
                 patch.object(hatch, 'ingest', wraps=hatch.ingest) as ingested:
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.ingest(self.ingestion)
                ingested.assert_not_called()
        self.assert_accounted_without_promotion()

    @secure_artifact_test
    def test_deadline_after_candidate_read_keeps_usage_without_promotion(self):
        import director_contracts
        self.enroll()
        original = director_contracts._read_artifact
        candidate = self.ingestion['input']['candidates'][0]
        with self.deadline_clock(self.ingestion) as expire_after:
            def read(root, ref):
                return (expire_after(original) if ref == candidate else original)(root, ref)
            with patch.object(director_contracts, '_read_artifact', side_effect=read), \
                 patch.object(hatch, 'ingest', wraps=hatch.ingest) as ingested:
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.ingest(self.ingestion)
                ingested.assert_not_called()
        self.assert_accounted_without_promotion()

    @secure_artifact_test
    def test_deadline_after_native_claim_preserves_committed_token_for_replay(self):
        request = self.request('claim')
        with self.deadline_clock(request) as expire_after:
            with patch.object(hatch, 'claim', side_effect=expire_after(hatch.claim)):
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.claim(request)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['status'], 'running')
        token = job['token']
        result = self.adapter.claim(request)
        self.assertEqual(result['value']['claim_id'], token)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 1)

    @secure_artifact_test
    def test_deadline_during_final_result_validation_cannot_return_success(self):
        import director_contracts
        request = self.request('claim')
        with self.deadline_clock(request) as expire_after:
            with patch.object(director_contracts, 'validate_method_result',
                              side_effect=expire_after(director_contracts.validate_method_result)):
                with self.assertRaisesRegex(RigError, 'deadline'):
                    self.adapter.claim(request)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['status'], 'running')
        self.assertIn('token', job)


if __name__ == '__main__':
    unittest.main()
