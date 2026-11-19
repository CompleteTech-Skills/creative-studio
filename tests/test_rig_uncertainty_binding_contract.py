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

    def test_generic_count_change_never_promotes_an_equal_opaque_occurrence(self):
        issue = deepcopy(self.rig.manifest['uncertainties'][0])
        self.rig.manifest['uncertainties'] = [issue, deepcopy(issue)]
        self.rig.manifest['anatomy_uncertainty_bindings'] = [object_hash(issue), None]
        save_rig(self.rig, operation='synthetic-distinct-occurrence-control')
        self.rig = load_rig(self.rig.root)
        self.rig.manifest['uncertainties'].pop(0)
        save_rig(self.rig, operation='synthetic-generic-occurrence-removal')
        loaded = load_rig(self.rig.root)
        self.assertEqual(loaded.manifest['uncertainties'], [issue])
        self.assertEqual(loaded.manifest['anatomy_uncertainty_bindings'], [None])


if __name__ == '__main__':
    unittest.main()
