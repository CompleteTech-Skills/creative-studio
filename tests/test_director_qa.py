"""Structural QA executes real bytes/decoders and never infers artistic review."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from director_qa import QAService, validate_profile
from director_schema import MILESTONES
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


def artifact(root, name, kind='test-fixture', media='application/json'):
    raw = (root/name).read_bytes()
    return dict(artifact_id='artifact-'+hashlib.sha256(name.encode()).hexdigest()[:16], kind=kind,
                schema_version=1, semantic_version='1.0.0', path=name,
                sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw), media_type=media,
                fixture=True, provenance_ref=None, dependencies=[])


def setup_qa(root):
    root = Path(root)
    shutil.copytree(ROOT/'examples/director-contracts', root/'examples/director-contracts')
    project = artifact(root, 'examples/director-contracts/project-v2-fixture.json', 'director-project')
    project.update(artifact_id='project-fixture', schema_version=2, semantic_version='2.0.0')
    from PIL import Image
    Image.new('RGBA', (3, 2), (10, 20, 30, 255)).save(root/'actual.png')
    image = artifact(root, 'actual.png', 'rendered-frame', 'image/png')
    profile = dict(schema_version=1, kind='director-qa-profile', id='structural-fixture',
                   checks=[dict(id='decode', operation='image.decode', artifact=image, options={},
                                scene='fixture-scene', shot='fixture-shot', frames=dict(start=0, end=1))],
                   milestones={name: [] for name in MILESTONES}, review_dependencies={'identity': [image]},
                   limits=dict(max_checks=8, max_total_bytes=16*1024**2, max_seconds=60))
    return root, project, profile


def request_for(root, project, profile):
    (root/'profile.json').write_text(json.dumps(profile))
    templates = json.loads((ROOT/'examples/director-contracts/contracts.json').read_text())
    request = deepcopy(next(x for x in templates if x.get('kind') == 'director-request' and x.get('method') == 'QAService.validate'))
    request['context'].update(project_id='project-fixture', project_revision_sha256=project['sha256'],
                              deadline=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat())
    from director_contracts import artifact_references
    refs = {json.dumps(ref, sort_keys=True): ref for ref in artifact_references(profile)}
    request['input'] = dict(artifacts=list(refs.values()), check_profile=artifact(root, 'profile.json', 'director-qa-profile'), thresholds=[])
    return request


class StructuralQATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.project, self.profile = setup_qa(Path(self.tmp.name))

    def supported(self):
        if not (hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd):
            with self.assertRaises(RigError) as failure:
                QAService(self.root, self.project)
            self.assertEqual(failure.exception.report['code'], 'DIRECTOR_PLATFORM')
            return False
        return True

    def test_real_pixels_decode_without_visual_approval(self):
        if not self.supported(): return
        request = request_for(self.root, self.project, self.profile)
        value = QAService(self.root, self.project).validate(request)
        self.assertEqual(value['result']['status'], 'succeeded')
        self.assertEqual(value['report']['checks'][0]['details'], dict(width=3, height=2, format='PNG', decoded=True))
        self.assertFalse(value['report']['production_art_acceptance'])
        self.assertTrue(value['result']['value']['unmeasured'])

    def test_actual_tamper_produces_scoped_bound_finding(self):
        if not self.supported(): return
        request = request_for(self.root, self.project, self.profile)
        (self.root/'actual.png').write_bytes(b'not the pinned PNG')
        value = QAService(self.root, self.project).validate(request)
        self.assertEqual(value['result']['status'], 'failed')
        finding = value['report']['findings'][0]
        self.assertEqual(finding['scene'], 'fixture-scene')
        self.assertEqual(finding['shot'], 'fixture-shot')
        self.assertEqual(finding['frames'], dict(start=0, end=1))
        self.assertEqual(finding['expected_sha256'], self.profile['checks'][0]['artifact']['sha256'])
        self.assertIsNone(finding['current_sha256'])  # never invent a current digest after a failed exact read
        self.assertIsNone(finding['confidence'])
        self.assertTrue(finding['corrective_action'])

    def test_unavailable_native_adapter_is_blocked_not_metadata_acceptance(self):
        if not self.supported(): return
        self.profile['checks'][0]['operation'] = 'native.reopen'
        request = request_for(self.root, self.project, self.profile)
        value = QAService(self.root, self.project).validate(request)
        self.assertEqual(value['result']['status'], 'blocked_capability')
        self.assertEqual(value['report']['checks'][0]['status'], 'blocked')
        self.assertNotEqual(value['report']['status'], 'passed')

    def test_canceled_qa_cannot_pass_or_claim_executed_checks(self):
        if not self.supported(): return
        request = request_for(self.root, self.project, self.profile)
        value = QAService(self.root, self.project).validate(request, cancel=lambda: True)
        self.assertEqual(value['result']['status'], 'canceled')
        self.assertEqual(value['report']['checks'], [])
        self.assertNotEqual(value['report']['status'], 'passed')

    def test_thresholds_and_stale_context_cannot_be_ignored(self):
        if not self.supported(): return
        request = request_for(self.root, self.project, self.profile)
        request['input']['thresholds'] = [dict(name='foot-slide', value=0, unit='m')]
        with self.assertRaises(RigError) as failure:
            QAService(self.root, self.project).validate(request)
        self.assertEqual(failure.exception.report['code'], 'DIRECTOR_QA_THRESHOLD')
        request['input']['thresholds'] = []
        request['context']['project_revision_sha256'] = '0'*64
        with self.assertRaises(RigError) as failure:
            QAService(self.root, self.project).validate(request)
        self.assertEqual(failure.exception.report['code'], 'DIRECTOR_QA_STALE')

    def test_byte_check_cannot_certify_render_or_restore_milestones(self):
        for name in ('rendering', 'native_editability', 'delivery', 'visual_qa'):
            profile = deepcopy(self.profile)
            profile['milestones'][name] = ['decode']
            with self.subTest(name=name), self.assertRaises(RigError):
                validate_profile(profile)

    def test_malformed_profiles_fail_typed_before_native_execution(self):
        for change in ({'checks': []}, {'limits': {'max_seconds': True}},
                       {'milestones': {**self.profile['milestones'], 'scene': [{}]}}):
            profile = {**deepcopy(self.profile), **change}
            with self.subTest(change=change), self.assertRaises(RigError):
                validate_profile(profile)

    def test_profile_schema_agrees_on_json_integer_semantics(self):
        from director_qa import schemas
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(schemas()['director-qa-profile.schema.json'])
        profile = deepcopy(self.profile)
        profile['schema_version'] = 1.0
        profile['limits']['max_checks'] = 8.0
        profile['checks'][0]['frames'] = dict(start=0.0, end=1.0)
        before = json.dumps(profile, sort_keys=True)
        validator.validate(profile)
        validate_profile(profile)
        self.assertEqual(json.dumps(profile, sort_keys=True), before)
        for edit in ('version', 'limit', 'frame', 'newline'):
            bad = deepcopy(profile)
            if edit == 'version': bad['schema_version'] = True
            elif edit == 'limit': bad['limits']['max_checks'] = True
            elif edit == 'frame': bad['checks'][0]['frames']['start'] = False
            else: bad['id'] += '\n'
            with self.subTest(edit=edit):
                self.assertTrue(list(validator.iter_errors(bad)))
                with self.assertRaises(RigError): validate_profile(bad)

    def test_profile_cannot_open_undeclared_inputs(self):
        if not self.supported(): return
        request = request_for(self.root, self.project, self.profile)
        request['input']['artifacts'] = []
        with self.assertRaises(RigError) as failure:
            QAService(self.root, self.project).validate(request)
        self.assertEqual(failure.exception.report['code'], 'DIRECTOR_QA_REFERENCE')


if __name__ == '__main__':
    unittest.main()
