"""Real two-hand object grips share one owner and retain authored pose versions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np

from rig_attachments import socket_world,attach_prop,detach_prop,prop_world
from rig_demo import create_demo
from rig_errors import RigError
from rig_grips import create_grip_pose,solve_grip_pose,validate_grip_pose,grip_pose_schema
from rig_math import decompose
from rig_skeleton import neutral_pose
from rig_skinning import evaluate_deformation


class TwoHandGripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.rig,_=create_demo(Path(cls.temp.name)/'human','humanoid',animate=False)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def recipe(self,rig=None):
        rig=rig or self.rig;pose=neutral_pose(rig.skeleton)
        prop=dict(id='bar',owner=None,socket=None,transform=decompose(np.eye(4)))
        grips=[]
        for side in ('left','right'):
            matrix=socket_world(rig,pose,'grip.'+side).copy()
            matrix[:3,3]+=[.22 if side=='left' else -.22,.15,.15]
            grips.append(dict(id='hold.'+side,chain='arm.'+side,socket='grip.'+side,transform=decompose(matrix)))
        return pose,prop,create_grip_pose(rig,'bar-grip','1.2.3',prop,grips)

    def test_both_actual_hands_reach_one_object_and_move_bound_geometry(self):
        rig=self.rig;pose,prop,recipe=self.recipe();before=deepcopy((pose,prop,recipe,rig.manifest))
        result=solve_grip_pose(rig,pose,prop,recipe)
        self.assertTrue(result['report']['converged'])
        self.assertLess(result['report']['max_residual_m'],.001)
        self.assertLess(result['report']['max_orientation_residual_rad'],1e-5)
        self.assertEqual(result['report']['recipe_version'],'1.2.3')
        baseline=evaluate_deformation(rig,pose);actual=evaluate_deformation(rig,result['pose'])
        for side in ('left','right'):
            self.assertGreater(np.max(np.abs(actual['hand-'+side]['positions']-baseline['hand-'+side]['positions'])),.05)
        self.assertEqual((pose,prop,recipe,rig.manifest),before)
        self.assertEqual(result['prop'],prop);self.assertFalse(result['report']['ownership_changed'])
        reversed_recipe=deepcopy(recipe);reversed_recipe['grips'].reverse()
        reversed_result=solve_grip_pose(rig,pose,prop,reversed_recipe)
        self.assertEqual(reversed_result['pose'],result['pose'])
        self.assertEqual(reversed_result['report']['contacts'],result['report']['contacts'])

    def test_recipe_roundtrip_is_strict_versioned_and_hash_bound(self):
        pose,prop,recipe=self.recipe();loaded=json.loads(json.dumps(recipe))
        self.assertEqual(validate_grip_pose(self.rig,loaded),recipe)
        from jsonschema import Draft202012Validator
        self.assertFalse(list(Draft202012Validator(grip_pose_schema()).iter_errors(loaded)))
        self.assertEqual(json.loads((Path(__file__).resolve().parents[1]/'profiles/rig-grip-pose.schema.json').read_text()),grip_pose_schema())
        for mutate in (lambda r:r.update(version='bad'),lambda r:r.update(rig_version='9.0.0'),
                       lambda r:r.update(rig_sha256='0'*64),lambda r:r.update(extra=True),
                       lambda r:r['grips'][0].update(transform={}),
                       lambda r:r['object']['transform'].update(scale=[2,1,1]),
                       lambda r:r['grips'][0]['transform'].update(rotation=[0,0,0,2]),
                       lambda r:r.update(position_tolerance_m=float('nan'))):
            bad=deepcopy(recipe);mutate(bad)
            with self.subTest(recipe=bad),self.assertRaises(RigError):solve_grip_pose(self.rig,pose,prop,bad)

    def test_reference_and_coupled_limb_admission_rejects_unsupported_recipes(self):
        pose,prop,recipe=self.recipe()
        for mutate in (lambda r:r['grips'][0].update(socket='missing'),
                       lambda r:r['grips'][0].update(chain='missing'),
                       lambda r:r['grips'][0].update(socket=r['grips'][1]['socket']),
                       lambda r:r['grips'][0].update(chain=r['grips'][1]['chain']),
                       lambda r:r['grips'][0].update(id=r['grips'][1]['id']),
                       lambda r:r['grips'][0].update(socket='grip.right')):
            bad=deepcopy(recipe);mutate(bad)
            with self.subTest(recipe=bad),self.assertRaises(RigError):validate_grip_pose(self.rig,bad)
        rig=deepcopy(self.rig)
        left=next(c for c in rig.manifest['controllers']['ik'] if c['id']=='arm.left')
        right=next(c for c in rig.manifest['controllers']['ik'] if c['id']=='arm.right')
        right['bones']=list(left['bones'])
        next(s for s in rig.manifest['attachments'] if s['id']=='grip.right')['bone']='hand.left'
        from rig_models import rig_signature
        bad=deepcopy(recipe);bad['rig_sha256']=rig_signature(rig.manifest)
        with self.assertRaises(RigError) as failure:validate_grip_pose(rig,bad)
        self.assertEqual(failure.exception.report['code'],'RIG_GRIP_COUPLED')

    def test_prop_identity_owner_and_world_changes_are_not_claimed(self):
        pose,prop,recipe=self.recipe()
        for mutate in (lambda p:p.update(id='different'),lambda p:p.update(owner='other'),
                       lambda p:p['transform']['translation'].__setitem__(0,.1)):
            changed=deepcopy(prop);mutate(changed);snapshot=deepcopy(changed)
            with self.subTest(prop=changed),self.assertRaises(RigError):solve_grip_pose(self.rig,pose,changed,recipe)
            self.assertEqual(changed,snapshot)
        with self.assertRaises(RigError):create_grip_pose(self.rig,'x','1.0.0',dict(prop,owner='other'),recipe['grips'],expected_owner='other')
        result=solve_grip_pose(self.rig,pose,prop,recipe)
        active=attach_prop(self.rig,result['pose'],result['prop'],'grip.left',expected_owner=None)
        self.assertEqual(active['owner'],self.rig.manifest['character']['id'])
        np.testing.assert_allclose(prop_world(self.rig,result['pose'],active),np.eye(4),atol=1e-8)
        released=detach_prop(self.rig,result['pose'],active);self.assertIsNone(released['owner'])

    def test_owned_prop_requires_explicit_snapshot_and_retains_its_single_owner(self):
        pose,prop,recipe=self.recipe()
        result=solve_grip_pose(self.rig,pose,prop,recipe)
        owned=attach_prop(self.rig,result['pose'],prop,'grip.left',expected_owner=None)
        owner=self.rig.manifest['character']['id']
        with self.assertRaises(RigError):create_grip_pose(self.rig,'held','1.0.0',owned,recipe['grips'],expected_owner=owner)
        authored=create_grip_pose(self.rig,'held','1.0.0',owned,recipe['grips'],expected_owner=owner,
                                  object_transform=decompose(np.eye(4)))
        held=solve_grip_pose(self.rig,result['pose'],owned,authored)
        self.assertEqual(held['prop'],owned);self.assertEqual(held['report']['owner'],owner)
        self.assertTrue(held['report']['converged'])
        for bad in ({},'invalid',dict(owned,socket=None),dict(owned,transform=None),
                    dict(owned,transform='bad'),dict(owned,transform={'translation':[0,0,0]})):
            with self.subTest(prop=bad),self.assertRaises(RigError):solve_grip_pose(self.rig,result['pose'],bad,authored)

    def test_owned_prop_motion_cannot_falsely_accept_frozen_world_grips(self):
        pose,prop,recipe=self.recipe()
        owned=attach_prop(self.rig,pose,prop,'grip.left',expected_owner=None)
        authored=create_grip_pose(self.rig,'moving-held','1.0.0',owned,recipe['grips'],
            expected_owner=self.rig.manifest['character']['id'],object_transform=decompose(np.eye(4)))
        with self.assertRaises(RigError) as failure:solve_grip_pose(self.rig,pose,owned,authored)
        self.assertEqual(failure.exception.report['code'],'RIG_TWO_HAND_GRIP')
        result=solve_grip_pose(self.rig,pose,owned,authored,require_success=False)
        self.assertFalse(result['report']['converged'])
        self.assertGreater(result['report']['object_residual_m'],.3)
        self.assertGreater(result['report']['max_residual_m'],.3)
        np.testing.assert_allclose(prop_world(self.rig,result['pose'],owned)[:3,3],[.22,.15,.15],atol=1e-10)

    def test_impossible_target_and_constraints_report_both_final_contacts(self):
        pose,prop,recipe=self.recipe();bad=deepcopy(recipe)
        bad['grips'][0]['transform']['translation']=[100.,100.,100.]
        snapshot=deepcopy((pose,prop,bad))
        result=solve_grip_pose(self.rig,pose,prop,bad,require_success=False)
        self.assertFalse(result['report']['converged']);self.assertEqual(len(result['report']['contacts']),2)
        self.assertGreater(result['report']['max_residual_m'],100.)
        with self.assertRaises(RigError) as failure:solve_grip_pose(self.rig,pose,prop,bad)
        self.assertEqual(failure.exception.report['code'],'RIG_TWO_HAND_GRIP')
        self.assertEqual((pose,prop,bad),snapshot)
        rig=deepcopy(self.rig)
        rig.manifest['controllers']['constraints'].append(dict(schema_version=1,id='freeze-left',
            bone='hand.left',type='hinge',axis=[1,0,0],minimum=0.,maximum=0.))
        from rig_models import rig_signature
        changed=deepcopy(recipe);changed['rig_sha256']=rig_signature(rig.manifest)
        # An independently authored rotated grip is incompatible with this hinge.
        changed['grips'][0]['transform']['rotation']=[0.,0.,np.sin(.25),np.cos(.25)]
        constrained=solve_grip_pose(rig,pose,prop,changed,require_success=False)
        self.assertFalse(constrained['report']['converged'])
        self.assertGreater(constrained['report']['max_orientation_residual_rad'],.1)


if __name__=='__main__':unittest.main()
