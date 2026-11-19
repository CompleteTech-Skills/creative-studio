"""Hash-bound OCIO profile capture, pinned transforms and the worker color context.

Profile capture needs the optional PyYAML dependency and transforms need the
pinned PyOpenColorIO and numpy from the optional color requirements. Each
group skips with a stated reason when its optional dependency is absent, so a
2D-only installation stays green. A calibrated SDR transform here is a
numerical result against an independent IEC sRGB oracle, not colorist or
visual approval.
"""
from copy import deepcopy
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import cinematics3d_color as color
from render3d_color_context import color_context
from render3d_contracts import Render3DError
from rig_errors import object_hash

HAS_YAML = importlib.util.find_spec('yaml') is not None
HAS_NUMPY = importlib.util.find_spec('numpy') is not None


def _pinned_ocio():
    try:
        import PyOpenColorIO as ocio
    except ImportError:
        return False
    return ocio.GetVersion() == color.OCIO_VERSION


HAS_OCIO = HAS_NUMPY and _pinned_ocio()
YAML_REASON = 'optional PyYAML color dependency is not installed (requirements-color.txt)'
OCIO_REASON = ('pinned PyOpenColorIO '+color.OCIO_VERSION+' and numpy are not installed '
               '(requirements-color.txt)')

# Linear Rec.709/D65 to CIE XYZ D65 (IEC 61966-2-1 derived matrix).
_XYZ = ('[0.4123907992659595, 0.357584339383878, 0.1804807884018343, 0, '
        '0.21263900587151036, 0.715168678767756, 0.07219231536073371, 0, '
        '0.01933081871559185, 0.11919477979462598, 0.9505321522496606, 0, 0, 0, 0, 1]')
CONFIG = '''ocio_profile_version: 2.1

roles:
  scene_linear: Linear Rec.709
  cie_xyz_d65_interchange: CIE-XYZ-D65
  default: Linear Rec.709

file_rules:
  - !<Rule> {name: Default, colorspace: Linear Rec.709}

displays:
  sRGB:
    - !<View> {name: Standard, colorspace: sRGB Display}

looks:
  - !<Look>
    name: Brighter
    process_space: Linear Rec.709
    transform: !<ExposureContrastTransform> {exposure: 1}

colorspaces:
  - !<ColorSpace>
    name: Linear Rec.709
    family: linear
    isdata: false

  - !<ColorSpace>
    name: CIE-XYZ-D65
    isdata: false
    from_scene_reference: !<MatrixTransform> {matrix: %s}

  - !<ColorSpace>
    name: sRGB Display
    isdata: false
    from_scene_reference: !<ExponentWithLinearTransform> {gamma: 2.4, offset: 0.055, direction: inverse}
''' % _XYZ

LUT_REFERENCE = '''
  - !<ColorSpace>
    name: Lut Space
    isdata: false
    from_scene_reference: !<FileTransform> {src: shaper.spi1d, interpolation: linear}
'''
LUT_PAYLOAD = b'Version 1\nFrom 0.0 1.0\nLength 2\nComponents 1\n{\n0.0\n1.0\n}\n'


def write_profile(root, config=CONFIG, extra=None):
    root = Path(root)
    (root/'config.ocio').write_text(config, encoding='utf-8')
    for name, payload in (extra or {}).items():
        target = root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


