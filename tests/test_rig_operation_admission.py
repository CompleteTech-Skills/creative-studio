"""Closed operation payloads tested without writing native asset outputs."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from rig_animation import clip_from_poses
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from rig_http import RigHandler
from rig_math import axis_angle
from rig_models import Geometry, Mesh, RigAsset, SkinWeights
from rig_operations import pose_operation, solve_operation
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone, skeleton_from_heads
from rig_skinning import evaluate_deformation


def memory_rig():
    skeleton=skeleton_from_heads([dict(id='b'+str(i),role='eye.left' if i==2 else 'joint-'+str(i),
        parent='b'+str(i-1) if i else None,head=[0,i,0],tail=[0,i+1,0],deform=True) for i in range(3)])
    positions=np.array([[.1,0,0],[-.1,0,0],[.1,2,0],[-.1,2,0.]])
    delta=np.zeros_like(positions);delta[:,2]=.2
    geometry=Geometry([Mesh('strip',positions,np.array([[0,1,2],[1,3,2]]),morphs={'blink':{'POSITION':delta}})])
    rest=evaluate_fk(skeleton)
    skin=SkinWeights('strip',['b0','b2'],np.array([[0],[0],[1],[1]],dtype=np.uint16),
                     np.ones((4,1)),np.array([np.linalg.inv(rest[name]) for name in ('b0','b2')]))
    manifest=dict(id='fixture',skeleton=skeleton,limits=deepcopy(DEFAULT_LIMITS),
        controllers=dict(constraints=[],ik=[dict(schema_version=1,id='arm',bones=['b0','b1','b2'],
            solver='two-bone',pole=[0,0,1],tolerance=1e-6,iterations=32)]),
        facial=dict(schema_version=1,eyes=['b2'],morph_targets={'strip':['blink']},
            expressions={'blink':dict(schema_version=1,morphs={'blink':1.},bones={},incompatible=[])},
            eyelids=[dict(eye='b2',mesh='strip',morph='blink',up_weight=.8,down_weight=.1,up_radians=.5,down_radians=.5)]))
    rig=RigAsset(manifest,geometry,{'strip':skin})
    a=neutral_pose(skeleton);b=rotate_bone(skeleton,a,'b1',axis_angle([0,0,1],.8))
    rig.clips['bend']=clip_from_poses(rig,'bend',[0.,.5,1.],[a,b,a])
    return rig


class RigOperationAdmissionTests(unittest.TestCase):
    def setUp(self):self.rig=memory_rig()

    def http(self, operation, body):
        handler=object.__new__(RigHandler);handler.path='/v1/rigs/fixture/'+operation;handler.command='POST'
        handler.server=SimpleNamespace(token='unit-test-token-0123456789',server_port=8766,budget=threading.BoundedSemaphore(2))
        handler.headers={'Authorization':'Bearer '+handler.server.token};handler._root=lambda identifier:identifier
        handler._body=lambda:body;responses=[];handler._respond=lambda *response:responses.append(response)
        with patch('rig_models.load_rig',return_value=self.rig):handler._run()
        self.assertEqual(len(responses),1)
        return responses[0]

    def test_http_rejects_unknown_falsey_and_malformed_pose_controls(self):
        bad=[{'unexpected':True},{'pose':[]},{'pose':None},{'pose':''},{'clip':''},{'clip':False},
             {'expressions':[]},{'expressions':None},{'expressions':{'blink':True}},
             {'gaze':[]},{'gaze':False},{'gaze':[True,0,1]}, {'fingers':{}},{'fingers':False},
             {'fingers':{'side':'left','curl':False}},{'fingers':{'side':'left','fingers':[]}},
             {'appendages':[]},{'appendages':{'unknown':0}},{'appendages':{'seed':False}},
             {'head_follow':True},{'head_follow':2},{'time':False},{'time':-1},
             {'time':float('nan')},{'time':10**10000},{'gaze':[0,0,10**10000]}]
        for body in bad:
            with self.subTest(fields=list(body)):
                status,result=self.http('pose',body)
                self.assertEqual(status,400);self.assertFalse(result['ok'])
                self.assertIn(result['error']['code'],('RIG_OPERATION_SCHEMA','RIG_NONFINITE'))
        self.assertEqual(self.http('pose',{'clip':None,'time':0,'expressions':{},'appendages':{}})[0],200)
        with self.assertRaises(RigError):pose_operation(self.rig,{'clip':'missing'})

    def test_pose_schema_version_and_actual_references_are_checked(self):
        for change in (lambda p:p.update(rig_version='9.0.0'),lambda p:p['bones'].update(missing={}),
                       lambda p:p['bones'].update(b1={'rotation':[0,0,0,2]}),
                       lambda p:p['morphs'].update(missing={}),lambda p:p['morphs'].update(strip={'missing':0}),
                       lambda p:p['morphs'].update(strip={'blink':3})):
            pose=neutral_pose(self.rig.skeleton);change(pose)
            with self.subTest(change=change),self.assertRaises(RigError):pose_operation(self.rig,{'pose':pose})

    def test_explicit_null_gaze_releases_eye_and_owned_lid_geometry(self):
        pose=rotate_bone(self.rig.skeleton,neutral_pose(self.rig.skeleton),'b2',axis_angle([1,0,0],.2))
        pose['morphs']={'strip':{'blink':.8}}
        before=deepcopy(pose);actual=pose_operation(self.rig,{'pose':pose,'gaze':None,'head_follow':0})
        self.assertNotIn('b2',actual['bones']);self.assertEqual(actual['morphs'],{})
        neutral=evaluate_deformation(self.rig,neutral_pose(self.rig.skeleton))['strip']['positions']
        np.testing.assert_allclose(evaluate_deformation(self.rig,actual)['strip']['positions'],neutral,atol=1e-12)
        self.assertGreater(np.max(np.abs(evaluate_deformation(self.rig,pose)['strip']['positions']-neutral)),.1)
        self.assertEqual(pose,before)

    def test_solve_filters_owned_fields_and_honors_explicit_pole(self):
        first=solve_operation(self.rig,{'chain':'arm','target':[1,1,0],'pole':[0,0,1],'orientation':None,'clip':None,'time':0})
        other=solve_operation(self.rig,{'chain':'arm','target':[1,1,0],'pole':[0,0,-1]})
        self.assertLess(first['report']['residual_m'],1e-6)
        np.testing.assert_allclose(evaluate_fk(self.rig.skeleton,first['pose'])['b2'][:3,3],[1,1,0],atol=1e-6)
        self.assertGreater(evaluate_fk(self.rig.skeleton,first['pose'])['b1'][2,3],.1)
        self.assertLess(evaluate_fk(self.rig.skeleton,other['pose'])['b1'][2,3],-.1)
        rest=evaluate_deformation(self.rig,neutral_pose(self.rig.skeleton))['strip']['positions']
        self.assertGreater(np.max(np.abs(evaluate_deformation(self.rig,first['pose'])['strip']['positions']-rest)),.1)
        self.assertEqual(self.http('solve-ik',{'chain':'arm','target':[1,1,0]})[0],200)
        for body in ({'chain':'arm','target':[True,1,0]}, {'chain':'arm','target':[1,1,0],'pole':[]},
                     {'chain':'arm','target':[1,1,0],'orientation':[0,0,0,2]},
                     {'chain':'arm','target':[1,1,0],'unknown':0}, {'chain':False,'target':[1,1,0]},{}):
            with self.subTest(body=body):self.assertEqual(self.http('solve-ik',body)[0],400)
        with self.assertRaises(RigError):pose_operation(self.rig,{'target':[1,1,0]})

    def test_cli_dispatch_preserves_valid_defaults_and_reports_bad_input(self):
        import rig
        with patch('rig_models.load_rig',return_value=self.rig):
            output=io.StringIO()
            with redirect_stdout(output):status=rig.main(['pose','--run-dir','memory'])
            self.assertEqual(status,0);self.assertEqual(json.loads(output.getvalue())['kind'],'hatch-3d-pose')
            for body in ({'pose':[]},{'head_follow':False},{'invented':0}):
                output=io.StringIO()
                with patch('rig.read_json',return_value=body),redirect_stdout(output):
                    status=rig.main(['pose','--run-dir','memory','--file','memory.json'])
                self.assertEqual(status,1);self.assertEqual(json.loads(output.getvalue())['error']['code'],'RIG_OPERATION_SCHEMA')


if __name__=='__main__':unittest.main()
