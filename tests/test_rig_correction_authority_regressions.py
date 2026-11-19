"""Native public correction authority and pose identity regressions (#544/#545).

These tests import only synthetic local OBJ bytes and use the public correction
facade. No mocked importer, external renderer, provider, or production asset is
involved. Non-persisted corrections are explicitly handle-relative operations.
"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from rig_api import apply_rig_corrections, create_run, load_rig
from rig_animation import load_pose, save_pose
from rig_errors import RigError, timestamp
from rig_models import rig_signature
from rig_operations import mutate, pose_operation
from rig_skeleton import neutral_pose


class CorrectionAuthorityRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.source = self.parent / 'synthetic.obj'
        self.source.write_bytes(
            b'o synthetic\nv -1 0 -1\nv 1 0 -1\nv 0 2 0\nv 0 0 1\n'
            b'f 1 3 2\nf 1 4 3\nf 2 3 4\nf 1 2 4\n')
        imported = create_run(self.source, self.parent / 'run', fixture=True)
        mutate(imported.root, 'auto', {'template': 'custom'})
        self.rig = load_rig(imported.root)
        unrelated = self.rig.root / 'unrelated' / 'artist-notes.bin'
        unrelated.parent.mkdir()
        unrelated.write_bytes(b'Independent unrelated artifact\x00\xff\n')

    def snapshot(self, *, include_job=True):
        """Compare actual bytes and membership, including immutable history."""
        return {
            path.relative_to(self.rig.root).as_posix(): path.read_bytes()
            for path in self.rig.root.rglob('*') if path.is_file()
            and (include_job or path.relative_to(self.rig.root).as_posix() != 'job.json')
        }

    def envelope(self, rig, edits):
        return dict(schema_version=1, kind='hatch-3d-rig-correction',
                    rig_id=rig.manifest['id'], rig_version=rig.manifest['version'],
                    expected_sha256=rig_signature(rig.manifest),
                    author='synthetic regression', reason='Independent correction regression',
                    created_at=timestamp(), edits=edits)

    def role_edit(self, role):
        return [dict(operation='bone-update', target='root', value={'role': role})]

    def template_edit(self, length):
        return [dict(operation='template', target='custom', value={
            'template': 'custom', 'landmarks': {'root': {
                'head': [0, 0, 0], 'tail': [0, length, 0], 'approved': True}}})]

    def direct(self, rig, edits, **kwargs):
        return apply_rig_corrections(rig, self.envelope(rig, edits), **kwargs)[0]

    def advance_with_stale_handle(self):
        stale = load_rig(self.rig.root)
        current = self.direct(load_rig(self.rig.root), self.role_edit('current-role'))
        self.assertEqual(current.skeleton['bones'][0]['role'], 'current-role')
        return stale, current

    def attempt_stale(self, stale):
        try:
            self.direct(stale, self.role_edit('stale-role'))
        except RigError as error:
            return error
        return None

    def assert_retained(self, before):
        after = self.snapshot()
        for name, content in before.items():
            if name not in ('rig.json', 'job.json'):
                self.assertIn(name, after)
                self.assertEqual(after[name], content, name)
        self.assertEqual(self.source.read_bytes(),
                         (self.rig.root / self.rig.manifest['source']['path']).read_bytes())

    def test_stale_direct_handle_is_rejected_by_public_facade(self):
        stale, _ = self.advance_with_stale_handle()
        original = deepcopy(stale.manifest)
        error = self.attempt_stale(stale)
        self.assertIsNotNone(error, 'A stale direct RigAsset correction was accepted')
        self.assertEqual(error.report['code'], 'RIG_STALE_CORRECTION')
        self.assertEqual(stale.manifest, original)

    def test_stale_direct_attempt_preserves_every_saved_byte_and_file(self):
        stale, current = self.advance_with_stale_handle()
        before = self.snapshot()
        self.attempt_stale(stale)
        self.assertEqual(self.snapshot(), before,
                         'Stale refusal must not write a manifest, correction, revision, or job')
        self.assertEqual(load_rig(current.root).manifest, current.manifest)

    def test_dispatcher_stale_control_preserves_artifacts(self):
        stale, current = self.advance_with_stale_handle()
        before = self.snapshot(include_job=False)
        with self.assertRaises(RigError) as caught:
            mutate(stale.root, 'correct', {
                'correction': self.envelope(stale, self.role_edit('stale-role'))})
        self.assertEqual(caught.exception.report['code'], 'RIG_STALE_CORRECTION')
        self.assertEqual(self.snapshot(include_job=False), before)
        self.assertEqual(load_rig(current.root).manifest, current.manifest)

    def test_fresh_direct_then_dispatcher_corrections_persist(self):
        before = self.snapshot()
        direct = self.direct(load_rig(self.rig.root), self.role_edit('direct-role'))
        self.assertEqual(load_rig(direct.root).skeleton['bones'][0]['role'], 'direct-role')
        fresh = load_rig(direct.root)
        outcome = mutate(fresh.root, 'correct', {
            'correction': self.envelope(fresh, self.role_edit('dispatcher-role'))})
        self.assertEqual(outcome['status'], 'succeeded')
        current = load_rig(fresh.root)
        self.assertEqual(current.skeleton['bones'][0]['role'], 'dispatcher-role')
        self.assertGreater(current.manifest['revision'], direct.manifest['revision'])
        self.assertEqual(len(list((fresh.root / 'rig/corrections').glob('*.json'))), 2)
        self.assert_retained(before)

    def test_persist_false_is_handle_relative_and_has_no_disk_side_effects(self):
        for stale_handle in (False, True):
            with self.subTest(stale_handle=stale_handle):
                handle = load_rig(self.rig.root)
                if stale_handle:
                    self.direct(load_rig(self.rig.root), self.role_edit('new-current-role'))
                before = self.snapshot()
                original = deepcopy(handle.manifest)
                detached = self.direct(handle, self.role_edit('detached-role'), persist=False)
                self.assertEqual(detached.skeleton['bones'][0]['role'], 'detached-role')
                self.assertNotEqual(detached.skeleton['version'], handle.skeleton['version'])
                self.assertEqual(handle.manifest, original)
                self.assertEqual(self.snapshot(), before)
                self.assertIsNot(detached, handle)

    def test_persist_false_still_rejects_an_envelope_not_bound_to_its_handle(self):
        stale, current = self.advance_with_stale_handle()
        before = self.snapshot()
        with self.assertRaises(RigError) as caught:
            apply_rig_corrections(stale, self.envelope(current, self.role_edit('wrong-role')),
                                  persist=False)
        self.assertEqual(caught.exception.report['code'], 'RIG_STALE_CORRECTION')
        self.assertEqual(self.snapshot(), before)

    def two_templates(self):
        first = self.direct(load_rig(self.rig.root), self.template_edit(0.7))
        pose_path = self.parent / 'saved-first-pose.json'
        save_pose(pose_path, neutral_pose(first.skeleton))
        first_pose = load_pose(pose_path)
        second = self.direct(load_rig(first.root), self.template_edit(1.3))
        self.assertNotEqual(first.skeleton['bones'][0]['tail'], second.skeleton['bones'][0]['tail'])
        return first, second, first_pose

    def test_successive_template_rest_revisions_have_distinct_versions(self):
        first, second, _ = self.two_templates()
        self.assertNotEqual(first.skeleton['version'], second.skeleton['version'],
                            'Distinct template rest revisions reused a skeleton version')
        third = self.direct(load_rig(second.root), self.template_edit(1.8))
        self.assertEqual(len({first.skeleton['version'], second.skeleton['version'],
                              third.skeleton['version']}), 3)

    def test_saved_pose_from_earlier_template_is_rejected(self):
        _, second, first_pose = self.two_templates()
        before = self.snapshot()
        with self.assertRaises(RigError) as caught:
            pose_operation(load_rig(second.root), {'pose': first_pose})
        self.assertEqual(caught.exception.report['code'], 'RIG_POSE_VERSION')
        self.assertEqual(self.snapshot(), before)

    def test_fresh_pose_and_ordinary_edit_controls(self):
        _, second, _ = self.two_templates()
        fresh_pose = neutral_pose(second.skeleton)
        self.assertEqual(pose_operation(second, {'pose': fresh_pose}), fresh_pose)
        changed = self.direct(load_rig(second.root), [dict(
            operation='bone-update', target='root', value={'tail': [0, 1.5, 0]})])
        self.assertNotEqual(changed.skeleton['version'], second.skeleton['version'])
        with self.assertRaises(RigError) as caught:
            pose_operation(changed, {'pose': fresh_pose})
        self.assertEqual(caught.exception.report['code'], 'RIG_POSE_VERSION')
        current_pose = neutral_pose(changed.skeleton)
        self.assertEqual(pose_operation(load_rig(changed.root), {'pose': current_pose}), current_pose)

    def test_template_after_ordinary_edit_cannot_reuse_a_previous_pose_version(self):
        first = self.direct(load_rig(self.rig.root), self.template_edit(0.7))
        saved = self.parent / 'before-ordinary-edit.json'
        save_pose(saved, neutral_pose(first.skeleton))
        ordinary = self.direct(load_rig(first.root), self.role_edit('ordinary-role'))
        last = self.direct(load_rig(ordinary.root), self.template_edit(1.8))
        self.assertEqual(len({first.skeleton['version'], ordinary.skeleton['version'],
                              last.skeleton['version']}), 3,
                         'Template replacement reused a version after an ordinary bone edit')
        with self.assertRaises(RigError) as caught:
            pose_operation(last, {'pose': load_pose(saved)})
        self.assertEqual(caught.exception.report['code'], 'RIG_POSE_VERSION')

    def test_successive_corrections_preserve_source_and_previous_history(self):
        before = self.snapshot()
        first = self.direct(load_rig(self.rig.root), self.template_edit(0.7))
        self.assert_retained(before)
        after_first = self.snapshot()
        self.assertTrue(any(name.startswith('rig/corrections/') for name in after_first))
        self.assertTrue(any(name.startswith('rig/revisions/') for name in after_first))
        second = self.direct(load_rig(first.root), self.template_edit(1.3))
        self.assert_retained(after_first)
        self.assertEqual(load_rig(second.root).manifest, second.manifest)


if __name__ == '__main__':
    unittest.main()
