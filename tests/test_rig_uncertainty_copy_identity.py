"""Copying public observations preserves their admitted ownership separately."""
from copy import copy, deepcopy
import unittest

import test_rig_uncertainty_occurrence_mutation as native_fixture


class UncertaintyCopyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = native_fixture.UncertaintyOccurrenceMutationTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_deepcopy_keeps_equal_known_and_opaque_occurrences_distinct(self):
        case = self.case
        case.rig.manifest['uncertainties'] = deepcopy(case.rig.manifest['uncertainties'])
        case.save_and_reload()
        case.remove_left_jaw()
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip, case.jaw])
        case.correct([dict(operation='bone-update', target='gripper',
                           value=dict(id='copied-gripper', roll=.25))])
        self.assertEqual(case.rig.manifest['uncertainties'],
                         [dict(case.grip, element='copied-gripper', resolution='corrected'), case.jaw])

    def test_replaced_observation_cannot_take_binding_before_direct_correction(self):
        case = self.case
        issues = case.rig.manifest['uncertainties']
        issues[2] = deepcopy(issues[1])
        issues.pop(1)
        case.remove_left_jaw(direct=True)
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip, case.jaw])
        self.assertIsNone(case.rig.manifest['anatomy_uncertainty_bindings'][1])

    def test_changed_code_cannot_reassign_a_different_occurrences_binding(self):
        case = self.case
        issues = case.rig.manifest['uncertainties']
        issues[2] = deepcopy(issues[1])
        issues[1]['code'] = 'RIG_SURFACE_UNCERTAIN'
        reclassified = dict(issues[1])
        case.remove_left_jaw(direct=True)
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip, reclassified, case.jaw])
        self.assertIsNone(case.rig.manifest['anatomy_uncertainty_bindings'][1])

    def test_shallow_copy_cannot_take_binding_on_generic_save(self):
        case = self.case
        issues = case.rig.manifest['uncertainties']
        issues[2] = copy(issues[1])
        issues.pop(1)
        case.save_and_reload()
        self.assertIsNone(case.rig.manifest['anatomy_uncertainty_bindings'][1])
        case.remove_left_jaw()
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip, case.jaw])

    def test_transplant_from_a_copied_array_cannot_take_ownership_in_the_original(self):
        case = self.case
        issues = case.rig.manifest['uncertainties']
        issues[2] = deepcopy(issues)[1]
        issues.pop(1)
        case.save_and_reload()
        self.assertIsNone(case.rig.manifest['anatomy_uncertainty_bindings'][1])
        case.remove_left_jaw()
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip, case.jaw])

    def test_copied_opaque_record_cannot_acquire_another_unique_records_binding(self):
        case = self.case
        known_jaw = case.rig.manifest['uncertainties'][1]
        opaque = deepcopy(case.rig.manifest['uncertainties'][2])
        opaque.clear()
        opaque.update(deepcopy(case.grip))
        case.rig.manifest['uncertainties'] = [known_jaw, opaque]
        case.save_and_reload()
        case.correct([dict(operation='bone-remove', target=name, value=None)
                      for name in ('jaw.left', 'jaw.right', 'gripper')])
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip])
        self.assertEqual(case.rig.manifest['uncertainties'][0]['resolution'], 'pending')

    def test_plain_replacement_is_opaque_even_with_identical_contents(self):
        case = self.case
        case.rig.manifest['uncertainties'][1] = dict(case.rig.manifest['uncertainties'][1])
        case.save_and_reload()
        case.remove_left_jaw()
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip, case.jaw, case.jaw])


    def assert_each_occurrence_bound_once(self):
        bound = [b for b in self.case.rig.manifest['anatomy_uncertainty_bindings'] if b is not None]
        self.assertEqual(len(bound), len(set(bound)))

    def test_original_kept_beside_its_copy_owns_the_occurrence_alone(self):
        case = self.case
        issues = case.rig.manifest['uncertainties']
        case.rig.manifest['uncertainties'] = deepcopy(issues) + [issues[1]]
        case.save_and_reload()
        self.assert_each_occurrence_bound_once()
        self.assertEqual([b is not None for b in case.rig.manifest['anatomy_uncertainty_bindings']],
                         [True, False, False, True])
        case.remove_left_jaw()
        self.assertEqual(case.rig.manifest['uncertainties'], [case.grip, case.jaw, case.jaw])

    def test_copied_array_beside_its_original_does_not_duplicate_ownership(self):
        case = self.case
        issues = case.rig.manifest['uncertainties']
        case.rig.manifest['uncertainties'] = deepcopy(issues) + issues
        case.save_and_reload()
        self.assert_each_occurrence_bound_once()
        self.assertEqual([b is not None for b in case.rig.manifest['anatomy_uncertainty_bindings']],
                         [False, False, False, True, True, False])
        case.remove_left_jaw()
        self.assertEqual(case.rig.manifest['uncertainties'],
                         [case.grip, case.jaw, case.jaw, case.grip, case.jaw])

if __name__ == '__main__':
    unittest.main()
