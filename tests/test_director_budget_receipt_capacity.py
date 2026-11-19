"""Public offline accounting with a real native journal and a scaled byte policy."""
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

import asset_contracts
import hatch
from director_budget import NativeBudget
from rig_errors import RigError
import test_director_budget_native as fixture


class UncertainReceiptCapacityTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    claim = fixture.NativeBudgetTests.claim
    observe = fixture.NativeBudgetTests.observe

    def job(self):
        return hatch.job_for(hatch.manifest(self.root, recovery=True), 'base')

    def check_capacity(self, prompt_size, *, require_unknown):
        # journal reads and writes use the same scaled byte limit throughout.
        limit = 128 * 1024
        with patch.object(asset_contracts, 'MAX_SOURCE_BYTES', limit), \
                patch.object(hatch, 'MAX_SOURCE_BYTES', limit):
            (self.root / 'prompts/base-pet.md').write_bytes(b'P' * prompt_size)
            handoff = self.claim()
            token = handoff['token']
            history = deepcopy(self.job()['history'])
            before = self.ledger.snapshot()
            unknown = self.observe('unknown-a', 'uncertain', amount=None)
            try:
                with hatch.ownership(self.root):
                    receipt = hatch.record_budget_usage(self.root,
                        hatch.manifest(self.root, recovery=True), 'base', token, unknown)
            except (ValueError, RigError) as error:
                self.assertFalse(require_unknown, str(error))
                # A deterministic capacity refusal must precede both durable
                # writes. A missing acknowledgement prevents all later replay.
                self.assertEqual(self.ledger.snapshot()['sha256'], before['sha256'],
                                 'Uncertain accounting committed before its receipt could fit')
                self.assertEqual(self.job()['budget_operation']['events'], [])
                resolves = []
            else:
                intent, = self.job()['budget_operation']['events']
                self.assertEqual(intent['receipt'], receipt)
                self.assertEqual(receipt['state'], 'uncertain')
                self.assertEqual(receipt['spent']['provider_calls'], '1')
                resolves = [unknown['id']]

            self.assertEqual(self.job()['status'], 'running')
            self.assertEqual(self.job()['token'], token)
            self.assertEqual(self.job()['history'], history)
            # Exercise a maximal terminal measurement from the same retained
            # token, including worst-width JSON escaping and actual distinct files.
            proofs = []
            for index in range(127):
                path = self.owner / f'synthetic-terminal-{index}.txt'
                path.write_text(f'synthetic terminal measurement {index}', encoding='ascii')
                proofs.append(self.ledger.retain_evidence(path))
            measured = '1000000000000000.' + '0' * 64
            known = self.observe('x' * 128, 'failed', resolves=resolves)
            known.update(evidence=proofs, measurement_source='\x01' * 1024,
                         usage={metric: measured for metric in known['usage']})
            with hatch.ownership(self.root):
                hatch.fail(self.root, hatch.manifest(self.root, recovery=True), 'base',
                           token, 'accounted', budget_usage=known)

            self.assertLessEqual((self.root / 'imagegen-jobs.json').stat().st_size, limit)
            job = self.job()
            self.assertEqual(job['status'], 'failed')
            self.assertNotIn('token', job)
            self.assertEqual(job['history'][:-1], history)
            self.assertEqual(job['history'][-1]['token'], token)
            final = job['budget_operation']['events'][-1]
            self.assertEqual(final['payload'], dict(token=token, **known))
            self.assertEqual(final['receipt']['state'], 'settled')
            self.assertEqual(Decimal(final['receipt']['spent']['provider_calls']),
                             Decimal(measured) + len(resolves))
            self.assertTrue(all(Decimal(v) == 0 for v in final['receipt']['held'].values()))
            settled = self.ledger.snapshot()
            with hatch.ownership(self.root):
                data = hatch.manifest(self.root, recovery=True)
                NativeBudget.for_run(self.root, data, recovery=True).reconcile()
            self.assertEqual(self.ledger.snapshot()['sha256'], settled['sha256'])

    def test_near_limit_uncertain_write_is_recoverable_or_refuses_before_accounting(self):
        self.check_capacity(69_000, require_unknown=False)

    def test_smaller_uncertain_receipt_and_maximal_terminal_resolution_control(self):
        self.check_capacity(60_000, require_unknown=True)


    def test_previous_descriptor_format_pending_intent_can_cancel_and_restart(self):
        # Produce the preceding descriptor-only proof using the actual owner and
        # ledger. A separate two-checkout run checks the real preceding writer.
        def previous_identity(job):
            fields = {key: job.get(key) for key in ('input_images', 'prompt_file', 'retry_prompt_file')}
            return {'@generation-job': hatch.object_hash(fields)}

        with patch.object(hatch, 'claim_job_identity', side_effect=previous_identity), \
                patch.object(NativeBudget, 'preflight_publication',
                             side_effect=InterruptedError('synthetic interruption before publication')):
            with self.assertRaises(InterruptedError):
                self.claim()
        old = deepcopy(self.job())
        operation = old['budget_operation']
        self.assertEqual(old['status'], 'pending')
        self.assertEqual(len(operation['attempts']), 1)
        self.assertEqual(operation['events'], [])
        self.assertNotIn('@journal-headroom', operation['handoff_hashes'])
        before = self.ledger.snapshot()
        self.assertEqual([e['action'] for e in before['state']['events']], ['reserve'])
        with self.assertRaises((ValueError, RigError)):
            self.claim()
        self.assertEqual(self.ledger.snapshot()['sha256'], before['sha256'])
        self.assertEqual(self.job(), old)

        path = self.owner / 'cancellation.txt'
        path.write_text('Synthetic preflight interruption. No handoff was published.', encoding='ascii')
        evidence = [self.ledger.retain_evidence(path)]
        with hatch.ownership(self.root):
            original = hatch.manifest(self.root, recovery=True)
            receipt = hatch.release_budget(self.root, original, 'base', outcome='unstarted',
                idempotency_key='cancel-earlier-pending', evidence=evidence)
        self.assertEqual(receipt['state'], 'released')
        released = self.job()
        self.assertEqual(released['history'], old['history'])
        self.assertEqual({k: v for k, v in released['budget_operation'].items() if k != 'events'},
                         {k: v for k, v in operation.items() if k != 'events'})
        cancellation, = released['budget_operation']['events']
        self.assertEqual(cancellation['receipt'], receipt)
        self.assertEqual(cancellation['payload']['evidence'], evidence)

        replacement = self.prepare('separate-replacement-run')
        self.bind(replacement)
        handoff = self.claim(replacement)
        self.assertNotEqual(handoff['budget_reservation_id'], operation['reservation_id'])
        self.assertNotIn(handoff['token'], operation['attempts'])
        current = self.ledger.snapshot()['state']
        self.assertEqual(current['events'][:1], before['state']['events'])
        self.assertEqual([e['action'] for e in current['events']],
                         ['reserve', 'release', 'reserve', 'native-claim'])
        row = current['reservations'][operation['reservation_id']]
        self.assertEqual(row['state'], 'released')
        self.assertTrue(all(Decimal(v) == 0 for v in (*row['spent'].values(), *row['held'].values())))
        self.assertEqual(self.job(), released)


if __name__ == '__main__':
    unittest.main()
