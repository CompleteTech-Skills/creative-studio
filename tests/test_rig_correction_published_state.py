"""Native corrections must identify the skeleton they actually publish (#544/#545).

Only synthetic local OBJ geometry, native authoring, persistence and reload are
used. These regressions exercise public handles without mocking ownership or
persistence, and retain the handle-relative contract for detached previews.
"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from rig_api import (apply_rig_corrections, create_run, generate_skeleton,
                     generate_skin_weights, load_rig, load_rig_template, save_rig)
from rig_animation import clip_from_poses, load_pose, save_pose
from rig_errors import RigError, timestamp
from rig_models import rig_signature
from rig_operations import pose_operation
from rig_skeleton import neutral_pose
from rig_states import map_states


class CorrectionPublishedStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.source = self.parent / 'synthetic.obj'
        self.source.write_bytes(
            b'o synthetic\nv -1 0 -1\nv 1 0 -1\nv 0 2 0\nv 0 0 1\n'
            b'f 1 3 2\nf 1 4 3\nf 2 3 4\nf 1 2 4\n')

    @staticmethod
    def snapshot(rig):
        return {p.relative_to(rig.root).as_posix(): p.read_bytes()
                for p in rig.root.rglob('*') if p.is_file()}

    @staticmethod
    def expression(name='published-expression'):
        return [dict(operation='expression', target=name, value=dict(
            schema_version=1, morphs={}, bones={}, incompatible=[]))]

    @staticmethod
    def template_definition():
        template = load_rig_template('custom')
        template['bones'][0]['deform'] = True
        return template

    def template(self, length):
        return [dict(operation='template', target='custom', value={
            'template': self.template_definition(), 'landmarks': {'root': {
                'head': [0, 0, 0], 'tail': [0, length, 0], 'approved': True}}})]

    def correct(self, rig, edits, *, persist=True):
        envelope = dict(schema_version=1, kind='hatch-3d-rig-correction',
                        rig_id=rig.manifest['id'], rig_version=rig.manifest['version'],
                        expected_sha256=rig_signature(rig.manifest),
                        author='synthetic regression', reason='Published skeleton identity',
                        created_at=timestamp(), edits=edits)
        return apply_rig_corrections(rig, envelope, persist=persist)

    def saved_history(self, name):
        rig = create_run(self.source, self.parent / name, fixture=True)
        fitted = generate_skeleton(rig.geometry, self.template_definition(),
                                   character_id=rig.manifest['character']['id'])
        for key in ('skeleton', 'controllers', 'attachments', 'uncertainties'):
            rig.manifest[key] = fitted[key]
        save_rig(rig, operation='synthetic-skeleton-authoring')
        rig = load_rig(rig.root)
        historical = []
        for index, length in enumerate((None, 0.7, 1.3)):
            if length is not None:
                rig, _ = self.correct(load_rig(rig.root), self.template(length))
            self.assertEqual(rig.skeleton['version'], '1.0.' + str(index))
            path = self.parent / (name + '-pose-' + str(index) + '.json')
            save_pose(path, neutral_pose(rig.skeleton))
            historical.append(load_pose(path))
        rig.weights = generate_skin_weights(rig.geometry, rig.skeleton, method='distance')
        pose = neutral_pose(rig.skeleton)
        rig.clips['neutral'] = clip_from_poses(rig, 'neutral', [0., 1.], [pose, pose])
        rig.manifest['state_mapping'] = map_states(rig, selected=['static-neutral'])['mapping']
        rig.manifest['facial']['expressions']['artist-authored'] = self.expression()[0]['value']
        save_rig(rig, operation='synthetic-dependent-authoring')
        note = rig.root / 'artist-notes.bin'
        note.write_bytes(b'Unrelated authored artifact\x00\xff')
        rig = load_rig(rig.root)
        self.assertTrue(rig.weights)
        self.assertTrue(rig.clips)
        self.assertTrue(rig.manifest['state_mapping'])
        return rig, historical

    def replacement(self, name, *, detached=False):
        current, historical = self.saved_history(name)
        handle = load_rig(current.root)
        fitted = generate_skeleton(handle.geometry, self.template_definition(),
                                   character_id=handle.manifest['character']['id'],
                                   landmarks={'root': {'head': [0, 0, 0],
                                                      'tail': [0, 1.8, 0], 'approved': True}})
        for key in ('skeleton', 'controllers', 'attachments', 'uncertainties'):
            handle.manifest[key] = fitted[key]
        handle.manifest['facial']['expressions']['unsaved-authoring'] = self.expression()[0]['value']
        self.assertEqual(handle.skeleton['version'], '1.0.0')
        if detached:
            before = self.snapshot(current)
            handle, _ = self.correct(handle, self.template(2.1), persist=False)
            self.assertEqual(handle.skeleton['version'], '1.0.1')
            self.assertEqual(self.snapshot(current), before)
        self.assertNotEqual(handle.skeleton['bones'][0]['tail'], current.skeleton['bones'][0]['tail'])
        return current, handle, historical

    def test_expression_publication_assigns_successor_for_actual_generated_or_detached_replacement(self):
        for detached in (False, True):
            with self.subTest(detached=detached):
                current, handle, _ = self.replacement('identity-' + str(detached), detached=detached)
                original = deepcopy(handle.manifest)
                updated, report = self.correct(handle, self.expression())
                saved = load_rig(updated.root)
                self.assertEqual(saved.skeleton['version'], '1.0.3')
                self.assertEqual(saved.skeleton['bones'], handle.skeleton['bones'])
                self.assertEqual(saved.manifest, updated.manifest)
                self.assertEqual(report['rig_sha256'], rig_signature(saved.manifest))
                self.assertEqual(handle.manifest, original)
                self.assertEqual(current.skeleton['version'], '1.0.2')

    def test_every_historical_pose_is_rejected_after_expression_publishes_replacement(self):
        for detached in (False, True):
            current, handle, historical = self.replacement('poses-' + str(detached), detached=detached)
            updated, _ = self.correct(handle, self.expression())
            saved = load_rig(updated.root)
            before = self.snapshot(saved)
            for pose in historical:
                with self.subTest(detached=detached, historical_version=pose['rig_version']):
                    with self.assertRaises(RigError) as caught:
                        pose_operation(saved, {'pose': pose})
                    self.assertEqual(caught.exception.report['code'], 'RIG_POSE_VERSION')
            fresh_path = self.parent / ('fresh-' + str(detached) + '.json')
            save_pose(fresh_path, neutral_pose(saved.skeleton))
            fresh = load_pose(fresh_path)
            self.assertEqual(pose_operation(saved, {'pose': fresh}), fresh)
            self.assertEqual(self.snapshot(saved), before)

    def test_actual_generated_rest_change_invalidates_saved_dependent_state(self):
        _, handle, _ = self.replacement('dependents')
        self.assertTrue(handle.weights)
        self.assertTrue(handle.clips)
        self.assertTrue(handle.manifest['state_mapping'])
        updated, report = self.correct(handle, self.expression())
        saved = load_rig(updated.root)
        for field in ('weights', 'clips'):
            with self.subTest(field=field):
                self.assertFalse(getattr(saved, field))
        for field in ('skins', 'animations', 'state_mapping'):
            with self.subTest(field=field):
                self.assertFalse(saved.manifest[field])
                self.assertIn(field, report['invalidated'])

    def test_replacement_publication_retains_original_history_and_unrelated_authoring(self):
        for detached in (False, True):
            with self.subTest(detached=detached):
                current, handle, _ = self.replacement('retention-' + str(detached), detached=detached)
                before = self.snapshot(current)
                updated, _ = self.correct(handle, self.expression())
                after = self.snapshot(updated)
                for name, content in before.items():
                    if name not in ('rig.json', 'job.json'):
                        self.assertEqual(after[name], content, name)
                self.assertEqual(self.source.read_bytes(),
                                 (updated.root / updated.manifest['source']['path']).read_bytes())
                self.assertIn('artist-authored', updated.manifest['facial']['expressions'])
                self.assertIn('unsaved-authoring', updated.manifest['facial']['expressions'])
                self.assertIn('published-expression', updated.manifest['facial']['expressions'])
                self.assertGreater(len([p for p in after if p.startswith('rig/revisions/')]),
                                   len([p for p in before if p.startswith('rig/revisions/')]))

    def test_unchanged_skeleton_expression_and_weight_corrections_keep_identity_and_dependents(self):
        for edit_kind in ('expression', 'weights'):
            with self.subTest(edit_kind=edit_kind):
                current, historical = self.saved_history('unchanged-' + edit_kind)
                mesh = current.geometry.meshes[0].id
                edits = self.expression() if edit_kind == 'expression' else [dict(
                    operation='weights', target=mesh, value={'assignments': {'0': {'root': 1.}}})]
                updated, report = self.correct(current, edits)
                saved = load_rig(updated.root)
                self.assertEqual(saved.skeleton, current.skeleton)
                self.assertEqual(saved.clips, current.clips)
                self.assertEqual(saved.manifest['state_mapping'], current.manifest['state_mapping'])
                self.assertTrue(saved.weights)
                self.assertNotIn('skins', report['invalidated'])
                self.assertNotIn('animations', report['invalidated'])
                self.assertNotIn('state_mapping', report['invalidated'])
                for key, skin in current.weights.items():
                    np.testing.assert_array_equal(saved.weights[key].indices, skin.indices)
                    np.testing.assert_array_equal(saved.weights[key].weights, skin.weights)
                if edit_kind == 'weights':
                    self.assertIn(0, saved.weights[mesh].locked)
                self.assertEqual(pose_operation(saved, {'pose': historical[-1]}), historical[-1])

    def test_expression_preview_after_replacement_is_handle_relative_even_when_saved_rig_advances(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                current, handle, _ = self.replacement('preview-' + str(stale))
                if stale:
                    self.correct(current, self.template(2.3))
                before = self.snapshot(current)
                original = deepcopy(handle.manifest)
                preview, report = self.correct(handle, self.expression(), persist=False)
                self.assertEqual(preview.skeleton, handle.skeleton)
                self.assertEqual(preview.clips, handle.clips)
                self.assertEqual(preview.manifest['state_mapping'], handle.manifest['state_mapping'])
                self.assertTrue(preview.weights)
                self.assertEqual(report['invalidated'], [])
                self.assertIn('published-expression', preview.manifest['facial']['expressions'])
                self.assertEqual(handle.manifest, original)
                self.assertEqual(self.snapshot(current), before)

    def test_direct_detached_template_then_expression_publishes_one_saved_successor(self):
        current, _ = self.saved_history('direct-detached')
        before = self.snapshot(current)
        detached, _ = self.correct(current, self.template(1.8), persist=False)
        self.assertEqual(self.snapshot(current), before)
        self.assertEqual(detached.skeleton['version'], '1.0.3')
        updated, _ = self.correct(detached, self.expression())
        saved = load_rig(updated.root)
        self.assertEqual(saved.skeleton['version'], '1.0.3')
        self.assertEqual(saved.skeleton['bones'], detached.skeleton['bones'])
        self.assertFalse(saved.weights)
        self.assertFalse(saved.clips)
        self.assertFalse(saved.manifest['state_mapping'])


if __name__ == '__main__':
    unittest.main()
