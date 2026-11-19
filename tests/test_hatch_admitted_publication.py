"""Fresh save destinations and previously admitted lifecycle writers stay distinct."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_hatch_direct_aliases as fixture
from source_interface import import_source

hatch = fixture.hatch


class AdmittedPublicationTests(unittest.TestCase):
    setUp = fixture.HatchDirectAliasTests.setUp
    assert_alias = fixture.HatchDirectAliasTests.assert_alias

    def test_plain_journal_cannot_redirect_direct_save_with_its_recorded_root(self):
        other = self.home / 'unrelated'
        other.mkdir()
        sentinel = other / 'imagegen-jobs.json'
        sentinel.write_bytes(b'Unrelated synthetic owner must remain unchanged.\n')
        original = sentinel.read_bytes()
        journal = json.loads((self.root / 'imagegen-jobs.json').read_text())
        journal['run_dir'] = str(other)
        journal['history'].append({'event': 'synthetic-direct-write'})
        hatch.save(self.caller_root, journal)
        self.assertEqual(json.loads((self.root / 'imagegen-jobs.json').read_text()), journal)
        self.assertEqual(sentinel.read_bytes(), original)

    def test_import_original_inside_aliased_run_is_refused_before_publication(self):
        source = self.root / 'synthetic-original.png'
        fixture.fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(source)
        descriptor = self.owner / 'internal-source.json'
        hatch.atomic(descriptor, {'schema_version': 1, 'kind': 'import', 'layout': 'image',
                                  'files': [str(source)], 'origin': 'Synthetic ownership test',
                                  'fixture': True})
        before = (self.root / 'imagegen-jobs.json').read_bytes(), source.read_bytes()
        with self.assertRaisesRegex(ValueError, 'outside the run'):
            import_source(self.caller_root, self.data, 'base', descriptor, 'Synthetic boundary only')
        self.assertEqual(((self.root / 'imagegen-jobs.json').read_bytes(), source.read_bytes()), before)
        self.assertFalse((self.root / 'decoded/base.png').exists())

    def test_ancestor_substitution_after_claim_admission_cannot_redirect_save(self):
        other = self.home / 'unrelated'
        (other / 'run').mkdir(parents=True)
        sentinel = other / 'run/imagegen-jobs.json'
        sentinel.write_bytes(b'Unrelated synthetic journal.\n')
        before, alternate = (self.root / 'imagegen-jobs.json').read_bytes(), sentinel.read_bytes()
        retained = self.home / 'retained-owner'
        actual_save = hatch.save
        reached = []

        def replace_before_publication(root, data):
            if hatch.job_for(data, 'base')['status'] != 'running':
                return actual_save(root, data)
            self.assertEqual(Path(root), self.root)
            self.owner.rename(retained)
            try:
                if os.name == 'nt':
                    result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(self.owner),
                                             str(other)], capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(self.owner.is_junction())
                else:
                    self.owner.symlink_to(other, target_is_directory=True)
                    self.assertTrue(self.owner.is_symlink())
                reached.append(True)
                return actual_save(root, data)
            finally:
                if os.name == 'nt':
                    self.owner.rmdir()
                else:
                    self.owner.unlink()
                retained.rename(self.owner)

        with patch.object(hatch, 'save', side_effect=replace_before_publication), \
                self.assertRaises((OSError, ValueError)):
            hatch.claim(self.root, self.data, 'base')
        self.assertEqual(reached, [True])
        self.assertEqual(sentinel.read_bytes(), alternate)
        self.assertEqual((self.root / 'imagegen-jobs.json').read_bytes(), before)
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['status'], 'pending')
