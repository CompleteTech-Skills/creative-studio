"""Exact retained Director source revision proof; no image/render acceptance."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest

from director_reliability import apply_camera_or_light_patch
from editor3d_repository import EditorRepository
from rig_errors import atomic_json, RigError


@unittest.skipUnless(os.environ.get('HATCH_DIRECTOR31_SCENE_PROJECT'), 'Requires retained verified Director MVP source')
class MvpProjectRevisionTests(unittest.TestCase):
    def test_actual_robot_source_identity_review_reopen_stale_revision_and_rollback(self):
        source = Path(os.environ['HATCH_DIRECTOR31_SCENE_PROJECT'])
        temporary = None if os.environ.get('HATCH_DIRECTOR31_PROJECT_EVIDENCE') else tempfile.TemporaryDirectory()
        self.addCleanup(lambda:temporary.cleanup() if temporary else None)
        root = Path(os.environ.get('HATCH_DIRECTOR31_PROJECT_EVIDENCE') or temporary.name)
        root.mkdir(parents=True,exist_ok=True)
        repository = EditorRepository.create(source,root/'repository')
        before = repository.snapshot()
        actors = {doc['id']:doc for doc in before['documents'].values() if doc.get('kind')=='scene3d-asset' and doc['id'] in ('aster','moss')}
        self.assertEqual(set(actors),{'aster','moss'})
        camera = deepcopy(next(n for n in before['scene']['nodes'] if n['id']=='wide')['camera'])
        camera['vertical_fov'] = .95
        receipt = apply_camera_or_light_patch(repository,before['revision'],before['sha256'],'wide','camera',camera,'mvp-camera-revision')
        self.assertEqual([item['id'] for item in receipt['affected_shots']],['establish'])
        self.assertEqual(receipt['affected_shots'][0]['frame_range'],dict(start=0,end=60))
        valid = repository.snapshot()
        with self.assertRaises(RigError):
            apply_camera_or_light_patch(repository,0,before['sha256'],'wide','camera',camera,'stale-mvp-camera')
        self.assertEqual(repository.snapshot(),valid)
        after_actors = {doc['id']:doc for doc in EditorRepository(repository.root).snapshot()['documents'].values() if doc.get('kind')=='scene3d-asset' and doc['id'] in ('aster','moss')}
        self.assertEqual(actors,after_actors)
        rolled_back = repository.undo(valid['revision'],'rollback-mvp-camera',expected_sha256=valid['sha256'])
        self.assertEqual(rolled_back['scene'],before['scene'])
        self.assertEqual(rolled_back['project'],before['project'])
        source_refs = {actor:dict(source=value['source'],skeleton=value['skeleton'],animations=value['animations'],review=value['review']) for actor,value in actors.items()}
        atomic_json(root/'mvp-project-revision-proof.json',dict(schema_version=1,kind='director-mvp-project-reliability',
            source_scene_project=str(source),source_identities=source_refs,patch=receipt,
            rollback=dict(revision=rolled_back['revision'],sha256=rolled_back['sha256']),
            stale_rejected=True,visual_review='not-performed'))
