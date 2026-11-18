"""Ordinary-directory replacement stays inside disposable synthetic fixtures.

Hooks schedule real filesystem renames immediately before the original writer;
they do not replace journal publication, input reads, or ledger accounting.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import hatch
from director_budget import NativeBudget
import test_director_budget_native as fixture


class JournalDirectoryIdentityTests(unittest.TestCase):
    setUp = fixture.NativeBudgetTests.setUp
    prepare = fixture.NativeBudgetTests.prepare
    bind = fixture.NativeBudgetTests.bind

    def replace_during_claim(self, *, ancestor=False):
        if ancestor:
            self.root = self.prepare('acquisitions/nested-run')
            self.bind(self.root)
        original_save = hatch.save
        observations = []
        moved = self.root.parent if ancestor else self.root
        retained = self.owner / 'retained-synthetic-directory'
        sentinel = b'Unrelated synthetic journal must remain byte-identical.\n'

        def substitute(target, data):
            if hatch.job_for(data, 'base')['status'] != 'running':
                return original_save(target, data)
            self.assertEqual(Path(target), self.root)
            original = (self.root / 'imagegen-jobs.json').read_bytes()
            ledger = self.ledger.snapshot()['sha256']
            moved.rename(retained)
            self.root.mkdir(parents=True)
            journal = self.root / 'imagegen-jobs.json'
            journal.write_bytes(sentinel)
            try:
                return original_save(target, data)
            finally:
                observations.append((journal.read_bytes(), original, ledger))
                shutil.rmtree(moved)
                retained.rename(moved)

        refused = False
        try:
            with hatch.ownership(self.root), patch.object(hatch, 'save', substitute):
                hatch.claim(self.root, hatch.manifest(self.root), 'base')
        except (OSError, ValueError):
            refused = True
        self.assertEqual(len(observations), 1)
        written, original, ledger = observations[0]
        self.assertEqual(written, sentinel, 'a replacement ordinary directory was overwritten')
        self.assertTrue(refused, 'replacement must not produce a usable claim')
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), original)
        self.assertEqual(self.ledger.snapshot()['sha256'], ledger)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'pending')

    def test_claim_refuses_replaced_ordinary_run_before_writing(self):
        self.replace_during_claim()

    def test_claim_refuses_replaced_ordinary_ancestor_before_writing(self):
        self.replace_during_claim(ancestor=True)

    def test_budget_object_retains_admission_after_constructor_returns(self):
        budget = NativeBudget(self.root, hatch.manifest(self.root))
        original = (self.root / 'imagegen-jobs.json').read_bytes()
        ledger = self.ledger.snapshot()['sha256']
        retained = self.owner / 'retained-synthetic-run'
        self.root.rename(retained)
        self.root.mkdir()
        journal = self.root / 'imagegen-jobs.json'
        sentinel = b'Synthetic replacement owner.\n'
        journal.write_bytes(sentinel)
        try:
            refused = False
            try:
                budget._save()
            except (OSError, ValueError):
                refused = True
            self.assertEqual(journal.read_bytes(), sentinel)
            self.assertTrue(refused)
        finally:
            shutil.rmtree(self.root)
            retained.rename(self.root)
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), original)
        self.assertEqual(self.ledger.snapshot()['sha256'], ledger)

    def test_explicit_direct_save_admits_its_current_directory(self):
        destination = self.owner / 'explicit-new-destination'
        destination.mkdir()
        data = {'synthetic': True, 'run_dir': str(self.root)}
        before = (self.root / 'imagegen-jobs.json').read_bytes()
        hatch.save(destination, data)
        self.assertEqual(json.loads((destination / 'imagegen-jobs.json').read_text()), data)
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), before)

    def test_post_save_replacement_cannot_gain_ledger_publication_authority(self):
        original_save = hatch.save
        retained = self.owner / 'retained-after-save'
        observed = []

        def replace_after_save(target, data):
            result = original_save(target, data)
            if hatch.job_for(data, 'base')['status'] == 'running':
                observed.append(self.ledger.snapshot()['sha256'])
                self.root.rename(retained)
                shutil.copytree(retained, self.root)
            return result

        refused = False
        try:
            with hatch.ownership(self.root), patch.object(hatch, 'save', replace_after_save):
                hatch.claim(self.root, hatch.manifest(self.root), 'base')
        except (OSError, ValueError, fixture.RigError):
            refused = True
        finally:
            if retained.exists():
                self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(),
                                 (retained / 'imagegen-jobs.json').read_bytes())
                shutil.rmtree(self.root)
                retained.rename(self.root)
        self.assertEqual(len(observed), 1)
        self.assertTrue(refused, 'a replacement cannot acquire published claim authority')
        self.assertEqual(self.ledger.snapshot()['sha256'], observed[0])
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'running')

    def test_parent_after_preexisting_alias_keeps_native_path_meaning(self):
        physical_run = self.prepare('physical/run')
        self.bind(physical_run)
        target = self.owner / 'physical/nested'
        target.mkdir()
        alias = self.owner / 'caller'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(alias), str(target)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(alias.is_junction())
        else:
            alias.symlink_to(target, target_is_directory=True)
            self.assertTrue(alias.is_symlink())
        caller = alias / '..' / 'run'
        expected = caller.resolve()
        self.assertIn(expected, (self.root, physical_run))
        self.assertTrue(caller.samefile(expected))
        other = physical_run if expected == self.root else self.root
        original = (other / 'imagegen-jobs.json').read_bytes()
        handoff = hatch.claim(caller, hatch.manifest(caller), 'base')
        self.assertEqual(hatch.job_for(hatch.manifest(expected), 'base')['token'], handoff['token'])
        self.assertEqual((other / 'imagegen-jobs.json').read_bytes(), original)

    def test_replacement_during_ledger_commit_cannot_return_a_handoff(self):
        real_change = fixture.BudgetLedger._change
        retained = self.owner / 'retained-during-ledger'
        reached = []

        def replace_during_commit(ledger, expected_revision, key, action, payload, mutate):
            if action != 'native-claim':
                return real_change(ledger, expected_revision, key, action, payload, mutate)
            reached.append(True)
            # Native Windows pins may refuse the rename itself. POSIX exercises
            # the real rename and the required post-publication identity check.
            self.root.rename(retained)
            shutil.copytree(retained, self.root)
            return real_change(ledger, expected_revision, key, action, payload, mutate)

        refused = False
        try:
            with hatch.ownership(self.root), patch.object(fixture.BudgetLedger, '_change', replace_during_commit):
                hatch.claim(self.root, hatch.manifest(self.root), 'base')
        except (OSError, ValueError, fixture.RigError):
            refused = True
        finally:
            if retained.exists():
                self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(),
                                 (retained / 'imagegen-jobs.json').read_bytes())
                shutil.rmtree(self.root)
                retained.rename(self.root)
        self.assertEqual(reached, [True])
        self.assertTrue(refused)
        job = hatch.job_for(hatch.manifest(self.root), 'base')
        self.assertEqual(job['status'], 'running')
        row = self.ledger.snapshot()['state']['reservations'][job['budget_operation']['reservation_id']]
        self.assertEqual(row['state'], 'reserved')
        self.assertEqual(row['spent']['provider_calls'], '0')
        self.assertEqual(row['held']['provider_calls'], '3')


if __name__ == '__main__':
    unittest.main()
