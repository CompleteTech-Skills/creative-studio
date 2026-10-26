"""Independent Editor3D contract, admission, and coordinate regression checks.

The retained examples are engineering bindings. These tests never promote a
valid binding or a parsed native manifest into scene execution or art approval.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from editor3d_contracts import (MAX_DOCUMENT_BYTES, READ_OPERATIONS,
    WRITE_OPERATIONS, contract_hash, generated_files, load_document, migrate_document, schemas,
    validate_contract, validate_project_bundle, validate_runtime_state)
from editor3d_adapters import (euler_to_quaternion, frame_time,
    quaternion_to_euler, transform_to_blender, validate_api_request,
    verify_native_reference)
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples/editor3d-contracts'
TOKEN = 'editor-regression-token-0123456789'
ORIGIN = 'http://127.0.0.1:8765'


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def example(name):
    return json.loads((EXAMPLES / (name + '.json')).read_text())


def rehash_bundle(project, scenes, nodes, assets, animations):
    """Rebind edited fixtures so semantic probes do not stop at stale hashes."""
    for scene in scenes:
        scene['nodes'] = [dict(id=n['id'], sha256=canonical_hash(n))
            for n in nodes if n['scene_id'] == scene['id']]
    for field, resources in [('scenes', scenes), ('shared_assets', assets),
                             ('animation_clips', animations)]:
        project[field] = [dict(id=x['id'], sha256=canonical_hash(x)) for x in resources]
    return project, scenes, nodes, assets, animations


class EditorContractTests(unittest.TestCase):
    def test_all_eleven_documents_roundtrip_without_mutating(self):
        values = [example(k) for k in ('project', 'scene', 'node', 'asset', 'animation',
            'command', 'revision', 'session', 'derived', 'request', 'response')]
        schema = schemas()['editor3d.schema.json']
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for value in values:
            with self.subTest(kind=value['kind']):
                original = deepcopy(value)
                validator.validate(value)
                validate_contract(value)
                self.assertEqual(value, original)
                self.assertEqual(contract_hash(value), canonical_hash(value))
                migrated = migrate_document(value)
                self.assertEqual(migrated, original)
                self.assertIsNot(migrated, value)
                self.assertEqual(contract_hash(json.loads(json.dumps(value))), canonical_hash(value))

    def test_generated_json_schema_and_typescript_match_authority(self):
        for path, content in generated_files().items():
            self.assertEqual(path.read_text(), content, str(path))
        typescript = (ROOT / 'assets/editor3d/contracts.ts').read_text()
        for kind in ('project', 'scene', 'node', 'asset', 'animation', 'command',
                     'revision', 'session', 'derived', 'request', 'response'):
            self.assertIn('export type ' + kind + ' = ', typescript)
        self.assertIn('runtime admission is still required', typescript)

    def test_every_document_is_closed_versioned_and_required(self):
        for path in EXAMPLES.glob('*.json'):
            original = json.loads(path.read_text())
            if not original.get('kind', '').startswith('editor3d-'):
                continue
            for mutation in ('extra', 'version', 'required'):
                with self.subTest(kind=original['kind'], mutation=mutation):
                    value = deepcopy(original)
                    if mutation == 'extra':
                        value['unexpected'] = 'rejected'
                    elif mutation == 'version':
                        value['schema_version'] = 2
                    else:
                        del value['kind']
                    with self.assertRaises(RigError):
                        validate_contract(value)

    def test_schema_exports_cannot_mutate_later_validation(self):
        exported = schemas()
        exported['editor3d.schema.json']['$defs']['project']['additionalProperties'] = True
        value = example('project'); value['hidden_session'] = {}
        with self.assertRaises(RigError):
            validate_contract(value)

    def test_nested_unknown_and_nonfinite_values_are_rejected(self):
        values = []
        value = example('project'); value['provenance']['fixture'] = 'true'; values.append(value)
        value = example('node'); value['native']['execute'] = 'untrusted.py'; values.append(value)
        for bad in (math.nan, math.inf, -math.inf):
            value = example('session'); value['viewport']['translation'][0] = bad; values.append(value)
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(RigError):
                    validate_contract(value)

    def test_persistent_session_and_derived_state_are_separate(self):
        for kind, field, value in [('project', 'selection', ['node']),
            ('scene', 'playhead', 3), ('node', 'world_matrix', [1] * 16),
            ('session', 'nodes', []), ('derived', 'unsaved_command_ids', [])]:
            doc = example(kind); doc[field] = value
            with self.subTest(kind=kind, field=field), self.assertRaises(RigError):
                validate_contract(doc)

    def test_local_semantics_reject_wrong_clock_provenance_and_quaternion(self):
        mutations = []
        value = example('project'); value['updated_at'] = '2026-10-24T23:59:59Z'; mutations.append(value)
        value = example('project'); value['provenance']['fixture'] = False; mutations.append(value)
        value = example('asset'); value['provenance']['fixture'] = False; mutations.append(value)
        value = example('session'); value['viewport']['rotation'] = [0, 0, 0, 2]; mutations.append(value)
        value = example('animation'); value['interval']['end'] = 0; mutations.append(value)
        value = example('animation'); value['frame_rate'] = {'numerator': 48, 'denominator': 2}; mutations.append(value)
        value = example('node'); value['node_type'] = 'bone'; mutations.append(value)
        for value in mutations:
            with self.subTest(kind=value['kind']), self.assertRaises(RigError):
                validate_contract(value)

    def test_native_file_paths_reject_nonportable_and_remote_inputs(self):
        for path in ('../outside.json', '/tmp/outside.json', 'C:/outside.json',
                     'https://example.com/a.json', 'a\\b.json', 'a/../b.json'):
            value = example('asset'); value['native']['file']['path'] = path
            with self.subTest(path=path), self.assertRaises(RigError):
                validate_contract(value)

    def test_rig_asset_binding_requires_actual_rig_owner(self):
        value = example('asset'); value['asset_type'] = 'rig'
        with self.assertRaises(RigError): validate_contract(value)

    def test_hash_identifiers_and_exact_versions_reject_ambiguous_spellings(self):
        for field, bad in [('id', 'asset\n'), ('version', '01.0.0'),
                           ('version', '1.0.0\n')]:
            value = example('asset'); value['native'][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(RigError):
                validate_contract(value)
        value = example('asset'); value['native']['file']['sha256'] += '\n'
        with self.assertRaises(RigError):
            validate_contract(value)

    def test_boolean_numeric_values_rejected_but_integral_json_numbers_work(self):
        value = example('project'); value['frame_rate'] = {'numerator': 24.0, 'denominator': 1.0}
        validate_contract(value)
        self.assertEqual(frame_time(720, value['frame_rate']), Fraction(30))
        for kind, field in [('project', 'revision'), ('node', 'sibling_order'),
                            ('session', 'playhead')]:
            value = example(kind); value[field] = True
            with self.subTest(kind=kind), self.assertRaises(RigError):
                validate_contract(value)

    def test_status_does_not_imply_execution_or_visual_approval(self):
        value = example('response')
        for status, executed in [('accepted', True), ('complete', False),
                                 ('rejected', True), ('unavailable', True)]:
            bad = deepcopy(value); bad.update(status=status, executed=executed)
            with self.subTest(status=status), self.assertRaises(RigError):
                validate_contract(bad)
        value['review_status'] = 'approved'
        with self.assertRaises(RigError):
            validate_contract(value)
        for kind in ('project', 'response'):
            value = example(kind); self.assertEqual(value['review_status'], 'unreviewed')
        value = example('derived'); value['status'] = 'verified'
        with self.assertRaises(RigError):
            validate_contract(value)

    def test_all_service_operations_have_closed_admission(self):
        original = example('request')
        global_ops = {'projects.list', 'projects.create', 'render.capabilities', 'recovery.list'}
        project_ops = {'projects.open', 'projects.get', 'projects.validate', 'projects.save',
            'projects.close', 'revisions.create', 'revisions.list', 'scenes.list',
            'scenes.create', 'assets.list', 'assets.import', 'render.create',
            'recovery.inspect', 'recovery.dependencies'}
        argument_ops = {'projects.create', 'projects.save', 'scenes.create', 'scenes.command',
            'assets.import', 'assets.relink', 'render.create', 'render.retry', 'recovery.recover'}
        for operation in READ_OPERATIONS + WRITE_OPERATIONS:
            with self.subTest(operation=operation):
                request = deepcopy(original); request['operation'] = operation
                request['project_id'] = None if operation in global_ops else 'project'
                request['target_id'] = None if operation in global_ops | project_ops else 'resource'
                request['argument'] = original['argument'] if operation in argument_ops else None
                if operation in READ_OPERATIONS or operation == 'projects.create':
                    request.update(expected_revision=None, expected_project_sha256=None)
                if operation in READ_OPERATIONS:
                    request['idempotency_key'] = None
                validate_contract(request)
        original['operation'] = 'shell.execute'
        with self.assertRaises(RigError):
            validate_contract(original)

    def test_mutations_require_explicit_idempotency_and_revision(self):
        for field in ('project_id', 'expected_revision', 'expected_project_sha256', 'idempotency_key'):
            value = example('request'); value[field] = None
            with self.subTest(field=field), self.assertRaises(RigError):
                validate_contract(value)
        value = example('request'); value['operation'] = 'projects.create'
        with self.assertRaises(RigError):
            validate_contract(value)

    def bundle(self):
        return example('project'), [example('scene')], [example('node')], [example('asset')], []

    def test_binding_closure_reports_only_binding_acceptance(self):
        bundle = self.bundle(); original = deepcopy(bundle)
        result = validate_project_bundle(*bundle)
        self.assertEqual(result['status'], 'valid-bindings')
        self.assertFalse(result['native_resources_verified'])
        self.assertEqual(result['review_status'], 'unreviewed')
        self.assertEqual(bundle, original)

    def test_bundle_rejects_missing_extra_duplicate_and_stale_bindings(self):
        bundle = self.bundle(); bundle[1].clear()
        with self.assertRaises(RigError): validate_project_bundle(*bundle)
        bundle = self.bundle(); bundle[2].append(deepcopy(bundle[2][0]))
        with self.assertRaises(RigError): validate_project_bundle(*bundle)
        bundle = self.bundle(); bundle[3][0]['original_filename'] = 'changed.json'
        with self.assertRaises(RigError): validate_project_bundle(*bundle)
        bundle = self.bundle(); bundle[0]['scenes'].append(deepcopy(bundle[0]['scenes'][0]))
        with self.assertRaises(RigError): validate_project_bundle(*bundle)

    def test_bundle_rejects_dangling_parent_camera_asset_rig_and_collection(self):
        cases = [('parent', 'unknown'), ('asset_id', 'unknown'), ('rig_id', 'asset'),
                 ('collections', ['unknown'])]
        for field, value in cases:
            bundle = self.bundle(); bundle[2][0][field] = value
            with self.subTest(field=field), self.assertRaises(RigError):
                validate_project_bundle(*rehash_bundle(*bundle))
        bundle = self.bundle(); bundle[1][0]['default_camera'] = 'node'
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))
        bundle = self.bundle(); bundle[1][0]['animations'] = ['missing']
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))

    def test_bundle_rejects_parent_cycle_and_ambiguous_sibling_order(self):
        bundle = self.bundle(); bundle[2][0]['parent'] = 'node'
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))
        bundle = self.bundle(); duplicate = deepcopy(bundle[2][0]); duplicate['id'] = 'second'
        duplicate['native_node_id'] = 'native-second'; bundle[2].append(duplicate)
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))

    def test_bundle_rejects_global_collisions_and_cross_scene_parents(self):
        bundle = self.bundle(); bundle[3][0]['id'] = 'node'
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))
        bundle = self.bundle(); second = deepcopy(bundle[1][0]); second['id'] = 'second-scene'
        bundle[1].append(second); node = deepcopy(bundle[2][0]); node['id'] = 'second-node'
        node['scene_id'] = 'second-scene'; node['parent'] = 'node'; bundle[2].append(node)
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))

    def test_animation_targets_must_exist(self):
        bundle = self.bundle(); animation = example('animation'); animation['targets'] = ['missing']
        bundle[4].append(animation); bundle[1][0]['animations'] = ['animation']
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))


    def test_animation_clock_and_scene_interval_must_agree(self):
        for mutation in ('rate', 'interval', 'outside-scene'):
            bundle = self.bundle(); animation = example('animation')
            if mutation == 'rate': animation['frame_rate']['numerator'] = 30
            elif mutation == 'interval': animation['interval']['end'] = 721
            else:
                second = deepcopy(bundle[1][0]); second['id'] = 'second-scene'
                bundle[1].append(second)
                bundle[2][0]['scene_id'] = 'second-scene'
            bundle[4].append(animation); bundle[1][0]['animations'] = ['animation']
            with self.subTest(mutation=mutation), self.assertRaises(RigError):
                validate_project_bundle(*rehash_bundle(*bundle))

    def test_duplicate_native_node_binding_cannot_alias_another_editor_node(self):
        bundle = self.bundle(); second = deepcopy(bundle[2][0])
        second.update(id='second-node', sibling_order=1)
        bundle[2].append(second)
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))

    def test_orphaned_ancestor_reports_typed_error_before_any_write(self):
        bundle = self.bundle(); first = bundle[2][0]; first['parent'] = 'second-node'
        second = deepcopy(first); second.update(id='second-node', parent='missing',
            native_node_id='second-native', sibling_order=1)
        bundle[2].append(second)
        original = deepcopy(bundle)
        with self.assertRaises(RigError): validate_project_bundle(*rehash_bundle(*bundle))
        self.assertEqual(bundle[2], original[2])

    def test_session_selection_playhead_and_saved_snapshot_are_bound(self):
        project, scenes, nodes, _, _ = self.bundle()
        session = example('session')
        validate_runtime_state(session, project, scenes, nodes)
        for field, value in [('base_revision', 1), ('base_project_sha256', 'f' * 64),
            ('project_id', 'other-project'), ('active_scene', 'missing'),
            ('selection', ['missing']), ('playhead', 721)]:
            bad = deepcopy(session); bad[field] = value
            with self.subTest(field=field), self.assertRaises(RigError):
                validate_runtime_state(bad, project, scenes, nodes)
        session['playhead'] = 720
        validate_runtime_state(session, project, scenes, nodes)
        session['active_scene'] = None
        with self.assertRaises(RigError): validate_runtime_state(session, project, scenes, nodes)

    def test_runtime_state_rejects_nodes_outside_saved_scene_context(self):
        project, scenes, nodes, _, _ = self.bundle()
        extra = deepcopy(nodes[0]); extra.update(id='orphan', scene_id='absent-scene')
        nodes.append(extra)
        for kind in ('session', 'derived'):
            with self.subTest(kind=kind), self.assertRaises(RigError):
                validate_runtime_state(example(kind), project, scenes, nodes)

    def test_derived_clock_and_frame_are_bound_to_saved_project(self):
        project, scenes, nodes, _, _ = self.bundle()
        state = example('derived'); state['frame'] = 720
        validate_runtime_state(state, project, scenes, nodes)
        for field, value in [('frame', 721), ('frame_rate', {'numerator': 30, 'denominator': 1})]:
            bad = deepcopy(state); bad[field] = value
            with self.subTest(field=field), self.assertRaises(RigError):
                validate_runtime_state(bad, project, scenes, nodes)

    def test_derived_and_session_context_reject_stale_or_absent_nodes(self):
        for kind in ('derived', 'session'):
            project, scenes, nodes, _, _ = self.bundle()
            state = example(kind)
            validate_runtime_state(state, project, scenes, nodes)
            with self.subTest(kind=kind, mutation='missing-node'), self.assertRaises(RigError):
                validate_runtime_state(state, project, scenes, [])
            nodes[0]['locked'] = True
            with self.subTest(kind=kind, mutation='stale-node'), self.assertRaises(RigError):
                validate_runtime_state(state, project, scenes, nodes)


class EditorAdapterTests(unittest.TestCase):
    def api(self, value=None, **changes):
        value = example('request') if value is None else value
        payload = json.dumps(value).encode()
        arguments = dict(authorization='Bearer ' + TOKEN, token=TOKEN, origin=ORIGIN,
            server_origin=ORIGIN, content_length=len(payload), current_revision=0,
            current_project_sha256=example('request')['expected_project_sha256'])
        arguments.update(changes)
        return validate_api_request(payload, **arguments)

    def test_authenticated_admission_is_read_only_and_revision_bound(self):
        value = example('request'); original = deepcopy(value)
        result = self.api(value)
        self.assertEqual(result['status'], 'admitted')
        self.assertFalse(result['executed'])
        self.assertEqual(value, original)
        for changes in ({'current_revision': 1}, {'current_project_sha256': 'f' * 64},
                        {'current_revision': None}):
            with self.subTest(changes=changes), self.assertRaises(RigError): self.api(**changes)

    def test_authentication_origin_content_type_and_length_rejections(self):
        for changes in ({'authorization': None}, {'authorization': 'Bearer wrong'},
            {'token': 'short'}, {'origin': 'http://evil.invalid'},
            {'server_origin': 'http://0.0.0.0:8765'}, {'server_origin': 'http://localhost'},
            {'server_origin': 'http://user@localhost:8765'},
            {'server_origin': 'https://localhost:8765'}, {'content_type': 'text/plain'},
            {'content_length': 1}, {'content_length': True}, {'content_length': MAX_DOCUMENT_BYTES + 1}):
            with self.subTest(changes=changes), self.assertRaises(RigError): self.api(**changes)
        self.assertFalse(self.api(origin=None)['executed'])

    def test_duplicate_keys_invalid_utf8_and_nonfinite_json_rejected(self):
        payloads = [b'{"kind":"editor3d-request","kind":"editor3d-project"}',
                    b'{"x":"\xff"}', b'{"x":NaN}', b'{"x":Infinity}']
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(RigError):
                validate_api_request(payload, authorization='Bearer ' + TOKEN, token=TOKEN,
                    origin=ORIGIN, server_origin=ORIGIN, content_length=len(payload))

    def test_exact_half_open_clock_and_ntsc_boundaries(self):
        self.assertEqual(frame_time(0, {'numerator': 24, 'denominator': 1}), 0)
        self.assertEqual(frame_time(719, {'numerator': 24, 'denominator': 1}), Fraction(719, 24))
        self.assertEqual(frame_time(720, {'numerator': 24, 'denominator': 1}), 30)
        self.assertEqual(frame_time(720.0, {'numerator': 24.0, 'denominator': 1.0}), 30)
        self.assertEqual(frame_time(30000, {'numerator': 30000, 'denominator': 1001}), 1001)
        for frame, fps in [(-1, {'numerator': 24, 'denominator': 1}),
            (True, {'numerator': 24, 'denominator': 1}),
            (1, {'numerator': 48, 'denominator': 2}),
            (1, {'numerator': 24, 'denominator': 0}),
            (1, {'numerator': True, 'denominator': 1})]:
            with self.subTest(frame=frame, fps=fps), self.assertRaises(RigError): frame_time(frame, fps)

    def test_actual_native_manifest_hash_identity_and_version(self):
        reference = example('asset')['native']
        report = verify_native_reference(reference, ROOT)
        self.assertEqual(report['status'], 'valid-native-contract')
        self.assertFalse(report['transitive_resources_verified'])
        self.assertEqual(report['review_status'], 'unreviewed')
        for field, value in [('id', 'other'), ('version', '9.9.9'), ('native_kind', 'model3d-job')]:
            altered = deepcopy(reference); altered[field] = value
            with self.subTest(field=field), self.assertRaises(RigError): verify_native_reference(altered, ROOT)
        for field, value in [('sha256', '0' * 64), ('bytes', reference['file']['bytes'] + 1),
                             ('media_type', 'application/octet-stream')]:
            altered = deepcopy(reference); altered['file'][field] = value
            with self.subTest(field=field), self.assertRaises(RigError): verify_native_reference(altered, ROOT)

    def test_unintegrated_owner_is_explicitly_unavailable(self):
        reference = example('scene')['native']
        with self.assertRaises(RigError) as raised:
            verify_native_reference(reference, ROOT)
        self.assertEqual(raised.exception.report['code'], 'EDITOR3D_UNAVAILABLE')

    def test_native_paths_reject_symlinks_without_reading_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root / 'manifest.json'
            try:
                path.symlink_to(ROOT / 'examples/model3d-contracts/robotic.json')
            except (OSError, NotImplementedError) as exc:
                self.skipTest('Symlink creation unavailable: ' + str(exc))
            reference = example('asset')['native']; reference['file']['path'] = path.name
            with self.assertRaises(RigError): verify_native_reference(reference, root)

    def test_coordinate_basis_maps_y_up_and_preserves_input(self):
        value = {'translation': [1, 2, 3], 'rotation': [0, 0, 0, 1], 'scale': [1, 1, 1]}
        original = deepcopy(value); matrix = transform_to_blender(value)
        self.assertEqual([row[3] for row in matrix[:3]], [1, -3, 2])
        self.assertEqual(value, original)
        self.assertEqual(matrix[3], [0, 0, 0, 1])
        value['rotation'] = [0, 0, 0, 2]
        with self.assertRaises(RigError): transform_to_blender(value)

    def test_euler_quaternion_roundtrip_including_gimbal_lock(self):
        for angles in ([0, 0, 0], [.2, -.4, 1.0], [.4, math.pi / 2, -.6],
                       [.4, -math.pi / 2, -.6]):
            with self.subTest(angles=angles):
                q = euler_to_quaternion(angles)
                result = quaternion_to_euler(q)
                recovered = euler_to_quaternion(result['euler'])
                self.assertAlmostEqual(abs(sum(a * b for a, b in zip(q, recovered))), 1, places=10)
                self.assertEqual(result['convention'], 'extrinsic-XYZ-radians')
                if abs(angles[1]) == math.pi / 2:
                    self.assertTrue(result['gimbal_lock'])
        q = euler_to_quaternion([math.pi / 2, 0, 0])
        self.assertAlmostEqual(q[0], math.sqrt(.5))
        self.assertAlmostEqual(q[3], math.sqrt(.5))

    def test_document_reader_rejects_real_file_growth_after_metadata_check(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'input.json'
            path.write_text('{}')
            real_fstat = os.fstat
            def grow_after_stat(fd):
                initial = real_fstat(fd)
                with path.open('ab') as output:
                    output.write(b' ' * (MAX_DOCUMENT_BYTES + 1))
                return initial
            with patch('editor3d_contracts.os.fstat', side_effect=grow_after_stat):
                with self.assertRaises(RigError) as raised:
                    load_document(path)
            self.assertIn(raised.exception.report['code'], ('EDITOR3D_LIMIT', 'EDITOR3D_FILE'))

    def test_cli_accepts_normal_document_and_rejects_fifo_without_blocking(self):
        result = subprocess.run([sys.executable, 'scripts/editor3d_contracts.py', '--validate',
            str(EXAMPLES / 'project.json')], cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['native_resources_verified'])
        if not hasattr(os, 'mkfifo'):
            self.skipTest('Named pipes are unavailable on this platform.')
        with tempfile.TemporaryDirectory() as td:
            fifo = Path(td) / 'input.json'; os.mkfifo(fifo)
            result = subprocess.run([sys.executable, 'scripts/editor3d_contracts.py', '--validate',
                str(fifo)], cwd=ROOT, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            diagnostic = json.loads(result.stdout)
            self.assertTrue(diagnostic['code'].startswith('EDITOR3D_'))
            self.assertNotIn('Traceback', result.stderr)

    def test_timestamp_validation_requires_no_optional_formatter(self):
        code = """import importlib.abc,json,sys
