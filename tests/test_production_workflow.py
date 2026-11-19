"""Workflow regressions use explicit synthetic fixtures; no art approvals or provider calls."""
from __future__ import annotations
from contextlib import contextmanager
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import hatch
import production_workflow as workflow
from production_contracts import load_project
from production_fixture import build_fixture_project, refresh_project_hashes
from production_render import render_shot, render_scene
from fixture_workflow import small_profile


class ProductionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hatch-production-workflow-')
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / 'project'
        build_fixture_project(self.root)
        project = hatch.read(self.root / 'project.json')
        project['limits']['max_generation_calls'] = 4  # Explicit synthetic controller claims in these tests only.
        hatch.atomic(self.root / 'project.json', project)
        self.character_target = {'kind': 'character', 'character_id': 'a', 'version': '1.0.0'}
        self.shot_target = {'kind': 'shot', 'scene_id': 'first-scene', 'shot_id': 'exchange'}

    def bundle(self, diagnostic=False):
        return load_project(self.root / 'project.json', verify_hashes=not diagnostic,
                            allow_missing_assets=diagnostic)

    def edit(self, path, callback, refresh=True):
        value = hatch.read(self.root / path)
        callback(value)
        hatch.atomic(self.root / path, value)
        if refresh:
            refresh_project_hashes(self.root)

    def edit_cast(self, callback):
        self.edit(hatch.read(self.root / 'project.json')['cast']['path'], callback)

    def edit_scene(self, scene_id, callback, refresh=True):
        bundle = self.bundle()
        self.edit(bundle['paths']['scenes'][scene_id], callback, refresh=refresh)

    def scene_shot(self, scene):
        return scene['shots'][0]

    def receipt(self, target=None, dimensions=None, render_manifest=None):
        receipt = workflow.review_template(self.root, target or self.character_target,
                                           dimensions=dimensions, render_manifest=render_manifest)
        receipt['reviewer'] = {'kind': 'synthetic', 'id': 'workflow-regression',
                               'name': 'Synthetic workflow gate only'}
        evidence = 'art/a-body.png'
        if render_manifest:
            manifest = hatch.read(self.root / render_manifest)
            evidence = (Path(render_manifest).parent / manifest['frames'][0]['path']).as_posix()
        for dimension in receipt['dimensions'].values():
            dimension.update(verdict='pass', notes='Synthetic exact-byte gate; no production art acceptance.',
                evidence=[{'path': evidence, 'sha256': workflow.file_hash(self.root / evidence),
                           'method': 'automated_check', 'note': 'Synthetic fixture artifact only.'}])
        return receipt

    def approve_fixture_assets(self):
        for key in self.bundle()['characters']:
            character, version = key.rsplit('@', 1)
            workflow.record_review(self.root, self.receipt({'kind': 'character', 'character_id': character, 'version': version}))
        for key in self.bundle()['clips']:
            clip, version = key.rsplit('@', 1)
            workflow.record_review(self.root, self.receipt({'kind': 'clip', 'clip_id': clip, 'version': version}))

    def acquisition(self, binding='a'):
        run = self.root / 'acquisition' / binding
        run.parent.mkdir(parents=True, exist_ok=True)
        profile = self.home / ('profile-' + binding + '.json')
        hatch.atomic(profile, small_profile())
        args = argparse.Namespace(replace_inputs=False, fixture_mode=True, profile=str(profile),
            pet_name='Workflow fixture', pet_notes='Explicit synthetic acquisition for controller tests only.',
            reference=[str(self.root / 'art/a-body.png')], avoid=['glitter'], require=['preserve fixture proportions'],
            brand_source=[], style_preset='clay', style_notes='Synthetic controller gate.')
        hatch.prepare(run, args)
        requirements = [{'character': 'a@1.0.0', 'dimension': 'viewpoint', 'value': 'front'},
                        {'character': 'a@1.0.0', 'dimension': 'hand', 'value': 'grasp'}]
        workflow.bind_generation_run(self.root, binding, run, requirements)
        return run, requirements

    def generated_base(self):
        run, requirements = self.acquisition()
        handoff = workflow.claim_generation(self.root, 'a', 'base')
        source = self.home / 'generated-fixture.png'
        shutil.copyfile(self.root / 'art/a-body.png', source)
        workflow.ingest_generation(self.root, 'a', 'base', source, handoff['token'],
                                   'Synthetic controller fixture; no generated art claim.', handoff['receipt_template'])
        return run, handoff

    def import_spec(self):
        source = self.home / 'replacement.png'
        shutil.copyfile(self.root / 'art/a-body.png', source)
        return {'schema_version': 1, 'kind': 'component-import', 'source': str(source),
                'fixture': True, 'origin': 'Synthetic test source, not production artwork',
                'viewpoint': 'front', 'costume': 'default', 'drawing_id': 'default',
                'note': 'Exercise immutable component import without claiming artwork acceptance.'}

    def run_cli(self, *arguments, success=True):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/production.py'), *map(str, arguments)],
                                capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        return result

    def test_coverage_requires_current_asset_and_clip_reviews(self):
        before = workflow.coverage_report(self.root)
        self.assertFalse(before['ready'])
        self.assertTrue(before['unreviewed'])
        self.assertGreater(len(before['requirements']), 10)
        self.approve_fixture_assets()
        after = workflow.coverage_report(self.root)
        self.assertTrue(after['ready'], after)
        self.assertEqual(after['expected_generation']['raster_asset_requests'], 0)
        self.assertFalse(workflow.review_status(self.root, self.character_target)['production_art_acceptance'])

    def test_missing_coverage_deduplicates_across_scenes_and_bounds_retries(self):
        for scene_id in ('first-scene', 'second-scene'):
            self.edit_scene(scene_id, lambda scene: self.scene_shot(scene)['instances'][0]['requirements'].update(mouth=['new-viseme']))
        report = workflow.coverage_report(self.root)
        self.assertGreaterEqual(len([item for item in report['missing'] if item.get('value') == 'new-viseme']), 2)
        self.assertEqual(report['expected_generation']['raster_asset_requests'], 1)
        self.assertEqual(report['expected_generation']['bounded_attempts'], 3)

    def test_coverage_reports_when_missing_work_exceeds_project_call_bound(self):
        self.edit_scene('first-scene', lambda scene: self.scene_shot(scene)['instances'][0]['requirements'].update(
            mouth=['new-viseme-' + str(index) for index in range(5)]))
        expected = workflow.coverage_report(self.root)['expected_generation']
        self.assertEqual(expected['raster_asset_requests'], 5)
        self.assertFalse(expected['within_limit'])
        self.assertEqual(expected['bounded_attempts'], 4)

    def test_combination_restrictions_report_the_affected_instance(self):
        self.edit_cast(lambda cast: cast['characters'][0]['restrictions'].append(
            {'when': {'viewpoint': ['front'], 'costume': ['default']}, 'reason': 'Synthetic combination gate.'}))
        report = workflow.coverage_report(self.root)
        self.assertFalse(report['ready'])
        self.assertTrue(any(item['reason'] == 'Synthetic combination gate.' for item in report['incompatible']))

    def test_invalid_transition_contract_returns_actionable_coverage_failure(self):
        self.edit_scene('first-scene', lambda scene: self.scene_shot(scene)['instances'][0]['clips'][0].pop('transition'))
        report = workflow.coverage_report(self.root)
        self.assertFalse(report['validated'])
        self.assertTrue(report['incompatible'][0]['required_action'])
        self.assertIsNone(report['expected_generation'])

    def test_camera_change_invalidates_only_its_shot_and_review_dimensions(self):
        workflow.record_review(self.root, self.receipt(self.shot_target, ['identity_construction', 'camera_composition']))
        snapshot = workflow.dependency_snapshot(self.root)
        self.edit_scene('first-scene', lambda scene: self.scene_shot(scene)['camera']['zoom']['keys'][0].update(value=1.2))
        report = workflow.impact_report(self.root, snapshot)
        self.assertEqual([item['shot'] for item in report['affected_shots']], ['first-scene/exchange'])
        self.assertEqual(report['affected_shots'][0]['changed_dependencies'], ['camera'])
        status = workflow.review_status(self.root, self.shot_target)['dimensions']
        self.assertEqual(status['identity_construction']['status'], 'pass')
        self.assertEqual(status['camera_composition']['status'], 'stale')

    def test_unused_drawing_and_character_additions_preserve_existing_shot_hashes(self):
        snapshot = workflow.dependency_snapshot(self.root)
        def add(cast):
            character = cast['characters'][0]
            body = next(item for item in character['components'] if item['id'] == 'body')
            body['drawings']['unused'] = copy.deepcopy(body['drawings']['default'])
            other = copy.deepcopy(character)
            other['id'] = 'unused-character'
            cast['characters'].append(other)
        self.edit_cast(add)
        self.assertEqual(workflow.impact_report(self.root, snapshot)['affected_shots'], [])

    def test_changed_image_bytes_stale_review_and_reject_stale_submission(self):
        receipt = self.receipt()
        workflow.record_review(self.root, receipt)
        source = self.root / 'art/a-body.png'
        with Image.open(source) as opened:
            image = opened.convert('RGBA')
        image.putpixel((0, 0), (12, 34, 56, 100))
        image.save(source)
        status = workflow.review_status(self.root, self.character_target)
        self.assertEqual(status['dimensions']['identity_construction']['status'], 'stale')
        with self.assertRaises(ValueError):
            workflow.record_review(self.root, receipt)

    def test_review_history_is_append_only_and_detects_receipt_tampering(self):
        workflow.record_review(self.root, self.receipt())
        failed = self.receipt()
        failed['dimensions']['identity_construction']['verdict'] = 'fail'
        workflow.record_review(self.root, failed)
        status = workflow.review_status(self.root, self.character_target)
        self.assertEqual(status['history_count'], 2)
        self.assertFalse(status['current_pass'])
        path = self.root / workflow.WORKFLOW_FILE
        ledger = hatch.read(path)
        ledger['reviews'][0]['receipt']['dimensions']['identity_construction']['notes'] = 'Rewritten history'
        hatch.atomic(path, ledger)
        with self.assertRaisesRegex(ValueError, 'history was modified'):
            workflow.review_status(self.root, self.character_target)

    def test_human_or_assistant_identity_cannot_promote_automated_checks(self):
        for kind in ('human', 'assistant'):
            receipt = self.receipt()
            receipt['reviewer']['kind'] = kind
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'automated metrics alone'):
                workflow.record_review(self.root, receipt)
        result = workflow.record_review(self.root, self.receipt())
        self.assertTrue(result['synthetic_gate_only'])
        self.assertFalse(result['human_review'])
        self.assertFalse(result['production_art_acceptance'])

    def test_wrong_evidence_hash_and_old_dependency_receipts_are_rejected(self):
        receipt = self.receipt()
        bad = copy.deepcopy(receipt)
        bad['dimensions']['identity_construction']['evidence'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'current inspected file'):
            workflow.record_review(self.root, bad)
        self.edit_cast(lambda cast: cast['characters'][0]['identity']['markings'].append('explicit new marker'))
        with self.assertRaisesRegex(ValueError, 'stale review dependencies'):
            workflow.record_review(self.root, receipt)

    def test_component_review_tracks_ancestor_construction_but_not_siblings(self):
        target = {'kind': 'component', 'character_id': 'a', 'version': '1.0.0', 'component_id': 'mouth'}
        workflow.record_review(self.root, self.receipt(target))
        self.edit_cast(lambda cast: next(item for item in cast['characters'][0]['components'] if item['id'] == 'hand').update(z=99))
        self.assertTrue(workflow.review_status(self.root, target)['current_pass'])
        self.edit_cast(lambda cast: next(item for item in cast['characters'][0]['components'] if item['id'] == 'head').update(z=98))
        status = workflow.review_status(self.root, target)
        self.assertFalse(status['current_pass'])
        self.assertTrue(any(key.startswith('attachment:') for key in status['dimensions']['identity_construction']['changed_dependencies']))

    def test_new_declared_coverage_needs_review_without_invalidating_unused_shots(self):
        workflow.record_review(self.root, self.receipt())
        snapshot = workflow.dependency_snapshot(self.root)
        self.edit_cast(lambda cast: cast['characters'][0]['capabilities']['mouth'].append('additional-viseme'))
        self.assertEqual(workflow.impact_report(self.root, snapshot)['affected_shots'], [])
        self.assertFalse(workflow.review_status(self.root, self.character_target)['current_pass'])

    def test_final_artifact_review_binds_exact_rendered_bytes(self):
        render_shot(self.bundle(), 'exchange', self.root / 'renders/exchange', track_layers=False)
        relative = 'renders/exchange/render-manifest.json'
        workflow.record_review(self.root, self.receipt(self.shot_target, ['final_artifacts'], relative))
        self.assertEqual(workflow.review_status(self.root, self.shot_target)['dimensions']['final_artifacts']['status'], 'pass')
        path = self.root / 'renders/exchange/frames/000000.png'
        with Image.open(path) as image:
            changed = image.convert('RGBA')
        changed.putpixel((0, 0), (255, 0, 0, 255))
        changed.save(path)
        self.assertEqual(workflow.review_status(self.root, self.shot_target)['dimensions']['final_artifacts']['status'], 'stale')

    def test_old_render_cannot_support_review_after_camera_change(self):
        render_shot(self.bundle(), 'exchange', self.root / 'renders/exchange', track_layers=False)
        self.edit_scene('first-scene', lambda scene: self.scene_shot(scene)['camera']['zoom']['keys'][0].update(value=1.1))
        with self.assertRaisesRegex(ValueError, 'dependencies are stale'):
            workflow.review_template(self.root, self.shot_target, ['final_artifacts'],
                                     'renders/exchange/render-manifest.json')

    def test_final_review_rejects_wrong_identity_timing_and_audio_count(self):
        render_shot(self.bundle(), 'exchange', self.root / 'renders/exchange', track_layers=False)
        relative = 'renders/exchange/render-manifest.json'
        original = hatch.read(self.root / relative)
        mutations = [('project_id', lambda value: value.update(project_id='wrong-project')),
                     ('fixture', lambda value: value.update(fixture=False)),
                     ('fps', lambda value: value['fps'].update(numerator=99)),
                     ('audio', lambda value: value['audio'].update(sample_count=value['audio']['sample_count'] + 1))]
        for name, mutation in mutations:
            changed = copy.deepcopy(original)
            mutation(changed)
            hatch.atomic(self.root / relative, changed)
            with self.subTest(name=name), self.assertRaises(ValueError):
                workflow.review_template(self.root, self.shot_target, ['final_artifacts'], relative)
        hatch.atomic(self.root / relative, original)

    def test_import_creates_new_version_with_original_source_and_no_old_shot_invalidation(self):
        snapshot = workflow.dependency_snapshot(self.root)
        result = workflow.import_component(self.root, 'a', '1.0.0', 'body', self.import_spec(), '1.0.1')
        self.assertEqual(result['character'], 'a@1.0.1')
        bundle = self.bundle()
        self.assertIn('a@1.0.0', bundle['characters'])
        self.assertIn('a@1.0.1', bundle['characters'])
        provenance = result['image']['provenance']
        self.assertEqual(provenance['kind'], 'import')
        self.assertEqual(workflow.file_hash(self.root / provenance['source']['path']), provenance['source_sha256'])
        self.assertTrue(all(review['status'] == 'pending' for review in bundle['characters']['a@1.0.1']['view_reviews'].values()))
        self.assertEqual(workflow.impact_report(self.root, snapshot)['affected_shots'], [])

    def test_import_rejects_fixture_relabeling_undeclared_view_and_implicit_resize(self):
        for name, mutation in [('fixture', lambda spec: spec.update(fixture=False)),
                               ('view', lambda spec: spec.update(viewpoint='back')),
                               ('size', lambda spec: spec.update(crop=[0, 0, 10, 10]))]:
            spec = self.import_spec()
            mutation(spec)
            before = workflow.file_hash(self.root / 'project.json')
            with self.subTest(name=name), self.assertRaises(ValueError):
                workflow.import_component(self.root, 'a', '1.0.0', 'body', spec, '1.0.1')
            self.assertEqual(workflow.file_hash(self.root / 'project.json'), before)

    def test_claim_retry_preserves_context_original_inputs_and_exact_token(self):
        run, requirements = self.acquisition()
        first = workflow.claim_generation(self.root, 'a', 'base')
        with self.assertRaises(ValueError):
            workflow.claim_generation(self.root, 'a', 'base')
        workflow.fail_generation(self.root, 'a', 'base', first['token'], 'Worker outcome uncertain; preserve its attempt.')
        second = workflow.claim_generation(self.root, 'a', 'base')
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(second['production_context']['requirements'], requirements)
        self.assertEqual(first['input_images'], second['input_images'])
        self.assertTrue(second['previous_failures'])
        prompt = Path(second['prompt']).read_text('utf-8')
        self.assertEqual(prompt.count('Production coverage binding SHA-256:'), 1)
        self.assertEqual(workflow.generation_status(self.root)['active_claims'][0]['token'], second['token'])

    def test_project_call_budget_counts_failed_claims_after_explicit_repair(self):
        self.acquisition()
        workflow.configure_generation_limits(self.root, max_calls=1)
        handoff = workflow.claim_generation(self.root, 'a', 'base')
        workflow.fail_generation(self.root, 'a', 'base', handoff['token'], 'Synthetic failure')
        workflow.repair_generation(self.root, 'a', 'base', 'Specific fixture retry repair')
        status = workflow.generation_status(self.root)
        self.assertEqual(status['calls_used'], 1)
        self.assertEqual(status['remaining_calls'], 0)
        with self.assertRaisesRegex(ValueError, 'call limit exhausted'):
            workflow.claim_generation(self.root, 'a', 'base')

    def test_ingest_uses_native_receipt_contract_and_preserves_unknown_model_metadata(self):
        run, _ = self.acquisition()
        handoff = workflow.claim_generation(self.root, 'a', 'base')
        source = self.home / 'synthetic-source.png'
        shutil.copyfile(self.root / 'art/a-body.png', source)
        bad = copy.deepcopy(handoff['receipt_template'])
        bad['production_context']['requirements'] = ['changed']
        with self.assertRaisesRegex(ValueError, 'exact bound production coverage'):
            workflow.ingest_generation(self.root, 'a', 'base', source, handoff['token'], 'Synthetic', bad)
        workflow.ingest_generation(self.root, 'a', 'base', source, handoff['token'], 'Synthetic gate only.', handoff['receipt_template'])
        item = hatch.job_for(hatch.manifest(run), 'base')
        self.assertEqual(item['status'], 'generated')
        self.assertIsNone(item['model_call']['id'])
        self.assertIsNone(item['model_call']['model'])
        self.assertTrue(item['model_call']['unavailable_reason'])
        self.assertEqual(item['generation_receipt']['production_context'], handoff['production_context'])

    def test_generated_component_adoption_preserves_complete_acquisition_history(self):
        run, handoff = self.generated_base()
        spec = {key: value for key, value in self.import_spec().items() if key not in ('source', 'origin')}
        result = workflow.adopt_generated_component(self.root, 'a', 'base', 'a', '1.0.0', 'body', spec, '1.0.1')
        provenance = result['image']['provenance']
        self.assertEqual(provenance['kind'], 'fixture')
        snapshot = hatch.read(self.root / provenance['acquisition']['receipt_path'])
        self.assertEqual(snapshot['job']['history'], hatch.job_for(hatch.manifest(run), 'base')['history'])
        self.assertEqual(snapshot['job']['generation_receipt']['production_context'], handoff['production_context'])
        self.assertTrue(result['review_required'])
        self.assertFalse(result['provider_called'])

    def test_ingest_canonicalizes_only_its_owned_temporary_receipt_directory(self):
        run, _ = self.acquisition()
        handoff = workflow.claim_generation(self.root, 'a', 'base')
        source = self.home / 'synthetic-alias-source.png'
        shutil.copyfile(self.root / 'art/a-body.png', source)
        original_temporary = tempfile.TemporaryDirectory
        observed = []

        @contextmanager
        def aliased_receipt_directory(*args, **kwargs):
            with original_temporary(*args, **kwargs) as directory:
                if kwargs.get('prefix') != 'hatch-production-receipt-':
                    yield directory
                    return
                alias = self.home / 'owned-receipt-alias'
                alias.symlink_to(Path(directory).resolve(), target_is_directory=True)
                observed.append(alias)
                try:
                    yield str(alias)
                finally:
                    alias.unlink()

        with patch.object(workflow.tempfile, 'TemporaryDirectory', side_effect=aliased_receipt_directory):
            result = workflow.ingest_generation(self.root, 'a', 'base', source, handoff['token'],
                                                'Synthetic alias regression.', handoff['receipt_template'])
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertFalse(result['provider_called'])
        item = hatch.job_for(hatch.manifest(run), 'base')
        self.assertEqual(item['status'], 'generated')
        self.assertEqual(item['generation_receipt']['production_context'], handoff['production_context'])

    def test_missing_used_source_reports_recovery_work_and_stale_review(self):
        workflow.record_review(self.root, self.receipt())
        (self.root / 'art/a-body.png').unlink()
        report = workflow.coverage_report(self.root)
        self.assertFalse(report['validated'])
        self.assertEqual(report['expected_generation']['source_recovery_items'], 1)
        self.assertEqual(report['expected_generation']['raster_asset_requests'], 0)
        self.assertFalse(workflow.review_status(self.root, self.character_target)['current_pass'])

    def test_missing_unused_drawing_does_not_invalidate_shot_dependencies(self):
        def add(cast):
            body = next(item for item in cast['characters'][0]['components'] if item['id'] == 'body')
            drawing = copy.deepcopy(body['drawings']['default'])
            drawing['image']['path'] = 'art/unused.png'
            body['drawings']['unused'] = drawing
        shutil.copyfile(self.root / 'art/a-body.png', self.root / 'art/unused.png')
        self.edit_cast(add)
        snapshot = workflow.dependency_snapshot(self.root)
        (self.root / 'art/unused.png').unlink()
        self.assertEqual(workflow.impact_report(self.root, snapshot)['affected_shots'], [])
        with self.assertRaises(ValueError):
            self.bundle()

    def test_stale_manifest_hash_cannot_be_reported_ready(self):
        self.approve_fixture_assets()
        self.edit_scene('first-scene', lambda scene: scene.update(version='1.0.1'), refresh=False)
        report = workflow.coverage_report(self.root)
        self.assertFalse(report['validated'])
        self.assertFalse(report['ready'])
        self.assertTrue(report['stale_sources'])

    def test_archive_roundtrip_preserves_identity_review_and_dependency_hashes(self):
        self.approve_fixture_assets()
        before = workflow.dependency_snapshot(self.root)
        archive = self.home / 'portable.zip'
        workflow.archive_project(self.root, archive)
        destination = self.home / 'another-directory' / 'restored'
        result = workflow.restore_project(archive, destination)
        self.assertEqual(result['project_id'], 'fixture-project')
        self.assertEqual(workflow.dependency_snapshot(destination), before)
        self.assertTrue(workflow.coverage_report(destination)['ready'])
        self.assertTrue(workflow.review_status(destination, self.character_target)['current_pass'])
        self.assertFalse(result['external_integration_verified'])

    def test_archive_preserves_acquisition_records_without_reading_runtime_locks(self):
        run, _ = self.acquisition()
        lock = run.parent / ('.' + run.name + '.hatch.lock')
        unrelated = self.root / 'notes.hatch.lock'
        unrelated.write_bytes(b'Ordinary project evidence; not a runtime lock.')
        original_hash = workflow.file_hash
        def mandatory_lock(path):
            if Path(path) == lock:
                raise PermissionError('Simulated Windows mandatory lock byte')
            return original_hash(path)
        archive = self.home / 'acquisition.zip'
        with patch.object(workflow, 'file_hash', side_effect=mandatory_lock):
            workflow.archive_project(self.root, archive)
        receipt = workflow.verify_project_archive(archive)
        self.assertNotIn(lock.relative_to(self.root).as_posix(), receipt['files'])
        self.assertIn(unrelated.name, receipt['files'])
        for path in run.rglob('*'):
            if path.is_file():
                self.assertEqual(receipt['files'][path.relative_to(self.root).as_posix()], original_hash(path))
        workflow.restore_project(archive, self.home / 'acquisition-restored')

    def test_archive_and_restore_preserve_destinations_created_during_publish(self):
        import production_sources as sources
        archive = self.home / 'original.zip'
        workflow.archive_project(self.root, archive)
        original_rename = sources._rename_no_replace
        for action in ('archive', 'restore'):
            with self.subTest(action=action):
                destination = self.home / ('racing-' + action)
                def race(stage, target):
                    if action == 'archive':
                        target.write_bytes(b'Independent archive')
                    else:
                        target.mkdir()
                    return original_rename(stage, target)
                with patch.object(sources, '_rename_no_replace', side_effect=race):
                    with self.assertRaises(FileExistsError):
                        if action == 'archive':
                            workflow.archive_project(self.root, destination)
                        else:
                            workflow.restore_project(archive, destination)
                if action == 'archive':
                    self.assertEqual(destination.read_bytes(), b'Independent archive')
                else:
                    self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(list(self.home.glob('*.incoming')))
        self.assertFalse(list(self.home.glob('.hatch-production-restore-*')))

    def test_archive_rejects_active_bound_and_unbound_tokens(self):
        run, _ = self.acquisition()
        handoff = workflow.claim_generation(self.root, 'a', 'base')
        with self.assertRaisesRegex(ValueError, 'active acquisition token'):
            workflow.archive_project(self.root, self.home / 'active.zip')
        ledger = hatch.read(self.root / workflow.WORKFLOW_FILE)
        ledger['generation_bindings'] = {}
        hatch.atomic(self.root / workflow.WORKFLOW_FILE, ledger)
        with self.assertRaisesRegex(ValueError, 'active acquisition token'):
            workflow.archive_project(self.root, self.home / 'unbound-active.zip')
        self.assertEqual(hatch.job_for(hatch.manifest(run), 'base')['token'], handoff['token'])

    def test_archive_rejects_tampered_inventory_and_member_size_excess(self):
        archive = self.home / 'portable.zip'
        workflow.archive_project(self.root, archive)
        tampered = self.home / 'tampered.zip'
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, 'w') as output:
            for name in source.namelist():
                data = source.read(name)
                output.writestr(name, data + b' ' if name == 'project.json' else data)
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            workflow.restore_project(tampered, self.home / 'bad')
        self.assertFalse((self.home / 'bad').exists())
        with patch.object(workflow, 'MAX_ARCHIVE_MEMBER_BYTES', 1), self.assertRaisesRegex(ValueError, 'bounded size'):
            workflow.verify_project_archive(archive)

    def test_explicit_migration_backs_up_exact_old_ledger_without_promoting_reviews(self):
        old = {'schema_version': 1, 'project_id': 'fixture-project', 'fixture': True,
               'generation_bindings': {}, 'reviews': [{'old_visual_approval': 'historical only'}], 'history': []}
        path = self.root / workflow.WORKFLOW_FILE
        raw = json.dumps(old, separators=(',', ':')).encode()
        path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'migrate explicitly'):
            workflow.review_status(self.root, self.character_target)
        result = workflow.migrate_project(self.root)
        self.assertEqual(Path(result['backup']).read_bytes(), raw)
        current = hatch.read(path)
        self.assertEqual(current['reviews'], [])
        self.assertEqual(current['history'][-1]['previous_reviews'], old['reviews'])
        self.assertFalse(workflow.review_status(self.root, self.character_target)['current_pass'])

    def test_archive_rejects_symlinks_and_destinations_inside_project(self):
        outside = self.home / 'outside.txt'
        outside.write_text('Do not include through a link', encoding='utf-8')
        (self.root / 'linked.txt').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'outside|symlink'):
            workflow.archive_project(self.root, self.home / 'linked.zip')
        (self.root / 'linked.txt').unlink()
        with self.assertRaisesRegex(ValueError, 'outside the project'):
            workflow.archive_project(self.root, self.root / 'recursive.zip')

    def test_cli_creates_valid_fixture_and_resumes_partial_render(self):
        destination = self.home / 'cli-fixture'
        created = self.run_cli('create', '--destination', destination, '--fixture', '--id', 'cli-fixture')
        self.assertTrue(created['fixture'])
        self.assertTrue(self.run_cli('validate', '--project', destination)['valid'])
        rendered = destination / 'renders/exchange'
        partial = self.run_cli('render', '--project', destination, '--shot', 'exchange',
                               '--destination', rendered, '--max-frames', 2, '--no-layers')
        self.assertEqual(partial['status'], 'partial')
        finished = self.run_cli('render', '--project', destination, '--shot', 'exchange',
                                '--destination', rendered, '--no-layers')
        self.assertEqual(finished['status'], 'complete')

    def test_cli_invalid_arguments_and_output_collision_do_not_claim(self):
        self.run_cli('render', '--project', self.root, success=False)
        self.acquisition()
        output = self.home / 'existing-receipt.json'
        output.write_text('preserve me', encoding='utf-8')
        self.run_cli('claim', '--project', self.root, '--binding', 'a', '--job', 'base',
                     '--output', output, success=False)
        self.assertEqual(output.read_text('utf-8'), 'preserve me')
        self.assertEqual(workflow.generation_status(self.root)['calls_used'], 0)

    def test_cli_rejects_ambiguous_review_target_flags(self):
        self.run_cli('review-template', '--project', self.root, '--character', 'a', '--version', '1.0.0',
                     '--scene', 'first-scene', '--shot', 'exchange', success=False)
        target = self.home / 'target.json'
        hatch.atomic(target, self.character_target)
        self.run_cli('reviews', '--project', self.root, '--target', target, '--character', 'a', success=False)

    def test_cli_portable_clone_preserves_identity_and_output_report_semantics(self):
        destination = self.home / 'clone'
        output = self.home / 'created.json'
        stdout = self.run_cli('create', '--from-project', self.root, '--destination', destination, '--output', output)
        self.assertEqual(stdout, {'output': str(output.resolve())})
        report = hatch.read(output)
        self.assertTrue(report['identity_preserved'])
        self.assertEqual(workflow.dependency_snapshot(destination), workflow.dependency_snapshot(self.root))
        self.run_cli('create', '--from-project', self.root, '--destination', destination, success=False)

    def test_restore_reports_stale_cached_shots_and_scenes_even_with_same_renderer(self):
        render_scene(self.bundle(), 'first-scene', self.root / 'renders/first-scene', track_layers=False)
        render_shot(self.bundle(), 'reuse', self.root / 'renders/reuse', track_layers=False)
        fresh = self.home / 'fresh.zip'
        workflow.archive_project(self.root, fresh)
        restored = workflow.restore_project(fresh, self.home / 'fresh-restored')
        self.assertFalse(restored['renderer_changed'])
        self.assertFalse(restored['rerender_required'], restored['render_freshness'])
        self.assertEqual(restored['render_freshness']['checked_renders'], 3)
        self.edit_scene('first-scene', lambda scene: self.scene_shot(scene)['camera']['zoom']['keys'][0].update(value=1.2))
        stale = self.home / 'stale.zip'
        workflow.archive_project(self.root, stale)
        restored = workflow.restore_project(stale, self.home / 'stale-restored')
        self.assertFalse(restored['renderer_changed'])
        self.assertTrue(restored['rerender_required'])
        affected = restored['render_freshness']['affected_renders']
        self.assertEqual(len(affected), 2, affected)
        self.assertTrue(any('camera' in key for item in affected for key in item['changed_dependencies']))
        self.assertEqual(restored['render_freshness']['reusable_renders'], ['renders/reuse/render-manifest.json'])
        render_scene(self.bundle(), 'first-scene', self.root / 'renders/first-scene', track_layers=False)
        repaired = self.home / 'repaired.zip'
        workflow.archive_project(self.root, repaired)
        repaired_result = workflow.restore_project(repaired, self.home / 'repaired-restored')
        self.assertFalse(repaired_result['rerender_required'], repaired_result['render_freshness'])


if __name__ == '__main__':
    unittest.main()