def manual_inventory(root, files):
    """Build an admitted inventory with only the standard library."""
    rows = [dict(path=name, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            for name, payload in sorted(files.items())]
    for row in rows:
        target = Path(root)/row['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[row['path']])
    value = dict(kind='cinematics-color-config', schema_version=1, config_path='config.ocio',
                 config_sha256=next(r['sha256'] for r in rows if r['path'] == 'config.ocio'),
                 files=rows, search_paths=[],
                 declared_roles={'scene_linear': 'lin', 'cie_xyz_d65_interchange': 'xyz'},
                 file_transforms=[])
    value['closure_sha256'] = object_hash(value)
    return value


def source_context(inventory, **changes):
    value = dict(kind='cinematics-color-source', schema_version=1,
                 config_closure_sha256=inventory['closure_sha256'],
                 working_space=inventory['declared_roles']['scene_linear'],
                 primaries=deepcopy(color.REC709_D65), transfer='linear', alpha_mode='straight',
                 view_applied=False, source_manifest_sha256='1'*64, source_artifact_sha256='2'*64)
    value.update(changes)
    return value


class Temporary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()/'profile'
        self.root.mkdir()


class PortablePaths(unittest.TestCase):
    def test_unsafe_relative_paths_are_rejected(self):
        for value in ('', '/abs/config.ocio', '../escape.ocio', 'a//b.ocio', 'a/./b.ocio',
                      'trailing.', 'con.ocio', 'LPT1.cube', 'space name.ocio', 'back\\slash.ocio',
                      'x'*513, 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                color._path(value)
        self.assertEqual(color._path('luts/shaper.spi1d'), 'luts/shaper.spi1d')

    def test_srgb_reference_oracle(self):
        self.assertAlmostEqual(color.linear_to_srgb_reference(0.), 0.)
        self.assertAlmostEqual(color.linear_to_srgb_reference(.002), .002*12.92)
        self.assertAlmostEqual(color.linear_to_srgb_reference(1.), 1.)
        self.assertAlmostEqual(color.linear_to_srgb_reference(.18), .4613561295, places=8)
        for value in (float('nan'), float('inf'), 1e9, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                color.linear_to_srgb_reference(value)


class WorkerVerification(Temporary):
    """Worker-side verification is standard-library only."""

    def setUp(self):
        super().setUp()
        self.files = {'config.ocio': b'roles: {}\n', 'luts/shaper.spi1d': LUT_PAYLOAD}
        self.inventory = manual_inventory(self.root, self.files)

    def test_verify_and_snapshot_need_no_yaml_or_ocio(self):
        with patch.dict(sys.modules, {'yaml': None, 'PyOpenColorIO': None, 'numpy': None}):
            self.assertEqual(color.verify_config(self.root, self.inventory), self.inventory['closure_sha256'])
            self.assertIs(color.verify_config_bytes, color.verify_config)
            with color.snapshot_config(self.root, self.inventory) as stage:
                self.assertNotEqual(stage, self.root)
                self.assertEqual((stage/'luts/shaper.spi1d').read_bytes(), LUT_PAYLOAD)
                self.assertEqual(color.verify_config(stage, self.inventory), self.inventory['closure_sha256'])
            self.assertFalse(stage.exists())

    def test_snapshot_under_symlinked_temporary_ancestor(self):
        real = self.root.parent/(self.root.name+'-real-tmp'); real.mkdir()
        alias = self.root.parent/(self.root.name+'-alias-tmp')
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'directory symlinks unavailable: {exc}')
        self.addCleanup(alias.unlink)
        self.addCleanup(lambda: real.rmdir() if real.exists() and not any(real.iterdir()) else None)
        with patch.object(tempfile, 'tempdir', str(alias)):
            with color.snapshot_config(self.root, self.inventory) as stage:
                self.assertTrue(stage.is_relative_to(real.resolve()))
                self.assertEqual((stage/'luts/shaper.spi1d').read_bytes(), LUT_PAYLOAD)
            self.assertFalse(stage.exists())

    def test_changed_lut_bytes_or_extra_files_fail_closed(self):
        (self.root/'luts/shaper.spi1d').write_bytes(LUT_PAYLOAD.replace(b'1.0\n}', b'0.9\n}'))
        with self.assertRaises(ValueError):
            color.verify_config(self.root, self.inventory)
        (self.root/'luts/shaper.spi1d').write_bytes(LUT_PAYLOAD)
        (self.root/'luts/extra.cube').write_bytes(b'LUT_1D_SIZE 2\n0 0 0\n1 1 1\n')
        with self.assertRaises(ValueError):
            color.verify_config(self.root, self.inventory)

    def test_stale_or_malformed_inventory_is_rejected(self):
        stale = deepcopy(self.inventory); stale['files'][1]['sha256'] = 'c'*64
        forged = deepcopy(self.inventory); forged['declared_roles']['scene_linear'] = 'other'
        extra = dict(self.inventory, unexpected=True)
        missing_role = deepcopy(self.inventory); del missing_role['declared_roles']['cie_xyz_d65_interchange']
        missing_role['closure_sha256'] = object_hash({k: v for k, v in missing_role.items() if k != 'closure_sha256'})
        alias = deepcopy(self.inventory); alias['files'].append(dict(alias['files'][1], path='LUTS/shaper.spi1d'))
        for value in (stale, forged, extra, missing_role, alias):
            with self.subTest(), self.assertRaises(ValueError):
                color.validate_inventory(value)
        self.assertEqual(color.validate_inventory(self.inventory), self.inventory)

    @unittest.skipIf(os.name == 'nt', 'symlink creation needs elevated privileges on Windows')
    def test_symlinked_profile_member_is_rejected(self):
        os.symlink(self.root/'luts/shaper.spi1d', self.root/'luts/link.spi1d')
        with self.assertRaises(ValueError):
            color.verify_config(self.root, self.inventory)


@unittest.skipUnless(HAS_YAML, YAML_REASON)
class ProfileCapture(Temporary):
    def test_capture_binds_config_luts_and_roles(self):
        config = CONFIG.replace('colorspaces:\n', 'search_path: luts\n\ncolorspaces:\n', 1)+LUT_REFERENCE
        write_profile(self.root, config, {'luts/shaper.spi1d': LUT_PAYLOAD})
        inventory = color.capture_config(self.root)
        self.assertEqual(color.validate_inventory(inventory), inventory)
        self.assertEqual([row['path'] for row in inventory['files']], ['config.ocio', 'luts/shaper.spi1d'])
        self.assertEqual(inventory['file_transforms'], [dict(source='shaper.spi1d', path='luts/shaper.spi1d')])
        self.assertEqual(inventory['declared_roles']['scene_linear'], 'Linear Rec.709')
        self.assertEqual(color.capture_config(self.root), inventory)
        (self.root/'luts/shaper.spi1d').write_bytes(LUT_PAYLOAD.replace(b'1.0\n}', b'0.5\n}'))
        changed = color.capture_config(self.root)
        self.assertNotEqual(changed['closure_sha256'], inventory['closure_sha256'])
        self.assertEqual(changed['config_sha256'], inventory['config_sha256'])

    def test_unclosed_or_dynamic_profiles_fail_closed(self):
        cases = {
            'missing-lut': (CONFIG+LUT_REFERENCE, {}),
            'environment': ('environment: {SHOT: a}\n'+CONFIG, {}),
            'substitution': (CONFIG+LUT_REFERENCE.replace('shaper.spi1d', '$SHOT.spi1d'), {}),
            'alias': (CONFIG.replace('default: Linear Rec.709', 'default: &a Linear Rec.709\n  data: *a'), {}),
            'missing-xyz-role': (CONFIG.replace('  cie_xyz_d65_interchange: CIE-XYZ-D65\n', ''), {}),
            'duplicate-key': (CONFIG.replace('  default: Linear Rec.709', '  scene_linear: CIE-XYZ-D65'), {}),
            'file-type': (CONFIG, {'notes.txt': b'x'}),
            'ambiguous-lut': (CONFIG.replace('colorspaces:\n', 'search_path: a:b\n\ncolorspaces:\n', 1)+LUT_REFERENCE,
                              {'a/shaper.spi1d': LUT_PAYLOAD, 'b/shaper.spi1d': LUT_PAYLOAD}),
        }
        for name, (config, extra) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = write_profile(Path(directory).resolve(), config, extra)
                with self.assertRaises(ValueError):
                    color.capture_config(root)

    def test_missing_config_and_oversized_config(self):
        with self.assertRaises(ValueError):
            color.capture_config(self.root)
        write_profile(self.root, CONFIG+'#'*(color.MAX_CONFIG_BYTES+1))
        with self.assertRaises(ValueError):
            color.capture_config(self.root)


@unittest.skipUnless(HAS_YAML and HAS_OCIO, OCIO_REASON)
class PinnedTransforms(Temporary):
    def setUp(self):
        super().setUp()
        write_profile(self.root)
        self.inventory = color.capture_config(self.root)
        self.source = source_context(self.inventory)

    def test_inspect_reports_actual_config_capabilities(self):
        report = color.inspect_config(self.root, self.inventory)
        self.assertEqual(report['ocio_version'], color.OCIO_VERSION)
        self.assertEqual(report['roles']['scene_linear'], 'Linear Rec.709')
        self.assertEqual(report['displays'][0]['name'], 'sRGB')
        self.assertEqual(report['displays'][0]['views'][0]['name'], 'Standard')
        self.assertEqual(report['looks'], ['Brighter'])

    def test_sdr_plan_is_calibrated_and_hash_bound(self):
        plan = color.make_plan(self.root, self.inventory, self.source)
        self.assertLess(plan['output_calibration_max_error'], 5e-4)
        self.assertEqual(plan['output_transfer'], 'srgb')
        self.assertEqual(plan['plan_sha256'], object_hash({k: v for k, v in plan.items() if k != 'plan_sha256'}))
        for key, pair in plan['measured_source_primaries'].items():
            for actual, expected in zip(pair, color.REC709_D65[key]):
                self.assertAlmostEqual(actual, expected, places=4)
        linear = color.make_plan(self.root, self.inventory, self.source, target='scene-linear',
                                 display='', view='', look='')
        self.assertIsNone(linear['output_calibration_max_error'])
        graded = color.make_plan(self.root, self.inventory, self.source, look='Brighter')
        self.assertIsNone(graded['output_calibration_max_error'])
        self.assertEqual(graded['output_transfer'], 'srgb')
        self.assertEqual(linear['output_transfer'], 'linear')

    def test_invalid_plans_fail_closed(self):
        wrong = deepcopy(color.REC709_D65); wrong['r'] = [0.708, 0.292]
        bad = [
            (source_context(self.inventory, primaries=wrong), {}),
            (source_context(self.inventory, config_closure_sha256='9'*64), {}),
            (source_context(self.inventory, working_space='CIE-XYZ-D65'), {}),
            (source_context(self.inventory, view_applied=True), {}),
            (self.source, dict(target='hdr-pq')),
            (self.source, dict(view='Filmic')),
            (self.source, dict(look='Missing')),
            (self.source, dict(exposure_stops=float('inf'))),
            (self.source, dict(target='scene-linear')),
        ]
        for source, options in bad:
            with self.subTest(options=options), self.assertRaises(ValueError):
                color.make_plan(self.root, self.inventory, source, **options)
        (self.root/'config.ocio').write_text(CONFIG+'\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            color.make_plan(self.root, self.inventory, self.source)

    def test_transform_matches_independent_srgb_oracle(self):
        import numpy as np
        plan = color.make_plan(self.root, self.inventory, self.source)
        pixels = np.array([[.18, .18, .18, 1.], [1., .5, 0., .5], [0., 0., 0., 0.]], dtype=np.float32)
        output, receipt = color.transform_pixels(self.root, plan, pixels)
        for row_in, row_out in zip(pixels, output):
            for value, actual in zip(row_in[:3], row_out[:3]):
                self.assertAlmostEqual(float(actual), color.linear_to_srgb_reference(float(value)), places=3)
            self.assertEqual(float(row_out[3]), float(row_in[3]))
        self.assertEqual(receipt['plan_sha256'], plan['plan_sha256'])
        self.assertTrue(receipt['pixel_transform_executed'])
        self.assertFalse(receipt['source_render_verified'])
        self.assertEqual(receipt['exposure_applications'], 1)
        self.assertTrue(np.array_equal(pixels[0], np.array([.18, .18, .18, 1.], dtype=np.float32)))

    def test_exposure_look_and_alpha_modes(self):
        import numpy as np
        pixels = np.array([[.09, .09, .09, 1.]], dtype=np.float32)
        exposed = color.make_plan(self.root, self.inventory, self.source, exposure_stops=1.)
        looked = color.make_plan(self.root, self.inventory, self.source, look='Brighter')
        expected = color.linear_to_srgb_reference(.18)
        for plan in (exposed, looked):
            output, _ = color.transform_pixels(self.root, plan, pixels)
            self.assertAlmostEqual(float(output[0, 0]), expected, places=3)
        premultiplied = source_context(self.inventory, alpha_mode='premultiplied')
        plan = color.make_plan(self.root, self.inventory, premultiplied, output_alpha='premultiplied')
        output, _ = color.transform_pixels(self.root, plan, np.array([[.09, .09, .09, .5]], dtype=np.float32))
        self.assertAlmostEqual(float(output[0, 0]), color.linear_to_srgb_reference(.18)*.5, places=3)
        with self.assertRaises(ValueError):
            color.transform_pixels(self.root, plan, np.array([[.1, 0., 0., 0.]], dtype=np.float32))

    def test_forged_plans_and_invalid_pixels_are_rejected(self):
        import numpy as np
        plan = color.make_plan(self.root, self.inventory, self.source)
        pixels = np.array([[.5, .5, .5, 1.]], dtype=np.float32)
        forged = deepcopy(plan); forged['exposure_stops'] = 2.
        relabeled = deepcopy(plan); relabeled['output_transfer'] = 'linear'
        for value in (forged, relabeled):
            with self.subTest(), self.assertRaises(ValueError):
                color.transform_pixels(self.root, value, pixels)
        for value in (np.array([[.5, .5, .5]], dtype=np.float32), np.array([[1, 1, 1, 1]]),
                      np.array([[np.nan, 0, 0, 1]], dtype=np.float32),
                      np.array([[.5, .5, .5, 1.5]], dtype=np.float32)):
            with self.subTest(shape=value.shape), self.assertRaises(ValueError):
                color.transform_pixels(self.root, plan, value)
        with self.assertRaises(ValueError):
            color.transform_pixels(self.root, plan, pixels, max_work_bytes=16)


class ColorContext(Temporary):
    def scene(self, management='FOLLOW_SCENE'):
        return NS(render=NS(image_settings=NS(color_management=management, file_format='OPEN_EXR',
                                              color_mode='RGBA', color_depth='32'), film_transparent=True),
                  view_settings=NS(view_transform='Standard', look='None', exposure=1.25, gamma=1),
                  display_settings=NS(display_device='sRGB'),
                  view_layers=[NS(name='native-layer', use_pass_z=True, use_pass_normal=False,
                                  use_pass_diffuse_color=True, pass_alpha_threshold=.5)])

    def setUp(self):
        super().setUp()
        self.inventory = manual_inventory(self.root, {'config.ocio': b'roles: {}\n'})
        self.pinned = str(self.root/'config.ocio')

    def test_actual_state_receipt_retains_threshold_and_unknowns(self):
        with patch.dict(os.environ, OCIO=self.pinned):
            receipt = color_context(self.scene(), self.root, self.inventory, {'alpha': 'straight'})
        self.assertEqual(receipt['closure_sha256'], self.inventory['closure_sha256'])
        self.assertEqual(receipt['scene_linear_role'], 'lin')
        self.assertEqual(receipt['exposure'], 1.25)
        self.assertEqual(receipt['passes'][0], dict(layer='native-layer', depth=True, normal=False,
                                                    albedo=True, alpha_threshold=.5))
        self.assertIsNone(receipt['numeric_primaries'])
        self.assertIsNone(receipt['numeric_white_point'])
        self.assertEqual(receipt['chromaticity_status'], 'not-established-by-renderer-receipt')
        self.assertEqual(receipt['alpha_association'], 'unverified')

    def test_unpinned_or_different_ocio_fails_closed(self):
        with patch.dict(os.environ, OCIO=str(Path(self.tmp.name)/'different'/'config.ocio')):
            with self.assertRaises(Render3DError) as caught:
                color_context(self.scene(), self.root, self.inventory, {'alpha': 'straight'})
        self.assertEqual(caught.exception.report['category'], 'verification-failed')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OCIO', None)
            with self.assertRaises(Render3DError):
                color_context(self.scene(), self.root, self.inventory, {'alpha': 'straight'})

    def test_changed_profile_bytes_or_image_override_fail_closed(self):
        with patch.dict(os.environ, OCIO=self.pinned):
            with self.assertRaises(Render3DError) as caught:
                color_context(self.scene('OVERRIDE'), self.root, self.inventory, {'alpha': 'straight'})
            self.assertEqual(caught.exception.report['category'], 'unsupported-feature')
            (self.root/'config.ocio').write_bytes(b'roles: {changed: x}\n')
            with self.assertRaises(ValueError):
                color_context(self.scene(), self.root, self.inventory, {'alpha': 'straight'})


if __name__ == '__main__':
    unittest.main()
