"""Independent #67 numerical oracles; no renderer or production-art approval."""
from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import animation3d_math as am
import animation3d_time as clock
from rig_errors import RigError
from animation3d_contracts import validate_contract
from animation3d_registry import TargetRegistry, sample_track
from rig_test_helpers import fixture
from rig_skeleton import neutral_pose
from rig_skinning import evaluate_deformation


class AnimationIndependentClockTests(unittest.TestCase):
    def test_required_rates_large_index_and_exact_subframe_roundtrip(self):
        rates = [(12, 1), (15, 1), (24, 1), (25, 1), (30, 1), (48, 1),
                 (60, 1), (120, 1), (24000, 1001), (30000, 1001), (60000, 1001)]
        for n, d in rates:
            fps = {'numerator': n, 'denominator': d}
            for frame, ticks in [(0, 0), (1, 1), (9734217, 999999)]:
                with self.subTest(fps=fps, frame=frame, ticks=ticks):
                    stamp = {'frame': frame, 'subframe_ticks': ticks}
                    expected = Fraction(frame * 1000000 + ticks, 1000000) * Fraction(d, n)
                    self.assertEqual(clock.seconds(stamp, fps), expected)
                    self.assertEqual(clock.at_seconds(expected, fps), stamp)
        # Reduced and unreduced rates denote the same clock exactly.
        self.assertEqual(clock.rate({'numerator': 48000, 'denominator': 2002}), Fraction(24000, 1001))

    def test_terminal_is_key_boundary_not_render_sample(self):
        fps = {'numerator': 24, 'denominator': 1}
        self.assertEqual(clock.sample_time(239, 240, fps), Fraction(239, 24))
        with self.assertRaises(RigError):
            clock.sample_time(240, 240, fps)
        self.assertEqual(clock.seconds({'frame': 240}, fps), 10)
        self.assertEqual(clock.shot_time({'frame': 28, 'subframe_ticks': 500000}, 20, 10, fps), Fraction(17, 48))
        with self.assertRaises(RigError):
            clock.shot_time({'frame': 30}, 20, 10, fps)
        self.assertEqual(clock.shot_time({'frame': 30}, 20, 10, fps, terminal=True), Fraction(5, 12))
        with self.assertRaises(RigError):
            clock.shot_time({'frame': 19}, 20, 10, fps)

    def test_clock_rejects_inexact_and_out_of_range_values(self):
        fps = {'numerator': 24, 'denominator': 1}
        for value in [Fraction(1, 7), -1, 0.1, True]:
            with self.subTest(value=value), self.assertRaises(RigError):
                clock.at_seconds(value, fps)
        for value in [{'frame': -1}, {'frame': True}, {'frame': 1.0},
                      {'frame': 0, 'subframe_ticks': 1000000},
                      {'frame': 0, 'subframe_ticks': -1},
                      {'frame': 0, 'subframe_ticks': False},
                      {'frame': 10000001}, {'frame': 0, 'extra': 1}]:
            with self.subTest(value=value), self.assertRaises(RigError):
                clock.frame_position(value)
        for bad in [{'numerator': True, 'denominator': 1}, {'numerator': 1, 'denominator': 0},
                    {'numerator': 1001, 'denominator': 1}, {'numerator': 1, 'denominator': 1001},
                    {'numerator': 24, 'denominator': 1, 'extra': 1}]:
            with self.subTest(rate=bad), self.assertRaises(RigError):
                clock.rate(bad)


