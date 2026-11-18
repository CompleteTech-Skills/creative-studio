"""Lifecycle regressions execute only local deterministic fixtures and safe temp installs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fixture_workflow import hatch, build_run, ingest_fixture, record_fixture_review, small_profile
from profile_spec import load_profile
from PIL import Image


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hatch-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'run'

    def build(self, profile=None):
        self.data = build_run(self.root, profile)
        return self.data

    def accept_and_package(self):
        record_fixture_review(self.root, self.data)
        self.package = self.base / 'package'
        hatch.package(self.root, self.data, self.package)

    def test_custom_full_lifecycle_and_exact_export(self):
        self.build()
        self.assertTrue(hatch.status(self.root, self.data)['validated'])
        self.accept_and_package()
        hatch.export(self.root, self.data, self.base / 'export')
        export = hatch.read(self.base / 'export/animation.json')
        self.assertEqual([s['id'] for s in export['states']], ['rest', 'jump'])
        self.assertEqual([f['duration_ms'] for f in export['states'][0]['frames']], [40, 70, 90])
        self.assertEqual(export['events'], {'ready': 'rest', 'success': 'jump'})
        self.assertTrue(export['fixture'])
        self.assertFalse(export['integration_verified'])
        receipt = hatch.read(self.base / 'export/export-receipt.json')
        self.assertIn('provenance.json', receipt['files'])
        self.assertIn('deterministic_metrics', hatch.read(self.base / 'export/provenance.json'))
        self.assertTrue(hatch.status(self.root, self.data)['packaged'])

    def test_default_contract_full_fixture(self):
        self.build(load_profile())
        with Image.open(self.root / 'final/spritesheet.webp') as image:
            self.assertEqual(image.size, (1536, 1872))
        self.assertEqual(len(hatch.read(self.root / 'frames/frames-manifest.json')['rows']), 9)
        self.assertTrue(hatch.status(self.root, self.data)['validated'])

    def test_edited_review_and_deleted_package_not_reported_accepted(self):
        self.build()
        self.accept_and_package()
        saved = (self.root / 'qa/visual-review.json').read_bytes()
        (self.root / 'qa/visual-review.json').write_text('{}')
        status = hatch.status(self.root, self.data)
        self.assertTrue(status['validated'])
        self.assertFalse(status['reviewed'])
        self.assertFalse(status['packaged'])
        (self.root / 'qa/visual-review.json').write_bytes(saved)
        (self.package / 'receipt.json').unlink()
        self.assertFalse(hatch.status(self.root, self.data)['packaged'])

    def test_failed_review_blocks_package_and_install(self):
        self.build()
        self.accept_and_package()
        record_fixture_review(self.root, self.data, 'fail')
        with self.assertRaisesRegex(ValueError, 'visual review'):
            hatch.install(self.root, self.data, self.package, self.base / 'installed-test', True)
        with self.assertRaisesRegex(ValueError, 'visual review'):
            hatch.package(self.root, self.data, self.base / 'another-package')
        self.assertFalse((self.base / 'installed-test').exists())

    def test_partial_repair_keeps_other_row_evidence(self):
        self.build()
        record_fixture_review(self.root, self.data)
        before = hatch.read(self.root / 'frames/frames-manifest.json')
        untouched = next(r for r in before['rows'] if r['state'] == 'rest')
        hatch.repair(self.root, self.data, 'jump', 'Increase peak height, retain red body and avoid glitter.')
        source = self.base / 'fixture-sources/jump.png'
        ingest_fixture(self.root, self.data, 'jump', source)
        result = hatch.validate(self.root, self.data)
        # Same raster source is permitted; the repair prompt still changes the generation evidence.
        after = hatch.read(self.root / 'frames/frames-manifest.json')
        self.assertEqual(next(r for r in after['rows'] if r['state'] == 'rest'), untouched)
        self.assertFalse(hatch.status(self.root, self.data)['reviewed'])
        for folder in ('rows', 'row-retries'):
            prompt = (self.root / f'prompts/{folder}/jump.md').read_text(encoding='utf-8')
            self.assertIn('red body', prompt)
            self.assertIn('glitter', prompt)
            self.assertIn('Increase peak', prompt)

    def test_input_change_invalidates_generation_and_approval(self):
        self.build()
        self.accept_and_package()
        request = hatch.read(self.root / 'pet_request.json')
        request['constraints']['avoid'].append('text')
        hatch.atomic(self.root / 'pet_request.json', request)
        result = hatch.status(self.root, self.data)
        self.assertFalse(result['validated'])
        self.assertFalse(result['reviewed'])
        self.assertTrue(all(j['status'] == 'pending' for j in result['jobs']))
        with self.assertRaisesRegex(ValueError, 'handoffs'):
            hatch.validate(self.root, self.data)

    def test_changed_frame_cannot_keep_review(self):
        self.build()
        self.accept_and_package()
        frame = self.root / 'frames/rest/00.png'
        with Image.open(frame) as image:
            altered = image.convert('RGBA')
        altered.putpixel((40, 40), (255, 0, 0, 255))
        altered.save(frame)
        self.assertFalse(hatch.status(self.root, self.data)['reviewed'])

    def test_retry_bound_and_inflight_resume(self):
        data = build_run(self.root, validate=False)
        hatch.repair(self.root, data, 'jump', 'Fix the landing.')
        first = hatch.claim(self.root, data, 'jump')
        resumed = hatch.resume(self.root, data)
        self.assertEqual(hatch.job_for(data, 'jump')['token'], first['token'])
        self.assertIn('Reconcile', resumed['next'])
        with self.assertRaisesRegex(ValueError, 'already running'):
            hatch.claim(self.root, data, 'jump')
        hatch.fail(self.root, data, 'jump', first['token'], 'Fixture simulated failure')
        for _ in range(2):
            handoff = hatch.claim(self.root, data, 'jump')
            hatch.fail(self.root, data, 'jump', handoff['token'], 'Fixture simulated failure')
        with self.assertRaisesRegex(ValueError, 'retry bound'):
            hatch.claim(self.root, data, 'jump')
        self.assertEqual(len([e for e in hatch.job_for(data, 'jump')['history'] if e['event'] == 'failed']), 3)

    def test_atomic_failure_and_interrupted_operation(self):
        path = self.base / 'record.json'
        hatch.atomic(path, {'old': 1})
        with patch.object(hatch.os, 'replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): hatch.atomic(path, {'new': 2})
        self.assertEqual(hatch.read(path), {'old': 1})
        self.assertEqual(list(self.base.glob('*.tmp')), [])
        self.build()
        self.data['operation'] = {'kind': 'validate', 'status': 'running', 'id': 'interrupted-fixture'}
        hatch.save(self.root, self.data)
        hatch.resume(self.root, self.data)
        self.assertEqual(self.data['operation']['status'], 'interrupted')
        hatch.validate(self.root, self.data)
        self.assertTrue(hatch.status(self.root, self.data)['validated'])

    def test_lock_excludes_other_process_and_releases(self):
        self.build()
        cmd = [sys.executable, str(hatch.SCRIPTS / 'hatch.py'), 'status', '--run-dir', str(self.root)]
        with hatch.ownership(self.root):
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('owned by another', result.stderr)
        self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)

    def test_install_failures_and_rollback_preserve_old_package(self):
        self.build()
        self.accept_and_package()
        target = self.base / 'isolated-test-install'
        target.mkdir()
        hatch.atomic(target / 'pet.json', {'id': 'old', 'spritesheetPath': 'spritesheet.webp'})
        Image.new('RGBA', (10, 10), 'blue').save(target / 'spritesheet.webp')
        before = hatch.tree_hashes(target)
        for flag in ('fail_after_backup', 'fail_after_promote'):
            with self.assertRaisesRegex(OSError, 'injected'):
                hatch.install(self.root, self.data, self.package, target, True, **{flag: True})
            self.assertEqual(hatch.tree_hashes(target), before)
        result = hatch.install(self.root, self.data, self.package, target, True)
        self.assertTrue(Path(result['backup']).is_dir())
        hatch.rollback(self.root, self.data)
        self.assertEqual(hatch.tree_hashes(target), before)

    def test_missing_provenance_never_accepted_and_rebuilt(self):
        self.build()
        (self.root / 'frames/frames-manifest.json').unlink()
        with self.assertRaisesRegex(ValueError, 'stale'):
            hatch.require_valid(self.root, self.data)
        hatch.validate(self.root, self.data)
        self.assertTrue(hatch.status(self.root, self.data)['validated'])

    def test_cleanup_preview_then_archive_preserves_repair_and_unrelated_files(self):
        self.build()
        self.accept_and_package()
        unrelated = self.root / 'frames/user-image.png'
        unrelated.write_bytes(b'user-owned')
        # It is outside state frame sets. Revalidate and review the now-visible inventory.
        hatch.validate(self.root, self.data)
        record_fixture_review(self.root, self.data)
        unknown_preview = self.root / 'qa/previews/user-note.txt'
        unknown_preview.write_text('retain')
        hatch.validate(self.root, self.data)
        record_fixture_review(self.root, self.data)
        alias = self.base / 'run-alias'
        alias.symlink_to(self.root.resolve(), target_is_directory=True)
        preview = hatch.cleanup(alias, self.data, 'repair', False)
        self.assertTrue((self.root / 'frames/rest/00.png').exists())
        self.assertNotIn(str(unrelated.resolve()), preview['remove'])
        self.assertNotIn(str(unknown_preview.resolve()), preview['remove'])
        hatch.cleanup(alias, self.data, 'repair', True)
        archive = Path(self.data['retention']['archive'])
        with zipfile.ZipFile(archive) as saved:
            self.assertIn('decoded/jump.png', saved.namelist())
            self.assertIn('references/canonical-base.png', saved.namelist())
            self.assertIn('qa/visual-review.json', saved.namelist())
        self.assertEqual(unrelated.read_bytes(), b'user-owned')
        self.assertEqual(unknown_preview.read_text(), 'retain')
        self.assertTrue((self.root / 'decoded/jump.png').is_file())
        hatch.resume(self.root, self.data)
        hatch.validate(self.root, self.data)
        self.assertTrue(hatch.status(self.root, self.data)['validated'])

    def test_minimal_retention_is_distinct_and_honest(self):
        self.build()
        record_fixture_review(self.root, self.data)
        hatch.cleanup(self.root, self.data, 'minimal-delivery', True)
        self.assertFalse((self.root / 'decoded/jump.png').exists())
        self.assertTrue((self.root / 'references/canonical-base.png').exists())
        with zipfile.ZipFile(self.data['retention']['archive']) as saved:
            self.assertNotIn('decoded/jump.png', saved.namelist())
        status = hatch.resume(self.root, self.data)
        self.assertFalse(status['validated'])
        self.assertEqual(hatch.job_for(self.data, 'jump')['status'], 'pending')

    def test_prepare_recovers_before_and_after_old_directory_rename(self):
        self.build()
        alias = self.base / 'parent-alias'
        alias.symlink_to(self.base.resolve(), target_is_directory=True)
        original_id = self.data['run_id']
        stage = self.base / '.run-prepare-fixture'
        new_data = build_run(stage, validate=False)
        backup = self.base / 'run.previous-fixture'
        journal = self.base / '.run.prepare.json'
        hatch.atomic(journal, {'stage': str(stage), 'backup': str(backup), 'run_id': new_data['run_id'],
                             'previous_manifest_hash': hatch.digest(self.root / 'imagegen-jobs.json')})
        hatch.recover_prepare(alias / self.root.name)
        self.assertEqual(hatch.manifest(self.root)['run_id'], new_data['run_id'])
        self.assertEqual(hatch.manifest(backup)['run_id'], original_id)
        self.assertFalse(journal.exists())
        # Journal after original rename, before promotion: no new root yet.
        stage2 = self.base / '.run-prepare-fixture2'
        new2 = build_run(stage2, validate=False)
        backup2 = self.base / 'run.previous-fixture2'
        hatch.atomic(journal, {'stage': str(stage2), 'backup': str(backup2), 'run_id': new2['run_id'],
                             'previous_manifest_hash': hatch.digest(self.root / 'imagegen-jobs.json')})
        os.replace(self.root, backup2)
        hatch.recover_prepare(alias / self.root.name)
        self.assertEqual(hatch.manifest(self.root)['run_id'], new2['run_id'])

    def test_install_recovery_before_legacy_backup_and_after_promotion(self):
        self.build()
        self.accept_and_package()
        import shutil
        target = self.base / 'recovery-test-install'
        target.mkdir()
        hatch.atomic(target / 'pet.json', {'id': 'old', 'spritesheetPath': 'spritesheet.webp'})
        Image.new('RGBA', (10, 10), 'blue').save(target / 'spritesheet.webp')
        before = hatch.tree_hashes(target)
        stage = self.base / '.recovery-test-install-stage-fixture'
        backup = self.base / 'recovery-test-install.previous-fixture'
        shutil.copytree(self.package, stage)
        journal = self.base / '.recovery-test-install.install.json'
        hatch.atomic(journal, {'stage': str(stage), 'backup': str(backup),
                             'receipt_hash': hatch.digest(stage / 'receipt.json'), 'previous_hashes': before})
        recovered = hatch.recover_install(target)
        self.assertEqual(recovered['recovered'], 'committed')
        self.assertEqual(hatch.tree_hashes(backup), before)
        self.assertEqual(hatch.verify_package(target)['run_id'], self.data['run_id'])
        # A crash after promotion recognizes exactly the promoted package and retains prior files.
        hatch.atomic(journal, {'stage': str(stage), 'backup': str(backup),
                             'receipt_hash': hatch.digest(target / 'receipt.json'), 'previous_hashes': before})
        self.assertEqual(hatch.recover_install(target)['recovered'], 'committed')
        self.assertEqual(hatch.tree_hashes(backup), before)

    def test_configure_invalidates_rows_but_preserves_base_provenance(self):
        self.build()
        original_request = (self.root / 'pet_request.json').read_bytes()
        original_base_receipt = hatch.job_for(self.data, 'base')['generation_receipt']
        hatch.configure(self.root, self.data, argparse.Namespace(background='chroma', holes=None,
                        shared_scale=None, key_threshold=None, chroma_key='#0000FF'))
        self.assertEqual((self.root / 'pet_request.json').read_bytes(), original_request)
        self.assertEqual(hatch.job_for(self.data, 'base')['generation_receipt'], original_base_receipt)
        self.assertEqual(hatch.job_for(self.data, 'base')['status'], 'generated')
        self.assertTrue(all(j['status'] == 'pending' for j in self.data['jobs'] if j['id'] != 'base'))
        self.assertIn('#0000FF', (self.root / 'prompts/rows/rest.md').read_text())

    def test_generating_and_deriving_have_one_active_origin(self):
        profile = small_profile()
        profile['states'][1]['allowed_derivations'] = [
            {'kind': 'horizontal-mirror', 'source': 'rest', 'requires_explicit_approval': True}]
        self.build(profile)
        hatch.repair(self.root, self.data, 'jump', 'Fixture symmetric silhouette permits mirror.')
        with self.assertRaisesRegex(ValueError, 'explicit mirror approval'):
            hatch.derive(self.root, self.data, 'jump', 'No approval', False)
        hatch.derive(self.root, self.data, 'jump', 'Fixture silhouette verified for mirror test.', True)
        self.assertNotIn('generation_receipt', hatch.job_for(self.data, 'jump'))
        self.assertIn('derivation_receipt', hatch.job_for(self.data, 'jump'))
        hatch.repair(self.root, self.data, 'jump', 'Regenerate fixture row.')
        ingest_fixture(self.root, self.data, 'jump', self.base / 'fixture-sources/jump.png')
        self.assertNotIn('derivation_receipt', hatch.job_for(self.data, 'jump'))
        self.assertIn('generation_receipt', hatch.job_for(self.data, 'jump'))

    def test_base_review_is_required_and_recalibration_detects_collisions(self):
        self.build()
        hatch.configure(self.root, self.data, argparse.Namespace(background='chroma', holes=None,
                        shared_scale=None, key_threshold=None, chroma_key='#B43C28'))
        with self.assertRaisesRegex(ValueError, 'base-review'):
            hatch.claim(self.root, self.data, 'rest')
        self.assertGreater(hatch.read(self.root / 'extraction-settings.json')['key_collision_pixels'], 0)
        with self.assertRaisesRegex(ValueError, 'collides'):
            hatch.record_base_review(self.root, self.data, 'Cannot safely accept this key.')
        hatch.configure(self.root, self.data, argparse.Namespace(background='alpha', holes=None,
                        shared_scale=None, key_threshold=None, chroma_key='#0000FF'))
        hatch.record_base_review(self.root, self.data, 'Fixture alpha retains the body colors.')
        self.assertEqual(hatch.claim(self.root, self.data, 'rest')['job'], 'rest')

    def test_missing_generation_job_cannot_accept_old_decoded_sources(self):
        self.build()
        self.data['jobs'] = self.data['jobs'][:-1]
        hatch.save(self.root, self.data)
        with self.assertRaisesRegex(ValueError, 'exactly one job'):
            hatch.manifest(self.root)

    def test_ingest_preserves_original_and_rejects_managed_output_as_source(self):
        data = build_run(self.root, validate=False)
        hatch.repair(self.root, data, 'rest', 'Fixture regeneration test.')
        handoff = hatch.claim(self.root, data, 'rest')
        receipt = self.base.resolve() / 'receipt.json'
        hatch.atomic(receipt, handoff)
        decoded = self.root / 'decoded/rest.png'
        before = decoded.read_bytes()
        # macOS /var aliases and Windows short names must not bypass containment.
        alias = self.base / 'run-alias'
        alias.symlink_to(self.root.resolve(), target_is_directory=True)
        for run in (self.root, alias):
            with self.subTest(root=run), self.assertRaisesRegex(ValueError, 'outside managed outputs'):
                hatch.ingest(run, data, 'rest', decoded, handoff['token'], 'Fixture', receipt, True)
        self.assertEqual(decoded.read_bytes(), before)
        source = self.base / 'fixture-sources/rest.png'
        original_hash = hatch.digest(source)
        hatch.ingest(self.root, data, 'rest', source, handoff['token'], 'Fixture', receipt, True)
        self.assertEqual(hatch.digest(source), original_hash)
        self.assertEqual(hatch.job_for(data, 'rest')['source_hash'], original_hash)


if __name__ == '__main__':
    unittest.main()
