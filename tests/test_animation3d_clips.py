"""Independent numerical and real native geometry checks for issue #68."""
from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from animation3d_test_helpers import samples
from animation3d_contracts import validate_contract
from animation3d_registry import TargetRegistry, sample_track
from animation3d_curves import evaluate_segment
from animation3d_clips import (ClipHistory,ClipLibrary,edit_key,retime_clip,reverse_clip,
                               source_time,events_between)
from animation3d_evaluator import RigClipEvaluator,PoseLibrary
from animation3d_time import frame_position
from rig_test_helpers import fixture
from rig_skeleton import neutral_pose
from rig_errors import RigError,object_hash


def clip(mode='linear'):
    value=samples()['clip']; value['tracks'][0]['keys']=[
        dict(time=dict(frame=0),value=[0,0,0],interpolation=mode),
        dict(time=dict(frame=24),value=[24,0,0])]
    return value


class CurveTests(unittest.TestCase):
    def sample(self,value,frame):
        validate_contract(value); track=value['tracks'][0]
        return evaluate_segment(track,0,Fraction(frame))

    def test_ease_in_out_and_both_have_independent_polynomial_oracles(self):
        for outgoing,incoming,expected in [(1,0,2.625),(0,1,7.125),(1,1,3.75)]:
            value=clip('ease'); a,b=value['tracks'][0]['keys']
            a['ease_out']=outgoing; b['ease_in']=incoming
            self.assertAlmostEqual(self.sample(value,6)[0],expected)

    def test_weighted_time_bezier_inversion_and_vector_controls(self):
        value=clip('bezier'); a,b=value['tracks'][0]['keys']
        a['out_handle']=dict(time_delta_frames=8,value_delta=[0,8,0],weighted=True,weight=.5)
        b['in_handle']=dict(time_delta_frames=-4,value_delta=[0,4,0])
        # At parameter 1/2, time=12 and point=(12,3,0).
        np.testing.assert_allclose(self.sample(value,12),[12,3,0],atol=1e-12)
        b['in_handle']['time_delta_frames']=-22
        with self.assertRaises(RigError):validate_contract(value)

    def test_catmull_and_tcb_match_literal_hermite_derivatives(self):
        for mode in ('catmull-rom','tcb'):
            value=clip(mode)
            # A two-key boundary uses the secant at both ends: exactly linear.
            np.testing.assert_allclose(self.sample(value,6),[6,0,0])
        value=clip('tcb'); a,b=value['tracks'][0]['keys']
        a['tension']=1; b['tension']=1
        np.testing.assert_allclose(self.sample(value,6),[3.75,0,0])
        a['tension']=2
        with self.assertRaises(RigError):validate_contract(value)

    def test_quaternion_squad_unit_shortest_path_and_neighbor_smoothness(self):
        value=clip('quaternion-squad'); track=value['tracks'][0]
        track.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),
                     value_type='quaternion',default_value=[0,0,0,1])
        track['keys']=[dict(time=dict(frame=i*8),value=[0,0,math.sin(i*.2),math.cos(i*.2)],interpolation='quaternion-squad') for i in range(4)]
        validate_contract(value)
        result=evaluate_segment(track,1,Fraction(12))
        np.testing.assert_allclose(result,[0,0,math.sin(.3),math.cos(.3)],atol=1e-12)
        track['keys'][2]['value']=[-v for v in track['keys'][2]['value']]
        np.testing.assert_allclose(evaluate_segment(track,1,Fraction(12)),result,atol=1e-12)
        self.assertAlmostEqual(np.linalg.norm(result),1)

    def test_curve_controls_cannot_be_silently_ignored(self):
        for mode,field,data in [('linear','out_handle',dict(time_delta_frames=1,value_delta=[0,0,0])),
                                ('catmull-rom','tension',.5),('linear','ease_out',.5)]:
            value=clip(mode); value['tracks'][0]['keys'][0][field]=data
            with self.assertRaises(RigError):validate_contract(value)


