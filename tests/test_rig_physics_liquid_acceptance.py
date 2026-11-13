"""Analytic surface/negative acceptance cases; these are not solver evidence."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_liquid_acceptance import clipped_surface, measure_surface, assess_series


class LiquidAcceptanceTests(unittest.TestCase):
    def test_clipping_measures_surface_area_not_vertex_population_or_mass(self):
        vertices = [[-1, .2, -1], [1, .2, -1], [1, .2, 1], [-1, .2, 1]]
        bounds = [[-.5, 0, -.5], [.5, 1, .5]]
        self.assertAlmostEqual(clipped_surface(vertices, [[0, 1, 2], [0, 2, 3]], bounds), 1.)
        self.assertEqual(clipped_surface(vertices, [[0, 1, 2]], [[-1, 1, -1], [1, 2, 1]]), 0.)
        # A different triangulation preserves area; no topology-index velocities.
        self.assertAlmostEqual(clipped_surface(vertices, [[0, 1, 3], [1, 2, 3]], bounds), 1.)

    def test_excursion_includes_underfloor_and_above_rim_overflow(self):
        for vertex, expected in [([0, -.4, 0], .4), ([1.3, 2., 0], .4), ([0, .2, -1.3], .4)]:
            result = measure_surface([vertex], [], cell_size=.125)
            self.assertAlmostEqual(result['exterior_excursion_m'], expected)
        self.assertEqual(measure_surface([], [], cell_size=.125)['pool_surface_area_m2'], 0.)

    def series(self):
        result = {}
        for case in ('baseline', 'viscous', 'uncontained'):
            result[case] = [dict(lowest_surface_m=2. if frame < 18 else .1,
                pool_radial_extent_m=.7 if case == 'baseline' else .3,
                pool_surface_area_m2=0. if frame < 18 or case == 'uncontained' else 1.,
                exterior_excursion_m=0.) for frame in range(48)]
        return result

    def assess(self, samples):
        return assess_series(samples, cell_size=.125, emission_end_frame=12)

    def test_named_windows_and_resolution_based_envelopes(self):
        report = self.assess(self.series())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['viscosity_frames'], list(range(19, 25)))
        self.assertEqual(report['leakage_envelope_m'], .25)
        self.assertEqual(report['minimum_retained_surface_m2'], .25)

    def test_no_change_wrong_direction_and_subcell_viscosity_fail(self):
        for radius in (.7, .8, .6):
            samples = self.series()
            for frame in samples['viscous']:
                frame['pool_radial_extent_m'] = radius
            self.assertFalse(self.assess(samples)['checks']['viscosity_reduces_impact_spread'])

    def test_immediate_leak_absent_control_and_overflow_fail(self):
        for case, field, value, check in [
            ('baseline', 'pool_surface_area_m2', 0., 'accumulated_surface_retained'),
            ('uncontained', 'pool_surface_area_m2', 1., 'uncontained_control_drains'),
            ('baseline', 'exterior_excursion_m', .251, 'contained_leakage_envelope'),
            ('viscous', 'exterior_excursion_m', .251, 'contained_leakage_envelope')]:
            samples = self.series()
            for frame in samples[case][36:]:
                frame[field] = value
            self.assertFalse(self.assess(samples)['checks'][check])

    def test_mean_effect_requires_direction_through_contact_transient(self):
        samples = self.series()
        # An early contact transient need not independently resolve a full
        # cell; the mean still must exceed the unchanged two-cell envelope.
        samples['viscous'][19]['pool_radial_extent_m'] = .69
        self.assertTrue(self.assess(samples)['checks']['viscosity_reduces_impact_spread'])
        samples['viscous'][19]['pool_radial_extent_m'] = .71
        self.assertFalse(self.assess(samples)['checks']['viscosity_reduces_impact_spread'])

    def test_disappearing_viscous_fluid_cannot_look_like_reduced_spread(self):
        samples = self.series()
        for frame in samples['viscous'][19:]:
            frame.update(lowest_surface_m=None, pool_radial_extent_m=0., pool_surface_area_m2=0.)
        checks = self.assess(samples)['checks']
        self.assertFalse(checks['viscosity_surfaces_present'])
        self.assertFalse(checks['viscous_surface_retained'])
        self.assertFalse(checks['viscosity_reduces_impact_spread'])


if __name__ == '__main__':
    unittest.main()
