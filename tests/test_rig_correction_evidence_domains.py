"""Independent #52 correction oracles using saved synthetic rigs and real dispatch.

Bone identity, semantics and binding metadata do not establish anatomical
placement. The free-text uncertainty codes prefixed FIXTURE_ below deliberately
represent independent evidence domains admitted by the public Rig contract.
"""
from copy import deepcopy
import tempfile
import unittest

from rig_demo import make_geometry
from rig_errors import RigError, read_json, sha256, timestamp
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

    def remove(self, target=None):
        return dict(operation='bone-remove', target=target or self.target, value=None)

    def restore(self, *, target=None, **updates):
        fields = ('role', 'parent', 'head', 'tail', 'deform', 'mirror', 'roll')
        value = {key: deepcopy(self.before_bones[self.target][key]) for key in fields}
        value.update(updates)
        return dict(operation='bone-add', target=target or self.target, value=value)

    def test_atomic_leaf_removal_and_identical_restoration_preserve_saved_evidence(self):
        manifest = deepcopy(self.rig.manifest)
        geometry = {mesh.id: (mesh.positions.copy(), mesh.triangles.copy())
                    for mesh in self.rig.geometry.meshes}
        self.apply(self.remove(), self.restore())
        # The persisted/reloaded rest transforms and every bone field are identical.
        self.assertEqual(self.bones(), self.before_bones)
        self.assert_pending()
        for field in ('controllers', 'attachments', 'facial'):
            self.assertEqual(self.rig.manifest[field], manifest[field])
        for mesh in self.rig.geometry.meshes:
            self.assertTrue((mesh.positions == geometry[mesh.id][0]).all())
            self.assertTrue((mesh.triangles == geometry[mesh.id][1]).all())

    def test_atomic_restoration_with_metadata_and_numeric_coordinates_keeps_pending(self):
        before = self.before_bones[self.target]
        self.apply(self.remove(), self.restore(role='reviewed-jaw-label', deform=False,
            head=list(map(str, before['head'])), tail=list(map(str, before['tail']))))
        self.assertEqual(self.bones()[self.target]['role'], 'reviewed-jaw-label')
        self.assertIs(self.bones()[self.target]['deform'], False)
        self.assert_pending()

    def test_atomic_restoration_then_rename_keeps_pending_lineage(self):
        self.apply(self.remove(), self.restore(), self.edit({'id': 'left-jaw'}))
        self.assert_pending(renamed='left-jaw')

    def test_atomic_rename_then_removal_and_restoration_keeps_pending_lineage(self):
        self.apply(self.edit({'id': 'left-jaw'}), self.remove('left-jaw'),
                   self.restore(target='left-jaw'))
        self.assert_pending(renamed='left-jaw')

    def test_repeated_atomic_restoration_and_geometry_revert_keep_pending(self):
        tail = list(self.before_bones[self.target]['tail'])
        tail[2] -= .02
        self.apply(self.remove(), self.restore(tail=tail), self.remove(), self.restore())
        self.assertEqual(self.bones(), self.before_bones)
        self.assert_pending()

    def test_atomic_restoration_with_changed_anatomy_records_real_correction(self):
        tail = list(self.before_bones[self.target]['tail'])
        tail[2] -= .02
        self.apply(self.remove(), self.restore(tail=tail))
        self.assertEqual(self.bones()[self.target]['tail'], tail)
        self.assert_anatomically_corrected()

    def test_distinct_replacement_identity_is_new_anatomical_evidence(self):
        self.apply(self.remove(), self.restore(target='replacement-jaw'))
        self.assertNotIn(self.target, self.bones())
        self.assertEqual(self.rig.manifest['uncertainties'],
                         [u for u in self.before_issues if u['element'] != self.target])
        replacement = self.bones()['replacement-jaw']
        self.assertEqual(replacement['evidence']['confidence'], 1.)
        self.assertEqual(replacement['evidence']['source'], 'human-landmark')
        self.assertEqual(self.bones()[self.other], self.before_bones[self.other])

    def test_rename_into_removed_identity_uses_surviving_bones_lineage(self):
        self.apply(self.remove(), self.edit({'id': self.target}, target=self.other))
        expected = deepcopy([u for u in self.before_issues if u['element'] == self.other])
        for issue in expected:
            issue['element'] = self.target
        self.assertEqual(self.rig.manifest['uncertainties'], expected)
        self.assertEqual(self.bones()[self.target]['evidence'],
                         self.before_bones[self.other]['evidence'])
        self.assertNotIn(self.other, self.bones())

    def test_new_bone_renamed_into_removed_identity_does_not_inherit_evidence(self):
        self.apply(self.remove(), self.restore(target='replacement-jaw'),
                   self.edit({'id': self.target}, target='replacement-jaw'))
        self.assertEqual(self.bones()[self.target]['evidence']['confidence'], 1.)
        self.assertEqual(self.rig.manifest['uncertainties'],
                         [u for u in self.before_issues if u['element'] != self.target])

    def test_genuine_removal_cleans_anatomy_and_controller_facial_attachment_references(self):
        controllers = self.rig.manifest['controllers']
        chain = dict(schema_version=1, id='synthetic-jaw-chain',
                     bones=['elbow', 'gripper', self.target], solver='two-bone',
                     pole=[0, 0, 1], tolerance=1e-6, iterations=32)
        controllers['ik'].append(chain)
        controllers['constraints'].extend([
            dict(schema_version=1, id='synthetic-jaw-lock', bone=self.target, type='locked'),
            dict(schema_version=1, id='synthetic-jaw-copy', bone=self.other,
                 target=self.target, type='copy-position')])
        attachment = deepcopy(self.rig.manifest['attachments'][0])
        attachment.update(id='synthetic-jaw-socket', bone=self.target)
        self.rig.manifest['attachments'].append(attachment)
        self.rig.manifest['facial']['eyes'] = [self.target, self.other]
        self.rig.manifest['facial']['expressions']['blink']['bones'] = {
            self.target: {'rotation': [0, 0, 0, 1]}, self.other: {}}
        independent = self.save_independent_uncertainties(first=True)
        before = deepcopy(self.rig.manifest)
        self.apply(self.remove())
        self.assertNotIn(self.target, self.bones())
        self.assertEqual(self.rig.manifest['uncertainties'], independent +
                         [u for u in self.before_issues
                          if u['code'] == 'RIG_ANATOMY_UNCERTAIN' and u['element'] != self.target])
        self.assertEqual(self.rig.manifest['controllers']['ik'],
                         [c for c in before['controllers']['ik'] if c != chain])
        self.assertEqual(self.rig.manifest['controllers']['constraints'],
                         [c for c in before['controllers']['constraints']
                          if not c['id'].startswith('synthetic-jaw-')])
        self.assertEqual(self.rig.manifest['attachments'],
                         [a for a in before['attachments'] if a != attachment])
        self.assertEqual(self.rig.manifest['facial']['eyes'], [self.other])
        self.assertEqual(self.rig.manifest['facial']['expressions']['blink']['bones'],
                         {self.other: {}})

    def test_invalid_restoration_preserves_saved_rig_and_revision_bytes(self):
        before = deepcopy(self.rig.manifest)
        protected = {p: sha256(p) for p in self.rig.root.rglob('*')
                     if p.is_file() and p.name not in ('job.json', '.rig.lock')}
        invalid = (([self.remove(), self.restore(tail=self.before_bones[self.target]['head'])],
                    'RIG_ZERO_VECTOR'),
                   ([self.remove(), self.restore(), self.restore()], 'RIG_BONE_EDIT'))
        for edits, code in invalid:
            with self.subTest(code=code):
                with self.assertRaises(RigError) as caught:
                    mutate(self.rig.root, 'correct', {'correction': self.correction(edits)})
                self.assertEqual(caught.exception.report['code'], code)
                self.assertEqual(load_rig(self.rig.root).manifest, before)
                self.assertEqual({p: sha256(p) for p in self.rig.root.rglob('*')
                                  if p.is_file() and p.name not in ('job.json', '.rig.lock')},
                                 protected)
                self.assertEqual(read_json(self.rig.root / 'job.json')['steps']['correct']['status'],
                                 'failed')

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

    def test_atomic_restoration_preserves_all_record_fields_domains_and_resolutions(self):
        anatomy = [dict(deepcopy(self.before_issues[0]), resolution=resolution)
                   for resolution in ('accepted', 'corrected')]
        self.rig.manifest['uncertainties'].extend(anatomy)
        self.save_independent_uncertainties(first=True)
        self.apply(self.remove(), self.restore())
        self.assert_pending()

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
