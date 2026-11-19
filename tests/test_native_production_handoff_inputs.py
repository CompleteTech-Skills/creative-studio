"""Observe an ordinary successful offline production claim. All fixture bytes remain as prepared by the real owners."""
from pathlib import Path
from unittest.mock import patch
import unittest

import hatch
import production_workflow as workflow
import test_director_budget_native as fixture


class FinalProductionInputs(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    project_enrollment_fixture = fixture.NativeBudgetTests.project_enrollment_fixture

    def test_production_handoff_rechecks_admitted_inputs_through_its_receipt(self):
        project, runs, ledger, proof = self.project_enrollment_fixture('normal')
        options = dict(owner_root=project, provider='imagegen', estimate=fixture.quantities(provider_calls=1, amount=2),
                       retry_allowance=0, evidence=proof)
        for run in runs:
            fixture.bind_native_budget(run, ledger.root, **options)
        selected = runs[0]
        phase = ['before receipt']
        reads = []
        real_read, real_save = hatch.input_bytes, workflow._save

        def read(path):
            path = Path(path)
            if path.is_relative_to(selected):
                reads.append((phase[0], path.relative_to(selected).as_posix()))
            return real_read(path)

        def save(*args, **kwargs):
            result = real_save(*args, **kwargs)
            phase[0] = 'after receipt'
            return result

        with patch.object(hatch, 'input_bytes', read), patch.object(workflow, '_save', save):
            handoff = workflow.claim_generation(project, 'first', 'base')
        expected_prompt = Path(handoff['prompt']).relative_to(selected).as_posix()
        self.assertIn(expected_prompt, handoff['receipt_template']['input_hashes'])
        before = {name for stage, name in reads if stage == 'before receipt'}
        after = {name for stage, name in reads if stage == 'after receipt'}
        print('Published prompt was read before production receipt:', expected_prompt in before)
        print('All selected-run files read after production receipt:', sorted(after))
        self.assertIn('imagegen-jobs.json', after)
        self.assertIn(expected_prompt, after, 'The public production handoff reopens its journal but does not recheck its published prompt at this boundary.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
