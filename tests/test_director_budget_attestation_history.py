"""Offline recovery of a committed native cancellation and its attested history."""
import unittest
from unittest.mock import patch

import test_director_budget_legacy_cancellation as legacy


class AttestationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LegacyCancellationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.two_measured()
        self.journal = self.fixture.root / 'imagegen-jobs.json'
        save = legacy.hatch.save

        def interrupt_ack(root, data):
            event = self.fixture.job(data)['budget_operation']['events'][-1]
            if event['payload']['outcome'] == 'canceled-known' and 'receipt' in event:
                raise OSError('synthetic interruption before release acknowledgement')
            return save(root, data)

        with patch.object(legacy.hatch, 'save', interrupt_ack), self.assertRaisesRegex(OSError, 'acknowledgement'):
            self.fixture.release()
        self.data = legacy.hatch.manifest(self.fixture.root, recovery=True)
        self.job = self.fixture.job(self.data)
        event = self.job['budget_operation']['events'][-1]
        self.assertNotIn('receipt', event)
        ledger_event = self.fixture.ledger.snapshot()['state']['events'][-1]
        self.assertEqual((ledger_event['action'], ledger_event['idempotency_key']), ('release', event['key']))
        self.receipt = ledger_event['receipt']
        self.assertEqual(self.receipt['state'], 'released')

    def ledger_bytes(self):
        root = self.fixture.ledger.root
        return {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}

    def assert_replay_refused(self):
        legacy.hatch.save(self.fixture.root, self.data)
        before_journal, before_ledger = self.journal.read_bytes(), self.ledger_bytes()
        with patch.object(legacy.hatch, 'save', wraps=legacy.hatch.save) as save:
            with self.assertRaises(legacy.RigError) as refused:
                self.fixture.release()
            self.assertEqual(refused.exception.report['code'], 'DIRECTOR_BUDGET_BINDING')
            save.assert_not_called()
        self.assertEqual(self.journal.read_bytes(), before_journal)
        self.assertEqual(self.ledger_bytes(), before_ledger)

    def test_changed_failure_details_refuse_receipt_recovery(self):
        failure = next(entry for entry in self.job['history'] if entry['event'] == 'failed')
        failure['reason'] += ' (edited after release)'
        self.assert_replay_refused()

    def test_reordered_attempts_refuse_receipt_recovery(self):
        attempts = self.job['budget_operation']['attempts']
        self.assertEqual(len(attempts), 2)
        attempts.reverse()
        self.assert_replay_refused()

    def test_appended_attempt_refuses_receipt_recovery(self):
        self.job['budget_operation']['attempts'].append('3' * 32)
        self.assert_replay_refused()

    def assert_exact_recovery(self):
        before_ledger = self.ledger_bytes()
        self.assertEqual(self.fixture.release(), self.receipt)
        data = legacy.hatch.manifest(self.fixture.root, recovery=True)
        event = self.fixture.job(data)['budget_operation']['events'][-1]
        self.assertEqual(event['receipt'], self.receipt)
        acknowledged = self.journal.read_bytes()
        self.assertEqual(self.fixture.release(), self.receipt)
        self.assertEqual(self.journal.read_bytes(), acknowledged)
        self.assertEqual(self.ledger_bytes(), before_ledger)

    def test_original_history_recovers_exact_receipt(self):
        self.assert_exact_recovery()

    def test_repair_preserves_attested_history_and_recovers_exact_receipt(self):
        with legacy.hatch.ownership(self.fixture.root):
            data = legacy.hatch.manifest(self.fixture.root)
            legacy.hatch.repair(self.fixture.root, data, 'base', 'Synthetic repair after release')
        job = self.fixture.job(legacy.hatch.manifest(self.fixture.root, recovery=True))
        self.assertEqual((job['status'], job['attempts'], job['history'][-1]['event']), ('pending', 0, 'repair'))
        self.assert_exact_recovery()


if __name__ == '__main__':
    unittest.main()
