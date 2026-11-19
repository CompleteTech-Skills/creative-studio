"""Native bent chain with a representable radius whose square overflows binary64."""
from copy import deepcopy
import json
import math
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


class BentFabrikDistanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)
        self.mesh = self.rig.geometry.meshes[0]
        self.rig.manifest['skeleton'] = chain_skeleton(20)
        self.names = ['b17', 'b18', 'b19']
        self.chain = self.rig.manifest['controllers']['ik'][0]
        self.chain.update(solver='fabrik', bones=self.names)
        vertices = len(self.mesh.positions)
        skin = SkinWeights(self.mesh.id, ['b0'], np.zeros((vertices, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (vertices, 1)), np.eye(4)[None])
        self.rig.weights = {self.mesh.id: skin}

        def step(name, path, start, end):
            return dict(schema_version=1, target=name, path=path, interpolation='STEP',
                        times=[0., .5], values=[start, end])

        tracks = []
        for i in range(17):
            tracks.append(step(f'b{i}', 'scale', [1.] * 3, [2.**29] * 3))
            tracks.append(step(f'b{i}', 'translation', [0., float(i > 0), 0.], [0.] * 3))
        # All authored values fit their declared domains. The two links form
        # a bent, already matched chain in the YZ plane with elbow toward +Z.
        self.radius = 2.**518
        for name, local in zip(self.names, ([0., -2.**25, 0.], [0., 2.**24, 2.**25],
                                           [0., 2.**24, -2.**25])):
            tracks.append(step(name, 'translation', [0., 1., 0.], local))
        self.contact = dict(schema_version=1, id='hand-plant', chain=self.chain['id'],
                            start=.5, end=1., target=[0., 0., 0.], kind='hand')
        self.clip = dict(deepcopy(self.rig.clips['neutral']), id='bent-fabrik',
                         tracks=tracks, contacts=[self.contact])
        self.rig.clips = {self.clip['id']: self.clip}
        validate_skeleton(self.rig.skeleton)
        validate_geometry(self.rig.geometry)
        validate_skin(skin, self.mesh, self.rig.skeleton)
        validate_controllers(self.rig.skeleton, self.rig.manifest['controllers'])
        validate_animation(self.rig, self.clip)
        self.seed = evaluate_animation(self.rig, self.clip, .5, contacts=False)['pose']

    def assert_geometry(self, pose):
        validate_contract(pose, 'Pose')
        world = evaluate_fk(self.rig.skeleton, pose)
        for matrix in world.values():
            self.assertTrue(np.isfinite(matrix).all())
        points = [world[name][:3, 3] for name in self.names]
        np.testing.assert_array_equal(points, [[0., -self.radius, 0.],
                                              [0., -self.radius / 2, self.radius],
                                              [0., 0., 0.]])
        lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
        self.assertEqual(lengths, [math.hypot(self.radius / 2, self.radius)] * 2)
        positions = evaluate_deformation(self.rig, pose)[self.mesh.id]['positions']
        np.testing.assert_array_equal(positions, self.mesh.positions * 2.**29)

    def assert_contact(self, record):
        json.dumps(record, allow_nan=False)
        self.assertTrue(record['reachable'])
        self.assertTrue(record['converged'])
        self.assertEqual(record['residual_m'], 0.)

    def test_contact_free_native_geometry_control(self):
        frame = evaluate_animation(self.rig, self.clip, .5, contacts=False)
        json.dumps(frame, allow_nan=False)
        self.assertEqual(frame['contacts'], [])
        self.assert_geometry(frame['pose'])

    def test_same_pose_two_bone_positive_control(self):
        self.chain['solver'] = 'two-bone'
        validate_controllers(self.rig.skeleton, self.rig.manifest['controllers'])
        with np.errstate(over='ignore', invalid='ignore'):
            frame = evaluate_animation(self.rig, self.clip, .5)
        self.assert_geometry(frame['pose'])
        self.assert_contact(frame['contacts'][0])
        self.assert_contact(frame['final_contacts'][0])

    def test_bent_fabrik_direct_solve_retains_matched_seed(self):
        with np.errstate(over='ignore', invalid='ignore'):
            solved = solve_ik(self.rig.skeleton, self.seed, self.chain, self.contact['target'])
        self.assert_geometry(solved.pose)
        self.assert_contact(solved.report())

    def test_bent_fabrik_animation_measures_satisfied_contact(self):
        with np.errstate(over='ignore', invalid='ignore'):
            frame = evaluate_animation(self.rig, self.clip, .5)
        self.assert_geometry(frame['pose'])
        self.assert_contact(frame['contacts'][0])
        self.assert_contact(frame['final_contacts'][0])

    def test_bent_fabrik_runtime_advances_into_satisfied_contact(self):
        runtime = RigStateRuntime(self.rig, 'static-positive',
                                  recipes={'static-positive': self.clip['id']},
                                  selected=['static-positive'], blend_seconds=0.)
        self.assertEqual(runtime.evaluate(.25)['status'], 'playing')
        with np.errstate(over='ignore', invalid='ignore'):
            frame = runtime.evaluate(.5)
        self.assertEqual((runtime.time, frame['status']), (.5, 'playing'))
        self.assert_geometry(frame['pose'])
        self.assert_contact(frame['solve_contacts'][0])
        self.assert_contact(frame['contacts'][0])
        json.dumps(frame, allow_nan=False)

    def test_match_returns_finite_measured_world_pole(self):
        with np.errstate(over='ignore', invalid='ignore'):
            matched = match_fk_to_ik(self.rig.skeleton, self.seed, self.chain)
        json.dumps(matched, allow_nan=False)
        np.testing.assert_array_equal(matched['target'], [0., 0., 0.])
        np.testing.assert_array_equal(matched['pole'], [0., -self.radius, self.radius])
        np.testing.assert_array_equal(matched['orientation'], [0., 0., 0., 1.])


if __name__ == '__main__':
    unittest.main()
