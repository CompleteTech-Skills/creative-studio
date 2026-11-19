"""Offline public recovery boundaries for retained native budget authority.

Historical cases reuse the existing simulated old-format contract fixture. They
are not actual predecessor-writer evidence; that separate cross-version client
is a local runtime check. The journal-cap tests scale both exposed reader limits
to 128 KiB, use real retained evidence bytes, and invoke no provider.
"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

import asset_contracts
import hatch
from director_budget import BudgetLedger
from rig_errors import RigError
import test_director_budget_native as native_fixture
import test_director_budget_interop_authority as interop_fixture

quantities = native_fixture.quantities


class BudgetRecoveryBoundaryTests(unittest.TestCase):
    setUp = native_fixture.NativeBudgetTests.setUp
    prepare = native_fixture.NativeBudgetTests.prepare
    bind = native_fixture.NativeBudgetTests.bind
    claim = native_fixture.NativeBudgetTests.claim
    row = native_fixture.NativeBudgetTests.row
    observe = native_fixture.NativeBudgetTests.observe
    fail_native = native_fixture.NativeBudgetTests.fail
    retain_json = interop_fixture.BudgetInteropAuthorityTests.retain_json
    historical = interop_fixture.BudgetInteropAuthorityTests.legacy_retry_fixture

    def journal(self):
        return hatch.manifest(self.root, recovery=True)

    def job(self):
        return hatch.job_for(self.journal(), 'base')

    def reconcile(self):
        with hatch.ownership(self.root):
            data = self.journal()
            hatch.reconcile(self.root, data)

    def cancel(self):
        with hatch.ownership(self.root):
            data = self.journal()
            hatch.reconcile(self.root, data)
            return hatch.release_budget(self.root, data, 'base',
                idempotency_key='evidenced-cancellation', outcome='canceled-known', evidence=self.proof)

    def repair(self):
        with hatch.ownership(self.root):
            hatch.repair(self.root, hatch.manifest(self.root), 'base',
                         'Synthetic explicit repair after measured failure')

    def assert_running_meter_boundary(self, *, historical, generic):
        # The supported historical fixture already spent one call / 0.25 and
        # leaves its second token running. Do not erase that prior expenditure.
        if historical:
            identifier, _, token = self.historical(second_running=True)
            baseline_calls, baseline_amount = Decimal(1), Decimal('0.25')
        else:
            handoff = self.claim()
            identifier, token = handoff['budget_reservation_id'], handoff['token']
            baseline_calls, baseline_amount = Decimal(0), Decimal(0)
        admitted = deepcopy(self.job()['budget_operation'])
        held_before = deepcopy(self.row()['held'])
        if generic:
            receipt = self.ledger.settle(identifier, usage=quantities(amount='0.20'),
                outcome='complete', expected_revision=self.ledger.snapshot()['sha256'],
                idempotency_key='independent-terminal-meter', evidence=self.proof)
            # Keep exercising original-token recovery even if the hold assertion
            # fails, so both acceptance failures remain visible.
            with self.subTest(boundary='independent meter preserves unfinished native hold'):
                self.assertEqual(receipt['state'], 'reserved')
                self.assertEqual(Decimal(receipt['held']['amount']),
                                 Decimal(held_before['amount']) - Decimal('0.20'))
                self.assertEqual(receipt['held']['provider_calls'], held_before['provider_calls'])
        self.assertEqual(self.job()['token'], token)
        self.assertEqual(self.job()['budget_operation'], admitted)
        observation = self.observe('original-token-meter', 'failed', calls=1, amount='0.50')
        try:
            with hatch.ownership(self.root):
                hatch.record_budget_usage(self.root, self.journal(), 'base', token, observation)
        except (ValueError, OSError, RigError) as error:
            self.fail('Authentic original-token usage must remain recordable after the '
                      f'independent meter: {type(error).__name__}: {error}')
        row = self.ledger.snapshot()['state']['reservations'][identifier]
        self.assertEqual(row['state'], 'settled')
        self.assertEqual(Decimal(row['spent']['provider_calls']), baseline_calls + 1)
        self.assertEqual(Decimal(row['spent']['amount']), baseline_amount + Decimal('0.50') +
                         (Decimal('0.20') if generic else 0))
        self.assertTrue(all(value == '0' for value in row['held'].values()))
        event = self.job()['budget_operation']['events'][-1]
        self.assertEqual(event['payload']['token'], token)
        self.assertEqual(event['payload']['id'], observation['id'])
        self.assertEqual(Decimal(event['payload']['usage']['provider_calls']), 1)
        self.assertEqual(Decimal(event['payload']['usage']['amount']), Decimal('0.50'))
        self.assertEqual(event['payload']['evidence'], self.proof)
        before = self.ledger.snapshot()
        with hatch.ownership(self.root):
            hatch.record_budget_usage(self.root, self.journal(), 'base', token, observation)
        self.assertEqual(self.ledger.snapshot(), before, 'Exact observation replay must never charge twice')
        self.assertEqual(self.job()['budget_operation']['events'][-1], event)

    def test_simulated_historical_running_token_survives_generic_terminal_meter(self):
        self.assert_running_meter_boundary(historical=True, generic=True)

    def test_simulated_historical_running_token_without_generic_meter_control(self):
        self.assert_running_meter_boundary(historical=True, generic=False)

    def test_current_running_token_survives_generic_terminal_meter_control(self):
        self.assert_running_meter_boundary(historical=False, generic=True)

    def test_current_running_token_without_generic_meter_control(self):
        self.assert_running_meter_boundary(historical=False, generic=False)

    def assert_exact_closure(self, expected_key, expected_payload):
        closures = [event for event in self.ledger.snapshot()['state']['events']
                    if event['action'] == 'native-finalize']
        self.assertEqual(len(closures), 1, 'The original native closure must be published exactly once')
        closure = closures[0]
        self.assertEqual(closure['idempotency_key'], expected_key)
        self.assertEqual(closure['payload'], expected_payload)
        for key in ('reservation_id', 'operation_id', 'token', 'observation_key', 'owner_history_sha256'):
            self.assertEqual(closure['receipt'][key], expected_payload[key])
        return closure

    def test_repair_after_interrupted_finalize_recovers_exact_original_closure(self):
        token = self.claim()['token']
        change = BudgetLedger._change
        pending = []

        def interrupt(ledger, revision, key, action, payload, mutate):
            if action == 'native-finalize':
                pending.append((key, deepcopy(payload)))
                raise OSError('Synthetic local interruption before native-finalize publication')
            return change(ledger, revision, key, action, payload, mutate)

        with patch.object(BudgetLedger, '_change', interrupt), self.assertRaises(OSError):
            self.fail_native(token, self.observe('measured-retry', 'retry', amount='0.25'))
        self.assertEqual(len(pending), 1, 'Reach the real ledger publication boundary')
        self.assertEqual(self.job()['status'], 'failed')
        self.assertNotIn('token', self.job())
        self.assertEqual(self.job()['history'][-1]['token'], token)
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        self.assertFalse(any(e['action'] == 'native-finalize' for e in self.ledger.snapshot()['state']['events']))
        original_observation = deepcopy(self.job()['budget_operation']['events'][-1])
        self.repair()
        self.assertEqual(self.job()['status'], 'pending')
        try:
            # Reopen with a new ledger object before the supported recovery path.
            self.ledger = BudgetLedger(self.ledger.root)
            self.reconcile()
            receipt = self.cancel()
        except (ValueError, OSError, RigError) as error:
            self.fail('Repair must retain enough authority to replay the original closure '
                      f'and cancel known work: {type(error).__name__}: {error}')
        self.assert_exact_closure(*pending[0])
        self.assertEqual(receipt['state'], 'released')
        self.assertEqual(receipt['spent']['provider_calls'], '1')
        self.assertEqual(receipt['spent']['amount'], '0.25')
        self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
        self.assertEqual(self.job()['budget_operation']['events'][0], original_observation)
        committed = self.ledger.snapshot()
        self.assertEqual(self.cancel(), receipt)
        self.reconcile()
        self.assertEqual(self.ledger.snapshot(), committed)
        self.assert_exact_closure(*pending[0])

    def test_finalized_before_repair_control_retains_original_closure_and_spend(self):
        token = self.claim()['token']
        self.fail_native(token, self.observe('measured-retry', 'retry', amount='0.25'))
        original = deepcopy(self.ledger.snapshot()['state']['events'][-1])
        self.assertEqual(original['action'], 'native-finalize')
        self.repair()
        receipt = self.cancel()
        self.assertEqual(receipt['state'], 'released')
        self.assertEqual(receipt['spent']['amount'], '0.25')
        self.assertEqual(receipt['spent']['provider_calls'], '1')
        self.assert_exact_closure(original['idempotency_key'], original['payload'])
        self.assertEqual(self.cancel(), receipt)

    def test_generic_uncertainty_during_finalize_can_reconcile_control(self):
        token = self.claim()['token']
        change = BudgetLedger._change
        inserted = []

        def concurrent_unknown(ledger, revision, key, action, payload, mutate):
            if action == 'native-finalize' and not inserted:
                inserted.append(ledger.settle(self.row()['id'], usage=quantities(amount=None),
                    outcome='uncertain', expected_revision=ledger.snapshot()['sha256'],
                    idempotency_key='independent-unknown', evidence=self.proof))
            return change(ledger, revision, key, action, payload, mutate)

        with patch.object(BudgetLedger, '_change', concurrent_unknown), self.assertRaises(RigError):
            self.fail_native(token, self.observe('measured-retry', 'retry', amount='0.25'))
        self.assertEqual(len(inserted), 1)
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.row()['spent']['amount'], '0.25')
        self.ledger.settle(self.row()['id'], usage=quantities(amount='0.20'), outcome='retry',
            expected_revision=self.ledger.snapshot()['sha256'],
            idempotency_key='independent-resolution', evidence=self.proof)
        receipt = self.cancel()
        self.assertEqual(receipt['state'], 'released')
        self.assertEqual(receipt['spent']['amount'], '0.45')
        self.assertEqual(receipt['spent']['provider_calls'], '1')
        closures = [e for e in self.ledger.snapshot()['state']['events'] if e['action'] == 'native-finalize']
        self.assertEqual(len(closures), 1)
        self.assertEqual(closures[0]['payload']['token'], token)


class ScaledNativeRecoveryCapacityTests(unittest.TestCase):
    def test_acknowledged_maximal_retry_can_fail_and_cancel_under_scaled_policy(self):
        # Neighboring prompt controls, not an exact byte threshold contract.
        # Both the encoded journal and its real bounded reader use 128 KiB.
        for prompt_size in (63000, 69500, 70000):
            with self.subTest(prompt_bytes=prompt_size):
                case = BudgetRecoveryBoundaryTests()
                case.setUp()
                try:
                    self.exercise_capacity(case, prompt_size)
                finally:
                    case.doCleanups()

    def exercise_capacity(self, case, prompt_size):
        limit = 128 * 1024
        with ExitStack() as stack:
            for module in (asset_contracts, hatch):
                stack.enter_context(patch.object(module, 'MAX_SOURCE_BYTES', limit))
            prompt = case.root / 'prompts/base-pet.md'
            prompt.write_bytes(b'P' * prompt_size)
            proof = []
            for index in range(127):
                evidence = case.owner / f'fixture-meter-{index}.txt'
                evidence.write_text(f'Synthetic retained measurement details {index}', encoding='utf-8')
                proof.append(case.ledger.retain_evidence(evidence))
            observation = case.observe('x' * 128, 'retry', calls=1, amount='0.50')
            observation.update(evidence=proof, measurement_source='\x01' * 1024)
            predecessor = ((case.root / 'imagegen-jobs.json').read_bytes(), case.ledger.snapshot())
            try:
                handoff = case.claim()
            except (ValueError, RigError):
                # An earlier safe admission refusal is valid. It must leave the
                # exact recoverable predecessor immutable and publish no claim.
                self.assertEqual((case.root / 'imagegen-jobs.json').read_bytes(), predecessor[0])
                self.assertEqual(case.ledger.snapshot(), predecessor[1])
                self.assertEqual(case.job()['status'], 'pending')
                return
            token = handoff['token']
            operation = deepcopy(case.job()['budget_operation'])
            self.assertEqual(case.job()['token'], token)
            self.assertEqual(case.job()['attempts'], 1)
            self.assertEqual(case.job()['history'][-1]['prompt_snapshot']['utf8'], 'P' * prompt_size)
            predecessor = ((case.root / 'imagegen-jobs.json').read_bytes(), case.ledger.snapshot())
            try:
                with hatch.ownership(case.root):
                    hatch.record_budget_usage(case.root, case.journal(), 'base', token, observation)
            except (ValueError, RigError):
                # Refusing the large observation before acknowledgement is also
                # safe only if the original running authority stays recoverable.
                self.assertEqual((case.root / 'imagegen-jobs.json').read_bytes(), predecessor[0])
                self.assertEqual(case.ledger.snapshot(), predecessor[1])
                self.assertEqual(case.job()['token'], token)
                observation = case.observe('concise-retry', 'retry', calls=1, amount='0.50')
            else:
                event = case.job()['budget_operation']['events'][-1]
                self.assertEqual(event['payload']['token'], token)
                self.assertEqual(event['payload']['id'], observation['id'])
                self.assertEqual(event['payload']['evidence'], proof)
                self.assertEqual(event['payload']['measurement_source'], '\x01' * 1024)
                self.assertEqual(case.row()['spent']['provider_calls'], '1')
                self.assertEqual(case.row()['spent']['amount'], '0.50')
                self.assertEqual(len(case.row()['attempts']), 1)
                self.assertLessEqual((case.root / 'imagegen-jobs.json').stat().st_size, limit)
            try:
                with hatch.ownership(case.root):
                    hatch.fail(case.root, case.journal(), 'base', token, 'accounted', budget_usage=observation)
                case.ledger = BudgetLedger(case.ledger.root)
                case.reconcile()
                receipt = case.cancel()
            except (ValueError, OSError, RigError) as error:
                self.fail('An admitted claim and acknowledged retry must preserve capacity '
                          'for concise failure and evidenced recovery/cancellation: '
                          f'{type(error).__name__}: {error}')
            self.assertEqual(case.job()['status'], 'failed')
            self.assertNotIn('token', case.job())
            self.assertEqual(case.job()['budget_operation']['operation_id'], operation['operation_id'])
            self.assertEqual(case.job()['budget_operation']['attempts'], [token])
            self.assertEqual(receipt['state'], 'released')
            self.assertEqual(receipt['spent']['provider_calls'], '1')
            self.assertEqual(receipt['spent']['amount'], '0.50')
            self.assertTrue(all(value == '0' for value in case.row()['held'].values()))
            self.assertEqual(len(case.row()['attempts']), 1)
            self.assertLessEqual((case.root / 'imagegen-jobs.json').stat().st_size, limit)
            committed = case.ledger.snapshot()
            self.assertEqual(case.cancel(), receipt)
            case.reconcile()
            self.assertEqual(case.ledger.snapshot(), committed)


if __name__ == '__main__':
    unittest.main()
