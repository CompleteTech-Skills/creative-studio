"""Exact-byte unit publication fixtures, never production snapshot proof."""
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

import director_contracts as contracts
from director_revision import _bytes, _hash
from director_revision_policy import LogicalRevisionPolicy
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


class LogicalRevisionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT/'examples/director-contracts', self.root/'examples/director-contracts')
        import json
        self.project = json.loads((self.root/'examples/director-contracts/project-fixture.json').read_bytes())
        brief = json.loads((self.root/self.project['brief']['path']).read_bytes())
        brief['project_id'] = self.project['id']
        brief['requirements']['locked'] = ['identity:*']
        brief['semantic_version'] = '1.0.1'
        self.brief = self.store(_bytes(brief), artifact_id=brief['id'], kind=brief['kind'], semantic_version='1.0.1')
        old_brief = self.project['brief']
        self.project['brief'] = self.brief
        self.project['inventory'] = [self.brief if ref == old_brief else ref for ref in self.project['inventory']]
        self.resource = self.store(b'unchanged native source bytes', artifact_id='native-resource', kind='fixture-source')
        self.before = self.store(b'old native snapshot fixture', artifact_id='native-project', kind='scene3d-project',
                                 dependencies=[self.resource])
        self.after = self.store(b'changed native snapshot fixture', artifact_id='native-project', kind='scene3d-project',
                                semantic_version='1.0.1', dependencies=[self.resource])
        self.unrelated = self.store(b'untouched other shot fixture', artifact_id='other-scene', kind='scene3d-project')
        self.review = self.store(b'historical affected review', artifact_id='review', kind='fixture-review')
        self.other_review = self.store(b'unaffected review', artifact_id='other-review', kind='fixture-review')
        self.export = self.store(b'old affected movie bytes', artifact_id='export', kind='fixture-export')
        self.project.update(scenes=[self.before, self.unrelated], reviews=[self.review, self.other_review],
                            exports=[self.export], status='complete')
        for milestone in self.project['milestones']:
            milestone.update(status='passed', evidence=[self.resource], authority='human')
        self.project_ref = self.store(_bytes(self.project), artifact_id=self.project['id'], kind='director-project')
        self.patch = dict(schema_version=1, kind='director-revision-patch', instruction='Set camera camera near to 0.2',
            parent=dict(revision=0, sha256='1'*64, project_sha256=self.before['sha256'], scene_path='scene.json', artifact=self.before),
            change=dict(operation='camera', target='camera', properties={'near': .2}), locked_constraints=['identity:*'])
        self.patch['sha256'] = _hash(self.patch)
        self.patch_ref = self.store(_bytes(self.patch), artifact_id='revision-'+self.patch['sha256'],
                                    kind='director-revision-patch', dependencies=[self.before])
        self.binding = dict(logical_project=self.project_ref, patch=self.patch_ref,
            replacements=[dict(field='scenes', before=self.before, after=self.after)],
            invalidated_reviews=[self.review], invalidated_exports=[self.export])
        self.calls = []
        self.policy = LogicalRevisionPolicy(self.root, binding_resolver=lambda *args: deepcopy(self.binding),
                                             publish_artifact=self.publisher)

    def store(self, payload, *, artifact_id, kind, semantic_version='1.0.0', schema_version=1,
              dependencies=(), fixture=True):
        digest = hashlib.sha256(payload).hexdigest()
        path = 'unit-artifacts/' + artifact_id + '-' + semantic_version + '-' + digest + '.bin'
        target = self.root/path
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            self.assertEqual(target.read_bytes(), payload)
        else:
            target.write_bytes(payload)
        return dict(artifact_id=artifact_id, kind=kind, schema_version=schema_version,
                    semantic_version=semantic_version, path=path, sha256=digest, byte_size=len(payload),
                    media_type='application/octet-stream', provenance_ref=None, fixture=fixture,
                    dependencies=deepcopy(list(dependencies)))

    def publisher(self, payload, **kwargs):
        self.calls.append((payload, deepcopy(kwargs)))
        return self.store(payload, **kwargs)

    def prepare(self, **kwargs):
        return self.policy.prepare(self.project_ref, self.patch_ref,
            expected_project_sha256=kwargs.get('expected', self.project_ref['sha256']),
            next_version=kwargs.get('version', '1.0.1'))

    def repatch(self, operation):
        self.patch['change'] = operation
        self.patch.pop('sha256')
        self.patch['sha256'] = _hash(self.patch)
        self.patch_ref = self.store(_bytes(self.patch), artifact_id='revision-'+self.patch['sha256'],
                                    kind='director-revision-patch', dependencies=[self.before])
        self.binding['patch'] = self.patch_ref

    def test_prepare_advances_logical_document_and_preserves_exact_sources_history(self):
        before_files = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        candidate = self.prepare()
        self.assertEqual(candidate['state'], 'immutable-candidate-unselected')
        project = candidate['project']
        self.assertEqual(project['scenes'], [self.after, self.unrelated])
        for field in ('original_request', 'brief', 'characters', 'asset_registry', 'story', 'shot_plan'):
            self.assertEqual(project[field], self.project[field])
        self.assertEqual(project['reviews'], [self.other_review])
        self.assertEqual(project['exports'], [])
        self.assertEqual(project['semantic_version'], '1.0.1')
        self.assertEqual(project['dependencies'], [self.project_ref, self.patch_ref])
        self.assertIn(self.after, project['inventory'])
        self.assertEqual(before_files, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(self.calls, [])
        contracts.validate_document(project)

    def test_minimum_invalidation_owned_by_policy_and_unaffected_milestones_retained(self):
        project = self.prepare()['project']
        for old, new in zip(self.project['milestones'], project['milestones']):
            if old['name'] in ('planning', 'assets', 'motion'):
                self.assertEqual(old, new)
            else:
                self.assertEqual(new['status'], 'unverified')
                self.assertEqual(new['evidence'], [])
        self.repatch(dict(operation='material', target='mesh', slot='surface', surface={'roughness': .2}))
        assets = next(m for m in self.prepare()['project']['milestones'] if m['name'] == 'assets')
        self.assertEqual(assets['status'], 'unverified')

    def test_publish_readback_and_abandon_leave_old_bytes_and_no_native_mutation(self):
        prepared = self.prepare()
        receipt = self.policy.publish(prepared)
        self.assertEqual(receipt['state'], 'published-unselected')
        self.assertFalse(receipt['native_mutation'])
        self.assertEqual(contracts._read_artifact(self.root, receipt['project']), _bytes(prepared['project']))
        self.assertEqual(receipt['project']['dependencies'], [self.project_ref, self.patch_ref])
        abandoned = self.policy.rollback(receipt)
        self.assertFalse(abandoned['native_mutation'])
        self.assertEqual(contracts._read_artifact(self.root, self.project_ref), _bytes(self.project))
        self.assertEqual(contracts._read_artifact(self.root, self.resource), b'unchanged native source bytes')
        self.assertEqual(contracts._read_artifact(self.root, self.review), b'historical affected review')
        self.assertTrue((self.root/receipt['project']['path']).exists())

    def test_missing_authorities_fail_before_publication(self):
        self.policy.binding_resolver = None
        with self.assertRaises(RigError): self.prepare()
        self.assertEqual(self.calls, [])
        self.policy.binding_resolver = lambda *args: deepcopy(self.binding)
        prepared = self.prepare()
        self.policy.publish_artifact = None
        with self.assertRaises(RigError): self.policy.publish(prepared)

    def test_stale_and_nonadvancing_versions_fail(self):
        for kwargs in ({'expected': '0'*64}, {'version': '1.0.0'}, {'version': '0.9.9'}, {'version': 'bad'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RigError): self.prepare(**kwargs)
        self.binding['logical_project'] = self.after
        with self.assertRaises(RigError): self.prepare()
        self.assertEqual(self.calls, [])

    def test_missing_wrong_duplicate_and_identity_bindings_fail(self):
        original = deepcopy(self.binding)
        for replacement in (
            dict(field='scenes', before=self.unrelated, after=self.after),
            dict(field='scenes', before=self.after, after=self.before),
            dict(field='characters', before=self.project['characters'][0], after=self.after),
            dict(field='brief', before=self.brief, after=self.after)):
            with self.subTest(field=replacement['field']), self.assertRaises(RigError):
                self.binding['replacements'] = [replacement]
                self.prepare()
        self.binding = deepcopy(original)
        self.binding['replacements'] *= 2
        with self.assertRaises(RigError): self.prepare()
        self.binding = deepcopy(original)
        self.binding['invalidated_reviews'] = [self.export]
        with self.assertRaises(RigError): self.prepare()

    def test_native_parent_must_actually_occur_in_logical_document(self):
        self.patch['parent']['artifact'] = self.unrelated
        self.patch['parent']['project_sha256'] = self.before['sha256']
        self.repatch(self.patch['change'])
        with self.assertRaises(RigError): self.prepare()

    def test_actual_tampered_missing_and_unsafe_bytes_fail(self):
        path = self.root/self.after['path']
        saved = path.read_bytes()
        path.write_bytes(b'tampered')
        with self.assertRaises(RigError): self.prepare()
        path.unlink()
        with self.assertRaises(RigError): self.prepare()
        target = self.root/'symlink-target'
        target.write_bytes(saved)
        path.symlink_to(target)
        with self.assertRaises(RigError): self.prepare()

    def test_original_brief_identity_and_character_bytes_are_checked(self):
        character = self.root/self.project['characters'][0]['path']
        character.write_bytes(b'wrong character bytes')
        with self.assertRaises(RigError): self.prepare()

    def test_malformed_patch_and_unsupported_semantic_changes_fail(self):
        for operation in (dict(operation='camera'), dict(operation='duration', target='shot', additional_seconds='1/3'),
                          dict(operation='ending', target='shot'), dict(operation='story')):
            self.repatch(operation)
            with self.subTest(operation=operation), self.assertRaises(RigError): self.prepare()
        self.assertEqual(self.calls, [])

    def test_omitted_original_identity_lock_and_invalid_digest_fail(self):
        self.patch['locked_constraints'] = []
        self.repatch(self.patch['change'])
        with self.assertRaises(RigError): self.prepare()
        self.patch['locked_constraints'] = ['identity:*']
        self.patch['sha256'] = '0'*64
        self.patch_ref = self.store(_bytes(self.patch), artifact_id='bad-digest', kind='director-revision-patch')
        self.binding['patch'] = self.patch_ref
        with self.assertRaises(RigError): self.prepare()

    def test_tampered_candidate_or_changed_owner_mapping_cannot_publish(self):
        prepared = self.prepare()
        prepared['project']['characters'] = []
        with self.assertRaises(RigError): self.policy.publish(prepared)
        prepared = self.prepare()
        self.binding['invalidated_reviews'] = []
        with self.assertRaises(RigError): self.policy.publish(prepared)
        self.assertEqual(self.calls, [])

    def test_publisher_failures_leave_existing_project_bytes(self):
        prepared = self.prepare()
        def fail(*args, **kwargs):
            raise RigError('FIXTURE_PUBLISH_FAILURE', 'Publication fixture failed.')
        self.policy.publish_artifact = fail
        with self.assertRaises(RigError): self.policy.publish(prepared)
        self.assertEqual(contracts._read_artifact(self.root, self.project_ref), _bytes(self.project))
        def wrong(*args, **kwargs):
            return deepcopy(self.before)
        self.policy.publish_artifact = wrong
        with self.assertRaises(RigError): self.policy.publish(prepared)
        self.assertEqual(contracts._read_artifact(self.root, self.project_ref), _bytes(self.project))


class LogicalV2RevisionPolicyTests(unittest.TestCase):
    # Reuse byte-writing fixture utilities, without inheriting and duplicating
    # the v1 test suite. V2 records come from the actual canonical v2 fixture.
    store = LogicalRevisionPolicyTests.store
    publisher = LogicalRevisionPolicyTests.publisher
    repatch = LogicalRevisionPolicyTests.repatch

    def setUp(self):
        LogicalRevisionPolicyTests.setUp(self)
        import json
        fixture = json.loads((ROOT/'examples/director-contracts/project-v2-fixture.json').read_bytes())
        envelopes = json.loads((ROOT/'examples/director-contracts/contracts.json').read_bytes())
        review_envelope = next(d for d in envelopes if d['kind'] == 'director-result' and
                               d['method'] == 'QAService.record_review')
        contracts.validate_document(review_envelope)
        self.other_review = self.store(_bytes(review_envelope), artifact_id='other-review', kind='director-result')
        self.project['reviews'] = [self.review, self.other_review]
        self.project['schema_version'] = fixture['schema_version']
        self.project['milestones'] = deepcopy(fixture['milestones'])
        for milestone in self.project['milestones']:
            milestone.update(status='passed', evidence=[self.resource], authority='human',
                required_inputs=[self.before, self.brief], artifact_refs=[self.resource],
                evidence_refs=[self.resource], remaining_work=[], failure_reason=None,
                validation_results=[dict(check='unit-fixture-only', status='passed',
                                          evidence=[self.resource], diagnostics=[])])
        contracts.validate_document(self.project)
        self.project_ref = self.store(_bytes(self.project), artifact_id=self.project['id'],
                                      kind='director-project', schema_version=2)
        self.binding['logical_project'] = self.project_ref
        self.timestamp = '2026-11-13T12:01:00Z'

    def prepare(self, **kwargs):
        return self.policy.prepare(self.project_ref, self.patch_ref,
            expected_project_sha256=kwargs.get('expected', self.project_ref['sha256']),
            next_version=kwargs.get('version', '1.0.1'),
            revision_timestamp=kwargs.get('timestamp', self.timestamp))

    def test_v2_exact_records_invalidation_publication_and_rollback(self):
        prepared = self.prepare()
        candidate = prepared['project']
        self.assertEqual(candidate['schema_version'], 2)
        for old, new in zip(self.project['milestones'], candidate['milestones']):
            if old['name'] in ('planning', 'assets', 'motion'):
                self.assertEqual(new, old)
            else:
                self.assertEqual(new['timestamp'], self.timestamp)
                self.assertEqual(new['required_inputs'], [self.after, self.brief, self.patch_ref])
                for field in ('evidence', 'evidence_refs', 'artifact_refs', 'validation_results'):
                    self.assertEqual(new[field], [])
                self.assertIsNone(new['failure_reason'])
                self.assertTrue(new['remaining_work'])
                self.assertEqual(new['status'], 'unverified')
        receipt = self.policy.publish(prepared)
        self.assertEqual(receipt['project']['schema_version'], 2)
        self.assertEqual(contracts._read_artifact(self.root, receipt['project']), _bytes(candidate))
        self.assertFalse(self.policy.rollback(receipt)['native_mutation'])
        self.assertEqual(contracts._read_artifact(self.root, self.project_ref), _bytes(self.project))
        self.assertEqual(contracts._read_artifact(self.root, self.before), b'old native snapshot fixture')
        self.assertEqual(contracts._read_artifact(self.root, self.review), b'historical affected review')
        self.assertEqual(candidate['original_request'], self.project['original_request'])
        self.assertEqual(candidate['brief'], self.project['brief'])
        self.assertEqual(candidate['characters'], self.project['characters'])
        self.assertEqual(candidate['reviews'], [self.other_review])

    def test_v2_missing_malformed_stale_and_future_timestamp_fail(self):
        for timestamp in (None, 'not-a-time', '2026-11-13T11:59:59Z', '2999-01-01T00:00:00Z'):
            with self.subTest(timestamp=timestamp), self.assertRaises(RigError):
                self.prepare(timestamp=timestamp)
        self.assertEqual(self.calls, [])

    def test_v2_no_retained_approval_and_publication_failure_cannot_change_sources(self):
        prepared = self.prepare()
        prepared['project']['milestones'][2]['evidence_refs'] = [self.review]
        with self.assertRaises(RigError): self.policy.publish(prepared)
        self.assertEqual(self.calls, [])
        prepared = self.prepare()
        def fail(*args, **kwargs):
            raise RigError('FIXTURE_PUBLISH_FAILURE', 'V2 fixture publication failure.')
        self.policy.publish_artifact = fail
        with self.assertRaises(RigError): self.policy.publish(prepared)
        self.assertEqual(contracts._read_artifact(self.root, self.project_ref), _bytes(self.project))
        self.assertEqual(contracts._read_artifact(self.root, self.resource), b'unchanged native source bytes')


if __name__ == '__main__':
    unittest.main()
