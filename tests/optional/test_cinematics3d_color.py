"""Optional real OCIO numeric/security tests; all artwork-free engineering fixtures."""
from copy import deepcopy
import hashlib
import importlib.abc
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
import cinematics3d_color as color

ROOT = Path(__file__).resolve().parents[2]


class ColorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='color-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()/'profile'
        shutil.copytree(ROOT/'tests/fixtures/cinematics-color/profile', self.root)
        self.inventory = color.capture_config(self.root)
        self.source = {'kind': 'cinematics-color-source', 'schema_version': 1,
                       'config_closure_sha256': self.inventory['closure_sha256'],
                       'working_space': 'Linear Rec.709', 'primaries': deepcopy(color.REC709_D65),
                       'transfer': 'linear', 'alpha_mode': 'straight', 'view_applied': False, 'exposure_applied': False,
                       'source_manifest_sha256': 'a'*64, 'source_artifact_sha256': 'b'*64}

    def plan(self, **kwargs):
        return color.make_plan(self.root, self.inventory, self.source, **kwargs)

    def test_actual_standard_chart_matches_independent_srgb_reference_once(self):
        values = np.array([[0., .0031308, .18, 1.], [.5, 1., 4., .5]], dtype=np.float32)
        result, receipt = color.transform_pixels(self.root, self.plan(), values)
        reference = np.array([[color.linear_to_srgb_reference(float(v)) for v in row[:3]] for row in values])
        np.testing.assert_allclose(result[:, :3], reference, atol=2e-5, rtol=2e-5)
        np.testing.assert_array_equal(result[:, 3], values[:, 3])
        self.assertGreater(float(result[1, 2]), 1.)
        self.assertLess(abs(float(result[0, 2])-.4613561), 2e-5)
        self.assertFalse(receipt['source_render_verified'])
        self.assertTrue(receipt['pixel_transform_executed'])
        self.assertEqual(receipt['exposure_applications'], 1)
        self.assertEqual(receipt['output_pixels_sha256'], hashlib.sha256(result.tobytes()).hexdigest())

    def test_scene_linear_hdr_and_exact_exposure_do_not_bake_a_view(self):
        plan = self.plan(target='scene-linear', display='', view='', exposure_stops=1.)
        values = np.array([[.18, 2., 4., .25]], dtype=np.float32)
        result, receipt = color.transform_pixels(self.root, plan, values)
        np.testing.assert_allclose(result[:, :3], values[:, :3]*2, atol=2e-6)
        np.testing.assert_array_equal(result[:, 3], values[:, 3])
        self.assertFalse(receipt['view_applied'])
        self.assertEqual(receipt['output_transfer'], 'linear')
        with self.assertRaises(ValueError):
            self.plan(target='scene-linear')
        with self.assertRaises(ValueError):
            self.plan(target='hdr-pq')

    def test_premultiplied_alpha_is_unassociated_before_view_and_reassociated_only_when_requested(self):
        straight = np.array([[.18, .4, 2., .25], [0., 0., 0., 0.]], dtype=np.float32)
        premult = straight.copy(); premult[:, :3] *= premult[:, 3:4]
        reference, _ = color.transform_pixels(self.root, self.plan(), straight)
        self.source['alpha_mode'] = 'premultiplied'
        actual, _ = color.transform_pixels(self.root, self.plan(), premult)
        np.testing.assert_allclose(actual, reference, atol=2e-5)
        actual, _ = color.transform_pixels(self.root, self.plan(output_alpha='premultiplied'), premult)
        reference[:, :3] *= reference[:, 3:4]
        np.testing.assert_allclose(actual, reference, atol=2e-5)
        bad = premult.copy(); bad[1, 0] = 1.
        with self.assertRaises(ValueError):
            color.transform_pixels(self.root, self.plan(), bad)

    def test_double_gamma_unknown_primaries_and_data_labels_fail_closed(self):
        for key, value in [('transfer', 'srgb'), ('view_applied', True), ('exposure_applied', True), ('schema_version', True)]:
            source = deepcopy(self.source); source[key] = value
            with self.assertRaises(ValueError):
                color.make_plan(self.root, self.inventory, source)
        self.source['primaries']['r'][0] = .67
        with self.assertRaises(ValueError):
            self.plan()
        self.source['primaries'] = deepcopy(color.REC709_D65)
        self.source['working_space'] = 'sRGB'
        with self.assertRaises(ValueError):
            self.plan()

    def test_forged_output_labels_and_stale_lut_bytes_cannot_reuse_plan(self):
        plan = self.plan()
        forged = deepcopy(plan); forged['output_transfer'] = 'pq'
        forged['plan_sha256'] = color.object_hash({k: v for k, v in forged.items() if k != 'plan_sha256'})
        with self.assertRaises(ValueError):
            color.transform_pixels(self.root, forged, np.zeros((1, 4), dtype=np.float32))
        (self.root/'luts/identity.spi1d').write_bytes(b'changed original LUT')
        with self.assertRaises(ValueError):
            color.transform_pixels(self.root, plan, np.zeros((1, 4), dtype=np.float32))
        with self.assertRaises(ValueError):
            color.verify_config_bytes(self.root, self.inventory)

    def test_pixel_preflight_rejects_invalid_arrays_and_work_before_processor(self):
        plan = self.plan()
        for pixels in (np.ones((1, 3), dtype=np.float32), np.ones((1, 4), dtype=np.uint8),
                       np.zeros((0, 4), dtype=np.float32), np.array([[0., 0., 0., 2.]], dtype=np.float32),
                       np.array([[float('nan'), 0., 0., 1.]], dtype=np.float32),
                       np.array([[1e300, 0., 0., 1.]], dtype=np.float64)):
            with self.subTest(shape=pixels.shape, dtype=str(pixels.dtype)):
                with self.assertRaises(ValueError):
                    color.transform_pixels(self.root, plan, pixels)
        with patch.object(color, '_config', side_effect=AssertionError('expensive processing began')):
            with self.assertRaises(ValueError):
                color.transform_pixels(self.root, plan, np.zeros((1, 4), dtype=np.float32), max_work_bytes=1)

    def test_inventory_rejects_unsafe_alias_expansion_external_and_ambiguous_file_paths(self):
        original = (self.root/'config.ocio').read_text()
        for text in (original.replace('identity.spi1d', '../outside.spi1d'),
                     original.replace('identity.spi1d', '$HOME/identity.spi1d'),
                     original.replace('roles:', 'environment: {HOME: outside}\nroles:'),
                     original+'\nextra: &a [*a]\n', original.replace('strictparsing: true', 'strictparsing: true\nstrictparsing: false')):
            with self.subTest(text=text[-80:]):
                (self.root/'config.ocio').write_text(text)
                with self.assertRaises(ValueError):
                    color.capture_config(self.root)
        (self.root/'config.ocio').write_text(original)
        shutil.copy2(self.root/'luts/identity.spi1d', self.root/'identity.spi1d')
        with self.assertRaises(ValueError):
            color.capture_config(self.root)

    def test_nonregular_replacement_during_open_rejects_without_reading(self):
        target = self.root/'luts/identity.spi1d'
        original_open = os.open
        swapped = []
        def swap(path, *args, **kwargs):
            if Path(path).name == 'identity.spi1d' and not swapped:
                target.rename(target.with_suffix('.retained'))
                if hasattr(os, 'mkfifo'):
                    os.mkfifo(target)
                else:
                    target.mkdir()
                swapped.append(True)
            return original_open(path, *args, **kwargs)
        with patch.object(color.os, 'open', side_effect=swap):
            with self.assertRaises(ValueError):
                color._bytes(self.root, 'luts/identity.spi1d', 4096)
        self.assertEqual(swapped, [True])

    def test_worker_verification_is_standard_library_only_and_snapshot_is_bound(self):
        with color.snapshot_config(self.root, self.inventory) as snapshot:
            self.assertEqual(color.verify_config_bytes(snapshot, self.inventory), self.inventory['closure_sha256'])
        data = Path(self.temporary.name)/'inventory.json'; data.write_text(json.dumps(self.inventory))
        command = ('import sys,json;sys.path.insert(0,'+repr(str(ROOT/'scripts'))+');'
                   'import cinematics3d_color as c;'
                   'print(c.verify_config_bytes('+repr(str(self.root))+',json.load(open('+repr(str(data))+'))));'
                   'assert not any(x in sys.modules for x in ["PIL","yaml","numpy","PyOpenColorIO"])')
        result = subprocess.run([sys.executable, '-S', '-c', command], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.inventory['closure_sha256'], result.stdout)

    def test_actual_scene_linear_probe_measures_chromaticities(self):
        probe = color.probe_scene_linear(self.root, self.inventory)
        self.assertFalse(probe['source_render_verified'])
        for key in color.REC709_D65:
            np.testing.assert_allclose(probe['primaries'][key], color.REC709_D65[key], atol=2e-6)
        self.assertEqual(probe['config_closure_sha256'], self.inventory['closure_sha256'])

    def test_declared_lut_sizes_are_bounded_before_ocio_allocation(self):
        for payload, suffix in [(b'LUT_3D_SIZE 1000000000\n0 0 0\n', '.cube'),
                                (b'LUT_3D_SIZE 2\n0 0 0\n', '.cube'),
                                (b'LUT_1D_SIZE 2\n0 0 0\nnan 1 1\n', '.cube'),
                                (b'Version 1\nFrom 0 1\nLength 999999999\nComponents 3\n{\n0 0 0\n}\n', '.spi1d')]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    color._lut(payload, suffix)
        color._lut(b'LUT_1D_SIZE 2\n0 0 0\n1 1 1\n', '.cube')

    def test_schema_parity_and_all_public_record_shapes(self):
        import jsonschema
        documents = color.schemas()
        directory = ROOT/'schemas/cinematics-color-v1'
        self.assertEqual({path.name for path in directory.glob('*.json')}, set(documents))
        for name, value in documents.items():
            self.assertEqual(json.loads((directory/name).read_text()), value)
        plan = self.plan()
        _, execution = color.transform_pixels(self.root, plan, np.array([[.18, .2, 4., 1.]], dtype=np.float32))
        values = {'config': self.inventory, 'source': self.source, 'plan': plan, 'execution': execution,
                  'capabilities': color.inspect_config(self.root, self.inventory),
                  'probe': color.probe_scene_linear(self.root, self.inventory)}
        for name, value in values.items():
            jsonschema.Draft202012Validator(documents[name+'.schema.json']).validate(value)

    def test_malformed_yaml_and_disappeared_profile_file_raise_clear_failures(self):
        (self.root/'config.ocio').write_text('roles: [unterminated')
        with self.assertRaises(ValueError):
            color.capture_config(self.root)
        with self.assertRaises(ValueError):
            color._bytes(self.root, 'missing.spi1d', 4096)

    def test_real_cube_look_executes_once_and_bypasses_implicit_view_look(self):
        original = (self.root/'config.ocio').read_text()
        values = np.array([[.25, .5, .75, .5]], dtype=np.float32)
        cube_3d = 'LUT_3D_SIZE 2\n'+'\n'.join(f'{1-r} {g} {b}' for b in (0, 1) for g in (0, 1) for r in (0, 1))+'\n'
        cube_1d = 'LUT_1D_SIZE 2\n0 0 0\n2 2 2\n'
        for cube, expected_linear in [(cube_3d, [.75, .5, .75]), (cube_1d, [.5, 1., 1.5])]:
            with self.subTest(cube=cube[:20]):
                (self.root/'luts/grade.cube').write_text(cube)
                text = original.replace('{name: Standard, colorspace: sRGB}', '{name: Standard, colorspace: sRGB, looks: Grade}')
                text += '\nlooks:\n  - !<Look>\n    name: Grade\n    process_space: Linear Rec.709\n    transform: !<FileTransform> {src: grade.cube, interpolation: linear}\n'
                (self.root/'config.ocio').write_text(text)
                self.inventory = color.capture_config(self.root)
                self.source['config_closure_sha256'] = self.inventory['closure_sha256']
                baseline, _ = color.transform_pixels(self.root, self.plan(), values)
                np.testing.assert_allclose(baseline[0, :3], [color.linear_to_srgb_reference(float(v)) for v in values[0, :3]], atol=2e-5)
                graded, _ = color.transform_pixels(self.root, self.plan(look='Grade'), values)
                np.testing.assert_allclose(graded[0, :3], [color.linear_to_srgb_reference(v) for v in expected_linear], atol=2e-5)
                self.assertEqual(float(graded[0, 3]), .5)


if __name__ == '__main__':
    unittest.main()
