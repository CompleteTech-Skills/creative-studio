"""Endpoint derivative/cycle contracts and actual native extrapolated geometry."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).resolve().parent)]
from animation3d_contracts import validate_contract,KINDS
from animation3d_registry import sample_track,TargetRegistry
from animation3d_evaluator import RigClipEvaluator
from rig_test_helpers import fixture
from rig_skinning import evaluate_deformation
from rig_math import axis_angle
from rig_errors import RigError
from test_animation3d_clips import clip

class ExtrapolationTests(unittest.TestCase):
    def track(self,mode='linear'):
        value=clip(mode)['tracks'][0]
        value['keys'][0].update(time=dict(frame=10),value=[0,0,0])
        value['keys'][1].update(time=dict(frame=20),value=[10,0,0])
        value['extrapolation_before']=value['extrapolation_after']='linear'
        return value
    def sample(self,track,frame):
        validate_contract(dict(schema_version=1,kind=KINDS['track'],value=track))
        registry=SimpleNamespace(_validated_track_entry=lambda track:{})
        return sample_track(track,dict(frame=frame),registry)
    def test_linear_hermite_ease_and_bezier_continue_endpoint_derivatives(self):
        value=self.track()
        self.assertEqual(self.sample(value,0),[-10,0,0]);self.assertEqual(self.sample(value,30),[20,0,0])
        value=self.track('hermite');value['keys'][0]['out_tangent']=[2,0,0];value['keys'][1]['in_tangent']=[3,0,0]
        self.assertEqual(self.sample(value,0),[-20,0,0]);self.assertEqual(self.sample(value,30),[40,0,0])
        value=self.track('ease');value['keys'][0]['ease_out']=1;value['keys'][1]['ease_in']=0
        self.assertEqual(self.sample(value,0),[0,0,0]);self.assertEqual(self.sample(value,30),[20,0,0])
        value=self.track('bezier');value['keys'][0]['out_handle']=dict(time_delta_frames=2,value_delta=[4,0,0]);value['keys'][1]['in_handle']=dict(time_delta_frames=-2,value_delta=[-6,0,0])
        self.assertEqual(self.sample(value,0),[-20,0,0]);self.assertEqual(self.sample(value,30),[40,0,0])
    def test_cycle_wraps_exact_key_interval_and_hold_preserves_terminal(self):
        value=self.track();value['extrapolation_before']=value['extrapolation_after']='cycle'
        self.assertEqual(self.sample(value,5),[5,0,0]);self.assertEqual(self.sample(value,25),[5,0,0]);self.assertEqual(self.sample(value,20),[10,0,0])
        value['extrapolation_before']=value['extrapolation_after']='hold'
        self.assertEqual(self.sample(value,0),[0,0,0]);self.assertEqual(self.sample(value,30),[10,0,0])
    def test_quaternion_extrapolation_is_short_arc_and_unit_length(self):
        value=self.track('quaternion-slerp');value.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),value_type='quaternion',default_value=[0,0,0,1])
        value['keys'][0]['value']=axis_angle([0,0,1],0).tolist();value['keys'][1]['value']=(-axis_angle([0,0,1],np.pi/2)).tolist()
        for frame,angle in [(0,-np.pi/2),(30,np.pi)]:
            result=np.asarray(self.sample(value,frame));expected=axis_angle([0,0,1],angle)
            self.assertAlmostEqual(abs(float(result@expected)),1);self.assertAlmostEqual(float(result@result),1)
    def test_catmull_tcb_and_squad_use_actual_endpoint_derivatives(self):
        value=self.track('catmull-rom')
        value['keys'].append(dict(time=dict(frame=30),value=[10,0,0]))
        value['keys'][1]['interpolation']='catmull-rom'
        self.assertEqual(self.sample(value,0),[-10,0,0]);self.assertEqual(self.sample(value,40),[10,0,0])
        value['keys'][0]['interpolation']='tcb';value['keys'][1]['interpolation']='tcb'
        value['keys'][0]['tension']=1;value['keys'][-1]['tension']=1
        self.assertEqual(self.sample(value,0),[0,0,0]);self.assertEqual(self.sample(value,40),[10,0,0])
        rotation=self.track('quaternion-squad')
        rotation.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),value_type='quaternion',default_value=[0,0,0,1])
        rotation['keys'][0]['value']=axis_angle([0,0,1],0).tolist()
        rotation['keys'][1]['value']=axis_angle([0,0,1],np.pi/2).tolist()
        # With two keys, canonical SQUAD endpoint controls halve the SLERP
        # endpoint derivative: -45deg before and135deg after one segment.
        for frame,angle in [(0,-np.pi/4),(30,3*np.pi/4)]:
            result=np.asarray(self.sample(rotation,frame))
            self.assertAlmostEqual(abs(float(result@axis_angle([0,0,1],angle))),1)

    def test_degenerate_finite_bezier_derivative_and_detached_step_result(self):
        value=self.track('bezier');value['keys'][0]['out_handle']=dict(time_delta_frames=0,value_delta=[0,0,0])
        value['keys'][1]['in_handle']=dict(time_delta_frames=-2,value_delta=[-6,0,0])
        self.assertEqual(self.sample(value,0),[-5,0,0])
        value=self.track('step')
        sampled=self.sample(value,30);sampled[0]=999
        self.assertEqual(value['keys'][-1]['value'],[10,0,0])

    def test_invalid_linear_domains_and_work_bounds_fail_typed(self):
        value=self.track();value.update(target=dict(kind='morph',actor_id='actor',mesh_id='strip',morph_id='blink',property='weight'),value_type='float',default_value=0)
        value['keys'][0]['value']=0;value['keys'][1]['value']=1
        with self.assertRaises(RigError):self.sample(value,50)
        value=self.track('bezier');value['keys'][0]['out_handle']=dict(time_delta_frames=0,value_delta=[1,0,0]);value['keys'][1]['in_handle']=dict(time_delta_frames=-2,value_delta=[-1,0,0])
        with self.assertRaises(RigError) as rejected:self.sample(value,0)
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_EXTRAPOLATION')
        value=self.track();value['keys'][0]['time']=dict(frame=0);value['keys'][1]['time']=dict(frame=0,subframe_ticks=1)
        with self.assertRaises(RigError) as rejected:self.sample(value,2)
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_LIMIT')
    def test_native_geometry_uses_extrapolated_pose_without_playback_history(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False);value=clip();value['tracks'][0]['target']['bone_id']='b0'
            value['tracks'][0]['keys'][0].update(time=dict(frame=6),value=[0,0,0]);value['tracks'][0]['keys'][1].update(time=dict(frame=18),value=[1,0,0])
            value['tracks'][0]['extrapolation_before']=value['tracks'][0]['extrapolation_after']='linear'
            evaluator=RigClipEvaluator(rig.root,'actor');base=evaluate_deformation(rig)
            signatures=[]
            for frame,expected in [(24,1.5),(0,-.5),(24,1.5)]:
                state=evaluator.evaluate([value],dict(frame=frame));signatures.append(state['dependency_signature'])
                for mesh,data in base.items():np.testing.assert_allclose(np.array(state['geometry'][mesh]['positions'])-data['positions'],np.tile([expected,0,0],(len(data['positions']),1)),atol=1e-12)
            self.assertEqual(signatures[0],signatures[2])

if __name__=='__main__':unittest.main()
