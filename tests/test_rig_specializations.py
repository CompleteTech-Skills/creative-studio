from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import numpy as np
from rig_test_helpers import fixture
from rig_demo import create_demo
from rig_facial import apply_expressions,gaze_at,finger_controls,appendage_controls,validate_eyelid_bindings
from rig_attachments import socket_world,attach_prop,detach_prop,transfer_prop,prop_world,solve_grasp
from rig_skeleton import neutral_pose,evaluate_fk,mirror_pose
from rig_skinning import evaluate_deformation
from rig_math import decompose,axis_angle
from rig_errors import RigError
from rig_states import rig_capabilities,map_states
from rig_models import Geometry,Mesh,RigAsset,SkinWeights
from rig_skeleton import skeleton_from_heads,validate_skeleton


def eyelid_fixture():
    skeleton=skeleton_from_heads([
        dict(id='root',role='root',parent=None,head=[0,0,0],tail=[0,1,0],deform=False),
        dict(id='head',role='head',parent='root',head=[0,1,0],tail=[0,2,0],deform=False),
        dict(id='eye.left',role='eye.left',parent='head',head=[-.1,1,0],tail=[-.1,1.1,0],deform=True),
        dict(id='eye.right',role='eye.right',parent='head',head=[.1,1,0],tail=[.1,1.1,0],deform=True)])
    meshes=[];skins={};world=evaluate_fk(skeleton)
    for side,x in (('left',-.1),('right',.1)):
        name='lid.'+side;positions=np.array([[x-.05,1,.1],[x+.05,1,.1],[x,1.1,.1]])
        delta=np.zeros_like(positions);delta[:,2]=.04
        mesh=Mesh(name,positions,np.array([[0,1,2]]),morphs={'lift':{'POSITION':delta}},morph_defaults={'lift':.25})
        meshes.append(mesh)
        skins[name]=SkinWeights(name,['head'],np.zeros((3,1),dtype=np.int64),np.ones((3,1)),
                               np.linalg.inv(world['head'])[None,:,:])
    manifest={'skeleton':skeleton,'controllers':{'constraints':[]},
              'facial':{'schema_version':1,'eyes':['eye.left','eye.right'],
                        'morph_targets':{'lid.left':['lift'],'lid.right':['lift']},
                        'expressions':{},'eyelids':[
                            dict(eye='eye.left',mesh='lid.left',morph='lift',up_weight=.8,
                                 down_weight=-.2,up_radians=.5,down_radians=.4)]}}
    return RigAsset(manifest,Geometry(meshes),skins)


