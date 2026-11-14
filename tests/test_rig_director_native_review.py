"""Native Director journal invariants; actual files, decoder and owner lock."""
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

import hatch
import production_workflow as workflow
from rig_errors import RigError


ROOT = Path(__file__).resolve().parents[1]


def reference(root, name, *, kind='evidence', artifact_id='evidence', media_type='application/json'):
    payload = (root/name).read_bytes()
    return dict(artifact_id=artifact_id, kind=kind, schema_version=1, semantic_version='1.0.0',
                path=name, sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload),
                media_type=media_type, provenance_ref=None, fixture=True, dependencies=[])


def append_in_process(root, project, image, identifier):
    root = Path(root)
    journal = workflow.NativeReviewJournal(root, project)
    profile = reference(root, 'profile.json', kind='director-qa-profile', artifact_id='profile')
    report = dict(schema_version=1, kind='director-qa-report', project_id=journal.project_id,
                  fixture=True, project=project, profile=profile, status='passed',
                  inputs=[project, profile, image], checks=[dict(id='exact-bytes', artifact=image,
                      evidence=[image], status='passed')])
    deadline = time.monotonic() + 15
    while True:
        try:
            return journal.append_validation(report, identifier)['sha256']
        except ValueError as exc:
            if str(exc) != 'run is owned by another controller' or time.monotonic() >= deadline:
                raise
            time.sleep(.01)


class NativeReviewJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='native-review-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        docs = json.loads((ROOT/'examples/director-contracts/contracts.json').read_text())
        project = deepcopy(next(d for d in docs if d['kind'] == 'director-project'))
        (self.root/'director-project.json').write_text(json.dumps(project))
        self.project = reference(self.root, 'director-project.json', kind=project['kind'], artifact_id=project['id'])
        self.project['semantic_version'] = project['semantic_version']
        stream = BytesIO()
        Image.new('RGBA', (8, 8), (12, 34, 56, 78)).save(stream, format='PNG')
        (self.root/'inspected.png').write_bytes(stream.getvalue())
        self.image = reference(self.root, 'inspected.png', media_type='image/png')
        (self.root/'other.json').write_text('{"scope":"unrelated"}')
        self.other = reference(self.root, 'other.json', artifact_id='other')
        from director_schema import MILESTONES
        profile = dict(schema_version=1, kind='director-qa-profile', id='journal-fixture',
            checks=[dict(id='exact-bytes', operation='artifact.bytes', artifact=self.image, options={},
                         scene=None, shot=None, frames=None)],
            milestones={name: [] for name in MILESTONES}, review_dependencies={},
            limits=dict(max_checks=8, max_total_bytes=16*1024**2, max_seconds=60))
        (self.root/'profile.json').write_text(json.dumps(profile))
        self.profile = reference(self.root, 'profile.json', kind='director-qa-profile', artifact_id='profile')
        self.journal = workflow.NativeReviewJournal(self.root, self.project)
        self.current = dict(camera_composition=[self.image], lighting_lookdev=[self.image, self.other])

    def receipt(self, *, verdict='pass', reviewer='assistant', dimensions=None):
        return dict(schema_version=1, kind='director-native-review', project_id=self.journal.project_id,
                    fixture=True, reviewer=dict(kind=reviewer, id='reviewer-1', name='Synthetic review-policy input'),
                    dimensions={name: dict(verdict=verdict, notes='Synthetic policy-test attestation, not an executed production review.',
                        dependencies=deepcopy(self.current[name]),
                        evidence=[dict(artifact=deepcopy(self.image), method='visual_inspection',
                                       frames=dict(start=0, end=1), note='Synthetic inspection input bound to real PNG bytes.')])
                        for name in (dimensions or self.current)})

    def report(self, status='passed'):
        return dict(schema_version=1, kind='director-qa-report', project_id=self.journal.project_id,
                    fixture=True, project=self.project, profile=self.profile, status=status,
                    inputs=[self.project, self.profile, self.image],
                    checks=[dict(id='exact-bytes', artifact=self.image, evidence=[self.image], status='passed')],
                    production_art_acceptance=False)

    def test_record_reopens_exact_image_bound_chain_without_artistic_approval(self):
        row = self.journal.record(self.receipt(), 'review-1', current_dependencies=self.current)
        self.assertFalse(row['production_art_acceptance'])
        self.assertEqual(row['reviewer_identity_verification'], 'self-attested')
        reopened = workflow.NativeReviewJournal(self.root, self.project)
        status = reopened.status(self.current)
        self.assertTrue(status['current_pass'])
        self.assertFalse(status['production_art_acceptance'])
        state = reopened.history()
        self.assertEqual(state['reviews'][0]['sha256'], row['sha256'])
        self.assertEqual(state['history'][0]['receipt_sha256'], row['sha256'])
        self.assertEqual(list(self.root.glob('production-workflow*.json')), [self.root/workflow.WORKFLOW_FILE])

    def test_status_is_readonly_and_does_not_create_a_second_store(self):
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())
        status = self.journal.status(self.current)
        self.assertFalse(status['current_pass'])
        self.assertEqual({d['status'] for d in status['dimensions'].values()}, {'unreviewed'})
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())

    def test_empty_readonly_scope_never_confers_review_completion(self):
        status = self.journal.status({})
        self.assertEqual(status['dimensions'], {})
        self.assertFalse(status['current_pass'])
        self.assertFalse(status['production_art_acceptance'])
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())
        self.journal.record(self.receipt(), 'actual-scope', current_dependencies=self.current)
        self.assertTrue(self.journal.status(self.current)['current_pass'])
        self.assertFalse(self.journal.status({})['current_pass'])

    def test_changed_scope_is_stale_and_unrelated_scope_retains_current_review(self):
        self.journal.record(self.receipt(), 'review-1', current_dependencies=self.current)
        (self.root/'other.json').write_text('{"scope":"changed"}')
        current = deepcopy(self.current)
        current['lighting_lookdev'] = [self.image, reference(self.root, 'other.json', artifact_id='other')]
        status = self.journal.status(current)
        self.assertEqual(status['dimensions']['camera_composition']['status'], 'pass')
        self.assertEqual(status['dimensions']['lighting_lookdev']['status'], 'stale')
        self.assertEqual(status['history_count'], 1)

    def test_current_artifact_tamper_is_stale_and_cannot_append(self):
        self.journal.record(self.receipt(), 'review-1', current_dependencies=self.current)
        (self.root/'inspected.png').write_bytes(b'changed')
        self.assertEqual(self.journal.status(self.current)['dimensions']['camera_composition']['status'], 'stale')
        before = (self.root/workflow.WORKFLOW_FILE).read_bytes()
        with self.assertRaises(RigError):
            self.journal.record(self.receipt(), 'review-2', current_dependencies=self.current)
        self.assertEqual((self.root/workflow.WORKFLOW_FILE).read_bytes(), before)

    def test_failed_review_retained_after_new_scoped_review(self):
        failed = self.journal.record(self.receipt(verdict='fail'), 'failed', current_dependencies=self.current)
        passed = self.journal.record(self.receipt(dimensions=['camera_composition']), 'repair', current_dependencies=self.current)
        state = self.journal.history()
        self.assertEqual(len(state['reviews']), 2)
        self.assertEqual(passed['previous_sha256'], failed['sha256'])
        status = self.journal.status(self.current)
        self.assertEqual(status['dimensions']['camera_composition']['status'], 'pass')
        self.assertEqual(status['dimensions']['lighting_lookdev']['status'], 'fail')
        self.assertFalse(status['current_pass'])

    def test_synthetic_or_automated_only_cannot_grant_visual_review(self):
        for kind in ('synthetic', 'assistant', 'human'):
            receipt = self.receipt(reviewer=kind)
            if kind != 'synthetic':
                for dimension in receipt['dimensions'].values():
                    dimension['evidence'][0]['method'] = 'automated_check'
            with self.subTest(kind=kind), self.assertRaises(RigError):
                self.journal.record(receipt, 'forbidden', current_dependencies=self.current)
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())

    def test_masquerading_image_and_unbound_inspection_evidence_rejected(self):
        (self.root/'fake.png').write_text('{"not":"an image"}')
        fake = reference(self.root, 'fake.png', artifact_id='fake', media_type='image/png')
        current = dict(camera_composition=[fake])
        receipt = self.receipt(dimensions=['camera_composition'])
        receipt['dimensions']['camera_composition'].update(dependencies=[fake],
            evidence=[dict(artifact=fake, method='visual_inspection', frames=None, note='Claimed image')])
        with self.assertRaises(RigError):
            self.journal.record(receipt, 'fake', current_dependencies=current)
        receipt = self.receipt(dimensions=['camera_composition'])
        receipt['dimensions']['camera_composition']['evidence'][0]['artifact'] = self.other
        with self.assertRaises(RigError):
            self.journal.record(receipt, 'wrong-evidence', current_dependencies=self.current)

    def test_idempotent_review_and_validation_return_original_hash(self):
        receipt = self.receipt()
        first = self.journal.record(receipt, 'review-1', current_dependencies=self.current)
        before = (self.root/workflow.WORKFLOW_FILE).read_bytes()
        self.assertEqual(self.journal.record(receipt, 'review-1', current_dependencies=self.current), first)
        self.assertEqual((self.root/workflow.WORKFLOW_FILE).read_bytes(), before)
        changed = deepcopy(receipt)
        changed['dimensions']['camera_composition']['notes'] = 'Different content'
        with self.assertRaises(RigError):
            self.journal.record(changed, 'review-1', current_dependencies=self.current)
        report = self.report()
        first = self.journal.append_validation(report, 'structural-1')
        self.assertEqual(self.journal.append_validation(report, 'structural-1'), first)
        with self.assertRaises(RigError):
            self.journal.append_validation(self.report('partial'), 'structural-1')
        with self.assertRaises(RigError):
            self.journal.append_validation(report, 'review-1')

    def test_structural_validation_is_separate_and_never_sets_visual_pass(self):
        record = self.journal.append_validation(self.report(), 'structural-1')
        self.assertFalse(record['production_art_acceptance'])
        self.assertFalse(self.journal.status(self.current)['current_pass'])
        history = self.journal.validation_history()
        self.assertEqual(history['history_count'], 1)
        self.assertFalse(history['current_execution_verified'])
        self.assertEqual(self.journal.history()['reviews'], [])

    def test_structural_report_cannot_assert_missing_checks_or_art_acceptance(self):
        for change in ({'checks': []}, {'checks': [dict(id='failure', status='failed')]},
                       {'inputs': []}, {'production_art_acceptance': True}, {'schema_version': True},
                       {'canceled': True}, {'deadline_exceeded': True}, {'canceled': []}):
            with self.subTest(change=change), self.assertRaises(RigError):
                self.journal.append_validation({**self.report(), **change}, 'bad')
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())

    def test_hash_chain_or_event_history_tamper_is_rejected(self):
        self.journal.record(self.receipt(), 'review-1', current_dependencies=self.current)
        path = self.root/workflow.WORKFLOW_FILE
        original = path.read_bytes()
        for tamper in ('receipt', 'event', 'previous'):
            state = json.loads(original)
            if tamper == 'receipt':
                state['reviews'][0]['receipt']['reviewer']['name'] = 'Impostor'
            elif tamper == 'event':
                state['history'].clear()
            else:
                state['reviews'][0]['previous_sha256'] = '0'*64
            path.write_text(json.dumps(state))
            with self.subTest(tamper=tamper), self.assertRaises(ValueError):
                self.journal.history()
        path.write_bytes(original)
        self.assertEqual(self.journal.history()['reviews'][0]['receipt']['reviewer']['name'], 'Synthetic review-policy input')

    def test_foreign_legacy_store_and_project_context_are_preserved(self):
        path = self.root/workflow.WORKFLOW_FILE
        original = b'{"schema_version":2,"project_id":"legacy-2d","reviews":[]}'
        path.write_bytes(original)
        with self.assertRaises(RigError):
            self.journal.append_validation(self.report(), 'do-not-overwrite')
        self.assertEqual(path.read_bytes(), original)
        path.unlink()
        (self.root/'director-project.json').write_text('{}')
        with self.assertRaises(RigError):
            self.journal.append_validation(self.report(), 'stale-project')
        self.assertFalse(path.exists())

    def test_unknown_dimensions_and_fixture_relabeling_fail_without_writes(self):
        for change in ('dimension', 'fixture', 'reference'):
            receipt = self.receipt()
            if change == 'dimension':
                receipt['dimensions']['unknown'] = receipt['dimensions'].pop('camera_composition')
            elif change == 'fixture':
                receipt['fixture'] = False
            else:
                receipt['dimensions']['camera_composition']['dependencies'][0]['sha256'] = '0'*64
            with self.subTest(change=change), self.assertRaises(RigError):
                self.journal.record(receipt, 'invalid', current_dependencies=self.current)
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())

    def test_links_and_nonregular_journal_or_evidence_rejected(self):
        if hasattr(os, 'symlink'):
            outside = self.root/'outside.json'
            outside.write_text('untouched')
            path = self.root/workflow.WORKFLOW_FILE
            try:
                path.symlink_to(outside)
            except OSError:
                pass
            else:
                with self.assertRaises(RigError):
                    self.journal.append_validation(self.report(), 'linked-journal')
                self.assertEqual(outside.read_text(), 'untouched')
                path.unlink()
        if hasattr(os, 'mkfifo'):
            fifo = self.root/'fifo'
            os.mkfifo(fifo)
            fake = deepcopy(self.image)
            fake['path'] = 'fifo'
            report = self.report()
            report['inputs'] = [fake]
            with self.assertRaises(RigError):
                self.journal.append_validation(report, 'fifo-evidence')
            fifo.rename(self.root/workflow.WORKFLOW_FILE)
            with self.assertRaises(RigError):
                self.journal.history()

    def test_actual_concurrent_appends_share_canonical_owner_and_preserve_all_records(self):
        with ProcessPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(append_in_process, str(self.root), self.project, self.image, 'append-'+str(i))
                       for i in range(4)]
            hashes = [f.result(timeout=30) for f in futures]
        self.assertEqual(len(set(hashes)), 4)
        state = self.journal.history()
        self.assertEqual(len(state['validations']), 4)
        self.assertEqual(len(state['history']), 4)
        self.assertEqual({e['transaction_id'] for e in state['validations']}, {'append-'+str(i) for i in range(4)})

    def test_metadata_and_evidence_limits_are_checked_before_reads(self):
        receipt = self.receipt()
        receipt['dimensions']['camera_composition']['notes'] = 'x' * (workflow.MAX_REVIEW_BYTES + 1)
        with self.assertRaises(RigError):
            self.journal.record(receipt, 'oversized', current_dependencies=self.current)
        report = self.report()
        report['inputs'] = []
        for index in range(5):
            huge = deepcopy(self.image)
            huge.update(byte_size=256*1024**2, path='large-'+str(index)+'.png', artifact_id='large-'+str(index))
            report['inputs'].append(huge)
        with self.assertRaises(RigError) as error:
            self.journal.append_validation(report, 'oversized-artifacts')
        self.assertEqual(error.exception.report['code'], 'NATIVE_REVIEW_LIMIT')
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())

    def test_mutable_journal_cannot_become_its_own_input_signature(self):
        self.journal.append_validation(self.report(), 'initial')
        loop = reference(self.root, workflow.WORKFLOW_FILE, artifact_id='live-workflow')
        with self.assertRaises(RigError) as error:
            self.journal.status(dict(camera_composition=[loop]))
        self.assertEqual(error.exception.report['code'], 'NATIVE_REVIEW_SELF_REFERENCE')
        project = json.loads((self.root/'director-project.json').read_text())
        project['reviews'] = [loop]
        (self.root/'invalid-self-project.json').write_text(json.dumps(project))
        project_ref = reference(self.root, 'invalid-self-project.json', kind=project['kind'], artifact_id=project['id'])
        with self.assertRaises(RigError) as error:
            workflow.NativeReviewJournal(self.root, project_ref)
        self.assertEqual(error.exception.report['code'], 'NATIVE_REVIEW_SELF_REFERENCE')

    def test_real_native_snapshot_signature_survives_review_and_camera_revision_stales_only_camera(self):
        from editor3d_repository import EditorRepository
        from scene3d_example import build_example
        source = self.root/'native-source'
        build_example(source)
        repository = EditorRepository.create(source, self.root/'editor')
        before = repository.snapshot()
        native_name = 'revisions/00000000/native/' + before['scene_path']
        native = reference(repository.root, native_name, kind='scene3d-scene', artifact_id='example-scene')
        self.assertEqual(native['sha256'], before['inventory'][before['scene_path']]['sha256'])
        (repository.root/'inspected.png').write_bytes((self.root/'inspected.png').read_bytes())
        image = reference(repository.root, 'inspected.png', media_type='image/png')
        project = json.loads((self.root/'director-project.json').read_text())
        project['scenes'] = [native]
        (repository.root/'director-project.json').write_text(json.dumps(project))
        project_ref = reference(repository.root, 'director-project.json', kind=project['kind'], artifact_id=project['id'])
        journal = workflow.NativeReviewJournal(repository.root, project_ref)
        current = dict(camera_composition=[native, image], identity_construction=[image])
        receipt = self.receipt(dimensions=['camera_composition'])
        receipt['dimensions'] = {name: dict(verdict='pass', notes='Synthetic PNG review-policy input; this is not a native scene render.',
            dependencies=refs, evidence=[dict(artifact=image, method='visual_inspection', frames=dict(start=0, end=1),
                                               note='Actual decoded image')]) for name, refs in current.items()}
        journal.record(receipt, 'native-review', current_dependencies=current)
        self.assertEqual(repository.snapshot(), before)
        self.assertTrue(journal.status(current)['current_pass'])
        updated = repository.apply(0, dict(schema_version=1, commands=[dict(operation='transform',
            parameters=dict(identifier='camera', transform=dict(translation=[0, 3, 6], rotation=[0, 0, 0, 1], scale=[1, 1, 1])))]),
            'camera-change', expected_sha256=before['sha256'])
        new_name = 'revisions/00000001/native/' + updated['scene_path']
        changed = reference(repository.root, new_name, kind='scene3d-scene', artifact_id='example-scene')
        self.assertEqual(changed['sha256'], updated['inventory'][updated['scene_path']]['sha256'])
        project['scenes'] = [changed]
        project['semantic_version'] = '1.0.1'
        (repository.root/'director-project-1.0.1.json').write_text(json.dumps(project))
        new_project = reference(repository.root, 'director-project-1.0.1.json', kind=project['kind'], artifact_id=project['id'])
        new_project['semantic_version'] = '1.0.1'
        reopened = workflow.NativeReviewJournal(repository.root, new_project)
        current['camera_composition'] = [changed, image]
        status = reopened.status(current)
        self.assertEqual(status['dimensions']['camera_composition']['status'], 'stale')
        self.assertEqual(status['dimensions']['identity_construction']['status'], 'pass')
        self.assertEqual(status['history_count'], 1)
        original = repository.snapshot(0)
        # Undo/redo availability describes the CURRENT editor head, even when
        # snapshot(0) selects original immutable native content.
        self.assertEqual({k: v for k, v in original.items() if k not in ('undo_available', 'redo_available')},
                         {k: v for k, v in before.items() if k not in ('undo_available', 'redo_available')})

    def test_integral_json_versions_and_frame_intervals_preserve_declared_values(self):
        receipt = self.receipt()
        receipt['schema_version'] = 1.0
        for dimension in receipt['dimensions'].values():
            dimension['evidence'][0]['frames'] = dict(start=0.0, end=1.0)
        record = self.journal.record(receipt, 'integral-json', current_dependencies=self.current)
        self.assertEqual(record['receipt'], receipt)
        self.assertIs(type(record['receipt']['schema_version']), float)

    def test_wall_clock_adjustment_does_not_corrupt_authoritative_append_order(self):
        with patch.object(workflow.time, 'time', side_effect=[20.0, 10.0]):
            self.journal.append_validation(self.report(), 'clock-adjusted')
        state = self.journal.history()
        self.assertEqual(state['validations'][0]['recorded_at'], 20.0)
        self.assertEqual(state['history'][0]['time'], 10.0)
        self.assertEqual(len(state['validations']), 1)

    def test_actual_changed_png_failure_is_preserved_without_verifying_failed_bytes(self):
        from director_contracts import _read_artifact
        self.journal.append_validation(self.report(), 'before-tamper')
        (self.root/'inspected.png').write_bytes(b'actually modified PNG bytes')
        with self.assertRaises(RigError) as read_failure:
            _read_artifact(self.root, self.image)
        failed = self.report('failed')
        failed['checks'][0].update(status='failed', details=dict(code=read_failure.exception.report['code']))
        failed['findings'] = [dict(artifact=self.image, expected_sha256=self.image['sha256'],
                                   current_sha256=None, observation='Exact read failed before decode.')]
        record = self.journal.append_validation(failed, 'after-tamper')
        self.assertFalse(record['all_required_inputs_verified'])
        self.assertEqual(record['report']['findings'][0]['current_sha256'], None)
        history = self.journal.validation_history()
        self.assertEqual([r['report']['status'] for r in history['records']], ['passed', 'failed'])
        self.assertFalse(history['current_execution_verified'])
        self.assertEqual((self.root/'inspected.png').read_bytes(), b'actually modified PNG bytes')
        self.assertFalse(self.journal.status(self.current)['current_pass'])

    def test_partial_report_checks_passed_proof_but_retains_bad_expected_input(self):
        from director_contracts import _read_artifact
        profile = json.loads((self.root/'profile.json').read_text())
        profile['checks'].append(dict(id='other-bytes', operation='artifact.bytes', artifact=self.other,
                                      options={}, scene=None, shot=None, frames=None))
        (self.root/'partial-profile.json').write_text(json.dumps(profile))
        profile_ref = reference(self.root, 'partial-profile.json', kind='director-qa-profile', artifact_id='partial-profile')
        partial = self.report('partial')
        partial['profile'] = profile_ref
        partial['inputs'] = [self.project, profile_ref, self.image, self.other]
        raw = _read_artifact(self.root, self.image)
        partial['checks'][0]['details'] = dict(bytes=len(raw), source_sha256=self.image['sha256'])
        partial['checks'].append(dict(id='other-bytes', artifact=self.other, status='failed',
            evidence=[self.other], details={'native_files': [{'path': 'native-owner-descriptor.bin', 'sha256': '0'*64}]}))
        (self.root/'other.json').write_bytes(b'tampered required input')
        with self.assertRaises(RigError):
            _read_artifact(self.root, self.other)
        record = self.journal.append_validation(partial, 'partial-failure')
        self.assertFalse(record['all_required_inputs_verified'])
        self.assertEqual(record['report']['checks'][1]['status'], 'failed')
        before = (self.root/workflow.WORKFLOW_FILE).read_bytes()
        false_pass = deepcopy(partial)
        false_pass['status'] = 'passed'
        false_pass['checks'][1]['status'] = 'passed'
        with self.assertRaises(RigError):
            self.journal.append_validation(false_pass, 'false-passed-input')
        # A bad *claimed passed* check also blocks persistence of a partial report.
        (self.root/'inspected.png').write_bytes(b'bad passed proof')
        with self.assertRaises(RigError):
            self.journal.append_validation(partial, 'false-positive-in-partial')
        self.assertEqual((self.root/workflow.WORKFLOW_FILE).read_bytes(), before)

    def test_failed_missing_input_in_project_or_profile_dependencies_is_not_read(self):
        missing = deepcopy(self.image)
        missing.update(path='missing-frame.png', artifact_id='missing-frame')
        project = deepcopy(self.project)
        project['dependencies'] = [missing]
        profile = deepcopy(self.profile)
        profile['dependencies'] = [missing]
        journal = workflow.NativeReviewJournal(self.root, project)
        report = self.report('failed')
        report.update(project=project, profile=profile, inputs=[project, profile, missing])
        report['checks'] = [dict(id='missing-frame', artifact=missing, status='failed', evidence=[missing], details={})]
        journal.append_validation(report, 'missing-source')
        self.assertEqual(journal.validation_history()['records'][0]['report']['checks'][0]['artifact'], missing)
        self.assertFalse((self.root/'missing-frame.png').exists())
        false_pass = deepcopy(report)
        false_pass['status'] = 'passed'
        false_pass['checks'][0]['status'] = 'passed'
        with self.assertRaises(RigError):
            journal.append_validation(false_pass, 'missing-source-false-pass')

    def test_nonpassed_report_still_requires_actual_current_project_and_profile(self):
        failed = self.report('failed')
        failed['checks'][0]['status'] = 'failed'
        (self.root/'inspected.png').unlink()
        for bad_field in ('profile', 'project'):
            changed = deepcopy(failed)
            changed[bad_field]['sha256'] = '0'*64
            with self.subTest(field=bad_field), self.assertRaises(RigError):
                self.journal.append_validation(changed, 'invalid-'+bad_field)
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())

    def test_failed_reference_paths_are_validated_without_reading_failed_inputs(self):
        for name in ('../outside.png', '/absolute.png', 'nested/../../outside.png'):
            failed = self.report('failed')
            expected = deepcopy(self.image)
            expected['path'] = name
            failed['inputs'] = [expected]
            failed['checks'][0].update(status='failed', artifact=expected, evidence=[expected])
            with self.subTest(name=name), patch.object(self.journal, '_read_reference', side_effect=AssertionError('unexpected read')):
                with self.assertRaises(RigError):
                    self.journal.append_validation(failed, 'unsafe')
        self.assertFalse((self.root/workflow.WORKFLOW_FILE).exists())


if __name__ == '__main__':
    unittest.main()
