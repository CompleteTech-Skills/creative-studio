"""Independent offline contract tests for the canonical native-budget bridge.

All images, policies and usage are synthetic. Non-fixture runs exercise local
admission metadata only. No provider, executor, network client or credentials
are used. These tests retain the actual Hatch owner and append-only ledger.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hatch
from director_acquisition import AcquisitionGateway, NativeAcquisitionGateway
from director_budget import BudgetLedger, METRICS, NativeBudget, bind_native_budget
from director_contracts import validate_method_result
from fixture_workflow import small_profile, fixture_strip
from rig_errors import RigError, object_hash
from source_interface import import_source


def quantities(**values):
    return {key: values.get(key, 0) for key in METRICS}


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd,
                     'Native acquisition budget tests require secure descriptor-relative artifact reads.')
class DirectorAcquisitionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.owner = Path(self.temporary.name).resolve()
        self.profile = self.owner / 'profile.json'
        hatch.atomic(self.profile, small_profile())
        self.ledger = self.make_ledger(self.owner / 'ledger')
        proof = self.owner / 'synthetic-measurement.json'
        proof.write_text('{"fixture":true,"measurement":"offline scenario"}', encoding='utf-8')
        self.proof = [self.ledger.retain_evidence(proof)]
        self.root = self.make_run('run')
        self.bind(self.root)
        self.native = NativeAcquisitionGateway(self.root, asset_kind='raster')
        self.adapter = AcquisitionGateway(self.native, self.root, 'base')
        self.reservation = None
        self.templates = json.loads((Path(__file__).resolve().parents[1] /
            'examples/director-contracts/contracts.json').read_text())
        self.spec_document = json.loads((Path(__file__).resolve().parents[1] /
            'examples/director-contracts/asset-spec-fixture.json').read_text())
        self.spec_document.update(logical_id='base', asset_type='raster', formats=['png'])
        self.spec = self.json_ref('spec.json', self.spec_document, 'director-asset-spec')
        self.source = self.owner / 'synthetic.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(self.source)

    def make_ledger(self, path):
        limits = quantities(provider_calls=60, amount=120)
        return BudgetLedger.create(path, limits,
            provider_limits={'imagegen': limits}, currency='USD')

    def make_run(self, name, fixture=True):
        root = self.owner / name
        hatch.prepare(root, argparse.Namespace(replace_inputs=False, fixture_mode=fixture,
            profile=str(self.profile), pet_name='Offline bridge fixture',
            pet_notes='Synthetic local contract exercise; no provider execution',
            reference=[], avoid=[], require=[], brand_source=[],
            style_preset='clay', style_notes='synthetic'))
        return root

    def bind(self, root):
        return bind_native_budget(root, self.ledger.root, owner_root=self.owner,
            provider='imagegen', estimate=quantities(provider_calls=1, amount=2),
            retry_allowance=2, evidence=self.proof)

    def job(self):
        return hatch.job_for(hatch.manifest(self.root), self.adapter.job)

    def ref(self, path, payload, kind, media='application/json'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return dict(artifact_id=path.replace('/', '-'), kind=kind, schema_version=1,
            semantic_version='1.0.0', path=path, sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload), media_type=media, provenance_ref=None,
            fixture=self.native.binding['fixture'], dependencies=[])

    def json_ref(self, path, value, kind):
        ref = self.ref(path, json.dumps(value, sort_keys=True).encode(), kind)
        if kind == 'director-asset-spec':
            ref['artifact_id'] = value['id']
        return ref

    def request(self, method, state=None, token=None, candidates=None, diagnostics=None):
        name = 'AcquisitionGateway.' + method
        value = deepcopy(next(item for item in self.templates
            if item.get('method') == name and item['kind'] == 'director-request'))
        value['context']['deadline'] = '2099-01-01T00:00:00Z'
        value['context']['budget_reservation_id'] = (
            self.reservation['reservation_id'] if self.reservation else None)
        value['input'] = dict(spec=self.spec, input_sha256=self.adapter.input_sha256(),
            binding_id=self.native.binding['binding_id'], claim_id=token,
            expected_state=state or self.job()['status'], provider_request_id=None,
            candidates=candidates or [], diagnostics=diagnostics or [])
        return value

    def reserve(self):
        self.reservation = self.native.reserve_budget(self.adapter.job)
        return self.reservation

    def claim(self):
        if self.reservation is None:
            self.reserve()
        request = self.request('claim')
        result = self.adapter.claim(request)
        validate_method_result('AcquisitionGateway.claim', result, request)
        return result['value']['claim_id']

    def observation(self, identity='measured', outcome='failed', calls=1,
                    amount='0.5', resolves=None):
        return dict(id=identity, outcome=outcome,
            usage=quantities(provider_calls=calls, amount=amount),
            evidence=deepcopy(self.proof), measurement_source='Synthetic offline usage delta',
            resolves=resolves or [])

    def failure(self, token, observation, observed=None):
        evidence = self.json_ref('observation-' + observation['id'] + '.json',
            observation, 'acquisition-budget-observation')
        diagnostic = dict(code='NATIVE_ACQUISITION_OUTCOME', severity='warning',
            stage='acquisition', entity_id=self.adapter.job, field_path='outcome',
            expected=None,
            observed=observed or ('uncertain' if observation['outcome'] == 'uncertain' else 'known'),
            retryable=False, recommended_action='Retain synthetic measured outcome.',
            evidence=[evidence])
        return self.request('fail', 'running', token, diagnostics=[diagnostic])

    def receipt(self, token, observation=None):
        value = dict(generation_skill='$fixture' if self.native.binding['fixture'] else '$imagegen',
            job=self.adapter.job, token=token, input_hashes=self.job()['input_hashes'])
        if observation is not None:
            value['budget_usage'] = observation
        return value

    def ingest_request(self, token, observation=None):
        candidates = [self.ref('candidate.png', self.source.read_bytes(),
            'acquisition-source', 'image/png'),
            self.json_ref('receipt.json', self.receipt(token, observation), 'acquisition-receipt')]
        return self.request('ingest', 'running', token, candidates=candidates)

    def row(self):
        return self.ledger.snapshot()['state']['reservations'][self.reservation['reservation_id']]

    def assert_active(self, token, attempts=1):
        job = self.job()
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['token'], token)
        self.assertEqual(job['attempts'], attempts)

    def assert_no_usage(self, before, token):
        self.assertEqual(self.ledger.snapshot(), before)
        self.assert_active(token)

    def test_reserve_requires_enrollment_and_allocates_no_native_claim(self):
        root = self.make_run('unenrolled')
        native = NativeAcquisitionGateway(root, asset_kind='raster')
        before = (root / 'imagegen-jobs.json').read_bytes()
        with self.assertRaises((RigError, ValueError)):
            native.reserve_budget('base')
        self.assertEqual((root / 'imagegen-jobs.json').read_bytes(), before)
        descriptor = self.reserve()
        self.assertEqual(set(descriptor), {'reservation_id', 'operation_id',
            'ledger_revision', 'state', 'reserved'})
        self.assertEqual(descriptor['state'], 'reserved')
        self.assertEqual(descriptor['ledger_revision'], self.ledger.snapshot()['sha256'])
        self.assertEqual(descriptor['reserved'],
            {key: str(value) for key, value in quantities(provider_calls=3, amount=6).items()})
        job = self.job()
        self.assertEqual(job['status'], 'pending')
        self.assertEqual(job['attempts'], 0)
        self.assertNotIn('token', job)
        self.assertEqual(job['budget_operation']['attempts'], [])
        self.assertFalse(any(item['event'] == 'claim' for item in job['history']))
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            verified = NativeBudget.for_run(self.root, data).verify_reservation(
                hatch.job_for(data, 'base'), descriptor['reservation_id'])
        self.assertEqual(verified, descriptor)

    def test_reservation_reopen_and_concurrent_claim_reuse_exact_row(self):
        first = self.reserve()
        before = self.ledger.snapshot()
        reopened = NativeAcquisitionGateway(self.root, asset_kind='raster', binding=self.native.binding)
        self.assertEqual(reopened.reserve_budget('base'), first)
        self.assertEqual(self.ledger.snapshot(), before)
        request = self.request('claim')
        def claim(_):
            native = NativeAcquisitionGateway(self.root, asset_kind='raster', binding=self.native.binding)
            return AcquisitionGateway(native, self.root, 'base').claim(deepcopy(request))
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(claim, range(4)))
        tokens = {result['value']['claim_id'] for result in results}
        self.assertEqual(len(tokens), 1)
        self.assert_active(tokens.pop())
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.job()['budget_operation']['reservation_id'], first['reservation_id'])
        self.assertEqual(len(self.job()['budget_operation']['attempts']), 1)
        self.assertEqual(sum(item['event'] == 'claim' for item in self.job()['history']), 1)

    def test_all_enrolled_operations_authenticate_context_before_mutation(self):
        token = self.claim()
        observation = self.observation(outcome='complete')
        requests = {
            'prepare': self.request('prepare', 'running', token),
            'claim': self.request('claim', 'running', token),
            'reconcile': self.request('reconcile', 'running', token),
            'ingest': self.ingest_request(token, observation),
            'fail': self.failure(token, self.observation()),
        }
        other = self.make_run('other-run')
        self.bind(other)
        foreign = NativeAcquisitionGateway(other, asset_kind='raster').reserve_budget('base')
        unrelated = self.ledger.reserve(operation_id='unrelated-job', provider='imagegen',
            estimate=quantities(provider_calls=1, amount=2), retry_allowance=0,
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key='unrelated-job',
            evidence=self.proof)
        other_ledger = self.make_ledger(self.owner / 'other-ledger')
        other_proof = [other_ledger.retain_evidence(self.owner / 'synthetic-measurement.json')]
        foreign_ledger = other_ledger.reserve(operation_id='foreign-ledger', provider='imagegen',
            estimate=quantities(provider_calls=1, amount=2), retry_allowance=0,
            expected_revision=other_ledger.snapshot()['sha256'], idempotency_key='foreign-ledger',
            evidence=other_proof)
        wrong_ids = [None, 'reservation-invented', foreign['reservation_id'],
            unrelated['reservation_id'], foreign_ledger['reservation_id']]
        before = self.ledger.snapshot()
        native_before = (self.root / 'imagegen-jobs.json').read_bytes()
        foreign_before = other_ledger.snapshot()
        for method, original in requests.items():
            for identifier in wrong_ids:
                with self.subTest(method=method, reservation=identifier):
                    request = deepcopy(original)
                    request['context']['budget_reservation_id'] = identifier
                    with self.assertRaises(RigError):
                        getattr(self.adapter, method)(request)
                    self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), native_before)
                    self.assert_no_usage(before, token)
                    self.assertEqual(other_ledger.snapshot(), foreign_before)

    def configure_paid(self, root, enroll):
        self.root = root
        if enroll:
            self.bind(root)
        self.native = NativeAcquisitionGateway(root, asset_kind='raster')
        self.adapter = AcquisitionGateway(self.native, root, 'base')
        self.reservation = None
        data = hatch.manifest(root)
        data['limits'].update(generation_calls=10, unit_budget=20, estimated_units_per_call=2)
        hatch.save(root, data)
        policy_ref = self.ref('policy.txt', b'Synthetic bounded policy; no execution.',
            'policy', 'text/plain')
        self.paid_policy = dict(network='allow-configured', providers=['imagegen'],
            paid='bounded', policy_ref=policy_ref)
        spec = deepcopy(self.spec_document)
        spec['source_policy'] = deepcopy(self.paid_policy)
        self.spec = self.json_ref('spec.json', spec, 'director-asset-spec')

    def test_nonfixture_paid_claim_fails_closed_without_native_enrollment(self):
        self.configure_paid(self.make_run('paid-unenrolled', fixture=False), False)
        for paid in ('bounded', 'allow-unknown-price'):
            policy = dict(self.paid_policy, paid=paid)
            spec = deepcopy(self.spec_document)
            spec['source_policy'] = policy
            self.spec = self.json_ref('spec-' + paid + '.json', spec, 'director-asset-spec')
            request = self.request('claim')
            request['context']['policy'] = policy
            request['context']['budget_reservation_id'] = 'reservation-invented'
            before = self.ledger.snapshot()
            with self.subTest(paid=paid), self.assertRaises(RigError):
                self.adapter.claim(request)
            self.assertEqual(self.job()['attempts'], 0)
            self.assertNotIn('token', self.job())
            self.assertEqual(self.ledger.snapshot(), before)

    def test_native_reservation_does_not_replace_paid_policy_checks(self):
        self.configure_paid(self.make_run('paid-enrolled', fixture=False), True)
        self.reserve()
        allowed = self.request('claim')
        allowed['context']['policy'] = deepcopy(self.paid_policy)
        for scope in ('context', 'spec'):
            for field, value in [('network', 'deny'), ('providers', []),
                                 ('paid', 'deny'), ('policy_ref', None)]:
                with self.subTest(scope=scope, field=field):
                    request = deepcopy(allowed)
                    if scope == 'context':
                        request['context']['policy'][field] = value
                    else:
                        spec = deepcopy(self.spec_document)
                        spec['source_policy'] = dict(self.paid_policy, **{field: value})
                        request['input']['spec'] = self.json_ref(
                            'denied-' + field + '.json', spec, 'director-asset-spec')
                    before = self.ledger.snapshot()
                    with self.assertRaises(RigError):
                        self.adapter.claim(request)
                    self.assertEqual(self.ledger.snapshot(), before)
                    self.assertEqual(self.job()['attempts'], 0)
        result = self.adapter.claim(allowed)
        self.assertEqual(result['value']['native_state'], 'running')
        self.assertFalse(result['value']['binding']['fixture'])
        self.assertEqual(result['usage']['provider_calls']['value'], 0)
        # A deny policy forbids NEW spending, but cannot erase authenticated
        # expenditure from this original native owner.
        token = result['value']['claim_id']
        recovery = self.failure(token, self.observation('paid-recovery'))
        self.assertEqual(recovery['context']['policy']['paid'], 'deny')
        self.adapter.fail(recovery)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertEqual(self.job()['status'], 'failed')

    def test_repaired_import_uses_generation_inputs_for_hash_reserve_and_claim(self):
        descriptor = self.owner / 'import.json'
        hatch.atomic(descriptor, dict(schema_version=1, kind='import', layout='image',
            files=[self.source.name], origin='Synthetic fixture import', fixture=True))
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            import_source(self.root, data, 'base', descriptor, 'Synthetic import only.')
            hatch.repair(self.root, data, 'base', 'Explicit local fixture regeneration.')
        before = self.job()
        expected = hatch.generation_inputs(self.root, before)
        self.assertNotEqual(expected, hatch.inputs(self.root, before))
        self.assertEqual(self.adapter.input_sha256(), object_hash(expected))
        reservation = self.reserve()
        self.assertEqual(self.job()['import_receipt'], before['import_receipt'])
        token = self.claim()
        self.assert_active(token)
        self.assertEqual(self.job()['input_hashes'], expected)
        self.assertEqual(self.job()['budget_operation']['reservation_id'], reservation['reservation_id'])
        self.assertNotIn('import_receipt', self.job())

    def test_authentic_usage_precedes_every_candidate_validation_failure(self):
        cases = ('missing', 'wrong-hash', 'changed-bytes', 'invalid-image',
                 'wrong-kind', 'wrong-fixture', 'stale-input', 'missing-input')
        for case in cases:
            with self.subTest(case=case):
                root = self.make_run('bad-output-' + case)
                self.bind(root)
                self.root = root
                self.native = NativeAcquisitionGateway(root, asset_kind='raster')
                self.adapter = AcquisitionGateway(self.native, root, 'base')
                self.reservation = None
                self.spec = self.json_ref('spec.json', self.spec_document, 'director-asset-spec')
                token = self.claim()
                observation = self.observation('complete-' + case, 'complete', amount='0.7')
                request = self.ingest_request(token, observation)
                source = request['input']['candidates'][0]
                if case == 'missing':
                    (root / source['path']).unlink()
                elif case == 'wrong-hash':
                    source['sha256'] = '0' * 64
                elif case == 'changed-bytes':
                    (root / source['path']).write_bytes(b'Changed synthetic candidate')
                elif case == 'invalid-image':
                    request['input']['candidates'][0] = self.ref(
                        'invalid.png', b'Not an image', 'acquisition-source', 'image/png')
                elif case == 'wrong-kind':
                    source['kind'] = 'untrusted-raster'
                elif case == 'wrong-fixture':
                    source['fixture'] = False
                elif case == 'stale-input':
                    prompt = root / 'prompts/base-pet.md'
                    prompt.write_text(prompt.read_text() + '\nChanged synthetic prompt')
                else:
                    (root / 'prompts/base-pet.md').unlink()
                for replay in range(2):
                    with self.assertRaises((RigError, ValueError, OSError)):
                        self.adapter.ingest(deepcopy(request))
                    row = self.row()
                    self.assertEqual(row['spent']['amount'], '0.7')
                    self.assertEqual(row['spent']['provider_calls'], '1')
                    self.assertEqual(len(row['attempts']), 1)
                    self.assertEqual(row['state'], 'settled')
                    self.assert_active(token)
                    self.assertFalse((root / 'decoded/base.png').exists())

    def test_forged_receipt_wrong_spec_and_foreign_ownership_record_no_usage(self):
        token = self.claim()
        valid = self.ingest_request(token, self.observation(outcome='complete'))
        cases = ('receipt-hash', 'receipt-token', 'receipt-job', 'receipt-inputs',
                 'receipt-skill', 'receipt-fixture', 'receipt-kind', 'claim-token', 'spec')
        before = self.ledger.snapshot()
        for case in cases:
            with self.subTest(case=case):
                request = deepcopy(valid)
                receipt_ref = request['input']['candidates'][1]
                receipt = self.receipt(token, self.observation(outcome='complete'))
                if case in ('receipt-token', 'receipt-job', 'receipt-inputs', 'receipt-skill'):
                    field = {'receipt-token': 'token', 'receipt-job': 'job',
                        'receipt-inputs': 'input_hashes', 'receipt-skill': 'generation_skill'}[case]
                    receipt[field] = {} if field == 'input_hashes' else 'forged'
                    request['input']['candidates'][1] = self.json_ref(
                        'forged-' + case + '.json', receipt, 'acquisition-receipt')
                elif case == 'receipt-hash':
                    receipt_ref['sha256'] = '0' * 64
                elif case == 'receipt-fixture':
                    receipt_ref['fixture'] = False
                elif case == 'receipt-kind':
                    receipt_ref['kind'] = 'untrusted-receipt'
                elif case == 'claim-token':
                    request['input']['claim_id'] = 'forged'
                else:
                    spec = deepcopy(self.spec_document)
                    spec['licensing'] = 'changed-license'
                    request['input']['spec'] = self.json_ref(
                        'other-spec.json', spec, 'director-asset-spec')
                with self.assertRaises(RigError):
                    self.adapter.ingest(request)
                self.assert_no_usage(before, token)

    def test_valid_ingest_charges_exact_usage_once_and_preserves_fixture_source(self):
        token = self.claim()
        request = self.ingest_request(token, self.observation('complete', 'complete', amount='0.7'))
        result = self.adapter.ingest(request)
        validate_method_result('AcquisitionGateway.ingest', result, request)
        self.assertEqual(result['value']['native_state'], 'generated')
        self.assertEqual(result['value']['claim_id'], token)
        self.assertEqual(self.row()['spent']['amount'], '0.7')
        self.assertEqual(self.row()['state'], 'settled')
        self.assertEqual(len(self.row()['attempts']), 1)
        job = self.job()
        self.assertEqual(job['source_origin'], 'fixture')
        self.assertEqual(Path(job['source_path']).read_bytes(), self.source.read_bytes())
        self.assertNotIn('token', job)
        before = self.ledger.snapshot()
        with self.assertRaises((RigError, ValueError)):
            self.adapter.ingest(request)
        self.assertEqual(self.ledger.snapshot(), before)

    def test_failure_requires_one_exact_matching_budget_observation(self):
        token = self.claim()
        valid = self.failure(token, self.observation())
        cases = ('missing', 'two', 'kind', 'hash', 'uncertain-mismatch', 'known-mismatch', 'extra-field')
        before = self.ledger.snapshot()
        for case in cases:
            request = deepcopy(valid)
            diagnostic = request['input']['diagnostics'][0]
            if case == 'missing':
                diagnostic['evidence'] = []
            elif case == 'two':
                diagnostic['evidence'] *= 2
            elif case == 'kind':
                diagnostic['evidence'][0]['kind'] = 'untrusted-observation'
            elif case == 'hash':
                diagnostic['evidence'][0]['sha256'] = '0' * 64
            elif case == 'uncertain-mismatch':
                diagnostic['observed'] = 'uncertain'
            else:
                observation = self.observation('bad-' + case)
                if case == 'known-mismatch':
                    observation.update(outcome='uncertain')
                    observation['usage']['amount'] = None
                else:
                    observation['unrecognized'] = True
                diagnostic['evidence'] = [self.json_ref(
                    case + '.json', observation, 'acquisition-budget-observation')]
            with self.subTest(case=case), self.assertRaises(RigError):
                self.adapter.fail(request)
            self.assert_no_usage(before, token)

    def test_uncertain_replay_preserves_owner_then_explicit_resolution_retains_deltas(self):
        token = self.claim()
        unknown = self.observation('disconnect', 'uncertain', amount=None)
        request = self.failure(token, unknown)
        self.adapter.fail(request)
        after = self.ledger.snapshot()
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.row()['held']['amount'], '6')
        self.assert_active(token)
        self.native = NativeAcquisitionGateway(self.root, asset_kind='raster', binding=self.native.binding)
        self.adapter = AcquisitionGateway(self.native, self.root, 'base')
        self.adapter.fail(request)
        replay = self.adapter.claim(self.request('claim', 'pending'))
        self.assertEqual(replay['value']['claim_id'], token)
        self.assertEqual(self.ledger.snapshot(), after)
        self.assertEqual(len(self.job()['budget_operation']['attempts']), 1)
        with self.assertRaises(RigError):
            self.adapter.fail(self.failure(token,
                self.observation('unresolved', 'failed', calls=0, amount='0.4')))
        self.assertEqual(self.ledger.snapshot(), after)
        resolution = self.observation('resolved', 'failed', calls=0, amount='0.4',
            resolves=['disconnect'])
        result = self.adapter.fail(self.failure(token, resolution))
        self.assertEqual(result['value']['native_state'], 'failed')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '0.4')
        self.assertEqual(self.row()['state'], 'settled')
        self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
        self.assertNotIn('token', self.job())

    def test_retry_reuses_reservation_and_late_old_observation_cannot_clear_new_owner(self):
        token = self.claim()
        earlier = self.failure(token, self.observation('rate-limit', 'retry', amount='0.25'))
        self.adapter.fail(earlier)
        self.assertEqual(self.row()['state'], 'reserved')
        next_token = self.adapter.claim(self.request('claim'))['value']['claim_id']
        self.assertNotEqual(next_token, token)
        self.assert_active(next_token, attempts=2)
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.adapter.fail(earlier)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assert_active(next_token, attempts=2)
        self.assertEqual(len(before['state']['reservations']), 1)
        self.adapter.fail(self.failure(next_token,
            self.observation('final-failure', 'failed', amount='0.75')))
        self.assertEqual(self.row()['spent']['amount'], '1.00')
        self.assertEqual(self.row()['spent']['provider_calls'], '2')

    def test_interrupted_ledger_settlement_replays_native_intent_exactly_once(self):
        token = self.claim()
        request = self.failure(token, self.observation('durable-failure'))
        with patch.object(BudgetLedger, 'settle', side_effect=RuntimeError('synthetic publication fault')):
            with self.assertRaises(RuntimeError):
                self.adapter.fail(request)
        self.assertEqual(self.row()['spent']['amount'], '0')
        self.assert_active(token)
        self.native = NativeAcquisitionGateway(self.root, asset_kind='raster', binding=self.native.binding)
        self.adapter = AcquisitionGateway(self.native, self.root, 'base')
        self.adapter.fail(request)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertEqual(len(self.row()['attempts']), 1)
        self.assertEqual(self.job()['status'], 'failed')

    def test_post_settlement_pre_finalize_fault_preserves_original_owner_and_charge(self):
        token = self.claim()
        request = self.failure(token, self.observation('finalization-fault'))
        original = hatch.save
        def interrupted(root, data):
            if hatch.job_for(data, 'base')['status'] == 'failed':
                raise RuntimeError('synthetic native finalization fault')
            return original(root, data)
        with patch.object(hatch, 'save', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.adapter.fail(request)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assert_active(token)
        self.adapter.fail(request)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertEqual(len(self.row()['attempts']), 1)

    def test_legacy_fixture_without_enrollment_keeps_canonical_claim_fail_ingest(self):
        self.root = self.make_run('legacy')
        self.native = NativeAcquisitionGateway(self.root, asset_kind='raster')
        self.adapter = AcquisitionGateway(self.native, self.root, 'base')
        self.spec = self.json_ref('spec.json', self.spec_document, 'director-asset-spec')
        self.reservation = None
        before = self.ledger.snapshot()
        token = self.adapter.claim(self.request('claim'))['value']['claim_id']
        diagnostic = dict(code='NATIVE_ACQUISITION_OUTCOME', severity='warning',
            stage='acquisition', entity_id='base', field_path='outcome', expected=None,
            observed='known', retryable=False, recommended_action='Known synthetic failure.', evidence=[])
        self.adapter.fail(self.request('fail', 'running', token, diagnostics=[diagnostic]))
        next_token = self.adapter.claim(self.request('claim'))['value']['claim_id']
        self.assertNotEqual(token, next_token)
        self.adapter.ingest(self.ingest_request(next_token))
        self.assertEqual(self.job()['status'], 'generated')
        self.assertNotIn('director_budget', hatch.manifest(self.root))
        self.assertEqual(self.ledger.snapshot(), before)

    def import_fixture_base_and_rest(self):
        descriptor = self.owner / 'base-import.json'
        hatch.atomic(descriptor, dict(schema_version=1, kind='import', layout='image',
            files=[self.source.name], origin='Explicit synthetic base fixture', fixture=True))
        strip = self.owner / 'synthetic-rest.png'
        fixture_strip(small_profile()['states'][0], (96, 96)).save(strip)
        strip_descriptor = self.owner / 'rest-import.json'
        hatch.atomic(strip_descriptor, dict(schema_version=1, kind='import', layout='strip',
            files=[strip.name], origin='Explicit synthetic strip fixture', fixture=True))
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            import_source(self.root, data, 'base', descriptor, 'Synthetic setup; no artwork acceptance.')
            hatch.record_base_review(self.root, data,
                'Synthetic calibration checkpoint; no production artwork judgment.', True)
            import_source(self.root, data, 'rest', strip_descriptor, 'Synthetic setup only.')

    def test_other_native_job_reservation_in_same_run_cannot_authorize_claim(self):
        self.import_fixture_base_and_rest()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.repair(self.root, data, 'rest', 'Synthetic fixture regeneration.')
        self.adapter = AcquisitionGateway(self.native, self.root, 'rest')
        spec = dict(self.spec_document, logical_id='rest')
        self.spec = self.json_ref('rest-spec.json', spec, 'director-asset-spec')
        correct = self.reserve()
        foreign = self.native.reserve_budget('jump')
        before = self.ledger.snapshot()
        manifest_before = (self.root / 'imagegen-jobs.json').read_bytes()
        for method in ('prepare', 'claim', 'reconcile'):
            request = self.request(method)
            request['context']['budget_reservation_id'] = foreign['reservation_id']
            with self.subTest(method=method), self.assertRaises(RigError):
                getattr(self.adapter, method)(request)
            self.assertEqual(self.ledger.snapshot(), before)
            self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), manifest_before)
        self.assertNotEqual(correct['operation_id'], foreign['operation_id'])
        self.assertEqual(self.job()['attempts'], 0)

    def test_repaired_derivation_hash_excludes_retained_mirror_source(self):
        profile = small_profile()
        profile['states'][1]['allowed_derivations'] = [dict(kind='horizontal-mirror',
            source='rest', requires_explicit_approval=True)]
        hatch.atomic(self.profile, profile)
        self.root = self.make_run('derived-inputs')
        self.bind(self.root)
        self.native = NativeAcquisitionGateway(self.root, asset_kind='raster')
        self.adapter = AcquisitionGateway(self.native, self.root, 'jump')
        self.reservation = None
        spec = dict(self.spec_document, logical_id='jump')
        self.spec = self.json_ref('jump-spec.json', spec, 'director-asset-spec')
        self.import_fixture_base_and_rest()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.derive(self.root, data, 'jump',
                'Synthetic fixture mirror; no production identity approval.', True)
            hatch.repair(self.root, data, 'jump', 'Synthetic fixture regeneration.')
        retained = self.job()
        expected = hatch.generation_inputs(self.root, retained)
        self.assertIn('decoded/rest.png', hatch.inputs(self.root, retained))
        self.assertNotIn('decoded/rest.png', expected)
        self.assertEqual(self.adapter.input_sha256(), object_hash(expected))
        self.reserve()
        self.assertEqual(self.job()['derivation_receipt'], retained['derivation_receipt'])
        token = self.claim()
        self.assert_active(token)
        self.assertEqual(self.job()['input_hashes'], expected)
        self.assertNotIn('derived_from', self.job())

    def test_enrolled_canonical_admission_cannot_invent_missing_reservation(self):
        before = self.ledger.snapshot()
        manifest_before = (self.root / 'imagegen-jobs.json').read_bytes()
        for method in ('prepare', 'claim', 'reconcile'):
            for identifier in (None, 'reservation-invented'):
                request = self.request(method)
                request['context']['budget_reservation_id'] = identifier
                with self.subTest(method=method, reservation=identifier), self.assertRaises(RigError):
                    getattr(self.adapter, method)(request)
                self.assertEqual(self.ledger.snapshot(), before)
                self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), manifest_before)
        self.assertEqual(self.job()['attempts'], 0)
        self.assertNotIn('budget_operation', self.job())

    def test_unknown_price_native_policy_requires_both_canonical_policies_to_agree(self):
        root = self.make_run('unknown-price', fixture=False)
        bind_native_budget(root, self.ledger.root, owner_root=self.owner,
            provider='imagegen', estimate=quantities(provider_calls=1, amount=None),
            retry_allowance=2, evidence=self.proof, unknown_price=True, unknown_price_cap=2)
        self.configure_paid(root, False)
        self.reserve()
        bounded_spec = self.spec
        unknown_policy = dict(self.paid_policy, paid='allow-unknown-price')
        unknown_spec_document = dict(self.spec_document, source_policy=unknown_policy)
        unknown_spec = self.json_ref('unknown-price-spec.json', unknown_spec_document,
            'director-asset-spec')
        before = self.ledger.snapshot()
        for spec, policy in ((bounded_spec, unknown_policy),
                             (unknown_spec, self.paid_policy)):
            request = self.request('claim')
            request['input']['spec'] = spec
            request['context']['policy'] = deepcopy(policy)
            with self.subTest(spec=spec['path']), self.assertRaises(RigError):
                self.adapter.claim(request)
            self.assertEqual(self.ledger.snapshot(), before)
            self.assertEqual(self.job()['attempts'], 0)
        self.spec = unknown_spec
        request = self.request('claim')
        request['context']['policy'] = unknown_policy
        result = self.adapter.claim(request)
        self.assertEqual(result['value']['native_state'], 'running')
        self.assertTrue(self.row()['unknown_price'])
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
