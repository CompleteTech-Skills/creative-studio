"""Real-byte registration, exact resolution and hostile-input acceptance for #138."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from asset_library import AssetLibrary, file_record, legacy_roundtrip, read_source_bytes
from asset_library_contracts import (canonical_bytes, load_manifest, seal, schemas,
                                     validate, manifest_hash)
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


class AssetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # Genuine editable OBJ geometry; registration stores it without claiming
        # independent mesh import/validation (the #139/#140 adapter boundary).
        self.mesh = self.root / 'triangle.obj'
        self.mesh.write_bytes(b'o triangle\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        self.license = self.root / 'license.txt'
        self.license.write_bytes(b'Engineering fixture authored for test; CC0-1.0.\n')
        self.files = [file_record(self.mesh, 'mesh/triangle.obj', 'geometry'),
                      file_record(self.license, 'source/license.txt', 'license', False)]
        self.sources = {self.files[0]['path']: self.mesh, self.files[1]['path']: self.license}
        self.manifest = seal(dict(schema_version=1, kind='asset-library-version',
            identity=dict(id='triangle', namespace='fixtures', library='test-library',
                          slug='triangle', name='Triangle', kind='prop', subtype='mesh',
                          created_at='2026-10-23T00:00:00Z', attribution='Test author', owner='Test author'),
            version='1.0.0', files=self.files, dependencies=[],
            metadata=dict(description='Procedural engineering triangle', category='geometry',
                          author='Test author', publisher='Test author', source_url='local:test-fixture',
                          license='CC0-1.0', allowed_uses=['test'], keywords=['triangle'],
                          style_refs=[], capabilities=[], review_status='unreviewed'),
            compatibility=dict(coordinate_system='right-handed-y-up-z-forward', unit_scale=1,
                          rig_signature=[], skeleton_signature=[], material_capabilities=[],
                          texture_channels=[], animation_channels=[], renderers=[], formats=['obj'],
                          extensions=[], consumer_capabilities=[]), previews=[],
            provenance=dict(method='fixture', origin='Procedurally authored test geometry', fixture=True,
                          sources=[self.files[0]], provider=[], tools=[], parents=[], edits=[],
                          license_evidence=[self.files[1]], permissions=[], uncertainty=['Not geometry-validated']),
            publication=dict(visibility='private', distribution='not-evaluated',
                          license_evaluation='evidence-retained', inventory=list(self.sources),
                          verification='payload-verified', integration='not-exercised')))
        self.library = AssetLibrary(self.root / 'library', create=True)

    def register(self, manifest=None):
        return self.library.register(manifest or self.manifest, self.sources)

    def test_actual_geometry_registration_restart_relocation_and_exact_bytes(self):
        reference = self.register()
        self.assertEqual(self.library.resolve(reference), self.manifest)
        self.assertEqual(self.library.read_file(reference, self.files[0]['path']), self.mesh.read_bytes())
        self.assertEqual(self.library.scene_reference(reference, self.files[0]['path']),
                         dict(id='triangle', version='1.0.0', path=self.files[0]['path'],
                              sha256=self.files[0]['sha256'], kind='prop'))
        renamed = self.root / 'relocated'
        self.library.root.rename(renamed)
        reopened = AssetLibrary(renamed)
        self.assertEqual(reopened.resolve(reference), self.manifest)
        self.assertEqual(reopened.read_file(reference, self.files[1]['path']), self.license.read_bytes())
        self.assertEqual(hashlib.sha256(reopened.read_file(reference, self.files[0]['path'])).hexdigest(),
                         self.files[0]['sha256'])

    def test_immutable_versions_and_false_latest_are_rejected(self):
        reference = self.register()
        with self.assertRaises(RigError): self.register()
        for key, value in [('version', 'latest'), ('version', '2.0.0'), ('sha256', 'f'*64)]:
            wrong = {**reference, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(RigError):
                self.library.resolve(wrong)
        revised = deepcopy(self.manifest)
        revised['version'] = '2.0.0'
        revised['metadata']['description'] = 'New metadata, same source bytes'
        newer = self.register(seal(revised))
        self.assertEqual(self.library.resolve(reference), self.manifest)
        self.assertEqual(self.library.resolve(newer)['version'], '2.0.0')
        with closing(sqlite3.connect(self.library.database)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM blobs').fetchone()[0], 2)

    def test_wrong_hash_missing_content_and_header_lie_leave_no_version(self):
        cases = []
        wrong = deepcopy(self.manifest)
        wrong['files'][0]['sha256'] = 'f'*64
        wrong['provenance']['sources'][0]['sha256'] = 'f'*64
        cases.append(seal(wrong))
        wrong = deepcopy(self.manifest)
        wrong['files'][0]['format'] = 'glb'
        wrong['files'][0]['mime_type'] = 'model/gltf-binary'
        cases.append(seal(wrong))
        for case in cases:
            with self.assertRaises(RigError): self.register(case)
        self.mesh.unlink()
        with self.assertRaises(RigError): self.register()
        with closing(sqlite3.connect(self.library.database)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM blobs').fetchone()[0], 0)

    def test_corrupt_deleted_payload_and_manifest_are_not_ready(self):
        reference = self.register()
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('UPDATE blobs SET payload=? WHERE sha256=?', (b'wrong', self.files[0]['sha256']))
        with self.assertRaises(RigError): self.library.resolve(reference)
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('UPDATE blobs SET payload=? WHERE sha256=?', (self.mesh.read_bytes(), self.files[0]['sha256']))
            db.execute('DELETE FROM blobs WHERE sha256=?', (self.files[1]['sha256'],))
        with self.assertRaises(RigError): self.library.read_file(reference, self.files[0]['path'])
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('INSERT INTO blobs VALUES(?,?)', (self.files[1]['sha256'], self.license.read_bytes()))
        self.assertEqual(self.library.resolve(reference), self.manifest)
        changed = deepcopy(self.manifest)
        changed['identity']['id'] = 'substituted'
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('UPDATE versions SET manifest=?', (canonical_bytes(seal(changed)),))
        with self.assertRaises(RigError): self.library.resolve(reference)

    def test_exact_dependency_closure_is_verified_after_restart(self):
        reference = self.register()
        second = deepcopy(self.manifest)
        second['identity']['id'] = 'assembly'
        second['dependencies'] = [dict(target=reference, purpose='mesh', required=True,
                                       origin='authored', relationship='direct', capabilities=[])]
        assembly = self.register(seal(second))
        self.assertEqual(self.library.resolve(assembly)['identity']['id'], 'assembly')
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('DELETE FROM versions WHERE id=?', ('triangle',))
        with self.assertRaises(RigError): AssetLibrary(self.library.root).resolve(assembly)
        third = deepcopy(second)
        third['identity']['id'] = 'missing-dependency'
        with self.assertRaises(RigError): self.register(seal(third))

    def test_annotation_cas_is_separate_and_concurrent_writers_cannot_lose_edits(self):
        reference = self.register()
        initial = self.library.annotations(reference)
        self.assertEqual(initial['revision'], 0)
        barrier = threading.Barrier(2)
        outcomes = []
        def writer(tag):
            barrier.wait()
            try:
                outcomes.append(self.library.update_annotations(reference, expected_revision=0,
                               tags=[tag], favorite=True, note='Mutable user annotation'))
            except RigError as exc: outcomes.append(exc.report['code'])
        workers = [threading.Thread(target=writer, args=(tag,)) for tag in ('first', 'second')]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(sum(isinstance(x, dict) for x in outcomes), 1)
        self.assertIn('LIBRARY_CONFLICT', outcomes)
        self.assertEqual(self.library.annotations(reference)['revision'], 1)
        self.assertEqual(self.library.resolve(reference), self.manifest)

    def test_schema_structural_parity_and_drift(self):
        for name, schema in schemas().items():
            Draft202012Validator.check_schema(schema)
            self.assertEqual(json.loads((ROOT/'profiles'/name).read_text()), schema)
        schema = schemas()['asset-library-version.schema.json']
        mutants = []
        for key, value in [('schema_version', 2), ('files', []), ('version', True), ('unknown', 'field')]:
            value_manifest = deepcopy(self.manifest)
            value_manifest[key] = value
            mutants.append(value_manifest)
        for value in [self.manifest, *mutants]:
            structural_ok = not list(Draft202012Validator(schema).iter_errors(value))
            try: validate(value); runtime_ok = True
            except RigError: runtime_ok = False
            self.assertEqual(runtime_ok, structural_ok)

    def test_semantic_negatives_paths_aliases_origin_inventory_evidence(self):
        for path in ('../mesh.obj', '/mesh.obj', 'a//b', 'a/./b', 'C:/a', 'CON.txt', 'a/NUL', 'a/..', 'a.'):
            changed = deepcopy(self.manifest)
            changed['files'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(RigError): seal(changed)
        mutations = []
        case = deepcopy(self.manifest); case['files'].append(deepcopy(case['files'][0])); mutations.append(case)
        case = deepcopy(self.manifest); case['provenance']['fixture'] = False; mutations.append(case)
        case = deepcopy(self.manifest); case['provenance']['license_evidence'] = []; mutations.append(case)
        case = deepcopy(self.manifest); case['publication']['inventory'] = ['missing']; mutations.append(case)
        case = deepcopy(self.manifest); case['compatibility']['unit_scale'] = float('nan'); mutations.append(case)
        case = deepcopy(self.manifest); case['files'][0]['bytes'] = 0; mutations.append(case)
        case = deepcopy(self.manifest); case['identity']['name'] = 'x'*257; mutations.append(case)
        case = deepcopy(self.manifest); case['metadata']['review_status'] = 'reviewed-externally'; mutations.append(case)
        for case in mutations:
            with self.assertRaises((RigError, ValueError)): seal(case)
        with self.assertRaises(RigError): load_manifest(b'{"schema_version":1,"schema_version":1}')
        with self.assertRaises(RigError): load_manifest(b'['*70+b']'*70)
        with self.assertRaises(RigError): load_manifest(b' '* (1024*1024+1))

    def test_symlink_source_database_and_replaced_root_fail_closed(self):
        link = self.root / 'link.obj'
        try: link.symlink_to(self.mesh)
        except OSError:
            # Some Windows accounts cannot create links. Exercise rejection of
            # the observed reparse metadata without claiming a live-link probe.
            with patch('asset_library.Path.lstat', return_value=SimpleNamespace(st_mode=stat.S_IFLNK)):
                with self.assertRaises(RigError):
                    file_record(link, 'mesh.obj')
            return
        with self.assertRaises((RigError, OSError)): file_record(link, 'mesh.obj')
        link_dir = self.root / 'link-dir'
        link_dir.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((RigError, OSError)): file_record(link_dir / 'triangle.obj', 'mesh.obj')
        original = self.library.database.read_bytes()
        self.library.database.unlink()
        target = self.root / 'target.sqlite3'
        target.write_bytes(original)
        self.library.database.symlink_to(target)
        with self.assertRaises(RigError): self.register()
        self.assertEqual(target.read_bytes(), original)

    def test_public_source_reader_rejects_invalid_bounds_and_nonregular_files(self):
        self.assertEqual(read_source_bytes(self.mesh, len(self.mesh.read_bytes())), self.mesh.read_bytes())
        for limit in (0, -1, True, 64*1024*1024 + 1):
            with self.subTest(limit=limit), self.assertRaises(RigError):
                read_source_bytes(self.mesh, limit)
        with self.assertRaises(RigError):
            read_source_bytes(self.mesh, 1)
        empty = self.root / 'empty'
        empty.write_bytes(b'')
        with self.assertRaises(RigError):
            read_source_bytes(empty)
        if hasattr(os, 'mkfifo'):
            fifo = self.root / 'pipe'
            os.mkfifo(fifo)
            with self.assertRaises(RigError):
                read_source_bytes(fifo)

    def test_malformed_source_paths_have_structured_diagnostics_without_mutation(self):
        database_before = self.library.database.read_bytes()
        sources_before = {name: source.read_bytes() for name, source in self.sources.items()}
        for malformed in ('bad\0path', Path('bad\0path')):
            operations = (
                lambda: read_source_bytes(malformed),
                lambda: file_record(malformed, 'mesh/triangle.obj'),
                lambda: self.library.register(self.manifest, {**self.sources, self.files[0]['path']: malformed}),
            )
            for operation in operations:
                with self.subTest(path=repr(malformed)), self.assertRaises(RigError) as caught:
                    operation()
                self.assertEqual(caught.exception.report['code'], 'LIBRARY_SOURCE')
                self.assertIsInstance(caught.exception.__cause__, (OSError, ValueError))
                self.assertEqual(self.library.database.read_bytes(), database_before)
                self.assertEqual({name: source.read_bytes() for name, source in self.sources.items()}, sources_before)

    def test_source_reader_preserves_specific_structured_diagnostics(self):
        empty = self.root / 'empty-source'
        empty.write_bytes(b'')
        for path, limit, code in ((self.mesh, 0, 'LIBRARY_LIMIT'),
                                  (empty, 1, 'LIBRARY_PAYLOAD')):
            with self.subTest(code=code), self.assertRaises(RigError) as caught:
                read_source_bytes(path, limit)
            self.assertEqual(caught.exception.report['code'], code)

    def test_replaced_private_root_is_rejected_before_storage_access(self):
        original = self.library.database.read_bytes()
        old_root = self.root / 'retained-library'
        self.library.root.rename(old_root)
        self.library.root.mkdir(mode=0o700)
        with self.assertRaises(RigError):
            self.register()
        self.assertEqual((old_root / 'library.sqlite3').read_bytes(), original)

    def test_quota_failure_rolls_back_blobs_and_registration(self):
        with patch('asset_library.MAX_LIBRARY_BYTES', len(self.mesh.read_bytes())):
            with self.assertRaises(RigError):
                self.register()
        with closing(sqlite3.connect(self.library.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM blobs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)
        self.register()

    def test_duplicate_registration_writers_publish_exactly_one_version(self):
        barrier = threading.Barrier(2)
        results = []
        def writer():
            barrier.wait()
            try:
                results.append(self.register())
            except RigError as exc:
                results.append(exc.report['code'])
        workers = [threading.Thread(target=writer) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('LIBRARY_IMMUTABLE', results)

    def test_every_conceptual_contract_roundtrips_and_rejects_unknown_fields(self):
        reference = dict(id='triangle', version='1.0.0', sha256=self.manifest['manifest_sha256'])
        samples = dict(AssetVersion=self.manifest, AssetIdentity=self.manifest['identity'],
            AssetFile=self.files[0], AssetMetadata=self.manifest['metadata'],
            AssetCompatibility=self.manifest['compatibility'], AssetProvenance=self.manifest['provenance'],
            AssetPublication=self.manifest['publication'], AssetRef=reference,
            AssetAnnotations=dict(schema_version=1, asset=reference, revision=0, tags=[], favorite=False, note=''),
            AssetDependency=dict(target=reference, purpose='geometry', required=True,
                                 origin='authored', relationship='direct', capabilities=[]),
            AssetPreview=dict(type='thumbnail', source_version_sha256=reference['sha256'],
                              backend='test-fixture', backend_version='1.0.0', settings_sha256=self.files[1]['sha256'],
                              camera_sha256=self.files[1]['sha256'], lighting_sha256=self.files[1]['sha256'], file=self.files[0],
                              settings_file=self.files[1], camera_file=self.files[1], lighting_file=self.files[1],
                              width=1, height=1, status='generated'))
        for kind, value in samples.items():
            schema = next(value for value in schemas().values() if value['title'] == kind + ' v1')
            restored = json.loads(canonical_bytes(value))
            with self.subTest(kind=kind):
                self.assertEqual(validate(restored, kind), value)
                self.assertFalse(list(Draft202012Validator(schema).iter_errors(restored)))
                wrong = dict(restored, unsupported='field')
                self.assertTrue(list(Draft202012Validator(schema).iter_errors(wrong)))
                with self.assertRaises(RigError):
                    validate(wrong, kind)

    def test_native_documents_use_original_owners_and_preserve_ids(self):
        for kind, relative in [('legacy-cast', 'examples/production-pilot/cast.json'),
                               ('legacy-clip', 'examples/production-pilot/clips/luma-idle.json'),
                               ('rig', 'examples/rig-3d/robotic/rig.json')]:
            original = (ROOT / relative).read_bytes()
            with self.subTest(kind=kind):
                self.assertEqual(legacy_roundtrip(kind, original), original)
                self.assertEqual((ROOT / relative).read_bytes(), original)

    def test_review_integral_float_counts_preserve_raw_manifest_and_exact_bytes(self):
        def floats(value):
            if type(value) is int:
                return float(value)
            if isinstance(value, dict):
                return {key: floats(child) for key, child in value.items()}
            if isinstance(value, list):
                return [floats(child) for child in value]
            return value
        manifest = seal(floats(self.manifest))
        original_manifest = canonical_bytes(manifest)
        sources_before = {name: source.read_bytes() for name, source in self.sources.items()}
        schema = schemas()['asset-library-version.schema.json']
        self.assertFalse(list(Draft202012Validator(schema).iter_errors(manifest)))
        reference = self.register(manifest)
        reopened = AssetLibrary(self.library.root)
        restored = reopened.resolve(reference)
        self.assertEqual(canonical_bytes(restored), original_manifest)
        self.assertEqual(canonical_bytes(manifest), original_manifest)
        self.assertEqual(reference['sha256'], manifest['manifest_sha256'])
        for record in restored['files']:
            self.assertIs(type(record['bytes']), float)
            self.assertEqual(reopened.read_file(reference, record['path']), sources_before[record['path']])
        self.assertEqual(read_source_bytes(self.mesh, float(len(self.mesh.read_bytes()))), self.mesh.read_bytes())
        self.assertEqual({name: source.read_bytes() for name, source in self.sources.items()}, sources_before)
        annotation = reopened.update_annotations(reference, expected_revision=0.0,
            tags=['whole-float-revision'], favorite=False, note='Validated numeric revision copy')
        self.assertEqual(annotation['revision'], 1)
        self.assertEqual(canonical_bytes(reopened.resolve(reference)), original_manifest)
        self.assertEqual(reopened.annotations(reference), annotation)
        preview = dict(type='thumbnail', source_version_sha256=reference['sha256'],
            backend='schema-fixture', backend_version='1.0.0', settings_sha256=self.files[1]['sha256'],
            camera_sha256=self.files[1]['sha256'], lighting_sha256=self.files[1]['sha256'],
            settings_file=self.files[1], camera_file=self.files[1], lighting_file=self.files[1],
            file=self.files[0], width=1.0, height=1.0, status='generated')
        for kind, value in [('AssetAnnotations', {**annotation, 'revision': 1.0}),
                            ('AssetIdentity', restored['identity']), ('AssetPreview', preview)]:
            before = canonical_bytes(value)
            validate(value, kind)
            self.assertEqual(canonical_bytes(value), before)

    def test_review_invalid_integer_values_fail_structured_without_mutation(self):
        reference = self.register()
        database_before = self.library.database.read_bytes()
        sources_before = {name: source.read_bytes() for name, source in self.sources.items()}
        for value in (True, False, 1.5, -1, 0, 64*1024*1024+1, '43', None,
                      float('nan'), float('inf'), -float('inf'), 10**5000):
            manifest = deepcopy(self.manifest)
            manifest['version'] = '2.0.0'
            manifest['files'][0]['bytes'] = value
            with self.assertRaises(RigError):
                self.register(seal(manifest))
            with self.assertRaises(RigError):
                read_source_bytes(self.mesh, value)
        for revision in (True, False, 0.5, -1, 2**53, '0', None,
                         float('nan'), float('inf'), -float('inf'), 10**5000):
            with self.assertRaises(RigError):
                self.library.update_annotations(reference, expected_revision=revision,
                    tags=[], favorite=False, note='Rejected invalid revision')
        for version in (True, False, 1.5, -1, 0, 2, '1', None, float('nan'), float('inf'), 10**5000):
            manifest = deepcopy(self.manifest)
            manifest['schema_version'] = version
            with self.assertRaises(RigError):
                self.register(seal(manifest))
        self.assertEqual(self.library.database.read_bytes(), database_before)
        self.assertEqual({name: source.read_bytes() for name, source in self.sources.items()}, sources_before)
        self.assertEqual(self.library.resolve(reference), self.manifest)

    def test_review_escaped_surrogate_and_dictionary_encoding_fail_structured_without_mutation(self):
        reference = self.register()
        database_before = self.library.database.read_bytes()
        sources_before = {name: path.read_bytes() for name, path in self.sources.items()}
        for payload in (b'{"text":"\\ud800"}', '{"text":"\\ud800"}'):
            with self.subTest(kind=type(payload).__name__), self.assertRaises(RigError) as error:
                load_manifest(payload)
            self.assertEqual(error.exception.report['code'], 'LIBRARY_JSON')
        malformed = deepcopy(self.manifest)
        malformed['metadata']['description'] = '\ud800'
        for operation in (lambda: self.library.register(malformed, self.sources),
                          lambda: validate(malformed), lambda: canonical_bytes(malformed)):
            with self.assertRaises(RigError) as error:
                operation()
            self.assertEqual(error.exception.report['code'], 'LIBRARY_JSON')
        oversized_integer = {'integer': 10**5000}
        with self.assertRaises(RigError):
            canonical_bytes(oversized_integer)
        with self.assertRaises(RigError):
            seal(oversized_integer)
        self.assertEqual(self.library.database.read_bytes(), database_before)
        self.assertEqual({name: path.read_bytes() for name, path in self.sources.items()}, sources_before)
        self.assertEqual(self.library.resolve(reference), self.manifest)

    def test_review_regression_trailing_newline_identifiers_and_digests_reject_registration(self):
        for field in ('id', 'version'):
            changed = deepcopy(self.manifest)
            if field == 'id':
                changed['identity']['id'] += '\n'
            else:
                changed['version'] += '\n'
            changed['manifest_sha256'] = manifest_hash(changed)
            with self.subTest(field=field), self.assertRaises(RigError):
                self.register(changed)
        reference = self.register()
        with self.assertRaises(RigError):
            self.library.resolve({**reference, 'sha256': reference['sha256'] + '\n'})
        with self.assertRaises(RigError):
            validate({**self.files[0], 'sha256': self.files[0]['sha256'] + '\n'}, 'AssetFile')

    def test_review_regression_nonadjacent_file_directory_alias_rejects_registration(self):
        changed = deepcopy(self.manifest)
        changed['files'] = [{**self.files[0], 'path': path} for path in ('a', 'a-b', 'a/b')] + [self.files[1]]
        changed['provenance']['sources'] = [changed['files'][0]]
        changed['publication']['inventory'] = [f['path'] for f in changed['files']]
        changed['manifest_sha256'] = manifest_hash(changed)
        sources = {f['path']: self.mesh for f in changed['files'][:-1]}
        sources[self.files[1]['path']] = self.license
        with self.assertRaises(RigError) as error:
            self.library.register(changed, sources)
        self.assertEqual(error.exception.report['code'], 'LIBRARY_ALIAS')

    def test_review_regression_portable_paths_have_schema_runtime_parity(self):
        schema = schemas()['asset-library-file.schema.json']
        validator = Draft202012Validator(schema)
        for path in ('a/../secret', 'a/.', 'a//b', 'con.txt', 'a/Nul', 'a/COM1.bin',
                     'a/last.', 'a\n', 'a/b\n', 'a/file.obj', 'a/COMP1.obj'):
            value = {**self.files[0], 'path': path}
            structural_ok = not list(validator.iter_errors(value))
            try:
                validate(value, 'AssetFile')
                runtime_ok = True
            except RigError:
                runtime_ok = False
            with self.subTest(path=path):
                self.assertEqual(runtime_ok, structural_ok)
                self.assertEqual(runtime_ok, path in ('a/file.obj', 'a/COMP1.obj'))

    def test_review_source_fields_and_preview_configs_retain_bound_native_data(self):
        reference = self.register()
        changed = deepcopy(self.manifest)
        changed['identity']['id'] = 'preview-record'
        changed['provenance']['parents'] = [reference]
        changed['files'][0]['format_version'] = 'Wavefront OBJ'
        changed['files'][0]['technical_metadata'] = dict(vertices=3, triangles=1, engineering_fixture=True)
        changed['provenance']['sources'] = [changed['files'][0]]
        changed['provenance']['source_records'] = [dict(file=changed['files'][0],
            source_uri='local:authored-triangle-fixture', original_filename='triangle.obj',
            acquired_at='2026-10-23T00:00:00Z', author='Test author')]
        # Native configuration files remain opaque exact files owned by the
        # eventual renderer/camera owner. This test exercises source binding,
        # not actual preview generation or a provider integration.
        changed['previews'] = [dict(type='thumbnail', source_version_sha256=reference['sha256'],
            backend='structural-test-fixture', backend_version='1.0.0',
            settings_sha256=self.files[1]['sha256'], camera_sha256=self.files[1]['sha256'],
            lighting_sha256=self.files[1]['sha256'], file=changed['files'][0],
            settings_file=self.files[1], camera_file=self.files[1], lighting_file=self.files[1],
            width=1, height=1, status='generated')]
        result = self.register(seal(changed))
        self.assertEqual(self.library.resolve(result)['provenance']['source_records'],
                         changed['provenance']['source_records'])
        changed['previews'][0]['settings_sha256'] = '0'*64
        with self.assertRaises(RigError):
            seal(changed)
        with self.assertRaises(RigError):
            validate(changed['previews'][0], 'AssetPreview')
        bad = deepcopy(self.manifest['identity'])
        for date in ('yesterday', '2026-99-99T00:00:00Z', '2026-02-30T00:00:00Z'):
            bad['created_at'] = date
            with self.subTest(date=date), self.assertRaises(RigError):
                validate(bad, 'AssetIdentity')
        bad_provenance = deepcopy(self.manifest['provenance'])
        bad_provenance['source_records'] = [dict(file=self.files[0], source_uri='local:fixture',
            original_filename='triangle.obj', acquired_at='2026-02-30T00:00:00Z', author='Test author')]
        with self.assertRaises(RigError):
            validate(bad_provenance, 'AssetProvenance')

    def test_original_creation_time_is_stable_across_versions(self):
        self.register()
        changed = deepcopy(self.manifest)
        changed['version'] = '2.0.0'
        changed['identity']['created_at'] = '2026-10-24T00:00:00Z'
        with self.assertRaises(RigError):
            self.register(seal(changed))

    def test_legacy_profile_roundtrip_preserves_exact_bytes_and_2d_contract(self):
        source = ROOT / 'profiles/codex-v1.json'
        original = source.read_bytes()
        self.assertEqual(legacy_roundtrip('legacy-pet', original), original)
        files = [file_record(source, 'legacy/profile.json', 'native-profile'), self.files[1]]
        manifest = deepcopy(self.manifest)
        manifest['identity']['kind'] = 'legacy-pet'
        manifest['compatibility']['coordinate_system'] = 'legacy-2d'
        manifest['compatibility']['formats'] = ['json']
        manifest['files'] = files
        manifest['provenance']['sources'] = [files[0]]
        manifest['publication']['inventory'] = [f['path'] for f in files]
        ref = self.library.register(seal(manifest), {'legacy/profile.json': source,
                                                     self.files[1]['path']: self.license})
        restored = self.library.read_file(ref, 'legacy/profile.json')
        self.assertEqual(legacy_roundtrip('legacy-pet', restored), original)
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises((RigError, ValueError)):
            legacy_roundtrip('legacy-pet', b'{"schema_version":999}')


if __name__ == '__main__': unittest.main()
