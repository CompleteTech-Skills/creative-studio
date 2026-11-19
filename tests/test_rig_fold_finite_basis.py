"""Synthetic folded, finite world-basis control; no external assets."""
from copy import deepcopy
import json
import unittest

import numpy as np
from rig_animation import evaluate_animation
from rig_ik import solve_ik
from rig_skeleton import evaluate_fk
from rig_state_runtime import RigStateRuntime
import test_rig_contact_numeric_boundaries as fixtures


class IntegratedFoldControls(unittest.TestCase):
    def run_case(self, parents, operation):
        with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
            self.assert_case(parents, operation)

    def assert_case(self, parents, operation):
        helper = fixtures.RigContactNumericBoundaryTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.bind_root(parents + 4)
        rig = helper.rig
        factor = 2.**(29 * parents)
        names = [f'b{i}' for i in range(parents + 1, parents + 4)]
        chain = rig.manifest['controllers']['ik'][0]
        chain.update(bones=names, solver='two-bone', pole=[-2., 3., 1.])
        tracks = []
        for i in range(parents + 2):
            tracks.append(fixtures.step(f'b{i}', 'translation', [0., .5], [[0., float(i > 0), 0.], [0., 0., 0.]]))
        for i in range(parents + 1):
            scale = [2.**29] * 3 if i < parents else [3., 2., 1.]
            tracks.append(fixtures.step(f'b{i}', 'scale', [0., .5], [[1.] * 3, scale]))
        for name, sign in zip(names[1:], [1., -1.]):
            tracks.append(fixtures.step(name, 'translation', [0., .5], [[0., 1., 0.], [sign / factor] * 3]))
        goal = dict(schema_version=1, id='plant', chain=chain['id'], start=.5, end=1., target=[0.] * 3, kind='hand')
        clip = helper.install_clip(tracks, [goal], 'fold-control')
        pose = evaluate_animation(rig, clip, .5, contacts=False)['pose']
        actual = helper.assert_geometry(pose)
        np.testing.assert_array_equal(actual[names[1]][:3, 3], [3, 2, 1])
        np.testing.assert_array_equal(actual[names[-1]][:3, 3], [0, 0, 0])
        # A local Z quarter-turn and diagonal scale explicitly realize the pole.
        witness = deepcopy(pose)
        witness['bones'][names[0]].update(rotation=[0., 0., 2**-.5, 2**-.5], scale=[1.5, 2./3, 1.])
        after = helper.assert_geometry(witness)
        np.testing.assert_allclose(after[names[1]][:3, 3], chain['pole'], rtol=0., atol=1e-12)
        np.testing.assert_array_equal(after[names[-1]][:3, 3], [0, 0, 0])
        if operation == 'solve':
            result = solve_ik(rig.skeleton, pose, chain, [0., 0., 0.])
            self.assertTrue(result.converged)
            output = result.pose
        elif operation == 'animation':
            frame = evaluate_animation(rig, clip, .5)
            self.assertTrue(frame['final_contacts'][0]['converged'])
            json.dumps(frame, allow_nan=False)
            output = frame['pose']
        else:
            state = 'static-positive'
            runtime = RigStateRuntime(rig, state, recipes={state: clip['id']}, selected=[state])
            self.assertEqual(runtime.evaluate(.25)['status'], 'playing')
            frame = runtime.evaluate(.5)
            self.assertEqual((runtime.time, frame['status']), (.5, 'playing'))
            json.dumps(frame, allow_nan=False)
            output = frame['pose']
        final = helper.assert_geometry(output)
        np.testing.assert_allclose(final[names[1]][:3, 3], chain['pole'], rtol=0., atol=1e-12)
        np.testing.assert_array_equal(final[names[-1]][:3, 3], [0, 0, 0])

    def test_smaller_finite_basis(self):
        for operation in ('solve', 'animation', 'runtime'):
            with self.subTest(operation=operation):
                self.run_case(17, operation)

    def test_larger_finite_basis(self):
        for operation in ('solve', 'animation', 'runtime'):
            with self.subTest(operation=operation):
                self.run_case(18, operation)


if __name__ == '__main__':
    unittest.main(defaultTest='IntegratedFoldControls', verbosity=2)
