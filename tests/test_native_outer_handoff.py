"""Offline passive observability and positive accounting, using disposable fixtures."""
from collections import Counter
from decimal import Decimal
from pathlib import Path
import unittest
import hashlib
import sys
from unittest.mock import patch

import hatch
import production_workflow as production
import test_director_budget_native as setup

class SecurityReview(unittest.TestCase):
    setUp = setup.NativeBudgetTests.setUp
    prepare = setup.NativeBudgetTests.prepare
    bind = setup.NativeBudgetTests.bind
    project_enrollment_fixture = setup.NativeBudgetTests.project_enrollment_fixture
    claim = setup.NativeBudgetTests.claim
    observe = setup.NativeBudgetTests.observe
    fail_claim = setup.NativeBudgetTests.fail
    row = setup.NativeBudgetTests.row

    def test_exact_recovery_and_replay(self):
        handoff = self.claim()
        unit = '0.' + '0' * 63 + '1'
        self.fail_claim(handoff['token'], self.observe('known-fraction', 'retry', amount=unit))
        second = self.claim()
        self.assertNotEqual(second['token'], handoff['token'])
        self.assertEqual(second['budget_reservation_id'], handoff['budget_reservation_id'])
        event = self.observe('known-final', 'failed', amount=unit)
        self.fail_claim(second['token'], event)
        before = self.ledger.snapshot()['sha256']
        with hatch.ownership(self.root):
            hatch.record_budget_usage(self.root, hatch.manifest(self.root, recovery=True), 'base', second['token'], event)
        row = self.row()
        self.assertEqual(self.ledger.snapshot()['sha256'], before)
        self.assertEqual(Decimal(row['spent']['amount']), Decimal('2e-64'))
        self.assertEqual(row['spent']['provider_calls'], '2')
        self.assertTrue(all(Decimal(q) == 0 for q in row['held'].values()))
        print('2 observed synthetic calls, amount 2e-64, exact replay leaves ledger head unchanged.')

    def test_production_dispatch_input_read_order(self):
        project, runs, ledger, evidence = self.project_enrollment_fixture('project')
        for run in reversed(runs):
            setup.bind_native_budget(run, ledger.root, owner_root=project, provider='imagegen',
                    estimate=setup.quantities(provider_calls=1, amount=2), retry_allowance=0, evidence=evidence)
        selected = runs[0]
        open_names = Counter()
        observing = False
        def observe_open(event, args):
            if observing and event == 'open' and isinstance(args[0], (str, bytes)):
                raw = args[0].decode() if isinstance(args[0], bytes) else args[0]
                # os.open can report descriptor-relative leaf names.
                open_names[Path(raw).name] += 1
        sys.addaudithook(observe_open)
        reads = Counter()
        native_reads = Counter()
        saved = False
        actual_read = hatch.input_bytes
        actual_save = production._save
        def capture(path):
            p = Path(path)
            if p.is_relative_to(selected):
                (reads if saved else native_reads)[p.relative_to(selected).as_posix()] += 1
            return actual_read(path)
        def save(*args, **kwargs):
            nonlocal saved, observing
            result = actual_save(*args, **kwargs)
            saved = True
            observing = True
            return result
        with patch.object(hatch, 'input_bytes', capture), patch.object(production, '_save', save):
            handoff = production.claim_generation(project, 'first', 'base')
        observing = False
        self.assertTrue(saved)
        row = ledger.snapshot()['state']['reservations'][handoff['budget_reservation_id']]
        self.assertEqual(row['state'], 'reserved')
        self.assertTrue(all(Decimal(q) == 0 for q in row['spent'].values()))
        print('Durable ledger row is reserved, measured spend is zero in every metric.')
        self.assertEqual(Path(handoff['prompt']), selected / 'prompts/base-pet.md')
        baseline = hashlib.sha256(Path(handoff['prompt']).read_bytes()).hexdigest()
        self.assertEqual(baseline, handoff['receipt_template']['input_hashes']['prompts/base-pet.md'])
        print('Returned prompt names a mutable run file; ordinary unchanged bytes match the retained digest.')
        print('Interpreter audit open events mentioning prompt leaf after receipt and before return:', open_names['base-pet.md'])
        self.assertEqual(hatch.job_for(hatch.manifest(selected), 'base')['token'], handoff['token'])
        prompt = Path(handoff['prompt']).relative_to(selected).as_posix()
        print('Selected-run files actually read after production receipt and before return:', sorted(reads))
        print('Bound prompt reads before receipt:', native_reads[prompt], 'after receipt:', reads[prompt])
        self.assertGreater(native_reads[prompt], 0)
        self.assertGreater(reads['imagegen-jobs.json'], 0)
        self.assertGreater(reads[prompt], 0, 'Outer operation must verify captured dispatch input identity after its durable write')

if __name__ == '__main__':
    unittest.main(verbosity=2)