class AnimationIndependentMathTests(unittest.TestCase):
    def test_trs_and_inverse_against_literal_geometry(self):
        s = math.sqrt(0.5)
        actual = am.compose([4, 5, 6], [0, 0, s, s], [2, 3, 4])
        expected = np.array([[0, -3, 0, 4], [2, 0, 0, 5], [0, 0, 4, 6], [0, 0, 0, 1.]])
        inverse = np.array([[0, 0.5, 0, -2.5], [-1/3, 0, 0, 4/3],
                            [0, 0, 0.25, -1.5], [0, 0, 0, 1.]])
        np.testing.assert_allclose(actual, expected, atol=1e-12)
        np.testing.assert_allclose(am.inverse(actual), inverse, atol=1e-12)
        parts = am.decompose(actual)
        np.testing.assert_allclose(parts['translation'], [4, 5, 6], atol=1e-12)
        np.testing.assert_allclose(parts['scale'], [2, 3, 4], atol=1e-12)
        np.testing.assert_allclose(am.qmatrix(parts['rotation']) @ [1, 0, 0], [0, 1, 0], atol=1e-12)
        # Parent rotation rotates a child's local translation, rather than adding world axes.
        parent = np.array([[0, -1, 0, 10], [1, 0, 0, 20], [0, 0, 1, 30], [0, 0, 0, 1.]])
        child = np.eye(4); child[:3, 3] = [2, 3, 4]
        np.testing.assert_allclose(am.local_to_world(child, parent)[:3, 3], [7, 22, 34])

    def test_quaternion_log_exp_and_slerp_against_axis_angles(self):
        # Quaternion log stores half-angle: exp(log(q)) denotes the same rotation.
        for angle in [0, 0.01, math.pi/3, math.pi, 5*math.pi/3]:
            q = np.array([0, math.sin(angle/2), 0, math.cos(angle/2)])
            expected_angle = angle if angle <= math.pi else angle - 2*math.pi
            np.testing.assert_allclose(am.quaternion_log(q), [0, expected_angle/2, 0], atol=1e-12)
            for sign in [1, -1]:
                recovered = am.quaternion_exp(am.quaternion_log(q * sign))
                self.assertAlmostEqual(abs(float(np.dot(recovered, q))), 1, places=12)
        q0 = [0, 0, 0, 1]
        q1 = [0, math.sin(math.pi/3), 0, math.cos(math.pi/3)]
        halfway = am.slerp(q0, q1, 0.5)
        np.testing.assert_allclose(am.qmatrix(halfway) @ [1, 0, 0], [0.5, 0, -math.sqrt(3)/2], atol=1e-12)
        np.testing.assert_allclose(am.slerp(q1, -np.array(q1), .37), q1, atol=1e-12)

    def test_documented_euler_xyz_order_and_blender_basis(self):
        # Rx(90), then Ry(90), then Rz(90) maps +X to -Z and +Y to +Y.
        m = am.qmatrix(am.euler_xyz([math.pi/2]*3))
        np.testing.assert_allclose(m, [[0, 0, 1], [0, 1, 0], [-1, 0, 0]], atol=1e-12)
        np.testing.assert_allclose(am.BLENDER_FROM_CANONICAL @ [1, 2, 3, 1], [1, -3, 2, 1])

    def test_curves_against_literal_polynomials_and_metric_path(self):
        self.assertEqual(am.smoothstep(.25), .15625)
        np.testing.assert_allclose(am.bezier([0, 0, 0], [2, 4, 0], [4, 4, 0], [6, 0, 0], .5), [3, 3, 0])
        # With derivatives 1/sec and 3/sec over two seconds, h(1/2)=2.5.
        self.assertAlmostEqual(float(am.hermite(0, 6, 1, 3, .5, duration=2)), 2.5)
        for t in [0, .1, .4, .9, 1]:
            np.testing.assert_allclose(am.hermite([0, 0, 0], [2, 4, 6], [2, 4, 6], [2, 4, 6], t), np.array([2, 4, 6])*t)
        np.testing.assert_allclose(am.catmull_rom([0, 0, 0], [1, 2, 0], [2, 2, 0], [3, 0, 0], .5), [1.5, 2.25, 0])
        self.assertEqual(am.arc_length([[0, 0, 0], [3, 4, 0], [3, 4, 12]]), 17)

    def test_vector_and_world_to_local_contracts(self):
        np.testing.assert_allclose(am.vector_add([1, 2, 3], [-2, 3, 1]), [-1, 5, 4])
        np.testing.assert_allclose(am.vector_subtract([1, 2, 3], [-2, 3, 1]), [3, -1, 2])
        np.testing.assert_allclose(am.vector_lerp([0, 2, 4], [4, 6, 8], .25), [1, 3, 5])
        np.testing.assert_allclose(am.unit([3, 4, 0]), [.6, .8, 0])
        parent = np.array([[0, -1, 0, 10], [1, 0, 0, 20], [0, 0, 1, 30], [0, 0, 0, 1.]])
        world = parent.copy(); world[:3, 3] = [7, 22, 34]
        expected = np.eye(4); expected[:3, 3] = [2, 3, 4]
        np.testing.assert_allclose(am.world_to_local(world, parent), expected, atol=1e-12)
        with self.assertRaises(RigError):
            am.quaternion_exp([1e308, 0, 0])
        with self.assertRaises(RigError):
            am.hermite(0, 1, 1e308, 1e308, .5, 1e308)

    def test_malformed_numerical_inputs_fail(self):
        for matrix in [np.zeros((4, 4)), np.diag([1, 1, 0, 1]), np.full((4, 4), np.nan)]:
            with self.subTest(matrix=matrix), self.assertRaises(RigError):
                am.inverse(matrix)
        for q in [[0, 0, 0, 0], [0, 0, float('inf'), 1]]:
            with self.subTest(q=q), self.assertRaises(RigError):
                am.quaternion_log(q)
        for bad in [-.01, 1.01, float('nan'), float('inf')]:
            with self.subTest(t=bad), self.assertRaises(RigError):
                am.smoothstep(bad)
        with self.assertRaises(RigError):
            am.hermite([0, 1], [0, 1, 2], [1, 1], [1, 1], .5)
        with self.assertRaises(RigError):
            am.bezier([0, 0, 0], [0, float('nan'), 0], [0, 0, 0], [1, 1, 1], .5)
        with self.assertRaises(RigError):
            am.arc_length([[0, 0, 0]])
        reflection = np.diag([-1, 1, 1, 1])
        with self.assertRaises(RigError):
            am.local_to_world(reflection, np.eye(4))


