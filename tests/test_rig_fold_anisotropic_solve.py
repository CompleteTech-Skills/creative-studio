"""Native IK geometry under extreme parent scales and world translations."""
from copy import deepcopy
import json
import math
import unittest

import numpy as np

from rig_animation import evaluate_animation
from rig_contracts import validate_contract
from rig_ik import match_fk_to_ik, solve_ik
from rig_math import axis_angle
from rig_skeleton import evaluate_fk, neutral_pose, validate_skeleton
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import chain_skeleton
import test_rig_contact_numeric_boundaries as fixtures


class AnisotropicRelativeSolveTests(unittest.TestCase):
    def check_basis(self, parents):
        case = fixtures.RigContactNumericBoundaryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.bind_root(parents + 3)
        rig = case.rig
        names = [f'b{i}' for i in range(parents, parents + 3)]
        chain = rig.manifest['controllers']['ik'][0]
        chain.update(bones=names, solver='two-bone', pole=[0., 0., 1.])
        tracks = [fixtures.step(f'b{i}', 'translation', [0., .5],
                                [[0., float(i > 0), 0.], [0.] * 3])
                  for i in range(parents + 1)]
        tracks.extend(fixtures.step(f'b{i}', 'scale', [0., .5],
                                    [[1.] * 3, [2.**29, 2.**-3, 2.**29]])
                      for i in range(parents))
        for name, sign in zip(names[1:], (1., -1.)):
            tracks.append(fixtures.step(name, 'translation', [0., .5],
                [[0., 1., 0.], [math.ldexp(sign, -29 * parents), 0., 0.]]))
        contact = dict(schema_version=1, id='plant', chain=chain['id'], start=.5, end=1.,
                       target=[0.] * 3, kind='hand')
        clip = case.install_clip(tracks, [contact], 'anisotropic-relative-solve')
        pose = evaluate_animation(rig, clip, .5, contacts=False)['pose']
        world = case.assert_geometry(pose)
        np.testing.assert_array_equal(np.diag(world[names[0]][:3, :3]),
            [math.ldexp(1., 29 * parents), math.ldexp(1., -3 * parents), math.ldexp(1., 29 * parents)])
        witness = deepcopy(pose)
        witness['bones'][names[0]]['rotation'] = axis_angle([0., 1., 0.], -math.pi / 2).tolist()
        expected = case.assert_geometry(witness)
        np.testing.assert_allclose(expected[names[1]][:3, 3], chain['pole'], rtol=0., atol=1e-12)
        np.testing.assert_array_equal(expected[names[-1]][:3, 3], [0.] * 3)
        for operation in ('solve', 'animation', 'runtime'):
            with self.subTest(operation=operation), np.errstate(over='ignore'):
                if operation == 'solve':
                    result = solve_ik(rig.skeleton, pose, chain, [0.] * 3)
                    self.assertTrue(result.converged)
                    output = result.pose
                elif operation == 'animation':
                    result = evaluate_animation(rig, clip, .5)
                    self.assertTrue(result['final_contacts'][0]['converged'])
                    json.dumps(result, allow_nan=False)
                    output = result['pose']
                else:
                    state = 'static-positive'
                    runtime = RigStateRuntime(rig, state, recipes={state: clip['id']}, selected=[state])
                    self.assertEqual(runtime.evaluate(.25)['status'], 'playing')
                    result = runtime.evaluate(.5)
                    self.assertEqual((runtime.time, result['status']), (.5, 'playing'))
                    json.dumps(result, allow_nan=False)
                    output = result['pose']
                actual = case.assert_geometry(output)
                np.testing.assert_allclose(actual[names[1]][:3, 3], chain['pole'], rtol=0., atol=1e-12)
                np.testing.assert_array_equal(actual[names[-1]][:3, 3], [0.] * 3)

    def test_smaller_independent_parent_axes(self):
        self.check_basis(17)

    def test_extreme_independent_parent_axes(self):
        self.check_basis(35)


