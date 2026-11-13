"""Actual specialty fixture evidence; no production artwork acceptance."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from rig_demo import create_demo
from rig_errors import RigError, sha256
from rig_models import load_rig
from rig_acceptance_fixtures import garment_proof, tail_proof, grip_render_proof


class SpecialtyFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='rig-specialty-tests-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root=Path(cls.temporary.name)
        source=os.environ.get('HATCH_RIG_SPECIALTY_SOURCE_ROOT')
        if source:
            cls.humanoid=load_rig(Path(source)/'humanoid')
            cls.quadruped=load_rig(Path(source)/'quadruped')
        else:
            cls.humanoid,_=create_demo(cls.root/'humanoid','humanoid',fps=4)
            cls.quadruped,_=create_demo(cls.root/'quadruped','quadruped',fps=4)

    def destination(self,label):
        return self.root/label

    def test_separate_garment_has_nonrigid_geometry_and_exported_skin(self):
        source=sha256(self.humanoid.root/'rig.json')
        report=garment_proof(self.humanoid,self.destination('garment'))
        self.assertGreater(report['max_vertex_displacement_m'],.001)
        self.assertGreater(report['max_pair_distance_change_m'],.00001)
        derived=load_rig(report['derived_root'])
        self.assertIn(report['mesh'],derived.geometry.by_id())
        self.assertEqual(set(derived.geometry.by_id()),set(derived.weights))
        self.assertGreater(derived.manifest['geometry']['vertices'],self.humanoid.manifest['geometry']['vertices'])
        self.assertTrue(report['artifacts']['native_roundtrip']['ok'])
        self.assertFalse(report['artifacts']['native_roundtrip']['independent'])
        self.assertEqual(sha256(self.humanoid.root/'rig.json'),source)
        with self.assertRaises(RigError):garment_proof(self.humanoid,self.destination('garment'))

    def test_tail_actual_endpoint_seeded_motion_and_baked_export(self):
        source=sha256(self.quadruped.root/'rig.json')
        report=tail_proof(self.quadruped,self.destination('tail'))
        self.assertLessEqual(report['endpoint_error_m'],report['tolerance_m'])
        self.assertGreater(report['max_target_vertex_displacement_m'],.001)
        self.assertGreater(report['max_changed_seed_vertex_delta_m'],.000001)
        self.assertLessEqual(report['max_baked_sample_vertex_error_m'],1e-8)
        self.assertTrue(report['artifacts']['native_roundtrip']['ok'])
        self.assertEqual(sha256(self.quadruped.root/'rig.json'),source)

    @unittest.skipUnless(importlib.util.find_spec('rig_grips'),'reviewed cooperative authority is a separate integration dependency')
    def test_cooperative_grip_uses_final_bound_revision_and_actual_hands(self):
        source=sha256(self.humanoid.root/'rig.json')
        source_bones=self.humanoid.skeleton['bones']
        report=grip_render_proof(self.humanoid,self.destination('grip'))
        derived=load_rig(report['derived_root'])
        self.assertEqual(derived.skeleton['bones'][:-1],source_bones)
        anchor=derived.skeleton['bones'][-1]
        self.assertEqual(anchor['id'],report['prop_anchor'])
        self.assertTrue(anchor['deform'])
        self.assertEqual(anchor['parent'],self.humanoid.skeleton['root'])
        self.assertFalse(next(b for b in derived.skeleton['bones'] if b['id']==derived.skeleton['root'])['deform'])
        binding=derived.weights['proof-grip-bar']
        self.assertEqual(binding.joints,[anchor['id']])
        self.assertTrue((binding.weights[:,0]==1.).all())
        self.assertTrue(report['contact']['converged'])
        self.assertFalse(report['contact']['ownership_changed'])
        self.assertTrue(all(v>.05 for v in report['hand_vertex_displacement_m'].values()))
        self.assertFalse(report['render']['executed'])
        self.assertEqual(sha256(self.humanoid.root/'rig.json'),source)

    @unittest.skipUnless(os.environ.get('HATCH_RIG_SPECIALTY_RENDER_TEST')=='1' and importlib.util.find_spec('rig_grips'),
                         'actual render proof requires explicit native render lane and cooperative dependency')
    def test_actual_rendered_prop_diff_is_visible_at_solved_hand_anchors(self):
        report=grip_render_proof(self.humanoid,self.destination('grip-render'),render=True)
        self.assertTrue(report['contact']['converged'])
        self.assertGreaterEqual(report['render']['changed_pixels'],4)
        frame=report['render']['frame']
        self.assertEqual(sha256(frame['path']),frame['sha256'])
        self.assertEqual(report['visual_review'],'pending')


if __name__=='__main__':unittest.main()
