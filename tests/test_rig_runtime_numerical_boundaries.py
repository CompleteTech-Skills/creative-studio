"""Independent numerical-boundary oracles on real synthetic native rigs.

Authored against 38fafd68 without the forthcoming implementation correction.
No solver, FK, control pass, or skin deformation is mocked.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from rig_animation import clip_from_poses, evaluate_animation, validate_animation
from rig_errors import RigError
from rig_ik import solve_ik, validate_controllers
from rig_skeleton import evaluate_fk, neutral_pose, validate_skeleton
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import fixture
import test_rig_state_runtime_contacts as contact_helpers


class RigRuntimeNumericalBoundaryTests(unittest.TestCase):
    STATE_FIELDS = ('state', 'entered', 'time', 'source_pose', '_input_pose',
                    '_source_input_pose', '_source_contact_correction',
                    'root_offset', 'transition_count')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rig = fixture(temporary.name)

    def bounded_copy_fixture(self, reverse_ids=False, *, aim=False):
        # A nondeforming sibling supplies a genuine positional-control target.
        marker = deepcopy(self.rig.skeleton['bones'][-1])
        marker.update(id='marker', role='marker', parent='b0', deform=False)
        marker['rest']['translation'] = [0., 2., 0.]
        self.rig.skeleton['bones'].append(marker)
        validate_skeleton(self.rig.skeleton)
        constraints = [
            dict(schema_version=1, id='z-bounds' if reverse_ids else 'a-bounds',
                 bone='b2', type='translation', axes=[1], minimum=0., maximum=0.),
            dict(schema_version=1, id='a-follow' if reverse_ids else 'z-follow',
                 bone='b2', type='copy-position', target='marker', influence=.25)]
        if aim:
            constraints.append(dict(schema_version=1, id='m-world-aim', bone='b2',
                                    type='aim', axis=[0., 1., 0.], point=[2., 3., 0.]))
        self.rig.manifest['controllers']['constraints'] = constraints
        validate_controllers(self.rig.skeleton, self.rig.manifest['controllers'])
        source = neutral_pose(self.rig.skeleton)
        target = deepcopy(source)
        target['bones']['marker'] = {'translation': [0., 1., 0.]}
        contact = dict(schema_version=1, id='plant', chain='arm', start=0., end=1.,
                       target=[0., 2., 0.], kind='foot')
        for name, pose, contacts in (('held', source, [contact]), ('released', target, [])):
            self.rig.clips[name] = clip_from_poses(
                self.rig, name, [0., 1.], [pose, pose], loop=True, contacts=contacts)
            validate_animation(self.rig, self.rig.clips[name])

    def preserve_sources(self, recipes):
        saved = deepcopy((self.rig.manifest, self.rig.clips, recipes))
        source_files = {descriptor['path']:
                        (Path(self.rig.root) / descriptor['path']).read_bytes()
                        for descriptor in self.rig.manifest['source']['dependencies']}

        def check():
            self.assertEqual((self.rig.manifest, self.rig.clips, recipes), saved)
            for name, contents in source_files.items():
                self.assertEqual((Path(self.rig.root) / name).read_bytes(), contents)
        self.addCleanup(check)

    def assert_same_geometry(self, actual, expected):
        a, b = (evaluate_fk(self.rig.skeleton, pose) for pose in (actual, expected))
        for bone in b:
            np.testing.assert_allclose(a[bone], b[bone], rtol=0, atol=1e-9, err_msg=bone)
        a, b = (evaluate_deformation(self.rig, pose) for pose in (actual, expected))
        for mesh in b:
            np.testing.assert_allclose(a[mesh]['positions'], b[mesh]['positions'],
                                       rtol=0, atol=1e-9, err_msg=mesh)

    def travel_fixture(self, *, diagonal=False):
        # Reuse the existing real fixture author through its module, so unittest
        # does not rediscover the imported TestCase class as part of this suite.
        helper = contact_helpers.RigStateRuntimeContactTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.rig = helper.rig
        clip = helper.clip('travel', start=0., end=1.)
        if diagonal:
            track = next(t for t in clip['tracks']
                         if t['target'] == helper.root and t['path'] == 'translation')
            track['values'][-1][2] = 1.
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='root-bounds', bone=helper.root,
                 type='translation', axes=[0, 2] if diagonal else [0],
                 minimum=0., maximum=0.)]
        validate_skeleton(self.rig.skeleton)
        validate_controllers(self.rig.skeleton, self.rig.manifest['controllers'])
        validate_animation(self.rig, clip)
        return clip

    def test_native_copy_cannot_escape_hard_translation_limit_for_either_id_order(self):
        original = deepcopy(self.rig.skeleton)
        for reverse in (False, True):
            with self.subTest(reverse_ids=reverse):
                self.rig.manifest['skeleton'] = deepcopy(original)
                self.bounded_copy_fixture(reverse)
                pose = evaluate_animation(self.rig, 'released', .5)['pose']
                # b2's rest-local Y is one; the relative [0,0] limit is hard.
                self.assertEqual(pose['bones']['b2']['translation'][1], 1.)
                np.testing.assert_allclose(evaluate_fk(self.rig.skeleton, pose)['b2'][:3, 3],
                                           [0., 2., 0.], rtol=0, atol=1e-9)

    def test_release_midpoint_boundary_and_after_keep_limits_and_actual_skin_continuous(self):
        original = deepcopy(self.rig.skeleton)
        for reverse in (False, True):
            with self.subTest(reverse_ids=reverse):
                self.rig.manifest['skeleton'] = deepcopy(original)
                self.bounded_copy_fixture(reverse)
                recipes = {'static-positive': 'held', 'static-neutral': 'released'}
                runtime = RigStateRuntime(self.rig, 'static-positive', recipes=recipes,
                                          selected=list(recipes), blend_seconds=.5)
                source = runtime.evaluate(0.)['pose']
                self.assert_same_geometry(runtime.cancel(0.)['pose'], source)
                # The contact correction is zero for this straight, held arm;
                # both legal endpoints therefore have the same deform geometry.
                for time in (.25, .4999999, .5, .5000001):
                    with self.subTest(time=time):
                        result = runtime.evaluate(time)
                        self.assertEqual(result['pose']['bones']['b2']['translation'][1], 1.)
                        expected = deepcopy(source)
                        expected['bones']['marker'] = {
                            'translation': [0., 2. - min(time / .5, 1.), 0.]}
                        self.assert_same_geometry(result['pose'], expected)
                        self.assertEqual(result['contacts'], [])

    def test_world_aim_uses_position_after_copy_and_hard_translation_projection(self):
        original = deepcopy(self.rig.skeleton)
        for reverse in (False, True):
            with self.subTest(reverse_ids=reverse):
                self.rig.manifest['skeleton'] = deepcopy(original)
                self.bounded_copy_fixture(reverse, aim=True)
                pose = evaluate_animation(self.rig, 'released', .5)['pose']
                world = evaluate_fk(self.rig.skeleton, pose)['b2']
                direction = np.array([2., 3., 0.]) - world[:3, 3]
                expected = direction / math.hypot(*direction)
                actual = world[:3, :3] @ np.array([0., 1., 0.])
                actual /= math.hypot(*actual)
                np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-9)
                self.assertEqual(pose['bones']['b2']['translation'][1], 1.)

    def test_native_solver_reports_representable_distances_and_rejects_unrepresentable_distance(self):
        self.travel_fixture()
        pose = neutral_pose(self.rig.skeleton)
        original = deepcopy(pose)
        for solver in ('two-bone', 'fabrik'):
            chain = deepcopy(self.rig.manifest['controllers']['ik'][0])
            chain['solver'] = solver
            for target in ([1e155, 1.5, 0.], [1e308, 1e308, 0.]):
                with self.subTest(solver=solver, target=target):
                    self.assertTrue(math.isfinite(math.dist([0., 0., 0.], target)))
                    with np.errstate(over='ignore', invalid='ignore'):
                        result = solve_ik(self.rig.skeleton, pose, chain, target)
                    endpoint = evaluate_fk(self.rig.skeleton, result.pose)['b2'][:3, 3]
                    measured = math.dist(endpoint, target)
                    self.assertTrue(math.isfinite(result.residual_m))
                    self.assertTrue(math.isclose(result.residual_m, measured, rel_tol=1e-14))
                    self.assertFalse(result.reachable)
                    self.assertFalse(result.converged)
                    json.dumps(result.report(), allow_nan=False)
                    self.assertEqual(pose, original)
            with self.subTest(solver=solver, distance='unrepresentable'):
                target = [1.3e308, 1.3e308, 0.]
                self.assertTrue(all(math.isfinite(x) for x in target))
                self.assertTrue(math.isinf(math.dist([0., 0., 0.], target)))
                with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as failure:
                    solve_ik(self.rig.skeleton, pose, chain, target)
                self.assertTrue(failure.exception.report['code'].startswith('RIG_'))
                json.dumps(failure.exception.report, allow_nan=False)
                self.assertEqual(pose, original)

    def test_runtime_final_and_per_solver_contact_reports_remain_finite_and_strict_json(self):
        clip = self.travel_fixture()
        recipes = {'static-positive': clip['id']}
        self.preserve_sources(recipes)
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes=recipes,
                                  selected=list(recipes), blend_seconds=0.)
        with np.errstate(over='ignore', invalid='ignore'):
            result = runtime.evaluate(1e155)
        endpoint = evaluate_fk(self.rig.skeleton, result['pose'])['b2'][:3, 3]
        self.assertEqual(result['status'], 'controls-unsatisfied')
        for field in ('contacts', 'solve_contacts'):
            with self.subTest(report=field):
                self.assertEqual(len(result[field]), 1)
                report = result[field][0]
                measured = math.dist(endpoint, report['target'])
                self.assertTrue(math.isfinite(measured))
                self.assertTrue(math.isfinite(report['residual_m']))
                self.assertTrue(math.isclose(report['residual_m'], measured, rel_tol=1e-14))
                self.assertFalse(report['reachable'])
                self.assertFalse(report['converged'])
                json.dumps(report, allow_nan=False)
        with self.subTest(report='complete-result-json'):
            try:
                json.dumps(result, allow_nan=False)
            except ValueError as error:
                self.fail(f'Runtime report must serialize as strict JSON: {error}')
        for mesh in evaluate_deformation(self.rig, result['pose']).values():
            self.assertTrue(np.isfinite(mesh['positions']).all())

    def test_unrepresentable_combined_distance_rejects_and_restores_all_runtime_fields(self):
        clip = self.travel_fixture(diagonal=True)
        recipes = {'static-positive': clip['id'], 'static-neutral': clip['id']}
        self.preserve_sources(recipes)
        runtime = RigStateRuntime(self.rig, 'static-positive', recipes=recipes,
                                  selected=list(recipes), blend_seconds=.5)
        # Capture a real source/input snapshot before testing rollback; the
        # failure must preserve more than a newly constructed empty runtime.
        runtime.evaluate(.125)
        runtime.cancel(.125)
        before = runtime.evaluate(.25)
        saved = deepcopy({field: getattr(runtime, field) for field in self.STATE_FIELDS})
        target = [1.3e308, 1.5, 1.3e308]
        self.assertTrue(all(math.isfinite(value) for value in target))
        self.assertTrue(math.isinf(math.dist([0., 2., 0.], target)))
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as failure:
            runtime.evaluate(1.3e308)
        self.assertTrue(failure.exception.report['code'].startswith('RIG_'))
        self.assertTrue(failure.exception.report['message'])
        json.dumps(failure.exception.report, allow_nan=False)
        for field, expected in saved.items():
            if field == 'root_offset':
                np.testing.assert_array_equal(getattr(runtime, field), expected)
            else:
                self.assertEqual(getattr(runtime, field), expected, field)
        self.assert_same_geometry(runtime.evaluate(saved['time'])['pose'], before['pose'])


if __name__ == '__main__':
    unittest.main()
