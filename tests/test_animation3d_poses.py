"""Native pose operations must move real skinned geometry and retain sources."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import sys
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'scripts'), str(Path(__file__).resolve().parent)]
from rig_test_helpers import fixture
from rig_skeleton import neutral_pose, skeleton_from_heads
from rig_skinning import evaluate_deformation, generate_skin_weights
from rig_animation import evaluate_animation
from rig_errors import RigError, object_hash
from animation3d_poses import (validate_pose, duplicate_pose, reset_pose, edit_pose,
    blend_pose, mirror_pose, compare_poses, inspect_rest_pose, pose_transition,
    apply_expressions, save_pose, load_pose, transition_samples)


class PoseManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rig = fixture(self.root, animated=False)
        self.pose = neutral_pose(self.rig.skeleton)
        self.before = object_hash(self.rig.manifest)

    def test_sparse_neutral_blend_restores_bones_and_morphs_without_resetting_other_channels(self):
        moved = edit_pose(self.rig, self.pose, bones={'b0': {'translation': [2,0,0]}}, morphs={'strip': {'blink': 1}})
        halfway = blend_pose(self.rig, moved, self.pose, .5)
        base = evaluate_deformation(self.rig, self.pose)['strip']['positions']
        actual = evaluate_deformation(self.rig, halfway)['strip']['positions']
        np.testing.assert_allclose(actual, base + [1,0,.1], atol=1e-12)
        restored = blend_pose(self.rig, moved, self.pose, 1)
        np.testing.assert_allclose(evaluate_deformation(self.rig, restored)['strip']['positions'], base, atol=1e-12)
        self.assertEqual(object_hash(self.rig.manifest), self.before)
        self.assertEqual(self.pose['bones'], {})

    def test_locked_local_bones_preserve_source_during_blend_reset_and_mirror(self):
        moved = edit_pose(self.rig, self.pose, bones={'b0': {'translation': [2,0,0]}, 'b1': {'translation':[0,1.3,0]}})
        for result in (blend_pose(self.rig, moved, self.pose, 1, locked_bones=['b0']),
                       reset_pose(self.rig, moved, locked_bones=['b0']),
                       mirror_pose(self.rig, moved, approved=True, locked_bones=['b0'])):
            self.assertEqual(result['bones']['b0'], moved['bones']['b0'])
        with self.assertRaises(RigError):
            edit_pose(self.rig, moved, bones={'b0': {'translation': [4,0,0]}}, locked_bones=['b0'])
        with self.assertRaises(RigError):
            reset_pose(self.rig, moved, locked_bones=['fabricated'])
        self.assertEqual(moved['bones']['b0']['translation'], [2,0,0])

    def symmetric_rig(self):
        specs = [dict(id='root', role='root', parent=None, head=[0,0,0], tail=[0,1,0]),
                 dict(id='left', role='appendage.left', parent='root', head=[-1,1,0], tail=[-1,2,0], mirror='right'),
                 dict(id='right', role='appendage.right', parent='root', head=[1,1,0], tail=[1,2,0], mirror='left')]
        self.rig.manifest['skeleton'] = skeleton_from_heads(specs)
        self.rig.manifest['controllers'] = dict(ik=[], constraints=[])
        self.rig.weights = generate_skin_weights(self.rig.geometry, self.rig.skeleton)
        return neutral_pose(self.rig.skeleton)

    def test_explicit_native_symmetry_mapping_and_symmetry_aware_editing(self):
        rest = self.symmetric_rig()
        left = edit_pose(self.rig, rest, bones={'left': {'translation': [-.6,1,0]}})
        reflected = mirror_pose(self.rig, left, approved=True)
        np.testing.assert_allclose(reflected['bones']['right']['translation'], [.6,1,0], atol=1e-12)
        symmetric = edit_pose(self.rig, rest, bones={'left': {'translation':[-.6,1,0]}}, symmetric=True, approved=True)
        np.testing.assert_allclose(symmetric['bones']['right']['translation'], [.6,1,0], atol=1e-12)
        self.assertGreater(compare_poses(self.rig, rest, symmetric)['vertex_max_displacement']['strip'], 0)
        with self.assertRaises(RigError): mirror_pose(self.rig, left)
        with self.assertRaises(RigError): mirror_pose(self.rig, left, approved=True, mapping={'left':'right'})
        with self.assertRaises(RigError): edit_pose(self.rig, rest, bones={'left':{'translation':[-.6,1,0]}}, symmetric=True, approved=True, locked_bones=['right'])
        with self.assertRaises(RigError): edit_pose(self.rig, rest, bones={'left':{}, 'right':{}}, symmetric=True, approved=True)

    def test_morph_symmetry_uses_explicit_actual_target_mapping(self):
        mesh = self.rig.geometry.meshes[0]
        mesh.morphs['left'] = {'POSITION': np.tile([.2,0,0], (len(mesh.positions),1))}
        mesh.morphs['right'] = {'POSITION': np.tile([-.2,0,0], (len(mesh.positions),1))}
        source = edit_pose(self.rig, self.pose, morphs={'strip':{'left':.75, 'right':.1}})
        mirrored = mirror_pose(self.rig, source, approved=True, morph_mapping={'strip':{'left':'right','right':'left'}})
        self.assertEqual(mirrored['morphs']['strip']['right'], .75)
        self.assertEqual(mirrored['morphs']['strip']['left'], .1)
        with self.assertRaises(RigError): mirror_pose(self.rig, source, approved=True, morph_mapping={'strip':{'left':'absent'}})
        self.assertEqual(source['morphs']['strip']['left'], .75)

    def test_named_snapshot_roundtrip_duplicate_and_actual_rest_inspection(self):
        moved = edit_pose(self.rig, self.pose, bones={'b0':{'translation':[1,0,0]}})
        named = duplicate_pose(self.rig, moved, 'reach')
        path = self.root/'reach.json'
        save_pose(self.rig, path, named)
        self.rig.manifest['limits']['memory_bytes'] = float(self.rig.manifest['limits']['memory_bytes'])
        loaded = load_pose(self.rig, path)
        self.assertEqual(loaded, named)
        saved = path.read_bytes()
        invalid = deepcopy(named); invalid['rig_version'] = '99.0.0'
        with self.assertRaises(RigError): save_pose(self.rig, path, invalid)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(moved['id'], self.pose['id'])
        report = inspect_rest_pose(self.rig)
        self.assertEqual(len(report['bones']), 3)
        self.assertEqual(report['pose']['bones'], {})
        self.assertGreater(compare_poses(self.rig, self.pose, loaded)['vertex_max_displacement']['strip'], .99)
        self.assertEqual(compare_poses(self.rig, loaded, loaded)['vertex_max_displacement']['strip'], 0)

    def test_native_transition_clip_interpolates_actual_geometry_and_respects_locks(self):
        target = edit_pose(self.rig, self.pose, bones={'b0':{'translation':[2,0,0]}}, morphs={'strip':{'blink':1}})
        clip = pose_transition(self.rig, self.pose, target, duration_seconds=2, samples=3)
        result = evaluate_animation(self.rig, clip, 1)['pose']
        base = evaluate_deformation(self.rig, self.pose)['strip']['positions']
        np.testing.assert_allclose(evaluate_deformation(self.rig, result)['strip']['positions'], base+[1,0,.1], atol=1e-12)
        locked = pose_transition(self.rig, self.pose, target, samples=2, locked_bones=['b0'])
        end = evaluate_animation(self.rig, locked, 1)['pose']
        np.testing.assert_allclose(evaluate_deformation(self.rig, end)['strip']['positions'], base+[0,0,.2], atol=1e-12)
        for kwargs in ({'samples':True}, {'samples':257}, {'duration_seconds':float('nan')}, {'fps':120}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RigError): pose_transition(self.rig, self.pose, target, **kwargs)

    def test_expression_intensity_changes_face_without_resetting_body_pose(self):
        body = edit_pose(self.rig, self.pose, bones={'b0':{'translation':[2,0,0]}})
        face = apply_expressions(self.rig, body, {'blink':.5})
        self.assertEqual(face['bones'], body['bones'])
        base = evaluate_deformation(self.rig, self.pose)['strip']['positions']
        np.testing.assert_allclose(evaluate_deformation(self.rig, face)['strip']['positions'], base+[2,0,.1], atol=1e-12)
        for expressions in ({'invented':1}, {'blink':True}, {'blink':2}):
            with self.subTest(expressions=expressions), self.assertRaises(RigError): apply_expressions(self.rig, body, expressions)
        self.assertEqual(body['morphs'], {})

    def test_invalid_pose_and_native_anatomical_limits_fail_without_mutation(self):
        for change in (lambda p:p.update(rig_version='2.0.0'),
                       lambda p:p['bones'].update(absent={'translation':[0,0,0]}),
                       lambda p:p['bones'].update(b0={'rotation':[0,0,0,0]}),
                       lambda p:p['morphs'].update(strip={'missing':1})):
            value = deepcopy(self.pose); change(value)
            with self.assertRaises(RigError): validate_pose(self.rig, value)
        self.rig.manifest['controllers']['constraints'] = [dict(schema_version=1,id='root-limit',bone='b0',type='translation',minimum=-.2,maximum=.2,axes=[0])]
        with self.assertRaisesRegex(RigError, 'constraint'):
            edit_pose(self.rig, self.pose, bones={'b0':{'translation':[2,0,0]}})
        self.assertEqual(self.pose['bones'], {})

    def test_unrepresentable_numeric_and_boolean_controls_have_typed_failures(self):
        for operation in (lambda: blend_pose(self.rig, self.pose, self.pose, 10**400),
                          lambda: pose_transition(self.rig, self.pose, self.pose, duration_seconds=10**400),
                          lambda: apply_expressions(self.rig, self.pose, {'blink':10**400}),
                          lambda: edit_pose(self.rig, self.pose, bones={'b0':{'translation':[10**400,0,0]}}),
                          lambda: edit_pose(self.rig, self.pose, symmetric=1)):
            with self.assertRaises(RigError): operation()
        target = edit_pose(self.rig, self.pose, bones={'b0':{'translation':[2,0,0]}})
        poses = transition_samples(self.rig, self.pose, target, samples=3)
        np.testing.assert_allclose(poses[1]['bones']['b0']['translation'], [1,0,0], atol=1e-12)
        poses[1]['bones']['b0']['translation'][0] = 99
        self.assertEqual(target['bones']['b0']['translation'], [2,0,0])

    def test_snapshot_json_object_capacity_precedes_decoder_allocation(self):
        from unittest.mock import patch
        path = self.root/'too-large.json'
        path.write_text(json.dumps(dict(padding='x'*10000)))
        before = path.read_bytes()
        self.rig.manifest['limits']['memory_bytes'] = 10000
        with patch('animation3d_poses.decode_json', side_effect=AssertionError('decoder allocated before budget admission')):
            with self.assertRaises(RigError) as rejected:
                load_pose(self.rig, path)
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        self.assertEqual(path.read_bytes(), before)

    def test_snapshot_symlink_is_not_followed(self):
        path = self.root/'pose.json'; save_pose(self.rig, path, self.pose)
        link = self.root/'link.json'
        try: link.symlink_to(path)
        except (OSError, NotImplementedError): self.skipTest('Host does not support symlink creation')
        with self.assertRaises(RigError): load_pose(self.rig, link)


if __name__ == '__main__': unittest.main()
