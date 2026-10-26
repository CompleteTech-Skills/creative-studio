"""Independent admission and compatibility checks for Director P0 contracts.

The examples are engineering contract fixtures, never provider or render proof.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

import director_contracts as dc


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples/director-contracts/contracts.json'

# Enumerated independently from issue #18's mandatory-interface addendum.
REQUIRED_METHODS = {
    'CapabilityRegistry.probe', 'PlanningProvider.generate_structured',
    'AssetResolver.resolve', 'AcquisitionGateway.prepare',
    'AcquisitionGateway.claim', 'AcquisitionGateway.ingest',
    'AcquisitionGateway.fail', 'AcquisitionGateway.reconcile',
    'AssetBuildAdapter.build', 'RigMotionAdapter.prepare',
    'SceneAssembler.assemble', 'FrameEvaluator.evaluate',
    'GraphCompiler.compile', 'BudgetLedger.reserve', 'BudgetLedger.settle',
    'BudgetLedger.release', 'ExecutionService.submit', 'ExecutionService.status',
    'ExecutionService.resume', 'ExecutionService.cancel',
    'RenderAdapter.preflight', 'RenderAdapter.submit', 'RenderAdapter.status',
    'RenderAdapter.cancel', 'RenderAdapter.verify', 'ExportService.export',
    'ExportService.verify', 'QAService.validate', 'QAService.record_review',
    'RevisionService.plan', 'RevisionService.apply', 'RevisionService.impact',
}


class DirectorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def sample(self, kind):
        return deepcopy(next(d for d in self.documents if d['kind'] == kind))

    def method_sample(self, method, kind='director-result'):
        return deepcopy(next(d for d in self.documents
                             if d['kind'] == kind and d['method'] == method))

    @staticmethod
    def diagnostic():
        return {'code': 'INPUT_INVALID', 'severity': 'error', 'stage': 'validation',
                'entity_id': None, 'field_path': 'input', 'expected': 'valid input',
                'observed': 'invalid input', 'retryable': False,
                'recommended_action': 'Correct input.', 'evidence': []}

    @staticmethod
    def integral_json_numbers(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        if isinstance(value, dict):
            return {key: DirectorContractTests.integral_json_numbers(child)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [DirectorContractTests.integral_json_numbers(child) for child in value]
        return value

    @staticmethod
    def artifact(value):
        if isinstance(value, dict):
            if {'artifact_id', 'sha256', 'path', 'byte_size'} <= value.keys():
                return value
            for child in value.values():
                found = DirectorContractTests.artifact(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = DirectorContractTests.artifact(child)
                if found is not None:
                    return found
        return None

    @staticmethod
    def structural_validator(definition=None):
        schema = dc.schemas()['director.schema.json']
        if definition:
            schema = {'$defs': schema['$defs'], '$ref': '#/$defs/' + definition}
        return jsonschema.Draft202012Validator(schema)

    def test_exact_mandatory_methods_and_example_pairs(self):
        self.assertEqual(set(dc.METHODS), REQUIRED_METHODS)
        for kind in ('director-request', 'director-result'):
            with self.subTest(kind=kind):
                methods = [d['method'] for d in self.documents if d['kind'] == kind]
                self.assertEqual(len(methods), len(REQUIRED_METHODS))
                self.assertEqual(set(methods), REQUIRED_METHODS)

    def test_every_document_roundtrips_and_is_unchanged(self):
        before = deepcopy(self.documents)
        for document in self.documents:
            with self.subTest(kind=document['kind'], method=document.get('method')):
                parsed = json.loads(json.dumps(document, allow_nan=False))
                self.structural_validator().validate(parsed)
                dc.validate_document(parsed)
                self.assertEqual(parsed, document)
        self.assertEqual(before, self.documents)

    def test_schema_integral_numeric_forms_preserve_input_representation(self):
        for document in self.documents:
            with self.subTest(kind=document['kind'], method=document.get('method')):
                changed = self.integral_json_numbers(document)
                before = json.dumps(changed, sort_keys=True)
                self.structural_validator().validate(changed)
                dc.validate_document(changed)
                self.assertEqual(json.dumps(changed, sort_keys=True), before)
        fps = {'numerator': 24.0, 'denominator': 1.0}
        self.assertEqual(dc.frame_timestamp(719.0, fps), Fraction(719, 24))
        self.assertEqual(dc.duration_frames({'numerator': 30.0, 'denominator': 1.0}, fps), 720)

    def test_all_documents_reject_unknown_version_and_properties(self):
        for document in self.documents:
            for key, value in (('schema_version', 2), ('_unrecognized', 'bad')):
                with self.subTest(kind=document['kind'], method=document.get('method'), key=key):
                    changed = deepcopy(document)
                    changed[key] = value
                    with self.assertRaises(ValueError):
                        dc.validate_document(changed)

    def test_schema_generation_is_closed_valid_and_committed(self):
        generated = dc.schemas()
        self.assertTrue(generated)
        for name, schema in generated.items():
            with self.subTest(name=str(name)):
                jsonschema.Draft202012Validator.check_schema(schema)
                path = Path(name)
                if not path.is_absolute():
                    path = ROOT / 'profiles' / path
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), schema)

    def test_each_method_enforces_its_own_input_and_output_schema(self):
        requests = {d['method']: d for d in self.documents if d['kind'] == 'director-request'}
        results = {d['method']: d for d in self.documents if d['kind'] == 'director-result'}
        for method in sorted(REQUIRED_METHODS):
            with self.subTest(method=method):
                request, result = deepcopy(requests[method]), deepcopy(results[method])
                dc.validate_method_request(method, request)
                dc.validate_method_result(method, result, request)
                request['input']['_unrecognized'] = True
                with self.assertRaises(jsonschema.ValidationError):
                    self.structural_validator().validate(request)
                with self.assertRaises(ValueError):
                    dc.validate_method_request(method, request)
                self.assertIsInstance(result['value'], dict)
                result['value']['_unrecognized'] = True
                with self.assertRaises(jsonschema.ValidationError):
                    self.structural_validator().validate(result)
                with self.assertRaises(ValueError):
                    dc.validate_method_result(method, result, requests[method])

    def test_common_types_have_strict_generated_definitions(self):
        request, result = self.sample('director-request'), self.sample('director-result')
        samples = {
            'artifact-ref': self.artifact(self.documents),
            'operation-context': request['context'],
            'usage': result['usage'],
            'milestone': self.sample('director-project')['milestones'][0],
            'diagnostic': self.diagnostic(),
        }
        for name, sample in samples.items():
            with self.subTest(name=name):
                validator = self.structural_validator(name)
                validator.validate(sample)
                extra = deepcopy(sample)
                extra['_unrecognized'] = True
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(extra)
                for key in sample:
                    missing = deepcopy(sample)
                    del missing[key]
                    with self.subTest(missing=key), self.assertRaises(jsonschema.ValidationError):
                        validator.validate(missing)

    def test_artifact_paths_and_exact_identity_are_checked(self):
        validator = self.structural_validator('artifact-ref')
        original = self.sample('director-project')
        for path in ('../escape', '/absolute', 'a/../../escape', 'C:/asset',
                     'a\\asset', 'a//asset', 'a/./asset', 'a/CON.txt',
                     'a/trailing.', 'a/trailing ', 'a/<tag>', 'a/what?', 'a/star*',
                     'a/name\n', 'a/name\x00'):
            with self.subTest(path=path):
                changed = deepcopy(original)
                artifact = self.artifact(changed)
                artifact['path'] = path
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(artifact)
                with self.assertRaises(ValueError):
                    dc.validate_document(changed)

    def test_unknown_usage_requires_explanation_and_counts_are_integral(self):
        original = self.sample('director-result')
        for update in (
                lambda d: d['usage']['cpu_seconds'].update(explanation=None),
                lambda d: d['usage']['provider_calls'].update(value=1),
                lambda d: d['usage'].update(currency='USD'),
                lambda d: (d['usage'].update(classification='actual'),
                           d['usage']['provider_calls'].update(value=1.5))):
            with self.subTest(update=update):
                changed = deepcopy(original)
                update(changed)
                self.structural_validator().validate(changed)
                with self.assertRaises(ValueError):
                    dc.validate_document(changed)

    def test_context_rejects_implicit_network_and_paid_authority(self):
        original = self.sample('director-request')
        for policy in ({'network': 'allow-configured'}, {'paid': 'bounded'},
                       {'paid': 'allow-unknown-price'}):
            changed = deepcopy(original)
            changed['context']['policy'].update(policy)
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                dc.validate_document(changed)
        changed = deepcopy(original)
        changed['context']['credentials'] = 'must never be serialized'
        with self.assertRaises(ValueError):
            dc.validate_document(changed)

    def test_all_method_failures_and_cancellations_remain_typed(self):
        for method in sorted(REQUIRED_METHODS):
            original = self.method_sample(method)
            for status in ('blocked_capability', 'blocked_budget', 'awaiting_external_result',
                           'awaiting_review', 'failed', 'canceled'):
                result = deepcopy(original)
                result.update(status=status, value=None, diagnostics=[self.diagnostic()])
                with self.subTest(method=method, status=status):
                    dc.validate_method_result(method, result)
                    result['diagnostics'] = []
                    with self.assertRaises(ValueError):
                        dc.validate_method_result(method, result)
                    result['diagnostics'] = [self.diagnostic()]
                    result['value'] = original['value']
                    with self.assertRaises(ValueError):
                        dc.validate_method_result(method, result)

    def test_result_digest_and_method_cannot_be_rebound(self):
        original = self.method_sample('CapabilityRegistry.probe')
        request = self.method_sample('CapabilityRegistry.probe', 'director-request')
        changed = deepcopy(original)
        changed['input_digest'] = 'f' * 64
        with self.assertRaises(ValueError):
            dc.validate_method_result('CapabilityRegistry.probe', changed, request)
        with self.assertRaises(ValueError):
            dc.validate_method_result('AssetResolver.resolve', original, request)

    def test_results_remain_bound_to_requested_semantics(self):
        mutations = {
            'PlanningProvider.generate_structured':
                lambda value: value.update(template_sha256='f' * 64),
            'FrameEvaluator.evaluate':
                lambda value: value['frame'].update(numerator=1),
            'RenderAdapter.verify':
                lambda value: value['frame_rate'].update(numerator=25),
            'ExportService.export':
                lambda value: value.update(target='gltf'),
            'ExecutionService.status':
                lambda value: value.update(job_id='different-job'),
            'QAService.record_review':
                lambda value: value.update(reviewer_id='different-reviewer'),
        }
        for method, mutate in mutations.items():
            with self.subTest(method=method):
                result = self.method_sample(method)
                request = self.method_sample(method, 'director-request')
                mutate(result['value'])
                # It remains a valid isolated document; the matching request
                # supplies the cross-record invariant under test.
                dc.validate_document(result)
                with self.assertRaises(ValueError):
                    dc.validate_method_result(method, result, request)

    def test_acquisition_reconciliation_preserves_original_claim_and_provider(self):
        method = 'AcquisitionGateway.reconcile'
        request = self.method_sample(method, 'director-request')
        for field in ('claim_id', 'provider_request_id'):
            result = self.method_sample(method)
            result['value'][field] = 'different-' + field
            with self.subTest(field=field), self.assertRaises(ValueError):
                dc.validate_method_result(method, result, request)

    def test_budget_receipts_preserve_operation_and_uncertain_reservation(self):
        for method, wrong_state in (('BudgetLedger.reserve', 'released'),
                                    ('BudgetLedger.release', 'reserved'),
                                    ('BudgetLedger.settle', 'released')):
            result = self.method_sample(method)
            request = self.method_sample(method, 'director-request')
            result['value']['state'] = wrong_state
            with self.subTest(method=method), self.assertRaises(ValueError):
                dc.validate_method_result(method, result, request)
        request = self.method_sample('BudgetLedger.settle', 'director-request')
        request['input']['outcome'] = 'uncertain'
        result = self.method_sample('BudgetLedger.settle')
        result['input_digest'] = hashlib.sha256(json.dumps(
            request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        result['value']['state'] = 'released'
        with self.assertRaises(ValueError):
            dc.validate_method_result('BudgetLedger.settle', result, request)
        result['value']['state'] = 'uncertain'
        dc.validate_method_result('BudgetLedger.settle', result, request)

    def test_graph_edges_need_declared_inputs_and_acyclic_ids(self):
        original = self.method_sample('GraphCompiler.compile')
        artifact = deepcopy(self.artifact(self.documents))
        graph = original['value']
        first = graph['nodes'][0]
        first['id'] = 'first'
        first['inputs'] = [deepcopy(artifact)]
        second = deepcopy(first)
        second['id'] = 'second'
        graph['nodes'].append(second)
        graph['edges'] = [{'source': 'first', 'target': 'second', 'artifact': artifact}]
        dc.validate_document(original)
        for update in (
                lambda d: d['value']['edges'][0].update(target='missing'),
                lambda d: d['value']['nodes'][1].update(inputs=[]),
                lambda d: d['value']['edges'].append(
                    {'source': 'second', 'target': 'first', 'artifact': deepcopy(artifact)}),
                lambda d: d['value']['nodes'][1].update(id='first')):
            changed = deepcopy(original)
            update(changed)
            with self.subTest(update=update), self.assertRaises(ValueError):
                dc.validate_document(changed)

    def test_submission_cannot_claim_completed_execution(self):
        for method in ('ExecutionService.submit', 'RenderAdapter.submit'):
            result = self.method_sample(method)
            result['value']['state'] = 'completed'
            result['value']['artifacts'] = [deepcopy(self.artifact(self.documents))]
            result['value']['native_workers'] = [deepcopy(self.artifact(self.documents))]
            with self.subTest(method=method), self.assertRaises(ValueError):
                dc.validate_document(result)

    def test_preflight_cannot_be_ready_with_missing_capability_or_error(self):
        for field, value in (('missing_capabilities', ['required-renderer']),
                             ('diagnostics', [self.diagnostic()])):
            result = self.method_sample('RenderAdapter.preflight')
            result['value'].update(ready=True)
            result['value'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                dc.validate_document(result)

    def test_project_completion_and_visual_review_are_independent(self):
        original = self.sample('director-project')
        changed = deepcopy(original)
        changed['status'] = 'complete'
        with self.assertRaises(ValueError):
            dc.validate_document(changed)
        changed = deepcopy(original)
        visual = next(x for x in changed['milestones'] if x['name'] == 'visual_qa')
        visual.update(status='passed', authority='automated',
                      evidence=[deepcopy(self.artifact(self.documents))])
        with self.assertRaises(ValueError):
            dc.validate_document(changed)
        changed = deepcopy(original)
        changed['milestones'][-1] = deepcopy(changed['milestones'][0])
        with self.assertRaises(ValueError):
            dc.validate_document(changed)

    def test_physical_scale_and_identity_mappings_are_unambiguous(self):
        for kind in ('director-asset-spec', 'director-character-identity'):
            changed = self.sample(kind)
            changed['scale_meters'] = 0
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                dc.validate_document(changed)
        for mappings in ([{'role': 'arm', 'bone': 'bone1'}, {'role': 'arm', 'bone': 'bone2'}],
                         [{'role': 'left-arm', 'bone': 'bone1'}, {'role': 'right-arm', 'bone': 'bone1'}]):
            changed = self.sample('director-character-identity')
            changed['bone_mapping'] = mappings
            with self.subTest(mappings=mappings), self.assertRaises(ValueError):
                dc.validate_document(changed)
        changed = self.sample('director-character-identity')
        changed['proportions'] = [{'name': 'head-body', 'ratio': 0}]
        with self.assertRaises(ValueError):
            dc.validate_document(changed)

    def test_success_outputs_require_real_artifact_references(self):
        for method, key in (('AssetBuildAdapter.build', 'meshes'),
                            ('RigMotionAdapter.prepare', 'deformation_checks'),
                            ('SceneAssembler.assemble', 'scenes'),
                            ('ExportService.export', 'outputs')):
            changed = self.method_sample(method)
            changed['value'][key] = []
            with self.subTest(method=method), self.assertRaises(ValueError):
                dc.validate_document(changed)

    def test_shots_require_full_half_open_coverage_and_valid_markers(self):
        original = self.sample('director-shot-plan')
        for update in (
                lambda d: d['shots'][0].update(start_frame=1),
                lambda d: d['shots'][0].update(duration_frames=719),
                lambda d: d['shots'][0].update(end_frame=0),
                lambda d: d['shots'][0]['transition'].update(overlap_frames=1),
                lambda d: d.update(shots=d['shots'] + [deepcopy(d['shots'][0])])):
            changed = deepcopy(original)
            update(changed)
            with self.subTest(update=update), self.assertRaises(ValueError):
                dc.validate_document(changed)
        for frame, target in ((720, 'aster-fixture'), (0, 'absent-actor')):
            changed = deepcopy(original)
            changed['shots'][0]['markers'] = [
                {'id': 'marker', 'frame': frame, 'target': target, 'action': 'inspect'}]
            with self.subTest(frame=frame, target=target), self.assertRaises(ValueError):
                dc.validate_document(changed)

    def test_verified_frames_and_silent_video_require_evidence(self):
        original = self.method_sample('RenderAdapter.verify')
        for update in (lambda d: d['value']['frames'].clear(),
                       lambda d: d['value']['frames'][0].update(index=1),
                       lambda d: d['value']['frames'][0]['timestamp'].update(numerator=1),
                       lambda d: d['value']['frames'][0].update(resolution=[2, 1])):
            changed = deepcopy(original)
            update(changed)
            with self.subTest(update=update), self.assertRaises(ValueError):
                dc.validate_document(changed)
        result = self.method_sample('ExportService.verify')
        result['value'].update(target='mp4', verified=True, audio_streams=1,
                               decoded_frames=1, outputs=[deepcopy(self.artifact(self.documents))],
                               verification=[deepcopy(self.artifact(self.documents))])
        with self.assertRaises(ValueError):
            dc.validate_document(result)
        result['value']['audio_streams'] = 0
        dc.validate_document(result)

    def test_adverse_frame_span_is_rejected_before_unbounded_allocation(self):
        result = self.method_sample('RenderAdapter.verify')
        result['value']['frame_range']['end_frame'] = 2_000_000_000
        with self.assertRaises(ValueError):
            dc.validate_document(result)

    def test_bundle_references_ids_and_versions_are_exact(self):
        dc.validate_bundle(self.documents)
        with self.assertRaises(ValueError):
            dc.validate_bundle(self.documents + [deepcopy(self.documents[0])])
        changed = deepcopy(self.documents)
        shot = next(x for x in changed if x['kind'] == 'director-shot-plan')
        shot['story']['artifact_id'] = 'absent-story'
        with self.assertRaises(ValueError):
            dc.validate_bundle(changed)
        changed = deepcopy(self.documents)
        shot = next(x for x in changed if x['kind'] == 'director-shot-plan')
        shot['shots'][0]['beat_ids'] = ['absent-beat']
        with self.assertRaises(ValueError):
            dc.validate_bundle(changed)
        for field, value in (('schema_version', 2), ('semantic_version', '2.0.0')):
            changed = [self.sample('director-creative-brief'), self.sample('director-story-plan')]
            changed[1]['brief'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                dc.validate_bundle(changed)

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd,
                         'Secure descriptor-relative closure is unavailable on this platform.')
    def test_byte_closure_rejects_tampering_and_symlink_without_writes(self):
        result = self.method_sample('PlanningProvider.generate_structured')
        artifact = self.artifact(result)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / artifact['path']
            path.parent.mkdir(parents=True)
            source = (ROOT / artifact['path']).read_bytes()
            path.write_bytes(source)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            dc.validate_bundle([result], root=root)
            integral_result = self.integral_json_numbers(result)
            representation = json.dumps(integral_result, sort_keys=True)
            dc.validate_bundle([integral_result], root=root)
            self.assertEqual(json.dumps(integral_result, sort_keys=True), representation)
            after = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            path.write_bytes(bytes([source[0] ^ 1]) + source[1:])
            with self.assertRaises(ValueError):
                dc.validate_bundle([result], root=root)
            path.unlink()
            path.symlink_to(ROOT / artifact['path'])
            with self.assertRaises(ValueError):
                dc.validate_bundle([result], root=root)
            if hasattr(os, 'mkfifo'):
                path.unlink()
                os.mkfifo(path)
                with self.assertRaises(ValueError):
                    dc.validate_bundle([result], root=root)

    def test_position_basis_is_reversible_and_matches_literal_expectations(self):
        self.assertEqual(dc.convert_position([2, 3, 4]), [2, -4, 3])
        self.assertEqual(dc.convert_position([2, -4, 3], 'blender', 'canonical'), [2, 3, 4])
        self.assertEqual(dc.convert_position([-1, 5, 2], 'canonical', 'canonical'), [-1, 5, 2])
        for vector in ([True, 0, 0], [1, 2], [float('nan'), 0, 0],
                       [0, 0, float('inf')], [10**1000, 0, 0]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                dc.convert_position(vector)
        with self.assertRaises(ValueError):
            dc.convert_position([1, 2, 3], 'unlisted', 'canonical')

    def test_artifact_hashes_versions_and_ids_reject_malformed_values(self):
        original = self.sample('director-project')
        validator = self.structural_validator('artifact-ref')
        for key, value in (('sha256', '0' * 63), ('sha256', 'G' * 64),
                           ('sha256', '0' * 64 + '\n'), ('byte_size', -1),
                           ('byte_size', True), ('semantic_version', 'latest'),
                           ('artifact_id', 'unsafe\n')):
            with self.subTest(key=key, value=value):
                changed = deepcopy(original)
                artifact = self.artifact(changed)
                artifact[key] = value
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(artifact)
                with self.assertRaises(ValueError):
                    dc.validate_document(changed)

    def test_exact_half_open_frame_math(self):
        fps = {'numerator': 24, 'denominator': 1}
        self.assertEqual(dc.duration_frames(30, fps), 720)
        self.assertEqual(dc.frame_timestamp(0, fps), Fraction(0))
        self.assertEqual(dc.frame_timestamp(719, fps), Fraction(719, 24))
        self.assertEqual(dc.frame_timestamp(720, fps), Fraction(30))
        self.assertEqual(dc.duration_frames(1001, {'numerator': 30000, 'denominator': 1001}), 30000)
        with self.assertRaises(ValueError):
            dc.duration_frames(Fraction(1, 100), fps)
        for invalid in (-1, True, float('nan'), float('inf')):
            with self.subTest(frame=invalid), self.assertRaises(ValueError):
                dc.frame_timestamp(invalid, fps)
        for invalid in ({'numerator': 0, 'denominator': 1},
                        {'numerator': 24, 'denominator': 0},
                        {'numerator': True, 'denominator': 1}):
            with self.subTest(fps=invalid), self.assertRaises(ValueError):
                dc.frame_timestamp(1, invalid)

    def test_decoder_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for payload in ('{"schema_version":1,"schema_version":2}',
                        '{"number":NaN}', '{"number":Infinity}',
                        '{"number":-Infinity}', '{"number":1e999}',
                        '{"text":"\\ud800"}'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'bad.json'
                path.write_text(payload, encoding='utf-8')
                with self.assertRaises(ValueError):
                    dc.load_document(path)

    def test_import_keeps_2d_dependency_boundary(self):
        script = '''
import sys
class RejectOptional:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'pygltflib', 'bpy', 'scipy', 'trimesh'}:
            raise RuntimeError('Optional backend imported: ' + fullname)
sys.meta_path.insert(0, RejectOptional())
import director_contracts
director_contracts.schemas()
print('PASS')
'''
        result = subprocess.run([sys.executable, '-c', script],
            cwd=ROOT, env={'PYTHONPATH': str(ROOT / 'scripts')},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'PASS')


if __name__ == '__main__':
    unittest.main()