class RepresentedMatchingPoleTests(unittest.TestCase):
    def setUp(self):
        self.skeleton = chain_skeleton(27)
        validate_skeleton(self.skeleton)
        self.chain = dict(schema_version=1, id='arm', bones=['b24', 'b25', 'b26'],
                          solver='two-bone', pole=[0., 0., 1.], tolerance=1e-6, iterations=32)

    def native_bend(self, origin_x, sign):
        pose = neutral_pose(self.skeleton)
        # Twenty-four bounded scales compose to 2**576. At that scale the
        # small translations below still produce two distinct, finite links.
        for i in range(24):
            pose['bones'][f'b{i}'] = dict(translation=[0.] * 3, scale=[2.**24] * 3)
        pose['bones']['b24'] = dict(translation=[origin_x / 2.**576, 0., 0.])
        pose['bones']['b25'] = dict(translation=[sign * 2.**-50, 2.**-57, 0.])
        pose['bones']['b26'] = dict(translation=[-sign * 2.**-50, 2.**-57, 0.])
        validate_contract(pose, 'Pose')
        world = evaluate_fk(self.skeleton, pose)
        points = np.array([world[name][:3, 3] for name in self.chain['bones']])
        np.testing.assert_array_equal(points, [
            [origin_x, 0., 0.],
            [origin_x + sign * 2.**526, 2.**519, 0.],
            [origin_x, 2.**520, 0.],
        ])
        self.assertEqual(points[1, 0] - points[0, 0], sign * 2.**526)
        return pose, points

    def assert_solved_geometry(self, pose, points, **targets):
        with np.errstate(over='ignore'):
            result = solve_ik(self.skeleton, pose, self.chain, **targets)
        self.assertTrue(result.converged, result.report())
        world = evaluate_fk(self.skeleton, result.pose)
        np.testing.assert_array_equal([world[name][:3, 3] for name in self.chain['bones']], points)

    def test_native_elbow_pole_preserves_geometry(self):
        for sign in (-1., 1.):
            with self.subTest(sign=sign):
                pose, points = self.native_bend(2.**576, sign)
                self.assert_solved_geometry(pose, points, target=points[-1], pole=points[1],
                                            orientation=[0., 0., 0., 1.])

    def test_match_preserves_measured_bend_at_large_world_origins(self):
        # 2**568 resolves a reach-sized X offset. At 2**576 the same
        # displacement rounds away, while the source elbow remains distinct.
        for origin_x in (2.**568, 2.**576):
            for sign in (-1., 1.):
                with self.subTest(origin_x=origin_x, sign=sign):
                    pose, points = self.native_bend(origin_x, sign)
                    with np.errstate(over='ignore'):
                        matched = match_fk_to_ik(self.skeleton, pose, self.chain)
                    json.dumps(matched, allow_nan=False)
                    np.testing.assert_array_equal(matched['target'], points[-1])
                    self.assertGreater(sign * (matched['pole'][0] - origin_x), 0.)
                    self.assertEqual(matched['pole'][2], 0.)
                    self.assert_solved_geometry(pose, points, **matched)


