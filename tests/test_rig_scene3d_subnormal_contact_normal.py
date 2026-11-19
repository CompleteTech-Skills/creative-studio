"""Independent unit-normal regressions frozen before normalization repair.

Baseline: d4aaa0829e2d9a14a438c41814845ec59a9a49cb.
Native synthetic OBJ import and source integrity checks reuse the independent
fixture helper. The nearest point is analytically the origin: both triangle
coordinates in its plane are nonpositive, while all center coordinates are
positive. Thus the initial contact direction is (1, 1, 1) / sqrt(3), at every
positive scale. This is offline geometry evidence, not production acceptance.
"""
from fractions import Fraction
import math
import unittest

import test_rig_scene3d_contact_precision as precision


class SubnormalContactNormalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = precision.ContactPrecisionTests()
        self.addCleanup(self._cleanup_fixture)

    def _cleanup_fixture(self):
        self.assertTrue(self.fixture.doCleanups(), 'Synthetic source integrity cleanup failed')

    def assert_initial_normal(self, scale, moving):
        triangle = [[0, 0, 0], [-1, 0, 0], [0, -1, 0]]
        center, radius = [scale]*3, 2*scale
        end = [1, 1, 1] if moving else center
        self.assertEqual(precision.exact_distance_squared(center, triangle), 3*Fraction(scale)**2)
        self.assertLess(3*Fraction(scale)**2, Fraction(radius)**2)
        hit = self.fixture.index_from_obj(triangle).sweep_sphere(center, end, radius)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['fraction'], 0.0)
        self.assertEqual(hit['center'], center)
        self.assertEqual(hit['point'], [0, 0, 0])
        self.assertEqual(hit['triangle'], 0)
        self.assertEqual(len(hit['normal']), 3)
        self.assertTrue(all(math.isfinite(value) for value in hit['normal']))
        self.assertAlmostEqual(math.hypot(*hit['normal']), 1.0, places=12)
        for component in hit['normal']:
            self.assertAlmostEqual(component, 1/math.sqrt(3), places=12)

    def test_minimum_subnormal_stationary(self):
        self.assert_initial_normal(math.ulp(0.0), False)

    def test_minimum_subnormal_moving(self):
        self.assert_initial_normal(math.ulp(0.0), True)

    def test_larger_subnormal_stationary(self):
        self.assert_initial_normal(1e-320, False)

    def test_larger_subnormal_moving(self):
        self.assert_initial_normal(1e-320, True)

    def test_ordinary_scale_stationary_control(self):
        self.assert_initial_normal(0.25, False)

    def test_ordinary_scale_moving_control(self):
        self.assert_initial_normal(0.25, True)


if __name__ == '__main__':
    unittest.main()
