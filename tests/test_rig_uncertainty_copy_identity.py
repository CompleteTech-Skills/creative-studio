"""Copying public observations preserves their admitted ownership separately."""
from copy import deepcopy
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


if __name__ == '__main__':
    unittest.main()
