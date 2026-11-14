"""Spatial attachment and native release input behavior; no mocked solver proof."""
from copy import deepcopy
import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_backend_example import build_inputs
from physics_backends import canonical, digest, parse
from physics_grabbing import (rigid_transform, reach_report, attach_kinematic, attachment_pose,
    detach_kinematic, handoff_kinematic, release_state, compile_release, capabilities)
from rig_errors import RigError
from rig_math import axis_angle, transform_matrix
from rig_models import RigAsset, Geometry
from rig_skeleton import skeleton_from_heads, neutral_pose


def transform(position=(0, 0, 0), rotation=(0, 0, 0, 1)):
    return dict(translation=list(position), rotation=list(rotation), scale=[1, 1, 1])


def grip_fixture():
    # A valid canonical skeleton/socket fixture. No production rig or solver
    # readiness is inferred from this unit-only spatial input.
    skeleton = skeleton_from_heads([dict(id='hand', role='hand.right', parent=None,
        head=[0, 0, 0], tail=[0, 1, 0])], source='procedural-fixture', reference='unit grip', confidence=1.)
    rig = RigAsset(manifest=dict(character=dict(id='shared-character'), skeleton=skeleton,
        attachments=[dict(schema_version=1, id='grip.right', bone='hand', transform=transform(),
                          role='grip', owner='shared-character')]), geometry=Geometry([]))
    _, payloads = build_inputs()
    asset = parse(payloads['ball-asset.json'])
    asset['sockets'] = [dict(id='handle', transform=transform([.5, 0, 0]))]
    return rig, neutral_pose(skeleton), asset


def release(*, impulse=(0, 0, 0), angular_impulse=(0, 0, 0)):
    return release_state(dict(time=dict(numerator=0, denominator=1), transform=transform([-.1, 3, 0])),
        dict(time=dict(numerator=1, denominator=10), transform=transform([0, 3, 0])),
        impulse=impulse, angular_impulse=angular_impulse)


class GripSpatialTests(unittest.TestCase):
    def test_named_anchor_reach_uses_actor_world_root(self):
        rig, pose, asset = grip_fixture()
        report = reach_report(rig, pose, 'grip.right', transform([10, 2, 3]),
                              asset, transform([9.5, 2, 3]), 'handle')
        self.assertTrue(report['eligible'])
        self.assertEqual(report['distance_m'], 0)
        report = reach_report(rig, pose, 'grip.right', transform(), asset, transform([10, 0, 0]), 'handle')
        self.assertFalse(report['eligible'])

    def test_orientation_mismatch_and_missing_anchors_fail(self):
        rig, pose, asset = grip_fixture()
        report = reach_report(rig, pose, 'grip.right', transform(), asset,
                              transform([.5, 0, 0], axis_angle([0, 1, 0], math.pi).tolist()), 'handle')
        self.assertFalse(report['eligible'])
        for socket, grip in [('missing', 'handle'), ('grip.right', 'missing')]:
            with self.subTest(socket=socket, grip=grip), self.assertRaises(RigError):
                attach_kinematic(rig, pose, socket, transform(), asset, transform(), grip)

    def test_carry_and_detach_preserve_world_pose_without_mutating_inputs(self):
        rig, pose, asset = grip_fixture()
        source = transform([9.5, 2, 3]);before = deepcopy(source)
        attached = attach_kinematic(rig, pose, 'grip.right', transform([10, 2, 3]), asset, source, 'handle')
        np.testing.assert_allclose(transform_matrix(attached['world_transform']), transform_matrix(source), atol=1e-12)
        carried = attachment_pose(rig, pose, attached['attachment'], transform([11, 3, 4]))
        np.testing.assert_allclose(carried['translation'], [10.5, 3, 4], atol=1e-12)
        detached = detach_kinematic(rig, pose, attached['attachment'], transform([11, 3, 4]))
        self.assertEqual(carried, detached)
        self.assertEqual(source, before)
        self.assertIn('uncommitted', attached['ownership_status'])

    def test_handoff_same_character_instances_keeps_world_pose(self):
        rig, pose, asset = grip_fixture()
        first = attach_kinematic(rig, pose, 'grip.right', transform([10, 2, 3]),
                                 asset, transform([9.5, 2, 3]), 'handle')
        # Same underlying character, different actor root/pose. Atomic instance
        # ownership is reserved for the registry, never inferred from this ID.
        other_pose = deepcopy(pose);other_pose['bones']['hand'] = dict(translation=[1, 0, 0])
        target = handoff_kinematic(rig, pose, first['attachment'], transform([10, 2, 3]),
            rig, other_pose, 'grip.right', transform([9, 2, 3]), asset, 'handle')
        np.testing.assert_allclose(transform_matrix(target['world_transform']),
                                   transform_matrix(first['world_transform']), atol=1e-12)
        self.assertIn('actor-instance CAS', target['ownership_status'])

    def test_wrong_attachment_owner_is_rejected(self):
        rig, pose, _ = grip_fixture()
        with self.assertRaises(RigError):
            attachment_pose(rig, pose, dict(owner='different-character', socket='grip.right', transform=transform()), transform())

    def test_pose_version_and_boolean_overrides_cannot_change_attachment(self):
        rig, pose, asset = grip_fixture()
        for corrupt in [dict(rig_version='2.0.0'), dict(bones={'hand':dict(translation=[True,0,0])})]:
            candidate = {**pose, **corrupt}
            with self.subTest(corrupt=corrupt), self.assertRaises(RigError):
                attach_kinematic(rig, candidate, 'grip.right', transform(), asset, transform([-.5,0,0]), 'handle')

    def test_malformed_attachment_and_socket_inputs_remain_typed(self):
        rig, pose, asset = grip_fixture()
        for bad in [None, [], {}, dict(owner=None, socket='grip.right', transform=transform()),
                    dict(owner='shared-character', socket=[], transform=transform()),
                    dict(owner='shared-character', socket='missing', transform=transform())]:
            for function in [attachment_pose, detach_kinematic]:
                with self.subTest(bad=bad, function=function.__name__), self.assertRaises(RigError):
                    function(rig, pose, bad, transform())
        for bad in [None, [], {}, True, 'x'*129]:
            with self.subTest(socket=bad), self.assertRaises(RigError):
                attach_kinematic(rig, pose, bad, transform(), asset, transform(), 'handle')

    def test_duplicate_and_wrong_character_sockets_fail_before_fk(self):
        rig, pose, asset = grip_fixture()
        for sockets in [[*rig.manifest['attachments'], *rig.manifest['attachments']],
                        [{**rig.manifest['attachments'][0], 'owner':'other-character'}]]:
            rig.manifest['attachments'] = sockets
            with self.subTest(sockets=sockets), self.assertRaises(RigError):
                attach_kinematic(rig, pose, 'grip.right', transform(), asset, transform(), 'handle')

    def test_malformed_numeric_values_do_not_escape_typed_boundary(self):
        for value in [True, '1', None, float('nan'), float('inf'), 10**400, [1], {}]:
            with self.subTest(value=str(value)[:20]), self.assertRaises(RigError):
                rigid_transform(transform([value, 0, 0]))
        for scale in [[1, 1, 1.000001], [1, 0, 1], [True, 1, 1]]:
            value = transform();value['scale'] = scale
            with self.subTest(scale=scale), self.assertRaises(RigError):rigid_transform(value)

    def test_reach_bounds_are_typed_and_positive(self):
        rig, pose, asset = grip_fixture()
        for value in [0, -1, True, 10**400, float('nan')]:
            with self.subTest(value=str(value)[:20]), self.assertRaises(RigError):
                reach_report(rig, pose, 'grip.right', transform(), asset, transform(), 'handle', max_distance=value)