class StraightChainMatchingPoleTests(unittest.TestCase):
    def test_straight_chain_far_along_its_arbitrary_bend_still_matches(self):
        # A straight chain has no measured bend, so _pole_perpendicular picks
        # -Z. With the root far along that axis the reach-sized pole rounds
        # back onto the chain; solve_ik reproduces the same arbitrary side.
        skeleton = chain_skeleton(4)
        validate_skeleton(skeleton)
        for z, segment in ((4e10, 2e-8), (1e17, 1.), (1e17, .25)):
            for solver in ('two-bone', 'fabrik'):
                with self.subTest(z=z, segment=segment, solver=solver):
                    chain = dict(schema_version=1, id='arm', bones=['b1', 'b2', 'b3'], solver=solver,
                                 pole=[0., 0., 1.], tolerance=1e-6, iterations=32)
                    scale = z / 1e9
                    pose = neutral_pose(skeleton)
                    pose['bones']['b0'] = dict(translation=[0.] * 3, scale=[scale] * 3)
                    pose['bones']['b1'] = dict(translation=[0., 0., 1e9], scale=[segment / scale] * 3)
                    pose['bones']['b2'] = dict(translation=[0., 1., 0.])
                    pose['bones']['b3'] = dict(translation=[0., 1., 0.])
                    validate_contract(pose, 'Pose')
                    with np.errstate(over='ignore'):
                        matched = match_fk_to_ik(skeleton, pose, chain)
                        result = solve_ik(skeleton, pose, chain, **matched)
                    json.dumps(matched, allow_nan=False)
                    self.assertTrue(result.converged, result.report())
                    before, after = evaluate_fk(skeleton, pose), evaluate_fk(skeleton, result.pose)
                    np.testing.assert_array_equal([after[name][:3, 3] for name in chain['bones']],
                                                  [before[name][:3, 3] for name in chain['bones']])

    def test_exactly_straight_chain_with_rounded_elbow_offset_still_matches(self):
        # Bounded scales place an exactly collinear chain about 1e22 from the
        # origin. The elbow's axis offset there is rounding above the absolute
        # 1e-8 cutoff, not a measured bend.
        skeleton = chain_skeleton(7)
        validate_skeleton(skeleton)
        for origin, step in (([-1.658168018455376e+22, -1.3407743160560732e+22, 8.171286620556768e+19], [-10, -40, -1]),
                             ([2.0441029244368707e+22, 6.144977582749839e+22, -2.0054775724344054e+18], [-31, 1, 8])):
            delta = (np.array(step, float) * np.spacing(np.abs(origin))).tolist()
            for solver in ('two-bone', 'fabrik'):
                with self.subTest(origin=origin, solver=solver):
                    chain = dict(schema_version=1, id='arm', bones=['b4', 'b5', 'b6'], solver=solver,
                                 pole=[0., 0., 1.], tolerance=1e-6, iterations=32)
                    pose = neutral_pose(skeleton)
                    pose['bones']['b0'] = dict(translation=[0.] * 3, scale=[1e9] * 3)
                    pose['bones']['b1'] = dict(translation=[0.] * 3, scale=[1e9] * 3)
                    pose['bones']['b2'] = dict(translation=(np.array(origin) / 1e18).tolist(), scale=[1e-9] * 3)
                    pose['bones']['b3'] = dict(translation=[0.] * 3, scale=[1e-9] * 3)
                    pose['bones']['b4'] = dict(translation=[0.] * 3)
                    pose['bones']['b5'] = dict(translation=delta)
                    pose['bones']['b6'] = dict(translation=delta)
                    validate_contract(pose, 'Pose')
                    before = evaluate_fk(skeleton, pose)
                    points = np.array([before[name][:3, 3] for name in chain['bones']])
                    np.testing.assert_array_equal(points[2] - points[0], 2 * (points[1] - points[0]))
                    with np.errstate(over='ignore'):
                        matched = match_fk_to_ik(skeleton, pose, chain)
                        result = solve_ik(skeleton, pose, chain, **matched)
                    self.assertTrue(result.converged, result.report())
                    after = evaluate_fk(skeleton, result.pose)
                    np.testing.assert_array_equal([after[name][:3, 3] for name in chain['bones']], points)

    def test_near_closed_bend_at_large_origin_keeps_its_elbow_pole(self):
        # Reach is a few ulps of the origin, so the rounding-scaled threshold
        # exceeds this genuine elbow offset; the elbow pole must still be used.
        def unit_axis(vector):
            vector = np.array(vector, float)
            return vector / np.linalg.norm(vector)
        skeleton = chain_skeleton(5)
        validate_skeleton(skeleton)
        cases = (
            ('fabrik', [629502095.0162318, 171908285.117486, -254015059.12116075], 2053.3762796853375,
             ([-0.5536609608194913, -1.1397926801093514, 0.6780836173485412], 0.21129582805937508),
             [-178312464.38862312, 588789181.2472327, -244328292.21096325], 0.0005281084823653652,
             ([-0.6216212108246881, 0.7200588448720091, -1.889221242077786], 0.5905607149602037),
             3.1415594435746437),
            ('two-bone', [916547363.6270716, -413886739.17713237, -558610904.9364548], 185.2746307041082,
             ([-0.03102266562061253, -1.1030145887121454, 0.524622926814447], 0.943575383028631),
             [604655516.078901, 471428423.5494137, 534404700.4437182], 0.00466802582579622,
             ([2.1363002817633916, -0.12060217379751542, -0.7103442791824164], 2.6310501503946573),
             3.1415922988004445))
        for solver, t0, s0, r0, t1, s1, r2, fold in cases:
            with self.subTest(solver=solver):
                chain = dict(schema_version=1, id='arm', bones=['b2', 'b3', 'b4'], solver=solver,
                             pole=[0., 0., 1.], tolerance=1e-6, iterations=32)
                pose = neutral_pose(skeleton)
                pose['bones']['b0'] = dict(translation=t0, scale=[s0] * 3,
                                           rotation=axis_angle(unit_axis(r0[0]), r0[1]).tolist())
                pose['bones']['b1'] = dict(translation=t1, scale=[s1] * 3)
                pose['bones']['b2'] = dict(rotation=axis_angle(unit_axis(r2[0]), r2[1]).tolist())
                pose['bones']['b3'] = dict(rotation=axis_angle([1, 0, 0], fold).tolist())
                validate_contract(pose, 'Pose')
                before = evaluate_fk(skeleton, pose)
                points = [before[name][:3, 3] for name in chain['bones']]
                with np.errstate(over='ignore'):
                    matched = match_fk_to_ik(skeleton, pose, chain)
                    result = solve_ik(skeleton, pose, chain, **matched)
                self.assertTrue(result.converged, result.report())
                after = evaluate_fk(skeleton, result.pose)
                np.testing.assert_array_equal([after[name][:3, 3] for name in chain['bones']], points)

if __name__ == '__main__':
    unittest.main()
