"""Offline public production admission controls; no provider execution or mocks.

Stricter project limits are legitimate owner policy changes. They must persist
before native accounting compares its working state with the current journal.
Every scenario creates disposable projects, native runs and (when enabled) a
real ledger, and uses the production API for claims and usage reconciliation.
"""
import argparse
from pathlib import Path
import tempfile
import unittest

import hatch
import production_workflow as workflow
from director_budget import BudgetLedger, METRICS, bind_native_budget
from fixture_workflow import small_profile
from production_fixture import build_fixture_project
from rig_errors import RigError


def quantities(**values):
    return {key: values.get(key, 0) for key in METRICS}


class ProductionLimitAdmissionTests(unittest.TestCase):
    def scenario(self, *, budgeted, tightened):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        owner = Path(temporary.name).resolve()
        project = owner / 'project'
        build_fixture_project(project)
        config = hatch.read(project / 'project.json')
        config['limits']['max_generation_calls'] = 3
        hatch.atomic(project / 'project.json', config)
        profile = owner / 'profile.json'
        hatch.atomic(profile, small_profile())
        ledger, evidence = None, None
        if budgeted:
            ledger = BudgetLedger.create(project / 'ledger',
                quantities(provider_calls=6, amount=12),
                provider_limits={'imagegen': quantities(provider_calls=6, amount=12)},
                currency='USD')
            proof = owner / 'synthetic-usage.json'
            proof.write_text('{"fixture":true,"measurement":"synthetic offline delta"}',
                             encoding='utf-8')
            evidence = [ledger.retain_evidence(proof)]
        runs = {}
        for name in ('first', 'second') if tightened == 'concurrency' else ('first',):
            run = project / 'acquisition' / name
            run.parent.mkdir(parents=True, exist_ok=True)
            hatch.prepare(run, argparse.Namespace(replace_inputs=False, fixture_mode=True,
                profile=str(profile), pet_name='Production limit admission fixture',
                pet_notes='Offline fixture; no provider calls', reference=[], avoid=[], require=[],
                brand_source=[], style_preset='clay', style_notes='fixture'))
            # Establish the initial native policy before budget enrollment.
            # All later changes come through the public production owner.
            if tightened == 'concurrency':
                with hatch.ownership(run):
                    data = hatch.manifest(run)
                    data['limits']['concurrency'] = 2
                    hatch.save(run, data)
            if ledger is not None:
                bind_native_budget(run, ledger.root, owner_root=project, provider='imagegen',
                    estimate=quantities(provider_calls=1, amount=2), retry_allowance=2,
                    evidence=evidence)
            workflow.bind_generation_run(project, name, run, ['Synthetic raster requirement'])
            runs[name] = run
        if tightened == 'attempts':
            workflow.configure_generation_limits(project, attempts_per_job=1)
        run = runs['first']
        before = hatch.manifest(run)
        self.assertEqual(before['limits']['attempts_per_job'], 3)
        self.assertEqual(before['limits']['concurrency'], 2 if tightened == 'concurrency' else 1)
        self.assertEqual(hatch.job_for(before, 'base')['attempts'], 0)
        if ledger is not None:
            self.assertEqual(ledger.snapshot()['state']['reservations'], {})

        # A setup error remains an ERROR. Only refusal of this legitimate first
        # production claim becomes the explicit regression assertion below.
        try:
            first = workflow.claim_generation(project, 'first', 'base')
        except (RigError, ValueError) as error:
            code = getattr(error, 'report', {}).get('code', type(error).__name__)
            self.fail(f'Legitimate first claim refused (budgeted={budgeted}, '
                      f'tightened={tightened}): {code}: {error}')
        self.assertRegex(first['token'], r'^[0-9a-f]{32}$')
        current = hatch.manifest(run)
        job = hatch.job_for(current, 'base')
        self.assertEqual(current['limits']['attempts_per_job'], 1 if tightened == 'attempts' else 3)
        self.assertEqual(current['limits']['concurrency'], 1)
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['token'], first['token'])
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(workflow.generation_status(project)['calls_used'], 1)
        if ledger is not None:
            rows = ledger.snapshot()['state']['reservations']
            self.assertEqual(len(rows), 1)
            row = rows[first['budget_reservation_id']]
            self.assertEqual(job['budget_operation']['reservation_id'], row['id'])
            self.assertEqual(first['budget_operation_id'], job['budget_operation']['operation_id'])
            expected = {key: str(value) for key, value in quantities(provider_calls=3, amount=6).items()}
            self.assertEqual(row['reserved'], expected)
            self.assertEqual(row['held'], expected)
            self.assertTrue(all(value == '0' for value in row['spent'].values()))

        if tightened == 'concurrency':
            # A different ready acquisition makes this a real project-wide
            # concurrency assertion, not merely a duplicate active-token check.
            before_other = hatch.manifest(runs['second'])
            before_ledger = ledger.snapshot() if ledger is not None else None
            with self.assertRaisesRegex(ValueError, 'production generation concurrency bound'):
                workflow.claim_generation(project, 'second', 'base')
            self.assertEqual(hatch.manifest(runs['second']), before_other)
            self.assertEqual(hatch.manifest(run), current)
            if ledger is not None:
                self.assertEqual(ledger.snapshot(), before_ledger)
            return

        options = {}
        if ledger is not None:
            options['budget_usage'] = dict(id='synthetic-retry', outcome='retry',
                usage=quantities(provider_calls=1, amount='0.25'), evidence=evidence,
                measurement_source='Synthetic offline measured delta', resolves=[])
        workflow.fail_generation(project, 'first', 'base', first['token'],
                                 'Synthetic retryable failure; no provider called', **options)
        failed = hatch.manifest(run)
        self.assertEqual(hatch.job_for(failed, 'base')['status'], 'failed')
        self.assertEqual(failed['limits']['attempts_per_job'], 1 if tightened == 'attempts' else 3)
        before_retry_ledger = ledger.snapshot() if ledger is not None else None
        if ledger is not None:
            row = before_retry_ledger['state']['reservations'][first['budget_reservation_id']]
            self.assertEqual(row['spent']['provider_calls'], '1')
            self.assertEqual(row['spent']['amount'], '0.25')
            self.assertEqual(row['held']['provider_calls'], '2')
            self.assertEqual(row['held']['amount'], '5.75')
        if tightened == 'attempts':
            with self.assertRaisesRegex(ValueError, 'production retry bound'):
                workflow.claim_generation(project, 'first', 'base')
            self.assertEqual(hatch.manifest(run), failed)
            self.assertEqual(workflow.generation_status(project)['calls_used'], 1)
            if ledger is not None:
                self.assertEqual(ledger.snapshot(), before_retry_ledger)
        else:
            second = workflow.claim_generation(project, 'first', 'base')
            self.assertRegex(second['token'], r'^[0-9a-f]{32}$')
            self.assertNotEqual(second['token'], first['token'])
            self.assertEqual(hatch.job_for(hatch.manifest(run), 'base')['attempts'], 2)
            self.assertEqual(workflow.generation_status(project)['calls_used'], 2)
            if ledger is not None:
                self.assertEqual(second['budget_reservation_id'], first['budget_reservation_id'])
                rows = ledger.snapshot()['state']['reservations']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[second['budget_reservation_id']]['spent'], row['spent'])

    def test_unbudgeted_default_limits_allow_claim_and_retry(self):
        self.scenario(budgeted=False, tightened=None)

    def test_budgeted_default_limits_allow_claim_and_retry(self):
        self.scenario(budgeted=True, tightened=None)

    def test_unbudgeted_stricter_attempt_cap_persists_and_blocks_retry(self):
        self.scenario(budgeted=False, tightened='attempts')

    def test_budgeted_stricter_attempt_cap_persists_and_blocks_retry(self):
        self.scenario(budgeted=True, tightened='attempts')

    def test_unbudgeted_stricter_concurrency_cap_persists_and_blocks_second_run(self):
        self.scenario(budgeted=False, tightened='concurrency')

    def test_budgeted_stricter_concurrency_cap_persists_and_blocks_second_run(self):
        self.scenario(budgeted=True, tightened='concurrency')


if __name__ == '__main__':
    unittest.main()
