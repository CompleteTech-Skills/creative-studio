"""Actual persisted native component revisions; no production rendering claim."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from director_reliability import apply_camera_or_light_patch
from editor3d_repository import EditorRepository
from rig_errors import RigError
from scene3d_example import build_example


class ComponentRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        build_example(self.root/'source')
        self.repository = EditorRepository.create(self.root/'source', self.root/'repository')

    def patch(self, revision, digest, *, fov=.9, transaction='camera-change'):
        camera = deepcopy(next(node for node in self.repository.snapshot()['scene']['nodes'] if node['id']=='camera')['camera'])
        camera['vertical_fov'] = fov
        return apply_camera_or_light_patch(self.repository, revision, digest, 'camera', 'camera', camera, transaction)

    def test_patch_reopens_and_rolls_back_without_asset_review_or_source_changes(self):
        original = self.repository.snapshot()
        source = {p.relative_to(self.root/'source'): p.read_bytes() for p in (self.root/'source').rglob('*') if p.is_file()}
        receipt = self.patch(0, original['sha256'])
        self.assertEqual(receipt['affected_shots'], [dict(path='example-shot.json', id='example-shot',version='1.0.0',frame_range=dict(start=0,end=24))])
        self.assertTrue(receipt['asset_identities_preserved'])
        self.assertTrue(receipt['independent_review_records_preserved'])
        reopened = EditorRepository(self.root/'repository')
        self.assertEqual(next(n for n in reopened.snapshot()['scene']['nodes'] if n['id']=='camera')['camera']['vertical_fov'], .9)
        restored = reopened.undo(1, 'rollback-camera', expected_sha256=receipt['after']['sha256'])
        self.assertEqual(restored['scene'], original['scene'])
        self.assertEqual(restored['project'], original['project'])
        self.assertEqual({p.relative_to(self.root/'source'):p.read_bytes() for p in (self.root/'source').rglob('*') if p.is_file()}, source)

    def test_stale_and_invalid_patch_preserve_last_valid_project(self):
        before = self.repository.snapshot()
        self.patch(0, before['sha256'])
        valid = self.repository.snapshot()
        for revision,digest,fov in [(0,before['sha256'],1.),(1,'0'*64,1.),(1,valid['sha256'],-1.)]:
            with self.subTest(revision=revision,digest=digest,fov=fov),self.assertRaises(RigError):
                self.patch(revision,digest,fov=fov,transaction='invalid-'+str(fov)+digest[:2])
            self.assertEqual(EditorRepository(self.root/'repository').snapshot(), valid)
            self.assertFalse(self.repository.recovery())
