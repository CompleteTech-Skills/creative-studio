"""Native geometry sampling tests; no rendering or aesthetic approval inferred."""
from pathlib import Path
import tempfile
import unittest

from director_assets import build_assets
from director_delivery import verify_performance_motion, verify_no_motion_negative, verify_shot_clips
from director_scene import author_clip, scene_document
from rig_errors import atomic_json
from rig_jobs import save_rig
from rig_models import load_rig


class DirectorDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / 'performance'
        cls.root.mkdir()
        build_assets(cls.root / 'assets')
        atomic_json(cls.root / 'scene.json', scene_document())
        for actor in ('aster', 'moss'):
            rig = load_rig(cls.root / 'assets' / ('rig-' + actor))
            author_clip(rig, actor)
            save_rig(rig, operation='director-motion-test')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_native_geometry_moves_for_both_robots_and_prop(self):
        result = verify_performance_motion(self.root)
        self.assertEqual(set(result['actors']), {'aster', 'moss'})
        self.assertTrue(all(x['maximum_vertex_displacement_m'] > .02 for x in result['actors'].values()))
        self.assertGreater(result['key_displacement_m'], .02)
        self.assertEqual(result['visual_review'], 'not-performed')

    def test_valid_static_native_animation_is_rejected_without_mutation(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = verify_no_motion_negative(self.root)
        self.assertIn('articulation is absent', result['expected_rejection']['message'])
        self.assertFalse(result['source_mutated'])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_rendered_static_shot_cannot_inherit_moving_full_clip(self):
        from copy import deepcopy
        from rig_animation import clip_from_poses
        from rig_skeleton import neutral_pose
        rigs = {actor: load_rig(self.root / 'assets' / ('rig-' + actor)) for actor in ('aster', 'moss')}
        rig = rigs['aster']
        clip = rig.clips['aster-discovery']
        rig.clips['aster-discovery'] = clip_from_poses(rig, 'aster-discovery', [0., 2.5],
            [neutral_pose(rig.skeleton), neutral_pose(rig.skeleton)], fps=24, provenance=deepcopy(clip['provenance']))
        with self.assertRaisesRegex(ValueError, 'Rendered shot clip differs'):
            verify_shot_clips(rigs)


class CompletedShotCheckpointTests(unittest.TestCase):
    def fixture(self, root, start=0):
        from PIL import Image
        from rig_errors import sha256
        images = []
        for frame, color in [(start, 'red'), (start+1, 'blue')]:
            path = root / f'{frame:06d}.png'
            Image.new('RGB', (2, 2), color).save(path)
            images.append(dict(frame=frame, file=dict(path='frames/'+path.name,
                sha256=sha256(path), bytes=path.stat().st_size)))
        backend = {'id': 'trusted-unit-fixture'}
        admission = dict(backend=backend, images=[dict(frame=item['frame'], **item['file']) for item in images])
        return dict(backend=backend, images=images), admission

    def test_valid_png_and_rewritten_result_cannot_replace_external_checkpoint(self):
        import shutil
        from PIL import Image
        from rig_errors import sha256
        from director_delivery import verify_completed_shot_checkpoint
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, admission = self.fixture(root)
            verify_completed_shot_checkpoint(result, {'start': 0, 'end': 2}, admission, admission['backend'])
            shutil.copyfile(root/'000001.png', root/'000000.png')
            with Image.open(root/'000000.png') as actual:
                actual.verify()
            result['images'][0]['file'].update(sha256=sha256(root/'000000.png'), bytes=(root/'000000.png').stat().st_size)
            with self.assertRaisesRegex(ValueError, 'trusted checkpoint'):
                verify_completed_shot_checkpoint(result, {'start': 0, 'end': 2}, admission, admission['backend'])

    def test_a_later_completed_shot_needs_its_own_checkpoint_coverage(self):
        from director_delivery import verify_completed_shot_checkpoint
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, admission = self.fixture(root)
            later, later_admission = self.fixture(root, 60)
            with self.assertRaisesRegex(ValueError, 'checkpoint coverage'):
                verify_completed_shot_checkpoint(later, {'start': 60, 'end': 62}, admission, admission['backend'])
            admission['images'].extend(later_admission['images'])
            verify_completed_shot_checkpoint(later, {'start': 60, 'end': 62}, admission, admission['backend'])

    def test_reused_result_cannot_claim_a_different_native_backend(self):
        from director_delivery import verify_completed_shot_checkpoint
        with tempfile.TemporaryDirectory() as temporary:
            result, admission = self.fixture(Path(temporary))
            result['backend'] = {'id': 'substituted-backend'}
            with self.assertRaisesRegex(ValueError, 'admitted backend'):
                verify_completed_shot_checkpoint(result, {'start': 0, 'end': 2}, admission, admission['backend'])
