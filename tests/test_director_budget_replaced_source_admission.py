"""A repaired explicit import can start its first generation under either owner."""
import argparse
from pathlib import Path
import tempfile
import unittest

import hatch
import production_workflow as workflow
from director_budget import BudgetLedger, METRICS, bind_native_budget
from fixture_workflow import fixture_strip, small_profile
from production_fixture import build_fixture_project
from rig_errors import RigError
from source_interface import import_source


def quantities(calls, amount):
    return {metric: {'provider_calls': calls, 'amount': amount}.get(metric, 0)
            for metric in METRICS}


class ReplacedSourceAdmissionTests(unittest.TestCase):
    def first_generation(self, *, budgeted, production):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        owner = home / 'project' if production else home
        if production:
            build_fixture_project(owner)
            project = hatch.read(owner / 'project.json')
            project['limits']['max_generation_calls'] = 2
            hatch.atomic(owner / 'project.json', project)
        run = owner / 'acquisition' / 'fixture'
        run.parent.mkdir(parents=True)
        profile = home / 'fixture-profile.json'
        hatch.atomic(profile, small_profile())
        hatch.prepare(run, argparse.Namespace(replace_inputs=False, fixture_mode=True,
            profile=str(profile), pet_name='Source replacement fixture', pet_notes='Offline only',
            reference=[], avoid=[], require=[], brand_source=[], style_preset='clay', style_notes='fixture'))
        if budgeted:
            ledger = BudgetLedger.create(owner / 'ledger', quantities(2, 4),
                provider_limits={'imagegen': quantities(2, 4)}, currency='USD')
            proof = home / 'synthetic-policy.json'
            hatch.atomic(proof, {'fixture': True})
            bind_native_budget(run, ledger.root, owner_root=owner, provider='imagegen',
                estimate=quantities(1, 2), retry_allowance=1,
                evidence=[ledger.retain_evidence(proof)])
        if production:
            workflow.bind_generation_run(owner, 'fixture', run, ['Synthetic base image'])

        source = home / 'explicit-base.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(source)
        original = source.read_bytes()
        descriptor = home / 'base-source.json'
        hatch.atomic(descriptor, {'schema_version': 1, 'kind': 'import', 'layout': 'image',
            'files': [source.name], 'origin': 'Synthetic review fixture', 'fixture': True})
        with hatch.ownership(run):
            import_source(run, hatch.manifest(run), 'base', descriptor, 'Explicit fixture import')
            imported = hatch.job_for(hatch.manifest(run), 'base')
            receipt = imported['import_receipt']
            self.assertEqual(receipt['files'][0]['sha256'], hatch.digest(source))
            hatch.repair(run, hatch.manifest(run), 'base', 'Request first generation after import')
        before = hatch.job_for(hatch.manifest(run), 'base')
        self.assertEqual(before['status'], 'pending')
        self.assertEqual(before['attempts'], 0)
        self.assertEqual(before['import_receipt'], receipt)
        self.assertEqual([entry['event'] for entry in before['history'] if
                          entry['event'] in ('imported', 'repair', 'claim')], ['imported', 'repair'])
        self.assertNotIn('budget_operation', before)
        if budgeted:
            self.assertEqual(ledger.snapshot()['state']['reservations'], {})

        # Setup errors remain errors. Refusing this first claim is the regression.
        try:
            if production:
                handoff = workflow.claim_generation(owner, 'fixture', 'base')
            else:
                with hatch.ownership(run):
                    handoff = hatch.claim(run, hatch.manifest(run), 'base')
        except (RigError, ValueError) as error:
            self.fail(f'First claim after explicit import and repair refused '
                      f'(production={production}, budgeted={budgeted}): {error}')

        current = hatch.job_for(hatch.manifest(run), 'base')
        token = handoff['token']
        self.assertRegex(token, r'^[0-9a-f]{32}$')
        self.assertEqual((current['status'], current['attempts'], current['token']), ('running', 1, token))
        self.assertEqual(handoff['attempt'], 1)
        self.assertEqual(handoff['receipt_template']['generation_skill'], '$fixture')
        self.assertEqual(handoff['receipt_template']['token'], token)
        self.assertEqual(current['input_hashes'], handoff['receipt_template']['input_hashes'])
        self.assertEqual(current['input_hashes'][current['prompt_file']], hatch.digest(Path(handoff['prompt'])))
        self.assertEqual(current['history'][:-1], before['history'])
        self.assertEqual(current['history'][-1]['event'], 'claim')
        self.assertEqual(current['history'][-1]['token'], token)
        self.assertEqual(next(e['receipt'] for e in current['history'] if e['event'] == 'imported'), receipt)
        self.assertNotIn('import_receipt', current)
        self.assertEqual(source.read_bytes(), original)
        if production:
            self.assertEqual(workflow.generation_status(owner)['calls_used'], 1)
        if budgeted:
            operation = current['budget_operation']
            reservation = handoff['budget_reservation_id']
            self.assertEqual(operation['attempts'], [token])
            self.assertEqual(operation['reservation_id'], reservation)
            self.assertEqual(operation['operation_id'], handoff['budget_operation_id'])
            rows = ledger.snapshot()['state']['reservations']
            self.assertEqual(set(rows), {reservation})
            row = rows[reservation]
            self.assertEqual(row['operation_id'], operation['operation_id'])
            self.assertEqual(row['state'], 'reserved')
            self.assertEqual(row['reserved'], {key: str(value) for key, value in quantities(2, 4).items()})
            self.assertEqual(row['held'], row['reserved'])
            self.assertTrue(all(value == '0' for value in row['spent'].values()))
        else:
            self.assertNotIn('budget_operation', current)
            self.assertNotIn('budget_reservation_id', handoff)

    def test_native_budgeted(self):
        self.first_generation(budgeted=True, production=False)

    def test_native_unbudgeted(self):
        self.first_generation(budgeted=False, production=False)

    def test_production_budgeted(self):
        self.first_generation(budgeted=True, production=True)

    def test_production_unbudgeted(self):
        self.first_generation(budgeted=False, production=True)