class GazeEyelidTests(unittest.TestCase):
    def test_authored_lid_follows_real_eye_pitch_and_changes_vertices(self):
        rig=eyelid_fixture();neutral=neutral_pose(rig.skeleton);before=deepcopy(neutral)
        target=[0,2,2]
        result=gaze_at(rig,neutral,target)
        pitch=np.arctan2(1.,np.sqrt(4.+.1**2))
        expected=.25+(.8-.25)*(pitch/.5)
        self.assertAlmostEqual(result['morphs']['lid.left']['lift'],expected,places=12)
        baseline=evaluate_deformation(rig,neutral);actual=evaluate_deformation(rig,result)
        np.testing.assert_allclose(actual['lid.left']['positions'][:,2]-baseline['lid.left']['positions'][:,2],
                                   .04*(expected-.25),atol=1e-12)
        np.testing.assert_allclose(actual['lid.right']['positions'],baseline['lid.right']['positions'])
        self.assertEqual(neutral,before)

    def test_pitch_limits_are_applied_before_lid_response_and_release_uses_defaults(self):
        rig=eyelid_fixture();neutral=neutral_pose(rig.skeleton)
        rig.manifest['controllers']['constraints']=[dict(schema_version=1,id='left-limit',bone='eye.left',
            type='hinge',axis=[1,0,0],minimum=-.2,maximum=.2)]
        posed=gaze_at(rig,neutral,[0,3,2])
        self.assertAlmostEqual(posed['morphs']['lid.left']['lift'],.25+(.8-.25)*(.2/.5),places=10)
        posed['morphs']['lid.right']={'lift':.7}
        released=gaze_at(rig,posed,None)
        self.assertNotIn('lid.left',released['morphs'])
        self.assertEqual(released['morphs']['lid.right']['lift'],.7)
        np.testing.assert_allclose(evaluate_deformation(rig,released)['lid.left']['positions'],
                                   evaluate_deformation(rig,neutral)['lid.left']['positions'])

    def test_lid_response_excludes_parent_rotation_and_bounds_distant_targets(self):
        rig=eyelid_fixture();neutral=neutral_pose(rig.skeleton)
        from rig_skeleton import rotate_bone
        posed=rotate_bone(rig.skeleton,neutral,'root',axis_angle([1,0,0],.4))
        matrix=evaluate_fk(rig.skeleton,posed)['root']
        target=(matrix @ [0.,1.,2.,1.])[:3]
        result=gaze_at(rig,posed,target)
        self.assertAlmostEqual(result['morphs']['lid.left']['lift'],.25,places=12)
        self.assertAlmostEqual(gaze_at(rig,neutral,[0,100,1])['morphs']['lid.left']['lift'],.8,places=12)
        self.assertAlmostEqual(gaze_at(rig,neutral,[0,-100,1])['morphs']['lid.left']['lift'],-.2,places=12)
        self.assertAlmostEqual(gaze_at(rig,neutral,[0,1,-2])['morphs']['lid.left']['lift'],.25,places=12)

    def test_binding_admission_rejects_unavailable_inert_and_ambiguous_channels(self):
        for change in (lambda r:r.manifest['facial']['eyelids'][0].update(eye='missing'),
                       lambda r:r.manifest['facial']['eyelids'][0].update(mesh='missing'),
                       lambda r:r.manifest['facial']['eyelids'][0].update(morph='missing'),
                       lambda r:r.manifest['facial']['eyelids'][0].update(up_radians=0),
                       lambda r:r.manifest['facial']['eyelids'][0].update(down_weight=3),
                       lambda r:r.manifest['facial']['eyelids'][0].update(up_weight=.25,down_weight=.25),
                       lambda r:r.manifest['facial']['eyelids'].append(deepcopy(r.manifest['facial']['eyelids'][0])),
                       lambda r:r.geometry.meshes[0].morphs['lift']['POSITION'].fill(0)):
            rig=eyelid_fixture();change(rig)
            with self.subTest(change=change),self.assertRaises(RigError):gaze_at(rig,neutral_pose(rig.skeleton),[0,2,2])
        for value in ({},'',0):
            rig=eyelid_fixture();rig.manifest['facial']['eyelids']=value
            with self.subTest(value=value),self.assertRaises(RigError):gaze_at(rig,neutral_pose(rig.skeleton),[0,2,2])

    def test_no_binding_preserves_legacy_gaze_without_inventing_morph_controls(self):
        rig=eyelid_fixture();rig.manifest['facial'].pop('eyelids')
        pose=gaze_at(rig,neutral_pose(rig.skeleton),[0,2,2])
        self.assertEqual(pose['morphs'],{})

    def test_binding_survives_native_storage_and_invalid_targets_fail_rig_validation(self):
        from rig_jobs import save_rig
        from rig_models import load_rig
        from rig_validation import validate_rig
        with tempfile.TemporaryDirectory() as directory:
            rig,_=create_demo(Path(directory)/'human','humanoid',animate=False)
            binding=dict(eye='eye.left',mesh='eye-white-left',morph='blink',up_weight=.7,
                         down_weight=.1,up_radians=.5,down_radians=.5)
            rig.manifest['facial']['eyelids']=[binding]
            save_rig(rig,operation='authored-eyelid-binding')
            loaded=load_rig(rig.root)
            self.assertEqual(loaded.manifest['facial']['eyelids'],[binding])
            self.assertTrue(validate_rig(loaded)['ok'])
            loaded.manifest['facial']['eyelids'][0]['mesh']='missing'
            report=validate_rig(loaded)
            self.assertFalse(report['ok'])
            self.assertEqual(report['checks'][0]['name'],'structure')


