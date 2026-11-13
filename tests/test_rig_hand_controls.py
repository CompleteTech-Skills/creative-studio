"""Represented anatomy is distinct from invented grip and finger capabilities."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import numpy as np
from rig_demo import create_demo
from rig_errors import RigError
from rig_facial import finger_controls
from rig_hand_controls import discover_hand_controls, apply_hand_control
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation


def rename_bones(rig, mapping):
    result = deepcopy(rig)
    for bone in result.skeleton['bones']:
        bone['id'] = mapping.get(bone['id'],bone['id'])
        for key in ('parent','mirror'):bone[key] = mapping.get(bone[key],bone[key])
    result.skeleton['root'] = mapping.get(result.skeleton['root'],result.skeleton['root'])
    for skin in result.weights.values():skin.joints = [mapping.get(name,name) for name in skin.joints]
    for constraint in result.manifest['controllers']['constraints']:
        for key in ('bone','target'):
            if key in constraint:constraint[key] = mapping.get(constraint[key],constraint[key])
    for chain in result.manifest['controllers']['ik']:chain['bones'] = [mapping.get(name,name) for name in chain['bones']]
    for socket in result.manifest['attachments']:socket['bone'] = mapping.get(socket['bone'],socket['bone'])
    return result


class HandControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory();root = Path(cls.temp.name)
        cls.robot,_ = create_demo(root/'robot','robotic',animate=False)
        cls.human,_ = create_demo(root/'human','humanoid',animate=False)
        cls.quad,_ = create_demo(root/'quad','quadruped',animate=False)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_robot_reports_real_travel_and_actual_mesh_motion_without_aperture_claim(self):
        rig = self.robot;report = discover_hand_controls(rig);pose = neutral_pose(rig.skeleton)
        self.assertEqual(report['substitutes']['gripper']['status'],'represented')
        self.assertEqual(report['grip_aperture']['status'],'unavailable')
        slide = next(c for c in report['controls'] if c['kind']=='prismatic-gripper-travel')
        before = deepcopy(pose)
        start = apply_hand_control(rig,pose,slide['id'],amount=0.)
        end = apply_hand_control(rig,pose,slide['id'],amount=1.)
        bone = next(b for b in rig.skeleton['bones'] if b['id']==slide['joint'])
        parent = evaluate_fk(rig.skeleton)[bone['parent']]
        expected = parent[:3,slide['axis']]*(slide['maximum']-slide['minimum'])
        world_a,world_b = evaluate_fk(rig.skeleton,start),evaluate_fk(rig.skeleton,end)
        np.testing.assert_allclose(world_b[bone['id']][:3,3]-world_a[bone['id']][:3,3],expected,atol=1e-12)
        a,b = evaluate_deformation(rig,start),evaluate_deformation(rig,end)
        self.assertGreater(max(np.max(np.linalg.norm(b[name]['positions']-mesh['positions'],axis=1)) for name,mesh in a.items()),.1)
        self.assertEqual(pose,before)

    def test_arbitrary_ids_preserve_semantic_controls_and_do_not_invent_substitutes(self):
        renamed = rename_bones(self.robot,{'gripper':'imported-42','jaw.left':'mitten','jaw.right':'claw'})
        report = discover_hand_controls(renamed)
        self.assertEqual(report['substitutes']['gripper']['joints'],['imported-42'])
        for kind in ('mitten','claw','paw'):self.assertEqual(report['substitutes'][kind]['status'],'unavailable')
        self.assertTrue(all(side['status']=='unavailable' for side in report['fingers'].values()))
        a = evaluate_deformation(self.robot,apply_hand_control(self.robot,neutral_pose(self.robot.skeleton),'slide:gripper-slide',amount=.6))
        b = evaluate_deformation(renamed,apply_hand_control(renamed,neutral_pose(renamed.skeleton),'slide:gripper-slide',amount=.6))
        for name in a:np.testing.assert_allclose(a[name]['positions'],b[name]['positions'],atol=1e-12)

    def test_real_finger_chains_reuse_native_controller_without_reapplying_constraints(self):
        rig = self.human;report = discover_hand_controls(rig)
        self.assertEqual(set(report['fingers']['left']['chains']),{'thumb','index','middle','ring','pinky'})
        pose = neutral_pose(rig.skeleton)
        actual = apply_hand_control(rig,pose,'fingers:left',curl=.6,spread=.2)
        expected = finger_controls(rig,pose,side='left',curl=.6,spread=.2)
        self.assertEqual(actual,expected)
        a,b = evaluate_deformation(rig,pose),evaluate_deformation(rig,actual)
        self.assertGreater(max(np.max(np.abs(b[name]['positions']-mesh['positions'])) for name,mesh in a.items()),.005)

    def test_paws_are_represented_and_missing_mitten_claw_remain_unavailable(self):
        report = discover_hand_controls(self.quad)
        self.assertEqual(len(report['substitutes']['paw']['joints']),4)
        self.assertTrue(report['substitutes']['paw']['sockets'])
        self.assertTrue(report['substitutes']['paw']['ik_chains'])
        self.assertEqual(report['substitutes']['mitten']['status'],'unavailable')
        self.assertEqual(report['substitutes']['claw']['status'],'unavailable')
        with self.assertRaises(RigError):apply_hand_control(self.quad,neutral_pose(self.quad.skeleton),'fingers:left',curl=.5)

    def test_unweighted_semantic_leaf_and_ambiguous_actuator_do_not_claim_support(self):
        rig = deepcopy(self.robot);extra = deepcopy(next(b for b in rig.skeleton['bones'] if b['id']=='jaw.left'))
        extra.update(id='unweighted-leaf',role='mitten');rig.skeleton['bones'].append(extra)
        report = discover_hand_controls(rig)
        self.assertEqual(report['substitutes']['mitten']['status'],'unavailable')
        slide = next(c for c in rig.manifest['controllers']['constraints'] if c['id']=='gripper-slide')
        slide['axes'] = [0,1]
        self.assertFalse(any(c['kind']=='prismatic-gripper-travel' for c in discover_hand_controls(rig)['controls']))

    def test_invalid_parameters_pose_and_missing_bindings_fail_without_mutation(self):
        rig = self.robot;pose = neutral_pose(rig.skeleton);original = deepcopy(pose)
        for value in (float('nan'),float('inf'),True,-.1,1.1,'one',10**1000,-10**1000):
            with self.subTest(value=value),self.assertRaises(RigError):apply_hand_control(rig,pose,'slide:gripper-slide',amount=value)
        with self.assertRaises(RigError):apply_hand_control(rig,pose,'slide:gripper-slide',unknown=1.)
        bad = deepcopy(pose);bad['bones']['missing'] = {'translation':[0,0,0]}
        with self.assertRaises(RigError):apply_hand_control(rig,bad,'slide:gripper-slide',amount=.5)
        bad = deepcopy(pose);bad['rig_version'] = '999.0.0'
        with self.assertRaises(RigError):apply_hand_control(rig,bad,'slide:gripper-slide',amount=.5)
        partial = deepcopy(rig);partial.weights.pop(next(iter(partial.weights)))
        self.assertFalse(discover_hand_controls(partial)['skin_complete'])
        with self.assertRaises(RigError):apply_hand_control(partial,pose,'slide:gripper-slide',amount=.5)
        self.assertEqual(pose,original)


if __name__=='__main__':unittest.main()
