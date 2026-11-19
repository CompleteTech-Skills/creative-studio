from unittest.mock import patch
import unittest
import hatch
from director_budget import NativeBudget
import test_director_budget_native as fixture

class PendingReleaseTests(unittest.TestCase):
    def test_pending_native_observation_cannot_be_erased_by_generic_release(self):
        for outcome in ('unstarted','canceled-known'):
            with self.subTest(outcome=outcome):
                f=fixture.NativeBudgetTests(); f.setUp()
                try:
                    with patch.object(NativeBudget,'publish_claim',side_effect=OSError('synthetic interrupted publication')):
                        with self.assertRaises(OSError): f.claim()
                    job=hatch.job_for(hatch.manifest(f.root,recovery=True),'base'); token=job['token']
                    with hatch.ownership(f.root),patch.object(NativeBudget,'_apply',side_effect=OSError('synthetic interrupted observation accounting')):
                        with self.assertRaises(OSError): hatch.record_budget_usage(f.root,hatch.manifest(f.root,recovery=True),'base',token,f.observe('pending-native-unknown','uncertain',calls=1,amount=None))
                    result=f.ledger.release(job['budget_operation']['reservation_id'],outcome=outcome,expected_revision=f.ledger.snapshot()['sha256'],idempotency_key='independent-release',evidence=f.proof)
                    replay_error=None
                    try:
                        with hatch.ownership(f.root): hatch.reconcile(f.root,hatch.manifest(f.root,recovery=True))
                    except Exception as exc: replay_error=type(exc).__name__+': '+str(exc)
                    row=f.row()
                    print(dict(outcome=outcome,direct_state=result['state'],recovery_error=replay_error,spent_calls=row['spent']['provider_calls'],held_amount=row['held']['amount']),flush=True)
                    self.fail('Generic release accepted despite durable native unknown, and recovery outcome was '+str(replay_error))
                finally: f.doCleanups()
if __name__=='__main__': unittest.main(verbosity=2)
