"""Closed operation payloads tested without writing native asset outputs."""
from contextlib import redirect_stdout
from copy import deepcopy
from email.message import Message
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from rig_animation import clip_from_poses
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from rig_http import RigHandler, RigServer
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


def articulated_rig():
    rig=memory_rig()
    specs=[dict(id=b['id'],role=b['role'],parent=b['parent'],head=b['head'],tail=b['tail'])
           for b in rig.skeleton['bones']]
    roles=['tail.1','wing-root.left','wing-root.right']+[name+'.1.left' for name in
           ('thumb','index','middle','ring','pinky')]
    for index,role in enumerate(roles):
        specs.append(dict(id=role,role=role,parent='b0',head=[index+1,0,0],tail=[index+1,1,0]))
    rig.manifest['skeleton']=skeleton_from_heads(specs)
    rest=evaluate_fk(rig.skeleton)
    # Each represented control owns a separate weighted triangle off its axis.
    # This exposes rotations, including wing twist, in actual deformed vertices.
    for role in roles:
        head=rest[role][:3,3]
        positions=head+np.array([[.1,.2,0],[.2,.5,.1],[.1,.8,-.1]])
        rig.geometry.meshes.append(Mesh(role,positions,np.array([[0,1,2]])))
        rig.weights[role]=SkinWeights(role,[role],np.zeros((3,1),dtype=np.uint16),
            np.ones((3,1)),np.array([np.linalg.inv(rest[role])]))
    return rig


