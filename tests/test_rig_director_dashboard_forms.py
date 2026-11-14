"""Canonical form bounds; no network, provider execution or fabricated context."""
from copy import deepcopy
import unittest

from director_dashboard_forms import validate_planning_form
from rig_errors import RigError


def form():
    return dict(original_prompt='Two robots explore a laboratory.',
        duration=dict(numerator=5, denominator=1), frame_rate=dict(numerator=24, denominator=1),
        resolution=[1920, 1080], style='Stylized robots, muted laboratory lighting.',
        budget=dict(max_provider_calls=0, max_cpu_seconds=100, max_gpu_seconds=0,
                    max_storage_bytes=1000000, max_memory_bytes=1000000,
                    currency=None, max_amount=None, policy_ref=None))


class PlanningFormTests(unittest.TestCase):
    def test_exact_canonical_fields_copy_without_policy_or_execution(self):
        original = form()
        value = validate_planning_form(original)
        self.assertEqual(value, original)
        value['resolution'][0] = 12
        self.assertEqual(original['resolution'][0], 1920)
        self.assertNotIn('context', value)
        self.assertNotIn('production_complete', value)

    def test_rational_timing_never_rounds(self):
        value = form()
        value.update(duration=dict(numerator=1001, denominator=100),
                     frame_rate=dict(numerator=30000, denominator=1001))
        self.assertEqual(validate_planning_form(value), value)
        value['duration'] = dict(numerator=1, denominator=1)
        with self.assertRaises(RigError):
            validate_planning_form(value)

    def test_canonical_contract_rejects_malformed_and_unknown_fields(self):
        mutations = [('original_prompt', ''), ('duration', dict(numerator=0, denominator=1)),
                     ('resolution', [32769, 1080]), ('frame_rate', dict(numerator=True, denominator=1)),
                     ('network', 'allow-configured')]
        for key, value in mutations:
            with self.subTest(key=key):
                data = form(); data[key] = value
                with self.assertRaises(RigError): validate_planning_form(data)

    def test_no_nonfinite_budget_or_unpaired_monetary_bound(self):
        for patch in [dict(max_cpu_seconds=float('nan')), dict(currency='USD'), dict(max_amount=10)]:
            value = form(); value['budget'].update(patch)
            with self.assertRaises(RigError): validate_planning_form(value)

    def test_unknown_nested_policy_fields_cannot_grant_authority(self):
        value = form(); value['budget']['network'] = 'allow-configured'
        with self.assertRaises(RigError): validate_planning_form(value)


if __name__ == '__main__':
    unittest.main()
