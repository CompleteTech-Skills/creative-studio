"""Independent #52 correction oracles using saved synthetic rigs and real dispatch.

Bone identity, semantics and binding metadata do not establish anatomical
placement. The free-text uncertainty codes prefixed FIXTURE_ below deliberately
represent independent evidence domains admitted by the public Rig contract.
"""
from copy import deepcopy
import tempfile
import unittest

from rig_demo import make_geometry
from rig_errors import read_json, timestamp
from rig_jobs import save_rig
from rig_models import load_rig, rig_signature
from rig_operations import mutate
from rig_test_helpers import fixture


class RigCorrectionEvidenceDomainTests(unittest.TestCase):
    target = 'jaw.left'
    other = 'jaw.right'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name, animated=False)
        _, self.template, self.landmarks, _ = make_geometry('robotic')
        for name in (self.target, self.other):
            self.landmarks[name].update(source='human-landmark', confidence=.5,
                                        reference='Synthetic low-confidence landmark: ' + name)
        source = self.rig.root / self.rig.manifest['source']['path']
        self.original_source = source.read_bytes()
        self.source_record = deepcopy(self.rig.manifest['source'])
        result = mutate(self.rig.root, 'auto', dict(replace=True, template='robotic',
            landmarks=dict(source_sha256=self.rig.manifest['source']['sha256'],
                           landmarks=self.landmarks)))
        self.assertEqual(result['status'], 'succeeded')
        self.rig = load_rig(self.rig.root)
        self.before_bones = deepcopy(self.bones())
        self.before_issues = deepcopy(self.rig.manifest['uncertainties'])
        self.assertEqual({u['element'] for u in self.before_issues}, {self.target, self.other})
        self.assertTrue(all(u['resolution'] == 'pending' for u in self.before_issues))
        self.assertEqual(self.bones()[self.target]['evidence']['confidence'], .5)

    def bones(self):
        return {bone['id']: bone for bone in self.rig.skeleton['bones']}

    def correction(self, edits):
        return dict(schema_version=1, kind='hatch-3d-rig-correction',
            rig_id=self.rig.manifest['id'], rig_version=self.rig.manifest['version'],
            expected_sha256=rig_signature(self.rig.manifest),
            author='synthetic evidence-domain test', reason='Verify independent evidence domains',
            created_at=timestamp(), edits=edits)

    def edit(self, value, target=None):
        return dict(operation='bone-update', target=target or self.target, value=value)

    def apply(self, *edits):
        history = {path: path.read_bytes() for directory in ('rig/revisions', 'rig/corrections')
                   for path in (self.rig.root / directory).glob('*.json')}
        result = mutate(self.rig.root, 'correct', {'correction': self.correction(list(edits))})
        self.assertEqual(result['status'], 'succeeded')
        self.rig = load_rig(self.rig.root)
        self.assertEqual(read_json(self.rig.root / 'rig.json'), self.rig.manifest)
        self.assertEqual(read_json(self.rig.root / 'job.json')['steps']['correct']['status'],
                         'succeeded')
        self.assertEqual(self.rig.manifest['source'], self.source_record)
        self.assertEqual((self.rig.root / self.source_record['path']).read_bytes(),
                         self.original_source)
        self.assertEqual({path: path.read_bytes() for path in history}, history)

    def assert_pending(self, *, renamed=None):
        target = renamed or self.target
        expected = deepcopy(self.before_issues)
        for issue in expected:
            if issue['element'] == self.target:
                issue['element'] = target
        # Compare whole records, including original evidence, messages and order.
        self.assertEqual(self.rig.manifest['uncertainties'], expected)
        self.assertEqual(self.bones()[target]['evidence'],
                         self.before_bones[self.target]['evidence'])
        self.assertEqual(self.bones()[self.other]['evidence'],
                         self.before_bones[self.other]['evidence'])

    def assert_anatomically_corrected(self, *, renamed=None):
        target = renamed or self.target
        expected = deepcopy(self.before_issues)
        for issue in expected:
            if issue['element'] == self.target and issue['code'] == 'RIG_ANATOMY_UNCERTAIN':
                issue.update(element=target, resolution='corrected')
        self.assertEqual(self.rig.manifest['uncertainties'], expected)
        evidence = self.bones()[target]['evidence']
        self.assertEqual(evidence['source'], 'human-landmark')
        self.assertEqual(evidence['confidence'], 1.)
        self.assertEqual(self.bones()[self.other], self.before_bones[self.other])

    def test_role_change_keeps_original_landmark_evidence_pending(self):
        self.apply(self.edit({'role': 'reviewed-jaw-label'}))
        self.assertEqual(self.bones()[self.target]['role'], 'reviewed-jaw-label')
        self.assert_pending()

    def test_mirror_pairing_keeps_original_landmark_evidence_pending(self):
        self.apply(self.edit({'mirror': self.other}),
                   self.edit({'mirror': self.target}, target=self.other))
        self.assertEqual(self.bones()[self.target]['mirror'], self.other)
        self.assertEqual(self.bones()[self.other]['mirror'], self.target)
        self.assert_pending()

    def test_deform_binding_change_keeps_original_landmark_evidence_pending(self):
        self.apply(self.edit({'deform': False}))
        self.assertIs(self.bones()[self.target]['deform'], False)
        self.assert_pending()

    def test_rename_keeps_original_landmark_evidence_pending(self):
        self.apply(self.edit({'id': 'left-jaw'}))
        self.assertNotIn(self.target, self.bones())
        self.assert_pending(renamed='left-jaw')

    def test_combined_metadata_change_does_not_supply_anatomical_evidence(self):
        self.apply(self.edit(dict(id='left-jaw', role='reviewed-jaw-label',
                                  mirror=self.other, deform=False)),
                   self.edit({'mirror': 'left-jaw'}, target=self.other))
        self.assert_pending(renamed='left-jaw')

    def test_persistent_head_change_supplies_anatomical_evidence(self):
        head = list(self.before_bones[self.target]['head'])
        head[2] -= .02
        self.apply(self.edit({'head': head}))
        self.assertEqual(self.bones()[self.target]['head'], head)
        self.assert_anatomically_corrected()

    def test_persistent_tail_change_supplies_anatomical_evidence(self):
        tail = list(self.before_bones[self.target]['tail'])
        tail[2] -= .02
        self.apply(self.edit({'tail': tail}))
        self.assertEqual(self.bones()[self.target]['tail'], tail)
        self.assert_anatomically_corrected()

    def test_persistent_roll_change_supplies_anatomical_evidence(self):
        self.apply(self.edit({'roll': .25}))
        self.assertEqual(self.bones()[self.target]['roll'], .25)
        self.assert_anatomically_corrected()

    def test_persistent_hierarchy_change_supplies_anatomical_evidence(self):
        self.apply(self.edit({'parent': 'elbow'}))
        self.assertEqual(self.bones()[self.target]['parent'], 'elbow')
        self.assert_anatomically_corrected()

    def test_rename_with_persistent_geometry_change_corrects_renamed_anatomy(self):
        tail = list(self.before_bones[self.target]['tail'])
        tail[2] -= .02
        self.apply(self.edit({'id': 'left-jaw', 'tail': tail}))
        self.assertEqual(self.bones()['left-jaw']['tail'], tail)
        self.assert_anatomically_corrected(renamed='left-jaw')

    def test_atomic_geometry_edit_and_numeric_revert_preserve_original_evidence(self):
        before = self.before_bones[self.target]
        tail = list(before['tail'])
        tail[2] -= .02
        self.apply(self.edit({'tail': tail, 'roll': .25, 'parent': 'elbow'}),
                   self.edit({'tail': list(map(str, before['tail'])),
                              'roll': before['roll'], 'parent': before['parent']}))
        self.assertEqual(self.bones()[self.target], before)
        self.assert_pending()

    def test_atomic_geometry_revert_with_metadata_change_still_remains_pending(self):
        tail = list(self.before_bones[self.target]['tail'])
        tail[2] -= .02
        self.apply(self.edit({'tail': tail}),
                   self.edit({'tail': self.before_bones[self.target]['tail'],
                              'role': 'reviewed-jaw-label'}))
        self.assertEqual(self.bones()[self.target]['tail'],
                         self.before_bones[self.target]['tail'])
        self.assert_pending()

    def independent_uncertainties(self):
        # Explicit synthetic domains, using the public free-text code contract.
        return [dict(element=self.target, code=code,
                     message='Synthetic independent-domain uncertainty: ' + code,
                     evidence=dict(source='import-metadata', reference='Synthetic evidence: ' + code,
                                   confidence=.3), resolution=resolution)
                for code in ('FIXTURE_BINDING_UNCERTAIN', 'FIXTURE_SOURCE_UNCERTAIN')
                for resolution in ('pending', 'accepted', 'corrected')]

    def save_independent_uncertainties(self, *, first=False):
        independent = self.independent_uncertainties()
        anatomy = deepcopy(self.rig.manifest['uncertainties'])
        self.rig.manifest['uncertainties'] = (independent + anatomy if first
                                               else anatomy + independent)
        save_rig(self.rig, operation='synthetic-independent-uncertainty-fixture')
        self.rig = load_rig(self.rig.root)
        self.before_issues = deepcopy(self.rig.manifest['uncertainties'])
        return independent

    def test_anatomical_edit_preserves_independent_records_after_anatomy_record(self):
        independent = self.save_independent_uncertainties()
        self.apply(self.edit({'roll': .25}))
        self.assertEqual([u for u in self.rig.manifest['uncertainties']
                          if u['code'].startswith('FIXTURE_')], independent)
        self.assert_anatomically_corrected()

    def test_anatomical_edit_preserves_independent_records_before_anatomy_record(self):
        independent = self.save_independent_uncertainties(first=True)
        self.apply(self.edit({'roll': .25}))
        self.assertEqual(self.rig.manifest['uncertainties'][:len(independent)], independent)
        self.assert_anatomically_corrected()

    def test_template_refit_preserves_independently_typed_uncertainty_records(self):
        independent = self.save_independent_uncertainties()
        replace = dict(operation='template', target='robotic',
                       value=dict(template=self.template, landmarks=self.landmarks))
        self.apply(replace)
        self.assertEqual([u for u in self.rig.manifest['uncertainties']
                          if u['code'].startswith('FIXTURE_')], independent)
        anatomy = [u for u in self.rig.manifest['uncertainties']
                   if u['code'] == 'RIG_ANATOMY_UNCERTAIN']
        self.assertEqual(anatomy, [u for u in self.before_issues
                                  if u['code'] == 'RIG_ANATOMY_UNCERTAIN'])
        self.assertEqual(self.bones(), self.before_bones)
