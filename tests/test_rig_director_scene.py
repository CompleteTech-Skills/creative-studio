"""Actual native skinned geometry and pure timeline checks for Director #29."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from director_assets import build_assets
from director_scene import author_clip, scene_document, evaluate_frame, validate_visibility, _copy_assets
from rig_animation import evaluate_animation
from rig_models import load_rig


class DirectorSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        build_assets(cls.root/'assets')
        cls.rigs={actor:load_rig(cls.root/'assets'/('rig-'+actor)) for actor in ['aster','moss']}
        for actor,rig in cls.rigs.items():author_clip(rig,actor)
        cls.scene=scene_document()

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_nonsequential_actual_vertex_evaluation_is_deterministic(self):
        expected={frame:evaluate_frame(self.rigs,self.scene,frame) for frame in [0,60,72,119]}
        for frame in [119,0,72,60]:self.assertEqual(evaluate_frame(self.rigs,self.scene,frame),expected[frame])
        for actor in self.rigs:
            left=np.concatenate([np.asarray(v) for v in expected[0]['actors'][actor]['vertices'].values()])
            right=np.concatenate([np.asarray(v) for v in expected[119]['actors'][actor]['vertices'].values()])
            self.assertGreater(float(np.linalg.norm(right-left,axis=1).max()),.02)

    def test_exact_pickup_to_single_owner_and_camera_visibility(self):
        before=evaluate_frame(self.rigs,self.scene,71)
        pickup=evaluate_frame(self.rigs,self.scene,72)
        after=evaluate_frame(self.rigs,self.scene,96)
        self.assertIsNone(before['key_owner']);self.assertEqual(pickup['key_owner'],'aster')
        self.assertEqual(pickup['key_matrix'],pickup['actors']['aster']['bones']['hand.right'])
        self.assertEqual(after['key_matrix'],after['actors']['aster']['bones']['hand.right'])
        for frame in [0,59,60,72,119]:validate_visibility(evaluate_frame(self.rigs,self.scene,frame))
        self.assertEqual(before['camera'],'investigation')

    def test_shot_segments_match_continuous_native_motion(self):
        for actor,rig in self.rigs.items():
            for segment,offset in [('establish',0),('discovery',2.5)]:
                for time in [0.,.5,2.5]:
                    full=evaluate_animation(rig,actor+'-investigation',time+offset,contacts=False,constraints=False)
                    part=evaluate_animation(rig,actor+'-'+segment,time,contacts=False,constraints=False)
                    self.assertEqual(full['pose']['bones'],part['pose']['bones'])

    def test_bad_frame_and_source_links_rejected(self):
        for frame in [-1,121,True,.5]:
            with self.assertRaises(ValueError):evaluate_frame(self.rigs,self.scene,frame)
        link=self.root/'linked';link.symlink_to(self.root/'assets',target_is_directory=True)
        with self.assertRaises(ValueError):_copy_assets(link,self.root/'copy')

    def test_actual_asset_copy_preserves_source_identities_and_bytes(self):
        destination=self.root/'snapshot'
        _copy_assets(self.root/'assets',destination)
        expected={p.relative_to(self.root/'assets'):p.read_bytes() for p in (self.root/'assets').rglob('*')
                  if p.is_file() and p.name not in {'.rig-aster.rig.lock','.rig-moss.rig.lock'}}
        actual={p.relative_to(destination):p.read_bytes() for p in destination.rglob('*') if p.is_file()}
        self.assertEqual(actual,expected)
