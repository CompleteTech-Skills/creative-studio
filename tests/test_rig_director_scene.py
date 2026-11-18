"""Actual native skinned geometry and pure timeline checks for Director #29."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import numpy as np
from asset_library import AssetLibrary
from director_assets import build_assets
from director_scene import (author_clip, scene_document, evaluate_frame, validate_visibility,
                            validate_lab_clearance, _copy_assets, build_performance)
from rig_animation import evaluate_animation
from rig_import import load_character_model
from rig_errors import sha256
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
        np.testing.assert_allclose(pickup['key_matrix'],before['key_matrix'],atol=1e-12)
        pickup_offset=np.linalg.solve(pickup['actors']['aster']['bones']['hand.right'],pickup['key_matrix'])
        carry_offset=np.linalg.solve(after['actors']['aster']['bones']['hand.right'],after['key_matrix'])
        np.testing.assert_allclose(carry_offset,pickup_offset,atol=1e-12)
        self.assertGreater(after['key_matrix'][1][3]-pickup['key_matrix'][1][3],.15)
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
        producer = self.root/'assets'
        self.assertEqual(_copy_assets(producer, destination), sha256(producer/'assets.json'))
        self.assertEqual(self.inventory(destination), self.inventory(producer))
        report = json.loads((destination/'assets.json').read_bytes())
        library = AssetLibrary(destination/'library')
        for name, record in report['assets'].items():
            reference = record['reference']
            self.assertEqual(library.resolve(reference), json.loads((destination/(name+'-asset.json')).read_bytes()))
            self.assertEqual(library.read_file(reference, record['source']), (producer/record['source']).read_bytes())

    def test_fixture_clearance_rejects_original_bench_penetration(self):
        lab = load_character_model(self.root/'assets/laboratory.glb').geometry
        lab_bounds = {mesh.id: (mesh.positions.min(axis=0),mesh.positions.max(axis=0)) for mesh in lab.meshes}
        key = load_character_model(self.root/'assets/access-key.glb').geometry.meshes[0].positions
        triangles = {actor: {mesh.id: mesh.triangles for mesh in rig.geometry.meshes} for actor,rig in self.rigs.items()}
        supported = validate_lab_clearance(evaluate_frame(self.rigs,self.scene,0),triangles,lab_bounds,key)
        self.assertAlmostEqual(supported['key_tabletop_gap_m'],0.,places=6)
        previous_scene = scene_document()
        next(node for node in previous_scene['nodes'] if node['id']=='aster')['transform']['translation'] = [-.5,0.,-1.65]
        with self.assertRaisesRegex(ValueError,'overlaps a laboratory component'):
            validate_lab_clearance(evaluate_frame(self.rigs,previous_scene,0),triangles,lab_bounds,key)

    @staticmethod
    def inventory(root):
        return {p.relative_to(root): sha256(p) for p in root.rglob('*') if p.is_file()}

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'), 'Set HATCH_BLENDER to run the native preview acceptance.')
    def test_native_performance_preserves_producer_and_uses_derived_rigs(self):
        producer = self.root/'assets'
        expected = self.inventory(producer)
        output = self.root/'performance'
        receipt = build_performance(producer, output, blender=os.environ['HATCH_BLENDER'])
        self.assertEqual(self.inventory(producer), expected)
        self.assertEqual(self.inventory(output/'assets'), expected)
        self.assertEqual(receipt['source_assets_sha256'], expected[Path('assets.json')])
        self.assertEqual(receipt['native_sha256'], sha256(output/'laboratory.blend'))
        self.assertEqual(receipt['reopened']['armatures'], 2)
        self.assertLess(receipt['max_vertex_error_m'], .01)
        self.assertEqual(receipt['clearance']['evaluated_frames'], 121)
        self.assertGreater(receipt['clearance']['final_key_gap_m'], .15)
        self.assertLessEqual(receipt['clearance']['pickup_hand_gap_m'], .003)
        self.assertLessEqual(receipt['clearance']['maximum_hand_vertex_depth_m'], .001)
        self.assertEqual([receipt['visibility'][frame]['camera'] for frame in (59,60)], ['wide','investigation'])
        playback = receipt['playback']
        self.assertEqual(playback['project_sha256'], sha256(output/'scene-project/project.json'))
        self.assertEqual((playback['runtime_frames'], playback['authoring_terminal_frame']), (120,120))
        self.assertEqual(playback['out_of_order_frames'], [60,59])
        self.assertEqual(playback['material_pins'], 0)
        self.assertFalse(playback['render_evaluated'])
        sampled = {sample['frame']: sample for sample in playback['sampled']}
        self.assertEqual(set(sampled), {0,36,59,60,71,72,73,96,119})
        self.assertEqual((sampled[59]['shot'],sampled[59]['local_frame']), ('establish',59))
        self.assertEqual((sampled[60]['shot'],sampled[60]['local_frame']), ('discovery',0))
        for sample in sampled.values():
            self.assertEqual(sample['vertices'], 7660)
            self.assertLess(sample['max_actor_component_delta_m'], 1e-7)
            self.assertLess(sample['key_matrix_delta'], 1e-7)
            self.assertLess(sample['blender_camera_world_delta'], 1e-6)
            self.assertLess(sample['blender_projection_delta'], 1e-6)
        self.assertEqual(receipt['created']['material_slots'], receipt['reopened']['material_slots'])
        for actor in ('aster', 'moss'):
            original = load_rig(output/'assets'/('rig-'+actor))
            animated = load_rig(output/'actor-rigs'/('rig-'+actor))
            self.assertNotIn(actor+'-investigation', original.clips)
            self.assertIn(actor+'-investigation', animated.clips)
            self.assertEqual(animated.manifest['source']['sha256'], original.manifest['source']['sha256'])
        library = AssetLibrary(output/'assets/library')
        self.assertEqual(len(library.list_references()), 4)
