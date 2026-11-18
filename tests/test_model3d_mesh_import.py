"""Actual native format roundtrips plus canonical public-reader contract tests."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_mesh_import import import_mesh, import_canonical
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from rig_gltf import export_geometry
from rig_import import load_character_model
from rig_jobs import create_run
from rig_models import Geometry, Mesh
import test_rig_asset_library_import as native_import_fixture

class MeshImportTests(unittest.TestCase):
    def test_replaced_source_root_is_rejected_before_reading_redirected_bytes(self):
        from contextlib import contextmanager
        import rig_errors
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root/'source'; source.mkdir()
            outside = root/'outside'; outside.mkdir()
            data = b'o mesh\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
            (source/'mesh.obj').write_bytes(data)
            (outside/'mesh.obj').write_bytes(data.replace(b'v 1 0 0', b'v 9 0 0'))
            original = rig_errors.open_payload
            @contextmanager
            def redirect(path, name, **kwargs):
                source.rename(root/'original')
                try:
                    source.symlink_to(outside, target_is_directory=True)
                except OSError:
                    (root/'original').rename(source)
                    self.skipTest('Directory symlinks unavailable on this host.')
                with original(path, name, **kwargs) as value:
                    yield value
            with patch.object(rig_errors, 'open_payload', redirect):
                with self.assertRaises(RigError) as caught:
                    import_mesh(source/'mesh.obj')
            self.assertEqual(caught.exception.report['code'], 'RIG_PAYLOAD_CHANGED')
            self.assertEqual((root/'original/mesh.obj').read_bytes(), data)

    def test_deep_hierarchy_returns_structured_bounded_failure(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            payload = dict(asset={'version': '2.0'}, scene=0, scenes=[{'nodes': [0]}],
                           nodes=[{'children': [i+1]} for i in range(1100)] + [{}])
            source = root/'deep.gltf'
            source.write_text(json.dumps(payload))
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/model3d.py'),
                                     'mesh', 'import', '--file', str(source), '--destination', str(root/'out')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(json.loads(result.stderr)['error']['code'], 'RIG_LIMIT_DEPTH')
            self.assertFalse((root/'out').exists())

    def test_trusted_root_alias_is_canonicalized_without_accepting_child_links(self):
        if not hasattr(os, 'symlink'):
            self.skipTest('Symlinks are unavailable.')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root/'real'; real.mkdir()
            model = real/'mesh.glb'
            export_geometry(self.geometry(), model)
            digest = hashlib.sha256(model.read_bytes()).hexdigest()
            alias = root/'alias'
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest('This host cannot create symlinks.')
            with import_mesh(alias/model.name) as captured:
                self.assertEqual(captured.model.geometry.vertex_count, 3)
                self.assertEqual(captured.source_records[0]['sha256'], digest)
            (real/'child.glb').symlink_to(model)
            with self.assertRaises(RigError):
                import_mesh(alias/'child.glb')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
    def geometry(self):
        return Geometry([Mesh('left',np.array([[0.,0,0],[1,0,0],[0,2,0]]),np.array([[0,1,2]]),name='Left hand')],[{}])
    def test_real_glb_gltf_snapshot_bounds_and_original_hash(self):
        for suffix in ('.glb','.gltf'):
            p=self.root/('triangle'+suffix);export_geometry(self.geometry(),p)
            before=p.read_bytes()
            with import_mesh(p,limits={'vertices':3.0}) as result:
                self.assertEqual(result.model.geometry.vertex_count,3)
                self.assertEqual(result.model.geometry.meshes[0].name,'Left hand')
                np.testing.assert_allclose(result.model.report['dimensions_m'],[1,2,0])
                record=next(r for r in result.source_records if r['path']==p.name)
                self.assertEqual(record['sha256'],hashlib.sha256(before).hexdigest())
                self.assertTrue(all(f.is_file() for f in result.model.source_files))
                private=result.root
            self.assertFalse(private.exists());self.assertEqual(p.read_bytes(),before)
    def test_obj_named_concave_original_and_scale(self):
        p=self.root/'mesh.obj';p.write_bytes(b'o left_hand\nv 0 0 0\nv 2 0 0\nv 2 2 0\nv 1 1 0\nv 0 2 0\nf 1 2 3 4 5\n')
        before=p.read_bytes()
        with import_mesh(p,scale=.01) as result:
            self.assertEqual(result.model.geometry.meshes[0].name,'left_hand')
            self.assertEqual(result.model.geometry.triangle_count,3)
            np.testing.assert_allclose(result.model.report['dimensions_m'],[.02,.02,0])
        self.assertEqual(p.read_bytes(),before)

    def test_obj_direct_source_cap_accepts_integral_float(self):
        obj = self.root/'mesh.obj'
        obj.write_bytes(b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        model = load_character_model(obj, limits={**DEFAULT_LIMITS, 'source_bytes': 96.0})
        self.assertEqual(model.geometry.triangle_count, 1)
        with self.assertRaises(RigError) as caught:
            load_character_model(obj, limits={**DEFAULT_LIMITS, 'source_bytes': 96.5})
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_SOURCE')

    def test_obj_dependency_budget_diagnostics_in_direct_and_managed_import(self):
        obj = self.root/'mesh.obj'
        obj.write_bytes(b'mtllib library\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        mtl = self.root/'library'
        texture = self.root/'texture'
        destination = self.root/'run'
        cases = (
            ('RIG_LIMIT_MTL', b'#' + b' ' * 1024**2, b'', {}),
            ('RIG_LIMIT_MTL', b'#' + b' ' * 96, b'', {'source_bytes': 96}),
            ('RIG_LIMIT_TEXTURE', b'newmtl color\nmap_Kd texture\n', b'x' * 97, {'source_bytes': 96}),
            ('RIG_LIMIT_DEPENDENCIES', b'# library\n', b'', {'dependency_bytes': obj.stat().st_size + 9}),
        )
        operations = {
            'direct': lambda limits: load_character_model(obj, limits={**DEFAULT_LIMITS, **limits}),
            'managed': lambda limits: create_run(obj, destination, limits=limits),
        }
        for code, material_data, texture_data, limits in cases:
            mtl.write_bytes(material_data)
            texture.write_bytes(texture_data)
            for name, operation in operations.items():
                with self.subTest(code=code, limits=limits, operation=name):
                    with self.assertRaises(RigError) as caught:
                        operation(limits)
                    self.assertEqual(caught.exception.report['code'], code)
                    self.assertFalse(destination.exists())

    def test_obj_empty_mtl_is_admitted_and_retained_verbatim(self):
        obj = self.root/'mesh.obj'
        obj.write_bytes(b'mtllib library\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        mtl = self.root/'library'
        mtl.write_bytes(b'')
        limits = {'dependency_bytes': obj.stat().st_size}
        direct = load_character_model(obj, limits={**DEFAULT_LIMITS, **limits})
        self.assertEqual(direct.geometry.triangle_count, 1)
        with import_mesh(obj, limits=limits) as snapshot:
            self.assertEqual(snapshot.model.geometry.triangle_count, 1)
        managed = create_run(obj, self.root/'run')
        self.assertEqual(managed.geometry.triangle_count, 1)
        self.assertEqual((managed.root/'source/library').read_bytes(), b'')
        self.assertIn({'path': 'source/library', 'sha256': hashlib.sha256(b'').hexdigest()},
                      managed.manifest['source']['dependencies'])

    def test_snapshot_rejects_empty_entry_and_texture(self):
        obj = self.root/'mesh.obj'
        obj.write_bytes(b'')
        with self.assertRaises(RigError) as caught:
            import_mesh(obj)
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_SOURCE')

        obj.write_bytes(b'mtllib library\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        (self.root/'library').write_bytes(b'newmtl color\nmap_Kd texture\n')
        (self.root/'texture').write_bytes(b'')
        with self.assertRaises(RigError) as caught:
            import_mesh(obj)
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_SOURCE')

    def test_snapshot_missing_parent_keeps_structured_source_error(self):
        obj = self.root/'absent/mesh.obj'
        with self.assertRaises(RigError) as caught:
            create_run(obj, self.root/'run')
        self.assertEqual(caught.exception.report['code'], 'RIG_SOURCE')
        self.assertFalse((self.root/'run').exists())

    def test_invalid_paths_counts_and_geometry(self):
        p=self.root/'bad.obj';p.write_text('v 0 0 0\nf 1 2 3\n')
        with self.assertRaises(RigError):import_mesh(p)
        for value in [True,3.5,float('inf')]:
            with self.assertRaises(RigError):import_mesh(p,limits={'vertices':value})
        p.write_text('mtllib ../escape.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        with self.assertRaises(RigError):import_mesh(p)
    def test_real_existing_skin_morph_animation_identity(self):
        source=Path(__file__).resolve().parents[1]/'examples/rig-3d/winged-creature/exports/character.glb'
        # Copy authentic retained fixture bytes into a new entry directory.
        p=self.root/'rig.glb';p.write_bytes(source.read_bytes())
        expected=load_character_model(p)
        self.assertTrue(expected.skeleton)
        self.assertTrue(expected.weights)
        self.assertTrue(expected.clips)
        with import_mesh(p) as result:
            self.assertEqual(result.model.skeleton,expected.skeleton)
            self.assertEqual(set(result.model.clips),set(expected.clips))
            self.assertEqual(set(result.model.weights),set(expected.weights))
            for name,weight in expected.weights.items():
                np.testing.assert_array_equal(result.model.weights[name].weights,weight.weights)
                np.testing.assert_array_equal(result.model.weights[name].inverse_bind,weight.inverse_bind)
            for actual,wanted in zip(result.model.geometry.meshes,expected.geometry.meshes):
                self.assertEqual((actual.id,actual.name,actual.source_node),(wanted.id,wanted.name,wanted.source_node))
                self.assertEqual(set(actual.morphs),set(wanted.morphs))
                np.testing.assert_array_equal(actual.positions,wanted.positions)
            self.assertEqual(result.source_records[0]['sha256'],hashlib.sha256(source.read_bytes()).hexdigest())

    def test_controlled_reader_preserves_animation_targets_and_decoded_source_hash(self):
        fixture = Path(__file__).resolve().parents[1]/'examples/rig-3d/winged-creature/exports/character.glb'
        original = fixture.read_bytes()
        source = self.root/'animated.glb'
        source.write_bytes(original)
        expected_targets = {}
        load_character_model(source, _animation_targets=expected_targets)
        self.assertTrue(expected_targets['bones'])
        self.assertTrue(expected_targets['meshes'])

        def capture(path, maximum):
            self.assertEqual(path, source)
            self.assertLessEqual(len(original), maximum)
            source.write_bytes(b'Changed after capture')
            return original

        targets = {}
        model = load_character_model(source, source_reader=capture, _animation_targets=targets)
        digest = hashlib.sha256(original).hexdigest()
        self.assertEqual(targets, expected_targets)
        self.assertEqual(model.report['source_sha256'], digest)
        self.assertTrue(model.clips)
        for clip in model.clips.values():
            self.assertEqual(clip['provenance']['input_hashes']['source'], digest)

    def test_original_mutation_after_capture_does_not_change_geometry(self):
        import rig_import
        p=self.root/'mesh.obj';p.write_bytes(b'o mesh\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        original=p.read_bytes();native=rig_import.load_character_model
        def mutate(path,**kwargs):
            result=native(path,**kwargs)
            if Path(path)==p:
                p.write_bytes(b'o changed\nv 0 0 0\nv 9 0 0\nv 0 1 0\nf 1 2 3\n')
            return result
        with patch.object(rig_import,'load_character_model',side_effect=mutate):
            with import_mesh(p) as result:
                np.testing.assert_allclose(result.model.report['dimensions_m'],[1,1,0])
                self.assertEqual(result.source_records[0]['sha256'],hashlib.sha256(original).hexdigest())
                self.assertEqual((result.root/'mesh.obj').read_bytes(),original)
        self.assertNotEqual(p.read_bytes(),original)

    def canonical(self):
        from asset_library import AssetLibrary
        root=Path(__file__).resolve().parents[1]/'examples/asset-library-foundation'
        manifest=json.loads((root/'manifest.json').read_text())
        library=AssetLibrary(self.root/'library',create=True)
        ref=library.register(manifest,{f['path']:root/f['path'] for f in manifest['files']})
        return library,manifest,ref

    def test_real_canonical_library_roundtrip_and_pre_read_caps(self):
        library,manifest,ref=self.canonical()
        with import_canonical(library,ref) as result:
            self.assertEqual(result.provenance['canonical_ref'],ref)
            self.assertEqual(result.provenance['manifest'],manifest)
            self.assertEqual(result.model.geometry.vertex_count,3)
            self.assertEqual(result.provenance['manifest']['identity']['namespace'],'fixtures')
            self.assertEqual({r['path'] for r in result.source_records},{f['path'] for f in manifest['files']})
        with patch.object(library,'read_file',wraps=library.read_file) as read:
            with self.assertRaises(RigError):import_canonical(library,ref,limits={'source_bytes':1})
            read.assert_not_called()
        with patch.object(library,'read_file',return_value=b'corrupt'):
            with self.assertRaises(RigError):import_canonical(library,ref)

    def test_manual_normalized_filename_keeps_legacy_explicit_entry(self):
        from asset_library import file_record
        from asset_library_contracts import seal
        from asset_library_import import TOOL, VERSION

        library, manifest, ref = self.canonical()
        license_path = self.root/'license.txt'
        license_path.write_bytes(library.read_file(ref, 'source/license.txt'))
        mesh_path = self.root/'normalized.glb'
        export_geometry(self.geometry(), mesh_path)
        mesh = file_record(mesh_path, 'normalized.glb', 'payload')
        manifest['files'][0] = mesh
        manifest['provenance']['sources'] = [mesh]
        manifest['compatibility']['formats'] = ['glb']
        manifest['publication']['inventory'][0] = mesh['path']
        paths = {'normalized.glb': mesh_path, 'source/license.txt': license_path}

        manifest['version'] = '1.1.0'
        legacy = library.register(seal(manifest), paths)
        with import_canonical(library, legacy, entry=mesh['path']) as result:
            self.assertEqual(result.model.geometry.triangle_count, 1)
            self.assertEqual(result.provenance['source_entry'], mesh['path'])
            self.assertNotIn('source_unit_scale', result.provenance)
        with self.assertRaises(RigError) as caught:
            import_canonical(library, legacy)
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_ENTRY')

        manifest['version'] = '1.2.0'
        manifest['compatibility']['unit_scale'] = 2
        nonunit = library.register(seal(manifest), paths)
        with self.assertRaises(RigError) as caught:
            import_canonical(library, nonunit, entry=mesh['path'])
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

        manifest['version'] = '1.3.0'
        manifest['compatibility']['unit_scale'] = 1
        manifest['provenance']['tools'] = [dict(name=TOOL, version=VERSION)]
        claimed = library.register(seal(manifest), paths)
        with self.assertRaises(RigError) as caught:
            import_canonical(library, claimed, entry=mesh['path'])
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_NORMALIZATION')

        manifest['version'] = '1.4.0'
        runtime = dict(mesh, role='runtime')
        manifest['files'][0] = runtime
        manifest['provenance']['sources'] = [runtime]
        manifest['provenance']['tools'] = []
        manifest['provenance']['conversions'] = [
            dict(tool='Example Offline Converter', version='1', timestamp=manifest['identity']['created_at'],
                 operation='Pack and normalize GLB', inputs=[runtime], outputs=[runtime])
        ]
        custom = library.register(seal(manifest), paths)
        for entry in (None, runtime['path']):
            with self.subTest(entry=entry), import_canonical(library, custom, entry=entry) as result:
                self.assertEqual(result.model.geometry.triangle_count, 1)
                self.assertNotIn('source_unit_scale', result.provenance)

    def test_actual_external_gltf_binary_image_closure(self):
        import base64,io
        from PIL import Image
        from rig_models import TextureImage
        stream=io.BytesIO();Image.new('RGB',(2,2),(10,20,30)).save(stream,format='PNG');png=stream.getvalue()
        geometry=self.geometry();geometry.meshes[0].attributes['TEXCOORD_0']=np.array([[0.,0],[1,0],[0,1]])
        geometry.images=[TextureImage('authentic fixture texture','image/png',png)]
        geometry.textures=[{'source':0}];geometry.materials=[{'pbrMetallicRoughness':{'baseColorTexture':{'index':0},'metallicFactor':0,'roughnessFactor':.65}}]
        entry=self.root/'external.gltf';export_geometry(geometry,entry)
        document=json.loads(entry.read_text());data=base64.b64decode(document['buffers'][0]['uri'].split(',',1)[1])
        (self.root/'buffers').mkdir();(self.root/'buffers/mesh.bin').write_bytes(data)
        document['buffers'][0]['uri']='buffers/mesh.bin'
        (self.root/'textures').mkdir();(self.root/'textures/image.png').write_bytes(png)
        for image in document['images']:
            image.pop('bufferView',None);image['uri']='textures/image.png'
        entry.write_text(json.dumps(document))
        originals={name:(self.root/name).read_bytes() for name in ['external.gltf','buffers/mesh.bin','textures/image.png']}
        with import_mesh(entry) as result:
            self.assertEqual({r['path'] for r in result.source_records},set(originals))
            self.assertEqual(result.model.geometry.images[0].data,png)
            for record in result.source_records:self.assertEqual(record['sha256'],hashlib.sha256(originals[record['path']]).hexdigest())
        for name,data in originals.items():self.assertEqual((self.root/name).read_bytes(),data)
    def test_actual_obj_mtl_image_closure(self):
        import io
        from PIL import Image
        stream=io.BytesIO();Image.new('RGB',(2,2),(10,20,30)).save(stream,format='PNG');png=stream.getvalue()
        (self.root/'texture.png').write_bytes(png)
        (self.root/'material.mtl').write_text('newmtl diffuse\nKd .2 .3 .4\nmap_Kd texture.png\n')
        entry=self.root/'external.obj';entry.write_text('mtllib material.mtl\no semantic_hand\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nusemtl diffuse\nf 1/1 2/2 3/3\n')
        originals={name:(self.root/name).read_bytes() for name in ['external.obj','material.mtl','texture.png']}
        with import_mesh(entry) as result:
            self.assertEqual({r['path'] for r in result.source_records},set(originals))
            self.assertEqual(result.model.geometry.meshes[0].name,'semantic_hand')
            self.assertEqual(result.model.geometry.images[0].data,png)
            for record in result.source_records:self.assertEqual(record['sha256'],hashlib.sha256(originals[record['path']]).hexdigest())
        for name,data in originals.items():self.assertEqual((self.root/name).read_bytes(),data)
    def test_canonical_wrong_reference_and_entry(self):
        library,manifest,ref=self.canonical()
        with self.assertRaises(RigError):import_canonical(library,dict(ref,sha256='a'*64))
        with self.assertRaises(RigError):import_canonical(library,ref,entry='../mesh.obj')

    def test_entry_and_dependency_symlinks_rejected(self):
        p=self.root/'mesh.obj';p.write_bytes(b'o mesh\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        link=self.root/'linked.obj'
        try:link.symlink_to(p)
        except OSError as exc:self.skipTest(str(exc))
        with self.assertRaises(RigError):import_mesh(link)
        (self.root/'material.mtl').symlink_to(p)
        p.write_bytes(b'mtllib material.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        with self.assertRaises(RigError):import_mesh(p)

    def test_source_budget_is_checked_before_native_parse(self):
        p=self.root/'mesh.obj';p.write_bytes(b'o mesh\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        import rig_obj
        with patch.object(rig_obj,'triangulate_polygon',side_effect=AssertionError('parsed')):
            with self.assertRaises(RigError):import_mesh(p,limits={'source_bytes':1})

    def test_mutation_during_capture_rejects_and_preserves_source(self):
        from contextlib import contextmanager
        import rig_errors
        p=self.root/'mesh.obj';p.write_bytes(b'o mesh\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        original=rig_errors.open_payload
        @contextmanager
        def racing(root,name,**kwargs):
            with original(root,name,**kwargs) as (stream,info):
                class ChangedReader:
                    def read(self,n):
                        data=stream.read(n)
                        if data:
                            with p.open('ab') as output:output.write(b'# mutation\n')
                        return data
                yield ChangedReader(),info
        with patch.object(rig_errors,'open_payload',racing),self.assertRaises(RigError):
            import_mesh(p)
        self.assertTrue(p.read_bytes().endswith(b'# mutation\n'))


@unittest.skipUnless(shutil.which('node') and (native_import_fixture.VALIDATOR/'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class NormalizedCanonicalImportTests(unittest.TestCase):
    def setUp(self):
        from asset_library import AssetLibrary
        from asset_library_import import ImportPipeline
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = AssetLibrary(self.root/'registry', create=True)
        self.library.upgrade_storage()
        self.source = native_import_fixture.NativeImportTests.external_source(self)
        self.license = self.root/'license.txt'
        self.license.write_text('Synthetic test fixture. CC0-1.0.\n')
        self.pipeline = ImportPipeline(self.library)
        self.ref = self.register('1.0.0', 2.5)

    def register(self, version, scale):
        return self.pipeline.import_asset(self.source, identity=native_import_fixture.IDENTITY, version=version,
                                          metadata=native_import_fixture.METADATA, license_file=self.license,
                                          unit_scale=scale)

    def test_real_normalized_glb_import_uses_baked_scale_once(self):
        import model3d_mesh_import

        manifest = self.library.resolve(self.ref)
        normalized = self.library.read_file(self.ref, 'normalized.glb')
        report = self.library.read_file(self.ref, 'import-report.json')
        for entry in (None, 'normalized.glb'):
            with (self.subTest(entry=entry),
                  patch.object(model3d_mesh_import, '_load', wraps=model3d_mesh_import._load) as load,
                  import_canonical(self.library, self.ref, entry=entry) as imported):
                self.assertEqual(imported.provenance['canonical_ref'], self.ref)
                self.assertEqual(imported.provenance['manifest'], manifest)
                self.assertEqual(imported.provenance['source_entry'], 'normalized.glb')
                self.assertEqual(imported.provenance['source_unit_scale'], 2.5)
                self.assertEqual(imported.provenance['import_report_sha256'], hashlib.sha256(report).hexdigest())
                self.assertEqual(imported.model.report['source_sha256'], hashlib.sha256(normalized).hexdigest())
                self.assertEqual(imported.model.report['scale_conversion'], 1)
                np.testing.assert_array_equal(imported.model.geometry.meshes[0].positions,
                                              [[0, 0, 0], [2.5, 0, 0], [0, 2.5, 0]])
                self.assertEqual({record['path'] for record in imported.source_records},
                                 {record['path'] for record in manifest['files']})
                self.assertLess(load.call_args.kwargs['limits']['memory_bytes'], DEFAULT_LIMITS['memory_bytes'])
        with patch.object(self.library, 'read_file', wraps=self.library.read_file) as reader:
            with self.assertRaises(RigError) as caught:
                import_canonical(self.library, self.ref, entry='source/model.gltf')
            self.assertEqual(caught.exception.report['code'], 'MODEL3D_COORDINATES')
            with self.assertRaises(RigError) as caught:
                import_canonical(self.library, self.ref, limits={'dependency_bytes': 1})
            self.assertEqual(caught.exception.report['code'], 'MODEL3D_BUDGET')
            with self.assertRaises(RigError) as caught:
                import_canonical(self.library, self.ref, limits={'memory_bytes': 2 * 1024**2})
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
            reader.assert_not_called()

    def test_unit_scale_native_and_existing_explicit_original_entry(self):
        ref = self.register('1.1.0', 1.0)
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.provenance['source_unit_scale'], 1)
            self.assertEqual(imported.provenance['source_entry'], 'normalized.glb')
        with import_canonical(self.library, ref, entry='source/model.gltf') as imported:
            self.assertEqual(imported.provenance['source_entry'], 'source/model.gltf')
            self.assertNotIn('source_unit_scale', imported.provenance)
            np.testing.assert_array_equal(imported.model.geometry.meshes[0].positions,
                                          [[0, 0, 0], [1, 0, 0], [0, 1, 0]])

    def test_real_glb_retains_skin_morphs_and_exact_clip_provenance(self):
        self.source = native_import_fixture.ROOT/'examples/rig-3d/humanoid/exports/character.glb'
        ref = self.register('1.2.0', 2.0)
        digest = hashlib.sha256(self.library.read_file(ref, 'normalized.glb')).hexdigest()
        with import_canonical(self.library, ref) as imported:
            self.assertTrue(imported.model.weights)
            self.assertTrue(any(mesh.morphs for mesh in imported.model.geometry.meshes))
            self.assertTrue(imported.model.clips)
            self.assertEqual(imported.provenance['normalized_sha256'], digest)
            for clip in imported.model.clips.values():
                self.assertEqual(clip['provenance']['input_hashes']['source'], digest)

    def test_rejects_report_bytes_inventory_and_scale_disagreements(self):
        from asset_library_contracts import seal

        for case, code in [('unbound-report', 'MODEL3D_HASH'), ('normalized-hash', 'MODEL3D_NORMALIZATION'),
                           ('original-filename', 'MODEL3D_NORMALIZATION'),
                           ('source-inventory', 'MODEL3D_NORMALIZATION'), ('declared-scale', 'MODEL3D_COORDINATES'),
                           ('actual-scale', 'MODEL3D_NORMALIZATION')]:
            with self.subTest(case=case):
                manifest = self.library.resolve(self.ref)
                records = {record['path']: record for record in manifest['files']}
                payloads = {name: self.library.read_file(self.ref, name) for name in records}
                report = json.loads(payloads['import-report.json'])
                if case == 'normalized-hash':
                    report['normalized_sha256'] = '0'*64
                elif case == 'original-filename':
                    report['original_filename'] = 'different.gltf'
                elif case == 'source-inventory':
                    report['source_files'].pop()
                else:
                    report['source_unit_scale'] = 3.5
                if case == 'actual-scale':
                    manifest['compatibility']['unit_scale'] = 3.5
                payload = payloads['import-report.json'] = json.dumps(report).encode()
                if case != 'unbound-report':
                    record = records['import-report.json']
                    record.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                    manifest['provenance']['conversions'][0]['outputs'][1] = record
                manifest = seal(manifest)
                ref = dict(self.ref, sha256=manifest['manifest_sha256'])
                reader = SimpleNamespace(resolve=lambda _: manifest, read_file=lambda _, name: payloads[name])
                with self.assertRaises(RigError) as caught:
                    import_canonical(reader, ref)
                self.assertEqual(caught.exception.report['code'], code)


if __name__=='__main__':unittest.main()
