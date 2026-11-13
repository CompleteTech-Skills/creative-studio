from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from director_budget import BudgetLedger, METRICS
from rig_errors import RigError


def quantities(value=0, **kwargs):
    result={key:value for key in METRICS};result.update(kwargs);return result


def reserve_child(args):
    root, revision, name = args
    try:
        receipt=BudgetLedger(root).reserve(operation_id=name,provider='local',estimate=quantities(provider_calls=6),
            retry_allowance=0,expected_revision=revision,idempotency_key=name,evidence=[])
        return receipt['state']
    except RigError as error:
        return error.report['code']


def die_before_publication(root):
    import os
    ledger=BudgetLedger(root)
    revision=ledger.snapshot()['sha256']
    ledger._publish=lambda state:os._exit(19)
    ledger.reserve(operation_id='interrupted',provider='local',estimate=quantities(provider_calls=3),
        retry_allowance=0,expected_revision=revision,idempotency_key='interrupted',evidence=[])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name).resolve()
        self.root=self.directory/'ledger'
        self.ledger=BudgetLedger.create(self.root,quantities(100),provider_limits={'local':quantities(10)},currency='USD')
        path=self.directory/'usage.json';path.write_text('{"provider_calls":1,"origin":"test-accounting-event"}')
        self.proof=[self.ledger.retain_evidence(path)]

    def reserve(self, **kwargs):
        options=dict(operation_id='op',provider='local',estimate=quantities(provider_calls=2),retry_allowance=1,
            expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='reserve',evidence=[])
        options.update(kwargs);return self.ledger.reserve(**options)

    def test_retries_reserved_and_duplicate_requests_return_original_receipt(self):
        before=self.ledger.snapshot()['sha256'];receipt=self.reserve()
        self.assertEqual(receipt['reserved']['provider_calls'],'4')
        self.assertEqual(self.reserve(expected_revision=before),receipt)
        with self.assertRaises(RigError):self.reserve(operation_id='different')
        with self.assertRaises(RigError):self.reserve(idempotency_key='second')

    def test_stale_revision_and_provider_bound_preserve_state(self):
        before=self.ledger.snapshot()['sha256'];self.reserve()
        current=self.ledger.snapshot()
        with self.assertRaisesRegex(RigError,'revision changed'):
            self.reserve(operation_id='other',idempotency_key='other',expected_revision=before)
        with self.assertRaisesRegex(RigError,'hard limits'):
            self.reserve(operation_id='other',idempotency_key='other',estimate=quantities(provider_calls=7),retry_allowance=0)
        self.assertEqual(self.ledger.snapshot(),current)

    def test_unknown_price_needs_policy_and_bounded_cap(self):
        with self.assertRaisesRegex(RigError,'Unknown-priced'):
            self.reserve(estimate=quantities(amount=None))
        receipt=self.reserve(estimate=quantities(amount=None),unknown_price=True,unknown_price_cap=2,evidence=self.proof)
        self.assertEqual(receipt['reserved']['amount'],'4')
        row=self.ledger.snapshot()['state']['reservations'][receipt['reservation_id']]
        self.assertTrue(row['unknown_price'])

    def test_uncertain_cannot_release_and_retry_consumption_is_retained(self):
        receipt=self.reserve();identifier=receipt['reservation_id']
        usage=quantities(provider_calls=1)
        uncertain=self.ledger.settle(identifier,usage=usage,outcome='uncertain',expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='unknown',evidence=self.proof)
        self.assertEqual(uncertain['held']['provider_calls'],'3')
        with self.assertRaisesRegex(RigError,'cannot be released'):
            self.ledger.release(identifier,outcome='canceled-known',expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='release',evidence=self.proof)
        final=self.ledger.settle(identifier,usage=quantities(provider_calls=1),outcome='failed',expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='final',evidence=self.proof)
        self.assertEqual(final['spent']['provider_calls'],'2');self.assertEqual(final['held']['provider_calls'],'0')
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations'][identifier]['attempts']),2)

    def test_unknown_usage_holds_full_unknown_dimensions(self):
        identifier=self.reserve()['reservation_id']
        receipt=self.ledger.settle(identifier,usage=quantities(provider_calls=None),outcome='uncertain',
            expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='unknown',evidence=self.proof)
        self.assertEqual(receipt['held']['provider_calls'],'4')
        with self.assertRaises(RigError):
            self.ledger.settle(identifier,usage=quantities(provider_calls=None),outcome='complete',
                expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='invalid',evidence=self.proof)

    def test_duplicate_settlement_cannot_charge_twice(self):
        identifier=self.reserve()['reservation_id'];revision=self.ledger.snapshot()['sha256']
        kwargs=dict(usage=quantities(provider_calls=1),outcome='complete',expected_revision=revision,idempotency_key='settle',evidence=self.proof)
        first=self.ledger.settle(identifier,**kwargs)
        self.assertEqual(self.ledger.settle(identifier,**kwargs),first)
        self.assertEqual(self.ledger.snapshot()['state']['reservations'][identifier]['spent']['provider_calls'],'1')

    def test_known_cancellation_releases_only_remainder(self):
        identifier=self.reserve()['reservation_id']
        self.ledger.settle(identifier,usage=quantities(provider_calls=1),outcome='retry',expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='attempt',evidence=self.proof)
        receipt=self.ledger.release(identifier,outcome='canceled-known',expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='cancel',evidence=self.proof)
        self.assertEqual(receipt['spent']['provider_calls'],'1')
        row=self.ledger.snapshot()['state']['reservations'][identifier]
        self.assertEqual(row['held']['provider_calls'],'0');self.assertEqual(row['state'],'released')

    def test_concurrent_reservations_never_overspend(self):
        revision=self.ledger.snapshot()['sha256']
        with ProcessPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(reserve_child,[(str(self.root),revision,'first'),(str(self.root),revision,'second')]))
        self.assertEqual(results.count('reserved'),1,results)
        state=self.ledger.snapshot()['state']
        self.assertEqual(len(state['reservations']),1)
        self.assertEqual(sum(int(r['held']['provider_calls']) for r in state['reservations'].values()),6)

    def test_interrupted_writer_does_not_publish_partial_state(self):
        from multiprocessing import Process
        before=self.ledger.snapshot()
        child=Process(target=die_before_publication,args=(str(self.root),));child.start();child.join(10)
        self.assertEqual(child.exitcode,19)
        self.assertEqual(self.ledger.snapshot(),before)
        self.reserve()
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']),1)

    def test_tampered_history_and_evidence_are_rejected(self):
        self.reserve()
        path=sorted(self.root.glob('*.json'))[-1];path.write_text(path.read_text().replace('reserved','released',1))
        with self.assertRaises(RigError):self.ledger.snapshot()

    def test_evidence_bytes_are_preserved_and_verified(self):
        identifier=self.reserve()['reservation_id']
        self.ledger.settle(identifier,usage=quantities(),outcome='complete',expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='done',evidence=self.proof)
        (self.root/self.proof[0]['path']).write_bytes(b'changed')
        with self.assertRaises(RigError):self.ledger.snapshot()

    def test_bad_inputs_and_symlink_destination_preserve_user_data(self):
        for value in [True,float('nan'),float('inf'),-1,'NaN']:
            with self.subTest(value=value),self.assertRaises(RigError):self.reserve(estimate=quantities(provider_calls=value))
        alias=self.directory/'alias'
        try:alias.symlink_to(self.root,target_is_directory=True)
        except OSError:self.skipTest('Symlink creation unavailable')
        with self.assertRaises(RigError):BudgetLedger(alias)

    def test_actual_overrun_is_recorded_and_blocks_more_work(self):
        identifier=self.reserve()['reservation_id']
        receipt=self.ledger.settle(identifier,usage=quantities(provider_calls=11),outcome='failed',
            expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='overrun',evidence=self.proof)
        self.assertTrue(receipt['over_budget'])
        self.assertEqual(receipt['spent']['provider_calls'],'11')
        self.assertEqual(self.ledger.snapshot()['state']['reservations'][identifier]['spent']['provider_calls'],'11')
        with self.assertRaisesRegex(RigError,'hard limits'):
            self.reserve(operation_id='next',idempotency_key='next',estimate=quantities(provider_calls=1),retry_allowance=0)

    def test_cumulative_actual_overrun_retains_every_accepted_delta(self):
        identifier=self.reserve()['reservation_id']
        self.ledger.settle(identifier,usage=quantities(provider_calls=10**15),outcome='retry',
            expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='large',evidence=self.proof)
        receipt=self.ledger.settle(identifier,usage=quantities(provider_calls=1),outcome='failed',
            expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='last',evidence=self.proof)
        self.assertEqual(receipt['spent']['provider_calls'],str(10**15+1))
        reopened=BudgetLedger(self.root).snapshot()['state']
        self.assertEqual(reopened['revision'],3)
        self.assertEqual(len(reopened['reservations'][identifier]['attempts']),2)
        self.assertEqual(reopened['reservations'][identifier]['spent']['provider_calls'],str(10**15+1))
        with self.assertRaisesRegex(RigError,'hard limits'):
            self.reserve(operation_id='next',idempotency_key='next',estimate=quantities(provider_calls=1))

    def test_director_request_adapter_matches_strict_contract(self):
        import json
        from director_budget import execute
        from director_contracts import validate_method_result
        root=Path(__file__).resolve().parents[1]
        request=next(d for d in json.loads((root/'examples/director-contracts/contracts.json').read_text())
            if d.get('kind')=='director-request' and d.get('method')=='BudgetLedger.reserve')
        request['input']['expected_ledger_revision']=self.ledger.snapshot()['sha256']
        request['input']['retry_allowance']=0
        request['input']['rate_evidence']=[]
        request['context']['policy']['providers']=['local']
        usage=request['input']['estimated_bound'];usage['classification']='estimated';usage['currency']='USD'
        for key in METRICS:usage[key]={'value':0,'explanation':None}
        usage['provider_calls']['value']=1
        result=execute(self.ledger,request,evidence_root=root)
        validate_method_result('BudgetLedger.reserve',result,request)
        self.assertEqual(result['value']['state'],'reserved')
        self.assertEqual(result['value']['remaining']['max_provider_calls'],99)

    def adapter_request(self, **metrics):
        import json
        root=Path(__file__).resolve().parents[1]
        request=next(d for d in json.loads((root/'examples/director-contracts/contracts.json').read_text())
            if d.get('kind')=='director-request' and d.get('method')=='BudgetLedger.reserve')
        request['input'].update(expected_ledger_revision=self.ledger.snapshot()['sha256'], retry_allowance=0, rate_evidence=[])
        usage=request['input']['estimated_bound'];usage['classification']='estimated';usage['currency']='USD'
        for key in METRICS:usage[key]={'value':metrics.get(key,0),'explanation':None}
        request['context']['policy']['providers']=['local']
        return request

    def test_adapter_denied_paid_and_unlisted_provider_preserve_revision(self):
        from director_budget import execute
        for paid,providers,amount in [('deny',['local'],1),('deny',[],0),('deny',['local'],None)]:
            with self.subTest(paid=paid,providers=providers,amount=amount):
                request=self.adapter_request(amount=amount)
                if amount is None:
                    request['input']['estimated_bound']['amount']['explanation']='Unknown price'
                    request['input']['estimated_bound']['currency']=None
                request['context']['policy'].update(paid=paid,providers=providers)
                before=self.ledger.snapshot()
                with self.assertRaisesRegex(RigError,'request'):
                    execute(self.ledger,request,evidence_root=self.root)
                self.assertEqual(self.ledger.snapshot(),before)

    def test_adapter_allows_explicit_bounded_paid_work_with_evidence(self):
        from director_budget import execute
        request=self.adapter_request(amount=1)
        proof=self.proof[0]
        reference=dict(artifact_id='rate-proof',kind='accounting-proof',schema_version=1,semantic_version='1.0.0',
            path=proof['path'],sha256=proof['sha256'],byte_size=(self.root/proof['path']).stat().st_size,
            media_type='application/json',provenance_ref=None,fixture=True,dependencies=[])
        request['context']['policy'].update(paid='bounded',policy_ref=reference)
        request['context']['budget_reservation_id']='outer-authorization'
        request['input']['rate_evidence']=[reference]
        result=execute(self.ledger,request,evidence_root=self.root)
        self.assertEqual(result['value']['state'],'reserved')
        row=next(iter(self.ledger.snapshot()['state']['reservations'].values()))
        self.assertEqual(row['reserved']['amount'],'1')

    def test_adapter_large_storage_and_call_limits_have_valid_result(self):
        from director_budget import execute
        self.ledger=BudgetLedger.create(self.directory/'large',quantities(3_000_000_000),
            provider_limits={'local':quantities(3_000_000_000)},currency='USD')
        result=execute(self.ledger,self.adapter_request(),evidence_root=self.root)
        self.assertEqual(result['value']['remaining']['max_storage_bytes'],3_000_000_000)
        self.assertEqual(result['value']['remaining']['max_provider_calls'],3_000_000_000)
        self.assertEqual(self.ledger.snapshot()['state']['revision'],1)

    def test_precise_reservation_aggregate_and_settlement(self):
        from decimal import localcontext
        value='1.00000000000000000000000000001'
        with localcontext() as context:
            context.prec=6  # Host application precision must not affect accounting.
            receipt=self.reserve(estimate=quantities(amount=value),retry_allowance=0,evidence=self.proof)
            self.assertEqual(receipt['reserved']['amount'],value)
            self.ledger.settle(receipt['reservation_id'],usage=quantities(amount=value),outcome='complete',
                expected_revision=self.ledger.snapshot()['sha256'],idempotency_key='precise',evidence=self.proof)
            self.assertEqual(self.ledger.snapshot()['state']['reservations'][receipt['reservation_id']]['spent']['amount'],value)
        ledger=BudgetLedger.create(self.directory/'exact',quantities(amount=1),
            provider_limits={'local':quantities(amount=1)},currency='USD')
        proof=[ledger.retain_evidence(self.root/self.proof[0]['path'])]
        for index,amount in enumerate(['0.5','0.50000000000000000000000000001']):
            options=dict(operation_id='op'+str(index),provider='local',estimate=quantities(amount=amount),
                retry_allowance=0,expected_revision=ledger.snapshot()['sha256'],idempotency_key='key'+str(index),evidence=proof)
            if index:
                before=ledger.snapshot()
                with self.assertRaisesRegex(RigError,'hard limits'):ledger.reserve(**options)
                self.assertEqual(ledger.snapshot(),before)
            else:ledger.reserve(**options)

    def test_decimal_scale_bound_and_unknown_zero_cap_fail_without_mutation(self):
        before=self.ledger.snapshot()
        for options in [dict(estimate=quantities(amount='1e-65'),evidence=self.proof),
                        dict(estimate=quantities(amount=None),unknown_price=True,unknown_price_cap=0,evidence=[]),
                        dict(estimate=quantities(amount=0),unknown_price=True,evidence=[])]:
            with self.assertRaises(RigError):self.reserve(**options)
            self.assertEqual(self.ledger.snapshot(),before)

    def test_strict_settle_release_adapters_preserve_fixture_evidence(self):
        import json
        from director_budget import execute
        from director_contracts import validate_method_result
        root=Path(__file__).resolve().parents[1]
        docs=json.loads((root/'examples/director-contracts/contracts.json').read_text())
        identifier=self.reserve()['reservation_id']
        reference=dict(artifact_id='usage-proof',kind='accounting-proof',schema_version=1,semantic_version='1.0.0',
            path=self.proof[0]['path'],sha256=self.proof[0]['sha256'],byte_size=(self.root/self.proof[0]['path']).stat().st_size,
            media_type='application/json',provenance_ref=None,fixture=True,dependencies=[])
        settle=next(d for d in docs if d.get('kind')=='director-request' and d.get('method')=='BudgetLedger.settle')
        settle['context']['idempotency_key']='settle-context'
        settle['input'].update(reservation_id=identifier,expected_ledger_revision=self.ledger.snapshot()['sha256'],outcome='uncertain',evidence=[reference])
        usage=settle['input']['actual_usage'];usage['classification']='actual';usage['currency']='USD'
        for key in METRICS:usage[key]={'value':0,'explanation':None}
        usage['provider_calls']['value']=1
        result=execute(self.ledger,settle,evidence_root=self.root)
        validate_method_result('BudgetLedger.settle',result,settle)
        self.assertEqual(result['value']['state'],'uncertain')
        self.assertTrue(result['value']['evidence'][0]['fixture'])
        release=next(d for d in docs if d.get('kind')=='director-request' and d.get('method')=='BudgetLedger.release')
        release['context']['idempotency_key']='release-context'
        release['input'].update(reservation_id=identifier,expected_ledger_revision=self.ledger.snapshot()['sha256'],evidence=[reference])
        with self.assertRaisesRegex(RigError,'cannot be released'):execute(self.ledger,release,evidence_root=self.root)
