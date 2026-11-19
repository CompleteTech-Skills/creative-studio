"""Native correction and persistence of individually owned uncertainty observations."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from rig_corrections import apply_rig_corrections
from rig_demo import make_geometry
from rig_errors import timestamp
from rig_jobs import save_rig
from rig_models import load_rig, rig_signature
from rig_operations import mutate
from rig_test_helpers import fixture


class UncertaintyOccurrenceMutationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source_model = Path(directory.name) / 'fixture.glb'
        self.rig = fixture(directory.name, animated=False)
        self.source_bytes = self.source_model.read_bytes()
        self.source_record = deepcopy(self.rig.manifest['source'])
        _, _, landmarks, _ = make_geometry('robotic')
        for name in ('gripper', 'jaw.left', 'jaw.right'):
            landmarks.pop(name)
        before = self.history()
        result = mutate(self.rig.root, 'auto', dict(template='robotic', replace=True,
            landmarks=dict(source_sha256=self.source_record['sha256'], landmarks=landmarks)))
        self.assertEqual(result['status'], 'succeeded')
        self.reload(before)
        self.grip, self.jaw, right = deepcopy(self.rig.manifest['uncertainties'])
        self.assertEqual([r['element'] for r in (self.grip, self.jaw, right)],
                         ['gripper', 'jaw.left', 'jaw.right'])
        self.assertTrue(all(r['resolution'] == 'pending' for r in (self.grip, self.jaw, right)))
        # The fitted left jaw is untouched. The copy replacing the right-jaw
        # observation is an ordinary, independently authored occurrence.
        self.rig.manifest['uncertainties'][2] = deepcopy(self.jaw)
        self.save_and_reload()
        self.assertEqual(self.rig.manifest['uncertainties'], [self.grip, self.jaw, self.jaw])
        self.extra = dict(right, message='Synthetic independent right-jaw observation')

    def history(self):
        return {path: path.read_bytes()
                for directory in ('source', 'rig/revisions', 'rig/corrections')
                for path in (self.rig.root / directory).rglob('*') if path.is_file()}

    def reload(self, before):
        self.rig = load_rig(self.rig.root)
        self.assertEqual(self.rig.manifest['source'], self.source_record)
        self.assertEqual(self.source_model.read_bytes(), self.source_bytes)
        self.assertEqual((self.rig.root / self.source_record['path']).read_bytes(),
                         self.source_bytes)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def save_and_reload(self):
        before = self.history()
        save_rig(self.rig, operation='synthetic-observation-edit')
        self.reload(before)

    def correct(self, edits, *, direct=False):
        before = self.history()
        correction = dict(schema_version=1, kind='hatch-3d-rig-correction',
            rig_id=self.rig.manifest['id'], rig_version=self.rig.manifest['version'],
            expected_sha256=rig_signature(self.rig.manifest), author='Synthetic reviewer',
            reason='Check uncertainty ownership across native edits', created_at=timestamp(),
            edits=edits)
        if direct:
            apply_rig_corrections(self.rig, correction)
        else:
            result = mutate(self.rig.root, 'correct', dict(correction=correction))
            self.assertEqual(result['status'], 'succeeded')
        self.reload(before)

    def remove_left_jaw(self, *, direct=False):
        self.correct([dict(operation='bone-remove', target='jaw.left', value=None)],
                     direct=direct)
        self.assertNotIn('jaw.left', [b['id'] for b in self.rig.skeleton['bones']])

    def test_equal_count_save_never_transfers_ownership_to_surviving_copy(self):
        issues = self.rig.manifest['uncertainties']
        issues.pop(1)  # Discard the fitted occurrence. The opaque copy shifts left.
        issues.append(self.extra)
        self.save_and_reload()
        self.assertEqual(self.rig.manifest['uncertainties'], [self.grip, self.jaw, self.extra])
        self.remove_left_jaw()
        self.assertEqual(self.rig.manifest['uncertainties'], [self.grip, self.jaw, self.extra],
                         'The surviving opaque left-jaw observation must stay pending.')

    def test_direct_correction_preserves_opaque_copy_before_generic_save(self):
        self.rig.manifest['uncertainties'].pop(1)
        self.remove_left_jaw(direct=True)
        self.assertEqual(self.rig.manifest['uncertainties'], [self.grip, self.jaw],
                         'Direct correction must preserve the surviving opaque occurrence.')

    def test_reordered_observations_cannot_reacquire_ownership_by_value(self):
        issues = self.rig.manifest['uncertainties']
        issues.pop(1)
        issues.reverse()  # Move the opaque copy ahead of the distinct fitted gripper.
        issues.append(self.extra)
        self.save_and_reload()
        self.remove_left_jaw()
        self.assertEqual(self.rig.manifest['uncertainties'], [self.jaw, self.grip, self.extra])

    def test_unaffected_fitted_observation_follows_renamed_and_corrected_bone(self):
        issues = self.rig.manifest['uncertainties']
        issues.pop(1)
        issues.append(self.extra)
        self.save_and_reload()
        self.correct([dict(operation='bone-update', target='gripper',
                           value=dict(id='renamed-gripper'))])
        renamed = dict(self.grip, element='renamed-gripper')
        self.assertEqual(self.rig.manifest['uncertainties'], [renamed, self.jaw, self.extra])
        self.correct([dict(operation='bone-update', target='renamed-gripper',
                           value=dict(roll=.25))])
        self.assertEqual(self.rig.manifest['uncertainties'],
                         [dict(renamed, resolution='corrected'), self.jaw, self.extra])


if __name__ == '__main__':
    unittest.main()