class EditingTests(unittest.TestCase):
    def test_key_transactions_reject_collisions_and_undo_redo_are_detached(self):
        value=clip(); before=object_hash(value); history=ClipHistory(value)
        new=dict(time=dict(frame=12),value=[1,2,3])
        changed=history.apply(edit_key,'move',key=new)
        changed['name']='external'; self.assertEqual(history.document['name'],'Clip')
        self.assertEqual(len(history.document['tracks'][0]['keys']),3)
        with self.assertRaises(RigError):history.apply(edit_key,'move',key=new)
        self.assertEqual(len(history.document['tracks'][0]['keys']),3)
        history.undo(); self.assertEqual(object_hash(history.document),before)
        history.redo(); history.apply(edit_key,'move',remove=dict(frame=12))
        self.assertEqual(object_hash(history.document),before)
        self.assertEqual(object_hash(value),before)

    def test_retime_preserves_hermite_shape_and_exact_source_ranges(self):
        value=clip('hermite'); a,b=value['tracks'][0]['keys']
        a['out_tangent']=[2,0,0]; b['in_tangent']=[0,0,0]
        transformed=retime_clip(value,Fraction(2),4)
        np.testing.assert_allclose(evaluate_segment(value['tracks'][0],0,Fraction(6)),
                                   evaluate_segment(transformed['tracks'][0],0,Fraction(16)))
        self.assertEqual(transformed['duration_frames'],52)
        self.assertEqual(frame_position(source_time(value,dict(frame=15),dict(numerator=30,denominator=1),source_in=6,source_out=18)),18)
        self.assertEqual(frame_position(source_time(value,dict(frame=15),dict(numerator=30,denominator=1),source_in=6,source_out=18,loop=True)),6)
        self.assertEqual(frame_position(source_time(value,dict(frame=5),dict(numerator=30,denominator=1),source_in=6,source_out=18,reverse=True)),14)
        with self.assertRaises(RigError):retime_clip(value,Fraction(1,7))
        with self.assertRaises(RigError):source_time(value,dict(frame=0),value['fps'],source_in=10,source_out=2)

    def test_reverse_linear_hermite_bezier_and_step_values(self):
        for mode in ('linear','hermite','bezier','step','ease','tcb','catmull-rom'):
            value=clip(mode); a,b=value['tracks'][0]['keys']
            if mode=='hermite':a['out_tangent']=[2,0,0]; b['in_tangent']=[0,0,0]
            if mode=='bezier':
                a['out_handle']=dict(time_delta_frames=4,value_delta=[8,2,0])
                b['in_handle']=dict(time_delta_frames=-8,value_delta=[-4,0,0])
            validate_contract(value); rev=reverse_clip(value); track=rev['tracks'][0]
            for position in (Fraction(1,1000000),Fraction(6),Fraction(12),Fraction(23)):
                right=next(i for i,k in enumerate(track['keys']) if frame_position(k['time'])>position)
                actual=evaluate_segment(track,right-1,position)
                expected=evaluate_segment(value['tracks'][0],0,24-position)
                np.testing.assert_allclose(actual,expected,atol=1e-10,err_msg=mode)

    def test_events_use_explicit_half_open_intervals_and_reverse_order(self):
        value=clip(); value['events']=[dict(id=str(i),name='marker',frame=i,type='marker') for i in (0,6,12,24)]
        self.assertEqual([e['frame'] for e in events_between(value,dict(frame=6),dict(frame=24))],[12,24])
        self.assertEqual([e['frame'] for e in events_between(value,dict(frame=24),dict(frame=6))],[12,6])
        self.assertEqual(events_between(value,dict(frame=6),dict(frame=6)),[])

    def test_clip_library_uses_exact_versions_and_never_aliases(self):
        value=clip(); library=ClipLibrary(); digest=library.add(value)
        returned=library.get('clip','1.0.0',digest); returned['name']='Changed'
        self.assertEqual(library.search('clip')[0]['sha256'],digest)
        with self.assertRaises(RigError):library.add(returned)
        with self.assertRaises(RigError):library.get('clip','1.0.0','0'*64)


class GeometryTests(unittest.TestCase):
    def test_independent_body_face_channels_move_real_vertices_out_of_order(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False); evaluator=RigClipEvaluator(rig.root,'actor')
            body=clip(); body['tracks'][0]['target']['bone_id']='b0'
            body['tracks'][0]['keys'][1]['value']=[1,0,0]
            face=clip(); face['id']='face'
            face['tracks'][0].update(target=dict(kind='morph',actor_id='actor',mesh_id=rig.geometry.meshes[0].id,morph_id='blink',property='weight'),value_type='float',default_value=0,
                keys=[dict(time=dict(frame=0),value=0),dict(time=dict(frame=24),value=1)])
            before=object_hash([body,face]); results={}
            for frame in (0,18,6,24,12,18,0):
                output=evaluator.evaluate([body,face],dict(frame=frame)); actual=object_hash(output)
                if frame in results:self.assertEqual(results[frame],actual)
                results[frame]=actual
                vertices=np.array(output['geometry'][rig.geometry.meshes[0].id]['positions'])
                expected=rig.geometry.meshes[0].positions+np.array([frame/24,0,.2*frame/24])
                np.testing.assert_allclose(vertices,expected,atol=1e-10)
                np.testing.assert_allclose(np.array(output['world_matrices']['b0'])[:3,3],[frame/24,0,0])
            self.assertEqual(object_hash([body,face]),before)
            # Facial clock is independent of the body source clock.
            output=evaluator.evaluate_sources([(body,dict(frame=6)),(face,dict(frame=24))])
            vertices=np.array(output['geometry'][rig.geometry.meshes[0].id]['positions'])
            np.testing.assert_allclose(vertices,rig.geometry.meshes[0].positions+[.25,0,.2])
            pose_library=PoseLibrary(rig); digest=pose_library.add(output['pose'])
            self.assertEqual(pose_library.get('neutral',digest),output['pose'])

    def test_missing_targets_conflicting_channels_and_world_space_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False); evaluator=RigClipEvaluator(rig.root,'actor')
            value=clip(); value['tracks'][0]['target']['bone_id']='b0'
            for update in ({'target':dict(kind='bone',actor_id='actor',bone_id='missing',property='translation')},
                           {'coordinate_space':'world'}):
                bad=deepcopy(value); bad['tracks'][0].update(update)
                with self.assertRaises(RigError):evaluator.evaluate([bad],dict(frame=12))
            with self.assertRaises(RigError):evaluator.evaluate([value,value],dict(frame=12))
            with self.assertRaises(RigError):evaluator.evaluate([value],dict(frame=25))

    def test_sampled_new_curves_honor_native_morph_and_scale_domains(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False); registry=TargetRegistry.from_rig(rig.root,'actor')
            entry=next(e for e in registry.document['entries'] if e['target']['kind']=='morph')
            track=clip('bezier')['tracks'][0]; track.update(target=entry['target'],value_type='float',default_value=0)
            track['keys']=[dict(time=dict(frame=0),value=0,interpolation='bezier',out_handle=dict(time_delta_frames=8,value_delta=10)),
                           dict(time=dict(frame=24),value=0,in_handle=dict(time_delta_frames=-8,value_delta=10))]
            with self.assertRaises(RigError):sample_track(track,dict(frame=12),registry)


if __name__=='__main__':unittest.main()
