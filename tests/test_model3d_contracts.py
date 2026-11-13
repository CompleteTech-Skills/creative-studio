"""Real generated meshes and adversarial checks for the additive contract boundary."""
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import struct
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from model3d_contracts import COORDINATES, KINDS, LIMITS, schemas, validate_contract
from model3d_validation import validate_document, validate_file
from rig_errors import RigError, decode_json


def payload(root, name, media='application/json'):
    data = (root / name).read_bytes()
    return dict(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), media_type=media)


def provenance():
    return dict(origin='procedural-fixture', fixture=True, description='Synthetic engineering mesh; not production artwork.',
                tools=[dict(name='rig_gltf.export_geometry', version='1.0.0')], evidence=[],
                license=dict(identifier=None, attribution=None, redistribution='unknown', evidence=None))


def mesh_details(model, record):
    reports = {item['id']: item for item in model.report['meshes']}
    return [dict(mesh_id=mesh.id, geometry=record, lod=0, vertices=len(mesh.positions),
                 triangles=len(mesh.triangles), material_slot=mesh.material,
                 normals='NORMAL' in mesh.attributes, tangents='TANGENT' in mesh.attributes,
                 uv_sets=[index for index in (0,1) if 'TEXCOORD_' + str(index) in mesh.attributes],
                 topology=dict(boundary_edges=reports[mesh.id]['boundary_edges'],
                               nonmanifold_edges=reports[mesh.id]['nonmanifold_edges'],
                               degenerate_faces=reports[mesh.id]['degenerate_triangles']),
                 evidence_class='synthetic') for mesh in model.geometry.meshes]


def part_metadata():
    return dict(version='1.0.0', category='general', pivot_m=[0,0,0], attachments=[],
                anatomical_side='unknown', decomposition_components=[], mesh_regions=[],
                visible_evidence=[], inferred_geometry=[], confidence=None, confidence_method=None,
                rig_association=None, review_state='pending', edit_history=[])


def asset_fixture(root, identifier='fixture', *, suffix='.glb'):
    from rig_gltf import export_geometry
    from rig_import import load_character_model
    from test_rig_skinning import test_geometry
    # The importer returns canonical paths (macOS /private/var and Windows
    # expanded short names); derive the inventory from the same existing root.
    root = root.resolve()
    path = root / (identifier + suffix)
    export_geometry(test_geometry(), path, provenance={'fixture': True})
    if suffix == '.gltf':
        content = json.loads(path.read_text())
        for index, buffer in enumerate(content.get('buffers', [])):
            uri = buffer['uri']
            if uri.startswith('data:'):
                name = f'{identifier}-{index}.bin'
                (root / name).write_bytes(base64.b64decode(uri.split(',', 1)[1], validate=True))
                buffer['uri'] = name
        path.write_text(json.dumps(content), encoding='utf-8')
    model = load_character_model(path)
    files = [payload(root, p.relative_to(root).as_posix(),
                     'model/gltf-binary' if p.suffix == '.glb' else
                     'model/gltf+json' if p.suffix == '.gltf' else 'application/octet-stream')
             for p in model.source_files]
    geometry = next(f for f in files if f['path'] == path.name)
    source = dict(schema_version=1, kind='model3d-source', id='original', version='1.0.0',
                  file=geometry, role='geometry', provenance=provenance(), camera=dict(status='unknown'),
                  original_filename=path.name, dimensions_px=None,
                  acquisition=dict(method='unknown', timestamp=None), evidence_class='synthetic',
                  mask=None, annotations=[])
    return dict(schema_version=1, kind='model3d-asset', id=identifier, version='1.0.0', category='character',
                coordinate_system=deepcopy(COORDINATES), sources=[source], files=files, geometry=geometry,
                measurements=dict(vertices=model.geometry.vertex_count, triangles=model.geometry.triangle_count,
                                  bounds=dict(minimum=model.report['bounds'][0], maximum=model.report['bounds'][1])),
                parts=[dict(schema_version=1, kind='model3d-part', id='body', name='Body', parent=None,
                            local_transform=dict(translation=[0, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1]),
                            mesh_ids=[m.id for m in model.geometry.meshes], material_ids=[],
                            source_ids=['original'], uncertainty_ids=[], **part_metadata())],
                materials=[], dependencies=[], uncertainties=[], provenance=provenance(),
                review=dict(status='pending', evidence=[]), limits=deepcopy(LIMITS),
                generation_revision='1.0.0', identity=None, pivot_m=[0,0,0], mesh_details=mesh_details(model, geometry),
                provider_receipts=[], previews=[], quality_reports=[], repair_history=[],
                available_formats=[suffix[1:]], completeness='partial')


def write_document(root, name, document):
    (root / name).write_text(json.dumps(document), encoding='utf-8')
    return payload(root, name)


def request_fixture(sources):
    return dict(schema_version=1, kind='model3d-request', id='request', version='1.0.0', asset_id='fixture',
        category='prop', method='import', description='Import the specified mesh.', negative_constraints=[],
        sources=deepcopy(sources), coordinate_system=deepcopy(COORDINATES), dimensions_m=[1,1,1],
        scale_evidence='estimated', intended_use='editing', formats=['glb'], semantic_parts=['body'],
        allowed_assumptions=[], identity=None, style=None, provider=None, seed=0, limits=deepcopy(LIMITS),
        existing_asset_version=None, source_type='geometry', provenance=provenance(), material_requirements=[],
        execution_budgets=dict(texture_pixels=None, runtime_seconds=None, compute_seconds=None),
        lod_requirements=[], fallback_policy='reject')


