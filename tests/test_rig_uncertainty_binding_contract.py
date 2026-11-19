"""Native persistence boundaries for optional anatomy occurrence metadata."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from rig_errors import RigError, atomic_json, object_hash
from rig_jobs import save_rig
from rig_models import load_rig, rig_signature
import test_rig_legacy_uncertainty as native_fixture


class AnatomyBindingContractTests(unittest.TestCase):
    def setUp(self):
        case = native_fixture.RigLegacyUncertaintyTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case
        self.rig = case.rig

    def inventory(self):
        return {path.relative_to(self.rig.root).as_posix(): path.read_bytes()
                for path in self.rig.root.rglob('*') if path.is_file()}

    def test_metadata_free_historical_manifest_load_preserves_bytes_and_signature(self):
        manifest = deepcopy(self.rig.manifest)
        manifest.pop('anatomy_uncertainty_bindings')
        atomic_json(self.rig.root / 'rig.json', manifest)
        before = self.inventory()
        loaded = load_rig(self.rig.root)
        self.assertEqual(loaded.manifest, manifest)
        self.assertEqual(rig_signature(loaded.manifest), rig_signature(manifest))
        self.assertEqual(self.inventory(), before)

    def test_invalid_present_metadata_refuses_before_geometry_loading(self):
        original = deepcopy(self.rig.manifest)
        cases = []
        for bindings in ([], ['not-a-digest', None], ['0' * 64, None]):
            manifest = deepcopy(original)
            manifest['anatomy_uncertainty_bindings'] = bindings
            cases.append(manifest)
        missing_owner = deepcopy(original)
        missing_owner['uncertainties'][0]['element'] = 'unrelated-missing-bone'
        missing_owner['anatomy_uncertainty_bindings'][0] = object_hash(
            missing_owner['uncertainties'][0])
        cases.append(missing_owner)
        for index, manifest in enumerate(cases):
            with self.subTest(case=index):
                atomic_json(self.rig.root / 'rig.json', manifest)
                before = self.inventory()
                with patch('rig_models.load_geometry') as geometry:
                    with self.assertRaises(RigError) as raised:
                        load_rig(self.rig.root)
                    self.assertIn(raised.exception.report['code'],
                                  ('RIG_SCHEMA', 'RIG_UNCERTAINTY_BINDING'))
                    geometry.assert_not_called()
                self.assertEqual(self.inventory(), before)

    def test_save_demotes_changed_occurrence_and_keeps_other_binding(self):
        original = deepcopy(self.rig.manifest)
        before = self.inventory()
        self.rig.manifest['uncertainties'][0]['message'] += ' Synthetic independent note.'
        save_rig(self.rig, operation='synthetic-binding-persistence-control')
        loaded = load_rig(self.rig.root)
        self.assertEqual(loaded.manifest['anatomy_uncertainty_bindings'],
                         [None, original['anatomy_uncertainty_bindings'][1]])
        self.assertEqual(loaded.manifest['source'], original['source'])
        for name, content in before.items():
            if name.startswith(('source/', 'rig/revisions/', 'rig/corrections/')):
                self.assertEqual((self.rig.root / name).read_bytes(), content)

    def test_deleting_one_fitted_record_and_binding_preserves_the_other_owner(self):
        issues = self.rig.manifest['uncertainties']
        bindings = self.rig.manifest['anatomy_uncertainty_bindings']
        self.assertEqual(bindings, [object_hash(issue) for issue in self.case.initial])
        remaining = deepcopy(issues[1])
        del issues[0], bindings[0]
        save_rig(self.rig, operation='synthetic-paired-occurrence-removal')
        self.case.rig = load_rig(self.rig.root)
        self.assertEqual(self.case.rig.manifest['uncertainties'], [remaining])
        self.assertEqual(self.case.rig.manifest['anatomy_uncertainty_bindings'], [object_hash(remaining)])
        self.case.apply(self.case.remove(remaining['element']))
        self.assertEqual(self.case.rig.manifest['uncertainties'], [])
        self.assertEqual(self.case.rig.manifest['anatomy_uncertainty_bindings'], [])

    def fitted_and_opaque_twins(self):
        issue = self.rig.manifest['uncertainties'][0]
        digest = object_hash(issue)
        self.assertEqual(self.rig.manifest['anatomy_uncertainty_bindings'][0], digest)
        self.rig.manifest['uncertainties'][1] = dict(issue)
        self.rig.manifest['anatomy_uncertainty_bindings'][1] = None
        save_rig(self.rig, operation='synthetic-distinct-occurrence-control')
        self.rig = load_rig(self.rig.root)
        self.assertEqual(self.rig.manifest['uncertainties'], [issue, issue])
        self.assertEqual(self.rig.manifest['anatomy_uncertainty_bindings'], [digest, None])
        return issue

    def test_generic_count_change_never_promotes_an_equal_opaque_occurrence(self):
        issue = self.fitted_and_opaque_twins()
        self.rig.manifest['uncertainties'].pop(0)
        save_rig(self.rig, operation='synthetic-generic-occurrence-removal')
        loaded = load_rig(self.rig.root)
        self.assertEqual(loaded.manifest['uncertainties'], [issue])
        self.assertEqual(loaded.manifest['anatomy_uncertainty_bindings'], [None])

    def test_bone_removal_retires_only_the_fitted_twin(self):
        issue = self.fitted_and_opaque_twins()
        self.case.rig = self.rig
        self.case.apply(self.case.remove(issue['element']))
        self.assertEqual(self.case.rig.manifest['uncertainties'], [issue])
        self.assertEqual(self.case.rig.manifest['anatomy_uncertainty_bindings'], [None])

    def test_forged_bindings_do_not_admit_changed_plain_or_aliased_records(self):
        changed, intact = self.rig.manifest['uncertainties']
        changed['message'] += ' Synthetic independent note.'
        plain = dict(intact)
        self.rig.manifest['uncertainties'] = [changed, plain, intact, intact]
        self.rig.manifest['anatomy_uncertainty_bindings'] = [
            object_hash(issue) for issue in self.rig.manifest['uncertainties']]
        save_rig(self.rig, operation='synthetic-forged-bindings')
        loaded = load_rig(self.rig.root)
        self.assertEqual(loaded.manifest['anatomy_uncertainty_bindings'], [None] * 4)

    def test_mismatched_entry_demotes_only_its_owner_and_cannot_restore_it(self):
        first, second = (object_hash(issue) for issue in self.rig.manifest['uncertainties'])
        self.rig.manifest['anatomy_uncertainty_bindings'][0] = second
        save_rig(self.rig, operation='synthetic-mismatched-binding')
        loaded = load_rig(self.rig.root)
        self.assertEqual(loaded.manifest['anatomy_uncertainty_bindings'], [None, second])
        loaded.manifest['anatomy_uncertainty_bindings'][0] = first
        save_rig(loaded, operation='synthetic-unowned-binding')
        reopened = load_rig(self.rig.root)
        self.assertEqual(reopened.manifest['anatomy_uncertainty_bindings'], [None, second])


if __name__ == '__main__':
    unittest.main()
