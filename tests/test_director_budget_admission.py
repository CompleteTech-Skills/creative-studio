"""Offline reserve-before-claim contracts; no provider execution or artwork approval."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hatch
from director_budget import BudgetLedger, NativeBudget, bind_native_budget, METRICS
from fixture_workflow import small_profile, fixture_strip
from rig_errors import RigError


def quantities(**values):
    return {key: values.get(key, 0) for key in METRICS}


class NativeBudgetAdmissionTests(unittest.TestCase):
    """Use real synthetic native manifests, retained evidence and append-only ledgers."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.owner = Path(temporary.name).resolve()
        limits = quantities(provider_calls=6, amount=12)
        self.ledger = BudgetLedger.create(self.owner / 'ledger', limits,
            provider_limits={'imagegen': limits}, currency='USD')
        evidence = self.owner / 'evidence.json'
        evidence.write_text('{"fixture":true,"source":"offline reserve admission scenario"}',
                            encoding='utf-8')
        self.proof = [self.ledger.retain_evidence(evidence)]
        self.root = self.prepare('run')
        self.bind(self.root)

    def prepare(self, name):
        root = self.owner / name
        profile = self.owner / 'profile.json'
        hatch.atomic(profile, small_profile())
        hatch.prepare(root, argparse.Namespace(replace_inputs=False, fixture_mode=True,
            profile=str(profile), pet_name='Admission fixture', pet_notes='No provider calls',
            reference=[], avoid=[], require=[], brand_source=[], style_preset='clay',
            style_notes='Synthetic accounting fixture'))
        return root

    def bind(self, root, **changes):
        options = dict(owner_root=self.owner, provider='imagegen',
            estimate=quantities(provider_calls=1, amount=2), retry_allowance=2,
            evidence=self.proof)
        options.update(changes)
        return bind_native_budget(root, self.ledger.root, **options)

    def reserve(self, root=None, name='base'):
        root = root or self.root
        with hatch.ownership(root):
            return hatch.reserve_budget(root, hatch.manifest(root), name)

    def claim(self, root=None, name='base'):
        root = root or self.root
        with hatch.ownership(root):
            return hatch.claim(root, hatch.manifest(root), name)

    def files(self):
        result = {}
        for path in self.owner.rglob('*'):
            if not path.is_file():
                continue
            name = path.relative_to(self.owner).as_posix()
            if path.name.endswith('.hatch.lock'):
                # An active Windows owner holds a mandatory lock on byte zero.
                info = path.stat()
                result[name] = (info.st_size, info.st_mtime_ns)
            else:
                result[name] = path.read_bytes()
        return result

    def readonly_verify(self, budget, job, reservation_id, *, input_hashes=None,
                        rejects=False):
        """Fail on writer entry, as well as any changed manifest or persisted bytes."""
        before_files, before_data = self.files(), deepcopy(budget.data)
        with ExitStack() as stack:
            for owner, name in ((BudgetLedger, 'reserve'), (BudgetLedger, 'settle'),
                                (BudgetLedger, 'release'), (BudgetLedger, '_publish'),
                                (BudgetLedger, 'retain_evidence'), (NativeBudget, '_save'),
                                (hatch, 'save')):
                stack.enter_context(patch.object(owner, name,
                    side_effect=AssertionError('verification entered writer: ' + name)))
            if rejects:
                with self.assertRaises(RigError):
                    budget.verify_reservation(job, reservation_id, input_hashes=input_hashes)
                result = None
            else:
                result = budget.verify_reservation(job, reservation_id,
                                                   input_hashes=input_hashes)
        self.assertEqual(budget.data, before_data)
        self.assertEqual(self.files(), before_files)
        return result

    def descriptor(self, value, *, state='reserved', calls='3', amount='6'):
        snapshot = self.ledger.snapshot()
        self.assertIsInstance(value, dict)
        for key in ('reservation_id', 'operation_id', 'ledger_revision', 'state', 'reserved'):
            self.assertIn(key, value)
        row = snapshot['state']['reservations'][value['reservation_id']]
        self.assertEqual(value['operation_id'], row['operation_id'])
        self.assertEqual(value['ledger_revision'], snapshot['sha256'])
        self.assertEqual(value['state'], state)
        self.assertEqual(value['reserved'], row['reserved'])
        self.assertEqual(value['reserved'], {
            key: str(number) for key, number in
            quantities(provider_calls=calls, amount=amount).items()})
        return row

    def observation(self, identity, outcome, *, amount='0.5'):
        return dict(id=identity, outcome=outcome,
                    usage=quantities(provider_calls=1, amount=amount),
                    evidence=self.proof, measurement_source='Synthetic measured usage delta',
                    resolves=[])

    def test_reserve_reopen_verify_then_claim_preserves_one_reservation(self):
        before = hatch.manifest(self.root)
        original = deepcopy(hatch.job_for(before, 'base'))
        reserved = self.reserve()
        self.descriptor(reserved)
        data = hatch.manifest(self.root)
        job = hatch.job_for(data, 'base')
        for key in ('status', 'attempts', 'token', 'history'):
            self.assertEqual(job.get(key), original.get(key))
        self.assertEqual(job['budget_operation']['attempts'], [])
        with hatch.ownership(self.root):
            reopened = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, reopened)
            owned = hatch.job_for(reopened, 'base')
            self.assertEqual(self.readonly_verify(budget, owned, reserved['reservation_id'],
                input_hashes=hatch.inputs(self.root, owned)), reserved)
        self.assertEqual(self.reserve(), reserved)
        claimed = self.claim()
        self.assertEqual(claimed['budget_reservation_id'], reserved['reservation_id'])
        self.assertEqual(claimed['budget_operation_id'], reserved['operation_id'])
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['budget_operation']['attempts'], [claimed['token']])
        self.assertEqual([e['token'] for e in job['history'] if e['event'] == 'claim'],
                         [claimed['token']])
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 1)

    def test_reserve_only_never_allocates_pending_native_token(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            original = deepcopy(job)
            first = budget.reserve_only(job, hatch.inputs(self.root, job))
            second = budget.reserve_only(job, hatch.inputs(self.root, job))
            self.assertEqual(first, second)
            self.descriptor(first)
            self.assertEqual(job['budget_operation']['attempts'], [])
            for key in ('status', 'attempts', 'token', 'history'):
                self.assertEqual(job.get(key), original.get(key))
        claimed = self.claim()
        self.assertEqual(claimed['budget_reservation_id'], first['reservation_id'])

    def test_reserve_only_preserves_pending_before_claim_token_after_reopen(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            pending = budget.before_claim(job, hatch.inputs(self.root, job))
            identity = job['budget_operation']['reservation_id']
        reserved = self.reserve()
        self.assertEqual(reserved['reservation_id'], identity)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 0)
        self.assertEqual(job['budget_operation']['attempts'], [pending])
        self.assertEqual(self.claim()['token'], pending)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 1)

    def test_claim_after_evidenced_retry_consumes_same_prior_reservation(self):
        reserved = self.reserve()
        first = self.claim()
        with hatch.ownership(self.root):
            hatch.fail(self.root, hatch.manifest(self.root), 'base', first['token'],
                       'Synthetic retry retains measured expenditure',
                       budget_usage=self.observation('retry', 'retry'))
        before = self.ledger.snapshot()
        again = self.reserve()
        self.assertEqual(again['reservation_id'], reserved['reservation_id'])
        self.assertEqual(self.ledger.snapshot(), before)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['budget_operation']['attempts'], [first['token']])
        second = self.claim()
        self.assertNotEqual(second['token'], first['token'])
        self.assertEqual(second['budget_reservation_id'], reserved['reservation_id'])
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(len(before['state']['reservations']), 1)

    def test_verification_returns_current_revision_after_unrelated_reservation(self):
        first = self.reserve()
        other = self.prepare('other')
        self.bind(other)
        self.reserve(other)
        self.assertNotEqual(self.ledger.snapshot()['sha256'], first['ledger_revision'])
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            verified = self.readonly_verify(budget, hatch.job_for(data, 'base'),
                                            first['reservation_id'])
            self.descriptor(verified)
            self.assertEqual(verified['operation_id'], first['operation_id'])
            self.assertEqual(verified['reservation_id'], first['reservation_id'])

    def test_wrong_ids_other_jobs_other_runs_and_detached_records_cannot_authenticate(self):
        first = self.reserve()
        other = self.prepare('other')
        self.bind(other)
        second = self.reserve(other)
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            base = hatch.job_for(data, 'base')
            foreign = hatch.job_for(hatch.manifest(other), 'base')
            arbitrary = deepcopy(base)
            arbitrary['id'] = 'arbitrary-job'
            for job, identity in (
                (base, 'reservation-does-not-exist'),
                (base, second['reservation_id']),
                (hatch.job_for(data, 'rest'), first['reservation_id']),
                (foreign, first['reservation_id']),
                (foreign, second['reservation_id']),
                (deepcopy(base), first['reservation_id']),
                (arbitrary, first['reservation_id'])):
                with self.subTest(job=job['id'], reservation=identity):
                    self.readonly_verify(budget, job, identity, rejects=True)

    def test_absent_intent_verification_does_not_admit_or_create_evidence(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            self.readonly_verify(budget, hatch.job_for(data, 'base'),
                                 'reservation-does-not-exist', rejects=True)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        self.assertNotIn('budget_operation', hatch.job_for(hatch.manifest(self.root), 'base'))

    def test_unpublished_reservation_intent_cannot_be_repaired_by_verification(self):
        with patch.object(BudgetLedger, 'reserve',
                          side_effect=RuntimeError('synthetic pre-publication crash')):
            with self.assertRaises(RuntimeError):
                self.reserve()
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            self.assertIn('budget_operation', job)
            self.assertNotIn('reservation_id', job['budget_operation'])
            self.readonly_verify(budget, job, 'reservation-does-not-exist', rejects=True)
        self.descriptor(self.reserve())
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 1)

    def test_unacknowledged_real_reservation_is_not_associated_by_verification(self):
        original = BudgetLedger.reserve
        def crash(ledger, **options):
            original(ledger, **options)
            raise RuntimeError('synthetic post-publication crash')
        with patch.object(BudgetLedger, 'reserve', crash), self.assertRaises(RuntimeError):
            self.reserve()
        rows = self.ledger.snapshot()['state']['reservations']
        self.assertEqual(len(rows), 1)
        identity = next(iter(rows))
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            self.assertNotIn('reservation_id', job['budget_operation'])
            self.readonly_verify(budget, job, identity, rejects=True)
        reserved = self.reserve()
        self.assertEqual(reserved['reservation_id'], identity)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 1)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')
                         ['budget_operation']['attempts'], [])
        self.assertEqual(self.claim()['budget_reservation_id'], identity)

    def test_changed_inputs_are_refused_before_unpublished_intent_can_reserve(self):
        with patch.object(BudgetLedger, 'reserve',
                          side_effect=RuntimeError('synthetic pre-publication crash')):
            with self.assertRaises(RuntimeError):
                self.reserve()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            hashes = hatch.inputs(self.root, job)
            changed = {**hashes, '@generation-profile': '0' * 64}
            before = self.files()
            with self.assertRaises(RigError):
                budget.reserve_only(job, changed)
            self.assertEqual(self.files(), before)
            self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        self.descriptor(self.reserve())

    def test_changed_inputs_fail_reservation_and_authentication_without_new_ledger_row(self):
        reserved = self.reserve()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            changed = {**hatch.inputs(self.root, job), '@generation-profile': '0' * 64}
            self.readonly_verify(budget, job, reserved['reservation_id'],
                                 input_hashes=changed, rejects=True)
            before = self.files()
            with self.assertRaises(RigError):
                budget.reserve_only(job, changed)
            self.assertEqual(self.files(), before)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 1)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 0)

    def test_native_claim_refuses_prompt_changed_after_reservation(self):
        reserved = self.reserve()
        data = hatch.manifest(self.root)
        job = hatch.job_for(data, 'base')
        prompt = self.root / job['prompt_file']
        prompt.write_bytes(prompt.read_bytes() + b'Synthetic changed prompt.')
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.reserve()
        with self.assertRaises(RigError):
            self.claim()
        self.assertEqual(self.ledger.snapshot(), before)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 0)
        self.assertEqual(job['budget_operation']['attempts'], [])
        self.assertEqual(job['budget_operation']['reservation_id'], reserved['reservation_id'])

    def test_verification_authenticates_original_normalized_request_and_bound(self):
        reserved = self.reserve()
        with hatch.ownership(self.root):
            for field, replacement in (
                ('provider', 'different-provider'),
                ('estimate', quantities(provider_calls='1', amount='1')),
                ('retry_allowance', 0),
                ('idempotency_key', 'foreign-reserve-event')):
                data = hatch.manifest(self.root)
                budget = NativeBudget.for_run(self.root, data)
                job = hatch.job_for(data, 'base')
                # Tamper only the loaded record after loading a valid owner.
                job['budget_operation']['reservation_request'][field] = replacement
                with self.subTest(field=field):
                    self.readonly_verify(budget, job, reserved['reservation_id'], rejects=True)

    def test_unknown_price_cap_authenticates_normalized_original_reserve_event(self):
        root = self.prepare('unknown-price')
        self.bind(root, estimate=quantities(provider_calls=1, amount=None),
                  unknown_price=True, unknown_price_cap='1.25')
        reserved = self.reserve(root)
        self.descriptor(reserved, amount='3.75')
        snapshot = self.ledger.snapshot()
        event = next(e for e in snapshot['state']['events']
                     if e['action'] == 'reserve')
        self.assertEqual(event['payload']['estimate']['amount'], '1.25')
        with hatch.ownership(root):
            data = hatch.manifest(root)
            budget = NativeBudget.for_run(root, data)
            job = hatch.job_for(data, 'base')
            self.assertIsNone(job['budget_operation']['reservation_request']['estimate']['amount'])
            self.assertEqual(self.readonly_verify(budget, job, reserved['reservation_id']), reserved)
        self.assertEqual(self.claim(root)['budget_reservation_id'], reserved['reservation_id'])

    def test_unknown_price_without_policy_does_not_allocate_claim_or_reservation(self):
        root = self.prepare('unpriced')
        self.bind(root, estimate=quantities(provider_calls=1, amount=None))
        with self.assertRaises(RigError):
            self.reserve(root)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        job = hatch.job_for(hatch.manifest(root), 'base')
        self.assertEqual(job['attempts'], 0)
        self.assertEqual(job.get('budget_operation', {}).get('attempts', []), [])

    def test_uncertain_and_settled_records_authenticate_but_cannot_admit_again(self):
        for outcome, expected, amount in (('uncertain', 'uncertain', None),
                                           ('failed', 'settled', '0.5')):
            with self.subTest(outcome=outcome):
                # Each case owns a distinct run but shares the real ledger.
                root = self.root if outcome == 'uncertain' else self.prepare('settled')
                if root != self.root:
                    self.bind(root)
                reserved = self.reserve(root)
                claimed = self.claim(root)
                with hatch.ownership(root):
                    data = hatch.manifest(root)
                    hatch.fail(root, data, 'base', claimed['token'],
                               'Synthetic final or uncertain accounting',
                               budget_usage=self.observation('result-' + outcome, outcome,
                                                             amount=amount))
                with hatch.ownership(root):
                    data = hatch.manifest(root)
                    budget = NativeBudget.for_run(root, data)
                    job = hatch.job_for(data, 'base')
                    verified = self.readonly_verify(budget, job, reserved['reservation_id'])
                    self.descriptor(verified, state=expected)
                    before = self.files()
                    with self.assertRaises(RigError):
                        budget.reserve_only(job, hatch.inputs(root, job))
                    with self.assertRaises(RigError):
                        budget.before_claim(job, hatch.inputs(root, job))
                    self.assertEqual(self.files(), before)
                with self.assertRaises((ValueError, RigError)):
                    self.reserve(root)
                with self.assertRaises((ValueError, RigError)):
                    self.claim(root)

    def test_released_record_authenticates_but_cannot_be_fresh_admission(self):
        reserved = self.reserve()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.release_budget(self.root, data, 'base', idempotency_key='unstarted',
                                 outcome='unstarted', evidence=self.proof)
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            self.descriptor(self.readonly_verify(budget, job, reserved['reservation_id']),
                            state='released')
            with self.assertRaises(RigError):
                budget.reserve_only(job, hatch.inputs(self.root, job))
        with self.assertRaises(RigError):
            self.claim()
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_reached_native_retry_call_and_unit_limits_create_no_new_reservation(self):
        for name, limits in (
            ('retry', {'max_attempts': 1}),
            ('calls', {'max_calls': 1}),
            ('units', {'estimate_units': 1, 'unit_budget': 1}),
        ):
            with self.subTest(limit=name):
                root = self.prepare('limit-' + name)
                self.bind(root, retry_allowance=0)
                with hatch.ownership(root):
                    hatch.configure_limits(root, hatch.manifest(root), argparse.Namespace(**limits))
                claimed = self.claim(root)
                with hatch.ownership(root):
                    hatch.fail(root, hatch.manifest(root), 'base', claimed['token'],
                               'Synthetic known retry records the consumed native call',
                               budget_usage=self.observation('retry-' + name, 'retry'))
                before = self.ledger.snapshot()
                with self.assertRaises(ValueError):
                    self.reserve(root)
                self.assertEqual(self.ledger.snapshot(), before)
                job = hatch.job_for(hatch.manifest(root), 'base')
                self.assertEqual(job['attempts'], 1)
                self.assertEqual(job['budget_operation']['attempts'], [claimed['token']])

    def test_active_native_concurrency_rejects_without_admitting_another_job(self):
        claimed = self.claim()
        before = self.ledger.snapshot()
        with self.assertRaisesRegex(ValueError, 'concurrency'):
            self.reserve(name='rest')
        self.assertEqual(self.ledger.snapshot(), before)
        data = hatch.manifest(self.root)
        self.assertEqual(hatch.job_for(data, 'base')['token'], claimed['token'])
        self.assertNotIn('budget_operation', hatch.job_for(data, 'rest'))

    def test_unmet_dependencies_reject_before_reservation(self):
        before = self.ledger.snapshot()
        with self.assertRaises(ValueError):
            self.reserve(name='rest')
        self.assertEqual(self.ledger.snapshot(), before)
        job = hatch.job_for(hatch.manifest(self.root), 'rest')
        self.assertEqual(job['attempts'], 0)
        self.assertNotIn('budget_operation', job)

    def test_missing_base_review_rejects_before_row_reservation(self):
        claimed = self.claim()
        receipt = claimed['receipt_template']
        receipt['budget_usage'] = self.observation('complete-base', 'complete')
        receipt_path = self.owner / 'base-receipt.json'
        hatch.atomic(receipt_path, receipt)
        source = self.owner / 'synthetic-base.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(source)
        with hatch.ownership(self.root):
            hatch.ingest(self.root, hatch.manifest(self.root), 'base', source, claimed['token'],
                         'Synthetic offline base; no visual review', receipt_path, fixture=True)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'generated')
        before = self.ledger.snapshot()
        with self.assertRaisesRegex(ValueError, 'base-review'):
            self.reserve(name='rest')
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertNotIn('budget_operation', hatch.job_for(hatch.manifest(self.root), 'rest'))

    def test_claim_rechecks_limits_changed_since_reservation(self):
        reserved = self.reserve()
        data = hatch.manifest(self.root)
        with hatch.ownership(self.root):
            hatch.configure_limits(self.root, data,
                argparse.Namespace(estimate_units=1, unit_budget=0.5))
        before = self.ledger.snapshot()
        with self.assertRaises(ValueError):
            self.claim()
        self.assertEqual(self.ledger.snapshot(), before)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['attempts'], 0)
        self.assertEqual(job['budget_operation']['attempts'], [])
        with hatch.ownership(self.root):
            hatch.configure_limits(self.root, data, argparse.Namespace(unit_budget=1))
        self.assertEqual(self.claim()['budget_reservation_id'], reserved['reservation_id'])

    def test_legacy_unenrolled_run_cannot_reserve_and_keeps_legacy_claim_behavior(self):
        root = self.prepare('legacy')
        before = self.ledger.snapshot()
        with self.assertRaises((ValueError, RigError)):
            self.reserve(root)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertNotIn('director_budget', hatch.manifest(root))
        claimed = self.claim(root)
        self.assertNotIn('budget_reservation_id', claimed)
        self.assertEqual(self.ledger.snapshot(), before)

    def test_interrupted_enrollment_cannot_fall_back_to_legacy_reservation(self):
        root = self.prepare('interrupted-enrollment')
        original = hatch.save
        def crash(path, data):
            if Path(path) == root and 'director_budget' in data:
                raise RuntimeError('synthetic crash after durable owner enrollment')
            return original(path, data)
        with patch.object(hatch, 'save', side_effect=crash), self.assertRaises(RuntimeError):
            self.bind(root)
        self.assertNotIn('director_budget', hatch.manifest(root))
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.reserve(root)
        with self.assertRaises(RigError):
            self.claim(root)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(hatch.job_for(hatch.manifest(root), 'base')['attempts'], 0)

    def test_repaired_imported_base_reserves_eventual_generation_inputs_without_erasing_provenance(self):
        from source_interface import import_source
        source = self.owner / 'synthetic-imported-base.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(source)
        descriptor = self.owner / 'synthetic-imported-base.json'
        hatch.atomic(descriptor, {
            'schema_version': 1, 'kind': 'import', 'layout': 'image',
            'files': [source.name], 'origin': 'Synthetic offline import admission fixture',
            'fixture': True})
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            import_source(self.root, data, 'base', descriptor,
                          'Synthetic import exercises the native owner; no artwork approval')
            hatch.repair(self.root, data, 'base',
                         'Synthetic transition from imported base to generation')
        data = hatch.manifest(self.root)
        original = deepcopy(hatch.job_for(data, 'base'))
        run_history = deepcopy(data['history'])
        self.assertEqual(original['status'], 'pending')
        self.assertEqual(original['source_origin'], 'import')
        self.assertIn('import_receipt', original)
        self.assertTrue(any(e['event'] == 'imported' for e in original['history']))
        self.assertTrue(any(e['event'] == 'repair' for e in original['history']))
        generation_job = deepcopy(original)
        for field in ('derived_from', 'derivation_receipt', 'import_receipt'):
            generation_job.pop(field, None)
        expected = hatch.inputs(self.root, generation_job)
        self.assertIn(original['prompt_file'], expected)
        self.assertNotEqual(hatch.inputs(self.root, original), expected)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        reserved = self.reserve()
        self.descriptor(reserved)
        reopened = hatch.manifest(self.root)
        pending = hatch.job_for(reopened, 'base')
        self.assertEqual({k: v for k, v in pending.items() if k != 'budget_operation'},
                         original)
        self.assertEqual(reopened['history'], run_history)
        self.assertEqual(pending['budget_operation']['input_hashes'], expected)
        self.assertEqual(pending['budget_operation']['attempts'], [])
        with hatch.ownership(self.root):
            budget = NativeBudget.for_run(self.root, reopened)
            self.assertEqual(self.readonly_verify(budget, pending,
                reserved['reservation_id'], input_hashes=expected), reserved)
        self.assertEqual(self.reserve(), reserved)
        still_pending = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual({k: v for k, v in still_pending.items() if k != 'budget_operation'},
                         original)
        claimed = self.claim()
        self.assertEqual(claimed['budget_reservation_id'], reserved['reservation_id'])
        self.assertEqual(claimed['budget_operation_id'], reserved['operation_id'])
        self.assertEqual(claimed['receipt_template']['input_hashes'], expected)
        generated = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(generated['input_hashes'], expected)
        self.assertEqual(generated['budget_operation']['input_hashes'], expected)
        self.assertEqual(generated['history'][:-1], original['history'])
        self.assertEqual(generated['history'][-1]['event'], 'claim')
        self.assertEqual(generated['history'][-1]['input_hashes'], expected)
        self.assertEqual(generated['budget_operation']['attempts'], [claimed['token']])
        self.assertEqual(generated['attempts'], 1)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 1)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_repaired_derived_row_reserves_eventual_generation_inputs_without_erasing_provenance(self):
        from source_interface import import_source
        profile = small_profile()
        profile['states'][1]['allowed_derivations'] = [{
            'kind': 'horizontal-mirror', 'source': 'rest',
            'requires_explicit_approval': True}]
        profile_path = self.owner / 'synthetic-derivation-profile.json'
        hatch.atomic(profile_path, profile)
        root = self.owner / 'derived-admission'
        hatch.prepare(root, argparse.Namespace(replace_inputs=False, fixture_mode=True,
            profile=str(profile_path), pet_name='Derived admission fixture',
            pet_notes='Synthetic sources; no provider calls', reference=[], avoid=[],
            require=[], brand_source=[], style_preset='clay',
            style_notes='Synthetic accounting fixture'))
        self.bind(root)
        base = self.owner / 'synthetic-derivation-base.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(base)
        strip = self.owner / 'synthetic-derivation-rest.png'
        fixture_strip(profile['states'][0], (96, 96)).save(strip)
        with hatch.ownership(root):
            data = hatch.manifest(root)
            for name, source, layout in (('base', base, 'image'), ('rest', strip, 'strip')):
                descriptor = self.owner / ('synthetic-derivation-' + name + '.json')
                hatch.atomic(descriptor, {
                    'schema_version': 1, 'kind': 'import', 'layout': layout,
                    'files': [source.name], 'origin': 'Synthetic offline derivation fixture',
                    'fixture': True})
                import_source(root, data, name, descriptor,
                              'Synthetic imported source for native derivation accounting')
                if name == 'base':
                    hatch.record_base_review(root, data,
                        'Synthetic calibration gate fixture; no production artwork approval',
                        accept_color_warning=True)
            hatch.derive(root, data, 'jump',
                         'Synthetic symmetric fixture permits mirroring to test native provenance',
                         approved=True)
            hatch.repair(root, data, 'jump',
                         'Synthetic transition from derived row to generation')
        data = hatch.manifest(root)
        original = deepcopy(hatch.job_for(data, 'jump'))
        run_history = deepcopy(data['history'])
        self.assertEqual(original['status'], 'pending')
        self.assertEqual(original['source_origin'], 'derived')
        self.assertEqual(original['derived_from'], 'rest')
        self.assertEqual(original['derivation_receipt']['kind'], 'horizontal-mirror')
        self.assertTrue(any(e['event'] == 'derived' for e in original['history']))
        self.assertTrue(any(e['event'] == 'repair' for e in original['history']))
        self.assertEqual(hatch.digest(root / original['output_path']), original['output_hash'])
        self.assertTrue(hatch.base_review_current(root, data))
        generation_job = deepcopy(original)
        for field in ('derived_from', 'derivation_receipt', 'import_receipt'):
            generation_job.pop(field, None)
        expected = hatch.inputs(root, generation_job)
        self.assertIn(original['prompt_file'], expected)
        self.assertIn(original['retry_prompt_file'], expected)
        self.assertNotIn('decoded/rest.png', expected)
        self.assertIn('decoded/rest.png', hatch.inputs(root, original))
        self.assertNotEqual(hatch.inputs(root, original), expected)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        reserved = self.reserve(root, name='jump')
        self.descriptor(reserved)
        reopened = hatch.manifest(root)
        pending = hatch.job_for(reopened, 'jump')
        self.assertEqual({k: v for k, v in pending.items() if k != 'budget_operation'},
                         original)
        self.assertEqual(reopened['history'], run_history)
        self.assertEqual(pending['budget_operation']['input_hashes'], expected)
        self.assertEqual(pending['budget_operation']['attempts'], [])
        with hatch.ownership(root):
            budget = NativeBudget.for_run(root, reopened)
            self.assertEqual(self.readonly_verify(budget, pending,
                reserved['reservation_id'], input_hashes=expected), reserved)
        self.assertEqual(self.reserve(root, name='jump'), reserved)
        still_pending = hatch.job_for(hatch.manifest(root), 'jump')
        self.assertEqual({k: v for k, v in still_pending.items() if k != 'budget_operation'},
                         original)
        claimed = self.claim(root, name='jump')
        self.assertEqual(claimed['budget_reservation_id'], reserved['reservation_id'])
        self.assertEqual(claimed['budget_operation_id'], reserved['operation_id'])
        self.assertEqual(claimed['receipt_template']['input_hashes'], expected)
        generated = hatch.job_for(hatch.manifest(root), 'jump')
        self.assertEqual(generated['input_hashes'], expected)
        self.assertEqual(generated['budget_operation']['input_hashes'], expected)
        self.assertEqual(generated['history'][:-1], original['history'])
        self.assertEqual(generated['history'][-1]['event'], 'claim')
        self.assertEqual(generated['history'][-1]['input_hashes'], expected)
        self.assertEqual(generated['budget_operation']['attempts'], [claimed['token']])
        self.assertEqual(generated['attempts'], 1)
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 1)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)


    def test_recovery_admission_rejects_before_replaying_or_mutating_any_state(self):
        for stage in ('fresh', 'reserved', 'pending-usage'):
            with self.subTest(stage=stage):
                if stage == 'reserved':
                    # Normal authority can still admit after a recovery refusal.
                    self.descriptor(self.reserve())
                elif stage == 'pending-usage':
                    claimed = self.claim()
                    with hatch.ownership(self.root), patch.object(BudgetLedger, 'settle',
                            side_effect=RuntimeError('synthetic pending usage publication')):
                        with self.assertRaises(RuntimeError):
                            hatch.record_budget_usage(self.root, hatch.manifest(self.root), 'base',
                                claimed['token'], self.observation('pending-admission', 'retry'))
                with hatch.ownership(self.root):
                    before_files, before_ledger = self.files(), self.ledger.snapshot()
                    data = hatch.manifest(self.root, recovery=True)
                    budget = NativeBudget.for_run(self.root, data, recovery=True)
                    job = hatch.job_for(data, 'base')
                    hashes = hatch.inputs(self.root, job)
                    before_data = deepcopy(data)
                    with ExitStack() as stack:
                        for owner, name in ((BudgetLedger, 'reserve'), (BudgetLedger, 'settle'),
                                (BudgetLedger, 'release'), (BudgetLedger, '_publish'),
                                (BudgetLedger, 'retain_evidence'), (NativeBudget, '_save'),
                                (hatch, 'save')):
                            stack.enter_context(patch.object(owner, name,
                                side_effect=AssertionError('recovery admission entered writer: ' + name)))
                        for entry in ('reserve_only', 'before_claim', 'reserve_budget'):
                            with self.subTest(entry=entry), self.assertRaises((ValueError, RigError)):
                                if entry == 'reserve_budget':
                                    hatch.reserve_budget(self.root, data, 'base')
                                else:
                                    getattr(budget, entry)(job, hashes)
                            self.assertEqual(data, before_data)
                            self.assertEqual(self.ledger.snapshot(), before_ledger)
                            self.assertEqual(self.files(), before_files)

    def test_recovery_verification_uses_original_anchor_without_replaying_pending_usage(self):
        reserved = self.reserve()
        claimed = self.claim()
        with hatch.ownership(self.root), patch.object(BudgetLedger, 'settle',
                side_effect=RuntimeError('synthetic pending verification usage')):
            with self.assertRaises(RuntimeError):
                hatch.record_budget_usage(self.root, hatch.manifest(self.root), 'base',
                    claimed['token'], self.observation('pending-verification', 'retry'))
        for name in ('pet_request.json', 'profile.json', 'prompts/base-pet.md'):
            (self.root / name).unlink()
        with hatch.ownership(self.root):
            before_files, before_ledger = self.files(), self.ledger.snapshot()
            data = hatch.manifest(self.root, recovery=True)
            budget = NativeBudget.for_run(self.root, data, recovery=True)
            job = hatch.job_for(data, 'base')
            anchor = deepcopy(job['budget_operation'])
            with patch.object(hatch, 'inputs',
                    side_effect=AssertionError('current inputs are not verification authority')):
                for hashes in (None, anchor['input_hashes']):
                    self.assertEqual(self.readonly_verify(budget, job, reserved['reservation_id'],
                        input_hashes=hashes), reserved)
            self.assertEqual(self.files(), before_files)
            self.assertEqual(self.ledger.snapshot(), before_ledger)
            # Alter only the two published prompt hashes; operation, token and owner remain authentic.
            job['input_hashes'][job['prompt_file']] = '0' * 64
            latest = [event for event in job['history'] if event['event'] == 'claim'][-1]
            latest['input_hashes'][job['prompt_file']] = '0' * 64
            hatch.save(self.root, data)
            forged_files = self.files()
            with patch.object(hatch, 'inputs',
                    side_effect=AssertionError('current inputs are not a forgery oracle')):
                for hashes in (None, deepcopy(job['input_hashes'])):
                    self.readonly_verify(budget, job, reserved['reservation_id'],
                                         input_hashes=hashes, rejects=True)
            self.assertEqual(job['budget_operation'], anchor)
            self.assertEqual(job['token'], claimed['token'])
            self.assertEqual(self.ledger.snapshot(), before_ledger)
            self.assertEqual(self.files(), forged_files)



if __name__ == '__main__':
    unittest.main()
