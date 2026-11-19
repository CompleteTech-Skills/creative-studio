"""Independent #52 count and privacy boundaries through saved native refits.

The fixture uses public dispatch, real GLB import and fresh disk reloads. No
correction implementation helpers, schema changes, mocks or external assets are
used. Two explicitly uncertain landmarks make the combined 1024/1025 boundary
observable without relying on the fitter's default uncertainty count.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest

from rig_contracts import definitions
from rig_demo import make_geometry
from rig_errors import RigError, read_json, timestamp
from rig_jobs import save_rig
from rig_models import load_rig, rig_signature
from rig_operations import mutate
from rig_test_helpers import fixture


class RefitLimitChecks:
    limit = 1024
    marker = 'SYNTHETIC_AUTHORED_OBSERVATION_DO_NOT_ECHO'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name, animated=False)
        self.input_source = Path(self.temp.name) / 'fixture.glb'
        self.source_bytes = self.input_source.read_bytes()
        self.source_record = deepcopy(self.rig.manifest['source'])
        _, self.template, self.landmarks, _ = make_geometry('robotic')
        for joint in ('jaw.left', 'jaw.right'):
            self.landmarks[joint].update(source='human-landmark', confidence=.5,
                                         reference='Synthetic uncertain landmark ' + joint)
        # Establish a real existing correction receipt and immutable revisions,
        # so refusal audits protect existing history as well as new file creation.
        result = mutate(self.rig.root, 'correct', self.parameters('correct'))
        self.assertEqual(result['status'], 'succeeded')
        self.rig = load_rig(self.rig.root)
        self.anatomy = deepcopy(self.rig.manifest['uncertainties'])
        self.assertEqual({u['element'] for u in self.anatomy}, {'jaw.left', 'jaw.right'})
        self.assertEqual(len(self.anatomy), 2)
        self.assertTrue(all(u['code'] == 'RIG_ANATOMY_UNCERTAIN' and
                            u['resolution'] == 'pending' for u in self.anatomy))
        self.assertEqual(definitions()['Rig']['properties']['uncertainties']['maxItems'],
                         self.limit)

    def parameters(self, kind=None):
        kind = kind or self.kind
        if kind == 'auto':
            return dict(template='robotic', replace=True,
                        landmarks=dict(source_sha256=self.rig.manifest['source']['sha256'],
                                       landmarks=deepcopy(self.landmarks)))
        correction = dict(schema_version=1, kind='hatch-3d-rig-correction',
            rig_id=self.rig.manifest['id'], rig_version=self.rig.manifest['version'],
            expected_sha256=rig_signature(self.rig.manifest),
            author='synthetic boundary test', reason='Offline count boundary',
            created_at=timestamp(), edits=[dict(operation='template', target='robotic',
                value=dict(template=deepcopy(self.template), landmarks=deepcopy(self.landmarks)))])
        return dict(correction=correction)

    def seed(self, count):
        self.independent = [dict(element='jaw.left', code='FIXTURE_BINDING_OBSERVATION',
            message=f'{self.marker}:{index}',
            evidence=dict(source='import-metadata', reference=f'Synthetic source note {index}',
                          confidence=.3), resolution=('pending', 'accepted', 'corrected')[index % 3])
            for index in range(count)]
        self.rig.manifest['uncertainties'] = deepcopy(self.independent)
        save_rig(self.rig, operation='synthetic-uncertainty-count-fixture')
        self.rig = load_rig(self.rig.root)
        self.assertEqual(self.rig.manifest['uncertainties'], self.independent)

    def protected_files(self):
        # All original files, including manifest, revisions, correction receipts,
        # geometry, skins and captured source, are compared as actual bytes.
        return {str(path.relative_to(self.rig.root)): path.read_bytes()
                for path in self.rig.root.rglob('*') if path.is_file()
                and path.name not in ('.rig.lock', 'job.json')}

    def assert_source_preserved(self):
        self.assertEqual(self.input_source.read_bytes(), self.source_bytes)
        self.assertEqual(self.rig.manifest['source'], self.source_record)
        self.assertEqual((self.rig.root / self.source_record['path']).read_bytes(),
                         self.source_bytes)

    def refuse(self):
        self.seed(self.limit - len(self.anatomy) + 1)
        self.before = self.protected_files()
        self.assertTrue(any(p.startswith('rig/revisions/') for p in self.before))
        self.assertTrue(any(p.startswith('rig/corrections/') for p in self.before))
        with self.assertRaises(RigError) as caught:
            mutate(self.rig.root, self.kind, self.parameters())
        self.rig = load_rig(self.rig.root)
        self.job = read_json(self.rig.root / 'job.json')
        self.report = caught.exception.report
        self.assert_source_preserved()
        self.assertEqual(self.job['status'], 'failed')
        self.assertEqual(self.job['steps'][self.kind]['status'], 'failed')
        self.assertEqual(self.job['rig_sha256'], rig_signature(self.rig.manifest))

    def assert_compact_count_report(self, report):
        encoded = json.dumps(report, sort_keys=True)
        # Safe assertion messages avoid copying the rejected collection into a
        # failing test log, including when this oracle runs against the defect.
        with self.subTest(check='authored-observation-privacy'):
            self.assertTrue(self.marker not in encoded,
                            'Rejected diagnostic copied synthetic authored observations')
        with self.subTest(check='bounded-message'):
            self.assertLessEqual(len(report['message']), 256)
        with self.subTest(check='bounded-report'):
            self.assertLessEqual(len(encoded), 1024)
        with self.subTest(check='count-and-limit'):
            self.assertTrue(all(re.search(r'\b' + str(n) + r'\b', encoded)
                                for n in (self.limit, self.limit + 1)),
                            'Count refusal must identify observed 1025 and limit 1024')

    def test_combined_limit_succeeds_and_preserves_every_observation(self):
        self.seed(self.limit - len(self.anatomy))
        before = self.protected_files()
        result = mutate(self.rig.root, self.kind, self.parameters())
        self.assertEqual(result['status'], 'succeeded')
        self.rig = load_rig(self.rig.root)
        issues = self.rig.manifest['uncertainties']
        self.assertEqual(len(issues), self.limit)
        self.assertEqual([u for u in issues if u['code'] == 'FIXTURE_BINDING_OBSERVATION'],
                         self.independent)
        self.assertEqual([u for u in issues if u['code'] == 'RIG_ANATOMY_UNCERTAIN'],
                         self.anatomy)
        self.assert_source_preserved()
        self.assertTrue(all((self.rig.root / path).read_bytes() == data
                            for path, data in before.items() if path != 'rig.json'),
                        'Successful refit changed an existing immutable file')
        self.assertEqual(read_json(self.rig.root / 'job.json')['steps'][self.kind]['status'],
                         'succeeded')

    def test_combined_limit_plus_one_refusal_preserves_original_files(self):
        self.refuse()
        after = self.protected_files()
        added = sorted(after.keys() - self.before.keys())
        removed = sorted(self.before.keys() - after.keys())
        changed = sorted(path for path in self.before.keys() & after.keys()
                         if self.before[path] != after[path])
        self.assertEqual(dict(added=added, removed=removed, changed=changed),
                         dict(added=[], removed=[], changed=[]))
        self.assertEqual(self.rig.manifest['uncertainties'], self.independent)

    def test_combined_limit_plus_one_returns_compact_private_count_diagnostic(self):
        self.refuse()
        self.assert_compact_count_report(self.report)

    def test_combined_limit_plus_one_persists_compact_private_failure(self):
        self.refuse()
        error = self.job['steps'][self.kind]['error']
        self.assertEqual(error, self.report)
        self.assert_compact_count_report(error)
        self.assertTrue(self.marker not in json.dumps(self.job),
                        'Persisted job copied synthetic authored observations')


class PublicAutoRefitUncertaintyLimitTests(RefitLimitChecks, unittest.TestCase):
    kind = 'auto'


class PublicTemplateCorrectionUncertaintyLimitTests(RefitLimitChecks, unittest.TestCase):
    kind = 'correct'


if __name__ == '__main__':
    unittest.main(verbosity=2)
