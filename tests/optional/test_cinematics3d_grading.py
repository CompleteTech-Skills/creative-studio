"""Actual optional numeric/OCIO grade behavior; no renderer approval claim."""
from copy import deepcopy
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
import cinematics3d_grading as grade


class GradingTests(unittest.TestCase):
    def setUp(self):
        self.source_context = {'kind': 'cinematics-color-source', 'schema_version': 1,
                               'config_closure_sha256': 'a'*64, 'working_space': 'Linear Rec.709',
                               'primaries': deepcopy(grade.color.REC709_D65), 'transfer': 'linear',
                               'alpha_mode': 'straight', 'view_applied': False, 'exposure_applied': False,
                               'source_manifest_sha256': 'b'*64, 'source_artifact_sha256': 'c'*64}

    def apply(self, *args, **kwargs):
        return grade.apply_grade(*args, source_context=self.source_context, **kwargs)

    def operation(self, kind, parameters, enabled=True):
        return {'id': kind, 'kind': kind, 'parameters': parameters, 'enabled': enabled}

    def plan(self, operations, **kwargs):
        return grade.make_grade(working_space='Linear Rec.709', source_id='shot-a', operations=operations, **kwargs)

    def pixels(self):
        return np.array([[.1, .2, .4, 0.], [.18, .18, .18, .5], [2., 3., 4., 1.]], np.float32)

    def test_module_import_does_not_load_optional_pixel_libraries(self):
        script = Path(__file__).resolve().parents[2]/'scripts'
        code = ('import sys;sys.path.insert(0,'+repr(str(script))+');'
                'import cinematics3d_grading;'
                'assert not any(x in sys.modules for x in ["numpy","PIL","yaml","PyOpenColorIO"])')
        result = subprocess.run([sys.executable, '-S', '-c', code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_persisted_schema_parity_and_malformed_types(self):
        import json
        import jsonschema
        for name, schema in grade.schemas().items():
            self.assertEqual(json.loads((Path(__file__).resolve().parents[2]/'schemas/cinematics-grade-v1'/name).read_text()), schema)
            validator = jsonschema.Draft202012Validator(schema)
            validator.validate(self.plan([self.operation('contrast', {'contrast': 1., 'pivot': .18})]))
            invalid = self.plan([]); invalid['intensity'] = True
            self.assertFalse(validator.is_valid(invalid))
        for kind in ({}, [], 1, None):
            with self.assertRaises(ValueError): self.plan([self.operation(kind, {})])

    def test_identity_and_disabled_operations_preserve_hdr_alpha_and_source(self):
        source = self.pixels(); retained = source.copy()
        result, receipt = self.apply('.', self.plan([self.operation('exposure', {'stops': 3}, False)]), source)
        np.testing.assert_array_equal(result, source); np.testing.assert_array_equal(source, retained)
        self.assertFalse(receipt['source_render_verified']); self.assertTrue(receipt['grade_executed'])
        self.assertGreater(receipt['diagnostics']['after']['max'], 1)

    def test_exposure_intensity_mask_are_actual_and_reversible(self):
        source = self.pixels()
        plan = self.plan([self.operation('exposure', {'stops': 1})], intensity=.5)
        result, _ = self.apply('.', plan, source, mask=np.array([0., .5, 1.], np.float32))
        np.testing.assert_allclose(result[:, :3], source[:, :3]*np.array([1., 1.25, 1.5])[:, None])
        np.testing.assert_array_equal(result[:, 3], source[:, 3])
        bypass = deepcopy(plan); bypass['operations'][0]['enabled'] = False
        bypass = self.plan(bypass['operations'])
        result, _ = self.apply('.', bypass, source)
        np.testing.assert_array_equal(result, source)

    def test_hdr_blend_endpoints_preserve_small_grade_outputs_and_exact_bypass(self):
        source = np.array([[1e20, -1e20, 1e20, 1.], [1e20, 1e20, 1e20, .5]], np.float32)
        operation = self.operation('channel-matrix', {'matrix': [[0., 0., 0.]]*3, 'offset': [.18, .18, .18]})
        result, _ = self.apply('.', self.plan([operation]), source)
        np.testing.assert_array_equal(result[:, :3], np.full((2,3), .18, np.float32))
        bypass, _ = self.apply('.', self.plan([operation], intensity=0.), source)
        np.testing.assert_array_equal(bypass, source)
        masked, _ = self.apply('.', self.plan([operation]), source, mask=np.array([0., 1.], np.float32))
        np.testing.assert_array_equal(masked[0], source[0])
        np.testing.assert_array_equal(masked[1, :3], np.full(3, .18, np.float32))
        np.testing.assert_array_equal(masked[:,3], source[:,3])

    def test_hdr_diagnostics_use_float64_reductions_without_changing_pixels(self):
        source = np.array([[1e38, 1e38, 1e38, 1.]], np.float32)
        for signs in ([-1., -1., -1.], [3., -3., 3.]):
            operation = self.operation('channel-matrix', {'matrix': np.diag(signs).tolist(), 'offset': [0.,0.,0.]})
            result, receipt = self.apply('.', self.plan([operation]), source)
            expected = (source[:, :3].astype(np.float64)*signs).astype(np.float32)
            np.testing.assert_array_equal(result[:, :3], expected)
            diagnostic = receipt['diagnostics']
            difference = np.abs(expected.astype(np.float64)-source[:, :3].astype(np.float64)).mean()
            self.assertEqual(diagnostic['mean_absolute_difference'], float(difference))
            self.assertTrue(np.isfinite(diagnostic['neutral_max_channel_spread']))
            self.assertEqual(receipt['execution_sha256'], grade.object_hash({k:v for k,v in receipt.items() if k != 'execution_sha256'}))

    def test_lift_gamma_gain_and_channel_matrix_have_reference_values(self):
        source = self.pixels()
        p = {'lift': [.1, 0., -.1], 'gamma': [2., 1., .5], 'gain': [1., 2., .5]}
        result, _ = self.apply('.', self.plan([self.operation('lift-gamma-gain', p)]), source)
        v = source[:, :3]+np.array(p['lift'])*(1-source[:, :3])
        expected = np.sign(v)*np.abs(v)**(1/np.array(p['gamma']))*np.array(p['gain'])
        np.testing.assert_allclose(result[:, :3], expected, rtol=2e-6)
        swap = {'matrix': [[0.,0.,1.], [0.,1.,0.], [1.,0.,0.]], 'offset': [0.,0.,0.]}
        result, _ = self.apply('.', self.plan([self.operation('channel-matrix', swap)]), source)
        np.testing.assert_array_equal(result[:, :3], source[:, [2,1,0]])

    def test_curves_execute_and_unit_slope_preserves_hdr_outside_domain(self):
        curves = [[[0., 0.], [.5, .25], [1., 1.]]]*3
        source = np.array([[.5,.5,.5,1.], [4.,4.,4.,1.], [-2.,-2.,-2.,1.]], np.float32)
        result, _ = self.apply('.', self.plan([self.operation('curves', {'rgb': curves})]), source)
        np.testing.assert_allclose(result[:,0], [.25,4.,-2.])

    def test_all_numeric_controls_execute_and_neutral_hue_is_preserved(self):
        examples = [('contrast', {'contrast': 1.4, 'pivot': .18}), ('saturation', {'amount': 0.}),
                    ('vibrance', {'amount': 1.5}), ('temperature-tint', {'temperature': .2, 'tint': -.1}),
                    ('hue', {'degrees': 80.}), ('tonal-balance', {'shadows': [.2,0.,0.], 'midtones': [0.,.2,0.], 'highlights': [0.,0.,.2]})]
        for kind, parameters in examples:
            with self.subTest(kind=kind):
                result, receipt = self.apply('.', self.plan([self.operation(kind, parameters)]), self.pixels())
                self.assertGreater(receipt['diagnostics']['mean_absolute_difference'], 0)
                np.testing.assert_array_equal(result[:,3], self.pixels()[:,3])
                if kind == 'hue': np.testing.assert_allclose(result[1,:3], [.18]*3, atol=1e-6)

    def test_unknown_primaries_or_premultiplied_source_are_not_inferred(self):
        for key, value in [('alpha_mode', 'premultiplied'), ('view_applied', True),
                           ('primaries', {'r': [.7, .3], 'g': [.3, .6], 'b': [.15, .06], 'w': [.3127, .329]})]:
            with self.subTest(key=key):
                context = deepcopy(self.source_context); context[key] = value
                with self.assertRaises(ValueError):
                    grade.apply_grade('.', self.plan([]), self.pixels(), source_context=context)

    def test_real_1d_cube_lut_executes_and_changed_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='grade-tests-') as temporary:
            root = Path(temporary).resolve(); path = root/'test.cube'
            path.write_text('LUT_1D_SIZE 2\n0 0 0\n.5 .5 .5\n')
            lut = grade.import_cube(root, 'test.cube', working_space='Linear Rec.709')
            plan = self.plan([self.operation('lut', {'inventory': lut})])
            source = np.array([[.2,.4,.8,1.]], np.float32)
            result, _ = self.apply(root, plan, source)
            np.testing.assert_allclose(result[:,:3], source[:,:3]*.5, atol=1e-6)
            path.write_text('LUT_1D_SIZE 2\n0 0 0\n1 1 1\n')
            with self.assertRaises(ValueError): self.apply(root, plan, source)

    def test_real_3d_cube_lut_axis_order_matches_reference(self):
        with tempfile.TemporaryDirectory(prefix='grade-tests-') as temporary:
            root = Path(temporary).resolve()
            rows = ['LUT_3D_SIZE 2']
            for blue in (0,1):
                for green in (0,1):
                    for red in (0,1): rows.append(f'{blue} {green} {red}')
            (root/'swap.cube').write_text('\n'.join(rows)+'\n')
            lut = grade.import_cube(root, 'swap.cube', working_space='Linear Rec.709')
            source = np.array([[.2,.4,.8,1.]], np.float32)
            result, _ = self.apply(root, self.plan([self.operation('lut', {'inventory': lut})]), source)
            np.testing.assert_allclose(result[:,:3], source[:,[2,1,0]], atol=1e-6)

    def test_malformed_incompatible_stale_and_excessive_inputs_fail_before_execution(self):
        source = self.pixels(); plan = self.plan([])
        for value in (10**400, True, float('nan'), -1):
            with self.assertRaises(ValueError): self.plan([], intensity=value)
        for mask in (np.array([0.,2.,0.]), np.array([0.,float('nan'),0.]), np.ones((1,3))):
            with self.assertRaises(ValueError): self.apply('.', plan, source, mask=mask)
        forged = deepcopy(plan); forged['scope'] = 'scene'
        with self.assertRaises(ValueError): self.apply('.', forged, source)
        with patch.object(grade, '_operation', side_effect=AssertionError('expensive operation began')):
            with self.assertRaises(ValueError): self.apply('.', plan, source, max_work_bytes=1)
        with self.assertRaises(ValueError): self.plan([self.operation('curves', {'rgb': [[[1.,0.],[0.,1.]]]*3})])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); (root/'bad.cube').write_text('LUT_3D_SIZE 1000000000\n0 0 0\n')
            with self.assertRaises(ValueError): grade.import_cube(root, 'bad.cube', working_space='Linear Rec.709')


if __name__ == '__main__': unittest.main()
