"""Actual native format roundtrips plus canonical public-reader contract tests."""
from pathlib import Path
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_mesh_import import import_mesh, import_canonical
from rig_errors import RigError
from rig_gltf import export_geometry
from rig_models import Geometry, Mesh

class MeshImportTests(unittest.TestCase):
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
        from rig_errors import open_payload
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root/'real'; real.mkdir()
            (real/'bytes.bin').write_bytes(b'unchanged')
            alias = root/'alias'
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest('This host cannot create symlinks.')
            with open_payload(alias, 'bytes.bin') as (stream, info):
                self.assertEqual(stream.read(), b'unchanged')
            (real/'child.bin').symlink_to(real/'bytes.bin')
            with self.assertRaises(RigError):
                with open_payload(alias, 'child.bin'):
                    pass

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
    def test_invalid_paths_counts_and_geometry(self):
        p=self.root/'bad.obj';p.write_text('v 0 0 0\nf 1 2 3\n')
        with self.assertRaises(RigError):import_mesh(p)
        for value in [True,3.5,float('inf')]:
            with self.assertRaises(RigError):import_mesh(p,limits={'vertices':value})
        p.write_text('mtllib ../escape.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        with self.assertRaises(RigError):import_mesh(p)
    def test_real_existing_skin_morph_animation_identity(self):
        from rig_import import load_character_model
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
        def racing(root,name):
            with original(root,name) as (stream,info):
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


if __name__=='__main__':unittest.main()
