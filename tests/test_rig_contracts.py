"""Contract boundaries exercised with real meshes and retained adapter evidence."""
from copy import deepcopy
import json
import os
import subprocess
import sys
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator
from pygltflib import GLTF2

from rig_test_helpers import fixture
from rig_contracts import schemas, validate_contract
from rig_errors import RigError, MAX_JSON_BYTES, decode_json, read_json, sha256
from rig_gltf import export_gltf
from rig_ik import solve_ik, validate_chain
from rig_jobs import save_rig
from rig_models import load_rig
from rig_skeleton import neutral_pose, skeleton_from_heads
from rig_skinning import generate_skin_weights
from rig_validation import validate_rig

ROOT = Path(__file__).resolve().parents[1]


class RigContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rig = fixture(self.root)

    def malformed_controllers(self):
        original = self.rig.manifest['controllers']
        missing = deepcopy(original)
        missing['ik'][0]['bones'][-1] = 'missing'
        duplicate = deepcopy(original)
        duplicate['ik'].append(deepcopy(duplicate['ik'][0]))
        disconnected = deepcopy(original)
        disconnected['ik'][0]['bones'] = ['b0', 'b2', 'b1']
        repeated_bone = deepcopy(original)
        repeated_bone['ik'][0]['bones'] = ['b0', 'b1', 'b1']
        invalid_orientation = deepcopy(original)
        invalid_orientation['ik'][0]['orientation'] = [0, 0, 0, 0]
        return {'missing-bone': missing, 'duplicate-id': duplicate,
                'disconnected-chain': disconnected, 'repeated-bone': repeated_bone,
                'invalid-orientation': invalid_orientation}

    def test_unused_controller_declarations_are_structurally_validated(self):
        # The clips have no contacts, so no solver invocation could accidentally
        # supply validation for these otherwise unused declarations.
        self.assertTrue(all(not clip['contacts'] for clip in self.rig.clips.values()))
        self.assertTrue(validate_rig(self.rig)['ok'])
        for name, controllers in self.malformed_controllers().items():
            with self.subTest(name=name):
                invalid = deepcopy(self.rig)
                invalid.manifest['controllers'] = controllers
                report = validate_rig(invalid)
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][0]['name'], 'structure')
                self.assertEqual(report['checks'][0]['status'], 'failed')

    def test_export_rejects_invalid_controllers_before_writing(self):
        output = self.root / 'preserved.glb'
        output.write_bytes(b'previous output remains intact')
        original = output.read_bytes()
        for name, controllers in self.malformed_controllers().items():
            with self.subTest(name=name):
                invalid = deepcopy(self.rig)
                invalid.manifest['controllers'] = controllers
                with self.assertRaises(RigError):
                    export_gltf(invalid, output)
                self.assertEqual(output.read_bytes(), original)

    def chain_fixture(self, solver, segment_length):
        root = self.root / f'{solver}-{segment_length}'
        rig = fixture(root, animated=False)
        # Every authored bone is one meter long. The IK segments are the
        # distances between evaluated joint origins, which can still collapse.
        rig.manifest['skeleton'] = skeleton_from_heads([
            dict(id=f'b{i}', role=f'joint-{i}', parent=f'b{i-1}' if i else None,
                 head=[0, i * segment_length, 0],
                 tail=[0, i * segment_length + 1, 0], deform=True)
            for i in range(3)
        ], source='procedural-fixture', reference='IK segment boundary fixture', confidence=1.)
        rig.manifest['controllers']['ik'][0]['solver'] = solver
        rig.weights = generate_skin_weights(rig.geometry, rig.skeleton)
        save_rig(rig, operation='test-ik-segment-boundary')
        return load_rig(rig.root)

    def test_unused_chains_reject_unmeasurable_rest_segments_before_export(self):
        for solver in ('two-bone', 'fabrik'):
            for length in (0., 1e-9, 1e-8):
                with self.subTest(solver=solver, segment_length=length):
                    rig = self.chain_fixture(solver, length)
                    self.assertFalse(rig.clips)
                    report = validate_rig(rig)
                    self.assertFalse(report['ok'])
                    self.assertEqual(report['checks'][0]['name'], 'structure')
                    self.assertEqual(report['checks'][0]['status'], 'failed')
                    chain = rig.manifest['controllers']['ik'][0]
                    with self.assertRaises(RigError) as error:
                        validate_chain(rig.skeleton, chain)
                    self.assertEqual(error.exception.report['code'], 'RIG_IK_LENGTH')
                    for extension in ('glb', 'gltf'):
                        output = rig.root / ('preserved.' + extension)
                        original = b'previous output remains intact'
                        output.write_bytes(original)
                        with self.assertRaises(RigError) as error:
                            export_gltf(rig, output)
                        self.assertEqual(error.exception.report['code'], 'RIG_IK_LENGTH')
                        self.assertEqual(output.read_bytes(), original)

    def test_measurable_chain_boundaries_remain_usable(self):
        for solver in ('two-bone', 'fabrik'):
            for length in (1.01e-8, 1.):
                with self.subTest(solver=solver, segment_length=length):
                    rig = self.chain_fixture(solver, length)
                    self.assertFalse(rig.clips)
                    self.assertTrue(validate_rig(rig)['ok'])
                    chain = rig.manifest['controllers']['ik'][0]
                    solved = solve_ik(rig.skeleton, neutral_pose(rig.skeleton),
                                      chain, [0, 2 * length, 0])
                    self.assertTrue(solved.converged, solved.report())
                    for extension in ('glb', 'gltf'):
                        output = rig.root / ('accepted.' + extension)
                        receipt = export_gltf(rig, output)
                        self.assertEqual(receipt['sha256'], sha256(output))

    def test_runtime_pose_keeps_segment_length_guard(self):
        for solver in ('two-bone', 'fabrik'):
            chain = deepcopy(self.rig.manifest['controllers']['ik'][0])
            chain['solver'] = solver
            validate_chain(self.rig.skeleton, chain)
            for bone in ('b1', 'b2'):
                for length in (0., 1e-9, 1e-8):
                    with self.subTest(solver=solver, bone=bone, segment_length=length):
                        pose = neutral_pose(self.rig.skeleton)
                        pose['bones'][bone] = {'translation': [0, length, 0]}
                        with self.assertRaises(RigError) as error:
                            solve_ik(self.rig.skeleton, pose, chain, [0, 2, 0])
                        self.assertEqual(error.exception.report['code'], 'RIG_IK_LENGTH')

    def test_real_glb_and_gltf_receipts_match_published_schema_and_bytes(self):
        schema = schemas()['rig-export.schema.json']
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for extension in ('glb', 'gltf'):
            with self.subTest(extension=extension):
                output = self.root / ('character.' + extension)
                receipt = export_gltf(self.rig, output)
                validator.validate(receipt)
                self.assertIs(validate_contract(receipt, 'RigExportReceipt'), receipt)
                self.assertEqual(receipt['sha256'], sha256(output))
                self.assertEqual(receipt['format'], extension)
                self.assertFalse(receipt['independently_verified'])
                imported = GLTF2().load(str(output))
                self.assertEqual(len(imported.skins), receipt['skins'])
                self.assertEqual([clip.name for clip in imported.animations], receipt['animations'])
                self.assertIsNotNone(imported.meshes[0].primitives[0].attributes.JOINTS_0)
                self.assertGreater(len(imported.animations[0].channels), 0)

    def test_export_receipt_rejects_unknown_fields_and_unearned_verification(self):
        receipt = export_gltf(self.rig, self.root / 'receipt.glb')
        mutations = [lambda r: r.update(extra=True),
                     lambda r: r.update(independently_verified=True),
                     lambda r: r.update(format='obj'),
                     lambda r: r.update(sha256='not-a-hash'),
                     lambda r: r.update(schema_version=2),
                     lambda r: r.update(joints=0)]
        for mutate in mutations:
            invalid = deepcopy(receipt)
            mutate(invalid)
            with self.assertRaises(RigError):
                validate_contract(invalid, 'RigExportReceipt')
        with self.assertRaises(RigError):
            validate_contract(validate_rig(self.rig), 'RigExportReceipt')

    def test_retained_blender_report_contract_requires_reopen_evidence(self):
        # This is schema coverage of retained real Blender output, not a new
        # Blender execution. Opt-in integration tests execute both modes.
        path = ROOT / 'examples/rig-3d/robotic/validation/blender.json'
        receipt = json.loads(path.read_text())
        receipt.update(schema_version=1, format='blend')
        validate_contract(receipt, 'RigExportReceipt')
        validator = Draft202012Validator(schemas()['rig-export.schema.json'])
        validator.validate(receipt)
        for field in ('roundtrip_sha256', 'roundtrip_path', 'compared_vertices',
                      'max_vertex_error_m', 'max_joint_error_m', 'tolerance_m'):
            invalid = deepcopy(receipt)
            invalid.pop(field)
            with self.subTest(missing=field), self.assertRaises(RigError):
                validate_contract(invalid, 'RigExportReceipt')
        invalid = deepcopy(receipt)
        invalid['reopened'] = False
        with self.assertRaises(RigError):
            validate_contract(invalid, 'RigExportReceipt')


