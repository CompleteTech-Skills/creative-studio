"""Public correction regressions for historical anatomy uncertainty ownership.

All geometry is synthetic. ``historical_format`` explicitly simulates an old
writer's saved record by restoring its pre-rename element spelling through
save_rig; it does not execute that old writer. The separate cross-version
reproduction uses the unmodified review486-74b1-integrity-upgrade.py against
929643842be2d4abc2554080c83e4df0683c9a89 and then the current public dispatcher.
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


class RigLegacyUncertaintyTests(unittest.TestCase):
    old_name = 'jaw.left'
    renamed = 'left-renamed'
    other = 'jaw.right'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rig = fixture(temporary.name, animated=False)
        _, _, landmarks, _ = make_geometry('robotic')
        # The missing left landmark produces geometry-fit uncertainty just as
        # in the genuine old-writer reproduction. The right record is a known
        # owner with distinguishable authored evidence for positive controls.
        landmarks.pop(self.old_name)
        landmarks[self.other].update(source='human-landmark', confidence=.5,
            reference='Synthetic right-jaw landmark for ownership controls')
        result = mutate(self.rig.root, 'auto', dict(template='robotic', replace=True,
            landmarks=dict(source_sha256=self.rig.manifest['source']['sha256'],
                           landmarks=landmarks)))
        self.assertEqual(result['status'], 'succeeded')
        self.rig = load_rig(self.rig.root)
        self.source_record = deepcopy(self.rig.manifest['source'])
        self.source_bytes = (self.rig.root / self.source_record['path']).read_bytes()
        self.initial = deepcopy(self.rig.manifest['uncertainties'])
        self.assertEqual([u['element'] for u in self.initial], [self.old_name, self.other])
        self.assertTrue(all(u['resolution'] == 'pending' for u in self.initial))

    def update(self, target, **value):
        return dict(operation='bone-update', target=target, value=value)

    def remove(self, target):
        return dict(operation='bone-remove', target=target, value=None)

    def apply(self, *edits):
        retained = {path: path.read_bytes()
                    for directory in ('rig/revisions', 'rig/corrections')
                    for path in (self.rig.root / directory).glob('*.json')}
        correction = dict(schema_version=1, kind='hatch-3d-rig-correction',
            rig_id=self.rig.manifest['id'], rig_version=self.rig.manifest['version'],
            expected_sha256=rig_signature(self.rig.manifest),
            author='synthetic legacy uncertainty test',
            reason='Verify historical evidence and current bone ownership',
            created_at=timestamp(), edits=list(edits))
        result = mutate(self.rig.root, 'correct', dict(correction=correction))
        self.assertEqual(result['status'], 'succeeded')
        self.rig = load_rig(self.rig.root)
        self.assertEqual(read_json(self.rig.root / 'rig.json'), self.rig.manifest)
        self.assertEqual(read_json(self.rig.root / 'job.json')['steps']['correct']['status'],
                         'succeeded')
        self.assertEqual(self.rig.manifest['source'], self.source_record)
        self.assertEqual((self.rig.root / self.source_record['path']).read_bytes(),
                         self.source_bytes)
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)

    def historical_format(self):
        """Simulate historical bytes; genuine old execution is separate evidence."""
        self.apply(self.update(self.old_name, id=self.renamed))
        self.assertEqual(self.rig.manifest['uncertainties'][0],
                         dict(self.initial[0], element=self.renamed))
        self.rig.manifest['uncertainties'][0] = deepcopy(self.initial[0])
        save_rig(self.rig, operation='synthetic-historical-uncertainty-format-fixture')
        self.rig = load_rig(self.rig.root)
        self.assertNotIn(self.old_name, [b['id'] for b in self.rig.skeleton['bones']])
        self.assertEqual(self.rig.manifest['uncertainties'], self.initial)

    def test_unrelated_role_edit_preserves_unowned_historical_record(self):
        self.historical_format()
        self.apply(self.update(self.other, role='right-jaw-reviewed-label'))
        # Whole-record equality includes message, original evidence, spelling,
        # pending resolution and ordering; a role label supplies no anatomy.
        self.assertEqual(self.rig.manifest['uncertainties'], self.initial)

    def test_known_bone_removal_preserves_unowned_historical_record(self):
        self.historical_format()
        self.apply(self.remove(self.other))
        self.assertNotIn(self.other, [b['id'] for b in self.rig.skeleton['bones']])
        self.assertEqual(self.rig.manifest['uncertainties'], self.initial[:1])

    def test_known_owner_rename_preserves_pending_evidence(self):
        self.apply(self.update(self.old_name, id=self.renamed))
        expected = [dict(self.initial[0], element=self.renamed), self.initial[1]]
        self.assertEqual(self.rig.manifest['uncertainties'], expected)
        self.apply(self.update(self.other, role='right-jaw-reviewed-label'))
        self.assertEqual(self.rig.manifest['uncertainties'], expected)

    def test_known_owner_rename_and_name_reuse_track_distinct_bones(self):
        self.apply(self.update(self.old_name, id=self.renamed),
                   self.update(self.other, id=self.old_name))
        expected = [dict(self.initial[0], element=self.renamed),
                    dict(self.initial[1], element=self.old_name)]
        self.assertEqual(self.rig.manifest['uncertainties'], expected)
        # Removing the bone that reuses the old spelling must remove its own
        # issue, while the original bone retains its original pending evidence.
        self.apply(self.remove(self.old_name))
        self.assertEqual(self.rig.manifest['uncertainties'], expected[:1])
        self.assertIn(self.renamed, [b['id'] for b in self.rig.skeleton['bones']])


if __name__ == '__main__':
    unittest.main()
