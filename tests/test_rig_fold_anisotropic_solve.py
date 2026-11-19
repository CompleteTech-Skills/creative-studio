"""Native attainable folded turns retain independent tiny parent axes."""
from copy import deepcopy
import json
import math
import unittest

import numpy as np

from rig_animation import evaluate_animation
from rig_ik import solve_ik
from rig_math import axis_angle
from rig_state_runtime import RigStateRuntime
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


if __name__ == '__main__':
    unittest.main()