def track_document():
    return {'id': 'move', 'target': {'kind': 'node', 'node_id': 'root', 'property': 'translation'},
            'value_type': 'vec3', 'default_value': [0, 0, 0], 'keys': [],
            'extrapolation_before': 'hold', 'extrapolation_after': 'hold', 'enabled': True}


def sample_layer():
    asset = {'id': 'clip', 'version': '1.0.0', 'path': 'clip.json', 'sha256': '0'*64, 'kind': 'clip'}
    return {'id': 'base', 'name': 'Base', 'priority': 0, 'blend_mode': 'override', 'weight': 1,
            'muted': False, 'solo': False, 'placements': [
                {'id': 'placed', 'clip': asset, 'start_frame': 0, 'duration_frames': 24,
                 'source_in_frame': 0, 'playback_rate': {'numerator': 24, 'denominator': 1},
                 'blend_in_frames': 0, 'blend_out_frames': 0, 'weight': 1,
                 'layer_id': 'base', 'enabled': True}]}


def wrapped(kind, value):
    return {'schema_version': 1, 'kind': 'animation_'+kind+'_3d', 'value': value}


class AnimationIndependentContractTests(unittest.TestCase):
    def test_typed_track_rejects_malformed_values_times_and_controls(self):
        baseline = wrapped('track', track_document())
        validate_contract(baseline)
        variations = []
        for update in [{'unexpected': 'field'}, {'value_type': 'float'}, {'default_value': [0, 0]},
                       {'target': {'kind': 'node', 'node_id': 'root', 'property': '__dict__'}}]:
            doc = deepcopy(baseline); doc['value'].update(update); variations.append(doc)
        for keys in [
            [{'time': {'frame': 1}, 'value': [0, 0, 0]}, {'time': {'frame': 1}, 'value': [1, 1, 1]}],
            [{'time': {'frame': 2}, 'value': [0, 0, 0]}, {'time': {'frame': 1}, 'value': [1, 1, 1]}],
            [{'time': {'frame': 0}, 'value': [0, 0, 0], 'interpolation': 'quaternion-slerp'}],
            [{'time': {'frame': 0}, 'value': [0, 0, 0], 'interpolation': 'bezier'}],
            [{'time': {'frame': 0}, 'value': [0, 0, 0], 'interpolation': 'linear', 'in_tangent': [0, 0, 0]}],
        ]:
            doc = deepcopy(baseline); doc['value']['keys'] = keys; variations.append(doc)
        for doc in variations:
            with self.subTest(doc=doc), self.assertRaises(RigError):
                validate_contract(doc)

    def test_rotation_and_visibility_contracts_reject_false_interpolation(self):
        track = track_document()
        track.update(target={'kind': 'node', 'node_id': 'root', 'property': 'rotation'},
                     value_type='quaternion', default_value=[0, 0, 0, 2])
        with self.assertRaises(RigError):
            validate_contract(wrapped('track', track))
        track.update(target={'kind': 'node', 'node_id': 'root', 'property': 'visibility'},
                     value_type='boolean', default_value=True,
                     keys=[{'time': {'frame': 0}, 'value': False, 'interpolation': 'linear'}])
        with self.assertRaises(RigError):
            validate_contract(wrapped('track', track))

    def test_layer_rejects_duplicate_placement_ids_and_foreign_layer(self):
        layer = sample_layer()
        validate_contract(wrapped('layer', layer))
        duplicate = deepcopy(layer); duplicate['placements'].append(deepcopy(duplicate['placements'][0]))
        with self.assertRaises(RigError):
            validate_contract(wrapped('layer', duplicate))
        foreign = deepcopy(layer); foreign['placements'][0]['layer_id'] = 'different'
        with self.assertRaises(RigError):
            validate_contract(wrapped('layer', foreign))

    def test_constraint_rejects_self_target_and_missing_ik_chain(self):
        source = {'kind': 'bone', 'actor_id': 'robot', 'bone_id': 'hand', 'property': 'translation'}
        constraint = {'id': 'reach', 'type': 'two_bone_ik', 'source': source, 'weight': 1, 'enabled': True,
                      'target': deepcopy(source), 'chain_bones': ['arm', 'forearm']}
        with self.assertRaises(RigError):
            validate_contract(wrapped('constraint', constraint))
        constraint['target'] = {'kind': 'node', 'node_id': 'goal', 'property': 'translation'}
        constraint['chain_bones'] = []
        with self.assertRaises(RigError):
            validate_contract(wrapped('constraint', constraint))

    def test_deep_acyclic_state_graph_stays_inside_declared_resource_bound(self):
        # Order links along the validator's first set traversal to expose recursive DFS depth.
        identifiers = [f'n{i}' for i in range(1500)]
        order = list(set(identifiers))
        asset = {'id': 'clip', 'version': '1.0.0', 'path': 'clip.json', 'sha256': '0'*64, 'kind': 'clip'}
        document = {'schema_version': 1, 'kind': 'animation_state_machine_3d', 'id': 'graph', 'version': '1.0.0',
                    'parameters': [{'name': 'go', 'type': 'bool', 'default': False}],
                    'states': [{'id': name, 'name': name, 'clip': asset, 'playback_speed': 1} for name in identifiers],
                    'transitions': [
                        {'id': f't{i}', 'from_state': order[i], 'to_state': order[i+1],
                         'conditions': [{'parameter': 'go', 'operator': 'eq', 'value': True}],
                         'condition_mode': 'all', 'priority': 0, 'duration_frames': 0,
                         'blend_curve': 'linear', 'interruption_policy': 'none'}
                        for i in range(len(order)-1)],
                    'initial_state': order[0], 'transition_limit_per_frame': 128,
                    'provenance': {'origin': 'independent fixture', 'kind': 'fixture', 'fixture': True, 'sources': []}}
        validate_contract(document)

    def test_asset_paths_are_portable_and_no_unknown_schema_fields(self):
        for path in ['../escape.json', '/absolute.json', 'C:/drive.json', 'safe/../escape.json']:
            layer = sample_layer(); layer['placements'][0]['clip']['path'] = path
            with self.subTest(path=path), self.assertRaises(RigError):
                validate_contract(wrapped('layer', layer))
        doc = wrapped('track', track_document()); doc['schema_version'] = 2
        with self.assertRaises(RigError):
            validate_contract(doc)


class AnimationIndependentRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rig = fixture(Path(self.tmp.name), animated=False)
        self.registry = TargetRegistry.from_rig(self.rig.root, 'robot')

    def bone_track(self):
        # Construct independently from the registry entries, preserving an actual owner target.
        return {'id': 'move', 'target': {'kind': 'bone', 'actor_id': 'robot', 'bone_id': 'b0', 'property': 'translation'},
                'value_type': 'vec3', 'default_value': [0, 0, 0], 'keys': [
                    {'time': {'frame': 0}, 'value': [0, 0, 0], 'interpolation': 'linear'},
                    {'time': {'frame': 10}, 'value': [2, 0, 0], 'interpolation': 'linear'}],
                'extrapolation_before': 'hold', 'extrapolation_after': 'hold', 'enabled': True}

    def test_real_rig_resolution_sampling_and_deformation_are_nonsequential(self):
        track = self.bone_track()
        values = [sample_track(track, {'frame': i}, self.registry) for i in [7, 2, 7, 5]]
        np.testing.assert_allclose(values, [[1.4, 0, 0], [.4, 0, 0], [1.4, 0, 0], [1, 0, 0]])
        baseline = evaluate_deformation(self.rig)
        pose = neutral_pose(self.rig.skeleton)
        pose['bones'].setdefault('b0', {})['translation'] = values[-1]
        moved = evaluate_deformation(self.rig, pose)
        for mesh in self.rig.geometry.meshes:
            np.testing.assert_allclose(moved[mesh.id]['positions'] - baseline[mesh.id]['positions'],
                                       np.tile([1, 0, 0], (len(mesh.positions), 1)), atol=1e-10)

    def test_registry_and_resolved_records_do_not_alias_callers(self):
        document = deepcopy(self.registry.document)
        registry = TargetRegistry(self.rig.root, document)
        target = self.bone_track()['target']
        document['entries'].clear()
        record = registry.resolve(target)
        record['source']['sha256'] = '0'*64
        self.assertNotEqual(registry.resolve(target)['source']['sha256'], '0'*64)
        unknown = deepcopy(target); unknown['bone_id'] = 'absent'
        with self.assertRaises(RigError):
            registry.resolve(unknown)

    def test_declared_bone_must_exist_and_other_owner_is_not_assumed(self):
        document = deepcopy(self.registry.document)
        target = self.bone_track()['target']
        entry = next(e for e in document['entries'] if e['target'] == target)
        entry['target']['bone_id'] = 'fabricated'
        registry = TargetRegistry(self.rig.root, document)
        with self.assertRaises(RigError):
            registry.resolve(entry['target'])
        entry['target'] = {'kind': 'camera', 'camera_id': 'cam', 'property': 'focal_length'}
        entry['owner'] = 'camera'
        registry = TargetRegistry(self.rig.root, document)
        with self.assertRaises(RigError) as caught:
            registry.resolve(entry['target'])
        self.assertEqual(caught.exception.report['code'], 'ANIMATION_OWNER')

    def test_manifest_and_native_dependency_tamper_are_rejected(self):
        target = self.bone_track()['target']
        manifest_path = self.rig.root / 'rig.json'
        raw = manifest_path.read_bytes()
        manifest_path.write_bytes(raw + b'\n')
        with self.assertRaises(RigError) as caught:
            self.registry.resolve(target)
        self.assertEqual(caught.exception.report['code'], 'ANIMATION_HASH')
        manifest_path.write_bytes(raw)
        geometry_path = self.rig.root / self.rig.manifest['geometry']['data']['path']
        geometry_path.write_bytes(geometry_path.read_bytes() + b'tampered')
        with self.assertRaises(RigError):
            self.registry.resolve(target)

    def test_mixed_hermite_to_linear_segment_has_required_incoming_tangent(self):
        track = self.bone_track()
        track['keys'][0].update(interpolation='hermite', out_tangent=[.2, 0, 0])
        track['keys'][1]['in_tangent'] = [.2, 0, 0]
        self.registry.validate_track(track)
        np.testing.assert_allclose(sample_track(track, {'frame': 5}, self.registry), [1, 0, 0], atol=1e-12)
        del track['keys'][1]['in_tangent']
        with self.assertRaises(RigError):
            self.registry.validate_track(track)

    def test_hermite_overshoot_respects_exact_registered_morph_bounds(self):
        document = deepcopy(self.registry.document)
        entry = next(e for e in document['entries'] if e['target']['kind'] == 'morph')
        entry.update(minimum=.25, maximum=.75)
        registry = TargetRegistry(self.rig.root, document)
        track = {'id': 'morph', 'target': entry['target'], 'value_type': 'float', 'default_value': .25,
                 'keys': [{'time': {'frame': 0}, 'value': .25, 'interpolation': 'hermite', 'out_tangent': .2},
                          {'time': {'frame': 10}, 'value': .5, 'interpolation': 'linear', 'in_tangent': -.2}],
                 'extrapolation_before': 'hold', 'extrapolation_after': 'hold', 'enabled': True}
        registry.validate_track(track)
        # The Hermite midpoint is .875: inside generic [0,1], outside this target's [.25,.75].
        with self.assertRaises(RigError):
            sample_track(track, {'frame': 5}, registry)


if __name__ == '__main__':
    unittest.main()
