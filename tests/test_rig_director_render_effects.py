"""Numerical fixtures and real image decoding, never production 3D approval."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from director_render_effects import (compare_evaluations, image_metrics,
                                     verify_control_pair, _request_control, _sample,
                                     DEFAULT_THRESHOLDS)
from rig_contracts import COORDINATES
from rig_errors import RigError
from render3d_contracts import Render3DError

try:
    import numpy as np
    import png
    DEPENDENCIES = True
except ImportError:
    DEPENDENCIES = False


def identity():
    return [[1., 0., 0., 0.], [0., 1., 0., 0.], [0., 0., 1., 0.], [0., 0., 0., 1.]]


def settings():
    return dict(resolution=[3, 2], output=dict(format='png', bit_depth=8, alpha='opaque',
                color_space='srgb', passes=['combined']))


def evaluation():
    """Explicit synthetic owner-shaped numerical fixture, not a native render."""
    def node(kind):
        return dict(type=kind, world_matrix=identity(), local_matrix=identity(),
                    visible=True, material_overrides=[], world_bounds=None)
    camera = node('camera')
    camera['camera'] = dict(projection='perspective', near=.1, far=100,
                            vertical_fov=.8, orthographic_height=4)
    light = node('light')
    light['light'] = dict(type='point', intensity=10., color=[1., 1., 1.])
    return dict(coordinate_system=COORDINATES, native_geometry_sampled=True,
        authored_mutated=False, camera='camera', world=dict(exposure=-.5),
        nodes=dict(actor=node('mesh'), prop=node('mesh'), camera=camera, key=light),
        native_geometry=dict(actor=dict(body=[[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])))


@unittest.skipUnless(DEPENDENCIES, 'optional native numerical/PNG dependencies unavailable')
class EffectNumericalTests(unittest.TestCase):
    def test_actual_owner_source_paths_and_exact_shot_time_are_consumed(self):
        from scene3d_example import build_example
        from render3d_snapshot import Scene3DAdapter, create_snapshot
        from test_render3d_contracts import request
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = build_example(root/'source')
            create_snapshot(root/'source', root/'snapshot', Scene3DAdapter())
            payload = (root/'snapshot/snapshot.json').read_bytes()
            value = request()
            value.update(project={k: bundle['project'][k] for k in ('id', 'version')},
                scene={k: bundle['project']['scenes'][0][k] for k in ('id', 'version')},
                shot={k: bundle['project']['shots'][0][k] for k in ('id', 'version')},
                camera='camera', source_start=dict(numerator=0, denominator=1),
                snapshot=dict(path='snapshot.json', bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
            (root/'output').mkdir(); (root/'output/frame.png').write_bytes(b'sample-path-only')
            # This source-adapter fixture claims no completed job or image
            # verification. Its real owner evaluator must receive paths and
            # exact source time rather than an unsupported selector dictionary.
            proof = dict(request=value, snapshot_root=root/'snapshot', output_root=root/'output',
                state=dict(frames=[dict(frame=5, state='complete', cache_key='a'*64)]),
                result=dict(images=[dict(frame=5, pass_name='combined', file={'path': 'frame.png'})]))
            sample = _sample(proof, 5, DEFAULT_THRESHOLDS)
            self.assertEqual(sample['evaluation']['scene']['path'], 'example-scene.json')
            self.assertEqual(sample['evaluation']['shot']['path'], 'example-shot.json')
            self.assertEqual(sample['evaluation']['frame'], dict(numerator=0, denominator=1))
            self.assertTrue(sample['fixture'])
            # The example has a real static triangle but no animated native
            # rig. It therefore cannot satisfy a deformation control.
            with self.assertRaises(RigError):
                compare_evaluations(sample['evaluation'], sample['evaluation'], settings(), effect='geometry', actor_ids=['mesh'])

    def test_actual_local_deformation_and_declared_prop_motion(self):
        a, b = evaluation(), evaluation()
        b['native_geometry']['actor']['body'][1][1] = .25
        b['nodes']['prop']['world_matrix'][0][3] = .1
        b['nodes']['prop']['local_matrix'][0][3] = .1
        original = deepcopy((a, b))
        result = compare_evaluations(a, b, settings(), effect='geometry',
                                    actor_ids=['actor'], moving_nodes=['prop'])
        self.assertEqual(result['changed_actors'], ['actor'])
        self.assertAlmostEqual(result['max_local_geometry_delta_m'], .25)
        self.assertFalse(result['native_execution_verified'])
        self.assertEqual((a, b), original)

    def test_root_translation_is_not_deformation(self):
        a, b = evaluation(), evaluation()
        b['nodes']['actor']['world_matrix'][0][3] = 1
        for point in b['native_geometry']['actor']['body']:
            point[0] += 1
        for moving in ([], ['actor']):
            with self.subTest(moving=moving), self.assertRaises(RigError):
                compare_evaluations(a, b, settings(), effect='geometry', actor_ids=['actor'], moving_nodes=moving)

    def test_geometry_change_must_be_declared_and_nonzero(self):
        a, b = evaluation(), evaluation()
        with self.assertRaises(RigError):
            compare_evaluations(a, b, settings(), effect='geometry', actor_ids=['actor'])
        b['native_geometry']['actor']['body'][1][1] = .25
        with self.assertRaises(RigError):
            compare_evaluations(a, b, settings(), effect='geometry', actor_ids=['missing'])
        b['native_geometry']['other'] = deepcopy(b['native_geometry']['actor'])
        b['nodes']['other'] = deepcopy(b['nodes']['actor'])
        with self.assertRaises(RigError):
            compare_evaluations(a, b, settings(), effect='geometry', actor_ids=['actor'])

    def test_camera_translation_and_rotation_are_physical_controls(self):
        a, b = evaluation(), evaluation()
        b['nodes']['camera']['world_matrix'][0][3] = .5
        result = compare_evaluations(a, b, settings(), effect='camera')
        self.assertAlmostEqual(result['camera_translation_m'], .5)
        b['nodes']['camera']['world_matrix'] = [[0., 0., 1., 0.], [0., 1., 0., 0.],
                                               [-1., 0., 0., 0.], [0., 0., 0., 1.]]
        result = compare_evaluations(a, b, settings(), effect='camera')
        self.assertAlmostEqual(result['camera_rotation_rad'], np.pi/2)

    def test_lens_only_change_does_not_establish_camera_motion(self):
        a, b = evaluation(), evaluation()
        b['nodes']['camera']['camera']['vertical_fov'] = .9
        with self.assertRaisesRegex(RigError, 'lens-only'):
            compare_evaluations(a, b, settings(), effect='camera')

    def test_light_intensity_control_preserves_all_other_light_state(self):
        a, b = evaluation(), evaluation()
        b['nodes']['key']['light']['intensity'] = 150.
        result = compare_evaluations(a, b, settings(), effect='lighting', light_ids=['key'])
        self.assertEqual(result['light_intensity_deltas'], dict(key=140.))
        for mutate in (lambda x: x['nodes']['key']['light'].update(color=[1., .5, 1.]),
                       lambda x: x['nodes']['key']['world_matrix'][0].__setitem__(3, 1),
                       lambda x: x['world'].update(exposure=0)):
            changed = deepcopy(b); mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(RigError):
                compare_evaluations(a, changed, settings(), effect='lighting', light_ids=['key'])

    def test_non_target_geometry_camera_material_visibility_light_and_world_fail(self):
        a, b = evaluation(), evaluation()
        b['native_geometry']['actor']['body'][1][1] = .25
        mutations = [lambda x: x['world'].update(exposure=.5),
            lambda x: x['nodes']['actor'].update(visible=False),
            lambda x: x['nodes']['actor'].update(material_overrides=[dict(slot=0, material='different')]),
            lambda x: x['nodes']['camera']['world_matrix'][0].__setitem__(3, .3),
            lambda x: x['nodes']['key']['light'].update(intensity=11),
            lambda x: x['nodes']['prop']['world_matrix'][0].__setitem__(3, .3)]
        for mutate in mutations:
            changed = deepcopy(b); mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(RigError):
                compare_evaluations(a, changed, settings(), effect='geometry', actor_ids=['actor'])
        changed = evaluation(); changed['nodes']['camera']['world_matrix'][0][3] = .3
        changed['native_geometry']['actor']['body'][1][1] = .25
        with self.assertRaises(RigError):
            compare_evaluations(a, changed, settings(), effect='camera')

    def test_repeat_requires_identical_physical_state(self):
        a, b = evaluation(), evaluation()
        result = compare_evaluations(a, b, settings(), effect='noise')
        self.assertTrue(result['numerical_control_verified'])
        b['native_geometry']['actor']['body'][1][1] = .25
        with self.assertRaises(RigError):
            compare_evaluations(a, b, settings(), effect='noise')

    def test_malformed_nonfinite_singular_and_overbudget_geometry_fail(self):
        mutations = [lambda x: x.update(nodes=[]),
            lambda x: x.pop('world'), lambda x: x.update(camera='missing'),
            lambda x: x['native_geometry']['actor'].update(body=[[0., float('nan'), 0.]]),
            lambda x: x['native_geometry']['actor'].update(body=[[0., True, 0.]]),
            lambda x: x['native_geometry']['actor'].update(body=[[0., 0.]]),
            lambda x: x['nodes']['actor'].update(world_matrix=[[0.]*4]*4),
            lambda x: x['nodes']['actor'].pop('world_matrix')]
        for mutate in mutations:
            value = evaluation(); mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(RigError):
                compare_evaluations(value, evaluation(), settings(), effect='noise')
        with self.assertRaises(RigError):
            compare_evaluations(evaluation(), evaluation(), settings(), effect='noise', thresholds=dict(max_vertices=2))

    def test_unknown_effect_and_unsafe_thresholds_fail(self):
        for thresholds in (dict(state_tolerance=1), dict(min_mean_abs=0), dict(max_vertices=True),
                           dict(min_changed_fraction=2), dict(noise_gain=.1), dict(unknown=1)):
            with self.subTest(thresholds=thresholds), self.assertRaises(RigError):
                compare_evaluations(evaluation(), evaluation(), settings(), effect='noise', thresholds=thresholds)


@unittest.skipUnless(DEPENDENCIES, 'optional native numerical/PNG dependencies unavailable')
class ActualImageTests(unittest.TestCase):
    def write(self, path, value):
        with path.open('wb') as stream:
            png.Writer(3, 2, greyscale=False).write(stream, [[value]*9]*2)

    def test_real_png_bytes_are_decoded_and_bound_without_execution_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            a, b = Path(temporary)/'a.png', Path(temporary)/'b.png'
            self.write(a, 0); self.write(b, 51)
            result = image_metrics(a, b, settings())
            self.assertAlmostEqual(result['mean_abs'], .2)
            self.assertAlmostEqual(result['rmse'], .2)
            self.assertEqual(result['changed_fraction'], 1)
            self.assertNotEqual(result['before']['sha256'], result['after']['sha256'])
            self.assertFalse(result['native_execution_verified'])
            self.assertEqual(image_metrics(a, a, settings())['mean_abs'], 0)

    def test_corrupt_symlink_dimension_and_memory_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            a, b = Path(temporary)/'a.png', Path(temporary)/'b.png'
            self.write(a, 0); b.write_bytes(b'not PNG')
            with self.assertRaises((RigError, Render3DError)): image_metrics(a, b, settings())
            b.unlink(); b.symlink_to(a)
            with self.assertRaises((RigError, Render3DError)): image_metrics(a, b, settings())
            with self.assertRaises(RigError): image_metrics(a, a, settings(), thresholds=dict(memory_bytes=1))
            wrong = settings(); wrong['resolution'] = [2, 2]
            with self.assertRaises((RigError, Render3DError)): image_metrics(a, a, wrong)


class NativeAuthorityTests(unittest.TestCase):
    def test_claimed_result_dictionary_never_substitutes_for_native_job(self):
        class Fake:
            def status(self, *args, **kwargs):
                raise AssertionError('Untrusted status used')
        with self.assertRaisesRegex(RigError, 'authoritative native JobStore'):
            verify_control_pair(Fake(), '.', dict(verified=True), dict(verified=True),
                                effect='geometry', before_frame=0, after_frame=1, actor_ids=['actor'])

    def test_actual_native_store_does_not_accept_unknown_jobs(self):
        from render3d_jobs import JobStore
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary)/'jobs')
            binding = dict(job='a'*64, job_revision=1, request_sha256='b'*64,
                           backend_sha256='c'*64, directory='output', snapshot_directory='snapshot')
            with self.assertRaises((RigError, Render3DError)):
                verify_control_pair(store, temporary, binding, binding,
                    effect='camera', before_frame=0, after_frame=0)

    def test_fixed_native_settings_and_snapshot_are_mandatory(self):
        request = dict(id='before', shot={'id': 'shot-a'}, frame_range=dict(start=0, end=2),
                       source_start=0, camera='camera', destination={'path': 'a'},
                       snapshot={'sha256': 'a'*64}, settings=dict(seed=7, samples=64))
        before = dict(backend=dict(id='blender', device='cpu'), request=request)
        after = deepcopy(before); after['request']['id'] = 'after'
        self.assertEqual(_request_control(before, after), ['id'])
        for mutate in (lambda x: x['request']['settings'].update(seed=8),
                       lambda x: x['request']['settings'].update(samples=128),
                       lambda x: x['backend'].update(device='gpu'),
                       lambda x: x['request']['snapshot'].update(sha256='b'*64)):
            after = deepcopy(before); mutate(after)
            with self.subTest(mutate=mutate), self.assertRaises(RigError):
                _request_control(before, after)


if __name__ == '__main__':
    unittest.main()
