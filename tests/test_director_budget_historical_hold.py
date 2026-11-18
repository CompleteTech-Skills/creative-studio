"""Offline accounting invariant at the snapshot writer for a supported historical journal."""
import unittest
from unittest.mock import patch

import test_director_budget_interop_authority as support


class HistoricalMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.case = support.BudgetInteropAuthorityTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.reservation, self.first, self.second = self.case.legacy_retry_fixture(second_running=True)
        self.journal = self.case.root / 'imagegen-jobs.json'
        current = support.hatch.job_for(support.hatch.manifest(self.case.root), 'base')
        self.assertEqual((current['status'], current['token']), ('running', self.second))
        self.assertEqual([entry['event'] for entry in current['history'] if entry['event'] in ('claim', 'failed')],
                         ['claim', 'failed', 'claim'])
        self.assertEqual(current['budget_operation']['events'][-1]['payload']['token'], self.first)
        self.assertEqual(self.balances(self.case.row()), ('reserved', '1', '0.25', '2', '5.75'))

    @staticmethod
    def balances(row):
        return (row['state'], row['spent']['provider_calls'], row['spent']['amount'],
                row['held']['provider_calls'], row['held']['amount'])

    def test_direct_measurement_retains_active_historical_hold(self):
        before = self.case.ledger.snapshot()
        journal_bytes = self.journal.read_bytes()
        # Capture the actual writer's proposed snapshot without committing any closure.
        with patch.object(self.case.ledger, '_publish') as writer:
            self.case.generic_settle(self.reservation, 'complete', 'synthetic-direct-measurement',
                                     usage=support.quantities(amount='0.10'))
        writer.assert_called_once()
        proposed = writer.call_args.args[0]['reservations'][self.reservation]
        self.assertEqual(self.journal.read_bytes(), journal_bytes)
        self.assertEqual(self.case.ledger.snapshot(), before)
        self.assertEqual(self.balances(proposed), ('reserved', '1', '0.35', '2', '5.65'))

    def test_original_owner_can_commit_terminal_delta(self):
        self.case.fail(self.second, self.case.observe('synthetic-terminal-owner', 'failed', amount='0.75'))
        current = support.hatch.job_for(support.hatch.manifest(self.case.root), 'base')
        self.assertEqual(current['status'], 'failed')
        self.assertNotIn('token', current)
        self.assertEqual(self.balances(self.case.row()), ('settled', '2', '1.00', '0', '0'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
