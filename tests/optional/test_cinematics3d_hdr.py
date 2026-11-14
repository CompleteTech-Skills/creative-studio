"""Actual optional OCIO absolute-light HDR numeric tests, no rendered claims."""
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import numpy as np
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import cinematics3d_color as color
import cinematics3d_hdr as hdr


class HDRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdr-color-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()/'profile'
        shutil.copytree(ROOT/'tests/fixtures/cinematics-color/profile', self.root)
        self.inventory = color.capture_config(self.root)
        self.source = dict(kind='cinematics-color-source', schema_version=1,
            config_closure_sha256=self.inventory['closure_sha256'], working_space='Linear Rec.709',
            primaries=deepcopy(color.REC709_D65), transfer='linear', alpha_mode='straight',
            view_applied=False, exposure_applied=False, source_manifest_sha256='a'*64, source_artifact_sha256='b'*64)

    def plan(self, target='hdr-pq', reference_white_nits=100):
        return hdr.make_plan(self.root, self.inventory, self.source, target=target, reference_white_nits=reference_white_nits)

    def test_actual_builtin_discovery_and_absolute_gray_calibration(self):
        caps = hdr.capabilities()
        self.assertEqual(caps['ocio_version'], '2.5.2')
        self.assertTrue(all(row['supported'] for row in caps['targets'].values()))
        for target, oracle in [('hdr-pq', hdr.pq_reference), ('hdr-hlg-1000', hdr.hlg_reference)]:
            with self.subTest(target=target):
                plan = self.plan(target)
                nits = np.array([0, .1, 1, 18, 100, 203, 1000], np.float32)
                rgba = np.ones((len(nits), 4), np.float32)
                rgba[:, :3] = nits[:, None]/100
                before = rgba.copy()
                actual, receipt = hdr.transform_pixels(self.root, plan, rgba)
                np.testing.assert_allclose(actual[:, :3], np.array([[oracle(float(x))]*3 for x in nits]), atol=2e-4, rtol=2e-4)
                np.testing.assert_array_equal(before, rgba)
                np.testing.assert_array_equal(actual[:, 3], rgba[:, 3])
                self.assertIsNone(receipt['mastering_metadata'])
                self.assertFalse(receipt['encoded_video_verified'])
                self.assertEqual(receipt['output_pixels_sha256'], hashlib.sha256(actual.tobytes()).hexdigest())

    def test_explicit_reference_white_changes_actual_pixels_and_identity(self):
        low, high = self.plan(reference_white_nits=100), self.plan(reference_white_nits=203)
        self.assertNotEqual(low['plan_sha256'], high['plan_sha256'])
        value = np.ones((1, 4), np.float32)
        a, _ = hdr.transform_pixels(self.root, low, value)
        b, _ = hdr.transform_pixels(self.root, high, value)
        self.assertGreater(float(b[0, 0]), float(a[0, 0]))
        self.assertAlmostEqual(float(a[0, 0]), hdr.pq_reference(100), delta=2e-4)
        self.assertAlmostEqual(float(b[0, 0]), hdr.pq_reference(203), delta=2e-4)

    def test_actual_out_of_domain_receipt_and_shared_alpha_unassociation(self):
        plan = self.plan()
        values = np.array([[-1., -1., -1., 1.], [200., 200., 200., 1.]], np.float32)
        result, receipt = hdr.transform_pixels(self.root, plan, values)
        self.assertEqual(receipt['input_bt2020_components_below_zero'], 3)
        self.assertEqual(receipt['input_bt2020_components_above_reference_peak'], 3)
        self.assertTrue(bool(((result >= 0) & (result <= 1)).all()))
        straight = np.array([[.18, .4, 2., .25], [0, 0, 0, 0]], np.float32)
        expected, _ = hdr.transform_pixels(self.root, plan, straight)
        premultiplied = straight.copy(); premultiplied[:, :3] *= premultiplied[:, 3:4]
        self.source['alpha_mode'] = 'premultiplied'
        actual, _ = hdr.transform_pixels(self.root, self.plan(), premultiplied)
        np.testing.assert_allclose(actual, expected, atol=2e-5)

    def test_untrusted_inputs_stale_profile_and_forged_plan_fail_closed(self):
        for white in (True, None, -1, 0, 10**400, float('nan'), float('inf')):
            with self.subTest(white=str(white)[:20]), self.assertRaises(ValueError):
                self.plan(reference_white_nits=white)
        for target in (None, [], {}, 'hdr-unknown'):
            with self.assertRaises(ValueError):
                self.plan(target)
        plan = self.plan()
        forged = deepcopy(plan); forged['reference_white_nits'] = 203
        with self.assertRaises(ValueError):
            hdr.transform_pixels(self.root, forged, np.ones((1, 4), np.float32))
        with self.assertRaises(ValueError):
            hdr.transform_pixels(self.root, plan, np.ones((1, 4), np.float32), max_work_bytes=1)
        (self.root/'config.ocio').write_bytes((self.root/'config.ocio').read_bytes()+b'\n')
        with self.assertRaises(ValueError):
            hdr.transform_pixels(self.root, plan, np.ones((1, 4), np.float32))


if __name__=='__main__': unittest.main()
