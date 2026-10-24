"""Scene3D schema, semantic, exact-time and real-file closure regression tests.

No NumPy import: these also exercise the optional-dependency boundary in 2D CI.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from rig_errors import RigError, decode_json
from scene3d_contracts import (canonical_bytes, definitions, document_hash, frame_time,
    load_project, portable_path, read_document, schemas, validate_document)
from scene3d_example import build_example, node

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples' / 'scene3d-contracts'


def fixture(name):
    return json.loads((EXAMPLE / (name + '.json')).read_text())


class SceneContractTests(unittest.TestCase):
    def test_hash_bound_example_files_disable_checkout_newline_conversion(self):
        result = subprocess.run(['git', 'check-attr', 'text', '--',
                                 'examples/scene3d-contracts/project.json',
                                 'examples/scene3d-contracts/triangle.obj'],
                                cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(len(result.stdout.splitlines()), 2)
        self.assertTrue(all(line.endswith(': text: unset') for line in result.stdout.splitlines()), result.stdout)

    def test_every_generated_schema_is_current_and_valid(self):
        self.assertEqual(len(schemas()), 8)
        for name, schema in schemas().items():
            with self.subTest(name=name):
                Draft202012Validator.check_schema(schema)
                self.assertEqual(json.loads((ROOT / 'profiles' / name).read_text()), schema)

    def test_all_document_kinds_roundtrip_structural_runtime_parity(self):
        for path in EXAMPLE.glob('*.json'):
            with self.subTest(path=path.name):
                value = read_document(path)
                schema = schemas()[value['kind'] + '.schema.json']
                self.assertFalse(list(Draft202012Validator(schema).iter_errors(value)))
                self.assertEqual(decode_json(canonical_bytes(value)), value)
        self.assertIs(validate_document(node('example'), 'node')['visible'], True)

    def test_closed_schemas_reject_unknown_fields_at_every_depth(self):
        for change in ('top', 'node', 'transform', 'reference', 'world'):
            doc = fixture('example-scene')
            target = {'top': doc, 'node': doc['nodes'][0],
                      'transform': doc['nodes'][0]['transform'], 'reference': doc['dependencies'][0],
                      'world': doc['world']}[change]
            target['surprise'] = True
            with self.subTest(change=change), self.assertRaises(RigError):
                validate_document(doc)

    def test_versions_and_types_are_strict(self):
        for field, value in [('schema_version', True), ('schema_version', 2),
                             ('version', '01.0.0'), ('kind', 'hatch-3d-rig'), ('id', 'a/b')]:
            doc = fixture('example-scene')
            doc[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RigError):
                validate_document(doc)

    def test_semantic_errors_are_distinct_from_structural_schema(self):
        doc = fixture('example-scene')
        doc['nodes'][0]['transform']['rotation'] = [0, 0, 0, 0]
        self.assertFalse(list(Draft202012Validator(schemas()['scene3d-scene.schema.json']).iter_errors(doc)))
        with self.assertRaisesRegex(RigError, 'normalized'):
            validate_document(doc)

    def test_invalid_quaternions_scales_and_nonfinite_values(self):
        for field, values in [('rotation', [[0, 0, 0, 0], [0, 0, 0, 0.5], [0, 0, 0, 2]]),
                              ('scale', [[0, 1, 1], [-1, 1, 1], [1e-9, 1, 1], [1e-7, 1e6, 1]])]:
            for value in values:
                doc = fixture('example-scene')
                doc['nodes'][0]['transform'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(RigError):
                    validate_document(doc)
        for bad in (float('nan'), float('inf'), -float('inf')):
            doc = fixture('example-scene')
            doc['nodes'][0]['transform']['translation'][0] = bad
            with self.subTest(bad=bad), self.assertRaises(RigError):
                validate_document(doc)

    def test_graph_rejects_duplicate_dangling_parent_cycles_and_roots(self):
        for mode in ('duplicate', 'dangling', 'cycle', 'bad-roots', 'second-root'):
            doc = fixture('example-scene')
            if mode == 'duplicate':
                doc['nodes'].append(deepcopy(doc['nodes'][0]))
            elif mode == 'dangling':
                doc['nodes'][1]['parent'] = 'missing'
            elif mode == 'cycle':
                doc['nodes'][1]['parent'] = 'camera'
                doc['nodes'][2]['parent'] = 'mesh'
            elif mode == 'bad-roots':
                doc['roots'] = ['mesh']
            else:
                doc['nodes'][1]['parent'] = None
            with self.subTest(mode=mode), self.assertRaises(RigError):
                validate_document(doc)

    def test_bounded_deep_graph_and_forward_parent_declaration(self):
        doc = fixture('example-scene')
        doc['nodes'].reverse()
        validate_document(doc)
        doc['nodes'] = [node('n' + str(i), parent='n' + str(i-1) if i else None) for i in range(129)]
        doc['roots'], doc['cameras'] = ['n0'], []
        with self.assertRaisesRegex(RigError, '128'):
            validate_document(doc)

    def test_collections_camera_and_light_inventories(self):
        for mode in ('collection', 'layer', 'camera', 'light', 'camera-far', 'camera-fov'):
            doc = fixture('example-scene')
            if mode == 'collection':
                doc['nodes'][0]['collections'] = ['unknown']
            elif mode == 'layer':
                doc['nodes'][0]['layers'] = ['unknown']
            elif mode == 'camera':
                doc['cameras'] = ['root']
            elif mode == 'light':
                doc['lights'] = ['mesh']
            elif mode == 'camera-far':
                doc['nodes'][2]['camera']['far'] = 0.01
            else:
                doc['nodes'][2]['camera']['vertical_fov'] = 4
            with self.subTest(mode=mode), self.assertRaises(RigError):
                validate_document(doc)

    def test_node_owned_fields_and_explicit_billboard_marker(self):
        for mode in ('missing-camera', 'camera-on-mesh', 'unmarked-billboard', 'prefab-on-group'):
            doc = fixture('example-scene')
            if mode == 'missing-camera':
                del doc['nodes'][2]['camera']
            elif mode == 'camera-on-mesh':
                doc['nodes'][1]['camera'] = doc['nodes'][2]['camera']
            elif mode == 'unmarked-billboard':
                doc['nodes'][1]['type'] = 'billboard_2d'
                doc['nodes'][1]['billboard'] = False
            else:
                doc['nodes'][0]['prefab'] = fixture('project')['prefabs'][0]
            with self.subTest(mode=mode), self.assertRaises(RigError):
                validate_document(doc)

    def test_constraints_and_attachments_cannot_add_evaluation_cycles(self):
        doc = fixture('example-scene')
        doc['nodes'][0]['constraints'] = [dict(id='follow', type='copy-transform', target='mesh', influence=1)]
        with self.assertRaisesRegex(RigError, 'Cyclic'):
            validate_document(doc)
        doc = fixture('example-scene')
        doc['nodes'][1]['attachment'] = dict(owner='camera', socket='root',
                                            offset=deepcopy(doc['nodes'][0]['transform']))
        with self.assertRaisesRegex(RigError, 'socket'):
            validate_document(doc)

    def test_duplicate_material_slots_are_rejected(self):
        doc = fixture('example-scene')
        r = deepcopy(doc['dependencies'][0])
        r['kind'] = 'material'
        doc['nodes'][1]['material_overrides'] = [dict(slot='surface', material=r)] * 2
        with self.assertRaisesRegex(RigError, 'Duplicate material'):
            validate_document(doc)

    def test_bounds_and_prefab_overrides_are_semantic(self):
        doc = fixture('triangle')
        doc['bounds']['minimum'][0] = 2
        with self.assertRaisesRegex(RigError, 'Inverted bounds'):
            validate_document(doc)
        doc = fixture('example-prefab')
        doc['overrides'] = [dict(node='missing', visible=False)]
        with self.assertRaisesRegex(RigError, 'Dangling prefab'):
            validate_document(doc)

    def test_fixture_provenance_never_becomes_production(self):
        doc = fixture('triangle')
        doc['provenance']['fixture'] = False
        with self.assertRaisesRegex(RigError, 'fixture'):
            validate_document(doc)
        doc = fixture('project')
        doc['status'] = 'production'
        with self.assertRaisesRegex(RigError, 'fixture'):
            validate_document(doc)

    def test_exact_rational_frame_time_and_terminal_boundary(self):
        self.assertEqual(frame_time(720, dict(numerator=24, denominator=1)), Fraction(30))
        self.assertEqual(frame_time(24000, dict(numerator=24000, denominator=1001)), Fraction(1001))
        for bad in [dict(numerator=0, denominator=1), dict(numerator=True, denominator=1),
                    dict(numerator=48, denominator=2), dict(numerator=241, denominator=1)]:
            with self.subTest(bad=bad), self.assertRaises(RigError):
                frame_time(1, bad)
        with self.assertRaises(RigError):
            frame_time(True, dict(numerator=24, denominator=1))

    def test_track_timing_interpolation_and_value_dimensions(self):
        for mode in ('duplicate', 'reverse', 'out-of-range', 'bad-fps', 'quaternion',
                     'dimension', 'boolean', 'scale', 'interpolation'):
            doc = fixture('example-animation')
            track = doc['tracks'][0]
            if mode == 'duplicate':
                track['keys'][1]['frame'] = 0
            elif mode == 'reverse':
                track['keys'].reverse()
            elif mode == 'out-of-range':
                track['keys'][1]['frame'] = 25
            elif mode == 'bad-fps':
                doc['fps'] = dict(numerator=48, denominator=2)
            elif mode == 'quaternion':
                track['property'], track['interpolation'] = 'rotation', 'slerp'
                track['keys'][0]['value'] = [0, 0, 0, 0]
            elif mode == 'dimension':
                track['keys'][0]['value'] = [0, 1, 2, 3]
            elif mode == 'boolean':
                track['property'], track['interpolation'] = 'visible', 'step'
            elif mode == 'scale':
                track['property'] = 'scale'
                track['keys'][0]['value'] = [-1, 1, 1]
            else:
                track['interpolation'] = 'slerp'
            with self.subTest(mode=mode), self.assertRaises(RigError):
                validate_document(doc)
        doc = fixture('example-animation')
        self.assertEqual(validate_document(doc)['tracks'][0]['keys'][-1]['frame'], 24)

    def test_clip_and_temporal_ranges_are_nonempty_half_open(self):
        doc = fixture('example-shot')
        clip = fixture('project')['animations'][0]
        doc['bindings'] = [dict(target='mesh', clip=clip, start_frame=3, end_frame=3, speed=1, loop=False)]
        with self.assertRaisesRegex(RigError, 'clip interval'):
            validate_document(doc)
        doc = fixture('example-render')
        doc['start_frame'] = doc['end_frame']
        with self.assertRaisesRegex(RigError, 'nonempty'):
            validate_document(doc)

    def test_exact_time_cannot_raise_unstructured_errors_on_integral_floats(self):
        for field in ('fps', 'duration', 'key'):
            doc = fixture('example-animation')
            if field == 'fps':
                doc['fps']['numerator'] = 24.0
            elif field == 'duration':
                doc['duration_frames'] = 24.0
            else:
                doc['tracks'][0]['keys'][0]['frame'] = 0.0
            with self.subTest(field=field), self.assertRaisesRegex(RigError, 'JSON integer'):
                validate_document(doc)

    def test_timestamps_and_evidence_free_review_status_rejected(self):
        for value in ('yesterday', '2026-10-23', '2026-10-22T00:00:00Z'):
            doc = fixture('project')
            doc['modified_at'] = value
            with self.subTest(value=value), self.assertRaises(RigError):
                validate_document(doc)
        doc = fixture('triangle')
        doc['review']['status'] = 'approved'
        with self.assertRaisesRegex(RigError, 'receipts'):
            validate_document(doc)

    def test_standalone_node_semantics_and_node_bindings_are_not_bypassed(self):
        for mode in ('camera', 'binding-interval', 'binding-target'):
            doc = fixture('example-scene')['nodes'][2 if mode == 'camera' else 1]
            if mode == 'camera':
                doc['camera']['far'] = 0.01
            else:
                doc['animation_bindings'] = [dict(target='mesh', clip=fixture('project')['animations'][0],
                                                start_frame=9, end_frame=1, speed=1, loop=False)]
                if mode == 'binding-target':
                    doc['animation_bindings'][0].update(target='missing-node', start_frame=0, end_frame=12)
            with self.subTest(mode=mode), self.assertRaises(RigError):
                validate_document(doc, 'node')

    def test_reference_ownership_types_are_strict(self):
        for mode in ('binding', 'skeleton', 'material'):
            doc = fixture('example-shot' if mode == 'binding' else 'triangle' if mode == 'skeleton' else 'example-scene')
            wrong = fixture('project')['assets'][0]
            if mode == 'binding':
                doc['bindings'] = [dict(target='mesh', clip=wrong, start_frame=0, end_frame=12, speed=1, loop=False)]
            elif mode == 'skeleton':
                doc['skeleton'] = fixture('project')['animations'][0]
            else:
                doc['nodes'][1]['material_overrides'] = [dict(slot='surface', material=wrong)]
            with self.subTest(mode=mode), self.assertRaises(RigError):
                validate_document(doc)

    def test_shot_camera_overrides_and_world_track_namespace(self):
        for mode in ('near-far', 'fov', 'world-target'):
            doc = fixture('example-shot')
            if mode == 'world-target':
                doc['tracks'] = [dict(target='mesh', property='world_exposure', interpolation='linear',
                                     keys=[dict(frame=0, value=0)])]
            else:
                doc['camera_overrides']['camera'] = fixture('example-scene')['nodes'][2]['camera']
                doc['camera_overrides']['camera']['far' if mode == 'near-far' else 'vertical_fov'] = 0.01 if mode == 'near-far' else 4
            with self.subTest(mode=mode), self.assertRaises(RigError):
                validate_document(doc)

    def test_full_string_schema_tokens_and_lexical_portable_paths(self):
        for name, bad in [('id', 'thing\n'), ('version', '1.0.0\n')]:
            doc = fixture('example-scene')
            doc[name] = bad
            self.assertTrue(list(Draft202012Validator(schemas()['scene3d-scene.schema.json']).iter_errors(doc)))
            with self.assertRaises(RigError):
                validate_document(doc)
        for bad in ('../x', 'a//b', 'CON.txt', 'lPt9.obj', 'COM¹.obj', 'LPT²', 'CONIN$', 'CONOUT$.txt', ' leading', 'a/../b', '/x', 'a\\b', 'a.', 'a '):
            pointer = fixture('project')['assets'][0]
            pointer['path'] = bad
            self.assertTrue(list(Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/reference'}).iter_errors(pointer)), bad)
        pointer = fixture('project')['assets'][0]
        pointer['sha256'] += '\n'
        self.assertTrue(list(Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/reference'}).iter_errors(pointer)))
        pointer = fixture('project')['assets'][0]
        pointer.update(id='Robot.Main:1', kind='hatch-3d-rig')
        self.assertFalse(list(Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/reference'}).iter_errors(pointer)))

    def test_material_track_values_require_hash_bound_dependencies(self):
        animation = fixture('example-animation')
        animation['tracks'] = [dict(target='mesh', property='material', interpolation='step',
                                    keys=[dict(frame=0, value='unbound@1.0.0')])]
        with self.assertRaisesRegex(RigError, 'hash-bound material'):
            validate_document(animation)

    def test_utf8_scalar_strings_are_required_for_reopenable_canonical_documents(self):
        for bad in ('\ud800', '\udfff', 'note\ud800tail'):
            doc = fixture('project')
            doc['metadata']['note'] = bad
            with self.subTest(bad=repr(bad)), self.assertRaisesRegex(RigError, 'Unicode scalar'):
                validate_document(doc)
        doc = fixture('project')
        doc['metadata']['note'] = 'é😀'
        self.assertEqual(decode_json(canonical_bytes(doc)), doc)

    def test_free_form_string_values_have_explicit_bounded_lengths(self):
        for kind, field in (('project', 'metadata'), ('example-prefab', 'parameters')):
            doc = fixture(kind)
            doc[field]['oversized'] = 'x' * 4097
            with self.subTest(kind=kind), self.assertRaises(RigError):
                validate_document(doc)

    def test_streamed_canonical_encoding_enforces_exact_utf8_manifest_byte_cap(self):
        doc = fixture('project')
        doc['metadata']['note'] = 'é😀'
        expected = canonical_bytes(doc)
        with patch('scene3d_contracts.MAX_DOCUMENT_BYTES', len(expected)):
            self.assertEqual(canonical_bytes(doc), expected)
        with patch('scene3d_contracts.MAX_DOCUMENT_BYTES', len(expected)-1):
            with self.assertRaisesRegex(RigError, 'manifest exceeds'):
                canonical_bytes(doc)

    def test_metadata_and_parameter_names_do_not_impersonate_transform_schemas(self):
        doc = fixture('project')
        doc['metadata'] = dict(minimum=0, maximum=1)
        self.assertEqual(decode_json(canonical_bytes(doc)), doc)
        prefab = fixture('example-prefab')
        prefab['parameters'] = dict(translation=0, rotation=1, scale=2)
        self.assertEqual(decode_json(canonical_bytes(prefab)), prefab)

    def test_portable_path_entry_rejects_surrogates_even_outside_a_document(self):
        for path in ('\udcff.json', 'x/\ud800.json'):
            with self.subTest(path=repr(path)), self.assertRaisesRegex(RigError, 'Unicode scalar'):
                portable_path(path)

    def test_canonical_serialization_is_stable_without_mutation(self):
        doc = fixture('example-scene')
        original = deepcopy(doc)
        reordered = dict(reversed(list(doc.items())))
        self.assertEqual(document_hash(doc), document_hash(reordered))
        self.assertEqual(doc, original)

    def test_duplicate_keys_depth_and_nonfinite_json_rejected(self):
        for payload in ['{"schema_version":1,"schema_version":2}', '{"v":NaN}', '[' * 70 + '0' + ']' * 70]:
            with self.subTest(payload=payload[:40]), self.assertRaises(RigError):
                decode_json(payload)

    def test_nonportable_paths_rejected(self):
        for path in ('../x', '/x', 'a//b', 'a/./b', 'a/../b', 'C:/x', 'a\\b',
                     'file:uri', 'a\x00b', 'a/NUL.txt', 'a/COM1', 'a/COM¹.obj', 'LPT³.txt', 'CONIN$', 'CONOUT$.txt', 'a/ leading', 'a/x.', 'a/x ', 'e\u0301.json'):
            with self.subTest(path=repr(path)), self.assertRaises(RigError):
                portable_path(path)
        self.assertEqual(portable_path('assets/épreuve.obj'), 'assets/épreuve.obj')


class SceneClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        shutil.copytree(EXAMPLE, self.root)

    def rehash(self):
        # Rebind genuine changes bottom-up, preserving exact raw-byte hashes.
        documents = {p.name: json.loads(p.read_text()) for p in self.root.glob('*.json')}
        from scene3d_contracts import all_refs
        done = {}
        def visit(name):
            if name in done:
                return done[name]
            if name in documents:
                doc = documents[name]
                for r in all_refs(doc):
                    r['sha256'] = visit(r['path'])
                payload = canonical_bytes(doc)
                (self.root / name).write_bytes(payload)
            else:
                payload = (self.root / name).read_bytes()
            done[name] = hashlib.sha256(payload).hexdigest()
            return done[name]
        visit('project.json')

    def write(self, name, doc):
        (self.root / name).write_text(json.dumps(doc))
        self.rehash()

    def test_checked_in_real_geometry_package_hashes_and_fresh_process(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        bundle = load_project(self.root)
        self.assertEqual(len(bundle['documents']), 7)
        self.assertEqual(len(bundle['inventory']), 8)
        self.assertEqual(bundle['status'], 'hash-and-contract-verified')
        self.assertIn(b'f 1 2 3', (self.root / 'triangle.obj').read_bytes())
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/scene3d_contracts.py'), str(self.root)],
                                  capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['files'], 8)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()})

    def test_example_builder_produces_same_validated_documents(self):
        generated = Path(self.temp.name) / 'generated'
        result = build_example(generated)
        self.assertEqual(result['inventory'], load_project(EXAMPLE)['inventory'])
        with self.assertRaises(FileExistsError):
            build_example(generated)

    def test_changed_and_missing_source_bytes_fail_closed(self):
        (self.root / 'triangle.obj').write_bytes(b'different original')
        with self.assertRaisesRegex(RigError, 'hash mismatch'):
            load_project(self.root)
        (self.root / 'triangle.obj').unlink()
        with self.assertRaisesRegex(RigError, 'Missing regular'):
            load_project(self.root)

    def test_directory_and_symlink_dependency_rejected(self):
        original = (self.root / 'triangle.obj').read_bytes()
        (self.root / 'triangle.obj').unlink()
        (self.root / 'triangle.obj').mkdir()
        with self.assertRaisesRegex(RigError, 'Missing regular'):
            load_project(self.root)
        (self.root / 'triangle.obj').rmdir()
        outside = Path(self.temp.name) / 'outside.obj'
        outside.write_bytes(original)
        try:
            (self.root / 'triangle.obj').symlink_to(outside)
        except (OSError, NotImplementedError):
            # Windows can lack symlink privileges; non-regular input covered above.
            return
        with self.assertRaisesRegex(RigError, 'symlink'):
            load_project(self.root)

    def test_wrong_reference_identity_cannot_hide_behind_correct_file_hash(self):
        project = fixture('project')
        project['assets'][0]['id'] = 'wrong-id'
        # No content changed, only reference identity; correct raw hash stays valid.
        (self.root / 'project.json').write_bytes(canonical_bytes(project))
        with self.assertRaisesRegex(RigError, 'identity/version'):
            load_project(self.root)

    def test_raw_reference_first_cannot_bypass_manifest_identity_validation(self):
        project = fixture('project')
        # Catalog is processed before assets; malicious inputs cannot cause an
        # already hash-verified raw file to skip its later typed validation.
        project['asset_catalog'] = deepcopy(project['assets'][0])
        project['asset_catalog']['kind'] = 'catalog'
        project['assets'][0]['id'] = 'wrong-id'
        (self.root / 'project.json').write_bytes(canonical_bytes(project))
        with self.assertRaisesRegex(RigError, 'identity/version'):
            load_project(self.root)

    def test_manifest_lock_coverage_required(self):
        project = fixture('project')
        project['dependency_lock'] = [r for r in project['dependency_lock'] if r['kind'] != 'scene3d-asset']
        (self.root / 'project.json').write_bytes(canonical_bytes(project))
        with self.assertRaisesRegex(RigError, 'lock'):
            load_project(self.root)

    def test_missing_shot_camera_and_node_are_not_admitted(self):
        shot = fixture('example-shot')
        shot['camera'] = 'missing-camera'
        self.write('example-shot.json', shot)
        with self.assertRaisesRegex(RigError, 'camera'):
            load_project(self.root)
        shot['camera'] = 'camera'
        shot['tracks'] = [dict(target='missing-node', property='visible', interpolation='step',
                              keys=[dict(frame=0, value=True)])]
        self.write('example-shot.json', shot)
        with self.assertRaisesRegex(RigError, 'missing node'):
            load_project(self.root)

    def test_render_cannot_exceed_actual_shot_duration(self):
        render = fixture('example-render')
        render['end_frame'] = 25
        self.write('example-render.json', render)
        with self.assertRaisesRegex(RigError, 'range exceeds'):
            load_project(self.root)

    def test_resource_byte_count_compared_with_actual_content(self):
        asset = fixture('triangle')
        asset['resources']['bytes'] += 1
        self.write('triangle.json', asset)
        with self.assertRaisesRegex(RigError, 'byte count'):
            load_project(self.root)

    def test_project_enforces_file_node_frame_and_byte_budgets(self):
        for field, value in [('max_files', 1), ('max_nodes', 2), ('max_frames', 23), ('max_bytes', 1)]:
            project = fixture('project')
            project['budgets'][field] = value
            (self.root / 'project.json').write_bytes(canonical_bytes(project))
            with self.subTest(field=field), self.assertRaisesRegex(RigError, 'budget'):
                load_project(self.root)

    def test_case_aliases_and_conflicting_hashes_are_rejected(self):
        for mode in ('alias', 'hash'):
            project = fixture('project')
            project['validation'] = [dict(path='Triangle.obj' if mode == 'alias' else 'triangle.obj',
                                          sha256=fixture('triangle')['content']['sha256'] if mode == 'alias' else '0'*64)]
            (self.root / 'project.json').write_bytes(canonical_bytes(project))
            with self.subTest(mode=mode), self.assertRaisesRegex(RigError, 'alias|Conflicting'):
                load_project(self.root)

    def test_asset_kind_checked_against_actual_geometry_node_manifest(self):
        asset = fixture('triangle')
        asset['asset_type'] = 'material'
        self.write('triangle.json', asset)
        with self.assertRaisesRegex(RigError, 'non-geometric'):
            load_project(self.root)

    def test_legacy_project_bridge_uses_actual_v1_kind_and_keeps_source_bytes(self):
        from production_fixture import build_fixture_project
        from production_contracts import load_project as load_legacy
        legacy_root = self.root / 'legacy'
        build_fixture_project(legacy_root)
        payload = (legacy_root / 'project.json').read_bytes()
        legacy = json.loads(payload)
        pointer = dict(id=legacy['id'], version=legacy['version'], kind='project',
                       path='legacy/project.json', sha256=hashlib.sha256(payload).hexdigest())
        project = fixture('project')
        project['linked_2d_project'] = pointer
        project['dependency_lock'].append(pointer)
        (self.root / 'project.json').write_bytes(canonical_bytes(project))
        self.assertEqual(load_project(self.root)['project']['linked_2d_project'], pointer)
        self.assertEqual((legacy_root / 'project.json').read_bytes(), payload)
        self.assertEqual(load_legacy(legacy_root)['project']['kind'], 'project')
        project['linked_2d_project']['id'] = 'incorrect-legacy-id'
        (self.root / 'project.json').write_bytes(canonical_bytes(project))
        with self.assertRaisesRegex(RigError, 'identity/version'):
            load_project(self.root)

    def test_legacy_pointer_cannot_reuse_a_preloaded_scene_manifest(self):
        project = fixture('project')
        pointer = deepcopy(project['scenes'][0])
        pointer['kind'] = 'project'
        project['linked_2d_project'] = pointer
        project['dependency_lock'].append(pointer)
        (self.root / 'project.json').write_bytes(canonical_bytes(project))
        with self.assertRaises(RigError):
            load_project(self.root)

    def test_cli_failure_is_structured_nonzero_and_nonmutating(self):
        (self.root / 'triangle.obj').unlink()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/scene3d_contracts.py'), str(self.root)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stderr)['ok'])
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.root / 'triangle.obj').exists())

    def test_scene_shot_inventory_and_project_character_type_are_resolved(self):
        scene = fixture('example-scene')
        scene['shots'].append('dangling-shot')
        self.write('example-scene.json', scene)
        with self.assertRaisesRegex(RigError, 'shot inventory'):
            load_project(self.root)
        self.write('example-scene.json', fixture('example-scene'))
        project = json.loads((self.root / 'project.json').read_text())
        project['characters'] = deepcopy(project['assets'])
        self.write('project.json', project)
        with self.assertRaisesRegex(RigError, 'character asset'):
            load_project(self.root)

    def test_temporal_constraint_inner_target_and_active_dependency_cycles(self):
        for mode in ('dangling', 'cycle'):
            shot = fixture('example-shot')
            shot['temporal_constraints'] = [dict(id='follow', target='root', start_frame=0, end_frame=12,
                constraint=dict(id='follow-inner', type='copy-transform',
                                target='missing' if mode == 'dangling' else 'mesh', influence=1))]
            self.write('example-shot.json', shot)
            with self.subTest(mode=mode), self.assertRaisesRegex(RigError, 'missing node|Cyclic'):
                load_project(self.root)
        # Reversed constraints in disjoint half-open intervals do not form a
        # simultaneously active cycle; admission must retain this valid case.
        shot = fixture('example-shot')
        shot['temporal_constraints'] = [dict(id='first', target='mesh', start_frame=0, end_frame=12,
            constraint=dict(id='a', type='copy-transform', target='camera', influence=1)),
            dict(id='second', target='camera', start_frame=12, end_frame=24,
            constraint=dict(id='b', type='copy-transform', target='mesh', influence=1))]
        self.write('example-shot.json', shot)
        self.assertEqual(load_project(self.root)['status'], 'hash-and-contract-verified')

    def test_node_bound_animation_requires_absolute_targets_in_its_subtree(self):
        scene = fixture('example-scene')
        scene['nodes'][1]['animation_bindings'] = [dict(target='mesh', clip=fixture('project')['animations'][0],
                                                       start_frame=0, end_frame=24, speed=1, loop=False)]
        self.write('example-scene.json', scene)
        self.assertEqual(load_project(self.root)['status'], 'hash-and-contract-verified')
        for target in ('missing-node', 'camera'):
            animation = fixture('example-animation')
            animation['tracks'][0]['target'] = target
            self.write('example-animation.json', animation)
            with self.subTest(target=target), self.assertRaisesRegex(RigError, 'missing node|outside its binding subtree'):
                load_project(self.root)

    def test_world_tracks_target_explicit_world_without_needing_a_node(self):
        shot = fixture('example-shot')
        shot['tracks'] = [dict(target='world', property='world_exposure', interpolation='linear',
                               keys=[dict(frame=0, value=0), dict(frame=24, value=1)])]
        self.write('example-shot.json', shot)
        self.assertEqual(load_project(self.root)['status'], 'hash-and-contract-verified')

    def test_prefab_instance_overrides_persist_without_altering_other_instances(self):
        scene = fixture('example-scene')
        prefab = fixture('project')['prefabs'][0]
        for identifier in ('first-instance', 'second-instance'):
            instance = node(identifier, 'prefab_instance', 'root')
            instance['prefab'] = deepcopy(prefab)
            instance['prefab_overrides'] = dict(parameters={}, materials={}, nodes=[])
            scene['nodes'].append(instance)
        scene['nodes'][-2]['prefab_overrides']['nodes'] = [dict(node='prefab-mesh', visible=False)]
        original_prefab = (self.root / 'example-prefab.json').read_bytes()
        self.write('example-scene.json', scene)
        bundle = load_project(self.root)
        loaded = bundle['documents']['example-scene.json']['nodes']
        self.assertFalse(loaded[-2]['prefab_overrides']['nodes'][0]['visible'])
        self.assertEqual(loaded[-1]['prefab_overrides']['nodes'], [])
        self.assertEqual((self.root / 'example-prefab.json').read_bytes(), original_prefab)
        for mode in ('node', 'parameter', 'material'):
            invalid = deepcopy(scene)
            overrides = invalid['nodes'][-1]['prefab_overrides']
            if mode == 'node':
                overrides['nodes'] = [dict(node='unknown', visible=True)]
            elif mode == 'parameter':
                overrides['parameters'] = dict(unknown=True)
            else:
                overrides['materials'] = dict(unknown={**fixture('project')['assets'][0], 'kind': 'material'})
            self.write('example-scene.json', invalid)
            if mode == 'material':
                project = json.loads((self.root / 'project.json').read_text())
                project['dependency_lock'].append(overrides['materials']['unknown'])
                self.write('project.json', project)
            with self.subTest(mode=mode), self.assertRaisesRegex(RigError, 'Unknown prefab'):
                load_project(self.root)

    def test_hash_bound_prefab_depth_budget_is_independent_of_registration_order(self):
        from scene3d_contracts import all_refs
        project = fixture('project')
        project['budgets'].update(max_files=512, max_bytes=8*1024*1024)
        pointers = []
        for index in range(129):
            identifier = 'nested-' + str(index)
            prefab = fixture('example-prefab')
            prefab.update(id=identifier, nodes=[node('nested-root')], roots=['nested-root'], dependencies=[])
            if pointers:
                child = node('child', 'prefab_instance', 'nested-root')
                child['prefab'] = deepcopy(pointers[-1])
                prefab['nodes'].append(child)
            payload = canonical_bytes(prefab)
            path = identifier + '.json'
            (self.root / path).write_bytes(payload)
            pointers.append(dict(id=identifier, version='1.0.0', kind='scene3d-prefab', path=path,
                                 sha256=hashlib.sha256(payload).hexdigest()))
        # Root project adds one dependency node: 127 prefabs + project =128
        # accepted; 128 prefabs + project =129 rejected, in either ordering.
        original = deepcopy(project)
        for length, valid in ((127, True), (128, False)):
            for reverse in (False, True):
                project = deepcopy(original)
                selected = deepcopy(pointers[:length])
                if reverse:
                    selected.reverse()
                project['prefabs'] = selected
                project['dependency_lock'].extend(selected)
                (self.root / 'project.json').write_bytes(canonical_bytes(project))
                with self.subTest(length=length, reverse=reverse):
                    if valid:
                        self.assertEqual(load_project(self.root)['status'], 'hash-and-contract-verified')
                    else:
                        with self.assertRaisesRegex(RigError, '128'):
                            load_project(self.root)

    def test_catalog_and_world_environment_reference_roles_are_enforced(self):
        project = fixture('project')
        project['asset_catalog'] = deepcopy(project['assets'][0])
        with self.assertRaises(RigError):
            validate_document(project)
        scene = fixture('example-scene')
        scene['world']['environment'] = fixture('project')['assets'][0]
        self.write('example-scene.json', scene)
        with self.assertRaisesRegex(RigError, 'environment asset'):
            load_project(self.root)

    def test_shots_cannot_reference_unregistered_project_scenes(self):
        alternate = fixture('example-scene')
        alternate['id'] = 'alternate-scene'
        (self.root / 'alternate-scene.json').write_bytes(canonical_bytes(alternate))
        payload = (self.root / 'alternate-scene.json').read_bytes()
        pointer = dict(id='alternate-scene', version='1.0.0', kind='scene3d-scene',
                       path='alternate-scene.json', sha256=hashlib.sha256(payload).hexdigest())
        shot = fixture('example-shot')
        shot['scene'] = pointer
        scene = fixture('example-scene')
        scene['shots'] = []
        (self.root / 'example-shot.json').write_bytes(canonical_bytes(shot))
        (self.root / 'example-scene.json').write_bytes(canonical_bytes(scene))
        project = fixture('project')
        project['dependency_lock'].append(pointer)
        self.write('project.json', project)
        with self.assertRaisesRegex(RigError, 'not registered'):
            load_project(self.root)

    def test_nested_consumed_clip_bindings_validate_targets_and_track_roles(self):
        # A real manifest chain: shot -> parent clip -> child clip. Every
        # changed manifest is hash-rebound before exercising admission.
        child = fixture('example-animation')
        child['id'] = 'child-animation'
        (self.root / 'child-animation.json').write_bytes(canonical_bytes(child))
        pointer = dict(id=child['id'], version='1.0.0', kind='scene3d-animation', path='child-animation.json',
                       sha256=hashlib.sha256((self.root / 'child-animation.json').read_bytes()).hexdigest())
        parent = fixture('example-animation')
        parent['bindings'] = [dict(target='mesh', clip=pointer, start_frame=0, end_frame=24, speed=1, loop=False)]
        shot = fixture('example-shot')
        shot['bindings'] = [dict(target='root', clip=fixture('project')['animations'][0],
                                 start_frame=0, end_frame=24, speed=1, loop=False)]
        project = fixture('project')
        project['dependency_lock'].append(pointer)
        (self.root / 'example-animation.json').write_bytes(canonical_bytes(parent))
        (self.root / 'example-shot.json').write_bytes(canonical_bytes(shot))
        self.write('project.json', project)
        self.assertEqual(load_project(self.root)['status'], 'hash-and-contract-verified')
        parent['bindings'][0]['target'] = 'missing-node'
        self.write('example-animation.json', parent)
        with self.assertRaisesRegex(RigError, 'Nested binding target'):
            load_project(self.root)
        parent['bindings'][0]['target'] = 'mesh'
        self.write('example-animation.json', parent)
        child['tracks'] = [dict(target='mesh', property='light_intensity', interpolation='linear',
                                keys=[dict(frame=0, value=1)])]
        self.write('child-animation.json', child)
        with self.assertRaisesRegex(RigError, 'light node'):
            load_project(self.root)

    def test_nested_scene3d_project_manifests_are_explicitly_unsupported(self):
        nested = fixture('project')
        nested['id'] = 'nested-project'
        payload = canonical_bytes(nested)
        (self.root / 'nested-project.json').write_bytes(payload)
        project = fixture('project')
        project['dependency_lock'].append(dict(id='nested-project', kind='scene3d-project',
            version='1.0.0', path='nested-project.json', sha256=hashlib.sha256(payload).hexdigest()))
        # A second Scene3D project is not a library asset or scoped dependency;
        # linked legacy 2D projects remain the separately validated bridge.
        with self.assertRaises(RigError):
            canonical_bytes(project)

    def test_windows_junction_escape_and_component_containment_policy(self):
        junction = self.root / 'junction'
        outside = Path(self.temp.name) / 'outside-directory'
        outside.mkdir()
        shutil.copyfile(self.root / 'triangle.obj', outside / 'triangle.obj')
        if os.name == 'nt':
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                                     capture_output=True, text=True)
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        else:
            # Linux has no native Windows junction: exercise both explicit
            # junction policy and resolved-containment fallback as unit checks.
            junction.mkdir()
            shutil.copyfile(outside / 'triangle.obj', junction / 'triangle.obj')
        asset = fixture('triangle')
        for file in (asset['content'], asset['source']):
            file['path'] = 'junction/triangle.obj'
        self.write('triangle.json', asset)
        if os.name == 'nt':
            with self.assertRaisesRegex(RigError, 'junction|outside'):
                load_project(self.root)
        else:
            with patch.object(Path, 'is_junction', lambda p: p.name == 'junction', create=True):
                with self.assertRaisesRegex(RigError, 'junction'):
                    load_project(self.root)
            resolve = Path.resolve
            def resolved(path, *args, **kwargs):
                return outside if path.name == 'junction' else resolve(path, *args, **kwargs)
            with patch.object(Path, 'resolve', resolved):
                with self.assertRaisesRegex(RigError, 'outside'):
                    load_project(self.root)

    def test_annotation_metadata_does_not_create_undeclared_file_dependencies(self):
        project = fixture('project')
        project['metadata'] = dict(path='annotation-only.txt', sha256='a descriptive label',
                                   kind='scene3d-project', id='metadata-id', version='99.0.0')
        self.write('project.json', project)
        bundle = load_project(self.root)
        self.assertEqual(len(bundle['inventory']), 8)
        self.assertNotIn('annotation-only.txt', bundle['inventory'])

    def test_root_manifest_filename_requires_portable_unicode_scalar_spelling(self):
        name = '\udcff.json'
        try:
            (self.root / name).write_bytes((self.root / 'project.json').read_bytes())
        except UnicodeError:
            # Some native filesystems reject the name themselves. Admission
            # still must reject it before consulting the native filesystem.
            pass
        with self.assertRaisesRegex(RigError, 'Unicode scalar'):
            load_project(self.root, name)

    def test_optional_3d_imports_remain_unnecessary_for_loading(self):
        code = '''import sys,importlib.abc
sys.path.insert(0, 'scripts')
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in ('numpy','bpy','pygltflib'): raise ImportError(fullname)
sys.meta_path.insert(0,Block())
from scene3d_contracts import load_project
assert len(load_project('examples/scene3d-contracts')['documents']) == 7
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
