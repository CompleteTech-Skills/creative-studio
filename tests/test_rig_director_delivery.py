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
