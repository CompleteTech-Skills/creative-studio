"""Actual native poses and boundary guards for the full Director narrative."""
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

import numpy as np

from director_assets import build_assets
from director_narrative import (ACTORS, FRAMES, FPS, SHOTS, GRIPS, HANDOFF,
    author_pose, foot_planted, key_transform, native_events, prepare_rigs,
    root_transform, world_pose)
from rig_attachments import socket_world
from rig_errors import RigError, sha256
from rig_math import transform_matrix
from rig_models import load_rig
from rig_skinning import evaluate_deformation
from rig_math import transform_points


class NarrativePoseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)/'assets'
        build_assets(cls.root)
        cls.source_hashes = {a: sha256(cls.root/('rig-'+a)/'rig.json') for a in ACTORS}
        cls.rigs = {a: load_rig(cls.root/('rig-'+a)) for a in ACTORS}
        prepare_rigs(cls.rigs)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_exact_five_shots_cover_720_half_open_frames(self):
        self.assertEqual([(start, end) for _, start, end in SHOTS],
                         [(0, 120), (120, 264), (264, 432), (432, 576), (576, 720)])
        self.assertEqual(sum(end-start for _, start, end in SHOTS), FRAMES)
        self.assertEqual(Fraction(FRAMES, FPS), 30)
        self.assertEqual(Fraction(FRAMES-1, FPS), Fraction(719, 24))

    def test_invalid_actor_frame_and_missing_native_gaze_are_typed(self):
        rig = self.rigs['aster']
        for value in (-1, 721, True, .5, '528'):
            with self.subTest(value=value), self.assertRaises(RigError):
                author_pose(rig, 'aster', value)
        with self.assertRaises(RigError):
            root_transform('third-robot', 0)
        with self.assertRaises(RigError):
            prepare_rigs({'aster': rig})
        with self.assertRaises(RigError):
            foot_planted(0, 'front')

    def test_actual_distinct_grips_are_reachable_in_world_space(self):
        sockets = {}
        key = transform_matrix(key_transform(HANDOFF))
        for actor, rig in self.rigs.items():
            pose, measured = author_pose(rig, actor, HANDOFF)
            self.assertTrue(measured['grasp_active'])
            self.assertLess(measured['grasp_residual_m'], .005)
            placed = world_pose(rig, pose, transform_matrix(root_transform(actor, HANDOFF)))
            side = 'left' if actor == 'aster' else 'right'
            sockets[actor] = socket_world(rig, placed, 'grip.'+side)
            expected = key @ transform_matrix(GRIPS[actor])
            np.testing.assert_allclose(sockets[actor], expected, atol=1e-7, rtol=0)
        self.assertAlmostEqual(float(np.linalg.norm(sockets['aster'][:3, 3]-sockets['moss'][:3, 3])), .18)

    def test_actual_gaze_blink_and_native_poses_preserve_original_sources(self):
        for actor, rig in self.rigs.items():
            first, _ = author_pose(rig, actor, 0)
            later, _ = author_pose(rig, actor, 624)
            self.assertNotEqual(first['bones']['eye.left'], later['bones']['eye.left'])
            self.assertTrue(any('blink' in weights for weights in first['morphs'].values()))
            self.assertEqual(sha256(self.root/('rig-'+actor)/'rig.json'), self.source_hashes[actor])

    def test_ownership_markers_are_distinct_from_controlled_hand_contact(self):
        giver = native_events('aster')
        receiver = native_events('moss')
        self.assertEqual([round(e['time']*FPS) for e in giver if e.get('value') == 'pickup-single-owner'], [360])
        self.assertEqual([round(e['time']*FPS) for e in giver if e.get('value') == 'ownership-transfer-to-moss'], [528])
        self.assertEqual([round(e['time']*FPS) for e in receiver if e.get('value') == 'ownership-transfer-from-aster'], [528])
        self.assertFalse(any(e['type'] == 'grasp' for e in receiver))
        self.assertEqual([round(e['time']*FPS) for e in giver if e.get('value') == 'giver-contact-end'], [541])
        self.assertTrue(any(e['type'] == 'foot_contact' for e in giver))
        self.assertTrue(any(e['type'] == 'foot_release' for e in giver))
        self.assertTrue(foot_planted(348, 'left'))
        self.assertTrue(foot_planted(348, 'right'))

    def test_native_boots_remain_on_floor_when_baked_ik_settles(self):
        for actor, rig in self.rigs.items():
            samples = []
            for frame in (347, 348):
                pose, _ = author_pose(rig, actor, frame)
                geometry = evaluate_deformation(rig, pose)
                world = transform_matrix(root_transform(actor, frame))
                boots = [transform_points(world, geometry['boot-'+side]['positions'])
                         for side in ('left', 'right')]
                for points in boots:
                    self.assertLess(abs(float(points[:, 1].min())), .002)
                samples.append(boots)
            for first, second in zip(*samples):
                self.assertLess(float(np.linalg.norm(first-second, axis=1).max()), .001)

    def test_discovery_gaze_has_no_single_frame_target_snap(self):
        for actor, rig in self.rigs.items():
            for frame in (264, 288, 312):
                first, _ = author_pose(rig, actor, frame-1)
                second, _ = author_pose(rig, actor, frame)
                for eye in ('head', 'eye.left', 'eye.right'):
                    a = np.asarray(first['bones'][eye]['rotation'])
                    b = np.asarray(second['bones'][eye]['rotation'])
                    delta = 2*np.arccos(min(1., abs(float(np.dot(a, b)))))
                    self.assertLess(float(delta), .05)


if __name__ == '__main__':
    unittest.main()
