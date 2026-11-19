"""Passive timing observers on ordinary fixture claims; no input or namespace substitutions."""
from pathlib import Path
import unittest
from unittest.mock import patch

import hatch
import native_journal
import production_workflow as workflow
import test_director_budget_native as fixture


class HandoffObservations(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    project_enrollment_fixture = fixture.NativeBudgetTests.project_enrollment_fixture

    def test_current_journal_rechecked_through_claim_handoff(self):
        real_change = fixture.BudgetLedger._change
        real_read = hatch.input_bytes
        phase = ['before commit']
        reads = []

        def change(ledger, revision, key, action, payload, mutate):
            result = real_change(ledger, revision, key, action, payload, mutate)
            if action == 'native-claim':
                phase[0] = 'after commit'
            return result

        def read(path):
            if Path(path) == self.root / 'imagegen-jobs.json':
                reads.append(phase[0])
            return real_read(path)

        with patch.object(fixture.BudgetLedger, '_change', change), patch.object(hatch, 'input_bytes', read):
            handoff = self.claim()
        self.assertEqual(handoff['token'], hatch.job_for(hatch.manifest(self.root), 'base')['token'])
        print('Actual current-journal reads by phase:', dict((p, reads.count(p)) for p in ('before commit', 'after commit')))
        self.assertIn('after commit', reads, 'Durable current journal was last read before the accounting commit')

    def test_project_owner_carries_one_run_witness_from_read_through_handoff(self):
        project, runs, ledger, proof = self.project_enrollment_fixture('project')
        opts = dict(owner_root=project, provider='imagegen', estimate=fixture.quantities(provider_calls=1, amount=2),
                    retry_allowance=0, evidence=proof)
        for run in runs:
            fixture.bind_native_budget(run, ledger.root, **opts)
        selected = runs[0]
        checks = []
        real_report = workflow._generation_report
        real_save = workflow._save

        def observe(stage):
            try:
                native_journal.admitted(selected)
            except ValueError:
                checks.append((stage, False))
            else:
                checks.append((stage, True))

        def report(*args, **kwargs):
            observe('production admission report')
            return real_report(*args, **kwargs)

        def save(*args, **kwargs):
            observe('production receipt save')
            return real_save(*args, **kwargs)

        with patch.object(workflow, '_generation_report', report), patch.object(workflow, '_save', save):
            result = workflow.claim_generation(project, 'first', 'base')
        self.assertEqual(result['token'], hatch.job_for(hatch.manifest(selected), 'base')['token'])
        print('Acquisition directory witness visible at production owner boundaries:', checks)
        self.assertEqual(checks, [('production admission report', True), ('production receipt save', True)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
