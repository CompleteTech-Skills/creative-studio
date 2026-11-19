"""Native writer contention and legitimate detached edits for #544/#545."""
from copy import deepcopy
import unittest

import test_rig_correction_authority_regressions as regression_fixture
from rig_api import apply_rig_corrections, generate_skeleton, load_rig, save_rig
from rig_animation import load_pose, save_pose
from rig_errors import RigError, atomic_json, job_lock, read_json
from rig_models import RigAsset, rig_signature
from rig_operations import mutate, pose_operation
from rig_skeleton import neutral_pose


class CorrectionPersistenceBoundaryTests(unittest.TestCase):
    # Reuse only the independent synthetic fixture and data helpers, never its
    # test methods or the implementation's admission bookkeeping.
    setUp = regression_fixture.CorrectionAuthorityRegressions.setUp
    snapshot = regression_fixture.CorrectionAuthorityRegressions.snapshot
    envelope = regression_fixture.CorrectionAuthorityRegressions.envelope
    role_edit = regression_fixture.CorrectionAuthorityRegressions.role_edit
    template_edit = regression_fixture.CorrectionAuthorityRegressions.template_edit
    direct = regression_fixture.CorrectionAuthorityRegressions.direct

    def authored_template_handle(self):
        first = self.direct(load_rig(self.rig.root), self.template_edit(0.7))
        current = self.direct(load_rig(first.root), self.template_edit(1.3))
        handle = load_rig(current.root)
        fitted = generate_skeleton(handle.geometry, 'custom',
                                   character_id=handle.manifest['id'])
        for key in ('skeleton', 'controllers', 'attachments', 'uncertainties'):
            handle.manifest[key] = fitted[key]
        handle.manifest['facial']['expressions']['authored'] = dict(
            schema_version=1, morphs={}, bones={}, incompatible=[])
        return first, current, handle

    def test_template_after_unsaved_public_generation_advances_saved_version(self):
        _, current, handle = self.authored_template_handle()
        before = self.snapshot()
        original = deepcopy(handle.manifest)
        updated, report = apply_rig_corrections(
            handle, self.envelope(handle, self.template_edit(1.8)))
        self.assertEqual(current.skeleton['version'], '1.0.2')
        self.assertEqual(updated.skeleton['version'], '1.0.3')
        self.assertEqual(load_rig(updated.root).manifest, updated.manifest)
        self.assertEqual(report['rig_sha256'], rig_signature(updated.manifest))
        self.assertIn('authored', updated.manifest['facial']['expressions'])
        self.assertEqual(handle.manifest, original)
        after = self.snapshot()
        for name, content in before.items():
            if name not in ('rig.json', 'job.json'):
                self.assertEqual(after[name], content, name)
        self.assertEqual(self.source.read_bytes(),
                         (updated.root / updated.manifest['source']['path']).read_bytes())

    def test_saved_pose_cannot_recur_after_unsaved_public_generation(self):
        first, _, handle = self.authored_template_handle()
        pose_path = self.parent / 'saved-first-pose.json'
        save_pose(pose_path, neutral_pose(first.skeleton))
        updated = self.direct(handle, self.template_edit(1.8))
        with self.assertRaises(RigError) as caught:
            pose_operation(load_rig(updated.root), {'pose': load_pose(pose_path)})
        self.assertEqual(caught.exception.report['code'], 'RIG_POSE_VERSION')
        fresh_pose = neutral_pose(updated.skeleton)
        self.assertEqual(pose_operation(load_rig(updated.root), {'pose': fresh_pose}), fresh_pose)
        outcome = mutate(updated.root, 'correct', {
            'correction': self.envelope(updated, self.template_edit(2.1))})
        self.assertEqual(outcome['status'], 'succeeded')
        self.assertEqual(load_rig(updated.root).skeleton['version'], '1.0.4')

    def test_bone_after_unsaved_public_generation_uses_saved_version(self):
        _, _, handle = self.authored_template_handle()
        before = self.snapshot()
        preview = self.direct(handle, self.role_edit('preview-role'), persist=False)
        self.assertEqual(preview.skeleton['version'], '1.0.1')
        self.assertEqual(self.snapshot(), before)
        updated = self.direct(handle, self.role_edit('saved-role'))
        self.assertEqual(updated.skeleton['version'], '1.0.3')
        self.assertEqual(load_rig(updated.root).skeleton['bones'][0]['role'], 'saved-role')
        self.assertEqual(updated.skeleton['bones'][0]['tail'],
                         handle.skeleton['bones'][0]['tail'])
        self.assertIn('authored', updated.manifest['facial']['expressions'])

    def test_native_writer_contention_refuses_without_touching_saved_files(self):
        before = self.snapshot()
        with job_lock(self.rig.root):
            with self.assertRaises(RigError) as caught:
                self.direct(self.rig, self.role_edit('blocked-writer'))
        self.assertEqual(caught.exception.report['code'], 'RIG_BUSY')
        self.assertEqual(self.snapshot(), before)

    def test_same_version_and_revision_do_not_hide_changed_saved_core(self):
        current = read_json(self.rig.root / 'rig.json')
        current['skeleton']['bones'][0]['role'] = 'other-writer'
        atomic_json(self.rig.root / 'rig.json', current)
        before = self.snapshot()
        with self.assertRaises(RigError) as caught:
            self.direct(self.rig, self.role_edit('stale-writer'))
        self.assertEqual(caught.exception.report['code'], 'RIG_STALE_CORRECTION')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(load_rig(self.rig.root).skeleton['bones'][0]['role'], 'other-writer')

    def test_current_handle_retains_legitimate_unsaved_changes(self):
        self.rig.manifest['facial']['expressions']['authored'] = dict(
            schema_version=1, morphs={}, bones={}, incompatible=[])
        updated = self.direct(self.rig, self.role_edit('fresh-edit'))
        loaded = load_rig(updated.root)
        self.assertIn('authored', loaded.manifest['facial']['expressions'])
        self.assertEqual(loaded.skeleton['bones'][0]['role'], 'fresh-edit')

    def test_successful_public_save_refreshes_current_handle_admission(self):
        self.rig.manifest['facial']['expressions']['saved'] = dict(
            schema_version=1, morphs={}, bones={}, incompatible=[])
        save_rig(self.rig, operation='synthetic-authoring')
        updated = self.direct(self.rig, self.role_edit('after-save'))
        self.assertEqual(load_rig(updated.root).manifest, updated.manifest)
        self.assertIn('saved', updated.manifest['facial']['expressions'])

    def test_current_explicitly_constructed_public_handle_remains_supported(self):
        current = load_rig(self.rig.root)
        constructed = RigAsset(deepcopy(current.manifest), deepcopy(current.geometry),
                               deepcopy(current.weights), deepcopy(current.clips), current.root)
        updated = self.direct(constructed, self.role_edit('constructed-current'))
        self.assertEqual(load_rig(updated.root).skeleton['bones'][0]['role'], 'constructed-current')

    def test_saved_report_signature_matches_direct_and_dispatch_publication(self):
        direct, report = apply_rig_corrections(
            self.rig, self.envelope(self.rig, self.role_edit('direct')))
        self.assertEqual(report['rig_sha256'], rig_signature(load_rig(direct.root).manifest))
        result = mutate(direct.root, 'correct', {
            'correction': self.envelope(direct, self.role_edit('dispatched'))})
        saved = load_rig(direct.root)
        self.assertEqual(result['result']['rig_sha256'], rig_signature(saved.manifest))
        self.assertEqual(result['rig_sha256'], rig_signature(saved.manifest))


if __name__ == '__main__':
    unittest.main()
