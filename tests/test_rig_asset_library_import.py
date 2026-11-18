"""Offline native import with real Khronos validation and canonical reopening."""
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import struct
import shutil
import tempfile
from types import SimpleNamespace
import traceback
import unittest
from unittest.mock import patch

from PIL import Image

from asset_library import AssetLibrary
from asset_library_import import ImportPipeline, resolve_import
from rig_errors import RigError
from rig_gltf import read_gltf
from rig_import import load_character_model

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = Path(os.environ.get('HATCH_GLTF_VALIDATOR_ROOT',
                               ROOT / 'tools/rig/node_modules/gltf-validator'))
IDENTITY = dict(id='triangle', namespace='fixtures', library='test-library',
                slug='triangle', name='Triangle', kind='prop', subtype='mesh',
                created_at='2026-10-23T00:00:00Z', attribution='Test author', owner='Test author')
METADATA = dict(description='Authored test geometry', category='geometry',
                author='Test author', publisher='Test author', source_url='local:test-fixture',
                license='CC0-1.0', allowed_uses=['test'], keywords=[], style_refs=[],
                capabilities=[], review_status='unreviewed')
STATIC_OBJ = (b'# exact source with CRLF and tab indentation\r\n'
              b'o Panel\r\n\tv\t0 0 0\r\nv 2 0 0\r\nv 2 1 0\r\nv 0 1 0\r\n'
              b'vt 0 0\r\nvt 1 0\r\nvt 1 1\r\nvt 0 1\r\nvt 0.25 0.5\r\n'
              b'vn 0 0 1\r\ns off\r\n'
              b'f 1/1/1 2/2/1 3/3/1\r\nf -4/5/1 -2/3/1 -1/4/1\r\n')