class SharedJsonDecoderTests(unittest.TestCase):
    def code(self, payload, expected):
        with self.assertRaises(RigError) as caught:
            decode_json(payload)
        self.assertEqual(caught.exception.report['code'], expected)

    def test_actual_5000_digit_integer_reports_structured_decode_error(self):
        previous = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(4300)
            for prefix in ('', '-'):
                with self.subTest(prefix=prefix):
                    self.code('{"limit":'+prefix+'9'*5000+'}', 'RIG_JSON')
        finally:
            sys.set_int_max_str_digits(previous)

    def test_integer_conversion_limit_boundary_stays_supported(self):
        previous = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            self.assertEqual(decode_json('9'*640), 10**640-1)
            self.code('9'*641, 'RIG_JSON')
        finally:
            sys.set_int_max_str_digits(previous)

    def test_specific_duplicate_nonfinite_and_depth_codes_survive(self):
        for payload, expected in ((b'{"x":1,"x":2}', 'RIG_JSON_DUPLICATE'),
                ('{"x":NaN}', 'RIG_NONFINITE'), ('{"x":Infinity}', 'RIG_NONFINITE'),
                ('{"x":-Infinity}', 'RIG_NONFINITE'), ('{"x":1e999}', 'RIG_NONFINITE'),
                ('['*65+'0'+']'*65, 'RIG_JSON_DEPTH')):
            with self.subTest(payload=payload):
                self.code(payload, expected)
        self.assertEqual(decode_json('['*64+'0'+']'*64),
                         json.loads('['*64+'0'+']'*64))

    def test_json_byte_bound_and_unicode(self):
        self.assertEqual(decode_json(b'true'+b' '*(MAX_JSON_BYTES-4)), True)
        self.code(b' '*(MAX_JSON_BYTES+1), 'RIG_LIMIT_JSON')
        self.assertEqual(decode_json('{"name":"雪"}'.encode()), {'name': '雪'})
        self.code(b'{"name":"\xff"}', 'RIG_JSON')
        self.code('{"x":', 'RIG_JSON')
        with self.assertRaises(RigError) as caught:
            decode_json('['*2000+'0'+']'*2000)
        # CPython versions differ in the parser's own nesting threshold.
        self.assertIn(caught.exception.report['code'], ('RIG_JSON', 'RIG_JSON_DEPTH'))

    def test_unrelated_runtime_errors_are_not_reclassified(self):
        with patch('rig_errors.json.loads', side_effect=RuntimeError('sentinel')):
            with self.assertRaisesRegex(RuntimeError, 'sentinel'):
                decode_json('{}')
        # Validation is outside the decoder exception boundary.
        with patch('rig_errors.finite_json', side_effect=ValueError('validation sentinel')):
            with self.assertRaisesRegex(ValueError, 'validation sentinel'):
                decode_json('{}')

    def test_actual_cli_rejects_huge_integer_without_persisting_state(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = fixture(temp)
            before = {p.relative_to(rig.root).as_posix(): sha256(p)
                      for p in rig.root.rglob('*') if p.is_file()}
            document = Path(temp)/'limits.json'
            document.write_text('{"bones":'+'9'*5000+'}')
            original = sha256(document)
            env = dict(os.environ, PYTHONINTMAXSTRDIGITS='4300', PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/rig.py'),
                'configure', '--run-dir', str(rig.root), '--file', str(document)],
                capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertIs(report['ok'], False)
            self.assertEqual(report['error']['code'], 'RIG_JSON')
            self.assertNotIn('Traceback', result.stderr)
            after = {p.relative_to(rig.root).as_posix(): sha256(p)
                     for p in rig.root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(original, sha256(document))
            with self.assertRaises(RigError) as caught:
                read_json(document)
            self.assertEqual(caught.exception.report['code'], 'RIG_JSON')

    def test_multibyte_string_and_byte_payloads_share_the_utf8_byte_cap(self):
        for limit, valid, oversized in ((8, '"ééé"', '"éééé"'),
                                        (6, '"😀"', '"😀é"')):
            with self.subTest(limit=limit), patch('rig_errors.MAX_JSON_BYTES', limit):
                for payload in (valid, valid.encode('utf-8')):
                    self.assertEqual(decode_json(payload), json.loads(valid))
                for payload in (oversized, oversized.encode('utf-8')):
                    self.code(payload, 'RIG_LIMIT_JSON')
        # JSON escape spelling is ASCII bytes, not the decoded string's size.
        with patch('rig_errors.MAX_JSON_BYTES', 8):
            self.assertEqual(decode_json('"\\u00e9"'), 'é')

    def test_raw_invalid_unicode_encoding_has_structured_diagnostic(self):
        for surrogate in ('\ud800', '\udfff'):
            with self.subTest(surrogate=repr(surrogate)):
                self.code('"'+surrogate+'"', 'RIG_JSON')
        # An explicitly escaped surrogate retains the decoder's prior semantics.
        self.assertEqual(decode_json('"\\ud800"'), '\ud800')

    def test_multibyte_byte_budget_accumulates_across_encoding_chunks(self):
        valid = '"'+'é'*9000+'"'
        with patch('rig_errors.MAX_JSON_BYTES', 18002):
            self.assertEqual(decode_json(valid), 'é'*9000)
            self.assertEqual(decode_json(valid.encode()), 'é'*9000)
        with patch('rig_errors.MAX_JSON_BYTES', 18001):
            self.code(valid, 'RIG_LIMIT_JSON')
            self.code(valid.encode(), 'RIG_LIMIT_JSON')

    def test_multibyte_oversize_and_invalid_raw_unicode_reject_before_parser(self):
        with patch('rig_errors.MAX_JSON_BYTES', 8):
            for payload, code in (('"éééé"', 'RIG_LIMIT_JSON'),
                                  ('"éééé"'.encode(), 'RIG_LIMIT_JSON'),
                                  ('"\ud800"', 'RIG_JSON')):
                with self.subTest(payload=repr(payload)), patch('rig_errors.json.loads') as parser:
                    self.code(payload, code)
                    parser.assert_not_called()
