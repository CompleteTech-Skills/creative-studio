from unittest.mock import patch
import unittest
from decimal import Decimal
import hatch
from director_budget import NativeBudget
from rig_errors import RigError
import test_director_budget_native as fixture

class OwnerControls(unittest.TestCase):
    def setUp(self):
        self.f=fixture.NativeBudgetTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
    def test_published_owner_retains_pending_usage_through_generic_complete(self):
        f=self.f; token=f.claim()['token']
        with hatch.ownership(f.root),patch.object(NativeBudget,'_apply',side_effect=OSError('synthetic accounting interruption')):
            with self.assertRaises(OSError): hatch.record_budget_usage(f.root,hatch.manifest(f.root,recovery=True),'base',token,f.observe('pending-owner','retry',calls=1,amount='.2'))
        result=f.ledger.settle(f.row()['id'],usage=fixture.quantities(amount='.1'),outcome='complete',expected_revision=f.ledger.snapshot()['sha256'],idempotency_key='independent-meter',evidence=f.proof)
        self.assertEqual(result['state'],'reserved')
        with hatch.ownership(f.root): hatch.reconcile(f.root,hatch.manifest(f.root,recovery=True))
        self.assertEqual(Decimal(f.row()['spent']['amount']),Decimal('.3')); self.assertEqual(f.row()['spent']['provider_calls'],'1')
    def test_unpublished_owner_with_generic_retry_preserves_pending_usage(self):
        f=self.f
        with patch.object(NativeBudget,'publish_claim',side_effect=OSError('synthetic publication interruption')):
            with self.assertRaises(OSError): f.claim()
        token=hatch.job_for(hatch.manifest(f.root,recovery=True),'base')['token']
        with hatch.ownership(f.root),patch.object(NativeBudget,'_apply',side_effect=OSError('synthetic accounting interruption')):
            with self.assertRaises(OSError): hatch.record_budget_usage(f.root,hatch.manifest(f.root,recovery=True),'base',token,f.observe('pending-owner','retry',calls=1,amount='.2'))
        f.ledger.settle(f.row()['id'],usage=fixture.quantities(amount='.1'),outcome='retry',expected_revision=f.ledger.snapshot()['sha256'],idempotency_key='independent-meter',evidence=f.proof)
        with hatch.ownership(f.root): hatch.reconcile(f.root,hatch.manifest(f.root,recovery=True))
        self.assertEqual(Decimal(f.row()['spent']['amount']),Decimal('.3')); self.assertEqual(f.row()['spent']['provider_calls'],'1')
    def test_published_owner_generic_release_refuses(self):
        f=self.f; f.claim(); before=f.ledger.snapshot()
        with self.assertRaises(RigError): f.ledger.release(f.row()['id'],outcome='canceled-known',expected_revision=before['sha256'],idempotency_key='independent-release',evidence=f.proof)
        self.assertEqual(f.ledger.snapshot(),before)
if __name__=='__main__': unittest.main(verbosity=2)