@unittest.skipUnless(shutil.which('node') and (VALIDATOR / 'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class NativeImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library_path = self.root / 'library'
        self.library = AssetLibrary(self.library_path, create=True)
        self.library.upgrade_storage()
        self.pipeline = ImportPipeline(self.library)
        self.license = self.root / 'license.txt'
        self.license.write_bytes(b'Engineering test fixture. CC0-1.0.\n')

    def import_source(self, path, **kwargs):
        return self.pipeline.import_asset(path, identity=IDENTITY, version='1.0.0',
                                          metadata=METADATA, license_file=self.license, **kwargs)

    def external_source(self):
        source = self.root / 'fixture.gltf'
        (self.root / 'resources').mkdir()
        buffer = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
        (self.root / 'resources/triangle.bin').write_bytes(buffer)
        image = BytesIO()
        Image.new('RGB', (2, 2), 'red').save(image, format='PNG')
        (self.root / 'resources/paint.png').write_bytes(image.getvalue())
        value = dict(asset=dict(version='2.0'), scene=0, scenes=[dict(nodes=[0])],
                     nodes=[dict(mesh=0)], meshes=[dict(primitives=[
                         dict(attributes=dict(POSITION=0, TEXCOORD_0=1), material=0)])],
                     buffers=[dict(byteLength=len(buffer), uri='resources/triangle.bin')],
                     bufferViews=[dict(buffer=0, byteLength=len(buffer), target=34962)],
                     accessors=[dict(bufferView=0, componentType=5126, count=3, type='VEC3',
                                     min=[0, 0, 0], max=[1, 1, 0]),
                                dict(componentType=5126, count=3, type='VEC2')],
                     images=[dict(uri='resources/paint.png')], textures=[dict(source=0)],
                     materials=[dict(pbrMetallicRoughness=dict(baseColorTexture=dict(index=0)))])
        source.write_text(json.dumps(value), encoding='utf-8')
        return source

    def test_external_gltf_is_normalized_and_reopens_with_exact_sources(self):
        path = self.external_source()
        probe = self.pipeline.probe(path)
        self.assertFalse(probe['geometry_validated'])
        self.assertEqual({entry['path']: entry['roles'] for entry in probe['files']},
                         {'model.gltf': ['source'], 'resources/paint.png': ['texture'],
                          'resources/triangle.bin': ['buffer']})
        sources = {'source/model.gltf': path.read_bytes(), 'license.txt': self.license.read_bytes()}
        for name in ('resources/paint.png', 'resources/triangle.bin'):
            sources['source/' + name] = (self.root / name).read_bytes()
        scale = 1.000000001
        reference = self.import_source(path, fixture=True, unit_scale=scale)
        path.unlink()
        shutil.rmtree(self.root / 'resources')
        self.license.unlink()
        reopened = AssetLibrary(self.library_path)
        manifest = reopened.resolve(reference)
        self.assertEqual(len(manifest['provenance']['sources']), 3)
        self.assertEqual(manifest['previews'], [])
        self.assertTrue(manifest['provenance']['fixture'])
        for logical_path, original in sources.items():
            self.assertEqual(reopened.read_file(reference, logical_path), original)
        report = resolve_import(reopened, reference)
        self.assertEqual(report['source_sha256'], hashlib.sha256(sources['source/model.gltf']).hexdigest())
        output = reopened.read_file(reference, 'normalized.glb')
        self.assertEqual(report['normalized_sha256'], hashlib.sha256(output).hexdigest())
        self.assertEqual(manifest['provenance']['conversions'][0]['outputs'][0]['sha256'],
                         report['normalized_sha256'])
        self.assertFalse(report['diagnostic']['visual_approval'])
        self.assertEqual(report['diagnostic']['geometry'], 'base-mesh-before-morphs-and-skinning')
        self.assertIn('punctual light range', report['unchanged_metadata_lengths'])
        with Image.open(BytesIO(reopened.read_file(reference, 'diagnostic.png'))) as preview:
            self.assertEqual(preview.size, (256, 256))
        normalized = self.root / 'normalized.glb'
        normalized.write_bytes(output)
        doc = read_gltf(normalized).json
        self.assertEqual(doc['nodes'][1]['matrix'][0], scale)
        self.assertEqual(doc['images'][0]['bufferView'], 1)
        self.assertEqual(doc['textures'], [dict(source=0)])
        self.assertEqual(doc['buffers'][0].keys(), {'byteLength'})
        self.assertTrue(load_character_model(normalized).report['fixture'])

    def test_static_obj_retains_exact_bytes_and_mesh_attributes(self):
        source = self.root / 'authored.OBJ'
        payload = STATIC_OBJ
        source.write_bytes(payload)
        probe = self.pipeline.probe(source)
        self.assertEqual(probe['format'], 'obj')
        self.assertTrue(probe['geometry_validated'])
        self.assertEqual(probe['files'], [dict(path='model.obj', roles=['source'],
                         sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))])
        preflight = probe['preflight']
        self.assertEqual(preflight['obj_source_bytes'], len(payload))
        self.assertEqual(preflight['generated_binary_bytes'], 4 * (5 * (3 + 2 + 3) + 6))
        generated_json = preflight['generated_json_bytes']
        self.assertGreater(generated_json, len(payload))
        self.assertEqual(preflight['encoded_bytes'], 28 + generated_json + (-generated_json % 4)
                         + preflight['generated_binary_bytes'])
        reference = self.import_source(source, unit_scale=2, fixture=True)
        source.unlink()
        reopened = AssetLibrary(self.library_path)
        manifest = reopened.resolve(reference)
        self.assertEqual(manifest['compatibility']['formats'], ['glb', 'obj'])
        self.assertEqual(manifest['provenance']['sources'][0]['format'], 'obj')
        self.assertEqual(manifest['provenance']['source_records'][0]['original_filename'], 'authored.OBJ')
        self.assertEqual(reopened.read_file(reference, 'source/model.obj'), payload)
        self.assertNotIn('source-khronos.json', manifest['publication']['inventory'])
        report = resolve_import(reopened, reference)
        self.assertEqual(report['source_validation'], 'native-geometry')
        self.assertEqual(report['preflight'], preflight)
        self.assertEqual(report['source_sha256'], hashlib.sha256(payload).hexdigest())
        output = reopened.read_file(reference, 'normalized.glb')
        validation = json.loads(reopened.read_file(reference, 'normalized-khronos.json'))
        self.assertEqual(validation['validator'], '2.0.0-dev.3.10')
        self.assertTrue(validation['ok'])
        self.assertEqual(validation['issues']['numErrors'], 0)
        self.assertEqual(validation['asset_sha256'], hashlib.sha256(output).hexdigest())
        normalized = self.root / 'obj.glb'
        normalized.write_bytes(output)
        document = read_gltf(normalized)
        primitive = document.json['meshes'][0]['primitives'][0]
        self.assertEqual(document.accessor(primitive['indices']).ravel().tolist(), [0, 1, 2, 3, 2, 4])
        self.assertEqual(set(primitive['attributes']), {'POSITION', 'TEXCOORD_0', 'NORMAL'})
        accessors = {name: document.accessor(index).tolist()
                     for name, index in primitive['attributes'].items()}
        self.assertEqual(accessors['POSITION'], [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 0, 0], [0, 1, 0]])
        self.assertEqual(accessors['TEXCOORD_0'], [[0, 1], [1, 1], [1, 0], [0.25, 0.5], [0, 0]])
        self.assertEqual(accessors['NORMAL'], [[0, 0, 1]] * 5)
        model = load_character_model(normalized)
        self.assertEqual(model.geometry.meshes[0].positions.max(axis=0).tolist(), [4, 2, 0])
        self.assertEqual(model.geometry.triangle_count, 2)
        self.assertFalse(model.clips)

    def test_static_obj_quad_omits_unsupplied_attributes(self):
        source = self.root / 'quad.obj'
        source.write_bytes(b'v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n')
        reference = self.import_source(source)
        normalized = self.root / 'quad.glb'
        normalized.write_bytes(self.library.read_file(reference, 'normalized.glb'))
        document = read_gltf(normalized)
        primitive = document.json['meshes'][0]['primitives'][0]
        self.assertEqual(set(primitive['attributes']), {'POSITION'})
        self.assertEqual(document.accessor(primitive['attributes']['POSITION']).shape, (4, 3))
        self.assertEqual(document.accessor(primitive['indices']).size, 6)

    def test_static_obj_rejects_materials_and_over_budget_geometry_before_publication(self):
        source = self.root / 'mesh.obj'
        geometry = b'v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n'
        for statement in (b'mtllib palette.mtl\n', b'usemtl default\n'):
            with self.subTest(statement=statement):
                source.write_bytes(geometry + statement)
                for operation in (self.pipeline.probe, self.import_source):
                    with self.assertRaises(RigError) as error:
                        operation(source)
                    self.assertEqual(error.exception.report['code'], 'RIG_OBJ_MATERIAL')
        source.write_bytes(geometry)
        self.pipeline = ImportPipeline(self.library, limits={'triangles': 1})
        with self.assertRaises(RigError) as error:
            self.import_source(source)
        self.assertEqual(error.exception.report['code'], 'RIG_LIMIT_TRIANGLES')
        self.assertEqual(self.library.list_references(), [])

    def test_static_obj_admits_generated_graph_before_builder_allocation(self):
        source = self.root / 'groups.obj'
        groups = ''.join(f'g panel-{index}\nf 1 2 3\n' for index in range(140))
        source.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\n' + groups, encoding='utf-8')
        self.pipeline = ImportPipeline(self.library, limits={'memory_bytes': 2 * 1024**2})
        with patch('asset_library_import.build_static_gltf') as build:
            with self.assertRaises(RigError) as error:
                self.import_source(source)
        self.assertEqual(error.exception.report['code'], 'RIG_LIMIT_MEMORY')
        self.assertGreater(error.exception.report['observed'], error.exception.report['expected'])
        build.assert_not_called()
        self.assertEqual(self.library.list_references(), [])

    def test_static_obj_rejects_quoted_directives(self):
        source = self.root / 'quoted.obj'
        source.write_bytes(STATIC_OBJ.replace(b'f ', b'"f" '))
        with self.assertRaises(RigError) as error:
            self.pipeline.probe(source)
        self.assertEqual(error.exception.report['code'], 'RIG_OBJ_RECORD')

    def test_static_obj_normalizes_finite_large_normals(self):
        source = self.root / 'large-normal.obj'
        source.write_bytes(STATIC_OBJ.replace(b'vn 0 0 1', b'vn 0 0 1e300'))
        self.assertTrue(self.pipeline.probe(source)['geometry_validated'])
        reference = self.import_source(source)
        normalized = self.root / 'normal.glb'
        normalized.write_bytes(self.library.read_file(reference, 'normalized.glb'))
        document = read_gltf(normalized)
        attributes = document.json['meshes'][0]['primitives'][0]['attributes']
        self.assertEqual(document.accessor(attributes['NORMAL']).tolist(), [[0, 0, 1]] * 5)

    def test_static_obj_checks_caller_deadline_after_triangulation(self):
        from rig_obj import triangulate_polygon

        source = self.root / 'timed.obj'
        source.write_bytes(b'v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n')
        self.pipeline = ImportPipeline(self.library, limits={'timeout_seconds': 1})
        clock = 10

        def advance_on_triangulation(points):
            nonlocal clock
            clock = 11
            return triangulate_polygon(points)

        with patch('rig_obj.monotonic', side_effect=lambda: clock), patch(
                'rig_obj.triangulate_polygon', side_effect=advance_on_triangulation) as triangulate:
            with self.assertRaises(RigError) as error:
                self.pipeline.probe(source)
        triangulate.assert_called_once()
        self.assertEqual(error.exception.report['code'], 'RIG_OBJ_TIMEOUT')

    def test_real_animated_glb_keeps_skin_morph_material_and_animation_records(self):
        path = ROOT / 'examples/rig-3d/humanoid/exports/character.glb'
        reference = self.import_source(path, unit_scale=2.0)
        original = read_gltf(path).json
        normalized = self.root / 'normalized.glb'
        normalized.write_bytes(self.library.read_file(reference, 'normalized.glb'))
        output = read_gltf(normalized).json
        for category in ('skins', 'animations', 'materials', 'textures', 'meshes'):
            self.assertEqual(original.get(category), output.get(category))
        self.assertTrue(original['skins'])
        self.assertTrue(original['animations'])
        self.assertTrue(any('targets' in primitive for mesh in original['meshes']
                            for primitive in mesh['primitives']))
        report = resolve_import(self.library, reference)
        self.assertGreater(report['native_observation']['morph_targets'], 0)
        self.assertTrue(report['native_observation']['clips'])

    def test_missing_resource_and_invalid_units_leave_library_empty(self):
        path = self.external_source()
        (self.root / 'resources/paint.png').unlink()
        with self.assertRaises(RigError) as error:
            self.import_source(path)
        self.assertEqual(error.exception.report['code'], 'LIBRARY_SOURCE')
        with self.assertRaises(RigError) as error:
            self.import_source(path, unit_scale=10**400)
        self.assertEqual(error.exception.report['code'], 'ASSET_IMPORT_SCALE')
        self.assertEqual(self.library.list_references(), [])

    def test_unavailable_independent_validator_leaves_library_empty(self):
        path = self.external_source()
        with patch.dict(os.environ, HATCH_GLTF_VALIDATOR_ROOT=str(self.root / 'missing')):
            with self.assertRaises(RigError) as error:
                self.import_source(path)
        self.assertEqual(error.exception.report['code'], 'RIG_VALIDATOR_MISSING')
        self.assertEqual(self.library.list_references(), [])

    def test_required_unavailable_extension_and_malformed_record_are_typed(self):
        path = self.external_source()
        value = json.loads(path.read_bytes())
        value['extensionsUsed'] = value['extensionsRequired'] = ['EXT_meshopt_compression']
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(RigError) as error:
            self.pipeline.probe(path)
        self.assertEqual(error.exception.report['code'], 'RIG_GLTF_EXTENSION')
        del value['extensionsUsed'], value['extensionsRequired']
        value['asset'] = []
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(RigError) as error:
            self.pipeline.probe(path)
        self.assertEqual(error.exception.report['code'], 'ASSET_IMPORT_FORMAT')
        self.assertEqual(self.library.list_references(), [])

    def test_projected_output_and_combined_memory_fail_before_writing_or_validation(self):
        source = self.external_source()
        closure_bytes = sum(entry['bytes'] for entry in self.pipeline.probe(source)['files'])
        cases = (
            (dict(source_bytes=source.stat().st_size), 'ASSET_IMPORT_LIMIT'),
            (dict(dependency_bytes=closure_bytes), 'ASSET_IMPORT_LIMIT'),
            (dict(memory_bytes=2 * 1024**2 + 100_000), 'RIG_LIMIT_MEMORY'),
        )
        for limits, code in cases:
            with self.subTest(limits=limits):
                self.pipeline = ImportPipeline(self.library, limits=limits)
                with patch('asset_library_import._write_glb') as write, \
                     patch('asset_library_import.validate_gltf') as validator:
                    with self.assertRaises(RigError) as error:
                        self.import_source(source, unit_scale=2.0, fixture=True)
                self.assertEqual(error.exception.report['code'], code)
                write.assert_not_called()
                validator.assert_not_called()
        self.assertEqual(self.library.list_references(), [])

    def test_postcommit_cleanup_does_not_fail_import_after_canonical_registration(self):
        source = self.external_source()
        cleanup = tempfile.TemporaryDirectory.cleanup

        def cleanup_error(directory):
            cleanup(directory)
            raise OSError('synthetic temporary cleanup failure')

        with patch.object(tempfile.TemporaryDirectory, 'cleanup', autospec=True, side_effect=cleanup_error):
            with patch.object(self.library, 'resolve', side_effect=OSError('synthetic resolution failure')) as resolve:
                reference = self.import_source(source)
                resolve.assert_not_called()
        reopened = AssetLibrary(self.library_path)
        self.assertEqual(reopened.list_references(), [reference])
        self.assertTrue(reopened.read_file(reference, 'normalized.glb').startswith(b'glTF'))

    def test_source_license_and_validator_failures_do_not_disclose_private_paths(self):
        source = self.external_source()
        marker = 'private-path-marker'
        missing_path = self.root / marker / 'missing.gltf'
        try:
            self.pipeline.probe(missing_path)
        except RigError as error:
            self.assertEqual(error.report['code'], 'LIBRARY_SOURCE')
            self.assertNotIn(marker, traceback.format_exc())
            self.assertNotIn(str(self.root), json.dumps(error.report))
        else:
            self.fail('Missing private source must fail')

        license = self.license
        self.license = self.root / marker / 'license.txt'
        with self.assertRaises(RigError) as error:
            self.import_source(source)
        self.assertEqual(error.exception.report['code'], 'LIBRARY_SOURCE')
        self.assertEqual(error.exception.report['asset'], 'license evidence')
        self.assertNotIn(marker, json.dumps(error.exception.report))
        self.license = license

        synthetic = RigError('RIG_VALIDATOR_MISSING', f'Cannot open {missing_path}',
                             asset=str(missing_path), observed=f'Module stack: {missing_path}')
        with patch('asset_library_import.validate_gltf', side_effect=synthetic):
            with self.assertRaises(RigError) as error:
                self.import_source(source)
        self.assertEqual(error.exception.report['code'], 'RIG_VALIDATOR_MISSING')
        self.assertNotIn(marker, json.dumps(error.exception.report))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual(self.library.list_references(), [])

    @unittest.skipUnless(
        os.environ.get('HATCH_BLENDER') and hasattr(os, 'wait4') and Path('/usr/bin/prlimit').is_file(),
        'Requires pinned Blender 4.5.3 on Linux with prlimit',
    )
    def test_blender_fixture_retains_original_and_two_verified_derivatives(self):
        with patch('asset_import_blender.os.killpg', wraps=os.killpg) as signal_group:
            reference = self.pipeline.import_blender_fixture(
                identity=IDENTITY, version='1.0.0', metadata=METADATA, license_file=self.license)
        signal_group.assert_not_called()
        reopened = AssetLibrary(self.library_path)
        manifest = reopened.resolve(reference)
        provenance = manifest['provenance']
        report = resolve_import(reopened, reference)
        original = provenance['sources'][0]
        blender, normalization = provenance['conversions']
        self.assertTrue(provenance['fixture'])
        self.assertEqual(original['path'], 'source/fixture.blend')
        self.assertEqual(original['role'], 'source')
        self.assertEqual(original['format'], 'binary')
        self.assertEqual(blender['inputs'], [original])
        self.assertEqual(normalization['inputs'], blender['outputs'])
        self.assertEqual(report['blender']['inspection']['build'], '67807e1800cc')
        self.assertEqual(report['blender']['inspection']['external_dependencies'], 0)
        for stage in ('create', 'reopen'):
            process = report['blender'][stage]
            self.assertEqual(process['peak_rss_source'], 'linux-wait4')
            self.assertGreater(process['peak_rss_bytes'], 0)
            self.assertLessEqual(process['peak_rss_bytes'], process['resident_budget_bytes'])
            self.assertEqual(process['status'], 'completed')
            self.assertEqual(process['exit_code'], 0)
            self.assertEqual(process['argv'][-4:], ['--python', '<worker>', '--', stage])
            self.assertIn('<blender>', process['argv'])
            self.assertEqual(process['cwd'], '<workspace>')
        self.assertNotIn(b'asset-gltf-import-', reopened.read_file(reference, original['path']))
        self.assertEqual(report['native_observation']['triangles'], 1)
        self.assertEqual(report['native_observation']['vertices'], 3)
        for record, digest, magic in (
            (original, report['blender']['source_sha256'], b'BLENDER'),
            (blender['outputs'][0], report['blender']['derivative_sha256'], b'glTF'),
            (normalization['outputs'][0], report['normalized_sha256'], b'glTF'),
        ):
            payload = reopened.read_file(reference, record['path'])
            self.assertTrue(payload.startswith(magic))
            self.assertEqual(hashlib.sha256(payload).hexdigest(), record['sha256'])
            self.assertEqual(record['sha256'], digest)
        for name in ('source-khronos.json', 'normalized-khronos.json'):
            validation = json.loads(reopened.read_file(reference, name))
            self.assertEqual(validation['validator'], '2.0.0-dev.3.10')
            self.assertTrue(validation['ok'])

    def test_regular_import_rejects_blender_paths_without_worker(self):
        with patch('asset_import_blender.create_fixture') as worker:
            with self.assertRaises(RigError) as error:
                self.import_source(self.root / 'unknown.blend', fixture=True)
            self.assertEqual(error.exception.report['code'], 'SCENE3D_IMPORT_FORMAT')
            worker.assert_not_called()
        self.assertEqual(self.library.list_references(), [])

    @unittest.skipUnless(
        os.environ.get('HATCH_BLENDER') and hasattr(os, 'wait4') and Path('/usr/bin/prlimit').is_file(),
        'Requires pinned Blender 4.5.3 on Linux with prlimit and wait4',
    )
    def test_blender_final_peak_rss_is_admitted_before_publication(self):
        system_wait4 = os.wait4
        budget = self.pipeline.limits['memory_bytes']

        def excessive_peak(pid, flags):
            child, status, usage = system_wait4(pid, flags)
            if child == pid:
                usage = SimpleNamespace(ru_maxrss=budget // 1024 + 1)
            return child, status, usage

        with patch('asset_import_blender.os.wait4', side_effect=excessive_peak):
            with self.assertRaises(RigError) as error:
                self.pipeline.import_blender_fixture(
                    identity=IDENTITY, version='1.0.0', metadata=METADATA, license_file=self.license)
        self.assertEqual(error.exception.report['code'], 'RIG_LIMIT_MEMORY')
        self.assertEqual(error.exception.report['observed'], budget + 1024)
        self.assertEqual(self.library.list_references(), [])

    @unittest.skipUnless(
        os.environ.get('HATCH_BLENDER') and hasattr(os, 'wait4') and Path('/usr/bin/prlimit').is_file(),
        'Requires pinned Blender 4.5.3 on Linux with prlimit and wait4',
    )
    def test_blender_already_reaped_child_is_never_signaled(self):
        system_wait4 = os.wait4

        def child_reaped_elsewhere(pid, flags):
            result = system_wait4(pid, flags)
            if result[0] == pid:
                raise ChildProcessError('Child already reaped')
            return result

        with (patch('asset_import_blender.os.wait4', side_effect=child_reaped_elsewhere),
              patch('asset_import_blender.os.killpg', wraps=os.killpg) as signal_group):
            with self.assertRaises(RigError) as error:
                self.pipeline.import_blender_fixture(
                    identity=IDENTITY, version='1.0.0', metadata=METADATA, license_file=self.license)
        self.assertEqual(error.exception.report['code'], 'ASSET_BLENDER_RSS')
        signal_group.assert_not_called()
        self.assertEqual(self.library.list_references(), [])
