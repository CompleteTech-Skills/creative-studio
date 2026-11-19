"""Actual native folded contacts under finite, rotated near-maximum bases."""
from copy import deepcopy
import json
import math
import unittest

import numpy as np

from rig_animation import evaluate_animation
from rig_ik import solve_ik
from rig_math import axis_angle, qmatrix
from rig_state_runtime import RigStateRuntime
import test_rig_contact_numeric_boundaries as fixtures


class FiniteRelativeSolveTests(unittest.TestCase):
    def check_basis(self, parents):
        case = fixtures.RigContactNumericBoundaryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.bind_root(parents + 4)
        rig = case.rig
        names = [f'b{i}' for i in range(parents + 1, parents + 4)]
        rotation = axis_angle([0., 0., 1.], math.pi / 4)
        chain = rig.manifest['controllers']['ik'][0]
        chain.update(bones=names, solver='two-bone', pole=(qmatrix(rotation) @ [-2., 3., 1.]).tolist())
        tracks = [fixtures.step(f'b{i}', 'translation', [0., .5],
                                [[0., float(i > 0), 0.], [0.] * 3])
                  for i in range(parents + 2)]
        tracks.append(fixtures.step('b0', 'rotation', [0., .5], [[0., 0., 0., 1.], rotation.tolist()]))
        for i in range(parents + 1):
            scale = [2.**29] * 3 if i < parents else [480., 320., 160.]
            tracks.append(fixtures.step(f'b{i}', 'scale', [0., .5], [[1.] * 3, scale]))
        for name, sign in zip(names[1:], (1., -1.)):
            local = math.ldexp(1., -29 * parents) / 160 * sign
            tracks.append(fixtures.step(name, 'translation', [0., .5], [[0., 1., 0.], [local] * 3]))
        contact = dict(schema_version=1, id='plant', chain=chain['id'], start=.5, end=1.,
                       target=[0.] * 3, kind='hand')
        clip = case.install_clip(tracks, [contact], 'finite-relative-solve')
        pose = evaluate_animation(rig, clip, .5, contacts=False)['pose']
        world = case.assert_geometry(pose)
        self.assertTrue(all(math.isfinite(math.hypot(*world[names[0]][:3, axis])) for axis in range(3)))
        witness = deepcopy(pose)
        witness['bones'][names[0]].update(rotation=[0., 0., 2**-.5, 2**-.5], scale=[1.5, 2./3, 1.])
        expected = case.assert_geometry(witness)
        np.testing.assert_allclose(expected[names[1]][:3, 3], chain['pole'], rtol=0., atol=1e-12)
        np.testing.assert_allclose(expected[names[-1]][:3, 3], [0.] * 3, rtol=0., atol=1e-12)
        for operation in ('solve', 'animation', 'runtime'):
            with self.subTest(operation=operation):
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
                np.testing.assert_allclose(actual[names[-1]][:3, 3], [0.] * 3, rtol=0., atol=1e-12)

    def test_smaller_rotated_basis(self):
        self.check_basis(17)

    def test_near_maximum_rotated_basis(self):
        self.check_basis(35)


if __name__ == '__main__':
    unittest.main()
