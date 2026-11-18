"""Independent offline enrollment, cancellation and bounded-finalization cases.

All timing hooks call the real public APIs and change only synthetic local
fixtures. The 128 KiB cases scale both exposed source-policy bindings and run
real serialization/readback; they are not full 64 MiB or provider evidence.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import asset_contracts
import hatch
from director_budget import BudgetLedger, NativeBudget, bind_native_budget
from rig_errors import RigError
import test_director_budget_native as fixture


class SyntheticRecoveryStop(RuntimeError):
    pass


class BudgetRecoveryCollisionTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    row = fixture.NativeBudgetTests.row
    observe = fixture.NativeBudgetTests.observe
    project_enrollment_fixture = fixture.NativeBudgetTests.project_enrollment_fixture

    def job(self, root=None):
        return hatch.job_for(hatch.manifest(root or self.root, recovery=True), 'base')

    def journal_path(self):
        return self.root / 'imagegen-jobs.json'

    def must_succeed(self, action):
        try:
            return action()
        except (ValueError, OSError, RigError) as error:
            self.fail(f'Legitimate recovery operation was refused: {type(error).__name__}: {error}')

    def project_fixture(self, name):
        project, runs, self.ledger, self.proof = self.project_enrollment_fixture(name)
        self.root = runs[0]
        options = dict(owner_root=project, provider='imagegen',
            estimate=fixture.quantities(provider_calls=1, amount=2),
            retry_allowance=0, evidence=self.proof)
        return project, runs, options

    def enroll_project(self, runs, options):
        for run in runs:
            bind_native_budget(run, self.ledger.root, **options)

    def enrollment_interruption(self, *, mutate):
        project, runs, options = self.project_fixture('capture-' + str(mutate))
        request = self.root / 'pet_request.json'
        original_bytes = request.read_bytes()
        original_digest = hashlib.sha256(original_bytes).hexdigest()
        original_save, original_digest_api = hatch.save, hatch.input_digest
        captures, stopped = [], []

        def digest(path):
            value = original_digest_api(path)
            if Path(path) == request:
                captures.append(value)
                # The first digest captures policy and the second validates the
                # prebound production binding. Exchange real bytes after that
                # validation, before any later retained-owner capture.
                if mutate and len(captures) == 2:
                    request.write_bytes(original_bytes + b'\n')
            return value

        def stop_journal(root, data):
            if Path(root) == self.root and 'director_budget' in data:
                stopped.append(True)
                # Also exercise the exchange with a future implementation that
                # shares a single capture instead of rereading this input.
                if mutate and request.read_bytes() == original_bytes:
                    request.write_bytes(original_bytes + b'\n')
                raise SyntheticRecoveryStop('synthetic interruption before enrolled journal save')
            return original_save(root, data)

        error = None
        with patch.object(hatch, 'input_digest', side_effect=digest), \
                patch.object(hatch, 'save', side_effect=stop_journal):
            try:
                bind_native_budget(self.root, self.ledger.root, **options)
            except (ValueError, RigError, SyntheticRecoveryStop) as exc:
                error = exc
        self.assertIsNotNone(error, 'The interruption or byte exchange must be observed')
        self.assertTrue(captures, 'The real request digest API must execute')
        if isinstance(error, SyntheticRecoveryStop):
            self.assertEqual(stopped, [True])
            self.assertTrue((project / 'director-budget-owner.json').is_file())
            self.assertIn('director_budget', hatch.read(project / fixture.workflow.WORKFLOW_FILE))
        self.assertNotIn('director_budget', hatch.manifest(self.root))
        self.assertEqual(self.ledger.snapshot()['state']['revision'], 0)
        owner_path = project / 'director-budget-owner.json'
        if owner_path.exists():
            with self.subTest(boundary='retained owner uses original capture'):
                entry = next(value for value in hatch.read(owner_path)['enrollments']
                             if value['run_id'] == hatch.manifest(self.root)['run_id'])
                self.assertEqual(entry['request_sha256'], original_digest,
                    'Retained owner and the original policy must not capture different request bytes')
        request.write_bytes(original_bytes)
        return project, runs, options

    def finish_enrollment(self, project, runs, options):
        self.must_succeed(lambda: self.enroll_project(runs, options))
        fixture.workflow.configure_generation_limits(project, concurrency=2)
        claims = [self.must_succeed(lambda label=label:
            fixture.workflow.claim_generation(project, label, 'base'))
            for label in ('first', 'second')]
        self.assertNotEqual(claims[0]['token'], claims[1]['token'])
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 2)

    def test_request_exchange_during_interrupted_enrollment_replays_original_policy(self):
        project, runs, options = self.enrollment_interruption(mutate=True)
        self.finish_enrollment(project, runs, options)

    def test_unmutated_interrupted_prebound_enrollment_replay_control(self):
        project, runs, options = self.enrollment_interruption(mutate=False)
        self.finish_enrollment(project, runs, options)

    def test_interrupted_prebound_enrollment_rejects_different_request_control(self):
        project, runs, options = self.enrollment_interruption(mutate=False)
        request = self.root / 'pet_request.json'
        request.write_bytes(request.read_bytes() + b'\n')
        before = {path.relative_to(project).as_posix(): path.read_bytes()
                  for path in project.rglob('*.json')}
        with self.assertRaises((ValueError, RigError)):
            bind_native_budget(self.root, self.ledger.root, **options)
        self.assertEqual({path.relative_to(project).as_posix(): path.read_bytes()
                          for path in project.rglob('*.json')}, before)

    def admit_pending(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            job = hatch.job_for(data, 'base')
            token = NativeBudget.for_run(self.root, data).before_claim(job, hatch.inputs(self.root, job))
        self.assertEqual(self.job()['status'], 'pending')
        self.assertEqual(self.job()['attempts'], 0)
        self.assertNotIn('token', self.job())
        return token

    def release_native(self):
        with hatch.ownership(self.root):
            return hatch.release_budget(self.root, hatch.manifest(self.root, recovery=True), 'base',
                idempotency_key='native-original-cancellation', outcome='unstarted', evidence=self.proof)

    def interrupt_cancellation(self):
        self.admit_pending()
        with patch.object(BudgetLedger, 'release', side_effect=SyntheticRecoveryStop(
                'synthetic interruption after durable cancellation intent')):
            with self.assertRaises(SyntheticRecoveryStop):
                self.release_native()
        event = self.job()['budget_operation']['events'][-1]
        self.assertEqual(event['payload']['id'], 'native-original-cancellation')
        self.assertEqual(event['payload']['outcome'], 'unstarted')
        self.assertNotIn('receipt', event)
        self.assertEqual(self.row()['state'], 'reserved')
        return deepcopy(event)

    def reconcile(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root, recovery=True)
            return NativeBudget.for_run(self.root, data, recovery=True).reconcile()

    def generic_release(self, identifier, outcome='unstarted'):
        return self.ledger.release(identifier, outcome=outcome, evidence=self.proof,
            idempotency_key='independent-generic-cancellation',
            expected_revision=self.ledger.snapshot()['sha256'])

    def test_matching_generic_cancellation_acknowledges_durable_native_intent(self):
        project, runs, options = self.project_fixture('matching-release')
        self.enroll_project(runs, options)
        pending = self.interrupt_cancellation()
        receipt = self.generic_release(self.row()['id'])
        released = self.ledger.snapshot()
        self.assertEqual(receipt['state'], 'released')
        self.must_succeed(self.reconcile)
        event = self.job()['budget_operation']['events'][-1]
        self.assertEqual({key: value for key, value in event.items() if key != 'receipt'}, pending)
        self.assertEqual(event['receipt']['reservation_id'], receipt['reservation_id'])
        self.assertEqual(event['receipt']['state'], 'released')
        self.assertEqual(self.ledger.snapshot(), released,
                         'Acknowledgment must not create another release, spend or revision')
        self.must_succeed(self.reconcile)
        self.assertEqual(self.ledger.snapshot(), released)
        self.assertEqual([event for event in released['state']['events']
                          if event['idempotency_key'] == 'independent-generic-cancellation'][0]['receipt'], receipt)
        self.must_succeed(lambda: fixture.workflow.generation_status(project))
        self.assertEqual(self.ledger.snapshot(), released)
        sibling = self.must_succeed(lambda: fixture.workflow.claim_generation(project, 'second', 'base'))
        self.assertEqual(self.job(runs[1])['token'], sibling['token'])
        self.assertNotEqual(sibling['budget_reservation_id'], receipt['reservation_id'])
        self.assertEqual(self.ledger.snapshot()['state']['reservations'][receipt['reservation_id']],
                         released['state']['reservations'][receipt['reservation_id']])

    def test_uninterrupted_native_unstarted_cancellation_control(self):
        project, runs, options = self.project_fixture('native-release-control')
        self.enroll_project(runs, options)
        self.admit_pending()
        receipt = self.release_native()
        self.assertEqual(receipt['state'], 'released')
        before = self.ledger.snapshot()
        self.must_succeed(self.reconcile)
        self.assertEqual(self.ledger.snapshot(), before)
        self.must_succeed(lambda: fixture.workflow.generation_status(project))
        sibling = self.must_succeed(lambda: fixture.workflow.claim_generation(project, 'second', 'base'))
        self.assertEqual(self.job(runs[1])['token'], sibling['token'])
        self.assertNotEqual(sibling['budget_reservation_id'], receipt['reservation_id'])
        self.assertEqual(self.ledger.snapshot()['state']['reservations'][receipt['reservation_id']],
                         before['state']['reservations'][receipt['reservation_id']])

    def test_conflicting_generic_cancellation_does_not_acknowledge_native_intent(self):
        pending = self.interrupt_cancellation()
        self.generic_release(self.row()['id'], outcome='canceled-known')
        before, journal = self.ledger.snapshot(), self.journal_path().read_bytes()
        with self.assertRaises((ValueError, RigError)):
            self.reconcile()
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.journal_path().read_bytes(), journal)
        self.assertEqual(self.job()['budget_operation']['events'][-1], pending)

    def test_different_reservation_cancellation_does_not_acknowledge_native_intent(self):
        pending = self.interrupt_cancellation()
        own_id = self.row()['id']
        other = self.ledger.reserve(operation_id='independent-generic-reservation', provider='imagegen',
            estimate=fixture.quantities(), retry_allowance=0, evidence=[],
            idempotency_key='independent-generic-reserve', expected_revision=self.ledger.snapshot()['sha256'])
        self.generic_release(other['reservation_id'])
        before = self.ledger.snapshot()
        self.assertEqual(before['state']['reservations'][own_id]['state'], 'reserved')
        self.must_succeed(self.reconcile)
        after = self.ledger.snapshot()
        self.assertEqual(after['state']['revision'], before['state']['revision'] + 1)
        self.assertEqual(after['state']['reservations'][own_id]['state'], 'released')
        self.assertEqual(after['state']['reservations'][other['reservation_id']],
                         before['state']['reservations'][other['reservation_id']])
        event = self.job()['budget_operation']['events'][-1]
        self.assertEqual(event['key'], pending['key'])
        self.assertEqual(event['payload'], pending['payload'])
        self.assertEqual(event['receipt']['reservation_id'], own_id)

    @contextmanager
    def scaled_source_policy(self):
        limit = 128 * 1024
        with patch.object(asset_contracts, 'MAX_SOURCE_BYTES', limit), \
                patch.object(hatch, 'MAX_SOURCE_BYTES', limit):
            yield limit

    def finalize_scaled_prompt(self, size, *, allow_prepublication_refusal):
        with self.scaled_source_policy() as limit:
            prompt = self.root / 'prompts/base-pet.md'
            prompt.write_bytes(b'P' * size)
            self.assertLess(prompt.stat().st_size, limit)
            history = deepcopy(self.job()['history'])
            run_history = deepcopy(hatch.manifest(self.root)['history'])
            handoff, error = None, None
            try:
                handoff = self.claim()
            except (ValueError, RigError) as exc:
                error = exc
            if error is not None:
                self.assertTrue(allow_prepublication_refusal, str(error))
                job = self.job()
                self.assertEqual(job['status'], 'pending')
                self.assertEqual(job['attempts'], 0)
                self.assertNotIn('token', job)
                self.assertEqual(job['history'], history)
                self.assertEqual(hatch.manifest(self.root)['history'][:len(run_history)], run_history)
                self.assertFalse(any(event['action'] == 'native-claim'
                                     for event in self.ledger.snapshot()['state']['events']))
                self.assertLessEqual(self.journal_path().stat().st_size, limit)
                return
            self.assertIsNotNone(handoff)
            token = handoff['token']
            claimed_history = deepcopy(self.job()['history'])
            # This is an actual supported terminal usage/finalization operation,
            # not a mocked save or an arbitrary estimate of future JSON growth.
            usage = self.observe('a', 'failed', amount='0.25')
            usage['measurement_source'] = 's'

            def terminal_usage():
                with hatch.ownership(self.root):
                    return hatch.fail(self.root, hatch.manifest(self.root), 'base', token,
                                      's', budget_usage=usage)

            self.must_succeed(terminal_usage)
            self.assertLessEqual(self.journal_path().stat().st_size, limit)
            job = self.job()
            self.assertEqual(job['status'], 'failed')
            self.assertEqual(job['history'][:len(claimed_history)], claimed_history)
            self.assertEqual(hatch.manifest(self.root)['history'][:len(run_history)], run_history)
            self.assertEqual(job['attempts'], 1)
            self.assertEqual(self.row()['spent']['amount'], '0.25')
            self.assertEqual(self.row()['spent']['provider_calls'], '1')
            self.assertTrue(all(value == '0' for value in self.row()['held'].values()))
            before = self.ledger.snapshot()
            self.must_succeed(self.reconcile)
            self.assertEqual(self.ledger.snapshot(), before)

    def test_scaled_near_bound_ascii_claim_has_terminal_usage_and_finalization_capacity(self):
        self.finalize_scaled_prompt(116800, allow_prepublication_refusal=True)

    def test_scaled_small_ascii_claim_terminal_usage_and_finalization_control(self):
        self.finalize_scaled_prompt(1024, allow_prepublication_refusal=False)

    def envelope_fixture(self):
        """A separate, synthetic owner with enough ledger capacity for probes."""
        owner = self.owner / 'envelope'
        owner.mkdir()
        limits = fixture.quantities(provider_calls=128, amount=256)
        self.ledger = BudgetLedger.create(owner / 'ledger', limits,
            provider_limits={'imagegen': limits}, currency='USD')
        self.maximal_proof = []
        for index in range(127):
            path = self.owner / f'synthetic-envelope-proof-{index}.json'
            hatch.atomic(path, {'synthetic_envelope_evidence': index})
            self.maximal_proof.append(self.ledger.retain_evidence(path))
        self.assertEqual(len({item['sha256'] for item in self.maximal_proof}), 127)
        self.proof = self.maximal_proof[:1]
        return owner

    def envelope_run(self, owner, label, size):
        self.root = self.prepare(f'envelope/acquisition/{label}')
        bind_native_budget(self.root, self.ledger.root, owner_root=owner, provider='imagegen',
            estimate=fixture.quantities(provider_calls=1, amount=2), retry_allowance=0,
            evidence=self.proof)
        (self.root / 'prompts/base-pet.md').write_bytes(b'P' * size)
        return self.root

    def maximal_usage(self, identity, outcome, *, calls=1, amount='0.25', resolves=None):
        observation = self.observe(identity, outcome, calls=calls, amount=amount, resolves=resolves)
        observation['evidence'] = deepcopy(self.maximal_proof)
        # Exactly 1024 raw codepoints. Non-BMP characters also exercise the
        # actual JSON encoder's escaping/UTF-8 policy without a guessed size.
        observation['measurement_source'] = '\U0001f642' * 1024
        self.assertEqual(len(observation['measurement_source']), 1024)
        self.assertEqual(len(observation['evidence']), 127)
        return observation

    def report_job(self, name, token, observation):
        with hatch.ownership(self.root):
            return hatch.fail(self.root, hatch.manifest(self.root, recovery=True), name,
                              token, 's', budget_usage=observation)

    def assert_terminal_preserves_claim(self, name, token, observation, history):
        self.must_succeed(lambda: self.report_job(name, token, observation))
        data = hatch.manifest(self.root, recovery=True)
        job = hatch.job_for(data, name)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['history'][:len(history)], history,
                         'Existing prompt snapshots and decoded history must survive finalization')
        event = job['budget_operation']['events'][-1]
        self.assertEqual(event['payload']['measurement_source'], observation['measurement_source'])
        self.assertEqual(event['payload']['evidence'], observation['evidence'])
        self.assertEqual(event['receipt']['state'], 'settled')
        row = self.ledger.snapshot()['state']['reservations'][job['budget_operation']['reservation_id']]
        self.assertEqual(row['spent']['amount'], '0.25')
        self.assertEqual(row['spent']['provider_calls'], '1')
        self.assertTrue(all(value == '0' for value in row['held'].values()))
        self.assertLessEqual(self.journal_path().stat().st_size, hatch.MAX_SOURCE_BYTES)

    def test_scaled_admitted_prompt_boundary_finalizes_maximal_supported_metadata(self):
        owner = self.envelope_fixture()
        with self.scaled_source_policy():
            # Find a real admission boundary to within one KiB using fresh
            # synthetic runs, without assuming a particular reserve algorithm.
            low, high, best, refused = 0, 120, None, []
            while low <= high:
                units = (low + high) // 2
                root = self.envelope_run(owner, f'boundary-{units}', units * 1024)
                before = deepcopy(self.job()['history'])
                publications = [event for event in self.ledger.snapshot()['state']['events']
                                if event['action'] == 'native-claim']
                try:
                    handoff = self.claim()
                except (ValueError, RigError):
                    refused.append(units)
                    self.assertEqual(self.job()['history'], before)
                    self.assertEqual(self.job()['attempts'], 0)
                    self.assertNotIn('token', self.job())
                    self.assertEqual([event for event in self.ledger.snapshot()['state']['events']
                                      if event['action'] == 'native-claim'], publications)
                    high = units - 1
                else:
                    best = (units, root, handoff, deepcopy(self.job()['history']))
                    low = units + 1
            self.assertIsNotNone(best, 'Scaled policy must admit at least one recoverable token')
            units, self.root, handoff, history = best
            self.assertGreater(units, 0)
            self.assertIn(units + 1, refused, 'The accepted prompt must be within one KiB of refusal')
            self.assert_terminal_preserves_claim('base', handoff['token'],
                self.maximal_usage('maximal-final', 'failed'), history)

    def test_scaled_small_claim_maximal_supported_metadata_control(self):
        owner = self.envelope_fixture()
        with self.scaled_source_policy():
            self.envelope_run(owner, 'small-maximal', 1024)
            handoff = self.must_succeed(self.claim)
            history = deepcopy(self.job()['history'])
            self.assert_terminal_preserves_claim('base', handoff['token'],
                self.maximal_usage('maximal-small', 'failed'), history)

    def test_scaled_maximal_unknown_then_known_usage_recovers_receipt_interruption(self):
        owner = self.envelope_fixture()
        with self.scaled_source_policy():
            self.envelope_run(owner, 'unknown-resolution', 1024)
            handoff = self.must_succeed(self.claim)
            token = handoff['token']
            history = deepcopy(self.job()['history'])
            unknown = self.maximal_usage('maximal-unknown', 'uncertain', amount=None)
            self.must_succeed(lambda: self.report_job('base', token, unknown))
            self.assertEqual(self.must_succeed(self.row)['state'], 'uncertain')
            self.assertEqual(self.job()['token'], token)
            known = self.maximal_usage('maximal-known', 'failed', calls=0,
                                       resolves=['maximal-unknown'])
            original_save, reached = hatch.save, []

            def stop_receipt(root, data):
                if Path(root) == self.root:
                    events = hatch.job_for(data, 'base')['budget_operation']['events']
                    if events and events[-1]['payload']['id'] == 'maximal-known' and 'receipt' in events[-1]:
                        reached.append(True)
                        raise SyntheticRecoveryStop('synthetic interruption before receipt acknowledgment')
                return original_save(root, data)

            error = None
            with patch.object(hatch, 'save', side_effect=stop_receipt):
                try:
                    self.report_job('base', token, known)
                except (ValueError, RigError, SyntheticRecoveryStop) as exc:
                    error = exc
            self.assertIsInstance(error, SyntheticRecoveryStop,
                'Maximal resolution must reach the actual committed-ledger receipt boundary: ' + str(error))
            self.assertEqual(reached, [True])
            self.assertEqual(self.row()['spent']['amount'], '0.25')
            committed = self.ledger.snapshot()
            self.assert_terminal_preserves_claim('base', token, known, history)
            self.assertEqual(self.ledger.snapshot(), committed, 'Receipt replay must not charge another delta')
            self.assertEqual(self.job()['budget_operation']['events'][-1]['payload']['resolves'],
                             ['maximal-unknown'])

    def approve_envelope_base(self):
        from PIL import Image
        handoff = self.claim()
        source = self.owner / 'synthetic-envelope-base.png'
        Image.new('RGBA', (96, 96), (30, 70, 160, 255)).save(source)
        receipt = deepcopy(handoff['receipt_template'])
        receipt['budget_usage'] = self.observe('synthetic-base-complete', 'complete', amount='0.25')
        receipt_path = self.owner / 'synthetic-envelope-base-receipt.json'
        hatch.atomic(receipt_path, receipt)
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.ingest(self.root, data, 'base', source, handoff['token'],
                         'Synthetic offline base', receipt_path, fixture=True)
            hatch.record_base_review(self.root, data, 'Synthetic local base approval', True)

    def test_scaled_active_tokens_preserve_each_others_maximal_terminal_capacity(self):
        owner = self.envelope_fixture()
        with self.scaled_source_policy():
            self.envelope_run(owner, 'active-siblings', 1024)
            self.approve_envelope_base()
            published = []
            for name in ('rest', 'jump'):
                with hatch.ownership(self.root):
                    data = hatch.manifest(self.root)
                    before = deepcopy(hatch.job_for(data, name))
                    ledger_before = self.ledger.snapshot()
                    try:
                        handoff = hatch.claim(self.root, data, name)
                    except (ValueError, RigError):
                        after = hatch.job_for(hatch.manifest(self.root), name)
                        self.assertEqual(after['history'], before['history'])
                        self.assertEqual(after['attempts'], 0)
                        self.assertNotIn('token', after)
                        self.assertEqual([event for event in self.ledger.snapshot()['state']['events']
                            if event['action'] == 'native-claim'],
                            [event for event in ledger_before['state']['events'] if event['action'] == 'native-claim'])
                    else:
                        published.append((name, handoff['token'],
                                          deepcopy(hatch.job_for(hatch.manifest(self.root), name)['history'])))
            self.assertTrue(published, 'At least one state token must retain supported recovery capacity')
            for index, (name, token, history) in enumerate(published):
                self.assert_terminal_preserves_claim(name, token,
                    self.maximal_usage('maximal-' + name, 'failed'), history)
                for pending_name, pending_token, pending_history in published[index + 1:]:
                    pending = hatch.job_for(hatch.manifest(self.root), pending_name)
                    self.assertEqual(pending['token'], pending_token)
                    self.assertEqual(pending['history'], pending_history)

    def test_scaled_historical_compact_save_preserves_every_decoded_value(self):
        # A save/reopen fixture for historical-format JSON, not evidence of a
        # live legacy provider operation or reconstructed ledger authority.
        with self.scaled_source_policy() as limit:
            data = hatch.manifest(self.root)
            data['history'].append({'event': 'synthetic-historical-decoded-preservation',
                'text': '', 'unicode': '\U0001f642' * 1024,
                'nested': {'items': [None, True, False, 17, 'original field']}})
            compact = lambda value: json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
            data['history'][-1]['text'] = 'H' * (limit - len(compact(data)) - 512)
            payload = compact(data)
            self.assertLess(len(payload), limit)
            self.assertGreater(len(json.dumps(data, indent=2).encode('utf-8')), limit)
            self.journal_path().write_bytes(payload)
            original = hatch.manifest(self.root, recovery=True)
            self.assertEqual(original, data)
            self.must_succeed(lambda: hatch.save(self.root, deepcopy(original)))
            self.assertLessEqual(self.journal_path().stat().st_size, limit)
            self.assertEqual(hatch.manifest(self.root, recovery=True), original)


if __name__ == '__main__':
    unittest.main()