class ReleaseTests(unittest.TestCase):
    def test_release_linear_velocity_and_impulse_remain_distinct(self):
        state = release(impulse=[2, 0, 0])
        self.assertEqual(state['linear_velocity'], [1, 0, 0])
        self.assertEqual(state['angular_velocity'], [0, 0, 0])
        self.assertEqual(state['impulse'], [2, 0, 0])
        self.assertEqual(state['sample_interval'], dict(numerator=1, denominator=10))

    def test_world_angular_velocity_and_com_offset(self):
        previous = dict(time=dict(numerator=0, denominator=1), transform=transform([0, 0, 0]))
        current = dict(time=dict(numerator=1, denominator=2), transform=transform([1, 0, 0], axis_angle([0, 0, 1], math.pi/2).tolist()))
        state = release_state(previous, current, center_of_mass=[1, 0, 0])
        np.testing.assert_allclose(state['origin_linear_velocity'], [2, 0, 0], atol=1e-12)
        np.testing.assert_allclose(state['angular_velocity'], [0, 0, math.pi], atol=1e-12)
        np.testing.assert_allclose(state['linear_velocity'], [2-math.pi, 0, 0], atol=1e-12)

    def test_quaternion_sign_does_not_create_spurious_release_spin(self):
        previous = dict(time=dict(numerator=0, denominator=1), transform=transform())
        current = dict(time=dict(numerator=1, denominator=10), transform=transform(rotation=[0, 0, 0, -1]))
        self.assertEqual(release_state(previous, current)['angular_velocity'], [0, 0, 0])

    def test_exact_time_and_velocity_limits(self):
        previous = dict(time=dict(numerator=0, denominator=1), transform=transform())
        for time in [dict(numerator=0, denominator=1), dict(numerator=True, denominator=10),
                     dict(numerator=1, denominator=0), dict(numerator=2, denominator=1)]:
            with self.subTest(time=time), self.assertRaises(RigError):
                release_state(previous, dict(time=time, transform=transform()))
        with self.assertRaises(RigError):
            release_state(previous, dict(time=dict(numerator=1, denominator=1000), transform=transform([1, 0, 0])))

    def test_ambiguous_half_turn_and_native_angular_speed_limit_fail(self):
        previous = dict(time=dict(numerator=0, denominator=1), transform=transform())
        for rotation in ([0,0,1,0], [0,0,-1,0]):
            with self.subTest(rotation=rotation), self.assertRaises(RigError) as error:
                release_state(previous, dict(time=dict(numerator=1,denominator=10),transform=transform(rotation=rotation)))
            self.assertEqual(error.exception.report['code'], 'PHYSICS_GRIP_ROTATION_AMBIGUOUS')
        with self.assertRaises(RigError) as error:
            release_state(previous, dict(time=dict(numerator=1,denominator=100),transform=transform(rotation=axis_angle([0,1,0], .6).tolist())))
        self.assertEqual(error.exception.report['code'], 'PHYSICS_GRIP_SPEED')

    def test_release_compiles_new_source_bound_scene_without_changing_originals(self):
        world, payloads = build_inputs(backend='pybullet')
        before_world, before_payloads = deepcopy(world), dict(payloads)
        released, sources, receipt = compile_release(world, payloads, 'ball', release(impulse=[2, 0, 0]))
        self.assertEqual(world, before_world);self.assertEqual(payloads, before_payloads)
        self.assertEqual(released['bodies'][0]['linear_velocity'], [1, 0, 0])
        self.assertEqual(released['forces'][0]['type'], 'impulse')
        self.assertNotEqual(released['scene']['sha256'], world['scene']['sha256'])
        self.assertEqual(digest(sources['physics-scene.json']), released['scene']['sha256'])
        self.assertEqual(parse(sources['physics-scene.json'])['revision'], 1)
        self.assertEqual(sources['ball.obj'], payloads['ball.obj'])
        self.assertIn('not performed', receipt['status'])

    def test_source_hash_change_and_conflicting_transform_writer_fail(self):
        world, payloads = build_inputs(backend='pybullet')
        corrupt = dict(payloads);corrupt['physics-scene.json'] += b' '
        with self.assertRaises(RigError):compile_release(world, corrupt, 'ball', release())
        scene = parse(payloads['physics-scene.json']);scene['nodes'][0]['locked'] = True
        payloads['physics-scene.json'] = canonical(scene)
        old = deepcopy(world['scene']);world['scene']['sha256'] = digest(payloads['physics-scene.json'])
        project = parse(payloads['project.json'])
        for field in ['scenes', 'dependency_lock']:
            project[field] = [{**world['scene'], 'kind':'scene3d-scene'} if x['id'] == old['id'] else x for x in project[field]]
        payloads['project.json'] = canonical(project)
        with self.assertRaises(RigError):compile_release(world, payloads, 'ball', release())

    def test_existing_force_program_cannot_be_silently_replaced(self):
        world, payloads = build_inputs(backend='pybullet')
        world['forces'] = [dict(id='old', type='impulse', bodies=['ball'], vector=[1,0,0], point=[0,0,0],
            frame_range=dict(start=0,end=1),strength=1,seed=0,categories=['rigid-body'])]
        with self.assertRaises(RigError):compile_release(world, payloads, 'ball', release())

    def test_missing_release_fields_and_unavailable_modes_are_explicit(self):
        world, payloads = build_inputs(backend='pybullet')
        with self.assertRaises(RigError):compile_release(world, payloads, 'ball', {})
        self.assertIn('unsupported', capabilities()['fixed_attachment'])
        self.assertIn('unavailable', capabilities()['persistent_ownership'])

    def test_release_center_of_mass_must_match_the_physical_body(self):
        world, payloads = build_inputs(backend='pybullet')
        world['bodies'][0]['mass']['center_of_mass'] = [.1, 0, 0]
        with self.assertRaises(RigError) as error:compile_release(world, payloads, 'ball', release())
        self.assertEqual(error.exception.report['code'], 'PHYSICS_GRIP_COM')

    def test_release_wakes_originally_sleeping_body_without_disabling_sleep(self):
        world, payloads = build_inputs(backend='pybullet')
        world['bodies'][0]['sleep'] = dict(enabled=True, initially_sleeping=True)
        released, _, _ = compile_release(world, payloads, 'ball', release())
        self.assertTrue(released['bodies'][0]['sleep']['enabled'])
        self.assertFalse(released['bodies'][0]['sleep']['initially_sleeping'])
        self.assertTrue(world['bodies'][0]['sleep']['initially_sleeping'])

    def test_manually_supplied_release_cannot_bypass_native_velocity_magnitude(self):
        world, payloads = build_inputs(backend='pybullet')
        for key in ('linear_velocity', 'angular_velocity'):
            state = release();state[key] = [40, 40, 0]
            with self.subTest(key=key), self.assertRaises(RigError) as error:
                compile_release(world, payloads, 'ball', state)
            self.assertEqual(error.exception.report['code'], 'PHYSICS_GRIP_SPEED')


if __name__ == '__main__':unittest.main()
