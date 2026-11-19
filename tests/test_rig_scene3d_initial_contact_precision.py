"""Independent initial-contact regressions frozen before the predicate repair.

Baseline: 86dd8e6e845f0055fdddc245a2e31894879571c8.
These synthetic triangles are persisted as OBJ, reopened without quantization,
and hash-checked by the existing independent fixture helper. Its exact rational
distance oracle minimizes over the face and closed edges, without calling the
production collision helpers. This is offline geometry evidence only.
"""
from fractions import Fraction
import math
import unittest

import test_rig_scene3d_contact_precision as precision


class InitialContactPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = precision.ContactPrecisionTests()
        self.addCleanup(self._cleanup_fixture)

    def _cleanup_fixture(self):
        self.assertTrue(self.fixture.doCleanups(), 'Synthetic source integrity cleanup failed')

    def test_strictly_separated_stationary_sphere_has_no_contact(self):
        triangle = [
            [12.31864239252063, 2.4744783512101027, -1.020901252205501],
            [-8.024746468385963, -1.6895550683365412, 0.43239213264501597],
            [5.590018511123537, -5.80262113015734, -5.187108017104714],
        ]
        center = [-0.03815808506652956, 0.06294822341987616, -0.1629765024258417]
        radius = 0.11765086832225397
        # Exact clearance is about +8.637217241851565e-18 metres. A stationary
        # sphere stays outside throughout; no temporal/root tolerance applies.
        squared_distance = precision.exact_distance_squared(center, triangle)
        self.assertGreater(squared_distance, Fraction(radius)**2)
        index = self.fixture.index_from_obj(triangle)
        self.assertIsNone(index.sweep_sphere(center, center, radius))

    def test_overlapping_stationary_sphere_has_initial_contact(self):
        triangle = [
            [258877029.17296016, -97779357.09754065, 96442046.70095032],
            [-88179600.72030865, 59217689.28600697, -39883030.480944134],
            [-174215274.6797548, -85414650.94813463, -47099783.22117342],
        ]
        center = [217784.28221309153, 370510.57219454163, 988781.0360718747]
        radius = 3218293.171926339
        # Exact clearance is about -2.868318934926137e-10 metres. All inputs
        # are inside the public sweep's documented 1e12 metre domain.
        squared_distance = precision.exact_distance_squared(center, triangle)
        self.assertLess(squared_distance, Fraction(radius)**2)
        index = self.fixture.index_from_obj(triangle)
        hit = index.sweep_sphere(center, center, radius)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['fraction'], 0.0)
        self.assertEqual(hit['center'], center)
        self.assertEqual(hit['triangle'], 0)
        for key in ('point', 'normal'):
            self.assertEqual(len(hit[key]), 3)
            self.assertTrue(all(math.isfinite(value) for value in hit[key]))
        self.assertAlmostEqual(math.hypot(*hit['normal']), 1.0, places=12)


if __name__ == '__main__':
    unittest.main()
