"""Regression oracles solve real contacts after complete runtime placement."""
from copy import deepcopy
import tempfile
import unittest

import numpy as np

from rig_test_helpers import fixture
from rig_animation import clip_from_poses, evaluate_animation
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime


class RigStateRuntimeContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name)
        self.root = self.rig.skeleton['root']

    def clip(self, name, start=0., end=.5, *, motion='embedded', contact=True):
        poses = [neutral_pose(self.rig.skeleton), neutral_pose(self.rig.skeleton)]
        for pose, x in zip(poses, (start, end)):
            pose['bones'][self.root] = {'translation': [x, 0., 0.]}
        contacts = [dict(schema_version=1, id='plant', chain='arm',
                         start=0., end=1., target=[start, 1.5, 0.], kind='foot')]
        clip = clip_from_poses(self.rig, name, [0., 1.], poses, loop=True,
                               root_motion=motion, contacts=contacts if contact else [])
        self.rig.clips[name] = clip
        return clip

    def preserve_inputs(self, recipes):
        manifest, clips, original_recipes = deepcopy((self.rig.manifest,
                                                       self.rig.clips, recipes))

        def check():
            self.assertEqual(self.rig.manifest, manifest)
            self.assertEqual(self.rig.clips, clips)
            self.assertEqual(recipes, original_recipes)

        self.addCleanup(check)

    def runtime(self, recipes, initial='static-positive'):
        self.preserve_inputs(recipes)
        return RigStateRuntime(self.rig, initial, recipes=recipes,
                               selected=list(recipes), blend_seconds=0)

    def assert_pose_geometry(self, actual, expected):
        actual_fk = evaluate_fk(self.rig.skeleton, actual)
        expected_fk = evaluate_fk(self.rig.skeleton, expected)
        self.assertEqual(set(actual_fk), set(expected_fk))
        for bone in expected_fk:
            np.testing.assert_allclose(actual_fk[bone], expected_fk[bone],
                                       rtol=0, atol=1e-9, err_msg=bone)
        actual_meshes = evaluate_deformation(self.rig, actual)
        expected_meshes = evaluate_deformation(self.rig, expected)
        self.assertEqual(set(actual_meshes), set(expected_meshes))
        for mesh in expected_meshes:
            np.testing.assert_allclose(actual_meshes[mesh]['positions'],
                                       expected_meshes[mesh]['positions'],
                                       rtol=0, atol=1e-9, err_msg=mesh)

    def placed_clip(self, clip, offset):
        # Oracle authored in world space: place keys and contact targets before
        # native evaluation, leaving the rig's authored world-space pole fixed.
        result = deepcopy(clip)
        displacement = np.array([offset, 0., 0.])
        for track in result['tracks']:
            if track['target'] == self.root and track['path'] == 'translation':
                track['values'] = (np.array(track['values']) + displacement).tolist()
        for contact in result['contacts']:
            contact['target'] = (np.array(contact['target']) + displacement).tolist()
        return result

    def test_embedded_contact_loops_match_native_fk_and_skin(self):
        clip = self.clip('planted-travel')
        runtime = self.runtime({'static-positive': clip['id']})
        for time in (0., .25, .875, 1., 1.25, 2.25, 3.75):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)
                self.assertTrue(expected['contacts'][0]['converged'])
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected['pose'])

    def test_in_place_contact_loops_are_native_control(self):
        clip = self.clip('planted-inplace', motion='in-place')
        runtime = self.runtime({'static-positive': clip['id']})
        first = evaluate_animation(self.rig, clip, .25, loop=True)['pose']
        for time in (.25, 1.25, 2.25, 3.25):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)['pose']
                self.assert_pose_geometry(expected, first)
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected)

    def test_post_solve_translation_is_detectable_despite_correct_endpoint(self):
        clip = self.clip('negative-control')
        self.preserve_inputs({'static-positive': clip['id']})
        local = evaluate_animation(self.rig, clip, .25, loop=True)['pose']
        translated = deepcopy(local)
        translated['bones'][self.root]['translation'][0] += 1.
        expected = evaluate_animation(self.rig, clip, 2.25, loop=True)['pose']
        a, b = (evaluate_fk(self.rig.skeleton, p) for p in (translated, expected))
        np.testing.assert_allclose(a['b2'][:3, 3], b['b2'][:3, 3], rtol=0, atol=1e-9)
        self.assertGreater(np.linalg.norm(a['b1'][:3, 3]-b['b1'][:3, 3]), .05)
        old_mesh = evaluate_deformation(self.rig, translated)['strip']['positions']
        native_mesh = evaluate_deformation(self.rig, expected)['strip']['positions']
        self.assertGreater(np.max(np.abs(old_mesh-native_mesh)), .001)

    def test_displaced_recipe_entry_solves_against_fixed_world_pole(self):
        self.clip('source-travel', contact=False)
        target = self.clip('target-travel', start=4., end=4.5)
        recipes = {'static-neutral': 'source-travel',
                   'static-positive': 'target-travel'}
        runtime = self.runtime(recipes, initial='static-neutral')
        # Source root is .5 * .5 = .25 when entering the target at time .5.
        # Align target start 4 to .25: all root keys/plants shift by -3.75.
        oracle = self.placed_clip(target, -3.75)
        runtime.request('static-positive', .5)
        for elapsed in (0., .25, .875, 1.25, 2.25):
            with self.subTest(elapsed=elapsed):
                expected = evaluate_animation(self.rig, oracle, elapsed, loop=True)
                np.testing.assert_allclose(
                    expected['pose']['bones'][self.root]['translation'],
                    [.25 + .5*elapsed, 0., 0.], rtol=0, atol=1e-12)
                self.assertTrue(expected['contacts'][0]['converged'])
                self.assert_pose_geometry(runtime.evaluate(.5+elapsed)['pose'],
                                          expected['pose'])

    def test_multi_clip_alignment_includes_sequence_cycles_before_ik(self):
        first = self.clip('sequence-a', start=.5, end=1.)
        second = self.clip('sequence-b', start=4., end=4.25)
        runtime = self.runtime({'static-positive': ['sequence-a', 'sequence-b']})
        # A begins at .5 and ends at 1; B therefore shifts by -3.
        # One complete A+B sequence travels .5 + .25 = .75.
        samples = [
            (0., first, 0., 0.),
            (.25, first, .25, 0.),
            (1., second, 0., -3.),
            (1.25, second, .25, -3.),
            (2., first, 0., .75),
            (2.25, first, .25, .75),
            (3.25, second, .25, -2.25),
            (4.75, first, .75, 1.5),
        ]
        for time, clip, local_time, offset in samples:
            with self.subTest(time=time, clip=clip['id']):
                oracle = self.placed_clip(clip, offset)
                expected = evaluate_animation(self.rig, oracle, local_time, loop=False)
                self.assertTrue(expected['contacts'][0]['converged'])
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected['pose'])

    def test_world_aim_constraint_observes_loop_displacement(self):
        clip = self.clip('aim-travel', contact=False)
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='world-aim', bone='b1', type='aim',
                 axis=[0., 1., 0.], point=[-1., 3., 2.])
        ]
        runtime = self.runtime({'static-positive': clip['id']})
        for time in (.25, 1.25, 2.25, 3.75):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)
                self.assertTrue(expected['constraints_applied'])
                self.assert_pose_geometry(runtime.evaluate(time)['pose'], expected['pose'])


    def test_authored_contact_cycles_survive_root_translation_limit(self):
        clip = self.clip('limited-root-travel')
        self.rig.manifest['controllers']['constraints'] = [
            dict(schema_version=1, id='root-slide-limit', bone=self.root,
                 type='translation', axes=[0], minimum=0., maximum=.2)
        ]
        runtime = self.runtime({'static-positive': clip['id']})
        # The authored cycle remains .5 even though the solved endpoint root
        # is clamped to .2. Contact placement uses the authored displacement.
        for time in (0., .25, .875, 1., 1.25, 2.25, 2.875, 3.25):
            with self.subTest(time=time):
                expected = evaluate_animation(self.rig, clip, time, loop=True)
                target = [.5*int(time), 1.5, 0.]
                root = [min(.5*time, .2), 0., 0.]
                np.testing.assert_allclose(expected['contacts'][0]['target'],
                                           target, rtol=0, atol=1e-12)
                np.testing.assert_allclose(
                    expected['pose']['bones'][self.root]['translation'],
                    root, rtol=0, atol=1e-12)
                self.assertTrue(expected['contacts'][0]['converged'])
                native_fk = evaluate_fk(self.rig.skeleton, expected['pose'])
                np.testing.assert_allclose(native_fk['b2'][:3, 3], target,
                                           rtol=0, atol=1e-9)
                actual = runtime.evaluate(time)['pose']
                np.testing.assert_allclose(actual['bones'][self.root]['translation'],
                                           root, rtol=0, atol=1e-12)
                self.assert_pose_geometry(actual, expected['pose'])



if __name__ == '__main__':
    unittest.main()
