"""Executable camera constraints against real Scene3D bounds and native rigs."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from camera3d_constraints import (ConstraintError, calculate_focus_distance, evaluate_camera_constraints,
    frame_subject, inspect_framing, look_at_world, rack_focus, track_subject)
from camera3d_scene_adapter import SceneTargets, TargetError
from rig_math import compose, axis_angle
from scene3d_contracts import canonical_bytes
from scene3d_example import node
from test_rig_camera3d_contracts import fixtures


def subject(position=(0,0,-5), half=(1,1,1)):
    p=np.asarray(position); h=np.asarray(half)
    return dict(id='actor', position=p.tolist(),world_matrix=compose(p).tolist(),
                bounds=dict(minimum=(p-h).tolist(),maximum=(p+h).tolist()))


class CameraConstraintTests(unittest.TestCase):
    def setUp(self):
        self.docs=fixtures();self.optics=self.docs['optics'];self.render=self.docs['render']

    def test_pole_fallback_is_proper_and_reproducible(self):
        for point in ([0,1,0],[0,-1,0],[0,1,1e-10]):
            a,report=look_at_world(np.eye(4),point)
            b,_=look_at_world(np.eye(4),point)
            np.testing.assert_array_equal(a,b)
            np.testing.assert_allclose(a[:3,:3].T@a[:3,:3],np.eye(3),atol=1e-10)
            self.assertAlmostEqual(np.linalg.det(a[:3,:3]),1)
            np.testing.assert_allclose(-a[:3,2],np.asarray(point)/np.linalg.norm(point),atol=1e-9)
            self.assertTrue(report['pole_fallback'])
        with self.assertRaisesRegex(ConstraintError,'coincide'):
            look_at_world(np.eye(4),[0,0,0])

    def test_focus_uses_rotated_camera_optical_depth_not_euclidean_distance(self):
        world=compose([3,2,1],axis_angle([0,1,0],np.pi/2))
        # Camera looks along world -X, with a lateral 10m offset.
        result=calculate_focus_distance(world,[subject([-2,2,11])])
        self.assertAlmostEqual(result['distance_m'],5)
        np.testing.assert_allclose(result['plane_point'],[-2,2,1],atol=1e-12)
        with self.assertRaisesRegex(ConstraintError,'behind'):
            calculate_focus_distance(world,[subject([4,2,1])])

    def test_weighted_focus_and_rack_exact_endpoints(self):
        result=calculate_focus_distance(np.eye(4),[subject([0,0,-2]),subject([0,0,-8])],weights=[3,1])
        self.assertEqual(result['distance_m'],3.5)
        self.assertEqual(rack_focus(2,8,0),2)
        self.assertEqual(rack_focus(2,8,1),8)
        self.assertEqual(rack_focus(2,8,.5),5)
        for bad in [True,float('nan'),-1,2]:
            with self.assertRaises(ConstraintError):rack_focus(2,8,bad)
        with self.assertRaises(ConstraintError):calculate_focus_distance(np.eye(4),[subject()],weights=[0])

    def test_perspective_fits_real_group_bounds_and_preserves_lens(self):
        targets=[subject([-2,0,-5]),subject([2,1,-7],half=[.5,2,.5])]
        result=frame_subject(np.eye(4),self.optics,self.render,targets,safe_rect=[.15,.1,.85,.9])
        self.assertTrue(result['framing']['satisfied'])
        self.assertEqual(result['optics'],self.optics)
        report=inspect_framing(result['world_matrix'],result['optics'],self.render,targets,safe_rect=[.15,.1,.85,.9])
        self.assertTrue(report['satisfied'])
        with self.assertRaises(ConstraintError):
            frame_subject(np.eye(4),self.optics,self.render,targets,distance_range_m=[.1,.2])
        with self.assertRaisesRegex(ConstraintError,'actual target bounds'):
            frame_subject(np.eye(4),self.optics,self.render,[dict(position=[0,0,-5])])

    def test_true_orthographic_framing_changes_scale_and_preserves_depth_independence(self):
        self.optics['projection']=dict(type='orthographic',orthographic_height_m=1,shift_x=0,shift_y=0,near_clip_m=.1,far_clip_m=100)
        a=frame_subject(np.eye(4),self.optics,self.render,[subject()],safe_rect=[.1,.1,.9,.9])
        self.assertAlmostEqual(a['optics']['projection']['orthographic_height_m'],2.5,places=6)
        self.assertTrue(a['framing']['satisfied'])
        shifted=deepcopy(self.optics);shifted['projection']['shift_x']=2
        shifted_result=frame_subject(np.eye(4),shifted,self.render,[subject()])
        self.assertTrue(shifted_result['framing']['satisfied'])

    def test_clipped_near_plane_bounds_do_not_falsely_pass_framing(self):
        report=inspect_framing(np.eye(4),self.optics,self.render,[subject([0,0,-.05],half=[.01,.01,.01])])
        self.assertFalse(report['satisfied']);self.assertIn('near',report['violations'])
        report=inspect_framing(np.eye(4),self.optics,self.render,[subject([0,0,0])])
        self.assertFalse(report['satisfied'])

    def test_order_is_stable_and_duplicate_rejected(self):
        a=deepcopy(self.docs['constraint']);a['id']='a';a['type']='look-at'
        b=deepcopy(a);b['id']='b';b['type']='focus';b['priority']=2
        resolve=lambda _:subject([1,0,-5])
        first=evaluate_camera_constraints(np.eye(4),self.optics,self.render,[b,a],resolve)
        second=evaluate_camera_constraints(np.eye(4),self.optics,self.render,[a,b],resolve)
        self.assertEqual(first,second)
        self.assertEqual([d['id'] for d in first['diagnostics']],['a','b'])
        with self.assertRaisesRegex(ConstraintError,'Duplicate'):
            evaluate_camera_constraints(np.eye(4),self.optics,self.render,[a,a],resolve)

    def test_unavailable_collision_is_explicit_and_warning_does_not_claim_satisfied(self):
        c=deepcopy(self.docs['constraint']);c.update(type='collision',targets=[])
        with self.assertRaisesRegex(ConstraintError,'not supplied'):
            evaluate_camera_constraints(np.eye(4),self.optics,self.render,[c],lambda _:None)
        c['failure']='warn'
        result=evaluate_camera_constraints(np.eye(4),self.optics,self.render,[c],lambda _:None)
        self.assertEqual(result['diagnostics'][0]['status'],'unsatisfied')
        result=evaluate_camera_constraints(np.eye(4),self.optics,self.render,[c],lambda _:None,collision=lambda w,r:dict(clear=True))
        self.assertEqual(result['diagnostics'][0]['status'],'satisfied')

    def test_later_rotation_cannot_silently_break_framing(self):
        frame=deepcopy(self.docs['constraint']);frame.update(id='frame',type='frame')
        look=deepcopy(frame);look.update(id='later',type='look-at',priority=2)
        look['targets'][0]['id']='different'
        def resolve(ref):
            return subject([100,0,-5]) if ref['id']=='different' else subject()
        with self.assertRaisesRegex(ConstraintError,'invalidates'):
            evaluate_camera_constraints(np.eye(4),self.optics,self.render,[frame,look],resolve)

    def test_follow_is_independent_of_history_and_has_explicit_offset(self):
        target=subject([2,3,-4])
        first=track_subject(np.eye(4),target,offset_m=[0,0,3],fixed_height_m=2)
        np.testing.assert_allclose(first[:3,3],[2,2,-1])
        np.testing.assert_array_equal(first,track_subject(np.eye(4),target,offset_m=[0,0,3],fixed_height_m=2))
        with self.assertRaises(ConstraintError):track_subject(np.eye(4),target,offset_m=[0,0,0],distance_range_m=[1,2])

    def test_position_follow_accepts_scaled_subject_without_inventing_rotation(self):
        target=subject([2,3,-4]);target['world_matrix']=compose([2,3,-4],scale=[2,3,4]).tolist()
        result=track_subject(np.eye(4),target,offset_m=[0,0,3])
        np.testing.assert_allclose(result[:3,3],[2,3,-1])
        with self.assertRaises(ValueError):
            track_subject(np.eye(4),target,follow_rotation=True)

    def test_follow_rechecks_distance_after_height_and_weight(self):
        target=subject([0,0,-5])
        with self.assertRaisesRegex(ConstraintError,'conflicts'):
            track_subject(np.eye(4),target,offset_m=[0,0,2],fixed_height_m=10,distance_range_m=[1,3])
        with self.assertRaisesRegex(ConstraintError,'conflicts'):
            track_subject(np.eye(4),target,offset_m=[0,0,2],weight=.1,distance_range_m=[1,3])

    def test_invalid_safe_rectangle_fails_before_anchor_access(self):
        for safe_rect in ([],[0,0,1], [0,0,0,1], [0,0,2,1]):
            with self.assertRaises(ConstraintError):
                frame_subject(np.eye(4),self.optics,self.render,[subject()],safe_rect=safe_rect)

    def test_later_aim_behind_focus_respects_warning_policy(self):
        focus=deepcopy(self.docs['constraint']);focus.update(id='focus',type='focus',failure='warn')
        look=deepcopy(focus);look.update(id='look',type='look-at',priority=2,failure='reject')
        look['targets'][0]['id']='behind'
        result=evaluate_camera_constraints(np.eye(4),self.optics,self.render,[focus,look],
            lambda ref:subject([0,0,5]) if ref['id']=='behind' else subject())
        self.assertEqual(result['diagnostics'][-1]['status'],'invalidated-by-later-constraint')


class NativeSceneTargetTests(unittest.TestCase):
    def setUp(self):
        import json
        self.scene=json.loads((ROOT/'examples/scene3d-contracts/example-scene.json').read_text())
        root=node('root');root['transform']['translation']=[4,0,0]
        child=node('actor',parent='root');child['transform']['translation']=[0,2,-5]
        child['bounds']=dict(minimum=[-1,-2,-1],maximum=[1,2,1])
        self.scene['nodes']=[root,child];self.scene['roots']=['root'];self.scene['cameras']=[];self.scene['lights']=[]
        self.scene['dependencies']=[]
        self.adapter=SceneTargets.from_scene(self.scene)
        self.target=fixtures()['target'];self.target['scene']=deepcopy(self.adapter.reference)

    def test_real_parent_world_bounds_and_offsets(self):
        result=self.adapter.resolve(self.target)
        np.testing.assert_allclose(result['position'],[4,3,-5])
        self.assertEqual(result['bounds'],dict(minimum=[3.,0.,-6.],maximum=[5.,4.,-4.]))
        self.scene['nodes'][0]['transform']['translation'][0]=50
        np.testing.assert_allclose(self.adapter.resolve(self.target)['position'],[4,3,-5])

    def test_exact_scene_source_and_missing_targets(self):
        self.target['scene']['sha256']='a'*64
        with self.assertRaisesRegex(TargetError,'reference differs'):self.adapter.resolve(self.target)
        self.target['scene']=self.adapter.reference;self.target['node']='missing'
        with self.assertRaisesRegex(TargetError,'Missing'):self.adapter.resolve(self.target)
        with self.assertRaises(TargetError):SceneTargets(self.scene,self.adapter.states,scene_path='scene.json',scene_bytes=b'{}')

    def test_native_rig_bone_and_socket_are_actual_fk(self):
        from rig_models import RigAsset,Geometry
        from rig_skeleton import skeleton_from_heads,neutral_pose
        skeleton=skeleton_from_heads([dict(id='root',parent=None,head=[0,0,0],tail=[0,1,0],role='root')])
        pose=neutral_pose(skeleton);pose['bones']['root']={'translation':[0,3,0]}
        rig=RigAsset(dict(skeleton=skeleton,attachments=[dict(id='eye',bone='root',transform=dict(translation=[0,.5,0],rotation=[0,0,0,1],scale=[1,1,1]))]),Geometry([]))
        self.adapter.rigs={'actor':(rig,pose)}
        self.target.update(space='bone',attachment='root',offset_m=[0,0,0])
        np.testing.assert_allclose(self.adapter.resolve(self.target)['position'],[4,5,-5])
        self.target.update(space='socket',attachment='eye')
        np.testing.assert_allclose(self.adapter.resolve(self.target)['position'],[4,5.5,-5])
        self.target['attachment']='absent'
        with self.assertRaisesRegex(TargetError,'Missing'):self.adapter.resolve(self.target)


if __name__=='__main__':unittest.main()


class CollisionResponseUnitTests(unittest.TestCase):
    """Analytical plane oracle tests response policy, not native mesh acceptance."""
    class PlaneOracle:
        # Free space x<0, outward normal -X; exact swept sphere/plane geometry.
        def sweep_sphere(self,start,end,radius):
            if start[0]>=-radius:
                return dict(fraction=0.,normal=[-1.,0.,0.],node='plane')
            if end[0]<-radius:
                return None
            fraction=(-radius-start[0])/(end[0]-start[0])
            return dict(fraction=fraction,normal=[-1.,0.,0.],node='plane')

    def test_stop_and_slide_policies_have_verified_clear_endpoints(self):
        from camera3d_constraints import resolve_camera_collision
        geometry=self.PlaneOracle()
        stopped=resolve_camera_collision([-2,0,0],[2,2,0],.1,geometry,policy='stop')
        self.assertTrue(stopped['clear']);self.assertLess(stopped['position'][0],-.1)
        slid=resolve_camera_collision([-2,0,0],[2,2,0],.1,geometry,policy='slide')
        self.assertTrue(slid['clear']);self.assertLess(slid['position'][0],-.1)
        self.assertAlmostEqual(slid['position'][1],2)
        self.assertEqual(slid,resolve_camera_collision([-2,0,0],[2,2,0],.1,geometry,policy='slide'))

    def test_reject_warning_and_initial_overlap_remain_distinct(self):
        from camera3d_constraints import resolve_camera_collision
        geometry=self.PlaneOracle()
        with self.assertRaises(ConstraintError):resolve_camera_collision([-2,0,0],[2,0,0],.1,geometry)
        warned=resolve_camera_collision([-2,0,0],[2,0,0],.1,geometry,policy='warn')
        self.assertFalse(warned['clear'])
        with self.assertRaisesRegex(ConstraintError,'starts in'):
            resolve_camera_collision([0,0,0],[-2,0,0],.1,geometry,policy='stop')
        with self.assertRaisesRegex(ConstraintError,'not supplied'):
            resolve_camera_collision([-2,0,0],[2,0,0],.1,object())

    def test_mandatory_bounds_and_continuous_path_segments(self):
        from camera3d_constraints import resolve_camera_collision,preflight_camera_path
        geometry=self.PlaneOracle()
        bounds=dict(minimum=[-3,-3,-3],maximum=[3,3,3])
        with self.assertRaises(ConstraintError):resolve_camera_collision([-2,0,0],[-2,5,0],.1,geometry,bounds=bounds,policy='warn')
        stopped=resolve_camera_collision([-2,0,0],[-2,5,0],.1,geometry,bounds=bounds,policy='stop')
        self.assertTrue(stopped['changed']);self.assertAlmostEqual(stopped['position'][1],2.9)
        result=preflight_camera_path([[-2,0,0],[2,0,0],[-2,0,0]],.1,geometry)
        self.assertFalse(result['clear']);self.assertEqual(result['segments'],2)


class NativeMeshCollisionAcceptance(unittest.TestCase):
    """Explicit integration gate requiring the owning Scene3D #62 modules."""
    @unittest.skipUnless(__import__('os').environ.get('HATCH_CAMERA_NATIVE_SPATIAL')=='1',
                         'Set HATCH_CAMERA_NATIVE_SPATIAL=1 with native Scene3D spatial modules')
    def test_actual_triangle_sweep_stop_slide_and_path_preflight(self):
        from scene3d_graph import SceneGraph
        from scene3d_spatial import SpatialIndex
        from camera3d_constraints import resolve_camera_collision,preflight_camera_path
        fixture=NativeSceneTargetTests();fixture.setUp();scene=fixture.scene
        for n in scene['nodes']:n['transform']['translation']=[0,0,0]
        geometry=SpatialIndex(SceneGraph(scene,external_references={}),{'actor':dict(
            vertices=[[0,-10,-10],[0,10,-10],[0,0,10]],triangles=[[0,1,2]])})
        first=geometry.sweep_sphere([-2,0,0],[2,2,0],.1)
        self.assertAlmostEqual(first['fraction'],.475)
        self.assertEqual(first['node'],'actor')
        for policy in ('stop','slide','nearby','adjust-distance'):
            result=resolve_camera_collision([-2,0,0],[2,2,0],.1,geometry,policy=policy)
            self.assertTrue(result['clear']);self.assertLess(result['position'][0],-.1)
            self.assertIsNone(geometry.sweep_sphere(result['position'],result['position'],.1))
        slid=resolve_camera_collision([-2,0,0],[2,2,0],.1,geometry,policy='slide')
        self.assertAlmostEqual(slid['position'][1],2)
        self.assertFalse(preflight_camera_path([[-2,0,0],[2,0,0]],.1,geometry)['clear'])
        self.assertTrue(preflight_camera_path([[-2,0,0],[-2,2,0]],.1,geometry)['clear'])
        with self.assertRaisesRegex(ConstraintError,'starts in'):
            resolve_camera_collision([0,0,0],[-2,0,0],.1,geometry,policy='stop')
