from copy import deepcopy
import math
import tempfile
import unittest
import numpy as np
from rig_test_helpers import fixture
from rig_animation import (evaluate_track,evaluate_animation,validate_animation,blend_poses,crossfade,bake_clip,reduce_keys,clip_from_poses)
from rig_skeleton import evaluate_fk,neutral_pose,rotate_bone
from rig_math import axis_angle,qangle
from rig_facial import apply_expressions
from rig_retarget import build_retarget_profile,retarget_animation
from rig_errors import RigError
from rig_skinning import evaluate_deformation


class RigAnimationTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.rig=fixture(self.temp.name)
    def test_step_and_linear_boundary_values(self):
        track=dict(times=[0.,1.],values=[[0,0,0],[2,4,6]],path='translation',interpolation='STEP')
        np.testing.assert_allclose(evaluate_track(track,.999),[0,0,0]);np.testing.assert_allclose(evaluate_track(track,1),[2,4,6])
        track['interpolation']='LINEAR';np.testing.assert_allclose(evaluate_track(track,.25),[.5,1,1.5])
    def test_cubic_hermite_uses_tangents_and_quaternion_normalization(self):
        track=dict(times=[0.,2.],values=[[0,0,0],[2,0,0]],in_tangents=[[0,0,0],[0,0,0]],out_tangents=[[2,0,0],[0,0,0]],path='translation',interpolation='CUBICSPLINE')
        np.testing.assert_allclose(evaluate_track(track,1),[1.5,0,0])
        track.update(path='rotation',values=[[0,0,0,1],[0,0,1,0]],in_tangents=[[0]*4]*2,out_tangents=[[0]*4]*2)
        self.assertAlmostEqual(np.linalg.norm(evaluate_track(track,1)),1)
    def test_slerp_changes_real_pose_and_duplicate_channels_fail(self):
        r=self.rig;pose=evaluate_animation(r,'bend',.25)['pose'];self.assertAlmostEqual(qangle(pose['bones']['b1']['rotation'],[0,0,0,1]),.4)
        bad=deepcopy(r.clips['bend']);bad['tracks'].append(deepcopy(bad['tracks'][0]))
        with self.assertRaises(RigError):validate_animation(r,bad)
    def test_unsorted_keys_and_missing_targets_fail(self):
        for change in (lambda c:c['tracks'][0].update(times=[0,.5,.4]),lambda c:c['tracks'][0].update(target='absent')):
            clip=deepcopy(self.rig.clips['bend']);change(clip)
            with self.assertRaises(RigError):validate_animation(self.rig,clip)
    def test_masked_additive_layers_and_expression_reset(self):
        r=self.rig;base=neutral_pose(r.skeleton);overlay=rotate_bone(r.skeleton,base,'b1',axis_angle([0,0,1],1.))
        self.assertNotIn('b1',blend_poses(r,base,overlay,.5,mask=['b2'])['bones'])
        result=blend_poses(r,base,overlay,.5,additive=True);self.assertAlmostEqual(qangle(result['bones']['b1']['rotation'],[0,0,0,1]),.5)
        facial=apply_expressions(r,base,{'blink':.7});self.assertEqual(facial['morphs']['strip']['blink'],.7)
        reset=apply_expressions(r,facial,{});self.assertEqual(reset['morphs'],{})
    def test_embedded_root_motion_accumulates_and_transition_does_not_double_it(self):
        r=self.rig;a=neutral_pose(r.skeleton);b=neutral_pose(r.skeleton);b['bones']['b0']={'translation':[0,0,1]}
        clip=clip_from_poses(r,'travel',[0,1],[a,b],loop=True);r.clips['travel']=clip
        self.assertAlmostEqual(evaluate_animation(r,clip,2.25,loop=True)['pose']['bones']['b0']['translation'][2],2.25)
        p=crossfade(r,clip,clip,.7,.2,.5);self.assertAlmostEqual(p['bones']['b0']['translation'][2],.7)
    def test_looped_contacts_follow_only_embedded_root_motion(self):
        r=self.rig;a=neutral_pose(r.skeleton);b=neutral_pose(r.skeleton)
        b['bones']['b0']={'translation':[.5,0.,0.]}
        clip=clip_from_poses(r,'planted-travel',[0.,1.],[a,b],loop=True)
        clip['contacts']=[dict(schema_version=1,id='plant',chain='arm',start=0.,end=1.,
                               target=[0.,1.5,0.],kind='foot')]
        for mode in ('in-place','embedded'):
            clip['root_motion']=mode
            first=evaluate_animation(r,clip,.25,loop=True)
            later=evaluate_animation(r,clip,2.25,loop=True)
            expected=np.array([1.,0.,0.]) if mode=='embedded' else np.zeros(3)
            for result,offset in ((first,np.zeros(3)),(later,expected)):
                contact=result['contacts'][0]
                np.testing.assert_allclose(contact['target'],np.array([0.,1.5,0.])+offset,atol=1e-12)
                np.testing.assert_allclose(evaluate_fk(r.skeleton,result['pose'])['b2'][:3,3],
                                           contact['target'],atol=1e-12)
                self.assertTrue(contact['converged'])
            before=evaluate_deformation(r,first['pose'])['strip']['positions']
            after=evaluate_deformation(r,later['pose'])['strip']['positions']
            if mode=='in-place':
                np.testing.assert_allclose(after,before,atol=1e-12)
            else:
                # The authored world-space pole stays fixed, so embedded
                # translation may change limb bend direction as well.
                self.assertGreater(np.max(np.linalg.norm(after-before,axis=1)),.5)
                self.assertTrue(np.isfinite(after).all())
        # A non-looping evaluation clamps at the end and never shifts the plant.
        final=evaluate_animation(r,clip,2.25,loop=False)
        np.testing.assert_allclose(final['contacts'][0]['target'],[0.,1.5,0.],atol=1e-12)

    def test_unconstrained_step_survives_baking(self):
        clip=deepcopy(self.rig.clips['bend']);clip['tracks'][0]['interpolation']='STEP'
        self.assertEqual(bake_clip(self.rig,clip),clip)
    def test_key_reduction_preserves_midpoint_cue(self):
        reduced=reduce_keys(self.rig.clips['bend'],tolerance=.001);self.assertEqual(len(reduced['tracks'][0]['times']),3)
    def test_retarget_requires_matching_identity_and_archetype(self):
        target=fixture(self.temp.name,name='target');profile=build_retarget_profile(self.rig,target)
        result=retarget_animation(self.rig,target,'bend',profile=profile);self.assertGreater(len(result['clip']['tracks']),0)
        target.manifest['archetype']='quadruped'
        with self.assertRaises(RigError):build_retarget_profile(self.rig,target)
        profile['source_rig']='wrong'
        with self.assertRaises(RigError):retarget_animation(self.rig,target,'bend',profile=profile)
