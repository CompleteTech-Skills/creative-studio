from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import numpy as np
from rig_test_helpers import fixture
from rig_demo import create_demo
from rig_facial import apply_expressions,gaze_at,finger_controls,appendage_controls
from rig_attachments import socket_world,attach_prop,detach_prop,transfer_prop,prop_world,solve_grasp
from rig_skeleton import neutral_pose,evaluate_fk,mirror_pose
from rig_skinning import evaluate_deformation
from rig_math import decompose,axis_angle
from rig_errors import RigError
from rig_states import rig_capabilities
from rig_models import Geometry,Mesh,RigAsset,SkinWeights
from rig_skeleton import skeleton_from_heads,validate_skeleton


class RigSpecializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.humanoid,_=create_demo(Path(cls.temp.name)/'human','humanoid',animate=False)
        cls.creature,_=create_demo(Path(cls.temp.name)/'creature','winged-creature',animate=False)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_fingers_gaze_and_morphs_change_actual_vertices(self):
        r=self.humanoid;neutral=neutral_pose(r.skeleton);before=evaluate_deformation(r,neutral)
        pose=finger_controls(r,neutral,side='right',curl=.7,spread=.4)
        after=evaluate_deformation(r,pose)
        self.assertGreater(np.linalg.norm(after['finger-index-right']['positions']-before['finger-index-right']['positions']),.01)
        pose=gaze_at(r,neutral,[.6,1.8,3.],head_follow=.3);world=evaluate_fk(r.skeleton,pose)
        self.assertFalse(np.allclose(world['eye.right'],evaluate_fk(r.skeleton)['eye.right']))
        blink=apply_expressions(r,neutral,{'blink':1.});delta=evaluate_deformation(r,blink)['eye-white-right']['positions']-before['eye-white-right']['positions']
        self.assertGreater(np.max(np.abs(delta)),.05)
    def test_appendages_are_deterministic_seeded_and_capability_checked(self):
        r=self.creature;p=neutral_pose(r.skeleton)
        first=appendage_controls(r,p,time=.3,tail_curl=.5,wing_flap=.8,secondary=.5,seed=17)
        second=appendage_controls(r,p,time=.3,tail_curl=.5,wing_flap=.8,secondary=.5,seed=17)
        self.assertEqual(first,second);self.assertIn('wing-root.right',first['bones'])
        with self.assertRaises(RigError):appendage_controls(self.humanoid,neutral_pose(self.humanoid.skeleton),tail_curl=.5)
        with self.assertRaises(RigError):finger_controls(self.creature,p,side='left',curl=.5)
    def test_controls_follow_anatomical_hierarchy_not_bone_array_order(self):
        human=deepcopy(self.humanoid);human.skeleton['bones'].reverse()
        normal=finger_controls(self.humanoid,neutral_pose(self.humanoid.skeleton),side='right',curl=.4,spread=.7)
        reordered=finger_controls(human,neutral_pose(human.skeleton),side='right',curl=.4,spread=.7)
        self.assertEqual(normal,reordered)
        creature=deepcopy(self.creature);creature.skeleton['bones'].reverse()
        normal=appendage_controls(self.creature,neutral_pose(self.creature.skeleton),time=.3,tail_wag=.6,secondary=.4)
        reordered=appendage_controls(creature,neutral_pose(creature.skeleton),time=.3,tail_wag=.6,secondary=.4)
        self.assertEqual(normal,reordered)
    def test_wing_twist_changes_only_requested_side_and_real_geometry(self):
        rig=self.creature;neutral=neutral_pose(rig.skeleton)
        twisted=appendage_controls(rig,neutral,wing_twist=.8,wing_side='left')
        before=evaluate_deformation(rig,neutral);after=evaluate_deformation(rig,twisted)
        self.assertTrue(any(name.endswith('.left') for name in twisted['bones']))
        rest_world=evaluate_fk(rig.skeleton,neutral);posed_world=evaluate_fk(rig.skeleton,twisted)
        for name in rest_world:
            if name.startswith('wing-') and name.endswith('.right'):
                np.testing.assert_allclose(posed_world[name],rest_world[name],atol=1e-12)
        self.assertGreater(max(np.max(np.abs(after[name]['positions']-data['positions']))
                               for name,data in before.items()),.01)
        for value in (-1.01,1.01,float('nan')):
            with self.assertRaises(RigError):appendage_controls(rig,neutral,wing_twist=value)
        with self.assertRaises(RigError):appendage_controls(rig,neutral,wing_side='front')
        with self.assertRaises(RigError):appendage_controls(self.humanoid,neutral_pose(self.humanoid.skeleton),wing_twist=.5)
    def test_imported_wing_twist_uses_authored_length_not_local_y(self):
        skeleton=skeleton_from_heads([dict(id='wing-root.left',role='wing-root.left',
            parent=None,head=[0,0,0],tail=[1,0,0])])
        skeleton['bones'][0]['rest']['rotation']=[0,0,0,1]
        validate_skeleton(skeleton)
        positions=np.array([[0.,0.,0.],[1.,0.,0.],[.5,.2,0.]])
        mesh=Mesh('wing',positions,np.array([[0,1,2]]))
        weights=SkinWeights('wing',['wing-root.left'],np.zeros((3,1),dtype=np.int64),
                            np.ones((3,1)),np.eye(4)[None,:,:])
        rig=RigAsset({'skeleton':skeleton,'controllers':{'constraints':[]}},Geometry([mesh]),{'wing':weights})
        pose=appendage_controls(rig,neutral_pose(skeleton),wing_twist=.7,wing_side='left')
        actual=evaluate_deformation(rig,pose)['wing']['positions']
        np.testing.assert_allclose(actual[:2],positions[:2],atol=1e-12)
        self.assertGreater(abs(actual[2,2]),.05)
    def test_expression_conflicts_and_unavailable_controls_fail(self):
        r=deepcopy(self.humanoid);r.manifest['facial']['expressions']['blink']['incompatible']=['squint']
        r.manifest['facial']['expressions']['squint']=dict(schema_version=1,morphs={'blink':.5},bones={},incompatible=['blink'])
        with self.assertRaises(RigError):apply_expressions(r,neutral_pose(r.skeleton),{'blink':1.,'squint':1.})
        with self.assertRaises(RigError):apply_expressions(r,neutral_pose(r.skeleton),{'invented-mouth':1.})
    def test_neutral_or_inert_expression_catalog_is_not_a_facial_capability(self):
        r=deepcopy(self.humanoid)
        r.manifest['facial']['expressions']={'neutral':dict(schema_version=1,morphs={},bones={},incompatible=[])}
        self.assertNotIn('expressive-face',rig_capabilities(r))
        r.manifest['facial']['expressions']['blink']=dict(schema_version=1,morphs={'blink':0.},bones={},incompatible=[])
        self.assertNotIn('expressive-face',rig_capabilities(r))
        r.manifest['facial']['expressions']['blink']['morphs']['blink']=1.
        self.assertIn('expressive-face',rig_capabilities(r))
        for mesh in r.geometry.meshes:
            for morph in mesh.morphs.values():
                for values in morph.values():values[:]=0.
        self.assertNotIn('expressive-face',rig_capabilities(r))
    def test_expression_on_unweighted_parent_moves_weighted_descendants(self):
        r=deepcopy(self.humanoid);root=r.skeleton['root']
        used={skin.joints[int(j)] for skin in r.weights.values() for j in set(skin.indices[skin.weights>0].tolist())}
        self.assertNotIn(root,used)
        r.manifest['facial']['expressions']={'move':dict(schema_version=1,morphs={},
            bones={root:{'rotation':axis_angle([0,0,1],.2).tolist()}},incompatible=[])}
        neutral=neutral_pose(r.skeleton)
        before=evaluate_deformation(r,neutral)
        after=evaluate_deformation(r,apply_expressions(r,neutral,{'move':1.}))
        self.assertGreater(max(np.max(np.abs(after[name]['positions']-value['positions']))
                               for name,value in before.items()),.01)
        self.assertIn('expressive-face',rig_capabilities(r))
    def test_antipodal_rest_quaternion_does_not_claim_facial_motion(self):
        r=deepcopy(self.humanoid)
        bone=next(b for b in r.skeleton['bones'] if b['id']=='head')
        r.manifest['facial']['expressions']={'inert':dict(schema_version=1,morphs={},
            bones={'head':{'rotation':(-np.asarray(bone['rest']['rotation'])).tolist()}},incompatible=[])}
        neutral=neutral_pose(r.skeleton)
        before=evaluate_deformation(r,neutral)
        after=evaluate_deformation(r,apply_expressions(r,neutral,{'inert':1.}))
        for name,value in before.items():
            np.testing.assert_allclose(after[name]['positions'],value['positions'],atol=1e-12)
        self.assertNotIn('expressive-face',rig_capabilities(r))
    def test_attach_detach_and_owner_transfer_preserve_world_space(self):
        r=self.humanoid;pose=neutral_pose(r.skeleton);matrix=socket_world(r,pose,'grip.right')
        prop=dict(id='cube',owner=None,socket=None,transform=decompose(matrix))
        attached=attach_prop(r,pose,prop,'grip.right');np.testing.assert_allclose(prop_world(r,pose,attached),matrix,atol=1e-12)
        detached=detach_prop(r,pose,attached);self.assertIsNone(detached['owner']);np.testing.assert_allclose(prop_world(r,pose,detached),matrix,atol=1e-12)
        other=deepcopy(r);other.manifest['character']['id']='other'
        moved=transfer_prop(r,pose,other,pose,attached,'grip.right');self.assertEqual(moved['owner'],'other')
        with self.assertRaises(RigError):attach_prop(r,pose,attached,'grip.left')
        with self.assertRaises(RigError):transfer_prop(r,pose,other,pose,attached,'grip.left')
    def test_object_relative_grasp_meets_actual_socket(self):
        r=self.humanoid;p=neutral_pose(r.skeleton);target=decompose(np.eye(4));target['translation']=[.35,1.25,.45]
        result=solve_grasp(r,p,'arm.right','grip.right',target,decompose(np.eye(4)))
        self.assertTrue(result['reachable']);self.assertLess(result['residual_m'],.001)
    def test_mirroring_requires_explicit_symmetry_approval(self):
        r=self.humanoid;p=finger_controls(r,neutral_pose(r.skeleton),side='right',curl=.4)
        with self.assertRaises(RigError):mirror_pose(r.skeleton,p)
        mirrored=mirror_pose(r.skeleton,p,approved=True);self.assertIn('index.1.left',mirrored['bones'])