class RigOperationAdmissionTests(unittest.TestCase):
    def setUp(self):self.rig=memory_rig()

    def http(self, operation, body):
        handler=object.__new__(RigHandler);handler.path='/v1/rigs/fixture/'+operation;handler.command='POST'
        handler.server=SimpleNamespace(token='unit-test-token-0123456789',server_port=8766,budget=threading.BoundedSemaphore(2))
        handler.headers=Message();handler.headers['Authorization']='Bearer '+handler.server.token
        handler._root=lambda identifier:identifier
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

    def test_invalid_morphs_reject_before_native_evaluation_for_pose_and_ik(self):
        invalid=[({'missing':{}},'RIG_MORPH_REFERENCE'),
                 ({'strip':{'missing':0}},'RIG_MORPH_REFERENCE'),
                 ({'strip':{'blink':3}},'RIG_OPERATION_SCHEMA'),
                 ({'strip':{'blink':float('nan')}},'RIG_NONFINITE'),
                 ({'strip':{'blink':float('inf')}},'RIG_NONFINITE')]
        for operation,extra in ((pose_operation,{}),(solve_operation,{'chain':'arm','target':[1,1,0]})):
            for morphs,code in invalid:
                pose=neutral_pose(self.rig.skeleton);pose['morphs']=morphs
                parameters={'pose':pose,**extra};before=deepcopy(parameters)
                with self.subTest(operation=operation.__name__,code=code), \
                        patch('rig_skeleton.evaluate_fk') as fk,patch('rig_ik.solve_ik') as ik:
                    with self.assertRaises(RigError) as caught:operation(self.rig,parameters)
                    self.assertEqual(caught.exception.report['code'],code)
                    fk.assert_not_called();ik.assert_not_called()
                # NaN is not equal to itself; compare the unchanged serialized payload.
                self.assertEqual(json.dumps(parameters,sort_keys=True),json.dumps(before,sort_keys=True))

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

    def test_cli_rejects_nonobject_parameter_files_before_merging_defaults(self):
        import rig
        with tempfile.TemporaryDirectory() as directory, patch('rig_models.load_rig', return_value=self.rig):
            parameter_file = Path(directory) / 'parameters.json'
            for command, extra in (
                ('pose', []), ('solve-ik', ['--chain', 'arm', '--target', '1', '1', '0']),
            ):
                for body in ([], '', [['expressions', {'blink': .5}]]):
                    with self.subTest(command=command, body=body):
                        parameter_file.write_text(json.dumps(body), encoding='utf-8')
                        output = io.StringIO()
                        with redirect_stdout(output):
                            status = rig.main([command, '--run-dir', 'memory', '--file', str(parameter_file), *extra])
                        self.assertEqual(status, 1)
                        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'RIG_OPERATION_SCHEMA')

    def test_loopback_http_admits_defaults_and_rejects_supplied_falsy_controls(self):
        # Real request parsing/authentication/dispatch with synthetic in-memory
        # geometry; only durable fixture loading is replaced.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'jobs'/'fixture').mkdir(parents=True)
            server=RigServer(root/'jobs',root,token='synthetic-operation-test-token')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch('rig_models.load_rig',return_value=self.rig):
                    for endpoint,body,expected in (
                        ('pose',{},200),('pose',{'clip':None,'gaze':None},200),
                        ('pose',{'gaze':False},400),('pose',{'head_follow':None},400),
                        ('pose',{'fingers':0},400),('pose',{'appendages':False},400),
                        ('pose',{'fingers':{'side':'left','fingers':None}},400),
                        ('pose',{'expressions':0},400),('pose',{'unknown':None},400),
                        ('solve-ik',{'chain':'arm','target':[1,1,0],'pole':None,'orientation':None},200),
                        ('solve-ik',{'chain':'arm','target':[1,1,0],'fingers':{'side':'left','fingers':None}},400),
                        ('solve-ik',{'chain':'arm','target':[1,1,0],'pole':False},400),
                        ('solve-ik',{'chain':'arm','target':[1,1,0],'orientation':False},400)):
                        with self.subTest(endpoint=endpoint,body=body):
                            connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                            try:
                                connection.request('POST','/v1/rigs/fixture/'+endpoint,
                                    json.dumps(body,allow_nan=False),headers={
                                        'Content-Type':'application/json','Authorization':'Bearer '+server.token})
                                response=connection.getresponse();result=json.loads(response.read())
                                self.assertEqual(response.status,expected)
                                if expected==400:self.assertEqual(result['error']['code'],'RIG_OPERATION_SCHEMA')
                            finally:connection.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_loopback_rejects_invalid_morphs_before_native_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'jobs'/'fixture').mkdir(parents=True)
            server=RigServer(root/'jobs',root,token='synthetic-operation-test-token')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch('rig_models.load_rig',return_value=self.rig):
                    for endpoint,extra in (('pose',{}),('solve-ik',{'chain':'arm','target':[1,1,0]})):
                        for morphs,code in (({'missing':{}},'RIG_MORPH_REFERENCE'),
                                ({'strip':{'missing':0}},'RIG_MORPH_REFERENCE'),
                                ({'strip':{'blink':3}},'RIG_OPERATION_SCHEMA')):
                            pose=neutral_pose(self.rig.skeleton);pose['morphs']=morphs
                            with self.subTest(endpoint=endpoint,morphs=morphs), \
                                    patch('rig_skeleton.evaluate_fk') as fk,patch('rig_ik.solve_ik') as ik:
                                connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                                try:
                                    connection.request('POST','/v1/rigs/fixture/'+endpoint,
                                        json.dumps({'pose':pose,**extra},allow_nan=False),headers={
                                            'Content-Type':'application/json','Authorization':'Bearer '+server.token})
                                    response=connection.getresponse();result=json.loads(response.read())
                                    self.assertEqual(response.status,400)
                                    self.assertEqual(result['error']['code'],code)
                                    fk.assert_not_called();ik.assert_not_called()
                                finally:connection.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_numeric_and_nested_control_boundaries_apply_to_both_operations(self):
        invalid=[{'time':1e9+1},{'time':None},{'head_follow':-1e-9},{'head_follow':1+1e-9},
                 {'expressions':{'blink':-1e-9}},{'expressions':{'blink':1+1e-9}},
                 {'fingers':{'side':'left','curl':-1e-9}},
                 {'fingers':{'side':'left','curl':1+1e-9}},
                 {'fingers':{'side':'left','spread':-1-1e-9}},
                 {'fingers':{'side':'left','spread':1+1e-9}},
                 {'fingers':{'side':'left','unknown':0}},
                 {'fingers':{'side':'left','fingers':None}},
                 {'fingers':{'side':'left','fingers':['index','index']}},
                 {'appendages':{'time':-1e-9}},{'appendages':{'time':120+1e-9}},
                 {'appendages':{'seed':2**31}},{'appendages':{'seed':-2**31-1}},
                 {'appendages':{'wing_side':'other'}}]
        invalid += [{'appendages':{field:value}} for field in
                    ('tail_curl','tail_wag','wing_flap','wing_fold','wing_twist','secondary')
                    for value in (-1-1e-9,1+1e-9,False,None)]
        for operation,extra in ((pose_operation,{}),(solve_operation,{'chain':'arm','target':[1,1,0]})):
            for body in invalid:
                with self.subTest(operation=operation.__name__,body=body),self.assertRaises(RigError) as caught:
                    operation(self.rig,{**extra,**body})
                self.assertEqual(caught.exception.report['code'],'RIG_OPERATION_SCHEMA')
            for body in ({'time':1e9,'head_follow':1,'expressions':{'blink':1}},
                         {'time':0,'head_follow':0,'expressions':{'blink':0}},
                         {'appendages':{'time':120,'seed':2**31-1}},
                         {'appendages':{'time':0,'seed':-2**31}}):
                with self.subTest(operation=operation.__name__,valid=body):
                    actual=operation(self.rig,{**extra,**body})
                    evaluate_deformation(self.rig,actual['pose'] if operation is solve_operation else actual)
        articulated=memory_rig()
        for bone,role in zip(articulated.skeleton['bones'],('tail.1','wing-root.left','index.1.left')):
            bone['role']=role
        for operation,extra in ((pose_operation,{}),(solve_operation,{'chain':'arm','target':[1,1,0]})):
            for amount in (-1,1):
                controls=dict(fingers=dict(side='left',fingers=['index'],curl=(amount+1)/2,spread=amount),
                    appendages=dict(time=120,tail_curl=amount,tail_wag=amount,wing_flap=amount,
                        wing_fold=amount,wing_twist=amount,secondary=amount,wing_side='left'))
                before=deepcopy(controls)
                with self.subTest(operation=operation.__name__,articulated=amount):
                    actual=operation(articulated,{**extra,**controls})
                    pose=actual['pose'] if operation is solve_operation else actual
                    self.assertTrue(np.isfinite(evaluate_deformation(articulated,pose)['strip']['positions']).all())
                    self.assertTrue(pose['bones'])
                    self.assertEqual(controls,before)

    def test_each_articulated_control_has_its_own_finite_geometry_effect(self):
        rig=articulated_rig()
        cases=[({'fingers':{'side':'left','curl':.7}},'index.1.left'),
               ({'fingers':{'side':'left','spread':.8,'fingers':['index','middle']}},'index.1.left')]
        cases += [({'appendages':{'time':.25,'seed':3,field:.7}},mesh) for field,mesh in
                  (('tail_curl','tail.1'),('tail_wag','tail.1'),('secondary','tail.1'),
                   ('wing_flap','wing-root.left'),('wing_fold','wing-root.left'),('wing_twist','wing-root.left'))]
        for operation,extra in ((pose_operation,{}),(solve_operation,{'chain':'arm','target':[1,1,0]})):
            baseline=operation(rig,extra)
            baseline=evaluate_deformation(rig,baseline['pose'] if operation is solve_operation else baseline)
            for controls,mesh in cases:
                before=deepcopy(controls)
                with self.subTest(operation=operation.__name__,controls=controls):
                    actual=operation(rig,{**extra,**controls})
                    geometry=evaluate_deformation(rig,actual['pose'] if operation is solve_operation else actual)
                    self.assertGreater(np.max(np.abs(geometry[mesh]['positions']-baseline[mesh]['positions'])),1e-3)
                    for value in geometry.values():self.assertTrue(np.isfinite(value['positions']).all())
                    unrelated='wing-root.right' if mesh in ('index.1.left','tail.1') else 'tail.1'
                    np.testing.assert_allclose(geometry[unrelated]['positions'],baseline[unrelated]['positions'],atol=1e-12)
                    self.assertEqual(controls,before)

    def test_finger_list_omission_keeps_default_and_explicit_bounded_selection(self):
        rig=articulated_rig()
        for operation,extra in ((pose_operation,{}),(solve_operation,{'chain':'arm','target':[1,1,0]})):
            with self.subTest(operation=operation.__name__):
                controls={'side':'left','curl':.7,'spread':.5}
                default=operation(rig,{**extra,'fingers':controls})
                explicit=operation(rig,{**extra,'fingers':{**controls,'fingers':['thumb','index','middle','ring','pinky']}})
                self.assertEqual(default,explicit)
                selected=operation(rig,{**extra,'fingers':{**controls,'fingers':['index']}})
                pose=selected['pose'] if operation is solve_operation else selected
                self.assertIn('index.1.left',pose['bones']);self.assertNotIn('thumb.1.left',pose['bones'])

    def test_finger_selection_cannot_rotate_eye_or_wing_geometry(self):
        for asset, name, side in ((self.rig, 'eye', 'left'), (articulated_rig(), 'wing-root', 'right')):
            before = deepcopy(asset.skeleton)
            neutral = neutral_pose(asset.skeleton)
            original = deepcopy(neutral)
            for operation, extra in ((pose_operation, {}), (solve_operation, {'chain': 'arm', 'target': [1, 1, 0]})):
                with self.subTest(operation=operation.__name__, anatomy=name):
                    with self.assertRaises(RigError) as refused:
                        operation(asset, {**extra, 'pose': neutral,
                            'fingers': {'side': side, 'curl': .7, 'fingers': [name]}})
                    self.assertEqual(refused.exception.report['code'], 'RIG_FINGER_UNAVAILABLE')
                    self.assertEqual(neutral, original)
                    self.assertEqual(asset.skeleton, before)

    def test_ik_default_pole_and_unit_orientation_use_native_solver(self):
        parameters={'chain':'arm','target':[1,1,0]}
        before=deepcopy(parameters)
        default=solve_operation(self.rig,parameters)
        explicit=solve_operation(self.rig,{**parameters,'pole':[0,0,1],'orientation':[0,0,0,1]})
        nulls=solve_operation(self.rig,{**parameters,'pole':None,'orientation':None})
        self.assertEqual(default,nulls)
        for actual in (default,explicit):
            world=evaluate_fk(self.rig.skeleton,actual['pose'])
            np.testing.assert_allclose(world['b2'][:3,3],[1,1,0],atol=1e-6)
            self.assertGreater(world['b1'][2,3],.1)
            self.assertTrue(np.isfinite(evaluate_deformation(self.rig,actual['pose'])['strip']['positions']).all())
        np.testing.assert_allclose(evaluate_fk(self.rig.skeleton,explicit['pose'])['b2'][:3,:3],np.eye(3),atol=1e-6)
        self.assertEqual(parameters,before)

    def test_null_gaze_releases_owned_default_through_pose_and_ik(self):
        from test_rig_eyelid_final_pose import final_pose_fixture,gaze_target
        rig=final_pose_fixture()
        active=pose_operation(rig,{'gaze':gaze_target(.6)})
        active['morphs']['lid.right']={'lift':.7};before=deepcopy(active)
        for operation,extra in ((pose_operation,{}),(solve_operation,{'chain':'chain','target':[.1,1,0]})):
            with self.subTest(operation=operation.__name__):
                actual=operation(rig,{'pose':active,'gaze':None,**extra})
                if operation is solve_operation:actual=actual['pose']
                self.assertNotIn('lid.left',actual['morphs'])
                self.assertEqual(actual['morphs']['lid.right']['lift'],.7)
                explicit=deepcopy(actual);explicit['morphs']['lid.left']={'lift':.25}
                np.testing.assert_allclose(evaluate_deformation(rig,actual)['lid.left']['positions'],
                                           evaluate_deformation(rig,explicit)['lid.left']['positions'])
        self.assertEqual(active,before)

    def test_nested_names_and_vector_numbers_reject_without_mutating_input(self):
        invalid=[{'expressions':{'':0}},{'expressions':{'x'*4097:0}},
                 {'expressions':{str(i):0 for i in range(129)}},
                 {'fingers':{'side':'left','fingers':['x'*129]}},
                 {'fingers':{'side':'left','fingers':['x'+str(i) for i in range(33)]}},
                 {'fingers':{'side':[]}},{'gaze':[0,0,1e9+1]},
                 {'gaze':[0,0,float('inf')]},{'gaze':[0,0,'1']}]
        for body in invalid:
            parameters={'pose':neutral_pose(self.rig.skeleton),**body}
            before=deepcopy(parameters)
            with self.subTest(fields=list(body)),self.assertRaises(RigError):pose_operation(self.rig,parameters)
            self.assertEqual(parameters,before)
        for field,size in (('target',3),('pole',3),('orientation',4)):
            for value in (False,[],[0]*(size-1),[0]*(size-1)+[True],
                          [0]*(size-1)+[float('inf')],[0]*(size-1)+[10**10000]):
                with self.subTest(field=field,type=type(value).__name__),self.assertRaises(RigError):
                    solve_operation(self.rig,{'chain':'arm','target':[1,1,0],field:value})

    def test_native_request_object_and_missing_ik_fields_reject(self):
        for operation in (pose_operation,solve_operation):
            for value in (None,False,0,'',[]):
                with self.subTest(operation=operation.__name__,value=value),self.assertRaises(RigError):
                    operation(self.rig,value)
        for parameters in ({'chain':'arm'},{'target':[1,1,0]},
                           {'chain':'','target':[1,1,0]},{'chain':'x'*129,'target':[1,1,0]}):
            with self.subTest(parameters=parameters),self.assertRaises(RigError):solve_operation(self.rig,parameters)


if __name__=='__main__':unittest.main()
