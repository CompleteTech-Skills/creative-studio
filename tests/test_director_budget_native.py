"""Offline native-owner accounting; synthetic evidence never implies provider execution."""
import argparse
from copy import deepcopy
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hatch
import production_workflow as workflow
from director_budget import BudgetLedger, NativeBudget, bind_native_budget, METRICS
from fixture_workflow import small_profile
from production_fixture import build_fixture_project
from rig_errors import RigError


def quantities(**values):
    return {key: values.get(key, 0) for key in METRICS}


def concurrent_claim(root):
    try:
        root = Path(root)
        with hatch.ownership(root):
            return hatch.claim(root, hatch.manifest(root), 'base')['token']
    except (ValueError, RigError):
        return None


def restarted_enrollment(root, ledger_root, options):
    try:
        return bind_native_budget(Path(root), Path(ledger_root), **options)
    except RigError as error:
        # Preserve the typed diagnostic across a fresh process; RigError itself
        # is not pickleable because its constructor has separate code/message.
        raise RuntimeError(f'{error.report["code"]}: {error}') from None


class NativeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.owner = Path(self.temporary.name).resolve()
        self.ledger = BudgetLedger.create(self.owner / 'ledger',
            quantities(provider_calls=6, amount=12),
            provider_limits={'imagegen': quantities(provider_calls=6, amount=12)}, currency='USD')
        evidence = self.owner / 'evidence.json'
        evidence.write_text('{"fixture":true,"source":"offline accounting scenario"}')
        self.proof = [self.ledger.retain_evidence(evidence)]
        self.root = self.prepare('run')
        self.bind(self.root)

    def prepare(self, name):
        root = self.owner / name
        root.parent.mkdir(parents=True, exist_ok=True)
        profile = self.owner / 'profile.json'
        hatch.atomic(profile, small_profile())
        hatch.prepare(root, argparse.Namespace(replace_inputs=False, fixture_mode=True,
            profile=str(profile), pet_name='Native budget fixture', pet_notes='No provider calls',
            reference=[], avoid=[], require=[], brand_source=[], style_preset='clay', style_notes='fixture'))
        return root

    def bind(self, root, **overrides):
        options = dict(owner_root=self.owner, provider='imagegen',
            estimate=quantities(provider_calls=1, amount=2), retry_allowance=2, evidence=self.proof)
        options.update(overrides)
        return bind_native_budget(root, self.ledger.root, **options)

    def claim(self, root=None):
        root = root or self.root
        with hatch.ownership(root):
            return hatch.claim(root, hatch.manifest(root), 'base')

    def observe(self, identity, outcome, *, calls=1, amount='0.5', resolves=None):
        return dict(id=identity, outcome=outcome, usage=quantities(provider_calls=calls, amount=amount),
                    evidence=self.proof, measurement_source='Synthetic measured usage delta',
                    resolves=resolves or [])

    def fail(self, token, observation):
        with hatch.ownership(self.root):
            return hatch.fail(self.root, hatch.manifest(self.root), 'base', token,
                              'Synthetic provider result', budget_usage=observation)

    def row(self, root=None):
        data = hatch.manifest(root or self.root)
        operation = hatch.job_for(data, 'base')['budget_operation']
        return self.ledger.snapshot()['state']['reservations'][operation['reservation_id']]

    def test_native_claim_owns_one_worst_case_reservation(self):
        first = self.claim()
        row = self.row()
        self.assertEqual(row['reserved'], {key: str(value) for key, value in quantities(provider_calls=3, amount=6).items()})
        self.assertEqual(first['budget_reservation_id'], row['id'])
        with self.assertRaises(ValueError):
            self.claim()  # Existing owner rejects another active claim.
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 1)

    def test_rate_limit_through_production_owner_preserves_measured_spend(self):
        project = self.owner / 'project'
        build_fixture_project(project)
        value = hatch.read(project / 'project.json')
        value['limits']['max_generation_calls'] = 3
        hatch.atomic(project / 'project.json', value)
        self.ledger = BudgetLedger.create(project / 'ledger',
            quantities(provider_calls=6, amount=12),
            provider_limits={'imagegen': quantities(provider_calls=6, amount=12)}, currency='USD')
        self.proof = [self.ledger.retain_evidence(self.owner / 'evidence.json')]
        root = self.prepare('project/acquisition/rate-limit')
        self.bind(root, owner_root=project)
        workflow.bind_generation_run(project, 'rate-limit', root, ['synthetic raster requirement'])
        first = workflow.claim_generation(project, 'rate-limit', 'base')
        rate = self.observe('http-429', 'retry', amount='0.25')
        workflow.fail_generation(project, 'rate-limit', 'base', first['token'],
                                 'Synthetic rate limit; measured charge retained', budget_usage=rate)
        second = workflow.claim_generation(project, 'rate-limit', 'base')
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(first['budget_reservation_id'], second['budget_reservation_id'])
        workflow.fail_generation(project, 'rate-limit', 'base', second['token'],
                                 'Synthetic known completion failure',
                                 budget_usage=self.observe('known-failure', 'failed', amount='0.75'))
        row = self.row(root)
        self.assertEqual(row['spent']['provider_calls'], '2')
        self.assertEqual(row['spent']['amount'], '1.00')
        self.assertTrue(all(value == '0' for value in row['held'].values()))
        self.assertEqual(workflow.generation_status(project)['calls_used'], 2)

    def test_uncertain_usage_keeps_token_and_requires_explicit_delta_reconciliation(self):
        token = self.claim()['token']
        unknown = self.observe('disconnected', 'uncertain', amount=None)
        self.fail(token, unknown)
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.row()['held']['amount'], '6')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], token)
        with self.assertRaises(RigError):
            self.fail(token, self.observe('not-resolved', 'retry', calls=0, amount='0.4'))
        self.fail(token, self.observe('resolved', 'retry', calls=0, amount='0.4', resolves=['disconnected']))
        self.assertEqual(self.row()['state'], 'reserved')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '0.4')
        self.assertNotEqual(self.claim()['token'], token)

    def test_missing_usage_and_changed_idempotency_payload_fail_without_clearing_token(self):
        token = self.claim()['token']
        with hatch.ownership(self.root), self.assertRaises(RigError):
            hatch.fail(self.root, hatch.manifest(self.root), 'base', token, 'No measured receipt')
        observation = self.observe('failure', 'failed')
        self.fail(token, observation)
        before = self.ledger.snapshot()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            hatch.record_budget_usage(self.root, data, 'base', token, observation)
            changed = deepcopy(observation)
            changed['usage']['amount'] = 1
            with self.assertRaises(RigError):
                hatch.record_budget_usage(self.root, data, 'base', token, changed)
        self.assertEqual(self.ledger.snapshot(), before)

    def test_cancellation_releases_only_known_remainder_and_preserves_usage(self):
        token = self.claim()['token']
        self.fail(token, self.observe('retry', 'retry', amount='0.5'))
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            result = budget.release(hatch.job_for(data, 'base'), idempotency_key='cancel',
                                    outcome='canceled-known', evidence=self.proof)
            self.assertEqual(result['spent']['amount'], '0.5')
            self.assertEqual(budget.release(hatch.job_for(data, 'base'), idempotency_key='cancel',
                             outcome='canceled-known', evidence=self.proof), result)
        self.assertEqual(self.row()['state'], 'released')
        with self.assertRaises(RigError):
            self.claim()

    def test_shared_ledger_concurrent_claims_cannot_exceed_limit(self):
        roots = [self.root, self.prepare('second'), self.prepare('third')]
        for root in roots[1:]:
            self.bind(root)
        with ProcessPoolExecutor(max_workers=3) as workers:
            tokens = list(workers.map(concurrent_claim, map(str, roots)))
        self.assertGreaterEqual(sum(token is not None for token in tokens), 1)
        self.assertLessEqual(sum(token is not None for token in tokens), 2)
        rows = self.ledger.snapshot()['state']['reservations'].values()
        self.assertLessEqual(sum(int(row['held']['provider_calls']) for row in rows), 6)
        self.assertLessEqual(sum(float(row['held']['amount']) for row in rows), 12)

    def test_crash_after_reservation_before_native_claim_reuses_exact_token(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            job = hatch.job_for(data, 'base')
            budget = NativeBudget.for_run(self.root, data)
            token = budget.before_claim(job, hatch.inputs(self.root, job))
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'pending')
        reopened = self.claim()
        self.assertEqual(reopened['token'], token)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_crash_between_usage_intent_and_ledger_publication_reconciles_once(self):
        token = self.claim()['token']
        observation = self.observe('known-failure', 'failed')
        with patch.object(BudgetLedger, 'settle', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.fail(token, observation)
        self.assertEqual(self.row()['spent']['amount'], '0')
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.reconcile(self.root, data)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], token)
        self.fail(token, observation)
        self.assertEqual(self.row()['spent']['amount'], '0.5')

    def test_crash_after_ledger_before_native_finalize_retains_spend_and_ownership(self):
        token = self.claim()['token']
        observation = self.observe('known', 'failed')
        original = hatch.save
        def crash(root, data):
            if hatch.job_for(data, 'base')['status'] == 'failed':
                raise RuntimeError('synthetic crash before native finalization')
            return original(root, data)
        with patch.object(hatch, 'save', side_effect=crash), self.assertRaises(RuntimeError):
            self.fail(token, observation)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], token)
        self.fail(token, observation)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'failed')

    def test_actual_overrun_blocks_retry_and_repair_preserves_final_spend(self):
        token = self.claim()['token']
        self.fail(token, self.observe('overrun', 'retry', amount='6.5'))
        with self.assertRaises(RigError):
            self.claim()
        self.assertEqual(self.row()['spent']['amount'], '6.5')
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.repair(self.root, data, 'base', 'Synthetic repair preserves accounting')
        with self.assertRaises(RigError):
            self.claim()
        self.assertEqual(self.row()['spent']['amount'], '6.5')

    def test_legacy_run_has_no_ledger_and_keeps_existing_fail_retry_behavior(self):
        root = self.prepare('legacy')
        first = self.claim(root)
        with hatch.ownership(root):
            data = hatch.manifest(root)
            hatch.fail(root, data, 'base', first['token'], 'Legacy behavior remains explicit')
        second = self.claim(root)
        self.assertNotEqual(first['token'], second['token'])
        self.assertNotIn('director_budget', hatch.manifest(root))
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)

    def test_unknown_price_and_policy_rebinding_do_not_create_native_claim(self):
        root = self.prepare('unknown-price')
        self.bind(root, estimate=quantities(provider_calls=1, amount=None))
        with self.assertRaises(RigError):
            self.claim(root)
        self.assertEqual(hatch.job_for(hatch.manifest(root), 'base')['attempts'], 0)
        with self.assertRaises(RigError):
            self.bind(self.root, retry_allowance=1)

    def test_invalid_ingest_source_still_preserves_measured_provider_spend(self):
        claim = self.claim()
        receipt = claim['receipt_template']
        receipt['budget_usage'] = self.observe('provider-complete', 'complete')
        receipt_path = self.owner / 'receipt.json'
        hatch.atomic(receipt_path, receipt)
        source = self.owner / 'invalid.png'
        source.write_bytes(b'Explicit invalid synthetic image')
        with hatch.ownership(self.root), self.assertRaises((ValueError, OSError)):
            hatch.ingest(self.root, hatch.manifest(self.root), 'base', source, claim['token'],
                         'Synthetic malformed provider output', receipt_path, fixture=True)
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], claim['token'])
        self.fail(claim['token'], receipt['budget_usage'])
        self.assertEqual(self.row()['spent']['amount'], '0.5')

    def test_unstarted_cancellation_and_uncertain_active_cancellation(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            budget = NativeBudget.for_run(self.root, data)
            job = hatch.job_for(data, 'base')
            budget.before_claim(job, hatch.inputs(self.root, job))
            hatch.release_budget(self.root, data, 'base', idempotency_key='unstarted',
                                 outcome='unstarted', evidence=self.proof)
        self.assertEqual(self.row()['state'], 'released')
        self.assertEqual(self.row()['spent']['amount'], '0')
        other = self.prepare('uncertain')
        self.bind(other)
        token = self.claim(other)['token']
        with hatch.ownership(other):
            data = hatch.manifest(other)
            hatch.fail(other, data, 'base', token, 'Synthetic uncertain result',
                       budget_usage=self.observe('unknown', 'uncertain', amount=None))
            with self.assertRaises(RigError):
                hatch.release_budget(other, data, 'base', idempotency_key='unsafe',
                                     outcome='canceled-known', evidence=self.proof)
        self.assertEqual(self.row(other)['state'], 'uncertain')

    def test_missing_opt_in_policy_cannot_bypass_existing_accounting(self):
        self.claim()
        data = hatch.manifest(self.root)
        del data['director_budget']
        hatch.save(self.root, data)
        with hatch.ownership(self.root), self.assertRaises(ValueError):
            hatch.reconcile(self.root, hatch.manifest(self.root))

    def test_crash_after_ledger_reserve_before_native_association_is_reconciled(self):
        original = BudgetLedger.reserve
        def crash(ledger, **options):
            original(ledger, **options)
            raise RuntimeError('synthetic crash after reservation publication')
        with patch.object(BudgetLedger, 'reserve', crash), self.assertRaises(RuntimeError):
            self.claim()
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertNotIn('reservation_id', job['budget_operation'])
        result = self.claim()
        self.assertEqual(result['budget_reservation_id'], self.row()['id'])
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['attempts'], 1)

    def test_crash_before_reservation_does_not_publish_native_claim(self):
        with patch.object(BudgetLedger, 'reserve', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.claim()
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'pending')
        self.claim()
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def test_successful_native_ingest_retains_measured_usage_and_original_token_history(self):
        from PIL import Image
        claim = self.claim()
        receipt = claim['receipt_template']
        receipt['budget_usage'] = self.observe('complete', 'complete', amount='0.7')
        path = self.owner / 'complete-receipt.json'
        hatch.atomic(path, receipt)
        source = self.owner / 'synthetic.png'
        Image.new('RGBA', (96, 96), (30, 70, 160, 255)).save(source)
        with hatch.ownership(self.root):
            hatch.ingest(self.root, hatch.manifest(self.root), 'base', source, claim['token'],
                         'Explicit synthetic native ingestion', path, fixture=True)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'generated')
        self.assertEqual(self.row()['spent']['amount'], '0.7')
        self.assertEqual(self.row()['state'], 'settled')

    def test_new_native_outcome_replays_earlier_durable_usage_before_finalizing(self):
        token = self.claim()['token']
        earlier = self.observe('pending-retry', 'retry', amount='0.25')
        with patch.object(BudgetLedger, 'settle', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.fail(token, earlier)
        self.assertEqual(self.row()['spent']['amount'], '0')
        # No caller reconcile: the same native token now reports a further delta.
        self.fail(token, self.observe('later-final', 'failed', calls=0, amount='0.75'))
        row = self.row()
        self.assertEqual(row['spent']['provider_calls'], '1')
        self.assertEqual(row['spent']['amount'], '1.00')
        self.assertEqual(len(row['attempts']), 2)
        self.assertEqual(row['state'], 'settled')
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(len(job['budget_operation']['events']), 2)

    def test_substituted_reservation_is_rejected_before_charging_other_operation(self):
        token = self.claim()['token']
        other = self.prepare('other-operation')
        self.bind(other)
        self.claim(other)
        other_id = self.row(other)['id']
        data = hatch.manifest(self.root)
        hatch.job_for(data, 'base')['budget_operation']['reservation_id'] = other_id
        hatch.save(self.root, data)
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.fail(token, self.observe('wrong-reservation', 'failed'))
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.row(other)['spent']['amount'], '0')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], token)

    def test_old_retry_receipt_cannot_finalize_newer_uncertainty_after_reopen(self):
        token = self.claim()['token']
        retry = self.observe('R', 'retry', amount='0.25')
        original = hatch.save
        def crash(root, data):
            if hatch.job_for(data, 'base')['status'] == 'failed':
                raise RuntimeError('synthetic crash after retry accounting')
            return original(root, data)
        with patch.object(hatch, 'save', side_effect=crash), self.assertRaises(RuntimeError):
            self.fail(token, retry)
        self.fail(token, self.observe('U', 'uncertain', calls=0, amount=None))
        before = self.ledger.snapshot()
        # Accounting replay remains valid; it is not native finalization authority.
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            old = hatch.record_budget_usage(self.root, data, 'base', token, retry)
            self.assertEqual(old['state'], 'reserved')
        self.assertEqual(self.ledger.snapshot(), before)
        with self.assertRaises(RigError):
            self.fail(token, retry)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], token)
        self.assertEqual(self.row()['state'], 'uncertain')
        with self.assertRaises(ValueError):
            self.claim()
        self.fail(token, self.observe('resolved-U', 'retry', calls=0, amount='0.75', resolves=['U']))
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '1.00')
        self.assertNotEqual(self.claim()['token'], token)

    def test_one_project_rejects_second_ledger_before_opt_in_binding_or_claim(self):
        project = self.owner / 'single-ledger-project'
        build_fixture_project(project)
        value = hatch.read(project / 'project.json')
        value['limits']['max_generation_calls'] = 2
        hatch.atomic(project / 'project.json', value)
        first = self.prepare('single-ledger-project/acquisition/first')
        second = self.prepare('single-ledger-project/acquisition/second')
        limits = quantities(provider_calls=1, amount=2)
        a = BudgetLedger.create(project / 'ledger-a', limits,
                               provider_limits={'imagegen': limits}, currency='USD')
        b = BudgetLedger.create(project / 'ledger-b', limits,
                               provider_limits={'imagegen': limits}, currency='USD')
        def enroll(run, ledger):
            return bind_native_budget(run, ledger.root, owner_root=project, provider='imagegen',
                estimate=limits, retry_allowance=0,
                evidence=[ledger.retain_evidence(self.owner / 'evidence.json')])
        enroll(first, a)
        workflow.bind_generation_run(project, 'first', first, ['synthetic'])
        with self.assertRaises(RigError):
            enroll(second, b)
        self.assertNotIn('director_budget', hatch.manifest(second))
        self.assertEqual(a.snapshot()['state']['revision'], 0)
        self.assertEqual(b.snapshot()['state']['revision'], 0)
        with self.assertRaises(RigError):
            workflow.bind_generation_run(project, 'second', second, ['synthetic'])
        enroll(second, a)
        workflow.bind_generation_run(project, 'second', second, ['synthetic'])
        workflow.configure_generation_limits(project, concurrency=2)
        workflow.claim_generation(project, 'first', 'base')
        with self.assertRaisesRegex(RigError, 'hard limits'):
            workflow.claim_generation(project, 'second', 'base')
        self.assertEqual(len(a.snapshot()['state']['reservations']), 1)
        self.assertEqual(b.snapshot()['state']['revision'], 0)
        self.assertEqual(hatch.job_for(hatch.manifest(second), 'base')['attempts'], 0)

    def project_enrollment_fixture(self, name):
        project = self.owner / name
        build_fixture_project(project)
        value = hatch.read(project / 'project.json')
        value['limits']['max_generation_calls'] = 2
        hatch.atomic(project / 'project.json', value)
        runs = [self.prepare(f'{name}/acquisition/{label}') for label in ('first', 'second')]
        for label, run in zip(('first', 'second'), runs):
            workflow.bind_generation_run(project, label, run, ['synthetic enrollment requirement'])
        limits = quantities(provider_calls=2, amount=4)
        ledger = BudgetLedger.create(project / 'ledger', limits,
            provider_limits={'imagegen': limits}, currency='USD')
        proof = [ledger.retain_evidence(self.owner / 'evidence.json')]
        return project, runs, ledger, proof

    def test_prebound_fresh_runs_enroll_in_either_order_before_admission(self):
        for order in ((0, 1), (1, 0)):
            with self.subTest(order=order):
                project, runs, ledger, proof = self.project_enrollment_fixture(f'enrollment-{order[0]}')
                def enroll(index):
                    return bind_native_budget(runs[index], ledger.root, owner_root=project,
                        provider='imagegen', estimate=quantities(provider_calls=1, amount=2),
                        retry_allowance=0, evidence=proof)
                first = enroll(order[0])
                before = ledger.snapshot()
                manifests = [hatch.manifest(run) for run in runs]
                for index in (0, 1):
                    with self.assertRaises(RigError):
                        workflow.claim_generation(project, ('first', 'second')[index], 'base')
                    with hatch.ownership(runs[index]):
                        with self.assertRaises(RigError):
                            hatch.claim(runs[index], hatch.manifest(runs[index]), 'base')
                self.assertEqual([hatch.manifest(run) for run in runs], manifests)
                self.assertEqual(ledger.snapshot(), before)
                second = enroll(order[1])
                self.assertEqual(first['ledger_path'], second['ledger_path'])
                self.assertEqual(first['genesis'], second['genesis'])
                state = hatch.read(project / workflow.WORKFLOW_FILE)
                owner = hatch.read(project / 'director-budget-owner.json')
                self.assertEqual(state['director_budget'], {key: owner[key] for key in state['director_budget']})
                self.assertEqual(len(owner['enrollments']), 2)
                workflow.configure_generation_limits(project, concurrency=2)
                claims = [workflow.claim_generation(project, label, 'base') for label in ('first', 'second')]
                self.assertNotEqual(claims[0]['token'], claims[1]['token'])
                self.assertEqual(len(ledger.snapshot()['state']['reservations']), 2)

    def test_prebound_invalid_sibling_rejects_enrollment_without_mutation(self):
        for invalid in ('claimed', 'run-id', 'request', 'requirements', 'restored'):
            with self.subTest(invalid=invalid):
                project, runs, ledger, proof = self.project_enrollment_fixture(f'invalid-{invalid}')
                if invalid == 'claimed':
                    workflow.claim_generation(project, 'second', 'base')
                elif invalid in ('run-id', 'request', 'requirements'):
                    state = hatch.read(project / workflow.WORKFLOW_FILE)
                    binding = state['generation_bindings']['second']
                    field = {'run-id': 'run_id', 'request': 'request_sha256',
                             'requirements': 'requirements_hash'}[invalid]
                    binding[field] = 'changed' if invalid == 'run-id' else '0' * 64
                    hatch.atomic(project / workflow.WORKFLOW_FILE, state)
                else:
                    data = hatch.manifest(runs[1])
                    data['history'].append({'event': 'restored'})
                    hatch.save(runs[1], data)
                def inventory():
                    return {path.relative_to(project).as_posix(): path.read_bytes()
                            for path in project.rglob('*.json')}
                before = inventory()
                with self.assertRaises((RigError, ValueError)):
                    bind_native_budget(runs[0], ledger.root, owner_root=project, provider='imagegen',
                        estimate=quantities(provider_calls=1, amount=2), retry_allowance=0, evidence=proof)
                self.assertEqual(inventory(), before)

    def test_partial_project_enrollment_rejects_ledger_or_policy_mismatch_without_mutation(self):
        for invalid in ('wrong-ledger', 'enrolled-policy'):
            with self.subTest(invalid=invalid):
                project, runs, ledger, proof = self.project_enrollment_fixture(f'mismatch-{invalid}')
                options = dict(owner_root=project, provider='imagegen',
                    estimate=quantities(provider_calls=1, amount=2), retry_allowance=0, evidence=proof)
                bind_native_budget(runs[0], ledger.root, **options)
                target_ledger = ledger
                if invalid == 'wrong-ledger':
                    limits = quantities(provider_calls=2, amount=4)
                    target_ledger = BudgetLedger.create(project / 'other-ledger', limits,
                        provider_limits={'imagegen': limits}, currency='USD')
                    options['evidence'] = [target_ledger.retain_evidence(self.owner / 'evidence.json')]
                else:
                    data = hatch.manifest(runs[0])
                    data['director_budget']['request_sha256'] = '0' * 64
                    hatch.save(runs[0], data)
                def inventory():
                    return {path.relative_to(project).as_posix(): path.read_bytes()
                            for path in project.rglob('*.json')}
                before = inventory()
                with self.assertRaises(RigError):
                    bind_native_budget(runs[1], target_ledger.root, **options)
                self.assertEqual(inventory(), before)
                self.assertNotIn('director_budget', hatch.manifest(runs[1]))

    def interrupt_project_enrollment(self, name, interruption):
        project, runs, ledger, proof = self.project_enrollment_fixture(name)
        options = dict(owner_root=project, provider='imagegen',
            estimate=quantities(provider_calls=1, amount=2), retry_allowance=0, evidence=proof)
        if interruption == 'owner-only':
            target, attribute = workflow, '_save'
        else:
            target, attribute = hatch, 'save'
        original = getattr(target, attribute)
        def interrupted(path, data):
            if Path(path) == (project if interruption == 'owner-only' else runs[0]) and 'director_budget' in data:
                raise RuntimeError('synthetic interrupted project enrollment')
            return original(path, data)
        with patch.object(target, attribute, side_effect=interrupted), self.assertRaises(RuntimeError):
            bind_native_budget(runs[0], ledger.root, **options)
        self.assertNotIn('director_budget', hatch.manifest(runs[0]))
        self.assertEqual(len(hatch.read(project / 'director-budget-owner.json')['enrollments']), 1)
        return project, runs, ledger, options

    def assert_project_enrollment_blocks_claims(self, project, runs, ledger):
        before = ledger.snapshot()
        manifests = [hatch.manifest(run) for run in runs]
        for index, run in enumerate(runs):
            with self.assertRaises(RigError):
                workflow.claim_generation(project, ('first', 'second')[index], 'base')
            with hatch.ownership(run), self.assertRaises(RigError):
                hatch.claim(run, hatch.manifest(run), 'base')
        self.assertEqual([hatch.manifest(run) for run in runs], manifests)
        self.assertEqual(ledger.snapshot(), before)

    def test_interrupted_prebound_project_enrollment_replays_after_restart_in_either_order(self):
        for interruption in ('owner-only', 'project-published'):
            for order in ((0, 1), (1, 0)):
                with self.subTest(interruption=interruption, order=order):
                    project, runs, ledger, options = self.interrupt_project_enrollment(
                        f'replay-{interruption}-{order[0]}', interruption)
                    self.assert_project_enrollment_blocks_claims(project, runs, ledger)
                    with ProcessPoolExecutor(max_workers=1) as restarted:
                        restarted.submit(restarted_enrollment, runs[order[0]], ledger.root, options).result(timeout=20)
                    self.assert_project_enrollment_blocks_claims(project, runs, ledger)
                    with ProcessPoolExecutor(max_workers=1) as restarted:
                        restarted.submit(restarted_enrollment, runs[order[1]], ledger.root, options).result(timeout=20)
                    owner = hatch.read(project / 'director-budget-owner.json')
                    self.assertEqual(len(owner['enrollments']), 2)
                    self.assertEqual(hatch.manifest(runs[0])['director_budget']['genesis'],
                                     hatch.manifest(runs[1])['director_budget']['genesis'])
                    workflow.configure_generation_limits(project, concurrency=2)
                    for label in ('first', 'second'):
                        workflow.claim_generation(project, label, 'base')
                    self.assertEqual(len(ledger.snapshot()['state']['reservations']), 2)

    def test_explicit_reenrollment_cannot_erase_any_retained_native_admission(self):
        for stage in ('preclaim', 'running', 'released'):
            for changed_prompt in (False, True):
                with self.subTest(stage=stage, changed_prompt=changed_prompt):
                    project, runs, ledger, proof = self.project_enrollment_fixture(
                        f'admitted-rollback-{stage}-{changed_prompt}')
                    manifest_path = runs[0] / 'imagegen-jobs.json'
                    original = manifest_path.read_bytes()
                    options = dict(owner_root=project, provider='imagegen',
                        estimate=quantities(provider_calls=1, amount=2), retry_allowance=0,
                        evidence=proof)
                    for run in runs:
                        bind_native_budget(run, ledger.root, **options)
                    if stage in ('preclaim', 'released'):
                        original_claim = NativeBudget.before_claim
                        def stop_after_admission(budget, job, hashes):
                            original_claim(budget, job, hashes)
                            raise RuntimeError('synthetic interruption after retained admission')
                        with patch.object(NativeBudget, 'before_claim', stop_after_admission):
                            with self.assertRaises(RuntimeError):
                                workflow.claim_generation(project, 'first', 'base')
                        if stage == 'released':
                            operation = hatch.job_for(hatch.manifest(runs[0]), 'base')['budget_operation']
                            ledger.release(operation['reservation_id'], outcome='unstarted',
                                evidence=proof, idempotency_key='synthetic-released-admission',
                                expected_revision=ledger.snapshot()['sha256'])
                    else:
                        workflow.claim_generation(project, 'first', 'base')
                    before = ledger.snapshot()
                    owner_bytes = (project / 'director-budget-owner.json').read_bytes()
                    workflow_bytes = (project / workflow.WORKFLOW_FILE).read_bytes()
                    manifest_path.write_bytes(original)
                    if changed_prompt:
                        prompt = runs[0] / 'prompts/base-pet.md'
                        prompt.write_bytes(prompt.read_bytes() + b'\nSynthetic changed prompt after rollback.\n')
                    with self.assertRaisesRegex(RigError, 'Native admission already exists'):
                        bind_native_budget(runs[0], ledger.root, **options)
                    self.assertEqual(manifest_path.read_bytes(), original)
                    self.assertEqual(ledger.snapshot(), before)
                    self.assertEqual((project / 'director-budget-owner.json').read_bytes(), owner_bytes)
                    self.assertEqual((project / workflow.WORKFLOW_FILE).read_bytes(), workflow_bytes)
                    with self.assertRaises((ValueError, RigError)):
                        workflow.claim_generation(project, 'first', 'base')

    def test_interrupted_project_enrollment_rejects_changed_identity_ledger_or_policy(self):
        for interruption in ('owner-only', 'project-published'):
            for invalid in ('request', 'run', 'ledger', 'policy'):
                with self.subTest(interruption=interruption, invalid=invalid):
                    project, runs, ledger, options = self.interrupt_project_enrollment(
                        f'bad-replay-{interruption}-{invalid}', interruption)
                    if invalid == 'request':
                        request = hatch.read(runs[0] / 'pet_request.json')
                        request['pet_notes'] = 'Changed synthetic request after interruption'
                        hatch.atomic(runs[0] / 'pet_request.json', request)
                    elif invalid == 'run':
                        data = hatch.manifest(runs[0])
                        data['run_id'] = 'changed-run-id'
                        hatch.save(runs[0], data)
                    elif invalid == 'ledger':
                        limits = quantities(provider_calls=2, amount=4)
                        ledger = BudgetLedger.create(project / 'wrong-ledger', limits,
                            provider_limits={'imagegen': limits}, currency='USD')
                        options['evidence'] = [ledger.retain_evidence(self.owner / 'evidence.json')]
                    else:
                        options['retry_allowance'] = 1
                    before = {path.relative_to(project).as_posix(): path.read_bytes()
                              for path in project.rglob('*.json')}
                    with self.assertRaises((RigError, ValueError)):
                        bind_native_budget(runs[0], ledger.root, **options)
                    self.assertEqual({path.relative_to(project).as_posix(): path.read_bytes()
                                      for path in project.rglob('*.json')}, before)

    def test_completed_enrollment_authenticates_retained_policy_and_identity_digests(self):
        for invalid in ('retained-policy', 'native-policy', 'request-digest', 'ledger-digest'):
            with self.subTest(invalid=invalid):
                project, runs, ledger, proof = self.project_enrollment_fixture(f'digest-{invalid}')
                options = dict(owner_root=project, provider='imagegen',
                    estimate=quantities(provider_calls=1, amount=2), retry_allowance=0, evidence=proof)
                for run in runs:
                    bind_native_budget(run, ledger.root, **options)
                owner_path = project / 'director-budget-owner.json'
                if invalid == 'native-policy':
                    data = hatch.manifest(runs[0])
                    data['director_budget']['retry_allowance'] = 1
                    hatch.save(runs[0], data)
                else:
                    owner = hatch.read(owner_path)
                    if invalid == 'ledger-digest':
                        owner['genesis'] = '0' * 64
                    else:
                        entry = next(value for value in owner['enrollments']
                                     if value['run_id'] == hatch.manifest(runs[0])['run_id'])
                        entry['policy_sha256' if invalid == 'retained-policy' else 'request_sha256'] = '0' * 64
                    hatch.atomic(owner_path, owner)
                before = {path.relative_to(project).as_posix(): path.read_bytes()
                          for path in project.rglob('*.json')}
                with self.assertRaises(RigError):
                    bind_native_budget(runs[0], ledger.root, **options)
                with self.assertRaises(RigError):
                    workflow.claim_generation(project, 'first', 'base')
                with hatch.ownership(runs[0]), self.assertRaises(RigError):
                    hatch.claim(runs[0], hatch.manifest(runs[0]), 'base')
                self.assertEqual({path.relative_to(project).as_posix(): path.read_bytes()
                                  for path in project.rglob('*.json')}, before)

    def test_old_completed_owner_enrollment_remains_readable(self):
        owner_path = self.owner / 'director-budget-owner.json'
        owner = hatch.read(owner_path)
        for entry in owner['enrollments']:
            del entry['policy_sha256']
        hatch.atomic(owner_path, owner)
        before = owner_path.read_bytes()
        self.bind(self.root)
        self.assertIn('budget_reservation_id', self.claim())
        self.assertEqual(owner_path.read_bytes(), before)

    def test_old_incomplete_project_enrollment_fails_without_retained_policy_provenance(self):
        for interruption in ('owner-only', 'project-published'):
            with self.subTest(interruption=interruption):
                project, runs, ledger, options = self.interrupt_project_enrollment(
                    f'old-incomplete-{interruption}', interruption)
                owner_path = project / 'director-budget-owner.json'
                owner = hatch.read(owner_path)
                del owner['enrollments'][0]['policy_sha256']
                hatch.atomic(owner_path, owner)
                before = {path.relative_to(project).as_posix(): path.read_bytes()
                          for path in project.rglob('*.json')}
                for run in runs:
                    with self.assertRaisesRegex(RigError, 'lacks its retained policy digest'):
                        bind_native_budget(run, ledger.root, **options)
                self.assert_project_enrollment_blocks_claims(project, runs, ledger)
                self.assertEqual({path.relative_to(project).as_posix(): path.read_bytes()
                                  for path in project.rglob('*.json')}, before)

    def test_old_incomplete_standalone_enrollment_cannot_invent_policy_provenance(self):
        root = self.prepare('old-incomplete-standalone')
        original_save = hatch.save
        def interrupted(path, data):
            if Path(path) == root and 'director_budget' in data:
                raise RuntimeError('synthetic interrupted standalone enrollment')
            return original_save(path, data)
        with patch.object(hatch, 'save', side_effect=interrupted), self.assertRaises(RuntimeError):
            self.bind(root)
        owner_path = self.owner / 'director-budget-owner.json'
        owner = hatch.read(owner_path)
        entry = next(value for value in owner['enrollments'] if value['run_id'] == hatch.manifest(root)['run_id'])
        del entry['policy_sha256']
        hatch.atomic(owner_path, owner)
        before = owner_path.read_bytes(), self.ledger.snapshot(), hatch.manifest(root)
        with self.assertRaisesRegex(RigError, 'lacks its retained policy digest'):
            self.bind(root)
        with self.assertRaisesRegex(RigError, 'previously enrolled'):
            self.claim(root)
        self.assertEqual((owner_path.read_bytes(), self.ledger.snapshot(), hatch.manifest(root)), before)

    def test_production_claim_checks_persisted_project_ledger_association(self):
        project = self.owner / 'association-project'
        build_fixture_project(project)
        value = hatch.read(project / 'project.json')
        value['limits']['max_generation_calls'] = 1
        hatch.atomic(project / 'project.json', value)
        root = self.prepare('association-project/acquisition/run')
        limits = quantities(provider_calls=1, amount=2)
        ledger = BudgetLedger.create(project / 'ledger', limits,
                                    provider_limits={'imagegen': limits}, currency='USD')
        bind_native_budget(root, ledger.root, owner_root=project, provider='imagegen',
            estimate=limits, retry_allowance=0,
            evidence=[ledger.retain_evidence(self.owner / 'evidence.json')])
        workflow.bind_generation_run(project, 'run', root, ['synthetic'])
        state = hatch.read(project / workflow.WORKFLOW_FILE)
        state['director_budget']['ledger_path'] = 'different-ledger'
        hatch.atomic(project / workflow.WORKFLOW_FILE, state)
        with self.assertRaises(RigError):
            workflow.claim_generation(project, 'run', 'base')
        self.assertEqual(ledger.snapshot()['state']['revision'], 0)
        self.assertEqual(hatch.job_for(hatch.manifest(root), 'base')['attempts'], 0)

    def test_actual_portable_restore_cannot_duplicate_budgeted_native_authority(self):
        from portable_runs import archive_run, restore_run
        owner = self.owner / 'portable-owner'
        original = self.prepare('portable-owner/original')
        limits = quantities(provider_calls=1, amount=2)
        ledger = BudgetLedger.create(owner / 'ledger', limits,
                                    provider_limits={'imagegen': limits}, currency='USD')
        bind_native_budget(original, ledger.root, owner_root=owner, provider='imagegen',
            estimate=limits, retry_allowance=0,
            evidence=[ledger.retain_evidence(self.owner / 'evidence.json')])
        archive = self.owner / 'budgeted-run.zip'
        with hatch.ownership(original):
            archive_run(original, hatch.manifest(original), archive)
        restored = owner / 'restored'
        token = self.claim(original)['token']
        before = ledger.snapshot()
        with self.assertRaisesRegex(ValueError, 'budgeted acquisitions'):
            restore_run(archive, restored)
        self.assertFalse(restored.exists())
        self.assertEqual(ledger.snapshot(), before)
        self.assertEqual(hatch.job_for(hatch.manifest(original), 'base')['token'], token)
        rows = list(ledger.snapshot()['state']['reservations'].values())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reserved']['provider_calls'], '1')

    def test_actual_legacy_portable_restore_preserves_unbudgeted_behavior(self):
        from portable_runs import archive_run, restore_run
        original = self.prepare('legacy-portable')
        archive = self.owner / 'legacy-run.zip'
        with hatch.ownership(original):
            archive_run(original, hatch.manifest(original), archive)
        restored = self.owner / 'legacy-restored'
        restore_run(archive, restored)
        first, second = self.claim(original), self.claim(restored)
        self.assertNotEqual(first['token'], second['token'])
        self.assertNotIn('director_budget', hatch.manifest(restored))
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)

    def test_same_path_preclaim_archive_cannot_reissue_outstanding_native_work(self):
        from portable_runs import archive_run, restore_run
        owner = self.owner / 'rollback-owner'
        original = self.prepare('rollback-owner/original')
        limits = quantities(provider_calls=1, amount=2)
        ledger = BudgetLedger.create(owner / 'ledger', limits,
                                    provider_limits={'imagegen': limits}, currency='USD')
        bind_native_budget(original, ledger.root, owner_root=owner, provider='imagegen',
            estimate=limits, retry_allowance=0,
            evidence=[ledger.retain_evidence(self.owner / 'evidence.json')])
        archive = self.owner / 'preclaim-rollback.zip'
        with hatch.ownership(original):
            archive_run(original, hatch.manifest(original), archive)
        token = self.claim(original)['token']
        before = ledger.snapshot()
        newer_bytes = (original / 'imagegen-jobs.json').read_bytes()
        preserved = owner / 'preserved-newer-original'
        original.rename(preserved)
        self.assertFalse(original.exists())
        # The old archive's canonical path matches, while external work may remain outstanding.
        with self.assertRaisesRegex(ValueError, 'budgeted acquisitions'):
            restore_run(archive, original)
        self.assertFalse(original.exists())  # No restored owner can return another handoff.
        self.assertEqual(ledger.snapshot(), before)
        self.assertEqual((preserved / 'imagegen-jobs.json').read_bytes(), newer_bytes)
        saved = hatch.read(preserved / 'imagegen-jobs.json')
        self.assertEqual(hatch.job_for(saved, 'base')['token'], token)
        rows = list(ledger.snapshot()['state']['reservations'].values())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['held']['provider_calls'], '1')

    def test_same_path_project_restore_cannot_rollback_budget_and_native_authority(self):
        project = self.owner / 'rollback-project'
        build_fixture_project(project)
        value = hatch.read(project / 'project.json')
        value['limits']['max_generation_calls'] = 1
        hatch.atomic(project / 'project.json', value)
        root = self.prepare('rollback-project/acquisition/run')
        limits = quantities(provider_calls=1, amount=2)
        ledger = BudgetLedger.create(project / 'ledger', limits,
                                    provider_limits={'imagegen': limits}, currency='USD')
        bind_native_budget(root, ledger.root, owner_root=project, provider='imagegen',
            estimate=limits, retry_allowance=0,
            evidence=[ledger.retain_evidence(self.owner / 'evidence.json')])
        workflow.bind_generation_run(project, 'run', root, ['synthetic'])
        archive = self.owner / 'preclaim-project.zip'
        workflow.archive_project(project, archive)
        token = workflow.claim_generation(project, 'run', 'base')['token']
        before = ledger.snapshot()
        newer_bytes = (root / 'imagegen-jobs.json').read_bytes()
        preserved = self.owner / 'preserved-newer-project'
        project.rename(preserved)
        with self.assertRaisesRegex(ValueError, 'budgeted projects'):
            workflow.restore_project(archive, project)
        self.assertFalse(project.exists())
        self.assertEqual(BudgetLedger(preserved / 'ledger').snapshot(), before)
        saved_path = preserved / 'acquisition/run/imagegen-jobs.json'
        self.assertEqual(saved_path.read_bytes(), newer_bytes)
        self.assertEqual(hatch.job_for(hatch.read(saved_path), 'base')['token'], token)
        self.assertEqual(len(before['state']['reservations']), 1)

    def test_existing_native_restore_marker_cannot_be_enrolled_into_budget_authority(self):
        from portable_runs import archive_run, restore_run
        original = self.prepare('restored-enrollment-source')
        archive = self.owner / 'unbudgeted-enrollment.zip'
        with hatch.ownership(original):
            archive_run(original, hatch.manifest(original), archive)
        restored = self.owner / 'restored-enrollment'
        restore_run(archive, restored)
        self.assertTrue(any(event['event'] == 'restored' for event in hatch.manifest(restored)['history']))
        before = self.ledger.snapshot()
        with self.assertRaisesRegex(RigError, 'Restored'):
            self.bind(restored)
        self.assertNotIn('director_budget', hatch.manifest(restored))
        self.assertEqual(self.ledger.snapshot(), before)


    def test_pre_enrollment_archive_cannot_remove_retained_owner_accounting_policy(self):
        from portable_runs import archive_run, restore_run
        owner = self.owner / 'pre-enrollment-owner'
        original = self.prepare('pre-enrollment-owner/original')
        archive = self.owner / 'before-budget-enrollment.zip'
        with hatch.ownership(original):
            archive_run(original, hatch.manifest(original), archive)
        limits = quantities(provider_calls=1, amount=2)
        ledger = BudgetLedger.create(owner / 'ledger', limits,
                                    provider_limits={'imagegen': limits}, currency='USD')
        bind_native_budget(original, ledger.root, owner_root=owner, provider='imagegen',
            estimate=limits, retry_allowance=0,
            evidence=[ledger.retain_evidence(self.owner / 'evidence.json')])
        token = self.claim(original)['token']
        before = ledger.snapshot()
        newer_bytes = (original / 'imagegen-jobs.json').read_bytes()
        owner_bytes = (owner / 'director-budget-owner.json').read_bytes()
        preserved = owner / 'preserved-enrolled-run'
        original.rename(preserved)
        restore_run(archive, original)
        restored = hatch.manifest(original)
        self.assertNotIn('director_budget', restored)
        self.assertEqual(restored['run_id'], hatch.read(preserved / 'imagegen-jobs.json')['run_id'])
        self.assertTrue(any(event['event'] == 'restored' for event in restored['history']))
        with self.assertRaisesRegex(RigError, 'previously enrolled'):
            self.claim(original)
        with self.assertRaisesRegex(RigError, 'previously enrolled'):
            NativeBudget.for_run(original, hatch.manifest(original))
        self.assertEqual(hatch.job_for(hatch.manifest(original), 'base')['attempts'], 0)
        self.assertEqual(ledger.snapshot(), before)
        self.assertEqual((owner / 'director-budget-owner.json').read_bytes(), owner_bytes)
        self.assertEqual((preserved / 'imagegen-jobs.json').read_bytes(), newer_bytes)
        self.assertEqual(hatch.job_for(hatch.read(preserved / 'imagegen-jobs.json'), 'base')['token'], token)
        self.assertEqual(len(before['state']['reservations']), 1)

    def test_interrupted_enrollment_blocks_legacy_claim_until_same_policy_replayed(self):
        root = self.prepare('interrupted-enrollment')
        original_save = hatch.save
        def interrupted(path, data):
            if Path(path) == root and 'director_budget' in data:
                raise RuntimeError('synthetic crash after owner enrollment')
            return original_save(path, data)
        with patch.object(hatch, 'save', side_effect=interrupted), self.assertRaises(RuntimeError):
            self.bind(root)
        self.assertNotIn('director_budget', hatch.manifest(root))
        before = self.ledger.snapshot()
        with self.assertRaisesRegex(RigError, 'previously enrolled'):
            self.claim(root)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(hatch.job_for(hatch.manifest(root), 'base')['attempts'], 0)
        self.bind(root)
        self.assertIn('budget_reservation_id', self.claim(root))
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
