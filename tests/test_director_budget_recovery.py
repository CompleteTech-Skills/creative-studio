"""Offline recovery contracts; synthetic receipts do not imply provider execution."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import hatch
from director_budget import BudgetLedger, NativeBudget
from rig_errors import RigError
import test_director_budget_native as native_fixture


class BudgetRecoveryTests(unittest.TestCase):
    # Reuse builders without importing/subclassing the original collected TestCase.
    setUp = native_fixture.NativeBudgetTests.setUp
    prepare = native_fixture.NativeBudgetTests.prepare
    bind = native_fixture.NativeBudgetTests.bind
    claim = native_fixture.NativeBudgetTests.claim
    observe = native_fixture.NativeBudgetTests.observe

    def journal(self):
        return hatch.manifest(self.root, recovery=True)

    def job(self):
        return hatch.job_for(self.journal(), 'base')

    def row(self):
        operation = self.job()['budget_operation']
        return self.ledger.snapshot()['state']['reservations'][operation['reservation_id']]

    def report_failure(self, token, observation):
        with hatch.ownership(self.root):
            return hatch.fail(self.root, self.journal(), 'base', token,
                              'Synthetic recovery outcome', budget_usage=observation)

    def receipt(self, claim, identity='complete'):
        receipt = deepcopy(claim['receipt_template'])
        receipt['budget_usage'] = self.observe(identity, 'complete')
        path = self.owner / 'receipt.json'
        hatch.atomic(path, receipt)
        return path

    def source(self):
        from PIL import Image
        path = self.owner / 'synthetic.png'
        Image.new('RGBA', (96, 96), (30, 70, 160, 255)).save(path)
        return path

    def ingest(self, claim, receipt, source):
        with hatch.ownership(self.root):
            return hatch.ingest(self.root, self.journal(), 'base', source, claim['token'],
                                'Explicit synthetic recovery output', receipt, fixture=True)

    def test_missing_current_files_allow_known_failure_without_normal_admission(self):
        token = self.claim()['token']
        request = hatch.read(self.root / 'pet_request.json')
        request['synthetic_recovery_change'] = True
        hatch.atomic(self.root / 'pet_request.json', request)
        with self.assertRaises((ValueError, OSError, RigError)):
            NativeBudget.for_run(self.root, hatch.manifest(self.root))
        for name in ('pet_request.json', 'profile.json', 'prompts/base-pet.md'):
            (self.root / name).unlink()
        with self.assertRaises((ValueError, OSError, RigError)):
            hatch.manifest(self.root)
        self.report_failure(token, self.observe('known', 'failed'))
        row = self.row()
        self.assertEqual(row['spent']['amount'], '0.5')
        self.assertEqual(row['state'], 'settled')
        self.assertTrue(all(value == '0' for value in row['held'].values()))
        self.assertEqual(self.job()['status'], 'failed')
        self.assertNotIn('token', self.job())

    def test_ingest_retains_authentic_usage_for_each_missing_or_changed_input(self):
        original_root = self.root
        for index, name in enumerate(('pet_request.json', 'profile.json', 'prompts/base-pet.md')):
            for missing in (False, True):
                with self.subTest(input=name, missing=missing):
                    if index or missing:
                        self.root = self.prepare('drift-' + str(index) + '-' + str(missing))
                        self.bind(self.root, retry_allowance=0)
                    claim = self.claim()
                    receipt, source = self.receipt(claim), self.source()
                    path = self.root / name
                    if missing:
                        path.unlink()
                    else:
                        path.write_text('Changed synthetic input; invalid current contract', encoding='utf-8')
                    with self.assertRaises((ValueError, OSError, RigError)):
                        self.ingest(claim, receipt, source)
                    self.assertEqual(self.row()['spent']['amount'], '0.5')
                    self.assertEqual(self.row()['state'], 'settled')
                    self.assertEqual(self.job()['status'], 'running')
                    self.assertEqual(self.job()['token'], claim['token'])
                    self.assertFalse((self.root / 'decoded/base.png').exists())
        self.root = original_root

    def test_usage_is_durable_before_first_bounded_current_input_read(self):
        claim = self.claim()
        receipt, source = self.receipt(claim), self.source()
        current = {self.root / name for name in
                   ('pet_request.json', 'profile.json', 'prompts/base-pet.md')}
        real_read = hatch.input_bytes
        observed = []

        def stop_at_current(path, *args, **kwargs):
            if Path(path) in current:
                observed.append(Path(path).name)
                # Consult the independent ledger, not the in-memory manifest.
                rows = list(self.ledger.snapshot()['state']['reservations'].values())
                self.assertEqual(rows[0]['spent']['amount'], '0.5')
                raise ValueError('synthetic stop at first current input read')
            return real_read(path, *args, **kwargs)

        with patch.object(hatch, 'input_bytes', side_effect=stop_at_current):
            with self.assertRaisesRegex(ValueError, 'synthetic stop'):
                self.ingest(claim, receipt, source)
        self.assertTrue(observed, 'ingest must perform bounded current validation')
        self.assertEqual(self.job()['token'], claim['token'])

    def test_uncertainty_survives_missing_inputs_until_explicit_delta_resolution(self):
        token = self.claim()['token']
        (self.root / 'pet_request.json').unlink()
        (self.root / 'profile.json').unlink()
        self.report_failure(token, self.observe('unknown', 'uncertain', amount=None))
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.row()['held']['amount'], '6')
        self.assertEqual(self.job()['token'], token)
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.report_failure(token, self.observe('unresolved', 'failed', calls=0))
        self.assertEqual(self.ledger.snapshot(), before)
        self.report_failure(token, self.observe('resolved', 'failed', calls=0,
                                               amount='0.75', resolves=['unknown']))
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '0.75')
        self.assertEqual(self.row()['state'], 'settled')
        self.assertNotIn('token', self.job())

    def test_wrong_token_or_forged_receipt_cannot_charge_before_input_failure(self):
        claim = self.claim()
        path, source = self.receipt(claim), self.source()
        valid = hatch.read(path)
        (self.root / 'pet_request.json').unlink()
        before = self.ledger.snapshot()
        for field, value in (('token', 'not-the-original-token'),
                             ('input_hashes', {}), ('job', 'another-job'),
                             ('generation_skill', '$imagegen')):
            with self.subTest(field=field):
                forged = deepcopy(valid)
                forged[field] = value
                hatch.atomic(path, forged)
                with self.assertRaises((ValueError, RigError)):
                    self.ingest(claim, path, source)
                self.assertEqual(self.ledger.snapshot(), before)
        hatch.atomic(path, valid)
        wrong = dict(claim, token='not-the-original-token')
        with self.assertRaises((ValueError, RigError)):
            self.ingest(wrong, path, source)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.job()['token'], claim['token'])

    def test_forged_external_enrollment_is_rejected_without_charging(self):
        token = self.claim()['token']
        path = self.owner / 'director-budget-owner.json'
        owner = hatch.read(path)
        owner['enrollments'][0]['request_sha256'] = '0' * 64
        hatch.atomic(path, owner)
        before = self.ledger.snapshot()
        owner_bytes = path.read_bytes()
        with self.assertRaises(RigError):
            self.report_failure(token, self.observe('forged-enrollment', 'failed'))
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(path.read_bytes(), owner_bytes)

    def test_tampered_recorded_operation_cannot_use_recovery_authority(self):
        token = self.claim()['token']
        data = self.journal()
        operation = hatch.job_for(data, 'base')['budget_operation']
        operation['input_hashes']['pet_request.json'] = '0' * 64
        hatch.save(self.root, data)
        before = self.ledger.snapshot()
        with self.assertRaises(RigError):
            self.report_failure(token, self.observe('forged-operation', 'failed'))
        self.assertEqual(self.ledger.snapshot(), before)

    def test_recovery_journal_and_budget_cannot_admit_or_mint_token(self):
        with hatch.ownership(self.root):
            data = self.journal()
            budget = NativeBudget.for_run(self.root, data, recovery=True)
            before = self.ledger.snapshot()
            hashes = hatch.inputs(self.root, hatch.job_for(data, 'base'))
            with self.assertRaises((ValueError, RigError)):
                budget.before_claim(hatch.job_for(data, 'base'), hashes)
            with self.assertRaises((ValueError, RigError)):
                hatch.claim(self.root, data, 'base')
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.job()['attempts'], 0)
        self.assertNotIn('token', self.job())
        self.assertNotIn('budget_operation', self.job())

    def test_recovery_replays_durable_usage_without_calling_reserve(self):
        token = self.claim()['token']
        observation = self.observe('durable-intent', 'failed')
        with patch.object(BudgetLedger, 'settle', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.report_failure(token, observation)
        self.assertEqual(self.row()['spent']['amount'], '0')
        # Lose only the native association acknowledgement; the reserve receipt exists.
        data = self.journal()
        reservation = hatch.job_for(data, 'base')['budget_operation'].pop('reservation_id')
        hatch.save(self.root, data)
        (self.root / 'pet_request.json').unlink()
        with patch.object(BudgetLedger, 'reserve', side_effect=AssertionError('new reserve forbidden')):
            with hatch.ownership(self.root):
                hatch.reconcile(self.root, self.journal())
            self.assertEqual(self.row()['spent']['amount'], '0.5')
            self.assertEqual(self.job()['budget_operation']['reservation_id'], reservation)
            before = self.ledger.snapshot()
            self.report_failure(token, observation)
            with hatch.ownership(self.root):
                hatch.record_budget_usage(self.root, self.journal(), 'base', token, observation)
                hatch.reconcile(self.root, self.journal())
            self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(len(self.row()['attempts']), 1)
        self.assertEqual(self.job()['status'], 'failed')

    def test_missing_original_reservation_cannot_be_fabricated_in_recovery(self):
        with patch.object(BudgetLedger, 'reserve', side_effect=RuntimeError('before admission')):
            with self.assertRaises(RuntimeError):
                self.claim()
        data = self.journal()
        job = hatch.job_for(data, 'base')
        self.assertIn('budget_operation', job)
        self.assertNotIn('reservation_id', job['budget_operation'])
        before = self.ledger.snapshot()
        with hatch.ownership(self.root):
            budget = NativeBudget.for_run(self.root, data, recovery=True)
            budget.reconcile()
            with self.assertRaises((ValueError, RigError)):
                budget._reserve(job['budget_operation'])
            with self.assertRaises((ValueError, RigError)):
                budget.before_claim(job, job['budget_operation']['input_hashes'])
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.job()['budget_operation']['attempts'], [])
        self.assertNotIn('token', self.job())

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'mkfifo'), 'requires POSIX FIFO')
    def test_bounded_input_helpers_reject_fifo_and_symlink_without_hanging(self):
        path = self.owner / 'input.fifo'
        os.mkfifo(path)
        regular = self.owner / 'regular.json'
        regular.write_text('{}', encoding='utf-8')
        link = self.owner / 'linked.json'
        link.symlink_to(regular)
        script = (
            "import sys; from pathlib import Path; import hatch\n"
            "for name in ('input_bytes', 'input_digest', 'input_json'):\n"
            " for path in sys.argv[1:]:\n"
            "  try: getattr(hatch, name)(Path(path))\n"
            "  except (ValueError, OSError): continue\n"
            "  raise AssertionError(name + ' accepted special input')\n"
        )
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join((str(hatch.SCRIPTS), env.get('PYTHONPATH', '')))
        result = subprocess.run([sys.executable, '-c', script, str(path), str(link)], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bounded_input_helpers_reject_oversized_regular_file(self):
        path = self.owner / 'oversized.json'
        with path.open('wb') as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
        for name in ('input_bytes', 'input_digest', 'input_json'):
            with self.subTest(helper=name), self.assertRaises((ValueError, OSError)):
                getattr(hatch, name)(path)
