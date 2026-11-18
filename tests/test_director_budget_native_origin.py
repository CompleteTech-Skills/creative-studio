"""Disposable offline ledger records cannot impersonate native owner observations."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_director_budget_native as fixture

hatch, RigError = fixture.hatch, fixture.RigError
quantities = fixture.quantities


class NativeObservationOriginTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    observe = fixture.NativeBudgetTests.observe
    report_failure = fixture.NativeBudgetTests.fail
    row = fixture.NativeBudgetTests.row

    def authority(self):
        return self.ledger.snapshot(), (self.root/'imagegen-jobs.json').read_bytes()

    def unowned_observation(self, handoff, outcome, *, resolves=()):
        # These are synthetic local accounting bytes, with no provider/account.
        payload = dict(token=handoff['token'],
                       **self.observe('synthetic-unowned-observation', outcome,
                                      calls=0, amount='0.625', resolves=list(resolves)))
        payload['usage'] = {key: str(value) for key, value in payload['usage'].items()}
        proof = dict(kind='native-budget-usage', fixture=True,
                     operation_id=handoff['budget_operation_id'], **payload)
        path = self.owner/'synthetic-unowned-proof.json'
        path.write_text(json.dumps(proof, sort_keys=True), encoding='utf-8')
        evidence = self.proof+[self.ledger.retain_evidence(path)]
        key = 'native-observation-'+hatch.object_hash([proof['operation_id'], payload['id']])
        return dict(usage=payload['usage'], outcome=outcome, evidence=evidence,
                    idempotency_key=key, expected_revision=self.ledger.snapshot()['sha256'])

    def assert_unowned_resolution_refused(self, outcome):
        handoff = self.claim()
        self.report_failure(handoff['token'], self.observe('native-disconnected', 'uncertain', amount=None))
        options = self.unowned_observation(handoff, outcome, resolves=['native-disconnected'])
        before = self.authority()
        with self.assertRaises(RigError):
            self.ledger.settle(handoff['budget_reservation_id'], **options)
        self.assertEqual(self.authority(), before)
        self.assertEqual(self.row()['held']['amount'], '6')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')

    def test_generic_complete_cannot_author_native_resolution(self):
        self.assert_unowned_resolution_refused('complete')

    def test_generic_failed_cannot_author_native_resolution(self):
        self.assert_unowned_resolution_refused('failed')

    def test_generic_retry_cannot_author_native_resolution(self):
        self.assert_unowned_resolution_refused('retry')

    def test_generic_latest_token_measurement_cannot_authorize_release(self):
        first = self.claim()
        self.report_failure(first['token'], self.observe('first-known-retry', 'retry', amount='0.25'))
        second = self.claim()
        options = self.unowned_observation(second, 'retry')
        before = self.authority()
        with self.assertRaises(RigError):
            self.ledger.settle(second['budget_reservation_id'], **options)
        self.assertEqual(self.authority(), before)
        with self.assertRaises(RigError):
            self.ledger.release(second['budget_reservation_id'], outcome='canceled-known',
                evidence=self.proof, idempotency_key='synthetic-unowned-cancel',
                expected_revision=self.ledger.snapshot()['sha256'])
        self.assertEqual(self.authority(), before)

    def test_actual_owner_resolution_and_exact_receipt_replay_remain_valid(self):
        handoff = self.claim()
        self.report_failure(handoff['token'], self.observe('native-disconnected', 'uncertain', amount=None))
        usage = self.observe('native-measured', 'failed', calls=0, amount='0.625',
                             resolves=['native-disconnected'])
        self.report_failure(handoff['token'], usage)
        self.assertEqual(self.row()['state'], 'settled')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '0.625')
        before = self.authority()
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            expected = hatch.job_for(data, 'base')['budget_operation']['events'][-1]['receipt']
            self.assertEqual(hatch.record_budget_usage(self.root, data, 'base', handoff['token'], usage), expected)
        self.assertEqual(self.authority(), before)

    def test_null_captured_settings_refuse_without_ordinary_reread_or_admission(self):
        path = self.root/'extraction-settings.json'
        hatch.atomic(path, None)
        before = self.authority()
        with patch.object(hatch, 'read', wraps=hatch.read) as reader:
            try:
                self.claim()
            except Exception as error:
                caught = error
            else:
                caught = None
        with self.subTest(boundary='typed refusal'):
            self.assertIsInstance(caught, RigError)
        with self.subTest(boundary='single bounded capture'):
            self.assertEqual([call for call in reader.call_args_list
                              if call.args and Path(call.args[0]) == path], [])
        self.assertEqual(self.authority(), before)

    def test_captured_settings_object_supplies_handoff_without_ordinary_reread(self):
        path = self.root/'extraction-settings.json'
        settings = deepcopy(hatch.read(self.root/'pet_request.json')['extraction'])
        hatch.atomic(path, settings)
        with patch.object(hatch, 'read', wraps=hatch.read) as reader:
            handoff = self.claim()
        self.assertTrue(handoff['token'])
        self.assertEqual([call for call in reader.call_args_list
                          if call.args and Path(call.args[0]) == path], [])


if __name__ == '__main__':
    unittest.main()
