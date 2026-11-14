"""Executed canonical curve and native deformed-geometry diagnostics."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from animation3d_contracts import KINDS, validate_contract, validate_track
from animation3d_diagnostics import diagnose_track, diagnose_clip, diagnose_euler_conversion
from animation3d_math import euler_xyz
from animation3d_registry import TargetRegistry, sample_track
from animation3d_evaluator import RigClipEvaluator
from animation3d_test_helpers import samples
from rig_errors import RigError, object_hash
from rig_test_helpers import fixture


class NumericRegistry:
    """Only the curve test target domain; never claimed as native admission."""
    def _validated_track_entry(self, track, duration_frames=None):
        validate_contract(dict(schema_version=1, kind=KINDS['track'], value=track))
        validate_track(track, duration_frames)
        return {}


def track(mode='linear', values=None, frames=None, prop='translation'):
    value = samples()['clip']['tracks'][0]
    value['target']['bone_id'] = 'b0'
    value['target']['property'] = prop
    value['default_value'] = [1,1,1] if prop == 'scale' else [0,0,0]
    values = values or [[0,0,0],[1,0,0]]
    frames = frames or [0,24]
    value['keys'] = [dict(time=dict(frame=f), value=deepcopy(v), interpolation=mode)
                     for f,v in zip(frames, values)]
    return value


def codes(result):
    return {f['code'] for f in result['findings']}


class DiagnosticsTests(unittest.TestCase):
    def diagnose(self, value, **kw):
        return diagnose_track(value, NumericRegistry(), fps=dict(numerator=24, denominator=1),
                              duration_frames=24, **kw)

    def test_continuous_motion_rates_jumps_and_acceleration_are_measured(self):
        value = track(values=[[0,0,0],[10,0,0],[10,0,0]], frames=[0,1,24])
        original = deepcopy(value)
        result = self.diagnose(value, policy=dict(maximum_velocity=10, maximum_acceleration=20))
        self.assertTrue({'position-jump','velocity-spike','extreme-acceleration'} <= codes(result))
        self.assertEqual(value, original)
        repeated = self.diagnose(value, policy=dict(maximum_velocity=10, maximum_acceleration=20))
        self.assertEqual(result, repeated)
        velocity = next(f['observed'] for f in result['findings'] if f['code']=='velocity-spike')
        self.assertAlmostEqual(velocity, 240.)
        self.assertFalse(result['approved'])

    def test_steps_and_explicit_cuts_do_not_produce_infinite_derivative_warnings(self):
        stepped = track('step', values=[[0,0,0],[100,0,0]], frames=[0,1])
        self.assertFalse(codes(self.diagnose(stepped)) & {'position-jump','velocity-spike','extreme-acceleration'})
        continuous = track(values=[[0,0,0],[100,0,0]], frames=[0,1])
        result = self.diagnose(continuous, intentional_cuts=[dict(frame=1)],
                               policy=dict(samples_per_segment=1))
        self.assertFalse(codes(result) & {'position-jump','velocity-spike','extreme-acceleration'})

    def test_rotation_sign_scale_and_short_rotation_jump(self):
        rotation = track(values=[[0,0,0,1],[0,0,0,-1]])
        rotation.update(value_type='quaternion', default_value=[0,0,0,1])
        rotation['target']['property']='rotation'
        for key in rotation['keys']: key['interpolation']='quaternion-slerp'
        result = self.diagnose(rotation)
        self.assertIn('quaternion-sign', codes(result))
        self.assertNotIn('rotation-jump', codes(result))
        rotation['keys'][1].update(time=dict(frame=1), value=[0,0,1,0])
        self.assertIn('rotation-jump', codes(self.diagnose(rotation)))
        scale = track(values=[[1,1,1],[3,1,1]],frames=[0,1], prop='scale')
        self.assertIn('scale-discontinuity', codes(self.diagnose(scale)))

    def test_spline_overshoot_and_canonical_domain_failure_remain_visible(self):
        value = track('hermite', values=[[0,0,0],[1,0,0]])
        value['keys'][0]['out_tangent']=[1,0,0]
        value['keys'][1]['in_tangent']=[-1,0,0]
        self.assertIn('spline-overshoot', codes(self.diagnose(value)))
        value['target']['property']='scale'
        value['default_value']=[1,1,1]
        value['keys'][0]['value']=value['keys'][1]['value']=[1,1,1]
        value['keys'][0]['out_tangent']=[-2,0,0]
        value['keys'][1]['in_tangent']=[2,0,0]
        self.assertIn('property-domain-violation', codes(self.diagnose(value)))

    def test_loop_value_and_velocity_seams_and_hold_behavior(self):
        self.assertIn('broken-loop-seam', codes(self.diagnose(track(), loop=True)))
        value = track(values=[[0,0,0],[1,0,0],[0,0,0]],frames=[0,12,24])
        self.assertIn('broken-loop-velocity', codes(self.diagnose(value,loop=True)))
        self.assertNotIn('broken-loop-seam', codes(self.diagnose(value,loop=True)))
        clip=samples()['clip']; clip['tracks']=[track()]; clip['loop_mode']='hold'
        self.assertNotIn('broken-loop-seam', codes(diagnose_clip(clip,NumericRegistry())['tracks'][0]))

    def test_visibility_pulses_and_acknowledged_cuts(self):
        value = track('step', values=[True,False,True],frames=[0,1,2])
        value.update(value_type='boolean', default_value=True)
        value['target']=dict(kind='node',node_id='mesh',property='visibility')
        self.assertIn('visibility-flicker', codes(self.diagnose(value)))
        redundant=deepcopy(value)
        redundant['keys'].insert(2,dict(time=dict(frame=2),value=False,interpolation='step'))
        redundant['keys'][-1]['time']=dict(frame=3)
        self.assertIn('visibility-flicker', codes(self.diagnose(redundant)))
        self.assertNotIn('visibility-flicker',codes(self.diagnose(value,intentional_cuts=[dict(frame=1)])))

    def test_invalid_extrapolation_and_all_malformed_policy_boundaries(self):
        value = track(); value['extrapolation_after']='unrecognized'
        result=self.diagnose(value)
        self.assertFalse(result['source_valid'])
        self.assertEqual(codes(result),{'invalid-extrapolation'})
        self.assertEqual(result['samples'],[])
        for policy in (dict(unknown=1),dict(maximum_velocity=float('nan')),dict(maximum_velocity=10**400),
                       dict(max_samples=True),dict(samples_per_segment=65),dict(max_samples=1)):
            with self.subTest(policy=policy),self.assertRaises(RigError):self.diagnose(track(),policy=policy)
        with self.assertRaises(RigError):self.diagnose([],policy={})
        with self.assertRaises(RigError):self.diagnose(track(),intentional_cuts=[dict(frame=25)])

    def test_nonuniform_exact_clocks_and_sample_work_bounds(self):
        value=track(); value['keys'][1]['time']=dict(frame=1,subframe_ticks=1)
        report=self.diagnose(value,policy=dict(samples_per_segment=7))
        positions=[Fraction(s['time']['frame'])+Fraction(s['time']['subframe_ticks'],1_000_000) for s in report['samples']]
        self.assertEqual(positions,sorted(set(positions)))
        self.assertIn(Fraction(1_000_001,1_000_000),positions)
        with self.assertRaises(RigError):self.diagnose(track(),policy=dict(max_samples=2))
        clip=samples()['clip'];clip['tracks']=[deepcopy(track()) for _ in range(129)]
        for i,t in enumerate(clip['tracks']):t['id']=f'track-{i}'
        with self.assertRaises(RigError):diagnose_clip(clip,NumericRegistry())

    def test_euler_actual_conversion_error_singularity_and_branch_jump(self):
        values=[[0,0,math.pi-.01],[0,0,-math.pi+.01],[0,math.pi/2,0]]
        quats=[euler_xyz(v).tolist() for v in values]
        report=diagnose_euler_conversion(quats,values)
        self.assertFalse(any(f['code']=='euler-conversion-error' for f in report['findings']))
        self.assertTrue({'euler-conversion-singularity','euler-coordinate-jump'} <= {f['code'] for f in report['findings']})
        wrong=diagnose_euler_conversion([euler_xyz([.5,.2,.1]).tolist()],[[.1,.2,.5]])
        self.assertEqual(wrong['findings'][0]['code'],'euler-conversion-error')
        for bad in ([0,0,0], [0,0,0,10**400], [0,0,False,1]):
            with self.assertRaises(RigError):diagnose_euler_conversion([bad],[[0,0,0]])
        for tolerance in (True,10**400,float('nan'),0):
            with self.assertRaises(RigError):diagnose_euler_conversion([[0,0,0,1]],[[0,0,0]],tolerance_rad=tolerance)

    def test_configurable_easing_has_independent_polynomial_oracles(self):
        # u=.25, arbitrary endpoint displacement and nondefault slope controls.
        for outgoing,incoming,expected in ((1,0,.109375),(0,1,.296875),(1,1,.15625),(.25,.5,.23828125)):
            value=track('ease',values=[[2,0,0],[10,0,0]])
            value['keys'][0]['ease_out']=outgoing
            value['keys'][1]['ease_in']=incoming
            actual=sample_track(value,dict(frame=6),NumericRegistry())[0]
            # Hermite basis at u=1/4: h01=5/32,h10=9/64,h11=-3/64.
            oracle=2+8*(Fraction(5,32)+Fraction(9,64)*(1-Fraction(str(outgoing)))-Fraction(3,64)*(1-Fraction(str(incoming))))
            self.assertAlmostEqual(actual,float(oracle),places=12)
            self.assertAlmostEqual((actual-2)/8,expected)

    def test_all_curve_families_use_the_canonical_sampled_values(self):
        for mode in ('step','linear','ease','bezier','hermite','catmull-rom','tcb'):
            with self.subTest(mode=mode):
                value=track(mode)
                a,b=value['keys']
                if mode=='ease':a['ease_out']=0;b['ease_in']=0
                if mode=='hermite':a['out_tangent']=[1/24,0,0];b['in_tangent']=[1/24,0,0]
                if mode=='bezier':
                    a['out_handle']=dict(time_delta_frames=8,value_delta=[1/3,0,0])
                    b['in_handle']=dict(time_delta_frames=-8,value_delta=[-1/3,0,0])
                result=self.diagnose(value)
                self.assertTrue(result['source_valid'])
                middle=next(s['value'] for s in result['samples'] if s['time']['frame']==12 and not s['time']['subframe_ticks'])
                np.testing.assert_allclose(middle,[0 if mode=='step' else .5,0,0],atol=1e-12)
        for mode in ('quaternion-slerp','quaternion-squad'):
            value=track(mode,values=[[0,0,0,1],[0,0,math.sin(math.pi/4),math.cos(math.pi/4)]])
            value.update(value_type='quaternion',default_value=[0,0,0,1])
            value['target']['property']='rotation'
            report=self.diagnose(value)
            middle=next(s['value'] for s in report['samples'] if s['time']['frame']==12 and not s['time']['subframe_ticks'])
            np.testing.assert_allclose(middle,[0,0,math.sin(math.pi/8),math.cos(math.pi/8)],atol=1e-12)
            self.assertAlmostEqual(np.linalg.norm(middle),1.)

    def test_actual_skinned_geometry_matches_measured_curve_velocity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            rig=fixture(root,animated=False)
            before={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
            registry=TargetRegistry.from_rig(rig.root,'actor')
            value=track(values=[[0,0,0],[2,0,0]],frames=[0,1])
            report=diagnose_track(value,registry,fps=dict(numerator=24,denominator=1),duration_frames=24,policy=dict(maximum_velocity=10))
            self.assertIn('velocity-spike',codes(report))
            self.assertEqual(report['track_sha256'],object_hash(value))
            self.assertIsNotNone(report['target_binding'])
            clip=samples()['clip'];clip['tracks']=[value]
            evaluator=RigClipEvaluator(rig.root,'actor')
            a=evaluator.evaluate([clip],dict(frame=0))
            b=evaluator.evaluate([clip],dict(frame=0,subframe_ticks=500000))
            for mesh in a['geometry']:
                displacement=np.asarray(b['geometry'][mesh]['positions'])-np.asarray(a['geometry'][mesh]['positions'])
                np.testing.assert_allclose(displacement,np.broadcast_to([1,0,0],displacement.shape),atol=1e-12)
            after={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
