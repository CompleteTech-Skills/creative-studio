"""Observe identity-check ordering in a normal budgeted local fixture claim."""
from contextlib import contextmanager
import os
from pathlib import Path
from unittest.mock import patch
import unittest
import native_journal
import test_director_budget_native as fixture

class CommitWindow(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim

    def test_original_directory_authority_at_ledger_publication(self):
        expected = self.root.stat()
        phase = ['before-writer']
        checked = []
        start = []
        published = []
        real_pin = native_journal.pinned_root
        real_change = fixture.BudgetLedger._change

        @contextmanager
        def observe(path):
            if Path(path) == self.root:
                checked.append(phase[0])
            with real_pin(path) as record:
                yield record

        def writer(ledger, expected_revision, key, action, payload, mutate):
            if action != 'native-claim':
                return real_change(ledger, expected_revision, key, action, payload, mutate)
            phase[0] = 'ledger-writer'
            for entry in Path('/proc/self/fd').iterdir():
                try:
                    if os.path.samestat(os.fstat(int(entry.name)), expected):
                        start.append(True)
                except OSError:
                    continue
            receipt = real_change(ledger, expected_revision, key, action, payload, mutate)
            published.append(receipt['token'])
            phase[0] = 'after-writer'
            return receipt

        with patch.object(native_journal, 'pinned_root', observe), patch.object(fixture.BudgetLedger, '_change', writer):
            handoff = self.claim()
        self.assertEqual(published, [handoff['token']])
        self.assertIn('before-writer', checked)
        print('Nondestructive live observations: phase of root-identity checks:', sorted(set(checked)),
              'run FD held entering claim ledger writer:', bool(start))
        self.assertTrue(start or 'ledger-writer' in checked or 'after-writer' in checked,
                        'Original directory authority ended before the ledger writer and was never checked through handoff.')

if __name__ == '__main__':
    unittest.main(verbosity=2)
