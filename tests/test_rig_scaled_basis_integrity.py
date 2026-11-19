"""Independent native controls for finite chain bases."""
from copy import deepcopy
import json
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_ik import match_fk_to_ik, solve_ik, validate_controllers
from rig_models import SkinWeights, validate_geometry
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from rig_state_runtime import RigStateRuntime
from rig_test_helpers import chain_skeleton, fixture


class NativeContactIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.rig = fixture(temp.name)
        self.mesh = self.rig.geometry.meshes[0]

    def bent(self, parents):
        rig, mesh = self.rig, self.mesh
        names = [f'b{i}' for i in range(parents, parents + 3)]
        rig.manifest['skeleton'] = chain_skeleton(parents + 3)
        chain = rig.manifest['controllers']['ik'][0]
        chain.update(solver='fabrik', bones=names)
        count = len(mesh.positions)
        skin = SkinWeights(mesh.id, ['b0'], np.zeros((count, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (count, 1)), np.eye(4)[None])
        rig.weights = {mesh.id: skin}
        def step(name, path, start, end):
            return dict(schema_version=1, target=name, path=path, interpolation='STEP',
                        times=[0., .5], values=[start, end])

        tracks = []
        for i in range(parents):
            tracks += [step(f'b{i}', 'scale', [1.] * 3, [2.**29] * 3),
                       step(f'b{i}', 'translation', [0., float(i > 0), 0.], [0.] * 3)]
        unit = 2.**(518 - 29 * parents)
        for name, local in zip(names, ([0., -unit, 0.], [0., unit / 2, unit],
                                        [0., unit / 2, -unit])):
            tracks.append(step(name, 'translation', [0., 1., 0.], local))
        goal = dict(schema_version=1, id='plant', chain=chain['id'], start=.5, end=1.,
                    target=[0., 0., 0.], kind='hand')
        clip = dict(deepcopy(rig.clips['neutral']), id='scaled-basis', tracks=tracks, contacts=[goal])
        rig.clips = {clip['id']: clip}
        validate_skeleton(rig.skeleton)
        validate_geometry(rig.geometry)
        validate_skin(skin, mesh, rig.skeleton)
        validate_controllers(rig.skeleton, rig.manifest['controllers'])
        validate_animation(rig, clip)
        seed = evaluate_animation(rig, clip, .5, contacts=False)['pose']
        validate_contract(seed, 'Pose')
        world = evaluate_fk(rig.skeleton, seed)
        radius = 2.**518
        np.testing.assert_array_equal([world[name][:3, 3] for name in names],
                                      [[0., -radius, 0.], [0., -radius/2, radius], [0., 0., 0.]])
        np.testing.assert_array_equal(world[names[0]][:3, :3], np.eye(3) * 2.**(29 * parents))
        np.testing.assert_array_equal(evaluate_deformation(rig, seed)[mesh.id]['positions'],
                                      mesh.positions * 2.**29)
        return clip, chain, seed

    def exercise_bent(self, parents):
        rig = self.rig
        clip, chain, seed = self.bent(parents)
        state = 'static-positive'
        runtime = RigStateRuntime(rig, state, recipes={state: clip['id']}, selected=[state], blend_seconds=0.)
        self.assertEqual(runtime.evaluate(.25)['status'], 'playing')
        goal = [0., 0., 0.]
        with np.errstate(over='ignore', invalid='ignore'):
            with self.subTest(operation='match'):
                matched = match_fk_to_ik(rig.skeleton, seed, chain)
                np.testing.assert_array_equal(matched['pole'], [0., -2.**518, 2.**518])
                json.dumps(matched, allow_nan=False)
            with self.subTest(operation='solve'):
                solved = solve_ik(rig.skeleton, seed, chain, goal)
                self.assertTrue(solved.converged)
                self.assertEqual(solved.residual_m, 0.)
            with self.subTest(operation='animation'):
                frame = evaluate_animation(rig, clip, .5)
                self.assertTrue(frame['final_contacts'][0]['converged'])
                json.dumps(frame, allow_nan=False)
            with self.subTest(operation='runtime'):
                frame = runtime.evaluate(.5)
                self.assertEqual((runtime.time, frame['status']), (.5, 'playing'))
                json.dumps(frame, allow_nan=False)

    def test_same_geometry_smaller_scale_control(self):
        self.exercise_bent(17)

    def test_same_geometry_larger_scale(self):
        self.exercise_bent(18)


if __name__ == '__main__':
    unittest.main()
