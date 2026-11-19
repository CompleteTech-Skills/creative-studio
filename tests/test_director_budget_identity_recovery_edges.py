"""Independent, offline admission identity and cancellation recovery regressions.

Fixtures use real local journals, ledgers and tiny synthetic images. No provider
is invoked. Historical intents are reconstructed from the documented old format,
not by modifying immutable ledger history.
"""
import argparse
import hashlib
from copy import deepcopy
import unittest
import uuid

from PIL import Image

import hatch
from director_budget import NativeBudget, object_hash
from rig_errors import RigError
import test_director_budget_native as fixture


class BudgetIdentityRecoveryEdgeTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    observe = fixture.NativeBudgetTests.observe
    report_failure = fixture.NativeBudgetTests.fail
    row = fixture.NativeBudgetTests.row

    def journal_path(self):
        return self.root / 'imagegen-jobs.json'

    def job(self):
        return hatch.job_for(hatch.manifest(self.root), 'base')

    def must_succeed(self, action):
        try:
            return action()
        except (ValueError, RigError) as error:
            self.fail(f'Legitimate recovery/accounting operation was refused: {error}')

    def admit_pending(self):
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            job = hatch.job_for(data, 'base')
            token = NativeBudget.for_run(self.root, data).before_claim(
                job, hatch.inputs(self.root, job))
        self.assertEqual(self.job()['status'], 'pending')
        self.assertEqual(self.job()['attempts'], 0)
        self.assertNotIn('token', self.job())
        self.assertEqual(self.job()['budget_operation']['attempts'], [token])
        return token

    def prepare_references(self):
        references = []
        for index, color in enumerate(('red', 'blue')):
            path = self.owner / f'synthetic-reference-{index}.png'
            Image.new('RGBA', (8, 8), color).save(path)
            references.append(str(path))
        root = self.owner / 'with-references'
        hatch.prepare(root, argparse.Namespace(replace_inputs=False, fixture_mode=True,
            profile=str(self.owner / 'profile.json'), pet_name='Attachment identity fixture',
            pet_notes='Synthetic offline references', reference=references,
            avoid=[], require=[], brand_source=[], style_preset='clay', style_notes='fixture'))
        self.root = root
        self.bind(root)
        images = self.job()['input_images']
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0]['path'], images[1]['path'])

    def attachment_mutation(self, mutation):
        self.prepare_references()
        token = self.admit_pending()
        original = deepcopy(self.job()['input_images'])
        admitted = deepcopy(self.job()['budget_operation'])
        hashes = hatch.inputs(self.root, self.job())
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            images = hatch.job_for(data, 'base')['input_images']
            if mutation == 'order':
                images.reverse()
            else:
                images[0]['note'] = 'Different generation instruction with unchanged image bytes'
            hatch.save(self.root, data)
        self.assertNotEqual(self.job()['input_images'], original)
        self.assertEqual(hatch.inputs(self.root, self.job()), hashes,
                         'Scenario must preserve all per-path input byte hashes')
        return token, original, admitted

    def assert_attachment_refused(self, mutation):
        token, original, admitted = self.attachment_mutation(mutation)
        before = self.ledger.snapshot()
        journal = self.journal_path().read_bytes()
        with self.assertRaises((ValueError, RigError),
                               msg='Changed ordered attachment descriptors returned dispatch authority'):
            self.claim()
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.journal_path().read_bytes(), journal)
        self.assertEqual(self.job()['budget_operation'], admitted)
        self.assertEqual(self.job()['status'], 'pending')
        self.assertEqual(self.job()['budget_operation']['attempts'], [token])
        # Restore only the ordered descriptor list; the original intent survives.
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            hatch.job_for(data, 'base')['input_images'] = original
            hatch.save(self.root, data)
        handoff = self.must_succeed(self.claim)
        self.assertEqual(handoff['token'], token)
        self.assertEqual(self.job()['attempts'], 1)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)
        expected = [{**image, 'path': str(self.root / image['path'])} for image in original]
        self.assertEqual(handoff['input_images'], expected)

    def test_pending_attachment_order_change_refuses_and_original_order_recovers(self):
        self.assert_attachment_refused('order')

    def test_pending_attachment_note_change_refuses_and_original_note_recovers(self):
        self.assert_attachment_refused('note')

    def test_unchanged_ordered_attachment_descriptors_recover_original_token_control(self):
        self.prepare_references()
        original = deepcopy(self.job()['input_images'])
        token = self.admit_pending()
        handoff = self.claim()
        self.assertEqual(handoff['token'], token)
        self.assertEqual(self.job()['input_images'], original)
        self.assertEqual(self.job()['history'][-1]['input_images'], original)
        self.assertEqual(self.job()['attempts'], 1)
        self.assertEqual(len(self.ledger.snapshot()['state']['reservations']), 1)

    def historical_pending(self, *, malformed=False):
        """Retain an authentic pre-publication-protocol reservation and journal."""
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root)
            job = hatch.job_for(data, 'base')
            policy = deepcopy(data['director_budget'])
            hashes = hatch.inputs(self.root, job)
            identifier = 'native-' + object_hash(dict(run_id=data['run_id'], job=job['id'],
                inputs=hashes, fixture=data['fixture_mode'], policy=policy))
            request = {key: deepcopy(policy[key]) for key in
                ('provider', 'estimate', 'retry_allowance', 'unknown_price', 'unknown_price_cap')}
            request.update(operation_id=identifier, idempotency_key=identifier + ':reserve',
                           evidence=deepcopy(policy['evidence']))
            intent = dict(kind='native-budget-intent', run_id=data['run_id'], job=job['id'],
                fixture=data['fixture_mode'], operation_id=identifier,
                input_hashes=deepcopy(hashes), policy=policy)
            self.assertNotIn('publication_protocol', intent)
            if malformed:
                # Still explicitly claims this operation, but its content cannot
                # authenticate to the retained operation hash.
                intent['input_hashes']['pet_request.json'] = '0' * 64
            path = self.owner / 'historical-native-intent.json'
            hatch.atomic(path, intent)
            request['evidence'].append(self.ledger.retain_evidence(path))
            receipt = self.ledger.reserve(**request,
                expected_revision=self.ledger.snapshot()['sha256'])
            token = uuid.uuid4().hex
            job['budget_operation'] = dict(operation_id=identifier, input_hashes=deepcopy(hashes),
                reservation_request=request, attempts=[token], events=[],
                reservation_id=receipt['reservation_id'])
            hatch.save(self.root, data)
        return token

    def release_native(self, outcome, key):
        with hatch.ownership(self.root):
            return hatch.release_budget(self.root, hatch.manifest(self.root, recovery=True),
                'base', idempotency_key=key, outcome=outcome, evidence=self.proof)

    def refuse_historical_unstarted(self):
        before = self.ledger.snapshot()
        journal = self.journal_path().read_bytes()
        with self.assertRaises((ValueError, RigError)):
            self.release_native('unstarted', 'historical-unstarted-refusal')
        return before, journal

    def recover_historical_claim_and_cancel(self, token):
        # Simulate recovery of the original historical claim journal. This does
        # not issue a handoff or forge a new protocol-1 ledger publication.
        with hatch.ownership(self.root):
            data = hatch.manifest(self.root, recovery=True)
            job = hatch.job_for(data, 'base')
            hashes = deepcopy(job['budget_operation']['input_hashes'])
            prompt = (self.root / job['prompt_file']).read_bytes()
            request = hatch.read(self.root / 'pet_request.json')
            job.update(status='running', token=token, attempts=1, input_hashes=hashes, started_at=0)
            job['history'].append(dict(event='claim', time=0, token=token, input_hashes=hashes,
                prompt_snapshot=dict(path=job['prompt_file'],
                    sha256=hashlib.sha256(prompt).hexdigest(), utf8=prompt.decode('utf-8')),
                input_images=deepcopy(job['input_images']), identity=request.get('identity', {}),
                resolved_style=request.get('resolved_style')))
            hatch.save(self.root, data)
        self.must_succeed(lambda: self.report_failure(token,
            self.observe('historical-known-usage', 'retry', calls=1, amount='0.5')))
        result = self.must_succeed(lambda: self.release_native(
            'canceled-known', 'historical-explicit-cancellation'))
        self.assertEqual(result['state'], 'released')
        self.assertEqual(self.row()['spent']['provider_calls'], '1')
        self.assertEqual(self.row()['spent']['amount'], '0.5')
        self.assertTrue(all(value == '0' for value in self.row()['held'].values()))

    def test_refused_historical_unstarted_does_not_persist_poison_intent(self):
        self.historical_pending()
        before, journal = self.refuse_historical_unstarted()
        with self.subTest(boundary='ledger'):
            self.assertEqual(self.ledger.snapshot(), before)
        with self.subTest(boundary='journal'):
            self.assertEqual(self.journal_path().read_bytes(), journal,
                             'Refused cancellation must not persist an unreplayable intent')

    def test_refused_historical_unstarted_preserves_later_explicit_recovery(self):
        token = self.historical_pending()
        self.refuse_historical_unstarted()
        self.recover_historical_claim_and_cancel(token)

    def test_historical_claim_direct_reconciliation_and_cancellation_control(self):
        token = self.historical_pending()
        self.recover_historical_claim_and_cancel(token)

    def add_generic_cross_operation_proof(self, key_style):
        # The original authentic native proof is retained for a DIFFERENT
        # operation. Supplying it as generic audit evidence grants no authority.
        self.admit_pending()
        donor = self.job()['budget_operation']
        reference = deepcopy(donor['reservation_request']['evidence'][-1])
        # Exercise an independently enrolled sibling, not merely the donor.
        self.root = self.prepare('legitimate-sibling')
        self.bind(self.root, retry_allowance=0)
        self.admit_pending()
        operation = 'native-' + 'c' * 64
        self.assertNotEqual(operation, donor['operation_id'])
        key = 'generic-cross-operation-audit' if key_style == 'ordinary' else operation + ':reserve'
        receipt = self.ledger.reserve(operation_id=operation, provider='imagegen',
            estimate=fixture.quantities(), retry_allowance=0, evidence=[reference],
            expected_revision=self.ledger.snapshot()['sha256'], idempotency_key=key)
        return receipt['reservation_id']

    def cross_operation_status(self, key_style):
        identifier = self.add_generic_cross_operation_proof(key_style)
        before = self.ledger.snapshot()
        status = self.must_succeed(lambda: NativeBudget.for_run(
            self.root, hatch.manifest(self.root)).status())
        self.assertEqual(status['reservations'][identifier]['state'], 'reserved')
        self.assertEqual(self.ledger.snapshot(), before)

    def cross_operation_claim(self, key_style):
        identifier = self.add_generic_cross_operation_proof(key_style)
        original = deepcopy(self.ledger.snapshot()['state']['reservations'][identifier])
        pending = self.job()['budget_operation']['attempts'][0]
        handoff = self.must_succeed(self.claim)
        self.assertEqual(handoff['token'], pending)
        self.assertNotEqual(handoff['budget_reservation_id'], identifier)
        self.assertEqual(self.ledger.snapshot()['state']['reservations'][identifier], original)

    def cross_operation_release(self, key_style):
        identifier = self.add_generic_cross_operation_proof(key_style)
        native_id = self.job()['budget_operation']['reservation_id']
        native = self.must_succeed(lambda: self.release_native(
            'unstarted', 'legitimate-native-unstarted'))
        self.assertEqual(native['state'], 'released')
        self.assertEqual(self.ledger.snapshot()['state']['reservations'][native_id]['state'], 'released')
        generic = self.must_succeed(lambda: self.ledger.release(identifier,
            outcome='unstarted', expected_revision=self.ledger.snapshot()['sha256'],
            idempotency_key='generic-audit-unstarted', evidence=self.proof))
        self.assertEqual(generic['state'], 'released')

    def test_cross_operation_audit_with_ordinary_key_preserves_native_status(self):
        self.cross_operation_status('ordinary')

    def test_cross_operation_audit_with_native_key_preserves_native_status(self):
        self.cross_operation_status('native-looking')

    def test_cross_operation_audit_with_ordinary_key_preserves_native_claim(self):
        self.cross_operation_claim('ordinary')

    def test_cross_operation_audit_with_native_key_preserves_native_claim(self):
        self.cross_operation_claim('native-looking')

    def test_cross_operation_audit_with_ordinary_key_preserves_native_and_generic_release(self):
        self.cross_operation_release('ordinary')

    def test_cross_operation_audit_with_native_key_preserves_native_and_generic_release(self):
        self.cross_operation_release('native-looking')

    def test_matching_native_operation_with_malformed_proof_stays_rejected(self):
        self.historical_pending(malformed=True)
        before = self.ledger.snapshot()
        journal = self.journal_path().read_bytes()
        with self.assertRaises((ValueError, RigError)):
            NativeBudget.for_run(self.root, hatch.manifest(self.root)).status()
        with self.assertRaises((ValueError, RigError)):
            self.claim()
        with self.assertRaises((ValueError, RigError)):
            self.ledger.release(self.job()['budget_operation']['reservation_id'],
                outcome='canceled-known', expected_revision=before['sha256'],
                idempotency_key='malformed-native-release', evidence=self.proof)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.journal_path().read_bytes(), journal)


if __name__ == '__main__':
    unittest.main()
