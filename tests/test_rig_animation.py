from copy import deepcopy
import math
import tempfile
import unittest
import numpy as np
from rig_test_helpers import fixture
from rig_animation import (evaluate_track,evaluate_animation,validate_animation,blend_poses,crossfade,bake_clip,reduce_keys,clip_from_poses)
from rig_skeleton import neutral_pose,rotate_bone,evaluate_fk
from rig_math import decompose
from rig_math import axis_angle,qangle
from rig_facial import apply_expressions
from rig_retarget import build_retarget_profile,retarget_animation,retarget_pose
from rig_skinning import evaluate_deformation
from rig_errors import RigError


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

    def test_identity_retarget_preserves_authored_contact_orientation(self):
        source=self.rig;target=fixture(self.temp.name,name='contact-target')
        desired=axis_angle([0,1,0],.6).tolist()
        contact=dict(schema_version=1,id='hold',chain='arm',start=0.,end=1.,
                     target=[0.,2.,0.],orientation=desired,kind='hand')
        clip=deepcopy(source.clips['neutral']);clip['contacts']=[contact]
        result=retarget_animation(source,target,clip)['clip']
        self.assertLess(qangle(result['contacts'][0]['orientation'],desired),1e-8)
        before=evaluate_animation(source,clip,.5)['pose']
        after=evaluate_animation(target,result,.5)['pose']
        a=decompose(evaluate_fk(source.skeleton,before)['b2'])['rotation']
        b=decompose(evaluate_fk(target.skeleton,after)['b2'])['rotation']
        self.assertLess(qangle(a,b),1e-8)
        source_mesh=evaluate_deformation(source,before)['strip']['positions']
        target_mesh=evaluate_deformation(target,after)['strip']['positions']
        np.testing.assert_allclose(target_mesh,source_mesh,atol=1e-8)
        del clip['contacts'][0]['orientation']
        positional=retarget_animation(source,target,clip)['clip']
        self.assertNotIn('orientation',positional['contacts'][0])

    def test_contact_reports_unsatisfied_orientation_after_limits(self):
        rig=self.rig
        rig.manifest['controllers']['constraints']=[dict(schema_version=1,id='hand-limit',
            bone='b2',type='hinge',axis=[1,0,0],minimum=0.,maximum=.2)]
        clip=deepcopy(rig.clips['neutral'])
        clip['contacts']=[dict(schema_version=1,id='hold',chain='arm',start=0.,end=1.,
            target=[0.,2.,0.],orientation=axis_angle([1,0,0],1.2).tolist(),kind='hand')]
        report=evaluate_animation(rig,clip,.5)['contacts'][0]
        self.assertLess(report['residual_m'],1e-8)
        self.assertAlmostEqual(report['orientation_residual_rad'],1.)
        self.assertFalse(report['converged'])

    def test_validation_rejects_current_unsatisfied_contact_orientation(self):
        from rig_validation import validate_rig
        rig=self.rig
        rig.manifest['controllers']['constraints']=[dict(schema_version=1,id='hand-limit',
            bone='b2',type='hinge',axis=[1,0,0],minimum=0.,maximum=.2)]
        clip=deepcopy(rig.clips['neutral'])
        clip['contacts']=[dict(schema_version=1,id='hold',chain='arm',start=0.,end=1.,
            target=[0.,2.,0.],orientation=axis_angle([1,0,0],1.2).tolist(),kind='hand')]
        rig.clips={'neutral':clip}
        report=validate_rig(rig)
        self.assertFalse(report['ok'])
        checks={check['name']:check for check in report['checks']}
        self.assertEqual(checks['contacts']['status'],'passed')
        self.assertEqual(checks['contact-orientations']['status'],'failed')
        self.assertAlmostEqual(report['metrics']['max_contact_orientation_error_rad'],1.)
        self.assertEqual(report['metrics']['contact_orientation_tolerance_rad'],1e-5)

    def test_validation_accepts_reached_antipodal_orientation_and_position_only_contacts(self):
        from rig_validation import validate_rig, contact_metrics
        rig=self.rig
        rig.manifest['controllers']['constraints']=[dict(schema_version=1,id='hand-limit',
            bone='b2',type='hinge',axis=[1,0,0],minimum=0.,maximum=.2)]
        clip=deepcopy(rig.clips['neutral'])
        clip['contacts']=[dict(schema_version=1,id='hold',chain='arm',start=0.,end=1.,
            target=[0.,2.,0.],orientation=(-axis_angle([1,0,0],.15)).tolist(),kind='hand')]
        rig.clips={'neutral':clip}
        report=validate_rig(rig)
        self.assertTrue(report['ok'])
        self.assertLess(report['metrics']['max_contact_orientation_error_rad'],1e-7)
        self.assertEqual(contact_metrics(rig,clip)['oriented_contacts'],1)
        del clip['contacts'][0]['orientation']
        report=validate_rig(rig)
        self.assertTrue(report['ok'])
        self.assertNotIn('contact-orientations',{check['name'] for check in report['checks']})
        self.assertNotIn('max_contact_orientation_error_rad',report['metrics'])
        self.assertEqual(contact_metrics(rig,clip)['oriented_contacts'],0)

    def test_retarget_keeps_independent_same_named_morphs_on_their_meshes(self):
        source=self.rig;target=fixture(self.temp.name,name='morph-target')
        for rig in (source,target):
            other=deepcopy(rig.geometry.meshes[0]);other.id='strip-right'
            rig.geometry.meshes.append(other)
        pose=neutral_pose(source.skeleton)
        pose['morphs']={'strip':{'blink':.2},'strip-right':{'blink':.8}}
        profile=build_retarget_profile(source,target)
        result=retarget_pose(source,target,pose,profile)
        self.assertEqual(result['morphs'],pose['morphs'])
        profile['morph_mapping']={'strip::blink':'strip-right::blink','strip-right::blink':'strip::blink'}
        swapped=retarget_pose(source,target,pose,profile)
        self.assertEqual(swapped['morphs'],{'strip':{'blink':.8},'strip-right':{'blink':.2}})
        for mapping in ({'blink':'blink'},
                        {'strip::blink':'strip::blink','strip-right::blink':'strip::blink'},
                        {'strip::blink':'missing::blink'}):
            with self.subTest(mapping=mapping):
                profile['morph_mapping']=mapping
                with self.assertRaises(RigError) as caught:retarget_pose(source,target,pose,profile)
                self.assertEqual(caught.exception.report['code'],'RIG_RETARGET_MORPH')
