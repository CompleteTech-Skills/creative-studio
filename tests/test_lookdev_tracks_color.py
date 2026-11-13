from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_tracks import evaluate_track,evaluate_state,validate_track
from lookdev_color import configuration,validate_configuration
from lookdev_contracts import content_hash
from rig_errors import RigError


def track(a,b,kind='scalar',mode='linear'):
    return {'property':kind,'interpolation':mode,'keys':[{'frame':0,'value':a},{'frame':10,'value':b}]}

class TrackTests(unittest.TestCase):
    def test_random_access_colors_and_native_quaternion_shortest_arc(self):
        t=track([.1,.2,.3],[.5,1.,1.5],'color');copy=deepcopy(t)
        self.assertEqual(evaluate_track(t,10),[.5,1.,1.5])
        for actual,expected in zip(evaluate_track(t,5),[.3,.6,.9]):self.assertAlmostEqual(actual,expected)
        self.assertEqual(t,copy)
        self.assertEqual(evaluate_track(track([0,0,0,1],[0,0,0,-1],'quaternion'),5),[0.,0.,0.,1.])

    def test_step_events_easing_and_resume_are_authored(self):
        switch=track(False,True,'discrete','step')
        self.assertFalse(evaluate_track(switch,9));self.assertTrue(evaluate_track(switch,10))
        self.assertAlmostEqual(evaluate_track(track(0.,1.,mode='smooth'),2),.104)
        self.assertEqual(evaluate_track(track(3.,5.),50),5.)
        for bad in [track(False,True,'discrete'),track(True,1),track([1,0,0,0],[0,0,0,2],'quaternion')]:
            with self.assertRaises(RigError):validate_track(bad)
        for frame in [True,-1,1.5,10_000_001]:
            with self.assertRaises(RigError):evaluate_track(track(0,1),frame)

    def test_native_cubic_derivatives_and_boundary_validation(self):
        t=track(0.,1.,mode='cubic')
        for key in t['keys']:key.update(in_tangent=0.,out_tangent=0.)
        t['keys'][0]['out_tangent']=.4
        self.assertAlmostEqual(evaluate_track(t,5),1.)
        self.assertEqual(evaluate_track(t,10),1.)
        t['keys'][0]['out_tangent']=[.4]
        with self.assertRaises(RigError):evaluate_track(t,5)

    def test_state_isolation_continuity_and_conflicting_bindings(self):
        base={'color':{'exposure':0},'material':{'variant':'dry'}};copy=deepcopy(base)
        tracks=[{'path':['color','exposure'],'track':track(0,2)}]
        self.assertEqual(evaluate_state(base,tracks,5)['color']['exposure'],1.)
        self.assertEqual(base,copy)
        hashed=deepcopy(base);hashed['sha256']=content_hash(hashed)
        changed=evaluate_state(hashed,tracks,5)
        self.assertNotEqual(changed['sha256'],hashed['sha256'])
        self.assertEqual(changed['sha256'],content_hash({k:v for k,v in changed.items() if k!='sha256'}))
        with self.assertRaises(RigError):evaluate_state(base,tracks,5,locks=[['color']])
        with self.assertRaises(RigError):evaluate_state(base,tracks+tracks,5)
        with self.assertRaises(RigError):evaluate_state(base,[{'path':['missing'],'track':track(0,1)}],5)

    def test_malformed_frames_and_dimensions_fail_before_evaluation(self):
        for key,value in [('keys',[]),('keys',[{'frame':0,'value':1},{'frame':0,'value':2}]),
                          ('keys',[{'frame':0,'value':[1,2]},{'frame':1,'value':[1,2,3]}])]:
            t=track([0,0],[1,1],'vector');t[key]=value
            with self.assertRaises(RigError):evaluate_track(t,0)

class ColorTests(unittest.TestCase):
    def test_hash_invalidation_and_explicit_output_semantics(self):
        a=configuration();b=configuration(exposure=1)
        self.assertNotEqual(a['sha256'],b['sha256'])
        self.assertEqual(validate_configuration(configuration(output='scene-linear-exr'))['working_space'],'linear-srgb')
        a['exposure_stops']=1
        with self.assertRaises(RigError):validate_configuration(a)

    def test_unimplemented_transforms_and_physical_bounds_reject(self):
        for args in [{'view':'ACES'},{'output':'jpeg'},{'exposure':True},{'temperature_kelvin':100},{'tint':501}]:
            with self.assertRaises(RigError):configuration(**args)
        value=configuration();value['extra']=0;value['sha256']=content_hash({k:v for k,v in value.items() if k!='sha256'})
        with self.assertRaises(RigError):validate_configuration(value)
