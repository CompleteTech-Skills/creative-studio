"""Offline native-owner attestations for historical reservations with no publication log.

No provider is invoked. Claim histories use the public historical accounting
fixtures. Nonexecution statements are synthetic operator evidence.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_director_budget_interop_authority as interop

hatch = interop.hatch
BudgetLedger = interop.BudgetLedger
RigError = interop.RigError


class LegacyCancellationTests(unittest.TestCase):
    setUp = interop.BudgetInteropAuthorityTests.setUp
    prepare = interop.BudgetInteropAuthorityTests.prepare
    bind = interop.BudgetInteropAuthorityTests.bind
    row = interop.BudgetInteropAuthorityTests.row
    observe = interop.BudgetInteropAuthorityTests.observe
    fail_native = interop.BudgetInteropAuthorityTests.fail
    retain_json = interop.BudgetInteropAuthorityTests.retain_json
    historical = interop.BudgetInteropAuthorityTests.legacy_retry_fixture
    authority = interop.BudgetInteropAuthorityTests.authority

    def job(self, data):
        return hatch.job_for(data, 'base')

    def release(self, evidence=None, data=None):
        with hatch.ownership(self.root):
            if data is None:
                data = hatch.manifest(self.root, recovery=True)
            return hatch.release_budget(self.root, data, 'base', idempotency_key='owner-cancellation',
                                        outcome='canceled-known', evidence=evidence or self.proof)

    def should_release(self, evidence=None):
        try:
            return self.release(evidence)
        except (ValueError, RigError) as error:
            self.fail(f'Explicit reconciled native cancellation was refused: {error}')

    def refused(self, action):
        before = self.authority()
        with self.assertRaises((ValueError, RigError)):
            action()
        self.assertEqual(self.authority(), before)

    def generic(self, identifier, outcome, key, evidence, usage=None):
        options = dict(outcome=outcome, idempotency_key=key, evidence=evidence,
                       expected_revision=self.ledger.snapshot()['sha256'])
        if usage is None:
            return self.ledger.release(identifier, **options)
        return self.ledger.settle(identifier, usage=usage, **options)

    def two_measured(self):
        identifier, first, second = self.historical(second_running=True)
        self.fail_native(second, self.observe('last-measured-retry', 'retry', amount='0.75'))
        return identifier, first, second

    def test_same_complete_failed_journal_does_not_authorize_generic_release(self):
        identifier, _, _ = self.historical(second_running=False)
        self.refused(lambda: self.generic(identifier, 'canceled-known', 'generic-release', self.proof))
        self.assertEqual(self.should_release()['state'], 'released')

    def test_stale_failed_argument_cannot_overwrite_current_running_journal(self):
        _, first, second = self.historical(second_running=True)
        old = hatch.manifest(self.root, recovery=True)
        job = self.job(old)
        self.assertEqual(job['history'].pop()['token'], second)
        job.update(status='failed', attempts=1, started_at=1.0)
        job.pop('token')
        job['budget_operation']['attempts'] = [first]
        # A missing acknowledgement would otherwise make reconciliation save
        # this stale argument before the release's ledger eligibility check.
        job['budget_operation']['events'][0].pop('receipt')
        self.refused(lambda: self.release(data=old))

    def test_known_latest_usage_requires_its_own_finalized_failure(self):
        _, first, second = self.two_measured()
        data = hatch.manifest(self.root, recovery=True)
        job = self.job(data)
        self.assertEqual(job['history'][-1]['token'], second)
        job['history'][-1]['token'] = first
        hatch.save(self.root, data)
        self.refused(self.release)

    def test_nonexecution_evidence_and_latest_measurement_bound_in_receipt(self):
        identifier, _, second = self.two_measured()
        evidence = [self.retain_json(dict(scope='all-outstanding-attempts',
                                          source='Synthetic native owner nonexecution statement'))]
        receipt = self.should_release(evidence)
        job = self.job(hatch.manifest(self.root, recovery=True))
        operation = job['budget_operation']
        usage, cancellation = operation['events'][-2:]
        retained = json.loads((self.ledger.root / cancellation['evidence'][-1]['path']).read_bytes())
        self.assertEqual(retained['evidence'], evidence)
        self.assertEqual(retained['operation_id'], operation['operation_id'])
        self.assertEqual(retained['fixture'], True)
        witness = retained['nonexecution_attestation']
        self.assertEqual(witness['scope'], 'all-outstanding-attempts')
        self.assertEqual(witness['reservation_id'], identifier)
        self.assertEqual(witness['latest_token'], second)
        self.assertEqual(witness['latest_observation_key'], usage['key'])
        history = [entry for entry in job['history'] if entry['event'] in ('claim', 'failed')]
        self.assertEqual(witness['claim_history_sha256'], hatch.object_hash(history))
        self.assertEqual(receipt['spent']['amount'], '1.00')
        self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
        self.refused(lambda: self.release(self.proof))

    def test_generic_cannot_promote_a_real_uncommitted_native_attestation(self):
        identifier, _, _ = self.historical(second_running=False)
        change = BudgetLedger._change

        def interrupt(ledger, revision, key, action, payload, mutate):
            if action == 'release':
                raise OSError('synthetic cut before ledger commit')
            return change(ledger, revision, key, action, payload, mutate)

        with patch.object(BudgetLedger, '_change', interrupt), self.assertRaises(OSError):
            self.should_release()
        job = self.job(hatch.manifest(self.root, recovery=True))
        pending = job['budget_operation']['events'][-1]
        self.assertNotIn('receipt', pending)
        self.refused(lambda: self.generic(identifier, 'canceled-known', pending['key'], pending['evidence']))
        self.assertEqual(self.should_release()['state'], 'released')

    def test_committed_native_release_recovers_exact_receipt_after_lost_ack(self):
        self.historical(second_running=False)
        save = hatch.save

        def interrupt(root, data):
            latest = self.job(data)['budget_operation']['events'][-1]
            if latest['payload']['outcome'] == 'canceled-known' and 'receipt' in latest:
                raise OSError('synthetic cut after ledger commit')
            return save(root, data)

        with patch.object(hatch, 'save', interrupt), self.assertRaises(OSError):
            self.should_release()
        committed = self.ledger.snapshot()
        receipt = committed['state']['events'][-1]['receipt']
        self.assertEqual(receipt['state'], 'released')
        self.assertEqual(self.should_release(), receipt)
        after = self.authority()
        self.assertEqual(self.should_release(), receipt)
        self.assertEqual(self.authority(), after)
        self.assertEqual(self.ledger.snapshot(), committed)

    def test_direct_uncertainty_during_native_release_prevents_commit(self):
        identifier, _, _ = self.historical(second_running=False)
        save = hatch.save
        reached = []

        def concurrent_unknown(root, data):
            save(root, data)
            latest = self.job(data)['budget_operation']['events'][-1]
            if latest['payload']['outcome'] == 'canceled-known' and not reached:
                reached.append(True)
                self.generic(identifier, 'uncertain', 'direct-unknown', self.proof,
                             usage=interop.quantities(amount=None))

        with patch.object(hatch, 'save', concurrent_unknown), self.assertRaises((ValueError, RigError)):
            self.release()
        self.assertEqual(reached, [True])
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.row()['held']['amount'], '5.75')
        self.generic(identifier, 'retry', 'direct-resolution', self.proof,
                     usage=interop.quantities(amount='0.1'))
        self.assertEqual(self.should_release()['spent']['amount'], '0.35')

    def test_later_measured_token_does_not_clear_prior_token_uncertainty(self):
        identifier, first, second = self.historical(second_running=True)
        data = hatch.manifest(self.root, recovery=True)
        operation = self.job(data)['budget_operation']
        payload = dict(token=first, **self.observe('earlier-unresolved', 'uncertain', calls=0, amount=None))
        payload['usage'] = {key: None if value is None else str(value)
                            for key, value in payload['usage'].items()}
        key = 'native-observation-' + hatch.object_hash([operation['operation_id'], payload['id']])
        retained = self.proof + [self.retain_json(dict(kind='native-budget-usage',
            operation_id=operation['operation_id'], fixture=True, **payload))]
        receipt = self.generic(identifier, 'uncertain', key, retained, usage=payload['usage'])
        operation['events'].append(dict(key=key, payload=payload, evidence=retained, receipt=receipt))
        hatch.save(self.root, data)
        self.generic(identifier, 'retry', 'direct-known', self.proof, usage=interop.quantities())
        self.fail_native(second, self.observe('second-known', 'retry', amount='0.75'))
        self.assertEqual(self.row()['state'], 'reserved')
        self.refused(self.release)

    def test_owner_attests_all_attempts_even_if_original_snapshot_omits_later_handoff(self):
        identifier, first, second = self.historical(second_running=False)
        journal = self.root / 'imagegen-jobs.json'
        old = journal.read_bytes()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root, recovery=True)
            job = self.job(data)
            previous = next(entry for entry in job['history'] if entry.get('token') == first)
            later = deepcopy(previous)
            later.update(time=3.0, token=second)
            job['history'].append(later)
            job['budget_operation']['attempts'].append(second)
            job.update(status='running', token=second, started_at=3.0, attempts=2)
            hatch.save(self.root, data)
        self.refused(self.release)
        # Restore the exact earlier bytes. A historical ledger has no T2
        # publication record. Only the operator supplies all-attempt evidence.
        journal.write_bytes(old)
        self.refused(lambda: self.generic(identifier, 'canceled-known', 'generic', self.proof))
        evidence = [self.retain_json(dict(scope='all-outstanding-attempts', checked=[first, second],
                                          source='Synthetic external cancellation confirmation'))]
        receipt = self.should_release(evidence)
        self.assertEqual(receipt['state'], 'released')
        self.assertEqual(receipt['spent']['amount'], '0.25')
        self.assertFalse(any(event['action'] == 'native-claim'
                             for event in self.ledger.snapshot()['state']['events']))


if __name__ == '__main__':
    unittest.main()