class EyelidCorrectionTests(unittest.TestCase):
    def setUp(self):
        from rig_jobs import save_rig
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.rig,_=create_demo(Path(self.temp.name)/'human','humanoid',animate=False)
        self.binding=dict(eye='eye.left',mesh='eye-white-left',morph='blink',up_weight=.7,
                          down_weight=.1,up_radians=.5,down_radians=.5)
        self.rig.manifest['facial']['eyelids']=[deepcopy(self.binding)]
        save_rig(self.rig,operation='authored-eyelid-binding')

    def correct(self, edits):
        from rig_corrections import apply_rig_corrections
        from rig_models import rig_signature
        from rig_errors import timestamp
        rig=self.rig
        envelope=dict(schema_version=1,kind='hatch-3d-rig-correction',rig_id=rig.manifest['id'],
            rig_version=rig.manifest['version'],expected_sha256=rig_signature(rig.manifest),
            author='fixture',reason='Exercise eyelid correction references',created_at=timestamp(),edits=edits)
        return apply_rig_corrections(rig,envelope)

    def test_eye_rename_persists_remapped_binding_and_gaze_still_evaluates(self):
        from rig_models import load_rig
        result,_=self.correct([dict(operation='bone-update',target='eye.left',value={'id':'imported-eye'})])
        loaded=load_rig(result.root)
        self.assertEqual(loaded.manifest['facial']['eyelids'][0]['eye'],'imported-eye')
        pose=gaze_at(loaded,neutral_pose(loaded.skeleton),[0,2,2])
        self.assertIn('imported-eye',pose['bones'])
        self.assertIn('blink',pose['morphs']['eye-white-left'])
        self.assertEqual(self.rig.manifest['facial']['eyelids'][0]['eye'],'eye.left')

    def test_eye_removal_prunes_only_its_binding_and_preserves_other_gaze(self):
        from rig_models import load_rig
        result,_=self.correct([dict(operation='bone-remove',target='eye.left',value=None)])
        loaded=load_rig(result.root)
        self.assertEqual(loaded.manifest['facial']['eyelids'],[])
        self.assertIn('eye.right',gaze_at(loaded,neutral_pose(loaded.skeleton),[0,2,2])['bones'])

    def test_morph_removal_prunes_exact_binding_and_preserves_same_name_elsewhere(self):
        from rig_models import load_rig
        result,_=self.correct([dict(operation='morph',target='eye-white-left',value={'action':'remove','name':'blink'})])
        loaded=load_rig(result.root)
        self.assertEqual(loaded.manifest['facial']['eyelids'],[])
        self.assertIn('blink',loaded.geometry.by_id()['eye-white-right'].morphs)
        gaze_at(loaded,neutral_pose(loaded.skeleton),[0,2,2])

    def test_invalid_facial_replacement_and_inert_morph_edit_reject_before_persistence(self):
        facial=deepcopy(self.rig.manifest['facial']);facial['eyelids'][0]['mesh']='missing'
        mesh=self.rig.geometry.by_id()['eye-white-left']
        changes=[dict(operation='facial',target='face',value=facial),
                 dict(operation='morph',target=mesh.id,value={'name':'blink','action':'edit',
                     'assignments':{str(i):[0.,0.,0.] for i in range(len(mesh.positions))}})]
        before=(self.rig.root/'rig.json').read_bytes()
        for edit in changes:
            with self.subTest(operation=edit['operation']),self.assertRaises(RigError):self.correct([edit])
            self.assertEqual((self.rig.root/'rig.json').read_bytes(),before)
            self.assertEqual(self.rig.manifest['facial']['eyelids'],[self.binding])


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
    def test_zero_expression_can_release_nonzero_default_morph(self):
        r=deepcopy(self.humanoid)
        for mesh in r.geometry.meshes:
            if 'blink' in mesh.morphs:mesh.morph_defaults['blink']=1.
        r.manifest['facial']['expressions']={'open':dict(schema_version=1,morphs={'blink':0.},bones={},incompatible=[])}
        neutral=neutral_pose(r.skeleton)
        before=evaluate_deformation(r,neutral)
        after=evaluate_deformation(r,apply_expressions(r,neutral,{'open':1.}))
        self.assertGreater(max(np.max(np.abs(after[name]['positions']-value['positions']))
                               for name,value in before.items()),.01)
        self.assertIn('expressive-face',rig_capabilities(r))
    def test_state_work_estimate_counts_unique_existing_clips_without_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory)
            result=map_states(rig,recipes={'static-neutral':['bend','neutral','bend']},selected=['static-neutral'])
            estimate=result['work_estimate']
            self.assertEqual(estimate['clip_ids'],['bend','neutral'])
            self.assertEqual(estimate['unique_clips'],2)
            self.assertEqual(estimate['duration_seconds'],2.)
            self.assertEqual(estimate['frame_samples'],26)
            self.assertEqual(estimate['keyframes'],5)
            self.assertEqual(estimate['keyframe_scalar_values'],20)
            self.assertEqual(estimate['float64_position_bytes_per_frame'],96)
            self.assertEqual(estimate['float64_all_sample_position_bytes'],2496)
            self.assertEqual(estimate['model_calls'],0)
            self.assertIsNone(estimate['encoded_output_bytes'])
            missing=map_states(rig,recipes={'static-neutral':'missing'},selected=['static-neutral'])
            self.assertEqual(missing['work_estimate']['frame_samples'],0)
            self.assertEqual(missing['work_estimate']['unique_clips'],0)
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