class Model3DContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.asset = asset_fixture(self.root)

    def assertInvalid(self, document, code=None, *, payloads=False):
        with self.assertRaises(RigError) as caught:
            validate_document(document, self.root) if payloads else validate_contract(document)
        if code:
            self.assertEqual(caught.exception.report['code'], code)

    def test_generated_schema_parity_and_schema_isolation(self):
        from jsonschema import Draft202012Validator
        schema = schemas()['model3d.schema.json']
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(self.asset)
        self.assertEqual(json.loads((ROOT / 'profiles/model3d.schema.json').read_text()), schema)
        schema['$defs']['asset']['properties'].clear()
        self.assertTrue(schemas()['model3d.schema.json']['$defs']['asset']['properties'])

    def test_real_glb_and_external_gltf_payloads(self):
        for asset in (self.asset, asset_fixture(self.root, 'external', suffix='.gltf')):
            with self.subTest(asset=asset['id']):
                result = validate_document(asset, self.root)
                self.assertTrue(result['contract_valid'])
                self.assertTrue(result['payloads_verified'])
                self.assertTrue(result['geometry_inspected'])
                self.assertGreater(result['geometry'][0]['inspection']['vertices'], 0)
                self.assertEqual(result['visual_approval'], 'not-assessed')
                self.assertEqual(result['provider_execution'], 'not-assessed')

    def test_fixture_and_inspection_support_aliased_workspace_root(self):
        actual = self.root / 'actual-workspace'
        actual.mkdir()
        alias = self.root / 'workspace-alias'
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Host cannot create directory symlinks: ' + str(exc))
        link_target = alias.readlink()
        canonical_root = actual / 'project'
        canonical_root.mkdir()
        aliased_root = alias / 'project'
        for suffix in ('.glb', '.gltf'):
            with self.subTest(suffix=suffix):
                asset = asset_fixture(aliased_root, 'aliased-' + suffix[1:], suffix=suffix)
                report = validate_document(asset, aliased_root)
                self.assertTrue(report['payloads_verified'])
                self.assertTrue(report['geometry_inspected'])
                self.assertEqual(report, validate_document(asset, canonical_root))
                for record in asset['files']:
                    self.assertFalse(Path(record['path']).is_absolute())
                    self.assertEqual((aliased_root / record['path']).read_bytes(),
                                     (canonical_root / record['path']).read_bytes())
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.readlink(), link_target)

    def _replace_geometry(self, document, path):
        record = payload(self.root, path.name, 'model/gltf+json' if path.suffix == '.gltf' else 'model/gltf-binary')
        old = document['geometry']
        document['files'] = [record if item == old else item for item in document['files']]
        document['geometry'] = record
        document['sources'][0]['file'] = record
        for item in document['mesh_details']:
            if item['geometry'] == old:
                item['geometry'] = record
        return document

    def test_snapshot_temporary_directory_with_aliased_parent(self):
        actual = self.root / 'temporary-parent'
        actual.mkdir()
        alias = self.root / 'temporary-alias'
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Host cannot create symlinks: ' + str(exc))
        # Exercise the internal temporary snapshot, not just the input root.
        with mock.patch('tempfile.tempdir', str(alias)):
            report = validate_document(self.asset, self.root)
        self.assertTrue(report['geometry_inspected'])
        self.assertEqual(list(actual.iterdir()), [])

    def test_visual_hull_counts_calibrated_observations_with_other_sources(self):
        from PIL import Image
        Image.new('RGB', (8, 8)).save(self.root / 'view.png')
        source = deepcopy(self.asset['sources'][0])
        source.update(id='front-view', role='front', file=payload(self.root, 'view.png', 'image/png'),
                      original_filename='view.png', dimensions_px=[8, 8])
        source['camera'] = dict(status='supplied', projection='orthographic',
            camera_to_world=[1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1], image_size=[8,8],
            orthographic_size=[1,1], evidence=source['file'])
        second = deepcopy(source)
        second.update(id='right-view', role='right')
        second['camera']['camera_to_world'][3] = 1
        request = request_fixture([source, second] + self.asset['sources'])
        request.update(method='visual-hull', source_type='mixed')
        self.assertTrue(validate_document(request, self.root)['payloads_verified'])
        request['sources'] = [source] + self.asset['sources']
        self.assertInvalid(request, 'MODEL3D_CAMERA')

    def test_source_and_calibration_dimensions_match_decoded_image(self):
        from PIL import Image
        Image.new('RGB', (8, 8)).save(self.root / 'view.png')
        source = deepcopy(self.asset['sources'][0])
        source.update(role='viewpoint', file=payload(self.root, 'view.png', 'image/png'),
                      original_filename='view.png', dimensions_px=[8, 8])
        source['camera'] = dict(status='supplied', projection='orthographic',
            camera_to_world=[1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1], image_size=[32768,32768],
            orthographic_size=[1,1], evidence=source['file'])
        self.assertInvalid(source, 'MODEL3D_CAMERA', payloads=True)
        source['camera']['image_size'] = [8,8]
        source['dimensions_px'] = [9,8]
        self.assertInvalid(source, 'MODEL3D_DIMENSIONS', payloads=True)
        source['dimensions_px'] = [8,8]
        self.assertTrue(validate_document(source, self.root)['payloads_verified'])
        source['acquisition'] = dict(method='imported', timestamp='invented')
        self.assertInvalid(source, 'MODEL3D_ACQUISITION')
        source['acquisition'] = dict(method='imported', timestamp=None)
        self.assertInvalid(source, 'MODEL3D_ACQUISITION')

    def test_malformed_mesh_identity_and_embedded_image_are_structured_cli_errors(self):
        import io
        from PIL import Image
        doc = asset_fixture(self.root, 'adversarial', suffix='.gltf')
        path = self.root / 'adversarial.gltf'
        original = json.loads(path.read_text())
        malformed = deepcopy(original)
        malformed['meshes'][0]['extras']['hatchMeshId'] = 42
        malformed['meshes'][0]['primitives'][0]['attributes'].pop('NORMAL', None)
        malformed['meshes'].append(deepcopy(original['meshes'][0]))
        malformed['meshes'][1].pop('extras', None)
        malformed['nodes'].append(dict(mesh=1))
        malformed['scenes'][0]['nodes'].append(len(malformed['nodes']) - 1)
        image = io.BytesIO()
        Image.new('RGB', (8,8)).save(image, format='PNG')
        image_data = image.getvalue()
        bomb = deepcopy(original)
        bomb['images'] = [dict(uri='data:image/png;base64,' + base64.b64encode(image_data).decode())]
        for content, lowered_threshold, code in ((malformed, None, 'MODEL3D_MESH_ID'), (bomb, 1, 'MODEL3D_MODEL')):
            path.write_text(json.dumps(content), encoding='utf-8')
            current = self._replace_geometry(deepcopy(doc), path)
            write_document(self.root, 'adversarial.json', current)
            # Lower Pillow's threshold for this real embedded PNG; no giant allocation.
            script = ('import sys;sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + ');'
                      + ('from PIL import Image;Image.MAX_IMAGE_PIXELS=1;' if lowered_threshold else '')
                      + 'import model3d;raise SystemExit(model3d.main())')
            result = subprocess.run([sys.executable, '-B', '-c', script, 'validate',
                '--file', str(self.root / 'adversarial.json')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            error = json.loads(result.stderr)
            self.assertFalse(error['ok'])
            self.assertEqual(error['error']['code'], code)
            self.assertNotIn('Traceback', result.stderr)

    def test_native_texture_errors_preserve_model_boundary_and_original_bytes(self):
        doc = asset_fixture(self.root, 'texture-boundary', suffix='.gltf')
        path = self.root / 'texture-boundary.gltf'
        original = json.loads(path.read_text())
        jpeg = (ROOT / 'tests/fixtures/strict-jpeg-valid.jpg').read_bytes()
        for data, unavailable, native, expected in (
                (jpeg[:-100] + b'\xff\xd9', False, 'RIG_TEXTURE_DECODE', 'MODEL3D_MODEL'),
                (jpeg, True, 'RIG_TEXTURE_VERIFIER_UNAVAILABLE', 'MODEL3D_DEPENDENCY')):
            content = deepcopy(original)
            content['images'] = [dict(uri='data:image/jpeg;base64,' + base64.b64encode(data).decode())]
            path.write_text(json.dumps(content), encoding='utf-8')
            current = self._replace_geometry(deepcopy(doc), path)
            document_path = self.root / 'texture-boundary.json'
            write_document(self.root, document_path.name, current)
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in self.root.rglob('*') if p.is_file()}
            script = ('import sys;sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + ');'
                      + ('sys.modules["simplejpeg"]=None;' if unavailable else '')
                      + 'import model3d;raise SystemExit(model3d.main())')
            result = subprocess.run([sys.executable, '-B', '-c', script, 'validate',
                                     '--file', str(document_path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            error = json.loads(result.stderr)['error']
            self.assertEqual(error['code'], expected)
            self.assertEqual(error['observed']['code'], native)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in self.root.rglob('*') if p.is_file()}, before)

    def test_secondary_lod_texture_errors_use_same_model_boundary(self):
        primary = asset_fixture(self.root, 'primary-texture')
        secondary = asset_fixture(self.root, 'secondary-texture', suffix='.gltf')
        path = self.root / 'secondary-texture.gltf'
        original = json.loads(path.read_text())
        jpeg = (ROOT / 'tests/fixtures/strict-jpeg-valid.jpg').read_bytes()
        for data, unavailable, native, expected in (
                (jpeg[:-100] + b'\xff\xd9', False, 'RIG_TEXTURE_DECODE', 'MODEL3D_MODEL'),
                (jpeg, True, 'RIG_TEXTURE_VERIFIER_UNAVAILABLE', 'MODEL3D_DEPENDENCY')):
            content = deepcopy(original)
            content['images'] = [dict(uri='data:image/jpeg;base64,' + base64.b64encode(data).decode())]
            path.write_text(json.dumps(content), encoding='utf-8')
            record = payload(self.root, path.name, 'model/gltf+json')
            doc = deepcopy(primary)
            doc['files'] += [record if f == secondary['geometry'] else f for f in secondary['files']]
            doc['mesh_details'] += [dict(detail, geometry=record, lod=1) for detail in secondary['mesh_details']]
            document_path = self.root / 'secondary-texture.json'
            write_document(self.root, document_path.name, doc)
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in self.root.rglob('*') if p.is_file()}
            script = ('import sys;sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + ');'
                      + ('sys.modules["simplejpeg"]=None;' if unavailable else '')
                      + 'import model3d;raise SystemExit(model3d.main())')
            result = subprocess.run([sys.executable, '-B', '-c', script, 'validate',
                                     '--file', str(document_path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            error = json.loads(result.stderr)['error']
            self.assertEqual(error['code'], expected)
            self.assertEqual(error['observed']['code'], native)
            self.assertEqual(error['asset'], path.name)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in self.root.rglob('*') if p.is_file()}, before)

    def test_eight_influence_skin_is_inspected_without_loss(self):
        import numpy as np
        from rig_gltf import GltfBuilder, _export_geometry
        from rig_import import load_character_model
        from test_rig_skinning import bound_fixture
        rig = bound_fixture()
        skin = rig.weights['strip']
        skin.joints = ['b' + str(i) for i in range(8)]
        skin.indices = np.tile(np.arange(8, dtype=np.uint16), (4, 1))
        skin.weights = np.full((4, 8), 1/8)
        skin.inverse_bind = np.tile(np.eye(4), (8, 1, 1))
        # Two bind joints at their actual rest locations.
        builder = GltfBuilder()
        builder.json['nodes'] = [dict(name=name) for name in skin.joints]
        nodes = _export_geometry(builder, rig.geometry, weights=rig.weights,
                                 bone_nodes={name:i for i, name in enumerate(skin.joints)}, root_node=0)
        builder.json['scenes'] = [dict(nodes=list(range(8)) + list(nodes.values()))]
        path = self.root / 'eight.glb'
        builder.write(path)
        model = load_character_model(path, limits={**__import__('rig_contracts').DEFAULT_LIMITS, 'influences':8})
        doc = deepcopy(self.asset)
        record = payload(self.root, path.name, 'model/gltf-binary')
        doc.update(geometry=record, files=[record])
        doc['sources'][0]['file'] = record
        doc['parts'][0]['mesh_ids'] = [mesh.id for mesh in model.geometry.meshes]
        doc['mesh_details'] = mesh_details(model, record)
        doc['measurements'] = dict(vertices=model.geometry.vertex_count, triangles=model.geometry.triangle_count,
                                  bounds=dict(minimum=model.report['bounds'][0], maximum=model.report['bounds'][1]))
        report = validate_document(doc, self.root)
        self.assertEqual(report['geometry'][0]['skin_bindings'], 1)
        self.assertEqual(model.weights['strip'].indices.shape[1], 8)
        np.testing.assert_allclose(model.weights['strip'].weights.sum(axis=1), 1)

    def test_windows_creation_time_is_not_used_as_a_write_fingerprint(self):
        from types import SimpleNamespace
        from model3d_validation import _file_fingerprint
        first = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=5)
        second = SimpleNamespace(**vars(first))
        second.st_ctime_ns = 6
        with mock.patch('model3d_validation.os.name', 'nt'):
            self.assertEqual(_file_fingerprint(first), _file_fingerprint(second))
            second.st_mtime_ns = 7
            self.assertNotEqual(_file_fingerprint(first), _file_fingerprint(second))
        second.st_mtime_ns = 4
        with mock.patch('model3d_validation.os.name', 'posix'):
            self.assertNotEqual(_file_fingerprint(first), _file_fingerprint(second))

    def test_obj_boundary_and_independent_glb_reader(self):
        from pygltflib import GLTF2
        from rig_import import load_character_model
        independent = GLTF2().load(str(self.root / 'fixture.glb'))
        vertex_count = sum(independent.accessors[p.attributes.POSITION].count
                           for mesh in independent.meshes for p in mesh.primitives)
        self.assertEqual(vertex_count, self.asset['measurements']['vertices'])
        obj = self.root / 'tetra.obj'
        obj.write_text('o tetra\nv 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n'
                       'f 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n', encoding='utf-8')
        model = load_character_model(obj)
        record = payload(self.root, 'tetra.obj', 'model/obj')
        doc = deepcopy(self.asset)
        doc.update(geometry=record, files=[record])
        doc['sources'][0]['file'] = record
        doc['parts'][0]['mesh_ids'] = [mesh.id for mesh in model.geometry.meshes]
        doc['mesh_details'] = mesh_details(model, record)
        doc['measurements'] = dict(vertices=model.geometry.vertex_count, triangles=model.geometry.triangle_count,
                                   bounds=dict(minimum=model.report['bounds'][0], maximum=model.report['bounds'][1]))
        report = validate_document(doc, self.root)
        self.assertTrue(report['geometry_inspected'])
        self.assertEqual(report['geometry'][0]['inspection']['triangles'], 4)

    def test_structural_only_never_claims_verified_payload(self):
        (self.root / 'fixture.glb').unlink()
        report = validate_document(self.asset, structural_only=True)
        self.assertTrue(report['contract_valid'])
        self.assertFalse(report['payloads_verified'])
        self.assertFalse(report['geometry_inspected'])
        self.assertInvalid(self.asset, payloads=True)

    def test_missing_and_same_size_corrupt_payload(self):
        path = self.root / 'fixture.glb'
        original = path.read_bytes()
        path.write_bytes(b'x' + original[1:])
        self.assertInvalid(self.asset, 'MODEL3D_HASH', payloads=True)
        path.write_bytes(original + b'x')
        self.assertInvalid(self.asset, 'MODEL3D_SIZE', payloads=True)
        path.unlink()
        self.assertInvalid(self.asset, payloads=True)

    def test_hash_consistent_fake_mesh_is_rejected(self):
        (self.root / 'fixture.glb').write_bytes(b'not a real glb file')
        record = payload(self.root, 'fixture.glb', 'model/gltf-binary')
        self.asset.update(geometry=record, files=[record])
        self.asset['sources'][0]['file'] = record
        self.assertInvalid(self.asset, payloads=True)

    def test_actual_counts_bounds_and_mesh_bindings(self):
        for mutate in (lambda x: x['measurements'].update(vertices=1),
                       lambda x: x['measurements']['bounds'].update(maximum=[100, 100, 100]),
                       lambda x: x['parts'][0].update(mesh_ids=['imaginary'])):
            doc = deepcopy(self.asset)
            mutate(doc)
            self.assertInvalid(doc, payloads=True)

    def test_unknown_version_field_nonfinite_bool_and_duplicate_json(self):
        for mutate in (lambda x: x.update(schema_version=2), lambda x: x.update(secret=True),
                       lambda x: x.update(version='01.0.0'),
                       lambda x: x['measurements'].update(vertices=True),
                       lambda x: x['parts'][0]['local_transform'].update(translation=[float('nan'), 0, 0])):
            doc = deepcopy(self.asset)
            mutate(doc)
            self.assertInvalid(doc)
        for data in (b'{"kind":"model3d-asset","kind":"model3d-source"}', b'{"a":Infinity}'):
            with self.assertRaises(RigError):
                decode_json(data)

    def test_invalid_quaternion_scale_parent_cycle_and_dangling_references(self):
        for field, value in [('rotation', [0, 0, 0, 0]), ('rotation', [0, 0, 0, 2]),
                             ('scale', [0, 1, 1]), ('scale', [-1, 1, 1]), ('scale', [1e-12, 1, 1])]:
            doc = deepcopy(self.asset)
            doc['parts'][0]['local_transform'][field] = value
            self.assertInvalid(doc)
        for field, value in [('parent', 'missing'), ('parent', 'body'), ('source_ids', ['missing']),
                             ('material_ids', ['missing']), ('uncertainty_ids', ['missing'])]:
            doc = deepcopy(self.asset)
            doc['parts'][0][field] = value
            self.assertInvalid(doc)
        doc = deepcopy(self.asset)
        second = deepcopy(doc['parts'][0])
        second.update(id='second', parent='body', mesh_ids=[])
        doc['parts'][0]['parent'] = 'second'
        doc['parts'].append(second)
        self.assertInvalid(doc, 'MODEL3D_CYCLE')

    def test_path_traversal_aliases_and_conflicting_hash_records(self):
        for path in ('../escape', '/absolute', 'C:/drive', 'a\\b', 'a/../b', 'a//b', 'aux.txt', 'a. ', 'e\u0301.glb'):
            with self.subTest(path=path):
                doc = deepcopy(self.asset)
                doc['provenance']['evidence'] = [dict(doc['geometry'], path=path)]
                self.assertInvalid(doc)
        for record in (dict(self.asset['geometry'], sha256='0'*64),
                       dict(self.asset['geometry'], path='FIXTURE.GLB')):
            doc = deepcopy(self.asset)
            doc['provenance']['evidence'] = [record]
            self.assertInvalid(doc)

    def test_symlink_and_special_file_payload_rejected(self):
        target = self.root / 'fixture.glb'
        moved = self.root / 'actual.glb'
        target.rename(moved)
        try:
            target.symlink_to(moved)
        except OSError as exc:
            self.skipTest('Host cannot create symlinks: ' + str(exc))
        self.assertInvalid(self.asset, payloads=True)

    def test_actual_external_resource_must_be_in_inventory(self):
        doc = asset_fixture(self.root, 'external', suffix='.gltf')
        extra = next(r for r in doc['files'] if r['path'] != doc['geometry']['path'])
        doc['files'].remove(extra)
        # Another legitimate reference makes the bytes available to the snapshot,
        # but it must not conceal an incomplete runtime inventory.
        doc['provenance']['evidence'].append(extra)
        self.assertInvalid(doc, 'MODEL3D_INVENTORY', payloads=True)
        doc['provenance']['evidence'].clear()
        self.assertInvalid(doc, payloads=True)

    def test_recursive_dependency_closure_identity_and_budget(self):
        child = asset_fixture(self.root, 'child')
        child_record = write_document(self.root, 'child.json', child)
        self.asset['dependencies'] = [dict(id='child', version='1.0.0', manifest=child_record)]
        result = validate_document(self.asset, self.root)
        self.assertEqual(result['dependency_asset_count'], 1)
        self.assertEqual(len(result['geometry']), 2)
        self.asset['dependencies'][0]['version'] = '2.0.0'
        self.assertInvalid(self.asset, 'MODEL3D_REFERENCE', payloads=True)
        self.asset['dependencies'][0]['version'] = '1.0.0'
        self.asset['limits']['files'] = 2
        self.assertInvalid(self.asset, 'MODEL3D_BUDGET', payloads=True)

    def test_dependency_cycle_by_immutable_identity(self):
        # The root document is supplied directly, so self-referential byte hashes
        # are unnecessary: an alternate manifest cannot re-enter the active ID.
        alternate = deepcopy(self.asset)
        record = write_document(self.root, 'alternate.json', alternate)
        child = asset_fixture(self.root, 'child')
        child['dependencies'] = [dict(id='fixture', version='1.0.0', manifest=record)]
        child_record = write_document(self.root, 'child.json', child)
        self.asset['dependencies'] = [dict(id='child', version='1.0.0', manifest=child_record)]
        self.assertInvalid(self.asset, 'MODEL3D_CYCLE', payloads=True)

    def test_cached_dependency_depth_and_version_conflict(self):
        leaf = asset_fixture(self.root, 'leaf')
        leaf_record = write_document(self.root, 'leaf.json', leaf)
        child = asset_fixture(self.root, 'child')
        child['dependencies'] = [dict(id='leaf', version='1.0.0', manifest=leaf_record)]
        child_record = write_document(self.root, 'child.json', child)
        middle = asset_fixture(self.root, 'middle')
        middle['dependencies'] = [dict(id='child', version='1.0.0', manifest=child_record)]
        middle_record = write_document(self.root, 'middle.json', middle)
        self.asset['dependencies'] = [dict(id='child', version='1.0.0', manifest=child_record),
                                      dict(id='middle', version='1.0.0', manifest=middle_record)]
        self.asset['limits']['dependency_depth'] = 2
        self.assertInvalid(self.asset, 'MODEL3D_DEPTH', payloads=True)
        self.asset['limits']['dependency_depth'] = 3
        self.assertTrue(validate_document(self.asset, self.root)['geometry_inspected'])
        altered = deepcopy(leaf)
        altered['provenance']['description'] += ' Different manifest with the same immutable version.'
        altered_record = write_document(self.root, 'altered.json', altered)
        middle['dependencies'] = [dict(id='leaf', version='1.0.0', manifest=altered_record)]
        middle_record = write_document(self.root, 'middle.json', middle)
        self.asset['dependencies'][1]['manifest'] = middle_record
        self.assertInvalid(self.asset, 'MODEL3D_VERSION_CONFLICT', payloads=True)

    def test_child_depth_budget_is_not_weakened_by_root(self):
        leaf = asset_fixture(self.root, 'leaf')
        leaf_record = write_document(self.root, 'leaf.json', leaf)
        child = asset_fixture(self.root, 'child')
        child['dependencies'] = [dict(id='leaf', version='1.0.0', manifest=leaf_record)]
        child_record = write_document(self.root, 'child.json', child)
        middle = asset_fixture(self.root, 'middle')
        middle['dependencies'] = [dict(id='child', version='1.0.0', manifest=child_record)]
        middle['limits']['dependency_depth'] = 1
        middle_record = write_document(self.root, 'middle.json', middle)
        self.asset['dependencies'] = [dict(id='middle', version='1.0.0', manifest=middle_record)]
        self.assertInvalid(self.asset, 'MODEL3D_DEPTH', payloads=True)

    def test_fixture_and_review_claim_boundaries(self):
        doc = deepcopy(self.asset)
        doc['provenance'].update(origin='imported', fixture=False)
        self.assertInvalid(doc, 'MODEL3D_PROVENANCE')
        doc = deepcopy(self.asset)
        doc['review']['status'] = 'approved'
        self.assertInvalid(doc, 'MODEL3D_REVIEW')
        for item in [doc['provenance'], doc['sources'][0]['provenance']]:
            item.update(origin='imported', fixture=False)
        doc['review']['status'] = 'pending'
        self.assertInvalid(doc, 'MODEL3D_PROVENANCE', payloads=True)

    def test_budget_limits_and_duplicate_parts(self):
        doc = deepcopy(self.asset)
        doc['limits']['file_bytes'] = 1
        self.assertInvalid(doc, 'MODEL3D_BUDGET')
        doc = deepcopy(self.asset)
        doc['parts'].append(deepcopy(doc['parts'][0]))
        self.assertInvalid(doc, 'MODEL3D_DUPLICATE')

    def test_uncertainty_requires_evidence_not_invented_confidence(self):
        entry = dict(schema_version=1, kind='model3d-uncertainty', id='u', version='1.0.0', target='body',
                     classification='procedurally-inferred', message='Hidden surfaces were authored, not observed.',
                     evidence=['original'], confidence=None, confidence_method=None, status='pending',
                     scope=dict(level='part', target='body', indices=[]), inferred_method=None,
                     missing_viewpoints=[], recommended_actions=[])
        validate_contract(entry)
        entry['confidence'] = .9
        self.assertInvalid(entry, 'MODEL3D_CONFIDENCE')
        entry['confidence_method'] = 'documented numeric test'
        validate_contract(entry)
        entry['evidence'] = []
        self.assertInvalid(entry, 'MODEL3D_CONFIDENCE')

    def test_source_camera_does_not_invent_unknown_calibration(self):
        source = deepcopy(self.asset['sources'][0])
        source['camera']['projection'] = 'orthographic'
        self.assertInvalid(source, 'MODEL3D_CAMERA')
        source['camera'] = dict(status='supplied', projection='orthographic',
            camera_to_world=[1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1], image_size=[32,32],
            orthographic_size=[1,1], evidence=source['file'])
        validate_contract(source)
        source['camera']['camera_to_world'][0] = -1
        self.assertInvalid(source, 'MODEL3D_CAMERA')

    def test_material_color_space_and_unknown_fields(self):
        record = dict(self.asset['geometry'], path='color.png', media_type='image/png')
        material = dict(schema_version=1, kind='model3d-material', id='mat', version='1.0.0', name='Mat',
                        slot=0, base_color=[.5,.5,.5,1], metallic=0, roughness=.5,
                        alpha_mode='OPAQUE', alpha_cutoff=.5, double_sided=False,
                        textures=dict(normal=dict(file=record, texcoord=0, color_space='linear-data')),
                        provenance=provenance(), evidence_class='unknown', texture_dimensions=dict(normal=[8,8]),
                        procedural_parameters=[], projection_evidence=[])
        validate_contract(material)
        material['textures']['normal']['color_space'] = 'srgb'
        self.assertInvalid(material, 'MODEL3D_COLOR_SPACE')

    def test_source_and_request_modality_boundaries(self):
        source = deepcopy(self.asset['sources'][0])
        source['file']['media_type'] = 'image/png'
        self.assertInvalid(source, 'MODEL3D_SOURCE')
        source = deepcopy(self.asset['sources'][0])
        source['role'] = 'front'
        self.assertInvalid(source, 'MODEL3D_SOURCE')
        request = request_fixture(self.asset['sources'])
        request['method'] = 'silhouette'
        self.assertInvalid(request, 'MODEL3D_SOURCE')

    def test_job_and_export_are_claims_not_execution(self):
        request_file = write_document(self.root, 'input.json', request_fixture(self.asset['sources']))
        job = dict(schema_version=1, kind='model3d-job', id='job', version='1.0.0', request=request_file, asset_id='fixture',
                   status='completed', attempts=1, maximum_attempts=2, outputs=[self.asset['geometry']],
                   error=None, dependencies=[], limits=deepcopy(LIMITS))
        result = validate_document(job, self.root)
        self.assertEqual(result['provider_execution'], 'not-assessed')
        self.assertFalse(result['geometry_inspected'])
        job['outputs'] = []
        self.assertInvalid(job, 'MODEL3D_JOB')
        job['status'] = 'failed'
        self.assertInvalid(job, 'MODEL3D_JOB')
        asset_file = write_document(self.root, 'asset.json', self.asset)
        export = dict(schema_version=1, kind='model3d-export', id='export', version='1.0.0',
                      asset=dict(id='fixture', version='1.0.0', manifest=asset_file), format='glb',
                      status='complete', files=[self.asset['geometry']], tools=[dict(name='native', version='1.0.0')],
                      losses=[], independent_inspection=None, limits=deepcopy(LIMITS))
        self.assertTrue(validate_document(export, self.root)['geometry_inspected'])
        export['files'] = []
        self.assertInvalid(export, 'MODEL3D_EXPORT')

    def test_request_schema_and_provider_isolation(self):
        request = request_fixture(self.asset['sources'])
        validate_contract(request)
        request['method'] = 'provider'
        self.assertInvalid(request, 'MODEL3D_PROVIDER')
        request['provider'] = dict(id='unconfigured', model=None, network_authorized=False)
        result = validate_document(request, self.root)
        self.assertEqual(result['provider_execution'], 'not-assessed')
        request['method'], request['provider'] = 'visual-hull', None
        self.assertInvalid(request, 'MODEL3D_CAMERA')

    def test_read_only_cli_and_missing_payload_exit(self):
        write_document(self.root, 'asset.json', self.asset)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        command = [sys.executable, '-B', str(ROOT / 'scripts/model3d.py'), 'validate', '--file', str(self.root / 'asset.json')]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['geometry_inspected'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        (self.root / 'fixture.glb').unlink()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stderr)['ok'])
        self.assertNotIn('Traceback', result.stderr)

    def test_image_format_and_decode_budget(self):
        from PIL import Image
        Image.new('RGB', (8, 8)).save(self.root / 'texture.png')
        source = deepcopy(self.asset['sources'][0])
        source.update(role='texture', file=payload(self.root, 'texture.png', 'image/png'),
                      original_filename='texture.png', dimensions_px=[8, 8])
        self.assertTrue(validate_document(source, self.root)['payloads_verified'])
        source['file']['media_type'] = 'image/jpeg'
        self.assertInvalid(source, 'MODEL3D_FORMAT', payloads=True)
        source['file']['media_type'] = 'image/png'
        self.asset['sources'].append(dict(source, id='texture'))
        self.asset['limits']['memory_bytes'] = 1
        self.assertInvalid(self.asset, 'MODEL3D_BUDGET', payloads=True)
        (self.root / 'texture.png').write_bytes(b'not an image')
        source['file'] = payload(self.root, 'texture.png', 'image/png')
        self.assertInvalid(source, 'MODEL3D_IMAGE', payloads=True)

    def test_job_request_must_be_a_matching_request_contract(self):
        reference = write_document(self.root, 'request.json', {'invented': 'success'})
        job = dict(schema_version=1, kind='model3d-job', id='job', version='1.0.0', request=reference, asset_id='fixture',
                   status='queued', attempts=0, maximum_attempts=1, outputs=[], error=None,
                   dependencies=[], limits=deepcopy(LIMITS))
        self.assertInvalid(job, 'MODEL3D_REFERENCE', payloads=True)
        request = request_fixture(self.asset['sources'])
        request['asset_id'] = 'different'
        job['request'] = write_document(self.root, 'request.json', request)
        self.assertInvalid(job, 'MODEL3D_REFERENCE', payloads=True)

    def test_malformed_model_has_structured_cli_failure(self):
        # Correct GLB header/chunk lengths do not establish valid glTF content.
        data = b'[]  '
        binary = struct.pack('<4sII', b'glTF', 2, 20 + len(data)) + struct.pack('<I4s', len(data), b'JSON') + data
        (self.root / 'fixture.glb').write_bytes(binary)
        record = payload(self.root, 'fixture.glb', 'model/gltf-binary')
        self.asset.update(geometry=record, files=[record])
        self.asset['sources'][0]['file'] = record
        write_document(self.root, 'asset.json', self.asset)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/model3d.py'), 'validate',
                                 '--file', str(self.root / 'asset.json')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stderr)['ok'])
        self.assertNotIn('Traceback', result.stderr)

    def test_snapshot_is_not_mixed_with_later_source_edits(self):
        from rig_import import load_character_model
        path = self.root / 'fixture.glb'
        original = path.read_bytes()
        def inspect_snapshot(frozen, **kwargs):
            self.assertNotEqual(Path(frozen).parent, self.root)
            path.write_bytes(b'x' + original[1:])
            return load_character_model(frozen, **kwargs)
        with mock.patch('rig_import.load_character_model', side_effect=inspect_snapshot):
            report = validate_document(self.asset, self.root)
        self.assertTrue(report['geometry_inspected'])
        self.assertEqual(report['geometry'][0]['geometry_sha256'], hashlib.sha256(original).hexdigest())
        self.assertInvalid(self.asset, 'MODEL3D_HASH', payloads=True)

    def test_concurrent_readers_and_fresh_directory_restoration(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: validate_document(self.asset, self.root), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        with tempfile.TemporaryDirectory() as directory:
            fresh = Path(directory)
            for record in results[0]['files']:
                destination = fresh / record['path']
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((self.root / record['path']).read_bytes())
            write_document(fresh, 'asset.json', self.asset)
            restored = validate_file(fresh / 'asset.json')
            self.assertEqual(restored, results[0])
            (fresh / self.asset['geometry']['path']).unlink()
            with self.assertRaises(RigError):
                validate_file(fresh / 'asset.json')

    def test_additional_lod_geometry_is_actually_decoded_and_budgeted(self):
        second = asset_fixture(self.root, 'alternate-lod')
        doc = deepcopy(self.asset)
        doc['files'].extend(second['files'])
        detail = deepcopy(second['mesh_details'][0])
        detail['lod'] = 1
        doc['mesh_details'].append(detail)
        report = validate_document(doc, self.root)
        observations = report['geometry'][0]['lod_geometry']
        self.assertEqual({item['path'] for item in observations}, {'fixture.glb', 'alternate-lod.glb'})
        self.assertEqual({level for item in observations for level in item['levels']}, {0,1})
        limited = deepcopy(doc)
        limited['limits']['vertices'] = self.asset['measurements']['vertices']
        self.assertInvalid(limited, 'MODEL3D_BUDGET')
        path = self.root / 'alternate-lod.glb'
        path.write_bytes(b'hash-correct malformed LOD')
        record = payload(self.root, path.name, 'model/gltf-binary')
        doc['files'][-1] = record
        doc['mesh_details'][1]['geometry'] = record
        self.assertInvalid(doc, payloads=True)

    def test_required_metadata_is_explicit_and_extensions_remain_hash_bound(self):
        source = deepcopy(self.asset['sources'][0])
        source['acquisition'] = dict(method='imported', timestamp='2026-10-23T12:00:00Z')
        validate_contract(source)
        for field in ('acquisition', 'dimensions_px', 'original_filename', 'evidence_class'):
            broken = deepcopy(source)
            del broken[field]
            self.assertInvalid(broken, 'MODEL3D_SCHEMA')
        request = request_fixture(self.asset['sources'])
        request.update(existing_asset_version='1.0.0', source_type='mixed', fallback_policy='import-only',
            execution_budgets=dict(texture_pixels=4096, runtime_seconds=30, compute_seconds=10),
            lod_requirements=[dict(level=0, triangles=1000, maximum_deviation_m=None)])
        validate_contract(request)
        for key in request['execution_budgets']:
            broken = deepcopy(request)
            broken['execution_budgets'][key] = -1
            self.assertInvalid(broken, 'MODEL3D_SCHEMA')
        request['lod_requirements'].append(deepcopy(request['lod_requirements'][0]))
        self.assertInvalid(request, 'MODEL3D_DUPLICATE')
        doc = deepcopy(self.asset)
        (self.root / 'extension.txt').write_text('Exact extension bytes.', encoding='utf-8')
        doc['extensions'] = [dict(namespace='fixture-extra', payload=payload(self.root, 'extension.txt', 'text/plain'))]
        self.assertTrue(validate_document(doc, self.root)['payloads_verified'])
        doc['extensions'][0]['payload']['sha256'] = '0' * 64
        self.assertInvalid(doc, 'MODEL3D_HASH', payloads=True)

    def test_region_only_parts_and_text_only_request_provenance(self):
        doc = deepcopy(self.asset)
        doc['parts'][0].update(mesh_ids=[], mesh_regions=[dict(mesh_id='strip', face_indices=[0])])
        second = deepcopy(doc['parts'][0])
        second.update(id='other-surface', mesh_regions=[dict(mesh_id='strip', face_indices=[1])])
        doc['parts'].append(second)
        self.assertTrue(validate_document(doc, self.root)['geometry_inspected'])
        doc['parts'][1]['mesh_regions'][0]['face_indices'] = [0]
        self.assertInvalid(doc, 'MODEL3D_REGION')
        request = request_fixture([])
        request.update(method='procedural', source_type='text')
        self.assertTrue(validate_document(request, self.root)['payloads_verified'])
        self.assertEqual(request['provenance']['license']['redistribution'], 'unknown')
        request['provenance']['fixture'] = False
        self.assertInvalid(request, 'MODEL3D_PROVENANCE')

    def test_all_evidence_contracts_roundtrip_without_promoting_claims(self):
        import zipfile
        evidence = self.root / 'native-evidence.txt'
        evidence.write_text('Synthetic schema fixture, not production execution or approval.', encoding='utf-8')
        native = payload(self.root, evidence.name, 'text/plain')
        provider_request = request_fixture(self.asset['sources'])
        provider_request.update(method='provider', provider=dict(id='unconfigured', model=None, network_authorized=False))
        request = write_document(self.root, 'request.json', provider_request)
        asset = write_document(self.root, 'asset.json', self.asset)
        asset_ref = dict(id=self.asset['id'], version=self.asset['version'], manifest=asset)
        with zipfile.ZipFile(self.root / 'portable.zip', 'w') as archive:
            archive.write(self.root / 'fixture.glb', 'fixture.glb')
        archive_record = payload(self.root, 'portable.zip', 'application/zip')
        common = dict(schema_version=1, id='fixture-evidence', version='1.0.0',
                      subject=dict(id='fixture', version='1.0.0'), owner='model3d',
                      native_evidence=[native], extensions=[])
        fields = {
            'provider-receipt': dict(provider='unconfigured', model=None, provider_version=None,
                request=request, status='unavailable', executed=False, outputs=[], seed=None, usage=None, error=None),
            'processing-stage': dict(operation='inspect', status='queued', inputs=[self.asset['geometry']],
                outputs=[], tools=[dict(name='test-fixture', version='1.0.0')], usage=None, error=None),
            'resource-usage': dict(status='unavailable', wall_seconds=None, cpu_seconds=None,
                peak_memory_bytes=None, input_bytes=None, output_bytes=None, compute_device=None, measurement_method=None),
            'validation-result': dict(validator=dict(name='test-fixture', version='1.0.0'), status='unverified',
                contract_valid=False, payloads_verified=False, geometry_inspected=False, diagnostics=[]),
            'review-decision': dict(reviewer='synthetic-fixture', reviewer_kind='automated', timestamp=None,
                verdict='pending', dimensions=['geometry'], inspected=[self.asset['geometry']],
                notes='Tests only; this is not approval.'),
            'repair-operation': dict(operation='repair', before=asset_ref, after=None, status='proposed',
                changed_parts=['body'], reason='Synthetic contract fixture only.', tools=[dict(name='test-fixture', version='1.0.0')]),
            'archive': dict(archive=archive_record, format='zip', asset=asset_ref, inventory=[self.asset['geometry']],
                status='unverified', restoration_evidence=None, preserved_reviews=[])}
        invalid = {
            'provider-receipt': dict(status='completed', executed=False),
            'processing-stage': dict(status='completed'),
            'resource-usage': dict(status='measured'),
            'validation-result': dict(geometry_inspected=True),
            'review-decision': dict(verdict='approved'),
            'repair-operation': dict(status='completed'),
            'archive': dict(status='restored')}
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(schemas()['model3d.schema.json'])
        for name, values in fields.items():
            with self.subTest(kind=name):
                document = dict(common, kind=KINDS[name], **values)
                validator.validate(document)
                encoded = json.dumps(document, sort_keys=True)
                self.assertEqual(validate_contract(decode_json(encoded)), document)
                report = validate_document(document, self.root)
                self.assertTrue(report['payloads_verified'])
                self.assertEqual(report['provider_execution'], 'not-assessed')
                self.assertEqual(report['visual_approval'], 'not-assessed')
                broken = deepcopy(document)
                broken.update(invalid[name])
                self.assertInvalid(broken)
                broken = deepcopy(document)
                broken['schema_version'] = 999
                self.assertInvalid(broken, 'MODEL3D_SCHEMA')
                broken = deepcopy(document)
                broken['fictional_approval'] = True
                self.assertInvalid(broken, 'MODEL3D_SCHEMA')
                broken = deepcopy(document)
                broken['native_evidence'][0]['sha256'] = '0' * 64
                self.assertInvalid(broken, 'MODEL3D_HASH', payloads=True)

    def test_part_regions_scoped_uncertainty_and_mesh_claims_are_measured(self):
        doc = deepcopy(self.asset)
        doc['parts'][0]['mesh_regions'] = [dict(mesh_id='strip', face_indices=[0])]
        uncertainty = dict(schema_version=1, kind=KINDS['uncertainty'], id='surface', version='1.0.0', target='body',
            classification='observed', message='Synthetic triangle observation.', evidence=['original'],
            confidence=None, confidence_method=None, status='pending',
            scope=dict(level='faces', target='strip', indices=[0]), inferred_method=None,
            missing_viewpoints=['back'], recommended_actions=['Inspect the back surface.'])
        doc['uncertainties'] = [uncertainty]
        self.assertTrue(validate_document(doc, self.root)['geometry_inspected'])
        for mutate, code in (
            (lambda x: x['parts'][0]['mesh_regions'][0].update(face_indices=[999]), 'MODEL3D_REGION'),
            (lambda x: x['uncertainties'][0]['scope'].update(indices=[999]), 'MODEL3D_UNCERTAINTY'),
            (lambda x: x['uncertainties'][0]['scope'].update(target='missing'), 'MODEL3D_REFERENCE'),
            (lambda x: x['mesh_details'][0].update(vertices=999), 'MODEL3D_MEASUREMENTS'),
            (lambda x: x['mesh_details'][0].update(normals=False), 'MODEL3D_MEASUREMENTS'),
            (lambda x: x['mesh_details'][0]['topology'].update(boundary_edges=999), 'MODEL3D_MEASUREMENTS')):
            broken = deepcopy(doc)
            mutate(broken)
            self.assertInvalid(broken, code, payloads=True)

    def test_document_image_budget_cannot_be_bypassed_through_cached_dependencies(self):
        from PIL import Image
        child = asset_fixture(self.root, 'child-budget')
        Image.new('RGB', (512,512)).save(self.root / 'child-texture.png')
        source = deepcopy(child['sources'][0])
        source.update(id='texture', role='texture', file=payload(self.root, 'child-texture.png', 'image/png'),
                      original_filename='child-texture.png', dimensions_px=[512,512])
        child['sources'].append(source)
        child['limits']['memory_bytes'] = 65536
        with mock.patch.object(Image.Image, 'load', side_effect=AssertionError('Pixels decoded before resource admission')) as decode:
            self.assertInvalid(child, 'MODEL3D_BUDGET', payloads=True)
            decode.assert_not_called()
        parent = deepcopy(self.asset)
        # Root captures the same pixels first. Cache reuse cannot waive child's limit.
        parent['sources'].append(deepcopy(source))
        record = write_document(self.root, 'child-budget.json', child)
        parent['dependencies'] = [dict(id=child['id'], version=child['version'], manifest=record)]
        self.assertInvalid(parent, 'MODEL3D_BUDGET', payloads=True)

    def test_whole_mesh_and_other_regional_owner_conflict(self):
        doc = deepcopy(self.asset)
        second = deepcopy(doc['parts'][0])
        second.update(id='region-owner', mesh_ids=[], mesh_regions=[dict(mesh_id='strip', face_indices=[0])])
        doc['parts'].append(second)
        self.assertInvalid(doc, 'MODEL3D_REGION', payloads=True)
        # A whole owner may annotate its own surface subset.
        doc['parts'].pop()
        doc['parts'][0]['mesh_regions'] = second['mesh_regions']
        self.assertTrue(validate_document(doc, self.root)['geometry_inspected'])

    def test_fixture_provenance_cannot_be_hidden_in_secondary_lod(self):
        from rig_gltf import export_geometry
        from rig_import import load_character_model
        from test_rig_skinning import test_geometry
        lod = asset_fixture(self.root, 'fixture-lod')
        path = self.root / 'ordinary.glb'
        export_geometry(test_geometry(), path, provenance={'fixture': False})
        model = load_character_model(path)
        record = payload(self.root, path.name, 'model/gltf-binary')
        doc = deepcopy(self.asset)
        doc.update(geometry=record, files=[record] + lod['files'],
                   mesh_details=mesh_details(model, record) + [dict(item, lod=1) for item in lod['mesh_details']])
        doc['sources'][0]['file'] = record
        for item in (doc['provenance'], doc['sources'][0]['provenance']):
            item.update(origin='imported', fixture=False)
        self.assertInvalid(doc, 'MODEL3D_PROVENANCE', payloads=True)
        doc['provenance']['fixture'] = True
        self.assertTrue(validate_document(doc, self.root)['geometry_inspected'])

    def test_evidence_references_preserve_subject_and_immutable_revision(self):
        native = self.asset['geometry']
        reference = write_document(self.root, 'before.json', self.asset)
        before = dict(id='fixture', version='1.0.0', manifest=reference)
        common = dict(schema_version=1, id='fixture-evidence', version='1.0.0',
                      subject=dict(id='fixture', version='1.0.0'), owner='model3d', native_evidence=[native])
        repair = dict(common, kind=KINDS['repair-operation'], operation='repair', before=before,
                      after=None, status='proposed', changed_parts=['body'], reason='Contract fixture.',
                      tools=[dict(name='fixture', version='1.0.0')])
        for after in (dict(before, manifest=write_document(self.root, 'alias-before.json', self.asset)),
                      dict(before, id='unrelated', version='2.0.0')):
            broken = deepcopy(repair)
            broken.update(status='completed', after=after)
            self.assertInvalid(broken, 'MODEL3D_REPAIR')
        broken = deepcopy(repair)
        broken['subject']['version'] = '2.0.0'
        self.assertInvalid(broken, 'MODEL3D_REFERENCE')
        request = request_fixture(self.asset['sources'])
        request.update(method='provider', provider=dict(id='unconfigured', model=None, network_authorized=False))
        receipt = dict(common, kind=KINDS['provider-receipt'], provider='unconfigured', model=None,
                       provider_version=None, request=write_document(self.root, 'provider.json', request),
                       status='unavailable', executed=False, outputs=[], seed=None, usage=None, error=None)
        self.assertTrue(validate_document(receipt, self.root)['payloads_verified'])
        for malformed in ({'unrelated': 'not a model request'}, dict(request, asset_id='unrelated'),
                          dict(request, provider=[]), dict(request, provider=dict(request['provider'], id='other'))):
            broken = deepcopy(receipt)
            broken['request'] = write_document(self.root, 'malformed-request.json', malformed)
            self.assertInvalid(broken, payloads=True)
        import zipfile
        with zipfile.ZipFile(self.root / 'contract.zip', 'w') as archive:
            archive.write(self.root / 'fixture.glb', 'fixture.glb')
        document = dict(common, kind=KINDS['archive'], archive=payload(self.root, 'contract.zip', 'application/zip'),
                        format='zip', asset=before, inventory=[native], status='unverified',
                        restoration_evidence=None, preserved_reviews=[])
        document['subject'] = dict(id='unrelated', version='1.0.0')
        self.assertInvalid(document, 'MODEL3D_REFERENCE')

    def test_direct_schema_and_runtime_reject_newline_tokens_and_unsafe_path_spellings(self):
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(schemas()['model3d.schema.json'])
        for field in ('id', 'version'):
            broken = deepcopy(self.asset)
            broken[field] += '\n'
            self.assertFalse(validator.is_valid(broken), field)
            self.assertInvalid(broken, 'MODEL3D_SCHEMA')
        broken = deepcopy(self.asset)
        broken['geometry']['sha256'] += '\n'
        self.assertFalse(validator.is_valid(broken))
        self.assertInvalid(broken, 'MODEL3D_SCHEMA')
        for path in ('../escape', '/absolute', 'C:/drive', 'a\\b', 'a/../b', 'a//b', 'aux.txt',
                     'a. ', 'nested/NUL.obj', 'nested/CONIN$.png', 'nested/trailing.', 'file\n.glb'):
            broken = deepcopy(self.asset)
            broken['geometry']['path'] = path
            self.assertFalse(validator.is_valid(broken), path)
            self.assertInvalid(broken, 'MODEL3D_SCHEMA')
        # Portable Unicode and leading-dot components remain supported.
        for path in ('models/é.glb', '.source/character.glb', 'models/console.glb'):
            broken = deepcopy(self.asset)
            broken['geometry']['path'] = path
            self.assertTrue(validator.is_valid(broken), path)

    def test_cached_geometry_rechecks_tighter_inherited_memory_in_both_dependency_orders(self):
        import numpy as np
        from rig_gltf import export_geometry
        from rig_import import load_character_model
        from test_rig_skinning import test_geometry
        leaf = asset_fixture(self.root, 'memory-leaf')
        middle = asset_fixture(self.root, 'memory-middle')
        def dependency_graph():
            leaf_record = write_document(self.root, 'memory-leaf.json', leaf)
            leaf_reference = dict(id=leaf['id'], version=leaf['version'], manifest=leaf_record)
            middle['dependencies'] = [leaf_reference]
            middle['limits']['memory_bytes'] = 65536
            middle_record = write_document(self.root, 'memory-middle.json', middle)
            return leaf_reference, dict(id=middle['id'], version=middle['version'], manifest=middle_record)
        # Small real geometry fits both budgets; retain one asset observation,
        # bound to the tightest memory context that was actually inspected.
        direct, indirect = dependency_graph()
        for references in ([direct, indirect], [indirect, direct]):
            doc = deepcopy(self.asset)
            doc['dependencies'] = references
            report = validate_document(doc, self.root)
            self.assertEqual(len(report['geometry']), 3)
            observation = next(item for item in report['geometry'] if item['asset_id'] == leaf['id'])
            self.assertEqual(observation['inspection_memory_budget_bytes'], 65536)
        # Enlarge the source to800 actual vertices/400 triangles; 64KiB cannot
        # admit its native importer allocation, regardless of cache visit order.
        geometry = test_geometry()
        mesh = geometry.meshes[0]
        vertices, faces = mesh.positions, mesh.triangles
        mesh.positions = np.concatenate([vertices + [i*3,0,0] for i in range(200)])
        mesh.triangles = np.concatenate([faces + 4*i for i in range(200)])
        path = self.root / 'memory-leaf.glb'
        export_geometry(geometry, path, provenance={'fixture': True})
        model = load_character_model(path)
        record = payload(self.root, path.name, 'model/gltf-binary')
        leaf.update(geometry=record, files=[record], mesh_details=mesh_details(model, record),
                    measurements=dict(vertices=model.geometry.vertex_count, triangles=model.geometry.triangle_count,
                        bounds=dict(minimum=model.report['bounds'][0], maximum=model.report['bounds'][1])))
        leaf['sources'][0]['file'] = record
        direct, indirect = dependency_graph()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        for references in ([direct, indirect], [indirect, direct]):
            doc = deepcopy(self.asset)
            doc['dependencies'] = references
            self.assertInvalid(doc, 'RIG_LIMIT_MEMORY', payloads=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_public_cli_rejects_malformed_dependency_limits_as_structured_failures(self):
        child = asset_fixture(self.root, 'malformed-child')
        for missing, limit in ((True, None), (False, None), (False, [])):
            broken = deepcopy(child)
            if missing:
                del broken['limits']
            else:
                broken['limits'] = limit
            reference = write_document(self.root, 'malformed-child.json', broken)
            root = deepcopy(self.asset)
            root['dependencies'] = [dict(id=child['id'], version=child['version'], manifest=reference)]
            path = self.root / 'malformed-root.json'
            write_document(self.root, path.name, root)
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/model3d.py'),
                'validate', '--file', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stderr)['error']['code'], 'MODEL3D_SCHEMA')
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_public_cli_rejects_oversized_json_integer_without_mutating_inputs(self):
        path = self.root / 'oversized-integer.json'
        raw = b'{"schema_version":1,"kind":"model3d-asset","untrusted":' + b'9' * 5000 + b'}'
        path.write_bytes(raw)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = subprocess.run([sys.executable, '-X', 'int_max_str_digits=4300', '-B',
            str(ROOT / 'scripts/model3d.py'), 'validate', '--file', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        error = json.loads(result.stderr)
        self.assertFalse(error['ok'])
        self.assertEqual(error['error']['code'], 'RIG_JSON')
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_transitive_per_file_budget_checks_cached_payloads_before_copy(self):
        import model3d_validation
        leaf = asset_fixture(self.root, 'file-leaf')
        middle = asset_fixture(self.root, 'file-middle')
        leaf_path = self.root / 'large-evidence.bin'
        leaf_path.write_bytes(b'x' * 200000)
        def graph():
            leaf['provenance']['evidence'] = [payload(self.root, leaf_path.name, 'application/octet-stream')]
            leaf_record = write_document(self.root, 'file-leaf.json', leaf)
            direct = dict(id=leaf['id'], version=leaf['version'], manifest=leaf_record)
            middle['dependencies'] = [direct]
            middle['limits']['file_bytes'] = 100000
            middle_record = write_document(self.root, 'file-middle.json', middle)
            return direct, dict(id=middle['id'], version=middle['version'], manifest=middle_record)
        direct, indirect = graph()
        original_open = model3d_validation._open_payload
        for references, expected_large_reads in (([indirect], 0), ([direct, indirect], 1), ([indirect, direct], 0)):
            doc = deepcopy(self.asset)
            doc['dependencies'] = references
            opened = []
            def observe_open(root, name):
                opened.append(name)
                return original_open(root, name)
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            with mock.patch('model3d_validation._open_payload', side_effect=observe_open):
                self.assertInvalid(doc, 'MODEL3D_BUDGET', payloads=True)
            self.assertEqual(opened.count(leaf_path.name), expected_large_reads)
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        # The exact limit is valid. Both cache orders preserve the100000 bytes.
        leaf_path.write_bytes(b'x' * 100000)
        direct, indirect = graph()
        for references in ([direct, indirect], [indirect, direct]):
            doc = deepcopy(self.asset)
            doc['dependencies'] = references
            report = validate_document(doc, self.root)
            self.assertTrue(report['geometry_inspected'])
            observation = next(item for item in report['geometry'] if item['asset_id'] == leaf['id'])
            self.assertEqual(observation['inspection_file_budget_bytes'], 100000)
            self.assertEqual(hashlib.sha256(leaf_path.read_bytes()).hexdigest(), leaf['provenance']['evidence'][0]['sha256'])

    def test_integral_float_byte_counts_stream_exact_bytes_and_reject_other_values(self):
        path = self.root / 'float-count.bin'
        for size in (3, 2 * 1024**2 + 3):
            raw = (b'abc' * ((size + 2) // 3))[:size]
            path.write_bytes(raw)
            source = deepcopy(self.asset['sources'][0])
            source.update(role='annotation', file=payload(self.root, path.name, 'application/octet-stream'),
                          original_filename=path.name)
            source['file']['bytes'] = float(size)
            validate_contract(source)
            source_path = self.root / 'float-source.json'
            write_document(self.root, source_path.name, source)
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/model3d.py'),
                'validate', '--file', str(source_path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['verified_bytes'], size)
            self.assertEqual(report['files'][0]['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
            for invalid in (True, False, size + .5, 0, -1, '3'):
                broken = deepcopy(source)
                broken['file']['bytes'] = invalid
                self.assertInvalid(broken, 'MODEL3D_SCHEMA', payloads=True)

    def test_integral_float_limits_and_geometry_counts_preserve_manifest_and_payload(self):
        doc = deepcopy(self.asset)
        doc['limits'] = {name: float(value) for name, value in doc['limits'].items()}
        doc['geometry']['bytes'] = float(doc['geometry']['bytes'])
        doc['measurements']['vertices'] = float(doc['measurements']['vertices'])
        doc['measurements']['triangles'] = float(doc['measurements']['triangles'])
        encoded = json.dumps(doc, sort_keys=True)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        report = validate_document(doc, self.root)
        self.assertTrue(report['geometry_inspected'])
        self.assertEqual(report['verified_bytes'], int(doc['geometry']['bytes']))
        self.assertEqual(json.dumps(doc, sort_keys=True), encoded)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_schema_discovery_does_not_import_numpy(self):
        command = [sys.executable, '-B', '-c',
                   'import sys;sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + ');'
                   'import model3d_contracts;model3d_contracts.schemas();assert "numpy" not in sys.modules']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
