"""Canonical captured inputs survive the outer receipt; old history stays usable."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import hatch
import production_workflow as workflow
from director_budget import bind_native_budget
import test_director_budget_native as fixture


class FinalHandoffTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind
    project_enrollment_fixture = fixture.NativeBudgetTests.project_enrollment_fixture

    def test_unbudgeted_retry_preserves_pre_projection_history(self):
        root = self.prepare('unbudgeted-historical-shape')
        first = hatch.claim(root, hatch.manifest(root), 'base')
        hatch.fail(root, hatch.manifest(root), 'base', first['token'], 'Synthetic failed attempt')
        data = hatch.manifest(root)
        job = hatch.job_for(data, 'base')
        # A synthetic retained old-format history. The separately retained
        # two-checkout comparator uses the actual original schema-2 CLI.
        for entry in job['history']:
            if entry.get('event') == 'claim':
                entry['input_hashes'].pop('@generation-profile')
                entry['input_hashes']['profile.json'] = hatch.input_digest(root / 'profile.json')
        before = deepcopy(job['history'])
        hatch.save(root, data)
        result = hatch.claim(root, hatch.manifest(root), 'base')
        saved = hatch.job_for(hatch.manifest(root), 'base')
        self.assertEqual(saved['history'][:len(before)], before)
        self.assertEqual((saved['status'], saved['attempts']), ('running', 2))
        self.assertEqual(result['token'], saved['token'])
        self.assertNotEqual(result['token'], first['token'])
        self.assertIn('@generation-profile', result['receipt_template']['input_hashes'])

    def check_final_receipt(self, *, budgeted):
        for leaf in ('prompts/base-pet.md', 'pet_request.json', 'profile.json',
                     'extraction-settings.json', 'references/final-reference.png'):
            with self.subTest(budgeted=budgeted, changed=leaf):
                project, runs, ledger, proof = self.project_enrollment_fixture(
                    'final-' + str(budgeted) + '-' + leaf.replace('/', '-'))
                run = runs[0]
                attachment = run / 'references/final-reference.png'
                attachment.parent.mkdir(exist_ok=True)
                hatch.Image.new('RGBA', (2, 2), (17, 31, 47, 255)).save(attachment)
                data = hatch.manifest(run)
                hatch.job_for(data, 'base')['input_images'].append(
                    {'path': 'references/final-reference.png', 'role': 'reference'})
                hatch.save(run, data)
                if budgeted:
                    for bound in runs:
                        bind_native_budget(bound, ledger.root, owner_root=project, provider='imagegen',
                            estimate=fixture.quantities(provider_calls=1, amount=2),
                            retry_allowance=0, evidence=proof)
                real_save = workflow._save
                observed = []

                def change_after_receipt(*args, **kwargs):
                    result = real_save(*args, **kwargs)
                    observed.append(ledger.snapshot()['sha256'])
                    path = run / leaf
                    if path.exists():
                        # Whitespace preserves JSON meaning while changing the
                        # exact captured identity; image and prompt bytes differ.
                        path.write_bytes(path.read_bytes() + b'\n ')
                    else:
                        path.write_text('{}\n')
                    return result

                with patch.object(workflow, '_save', change_after_receipt):
                    with self.assertRaisesRegex(ValueError, 'generation inputs changed'):
                        workflow.claim_generation(project, 'first', 'base')
                self.assertEqual(len(observed), 1)
                self.assertEqual(ledger.snapshot()['sha256'], observed[0])
                saved = hatch.job_for(hatch.input_json(run / 'imagegen-jobs.json'), 'base')
                self.assertEqual((saved['status'], saved['attempts']), ('running', 1))
                self.assertTrue(saved['token'])
                rows = list(ledger.snapshot()['state']['reservations'].values())
                if budgeted:
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['state'], 'reserved')
                    self.assertEqual(rows[0]['spent']['provider_calls'], '0')
                    self.assertEqual(rows[0]['held']['provider_calls'], '1')
                else:
                    self.assertEqual(rows, [])

    def test_budgeted_production_receipt_preserves_exact_captured_inputs(self):
        self.check_final_receipt(budgeted=True)

    def test_unbudgeted_production_receipt_preserves_exact_captured_inputs(self):
        self.check_final_receipt(budgeted=False)


if __name__ == '__main__':
    unittest.main()
