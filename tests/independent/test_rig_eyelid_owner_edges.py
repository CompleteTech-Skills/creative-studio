"""Offline controls against the exact review tree, using native synthetic geometry."""
from copy import deepcopy
import unittest
import tempfile
from pathlib import Path

import numpy as np
from rig_contracts import validate_contract
from rig_facial import validate_eyelid_bindings
from rig_math import axis_angle, quaternion, qmatrix
from rig_operations import pose_operation, solve_operation
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from test_rig_specializations import eyelid_fixture

def fixture():
    rig=eyelid_fixture()
    rig.geometry.materials=[{}]
    return rig

def admit(rig):
    from rig_models import validate_geometry
    validate_geometry(rig.geometry)
    validate_skeleton(rig.skeleton)
    validate_contract(rig.manifest['facial'], 'FacialController')
    validate_eyelid_bindings(rig)
    for mesh in rig.geometry.meshes:
        validate_skin(rig.weights[mesh.id], mesh, rig.skeleton)

class EyelidControls(unittest.TestCase):
    def test_locked_normalized_rest(self):
        for operation in ('pose', 'ik'):
            with self.subTest(operation=operation):
                rig=fixture()
                rig.manifest['controllers']['ik']=[dict(schema_version=1,id='chain',bones=['root','head','eye.right'],solver='two-bone',pole=[0.,1.,2.],iterations=8,tolerance=.001)]
                eye=next(b for b in rig.skeleton['bones'] if b['id']=='eye.left')
                rest=axis_angle([1.,2.,3.],2.89)
                eye['rest']['rotation']=rest.tolist()
                rig.manifest['facial']['eyelids'][0].update(up_radians=1e-16,down_radians=1e-16)
                rig.manifest['controllers']['constraints']=[dict(schema_version=1,id='stop',type='hinge',bone='eye.left',axis=[1.,0.,0.],minimum=0.,maximum=0.)]
                admit(rig)
                world=evaluate_fk(rig.skeleton)
                args=dict(gaze=(world['eye.left'][:3,3]+[0.,1.,2.]).tolist())
                if operation=='pose':
                    actual=pose_operation(rig,args)
                else:
                    solved=solve_operation(rig,dict(**args,chain='chain',target=world['eye.right'][:3,3].tolist()))
                    actual=solved['pose']
                    self.assertEqual(actual,solved['report']['pose'])
                q=actual['bones']['eye.left']['rotation']
                np.testing.assert_array_equal(q,quaternion(rest))
                expected=deepcopy(actual)
                expected['morphs']['lid.left']['lift']=rig.geometry.by_id()['lid.left'].morph_defaults['lift']
                d=np.max(np.linalg.norm(evaluate_deformation(rig,actual)['lid.left']['positions']-evaluate_deformation(rig,expected)['lid.left']['positions'],axis=1))
                weight=actual['morphs']['lid.left']['lift']
                print(dict(operation=operation, normalized_rest_exact=True, max_FK_orientation_matrix_difference=float(np.max(abs(qmatrix(q)-qmatrix(rest)))),actual_weight=weight,authored_default=.25,max_vertex_error_m=float(d)),flush=True)
                self.assertEqual((weight,d),(.25,0.))

    def test_yaw_only_with_scaled_parent(self):
        rig=fixture()
        head=next(b for b in rig.skeleton['bones'] if b['id']=='head')
        root=next(b for b in rig.skeleton['bones'] if b['id']=='root')
        root['rest']['scale']=[1.,1e5,1.]
        head['rest']['translation'][1]=1e-5
        head['rest']['scale']=[1.,2e5,1.]
        world=evaluate_fk(rig.skeleton)
        for mesh in rig.geometry.meshes:
            rig.weights[mesh.id].inverse_bind=np.linalg.inv(world['head'])[None,:,:]
        admit(rig)
        params=dict(gaze=(world['eye.left'][:3,3]+[1.,0.,2.]).tolist())
        unbound=deepcopy(rig)
        del unbound.manifest['facial']['eyelids']
        reference=pose_operation(unbound,params)
        self.assertGreater(abs(reference['bones']['eye.left']['rotation'][1]),.1)
        self.assertEqual(reference['bones']['eye.left']['rotation'][0],0.)
        self.assertEqual(reference['bones']['eye.left']['rotation'][2],0.)
        print('unbound final pure yaw',reference['bones']['eye.left']['rotation'],flush=True)
        actual=pose_operation(rig,params)
        self.assertEqual(actual['bones'],reference['bones'])
        self.assertEqual(actual['morphs']['lid.left']['lift'],.25)
        np.testing.assert_array_equal(evaluate_deformation(rig,actual)['lid.left']['positions'],evaluate_deformation(unbound,reference)['lid.left']['positions'])
    def test_surviving_invalid_binding_preserves_active_manifest(self):
        from rig_demo import create_demo
        from rig_errors import RigError, atomic_json
        from rig_operations import mutate
        with tempfile.TemporaryDirectory() as scratch:
            rig,_=create_demo(Path(scratch)/'human','humanoid',animate=False)
            seed=dict(eye='eye.left',mesh='eye-white-left',morph='unavailable',up_weight=.8,down_weight=.1,up_radians=.25,down_radians=.3)
            rig.manifest['facial']['eyelids']=[seed]
            validate_contract(rig.manifest['facial'],'FacialController')
            active=rig.root/'rig.json'
            atomic_json(active,rig.manifest)
            snapshot=active.read_bytes()
            with self.assertRaises(RigError) as failure:
                mutate(rig.root,'auto',dict(replace=True,template='humanoid'))
            self.assertEqual(failure.exception.report['code'],'RIG_EYELID_MORPH')
            self.assertEqual(active.read_bytes(),snapshot)
    def test_contract_requires_closed_binding_but_accepts_omission(self):
        from rig_errors import RigError
        rig=fixture()
        facial=deepcopy(rig.manifest['facial'])
        configured=facial.pop('eyelids')
        validate_contract(facial,'FacialController')
        for field in configured[0]:
            bad=deepcopy(configured)
            del bad[0][field]
            with self.subTest(missing=field),self.assertRaises(RigError):
                validate_contract({**facial,'eyelids':bad},'FacialController')
        with self.assertRaises(RigError):
            validate_contract({**facial,'eyelids':[{**configured[0],'extra':True}]},'FacialController')
if __name__=='__main__':
    unittest.main(verbosity=2)
