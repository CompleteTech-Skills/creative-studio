from unittest.mock import patch
from decimal import Decimal
import unittest
import hatch
from director_budget import NativeBudget
from test_director_budget_native import NativeBudgetTests, quantities

class InterruptedOwnerTests(unittest.TestCase):
    def test_pending_owner_usage_survives_generic_terminal_measurement(self):
        for terminal in ('complete','failed'):
            for owner_outcome in ('retry','uncertain'):
                with self.subTest(terminal=terminal,owner_outcome=owner_outcome):
                    f=NativeBudgetTests(); f.setUp()
                    try:
                        with patch.object(NativeBudget,'publish_claim',side_effect=OSError('synthetic interrupted publication')):
                            with self.assertRaises(OSError): f.claim()
                        job=hatch.job_for(hatch.manifest(f.root,recovery=True),'base'); token=job['token']
                        observation=f.observe('original-owner-pending',owner_outcome,calls=1,amount='.2' if owner_outcome=='retry' else None)
                        with hatch.ownership(f.root),patch.object(NativeBudget,'_apply',side_effect=OSError('synthetic interrupted observation accounting')):
                            with self.assertRaises(OSError): hatch.record_budget_usage(f.root,hatch.manifest(f.root,recovery=True),'base',token,observation)
                        before_journal=(f.root/'imagegen-jobs.json').read_bytes()
                        result=f.ledger.settle(job['budget_operation']['reservation_id'],usage=quantities(amount='.1'),outcome=terminal,expected_revision=f.ledger.snapshot()['sha256'],idempotency_key='independent-meter',evidence=f.proof)
                        self.assertEqual(before_journal,(f.root/'imagegen-jobs.json').read_bytes())
                        replay_error=None
                        try:
                            with hatch.ownership(f.root): hatch.reconcile(f.root,hatch.manifest(f.root,recovery=True))
                        except Exception as exc: replay_error=type(exc).__name__+': '+str(exc)
                        row=f.row()
                        print(dict(terminal=terminal,pending_owner_outcome=owner_outcome,direct_state=result['state'],recovery_error=replay_error,spent_calls=row['spent']['provider_calls'],spent_amount=row['spent']['amount'],held_amount=row['held']['amount']),flush=True)
                        self.assertIsNone(replay_error,'Durable original-owner usage must remain recoverable after independent measured delta')
                        self.assertEqual(Decimal(row['spent']['provider_calls']),1)
                    finally: f.doCleanups()

if __name__=='__main__': unittest.main(verbosity=2)
