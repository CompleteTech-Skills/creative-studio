"""Offline direct journal and import calls through preexisting actual aliases.

Use real POSIX symlinks or Windows junctions, with no simulated alias or skip
fallback. Linux execution models the macOS ancestor-alias condition; it does
not establish macOS-native or Windows execution. The unchanged independent
director budget boundary guards remain authoritative for late substitutions.
All artwork and operation records here are synthetic temporary test inputs.
"""
import argparse
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from fixture_workflow import fixture_strip, hatch, small_profile
from source_interface import import_source


class HatchDirectAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='hatch-direct-alias-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.owner = self.home / 'physical-owner'
        self.owner.mkdir()
        self.alias = self.home / 'caller-owner'
        self.assertIn(os.name, ('posix', 'nt'), 'requires actual supported OS aliases')
        if os.name == 'nt':
            result = subprocess.run(
                ['cmd', '/d', '/c', 'mklink', '/J', str(self.alias), str(self.owner)],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.alias.symlink_to(self.owner, target_is_directory=True)
        self.root = self.owner / 'run'
        self.caller_root = self.alias / 'run'
        self.assert_alias()
        self.assertNotEqual(self.caller_root, self.root)
        self.assertEqual(self.caller_root.resolve(), self.root)
        profile = self.owner / 'profile.json'
        hatch.atomic(profile, small_profile())
        hatch.prepare(self.root, argparse.Namespace(
            replace_inputs=False, fixture_mode=True, profile=str(profile),
            pet_name='Direct alias fixture', pet_notes='Synthetic offline path test',
            reference=[], avoid=[], require=[], brand_source=[],
            style_preset='clay', style_notes='Synthetic fixture only'))
        self.data = hatch.manifest(self.root)
        self.identity = (self.data['run_id'], self.data['run_dir'],
                         [(job['id'], job['output_path']) for job in self.data['jobs']])

    def assert_alias(self):
        if os.name == 'nt':
            self.assertTrue(self.alias.is_junction())
        else:
            self.assertTrue(self.alias.is_symlink())

    def assert_journal(self):
        canonical = hatch.manifest(self.root)
        self.assertEqual(canonical, self.data)
        self.assertEqual(hatch.manifest(self.caller_root), canonical)
        self.assertEqual((canonical['run_id'], canonical['run_dir'],
                          [(job['id'], job['output_path']) for job in canonical['jobs']]),
                         self.identity)
        self.assertEqual(canonical['run_dir'], str(self.root))
        self.assertEqual((self.caller_root / 'imagegen-jobs.json').stat().st_ino,
                         (self.root / 'imagegen-jobs.json').stat().st_ino)
        self.assert_alias()

    def artifact_hashes(self):
        return {path.relative_to(self.root).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*')
                if path.is_file() and path.name != 'imagegen-jobs.json'}

    def exercise_direct_save(self, public_root):
        artifacts = self.artifact_hashes()
        jobs = deepcopy(self.data['jobs'])
        history = deepcopy(self.data['history'])
        retained_event = {'event': 'synthetic-direct-save',
                          'note': 'Offline journal history preservation only.'}
        operation = {'id': 'synthetic-interrupted-operation', 'kind': 'validate',
                     'status': 'running'}
        self.data['history'].append(retained_event)
        self.data['operation'] = operation
        hatch.save(public_root, self.data)
        self.assert_journal()
        self.assertEqual(self.data['jobs'], jobs)
        self.assertEqual(self.data['history'], history + [retained_event])
        self.assertEqual(hatch.read(self.root / 'imagegen-jobs.json')['operation'], operation)
        self.assertEqual(self.artifact_hashes(), artifacts)

    def test_canonical_direct_save_preserves_prepared_journal(self):
        self.exercise_direct_save(self.root)

    def test_aliased_ancestor_direct_save_preserves_prepared_journal(self):
        self.exercise_direct_save(self.caller_root)

    def exercise_import(self, public_root):
        source = self.owner / 'synthetic-original.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(source)
        descriptor = self.owner / 'synthetic-source.json'
        origin = 'Synthetic local test artwork; no production art acceptance.'
        hatch.atomic(descriptor, {'schema_version': 1, 'kind': 'import',
                                 'layout': 'image', 'files': [source.name],
                                 'origin': origin, 'fixture': True})
        originals = {path: path.read_bytes() for path in (source, descriptor)}
        history = deepcopy(self.data['history'])
        jobs = deepcopy(self.data['jobs'])
        note = 'Synthetic source import and attribution test only.'
        result = import_source(public_root, self.data, 'base', descriptor, note)
        self.assertEqual(result, {'imported': 'base', 'origin': origin, 'fixture': True,
                                  'imagegen_output': False, 'visual_review_required': True})
        self.assert_journal()
        self.assertEqual(self.data['history'][:len(history)], history)
        job = hatch.job_for(self.data, 'base')
        self.assertEqual(job['status'], 'generated')
        self.assertEqual(job['source_origin'], 'import')
        self.assertEqual(job['attempts'], 0)
        self.assertNotIn('token', job)
        self.assertNotIn('generation_receipt', job)
        self.assertEqual(job['history'][:-1], jobs[0]['history'])
        receipt = job['import_receipt']
        self.assertEqual(receipt['origin'], origin)
        self.assertEqual(receipt['note'], note)
        self.assertTrue(receipt['fixture'])
        self.assertEqual(receipt['descriptor_sha256'], hatch.digest(descriptor))
        self.assertEqual(receipt['files'], [{'origin_path': str(source),
                                             'sha256': hatch.digest(source)}])
        self.assertEqual(job['history'][-1]['receipt'], receipt)
        output = self.root / job['output_path']
        self.assertEqual(job['output_hash'], hatch.digest(output))
        self.assertEqual(job['canonical_hash'], hatch.digest(self.root / 'references/canonical-base.png'))
        self.assertEqual(output.read_bytes(), (self.root / 'references/canonical-base.png').read_bytes())
        self.assertEqual((public_root / job['output_path']).stat().st_ino, output.stat().st_ino)
        for previous in jobs[1:]:
            current = hatch.job_for(self.data, previous['id'])
            for key in ('id', 'status', 'attempts', 'history', 'output_path'):
                self.assertEqual(current[key], previous[key])
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_canonical_synthetic_base_import_preserves_originals(self):
        self.exercise_import(self.root)

    def test_aliased_ancestor_synthetic_base_import_preserves_originals(self):
        self.exercise_import(self.caller_root)


if __name__ == '__main__':
    unittest.main()
