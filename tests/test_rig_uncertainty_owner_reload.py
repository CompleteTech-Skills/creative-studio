"""Public save/reload regressions for historical pending anatomy evidence.

The old-format seed is explicitly simulated by the existing legacy helper;
these tests do not execute an old writer or require historical Git objects.
Every subsequent change uses the native public mutation and persistence APIs.
"""
from copy import deepcopy
import unittest

import test_rig_legacy_uncertainty as legacy
from rig_demo import make_geometry
from rig_errors import read_json
from rig_jobs import save_rig
from rig_models import load_rig
from rig_operations import mutate


class RigUncertaintyOwnerReloadTests(unittest.TestCase):
    # Reuse fixture and public correction helpers without inheriting/discovering
    # the existing test methods a second time.
    old_name = legacy.RigLegacyUncertaintyTests.old_name
    renamed = legacy.RigLegacyUncertaintyTests.renamed
    other = legacy.RigLegacyUncertaintyTests.other
    setUp = legacy.RigLegacyUncertaintyTests.setUp
    update = legacy.RigLegacyUncertaintyTests.update
    remove = legacy.RigLegacyUncertaintyTests.remove
    apply = legacy.RigLegacyUncertaintyTests.apply
    historical_format = legacy.RigLegacyUncertaintyTests.historical_format

    def bone(self, name):
        return next(b for b in self.rig.skeleton['bones'] if b['id'] == name)

    def roundtrip(self):
        """Exercise another native save and load, preserving earlier evidence."""
        retained = {path: path.read_bytes()
                    for directory in ('rig/revisions', 'rig/corrections')
                    for path in (self.rig.root / directory).glob('*.json')}
        skeleton = deepcopy(self.rig.skeleton)
        uncertainties = deepcopy(self.rig.manifest['uncertainties'])
        save_rig(self.rig, operation='synthetic-owner-reload-regression')
        self.rig = load_rig(self.rig.root)
        self.assertEqual(read_json(self.rig.root / 'rig.json'), self.rig.manifest)
        self.assertEqual(self.rig.skeleton, skeleton)
        self.assertEqual(self.rig.manifest['uncertainties'], uncertainties)
        self.assertEqual(self.rig.manifest['source'], self.source_record)
        self.assertEqual((self.rig.root / self.source_record['path']).read_bytes(),
                         self.source_bytes)
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)

    def reuse_old_name(self, reuse):
        # Explicitly simulated old-format record, not an old-writer execution.
        self.historical_format()
        self.original_bone = deepcopy(self.bone(self.renamed))
        if reuse == 'rename-known':
            self.apply(self.update(self.other, id=self.old_name))
            expected = [self.initial[0], dict(self.initial[1], element=self.old_name)]
        else:
            original = self.bone(self.renamed)
            value = dict(role='synthetic-unrelated-jaw', parent=original['parent'],
                         head=[v + .02 for v in original['head']],
                         tail=[v + .02 for v in original['tail']], deform=False)
            self.apply(dict(operation='bone-add', target=self.old_name, value=value))
            expected = self.initial
        self.assertEqual(self.rig.manifest['uncertainties'], expected)
        self.roundtrip()
        self.assertEqual(self.rig.manifest['uncertainties'], expected)

    def assert_original_unchanged(self):
        self.assertEqual(self.bone(self.renamed), self.original_bone)

    def anatomical_edit_after_reload(self, field):
        for reuse in ('add-new', 'rename-known'):
            with self.subTest(reuse=reuse, field=field):
                # Each subcase has its own native run, source and version chain.
                if reuse == 'rename-known':
                    self.setUp()
                self.reuse_old_name(reuse)
                target = self.bone(self.old_name)
                value = .25 if field == 'roll' else [v + .01 for v in target['head']]
                self.apply(self.update(self.old_name, **{field: value}))
                self.roundtrip()
                self.assertEqual(self.rig.manifest['uncertainties'][0], self.initial[0],
                                 'Editing the reused name cannot correct historical anatomy')
                self.assert_original_unchanged()
                if reuse == 'rename-known':
                    self.assertEqual(self.rig.manifest['uncertainties'][1]['resolution'],
                                     'corrected')

    def test_roll_edit_after_reload_preserves_orphan_pending_evidence(self):
        self.anatomical_edit_after_reload('roll')

    def test_head_edit_after_reload_preserves_orphan_pending_evidence(self):
        self.anatomical_edit_after_reload('head')

    def test_removal_after_reload_preserves_orphan_pending_evidence(self):
        for reuse in ('add-new', 'rename-known'):
            with self.subTest(reuse=reuse):
                if reuse == 'rename-known':
                    self.setUp()
                self.reuse_old_name(reuse)
                self.apply(self.remove(self.old_name))
                self.roundtrip()
                expected = self.initial if reuse == 'add-new' else self.initial[:1]
                self.assertEqual(self.rig.manifest['uncertainties'], expected)
                self.assertNotIn(self.old_name, [b['id'] for b in self.rig.skeleton['bones']])
                self.assert_original_unchanged()

    def test_rename_after_reload_preserves_orphan_pending_evidence(self):
        for reuse in ('add-new', 'rename-known'):
            with self.subTest(reuse=reuse):
                if reuse == 'rename-known':
                    self.setUp()
                self.reuse_old_name(reuse)
                self.apply(self.update(self.old_name, id='unrelated-renamed'))
                self.roundtrip()
                expected = self.initial if reuse == 'add-new' else [
                    self.initial[0], dict(self.initial[1], element='unrelated-renamed')]
                self.assertEqual(self.rig.manifest['uncertainties'], expected)
                self.assertIn('unrelated-renamed', [b['id'] for b in self.rig.skeleton['bones']])
                self.assert_original_unchanged()

    def test_known_owner_rename_edit_removal_still_work_across_reloads(self):
        self.apply(self.update(self.old_name, id=self.renamed))
        self.roundtrip()
        expected = [dict(self.initial[0], element=self.renamed), self.initial[1]]
        self.assertEqual(self.rig.manifest['uncertainties'], expected)
        self.apply(self.update(self.renamed, roll=.25))
        self.roundtrip()
        self.assertEqual(self.rig.manifest['uncertainties'][0]['element'], self.renamed)
        self.assertEqual(self.rig.manifest['uncertainties'][0]['resolution'], 'corrected')
        self.assertEqual(self.rig.manifest['uncertainties'][1], self.initial[1])
        self.apply(self.remove(self.renamed))
        self.roundtrip()
        self.assertEqual(self.rig.manifest['uncertainties'], self.initial[1:])

    def test_byte_identical_occurrences_keep_distinct_ownership_after_reload(self):
        # Generate both missing-jaw records through the real fitting workflow.
        _, _, landmarks, _ = make_geometry('robotic')
        landmarks.pop(self.old_name)
        landmarks.pop(self.other)
        result = mutate(self.rig.root, 'auto', dict(template='robotic', replace=True,
            landmarks=dict(source_sha256=self.rig.manifest['source']['sha256'],
                           landmarks=landmarks)))
        self.assertEqual(result['status'], 'succeeded')
        self.rig = load_rig(self.rig.root)
        self.initial = deepcopy(self.rig.manifest['uncertainties'])
        self.assertEqual([u['element'] for u in self.initial], [self.old_name, self.other])
        self.reuse_old_name('rename-known')
        self.assertEqual(self.rig.manifest['uncertainties'], [self.initial[0]] * 2,
                         'The historical and known records must really be byte-identical')
        self.roundtrip()
        self.apply(self.remove(self.old_name))
        self.roundtrip()
        self.assertEqual(self.rig.manifest['uncertainties'], self.initial[:1],
                         'Only the actual removed bone loses its own occurrence')
        self.assert_original_unchanged()


if __name__ == '__main__':
    unittest.main()
