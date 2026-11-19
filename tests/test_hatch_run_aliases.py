"""Public Hatch calls accept an existing real directory ancestor alias.

These local synthetic fixtures model a caller retaining a legitimate path such
as macOS's /var alias, using POSIX symlinks or actual Windows junctions. Linux
execution does not constitute macOS-native or Windows execution. The alias
exists before each public call and is never substituted after native admission;
the separate, unchanged director budget boundary guards cover that rejection.
No provider calls, production artwork approvals, or real installs occur here.
"""
import argparse
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from fixture_workflow import (
    build_run, fixture_strip, hatch, record_fixture_review, small_profile,
)


class HatchRunAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='hatch-run-alias-')
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
        self.assert_alias_type()
        self.assertNotEqual(self.caller_root, self.root)
        self.assertEqual(self.caller_root.resolve(), self.root)

    def assert_alias_type(self):
        if os.name == 'nt':
            self.assertTrue(self.alias.is_junction())
        else:
            self.assertTrue(self.alias.is_symlink())

    def identity(self, data):
        return (data['run_id'], data['run_dir'],
                [(job['id'], job['output_path']) for job in data['jobs']])

    def assert_journal(self, data, identity):
        canonical = hatch.manifest(self.root)
        aliased = hatch.manifest(self.caller_root)
        self.assertEqual(canonical, data)
        self.assertEqual(aliased, canonical)
        self.assertEqual(self.identity(canonical), identity)
        self.assertEqual(canonical['run_dir'], str(self.root))
        self.assertEqual([job['id'] for job in canonical['jobs']],
                         ['base', 'rest', 'jump'])
        self.assertEqual((self.caller_root / 'imagegen-jobs.json').stat().st_ino,
                         (self.root / 'imagegen-jobs.json').stat().st_ino)
        self.assert_alias_type()

    def prepare(self, public_root):
        profile = self.owner / 'profile.json'
        hatch.atomic(profile, small_profile())
        hatch.prepare(public_root, argparse.Namespace(
            replace_inputs=False, fixture_mode=True, profile=str(profile),
            pet_name='Alias fixture', pet_notes='Synthetic offline path test',
            reference=[], avoid=[], require=[], brand_source=[],
            style_preset='clay', style_notes='Synthetic fixture only'))
        data = hatch.manifest(public_root)
        self.assert_journal(data, self.identity(data))
        return data

    def exercise_build(self, public_root):
        # Keep the caller's spelling through the unchanged fixture helper. This
        # reaches record_base_review after prepare/claim/ingest canonicalize.
        data = build_run(public_root)
        self.assert_journal(data, self.identity(data))
        self.assertTrue(hatch.status(public_root, data)['validated'])
        self.assertTrue(hatch.base_review_current(public_root, data))
        for job in data['jobs']:
            self.assertEqual(job['status'], 'generated')
            self.assertEqual(job['attempts'], 1)
            self.assertNotIn('token', job)
            self.assertEqual(job['generation_receipt']['job'], job['id'])

    def test_canonical_full_fixture_build(self):
        self.exercise_build(self.root)

    def test_aliased_ancestor_full_fixture_build(self):
        self.exercise_build(self.caller_root)

    def exercise_claim_and_ingest(self, public_root):
        data = self.prepare(public_root)
        identity = self.identity(data)
        handoff = hatch.claim(public_root, data, 'base')
        self.assert_journal(data, identity)
        self.assertEqual(hatch.job_for(data, 'base')['token'], handoff['token'])
        self.assertEqual(Path(handoff['prompt']).parent.parent, self.root)
        source = self.owner / 'source.png'
        fixture_strip({'frames': 1, 'motion': 'quiet'}, (96, 96)).save(source)
        # Bound this control to the run-root spelling. External source and
        # receipt inputs retain the canonical paths used by ingest_fixture.
        receipt = public_root.parent.resolve() / 'receipt.json'
        hatch.atomic(receipt, handoff['receipt_template'])
        result = hatch.ingest(public_root, data, 'base', source, handoff['token'],
                              'Synthetic local ingestion only.', receipt, True)
        self.assertEqual(result['generated'], 'base')
        self.assert_journal(data, identity)
        job = hatch.job_for(data, 'base')
        self.assertEqual(job['status'], 'generated')
        self.assertEqual(job['attempts'], 1)
        self.assertNotIn('token', job)
        self.assertEqual(job['generation_receipt'], handoff['receipt_template'])
        self.assertEqual(job['history'][0]['token'], handoff['token'])
        self.assertEqual(job['source_hash'], hatch.digest(source))

    def test_canonical_prepare_claim_and_ingest(self):
        self.exercise_claim_and_ingest(self.root)

    def test_aliased_ancestor_prepare_claim_and_ingest(self):
        self.exercise_claim_and_ingest(self.caller_root)

    def exercise_base_review(self, public_root):
        data = build_run(self.root, validate=False)
        identity, jobs = self.identity(data), deepcopy(data['jobs'])
        previous = deepcopy(data['base_review'])
        note = 'Second synthetic calibration checkpoint, no artwork approval.'
        result = hatch.record_base_review(public_root, data, note, True)
        self.assertTrue(result['base_reviewed'])
        self.assert_journal(data, identity)
        self.assertEqual(data['jobs'], jobs)
        self.assertEqual(data['base_review']['note'], note)
        self.assertTrue(hatch.base_review_current(public_root, data))
        self.assertTrue(any(event.get('event') == 'preserved-base-review' and
                            event['previous'] == previous for event in data['history']))

    def test_canonical_base_review(self):
        self.exercise_base_review(self.root)

    def test_aliased_ancestor_base_review(self):
        self.exercise_base_review(self.caller_root)

    def exercise_visual_review(self, public_root):
        data = build_run(self.root)
        identity, jobs = self.identity(data), deepcopy(data['jobs'])
        artifacts = hatch.artifact_hashes(self.root)
        result = record_fixture_review(public_root, data)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['fixture'])
        self.assert_journal(data, identity)
        self.assertEqual(data['jobs'], jobs)
        self.assertEqual(data['gates']['reviewed']['artifact_hashes'], artifacts)
        self.assertEqual(data['gates']['reviewed']['hash'],
                         hatch.digest(self.root / 'qa/visual-review.json'))
        self.assertTrue(hatch.status(public_root, data)['reviewed'])

    def test_canonical_visual_review(self):
        self.exercise_visual_review(self.root)

    def test_aliased_ancestor_visual_review(self):
        self.exercise_visual_review(self.caller_root)

    def exercise_repair(self, public_root):
        data = build_run(self.root)
        record_fixture_review(self.root, data)
        identity = self.identity(data)
        unaffected = {job['id']: deepcopy(job) for job in data['jobs']
                      if job['id'] != 'jump'}
        previous_hash = hatch.job_for(data, 'jump')['output_hash']
        reason = 'Synthetic repair: increase the jump peak.'
        result = hatch.repair(public_root, data, 'jump', reason)
        self.assertEqual(result['pending'], ['jump'])
        self.assert_journal(data, identity)
        for name, previous in unaffected.items():
            self.assertEqual(hatch.job_for(data, name), previous)
        job = hatch.job_for(data, 'jump')
        self.assertEqual(job['status'], 'pending')
        self.assertEqual(job['attempts'], 0)
        self.assertEqual(job['history'][-1]['previous_hash'], previous_hash)
        self.assertEqual(job['history'][-1]['reason'], reason)
        self.assertNotIn('reviewed', data['gates'])
        for key in ('prompt_file', 'retry_prompt_file'):
            self.assertIn(reason, (self.root / job[key]).read_text(encoding='utf-8'))

    def test_canonical_row_repair(self):
        self.exercise_repair(self.root)

    def test_aliased_ancestor_row_repair(self):
        self.exercise_repair(self.caller_root)

    def exercise_failure(self, public_root):
        data = self.prepare(self.root)
        identity = self.identity(data)
        handoff = hatch.claim(self.root, data, 'base')
        inputs = deepcopy(hatch.job_for(data, 'base')['input_hashes'])
        result = hatch.fail(public_root, data, 'base', handoff['token'],
                            'Synthetic local failure, no provider was called.')
        self.assertEqual(result['failed'], 'base')
        self.assert_journal(data, identity)
        job = hatch.job_for(data, 'base')
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('token', job)
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['input_hashes'], inputs)
        self.assertEqual(job['history'][-1]['token'], handoff['token'])

    def test_canonical_active_handoff_failure(self):
        self.exercise_failure(self.root)

    def test_aliased_ancestor_active_handoff_failure(self):
        self.exercise_failure(self.caller_root)


if __name__ == '__main__':
    unittest.main()
