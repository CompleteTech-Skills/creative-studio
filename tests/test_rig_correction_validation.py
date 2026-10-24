"""Correction admission on real saved rigs, distinct from animation readiness."""
from copy import deepcopy
import tempfile
import unittest

from rig_test_helpers import fixture
from rig_corrections import apply_rig_corrections
from rig_errors import RigError, read_json, sha256, timestamp
from rig_models import load_rig, rig_signature
from rig_operations import mutate
from rig_ik import validate_controllers


class RigCorrectionValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name)

    def correction(self, edits):
        rig = self.rig
        return dict(schema_version=1, kind='hatch-3d-rig-correction',
                    rig_id=rig.manifest['id'], rig_version=rig.manifest['version'],
                    expected_sha256=rig_signature(rig.manifest), author='unit fixture',
                    reason='Controller admission regression', created_at=timestamp(), edits=edits)

    def protected_files(self):
        root = self.rig.root
        return {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob('*')
                if p.is_file() and p.relative_to(root).as_posix() not in ('job.json', '.rig.lock')}

    def invalid_edits(self):
        original = self.rig.manifest['controllers']['ik'][0]
        for fields, code in (({'bones': ['b0', 'missing-bone', 'b2']}, 'RIG_IK_CHAIN'),
                             ({'orientation': [0, 0, 0, 2]}, 'RIG_QUATERNION'),
                             ({'bones': ['b0', 'b2', 'b1']}, 'RIG_IK_CHAIN')):
            chain = deepcopy(original)
            chain.update(fields)
            yield [dict(operation='ik', target='arm', value=chain)], code
        yield [dict(operation='bone-update', target='b2', value={'parent': 'b0'})], 'RIG_IK_CHAIN'
        # Positive authored bone length, but zero evaluated joint-origin separation.
        yield [dict(operation='bone-update', target='b1', value={'head': [0, 0, 0]})], 'RIG_IK_LENGTH'

    def assert_rejected_without_revision(self, edits, code, *, dispatch=False, persist=True):
        files = self.protected_files()
        manifest = deepcopy(self.rig.manifest)
        weights = {key: value.weights.copy() for key, value in self.rig.weights.items()}
        clips = deepcopy(self.rig.clips)
        with self.assertRaises(RigError) as caught:
            if dispatch:
                mutate(self.rig.root, 'correct', {'correction': self.correction(edits)})
            else:
                apply_rig_corrections(self.rig, self.correction(edits), persist=persist)
        self.assertEqual(caught.exception.report['code'], code)
        self.assertEqual(files, self.protected_files())
        self.assertEqual(manifest, self.rig.manifest)
        self.assertEqual(clips, self.rig.clips)
        for key, value in weights.items():
            self.assertTrue((self.rig.weights[key].weights == value).all())
        self.assertEqual(manifest, load_rig(self.rig.root).manifest)
        if dispatch:
            step = read_json(self.rig.root/'job.json')['steps']['correct']
            self.assertEqual(step['status'], 'failed')
            self.assertIsNone(step['output_sha256'])

    def test_invalid_controller_corrections_preserve_saved_and_caller_state(self):
        for edits, code in self.invalid_edits():
            for persist in (False, True):
                with self.subTest(edits=edits, persist=persist):
                    self.assert_rejected_without_revision(edits, code, persist=persist)

    def test_real_dispatch_rejects_invalid_controllers_before_revision_writes(self):
        for edits, code in self.invalid_edits():
            with self.subTest(edits=edits):
                self.assert_rejected_without_revision(edits, code, dispatch=True)

    def test_valid_ik_edit_persists_without_discarding_skin_or_animation(self):
        chain = deepcopy(self.rig.manifest['controllers']['ik'][0])
        chain.update(pole=[0, 0, -1], orientation=[0, 0, 0, 1])
        result = mutate(self.rig.root, 'correct', {'correction': self.correction(
            [dict(operation='ik', target='arm', value=chain)])})
        updated = load_rig(self.rig.root)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(updated.manifest['controllers']['ik'], [chain])
        self.assertTrue(updated.weights)
        self.assertTrue(updated.clips)
        self.assertTrue(list((updated.root/'rig/corrections').glob('*.json')))

    def test_rename_and_unbound_skeleton_corrections_remain_valid(self):
        edits = [dict(operation='bone-update', target='b1', value={'id': 'elbow', 'head': [.1, 1, 0]})]
        updated, report = apply_rig_corrections(self.rig, self.correction(edits))
        validate_controllers(updated.skeleton, updated.manifest['controllers'])
        self.assertEqual(updated.manifest['controllers']['ik'][0]['bones'], ['b0', 'elbow', 'b2'])
        self.assertFalse(updated.weights)
        self.assertFalse(updated.clips)
        self.assertIn('skins', report['invalidated'])
        self.assertIn('animations', report['invalidated'])
        self.assertEqual(load_rig(updated.root).manifest, updated.manifest)
        self.rig = updated
        # Subsequent correction of this intentionally unbound skeleton is admitted.
        updated, _ = apply_rig_corrections(updated, self.correction(
            [dict(operation='bone-update', target='elbow', value={'head': [.2, 1, 0]})]))
        self.assertFalse(updated.weights)
        self.assertFalse(updated.clips)
        self.assertEqual(load_rig(updated.root).manifest, updated.manifest)