sys.path.insert(0, 'scripts')
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] == 'rfc3339_validator':
   raise ImportError('Optional formatter intentionally absent')
sys.meta_path.insert(0,Block())
from editor3d_contracts import validate_contract
from rig_errors import RigError
for name,fields in [('project',('created_at','updated_at')),('revision',('created_at',))]:
 path='examples/editor3d-contracts/'+name+'.json'
 original=json.load(open(path))
 validate_contract(original)
 for field in fields:
  for timestamp in ('invalid','2026-10-25','2026-10-25T00:00:00','2026-13-25T00:00:00Z',
                    '2026-10-25T25:00:00Z','2026-10-25T00:00:00+24:00',
                    '2026-10-25T00:00:00+01:99','2026-10-25T00:00:00+00:60'):
   value=json.load(open(path));value[field]=timestamp
   try: validate_contract(value)
   except RigError as error:
    assert error.report['code'].startswith('EDITOR3D_'),error.report
   else: raise AssertionError((name,field,timestamp))
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
            text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_schema_and_admission_import_without_optional_graphics_dependencies(self):
        code = """import importlib.abc,sys
sys.path.insert(0, 'scripts')
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in ('numpy','bpy','pygltflib','playwright'):
   raise ImportError('Forbidden optional import: '+fullname)
sys.meta_path.insert(0,Block())
import editor3d_contracts,editor3d_adapters
assert len(editor3d_contracts.schemas()['editor3d.schema.json']['oneOf']) == 11
assert editor3d_adapters.frame_time(720,{'numerator':24,'denominator':1}) == 30
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
