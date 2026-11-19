"""Valid profile state names survive native and production claim publication."""
import argparse
from pathlib import Path
import tempfile
import unittest

import hatch
import production_workflow as workflow
from director_budget import BudgetLedger, METRICS, bind_native_budget
from fixture_workflow import fixture_strip, small_profile
from production_fixture import build_fixture_project
from profile_spec import load_profile
from rig_errors import RigError
from source_interface import import_source


def quantities(calls, amount):
    return {metric: {'provider_calls': calls, 'amount': amount}.get(metric, 0)
            for metric in METRICS}


class NativeJobNameTests(unittest.TestCase):
    def check_claim(self, length, *, production, budgeted):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        owner = home / 'project' if production else home
        if production:
            build_fixture_project(owner)
            project = hatch.read(owner / 'project.json')
            project['limits']['max_generation_calls'] = 2
            hatch.atomic(owner / 'project.json', project)
        run = owner / 'acquisition'
        name = ('quiet-' * length)[:length - 4] + 'idle'
        self.assertEqual(len(name), length)
        profile = small_profile()
        profile['states'][0]['id'] = name
        profile['events']['ready'] = name
        profile['export']['static_state'] = name
        profile_path = home / 'custom-profile.json'
        hatch.atomic(profile_path, load_profile(profile))
        hatch.prepare(run, argparse.Namespace(replace_inputs=False, fixture_mode=True,
            profile=str(profile_path), pet_name='Job name fixture', pet_notes='Offline',
            reference=[], avoid=[], require=[], brand_source=[], style_preset='clay', style_notes='Fixture'))
        if budgeted:
            ledger = BudgetLedger.create(owner / 'ledger', quantities(4, 4),
                provider_limits={'offline': quantities(4, 4)}, currency='USD')
            proof = home / 'policy.json'
            hatch.atomic(proof, {'fixture': True})
            bind_native_budget(run, ledger.root, owner_root=owner, provider='offline',
                estimate=quantities(1, 1), retry_allowance=1, evidence=[ledger.retain_evidence(proof)])
        if production:
            workflow.bind_generation_run(owner, 'fixture', run, ['Synthetic state artwork'])
        source = home / 'original.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(source)
        expected_source = source.read_bytes()
        descriptor = home / 'source.json'
        hatch.atomic(descriptor, dict(schema_version=1, kind='import', layout='image',
            files=[source.name], origin='Synthetic', fixture=True))
        with hatch.ownership(run):
            import_source(run, hatch.manifest(run), 'base', descriptor, 'Synthetic original')
            hatch.record_base_review(run, hatch.manifest(run), 'Synthetic base reviewed', True)
        before = hatch.manifest(run)
        base = hatch.job_for(before, 'base')
        self.assertEqual(base['import_receipt']['files'][0]['sha256'], hatch.digest(source))
        prior_history = hatch.job_for(before, name)['history']
        self.assertEqual(hatch.job_for(before, name)['status'], 'pending')
        if budgeted:
            self.assertEqual(ledger.snapshot()['state']['reservations'], {})

        try:
            if production:
                handoff = workflow.claim_generation(owner, 'fixture', name)
            else:
                with hatch.ownership(run):
                    handoff = hatch.claim(run, hatch.manifest(run), name)
        except (RigError, ValueError) as exc:
            # Record the original failed-after-write state in the predecessor
            # output without logging synthetic tokens or machine-local paths.
            current = hatch.manifest(run)
            refused = hatch.job_for(current, name)
            events = ledger.snapshot()['state'] if budgeted else {'events': [], 'reservations': {}}
            self.fail(f'Valid {length}-character state refused: {exc}. '
                      f'Status={refused["status"]}, active_token={bool(refused.get("token"))}, '
                      f'claim_records={sum(e["event"] == "claim" for e in refused["history"])}, '
                      f'ledger_actions={[e["action"] for e in events["events"]]}, '
                      f'held_calls={[row["held"]["provider_calls"] for row in events["reservations"].values()]}, '
                      f'base_retained={hatch.job_for(current, "base") == base}, '
                      f'original_retained={source.read_bytes() == expected_source}')

        current = hatch.manifest(run)
        row_job = hatch.job_for(current, name)
        token = handoff['token']
        self.assertEqual((handoff['job'], handoff['attempt']), (name, 1))
        self.assertEqual((row_job['id'], row_job['token'], row_job['status']), (name, token, 'running'))
        self.assertEqual(row_job['history'][:-1], prior_history)
        self.assertEqual((row_job['history'][-1]['event'], row_job['history'][-1]['token']), ('claim', token))
        self.assertEqual(sum(e['event'] == 'claim' for e in row_job['history']), 1)
        self.assertEqual(hatch.job_for(current, 'base'), base)
        self.assertEqual(source.read_bytes(), expected_source)
        self.assertEqual(handoff['receipt_template']['input_hashes'], row_job['input_hashes'])
        if production:
            self.assertEqual(workflow.generation_status(owner)['calls_used'], 1)
            self.assertEqual(handoff['production_context'], handoff['receipt_template']['production_context'])
        if budgeted:
            operation = row_job['budget_operation']
            state = ledger.snapshot()['state']
            reservation_id = handoff['budget_reservation_id']
            self.assertEqual(operation['attempts'], [token])
            self.assertEqual(set(state['reservations']), {reservation_id})
            self.assertEqual(operation['reservation_id'], reservation_id)
            self.assertEqual(handoff['budget_operation_id'], operation['operation_id'])
            self.assertEqual([e['action'] for e in state['events']], ['reserve', 'native-claim'])
            publication = state['events'][-1]['payload']
            self.assertEqual((publication['job'], publication['token'], publication['attempt']), (name, token, 1))
            self.assertEqual(publication['reservation_id'], reservation_id)
            self.assertEqual(publication['operation_id'], operation['operation_id'])
            self.assertEqual(publication['input_hashes'], row_job['input_hashes'])
            row = state['reservations'][reservation_id]
            self.assertEqual(row['held'], {k: str(v) for k, v in quantities(2, 2).items()})
            self.assertEqual(row['state'], 'reserved')
            self.assertTrue(all(v == '0' for v in row['spent'].values()))
        else:
            self.assertNotIn('budget_operation', row_job)
            self.assertNotIn('budget_reservation_id', handoff)

    def test_budgeted_native(self):
        for length in (128, 129):
            with self.subTest(length=length):
                self.check_claim(length, production=False, budgeted=True)

    def test_unbudgeted_native(self):
        for length in (128, 129):
            with self.subTest(length=length):
                self.check_claim(length, production=False, budgeted=False)

    def test_budgeted_production(self):
        for length in (128, 129):
            with self.subTest(length=length):
                self.check_claim(length, production=True, budgeted=True)

    def test_unbudgeted_production(self):
        for length in (128, 129):
            with self.subTest(length=length):
                self.check_claim(length, production=True, budgeted=False)


if __name__ == '__main__':
    unittest.main()
