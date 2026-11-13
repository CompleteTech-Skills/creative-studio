"""Real bounded asset import integration; optional rig environment required."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from asset_library import AssetLibrary
from asset_library_import import ImportPipeline, SourceSnapshot, _gltf_json, relative, integer
from rig_errors import RigError
from rig_gltf import export_gltf
from rig_test_helpers import fixture

ROOT=Path(__file__).resolve().parents[1]

class GeneratorAdmissionTests(unittest.TestCase):
    """Generator admission and actual CLI rejection, without native execution."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.library=AssetLibrary(self.root/'library',create=True)
        self.pipeline=ImportPipeline(self.library)
        self.license=self.root/'license.txt';self.license.write_bytes(b'Fixture license: CC0.\n')
        self.source=self.root/'original.bin';self.source.write_bytes(b'Original source stays untouched.')
        base=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
        self.request=dict(identity=base['identity'],version='1.0.0',metadata=base['metadata'],
            license_file=str(self.license),fixture=True,
            parameters=dict(primitive='box',dimensions=[1,2,3],color=[.2,.4,.6,1],animated=False))

    def assert_unpublished_and_original(self):
        self.assertEqual(self.license.read_bytes(),b'Fixture license: CC0.\n')
        self.assertEqual(self.source.read_bytes(),b'Original source stays untouched.')
        with sqlite3.connect(self.library.database) as db:
            for table in ('versions','blobs'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_invalid_generator_components_are_structured_before_native_execution(self):
        from unittest.mock import patch
        huge=json.loads('1'+'0'*400)
        for field in ('dimensions','color'):
            for value in (huge,-huge,True,False,'1',None,[],{},float('inf'),float('-inf'),float('nan')):
                with self.subTest(field=field,value=repr(value)):
                    request=copy.deepcopy(self.request);request['parameters'][field][0]=value
                    with patch.object(self.pipeline.blender,'execute') as execute:
                        with self.assertRaises(RigError) as caught:self.pipeline.generate(**request)
                        self.assertEqual(caught.exception.report['code'],'LIBRARY_IMPORT_REQUEST')
                        execute.assert_not_called()
                    self.assert_unpublished_and_original()

    def test_generator_outside_profile_is_rejected(self):
        for field,values in [('dimensions',(0,.000999,1000.001)),('color',(-.001,1.001))]:
            for value in values:
                with self.subTest(field=field,value=value):
                    request=copy.deepcopy(self.request);request['parameters'][field][0]=value
                    with self.assertRaises(RigError):self.pipeline.generate(**request)
                    self.assert_unpublished_and_original()

    def test_generator_endpoints_reach_native_dispatch_without_coercion(self):
        from unittest.mock import patch
        class DispatchObserved(Exception):pass
        for dimensions,color in [([.001,1000,1],[0,1,0.,1.]),([1.,2.,3.],[.2,.4,.6,1])]:
            request=copy.deepcopy(self.request)
            request['parameters'].update(dimensions=dimensions,color=color)
            with patch.object(self.pipeline.blender,'execute',side_effect=DispatchObserved) as execute:
                with self.assertRaises(DispatchObserved):self.pipeline.generate(**request)
                self.assertEqual(execute.call_args.kwargs['parameters'],request['parameters'])
            self.assert_unpublished_and_original()

    def test_actual_generate_cli_rejects_401_digit_components_without_publication(self):
        import subprocess
        import sys
        for field in ('dimensions','color'):
            with self.subTest(field=field):
                request=copy.deepcopy(self.request)
                request['parameters'][field][0]=json.loads('1'+'0'*400)
                request_path=self.root/'request.json';request_path.write_text(json.dumps(request))
                completed=subprocess.run([sys.executable,str(ROOT/'scripts/asset_library_import.py'),
                    'generate','--library',str(self.library.root),'--request',str(request_path)],
                    capture_output=True,text=True,timeout=30,cwd=ROOT)
                self.assertEqual(completed.returncode,2,completed.stderr)
                result=json.loads(completed.stdout)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['diagnostic']['code'],'LIBRARY_IMPORT_REQUEST')
                self.assertNotIn('Traceback',completed.stdout+completed.stderr)
                self.assert_unpublished_and_original()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.run=fixture(self.root)
        self.source=self.root/'source.glb';export_gltf(self.run,self.source)
        self.license=self.root/'license.txt';self.license.write_text('Synthetic engineering fixture author license CC0.\n')
        self.base=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
        self.library=AssetLibrary(self.root/'library',create=True)
        self.pipeline=ImportPipeline(self.library,blender=os.environ.get('HATCH_BLENDER'))

    def request(self,**updates):
        value=dict(identity=self.base['identity'],version='1.0.0',metadata=self.base['metadata'],license_file=self.license,fixture=True)
        value.update(updates);return value

    def external(self,**updates):
        document,binary=_gltf_json(self.source.read_bytes(),'glb')
        document['buffers'][0]['uri']='geometry.bin';document.update(updates)
        (self.root/'geometry.bin').write_bytes(binary[:document['buffers'][0]['byteLength']])
        source=self.root/'external.gltf';source.write_text(json.dumps(document));return source

    def test_external_closure_and_exact_restore(self):
        source=self.external();original={p.name:p.read_bytes() for p in (source,self.root/'geometry.bin')}
        result=self.pipeline.import_asset(source,**self.request(blender_roundtrip=False))
        restored=self.root/'restored';self.pipeline.restore_original(result['reference'],restored)
        self.assertEqual(original,{p.name:p.read_bytes() for p in restored.iterdir()})
        report=result['report'];self.assertFalse(report['native_observation']['independent'])
        self.assertEqual(report['native_observation']['vertices'],4)
        with self.assertRaises(RigError):self.pipeline.restore_original(result['reference'],restored)

    def test_units_basis_and_preserved_channels(self):
        result=self.pipeline.import_asset(self.source,**self.request(blender_roundtrip=False,unit_scale=2.,basis='right-handed-z-up',ground=True))
        document,_=_gltf_json(self.library.read_file(result['reference'],'runtime/asset.glb'),'glb')
        original,_=_gltf_json(self.source.read_bytes(),'glb')
        for key in ('skins','meshes','animations','materials'):
            self.assertEqual(document.get(key),original.get(key))
        self.assertGreater(len(document['nodes']),len(original['nodes']))
        self.assertAlmostEqual(result['report']['native_observation']['bounds'][0][1],0.,places=6)

    def test_external_actual_texture_and_missing_texture(self):
        from rig_gltf import GltfBuilder
        from PIL import Image
        import io
        image=io.BytesIO();Image.new('RGB',(2,2),(20,80,120)).save(image,format='PNG')
        builder=GltfBuilder();position=builder.accessor([[0,0,0],[1,0,0],[0,1,0]],'VEC3',bounds=True)
        uv=builder.accessor([[0,0],[1,0],[0,1]],'VEC2')
        builder.json.update(images=[dict(uri='texture.png')],textures=[dict(source=0)],
            materials=[dict(pbrMetallicRoughness=dict(baseColorTexture=dict(index=0)))],
            meshes=[dict(primitives=[dict(attributes=dict(POSITION=position,TEXCOORD_0=uv),material=0)])],
            nodes=[dict(mesh=0)],scenes=[dict(nodes=[0])])
        source=self.root/'textured.gltf';builder.write(source)
        texture=self.root/'texture.png';texture.write_bytes(image.getvalue())
        result=self.pipeline.import_asset(source,**self.request(blender_roundtrip=False))
        self.assertEqual(result['report']['native_observation']['textures'],1)
        jpeg=(ROOT/'tests/fixtures/strict-jpeg-valid.jpg').read_bytes()
        corrupt_png=bytearray(image.getvalue());corrupt_png[-1]^=1
        for version,data in [('2.0.0',bytes(corrupt_png)),('3.0.0',jpeg[:-20]+b'\xff\xd9')]:
            with self.subTest(corruption=version):
                texture.write_bytes(data)
                with self.assertRaises(RigError):self.pipeline.import_asset(source,**self.request(version=version,blender_roundtrip=False))
                self.assertEqual(texture.read_bytes(),data)
                with sqlite3.connect(self.library.database) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],1)
        texture.unlink()
        with self.assertRaises((RigError,OSError)):SourceSnapshot(source,self.root/'missing-texture')

    def test_runtime_retains_explicit_fixture_classification(self):
        from asset_library_import import document_fixture
        source=self.external();document=json.loads(source.read_text());document['asset'].pop('extras',None)
        self.assertFalse(document_fixture(document));source.write_text(json.dumps(document))
        result=self.pipeline.import_asset(source,**self.request(blender_roundtrip=False))
        runtime=self.root/'runtime.glb';runtime.write_bytes(self.library.read_file(result['reference'],'runtime/asset.glb'))
        self.assertTrue(document_fixture(_gltf_json(runtime.read_bytes(),'glb')[0]))
        with self.assertRaises(RigError):self.pipeline.import_asset(runtime,**self.request(version='1.0.1',fixture=False,blender_roundtrip=False))

    def test_source_file_directory_prefix_alias_never_publishes(self):
        from PIL import Image
        import io
        image=io.BytesIO();Image.new('RGB',(2,2),(20,80,120)).save(image,format='PNG')
        for buffer_name,image_name in [('A','a/texture.png'),('a/geometry.bin','A')]:
            with self.subTest(buffer=buffer_name,image=image_name):
                case=self.root/('case'+str(len(list(self.root.glob('case*')))));case.mkdir()
                document,binary=_gltf_json(self.source.read_bytes(),'glb')
                document['buffers'][0]['uri']=buffer_name;document['images']=[dict(uri=image_name)]
                for name,data in [(buffer_name,binary[:document['buffers'][0]['byteLength']]),(image_name,image.getvalue())]:
                    target=case/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
                source=case/'source.gltf';source.write_text(json.dumps(document))
                with self.assertRaises(RigError) as caught:self.pipeline.import_asset(source,**self.request(blender_roundtrip=False))
                self.assertEqual(caught.exception.report['code'],'LIBRARY_IMPORT_ALIAS')
                with sqlite3.connect(self.library.database) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)

    def test_obj_freeform_directives_rejected_before_blender(self):
        source=self.root/'freeform.obj'
        source.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\nv 10 0 0\nv 11 0 0\nv 12 1 0\nv 13 0 0\ncstype bezier\ndeg 3\ncurv 0 1 4 5 6 7\nparm u 0 1\nend\n')
        with self.assertRaisesRegex(RigError,'Unsupported OBJ directive'):self.pipeline.import_asset(source,**self.request())
        with sqlite3.connect(self.library.database) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'),'Pinned Blender optional integration not configured')
    def test_actual_obj_canonical_axes(self):
        source=self.root/'triangle.obj';source.write_text('o AxisTriangle\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        result=self.pipeline.import_asset(source,**self.request())
        observed=result['report']['native_observation']
        self.assertEqual(observed['triangles'],1)
        for actual,expected in zip(observed['bounds'],[[0,0,0],[1,1,0]]):
            for a,b in zip(actual,expected):self.assertAlmostEqual(a,b,places=6)

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'),'Pinned Blender optional integration not configured')
    def test_native_compositor_cannot_publish_unrequested_files(self):
        source=self.root/'compositor.blend';escaped=self.root/'outside-worker'
        script=self.root/'make_compositor.py'
        script.write_text('import bpy\nbpy.ops.object.select_all(action="SELECT");bpy.ops.object.delete(use_global=False)\nbpy.ops.mesh.primitive_cube_add()\ns=bpy.context.scene;s.use_nodes=True\nf=s.node_tree.nodes.new("CompositorNodeOutputFile");f.base_path='+repr(str(escaped))+'\ns.node_tree.links.new(s.node_tree.nodes.get("Render Layers").outputs["Image"],f.inputs[0])\nbpy.ops.wm.save_as_mainfile(filepath='+repr(str(source))+')\n')
        workspace=self.root/'compositor-fixture';workspace.mkdir()
        self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(script)],workspace)
        result=self.pipeline.import_asset(source,**self.request(trusted_packed_blend=True))
        self.assertEqual(result['report']['native_observation']['triangles'],12)
        self.assertTrue(result['report']['source_validation']['inspection']['scene_compositing'])
        self.assertFalse(escaped.exists())

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'),'Pinned Blender optional integration not configured')
    def test_absolute_native_shape_keys_reject_before_publication(self):
        source=self.root/'absolute-shapes.blend'
        script=self.root/'make_absolute_shapes.py'
        script.write_text('import bpy\nbpy.ops.object.select_all(action="SELECT");bpy.ops.object.delete(use_global=False)\n'
            'bpy.ops.mesh.primitive_cube_add()\no=bpy.context.object\no.shape_key_add(name="Basis")\n'
            'tall=o.shape_key_add(name="Tall")\nfor p in tall.data:p.co.z*=3\n'
            'o.data.shape_keys.use_relative=False\no.data.shape_keys.eval_time=100\n'
            'bpy.ops.wm.save_as_mainfile(filepath='+repr(str(source))+')\n')
        workspace=self.root/'absolute-shapes-fixture';workspace.mkdir()
        self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup',
            '--disable-autoexec','--python',str(script)],workspace)
        before=source.read_bytes()
        with self.assertRaisesRegex(RigError,'Absolute shape keys'):
            self.pipeline.import_asset(source,**self.request(trusted_packed_blend=True))
        self.assertEqual(source.read_bytes(),before)
        with sqlite3.connect(self.library.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'),'Pinned Blender optional integration not configured')
    def test_masked_native_shape_keys_reject_before_publication(self):
        source=self.root/'masked-shapes.blend'
        script=self.root/'make_masked_shapes.py'
        script.write_text('import bpy\nbpy.ops.object.select_all(action="SELECT");bpy.ops.object.delete(use_global=False)\n'
            'bpy.ops.mesh.primitive_cube_add()\no=bpy.context.object\no.shape_key_add(name="Basis")\n'
            'tall=o.shape_key_add(name="Tall")\nfor p in tall.data:p.co.z*=3\n'
            'group=o.vertex_groups.new(name="Mask");group.add(list(range(len(o.data.vertices))),.25,"REPLACE")\n'
            'tall.vertex_group="Mask";tall.value=1\n'
            'bpy.ops.wm.save_as_mainfile(filepath='+repr(str(source))+')\n')
        workspace=self.root/'masked-shapes-fixture';workspace.mkdir()
        self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup',
            '--disable-autoexec','--python',str(script)],workspace)
        before=source.read_bytes()
        with self.assertRaisesRegex(RigError,'Masked shape keys'):
            self.pipeline.import_asset(source,**self.request(trusted_packed_blend=True))
        self.assertEqual(source.read_bytes(),before)
        with sqlite3.connect(self.library.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'),'Pinned Blender optional integration not configured')
    def test_actual_eight_weight_blender_associations(self):
        import numpy as np
        from rig_gltf import GltfBuilder
        from asset_library_import import skin_associations
        from rig_contracts import DEFAULT_LIMITS
        builder=GltfBuilder();attrs=dict(POSITION=builder.accessor([[0,0,0],[1,0,0],[0,1,0]],'VEC3',bounds=True))
        weights=[.05,.06,.07,.08,.09,.10,.20,.35]
        for group in (0,1):
            attrs['JOINTS_'+str(group)]=builder.accessor([[group*4+i for i in range(4)]]*3,'VEC4',5121)
            attrs['WEIGHTS_'+str(group)]=builder.accessor([weights[group*4:group*4+4]]*3,'VEC4')
        nodes=[dict(mesh=0,skin=0,name='SkinMesh')]+[dict(name='Joint'+str(i),translation=[0,.1 if i else 0,0],**({'children':[i+2]} if i<7 else {})) for i in range(8)]
        matrices=np.tile(np.eye(4),(8,1,1));matrices[:,1,3]=[-.1*i for i in range(8)]
        builder.json.update(meshes=[dict(primitives=[dict(attributes=attrs)])],nodes=nodes,
            skins=[dict(joints=list(range(1,9)),skeleton=1,inverseBindMatrices=builder.accessor(matrices,'MAT4'))],scenes=[dict(nodes=[0,1])])
        source=self.root/'eight.glb';builder.write(source)
        result=self.pipeline.import_asset(source,**self.request())
        check=result['report']['skin_binding_comparison'];self.assertTrue(check['ok']);self.assertEqual(check['max_influences'],8)
        packed=self.root/'eight.blend';packed.write_bytes(self.library.read_file(result['reference'],'native/asset.blend'))
        native=self.pipeline.import_asset(packed,**self.request(version='2.0.0',trusted_packed_blend=True))
        self.assertEqual(native['report']['skin_binding_comparison']['max_influences'],8)
        for logical in ('runtime/asset.glb','native/roundtrip.glb'):
            path=self.root/(logical.split('/')[0]+'.glb');path.write_bytes(self.library.read_file(result['reference'],logical))
            observed=skin_associations(path,{**DEFAULT_LIMITS,'influences':8})
            self.assertEqual(observed['max_influences'],8)
            for _,association in observed['rows']:
                self.assertEqual(set(dict(association)),{'Joint'+str(i) for i in range(8)})
                for i,weight in enumerate(weights):self.assertAlmostEqual(dict(association)['Joint'+str(i)],weight,places=6)

    def test_integral_json_number_counts(self):
        source=self.external();document=json.loads(source.read_text())
        document['buffers'][0]['byteLength']=float(document['buffers'][0]['byteLength'])
        for accessor in document['accessors']:accessor['count']=float(accessor['count'])
        source.write_text(json.dumps(document))
        result=self.pipeline.import_asset(source,**self.request(blender_roundtrip=False))
        self.assertEqual(result['report']['native_observation']['vertices'],4)

    def test_missing_and_unsafe_dependencies_before_blender(self):
        for uri in ('missing.bin','../outside.bin','https://example.org/data.bin','file:///etc/passwd','%2e%2e/out.bin','C:/data.bin'):
            with self.subTest(uri=uri):
                source=self.external();document=json.loads(source.read_text());document['buffers'][0]['uri']=uri
                source.write_text(json.dumps(document))
                with self.assertRaises((RigError,OSError)):SourceSnapshot(source,self.root/('snapshot'+str(len(uri))))

    def test_required_extension_rejected_without_blender(self):
        source=self.external(extensionsUsed=['KHR_draco_mesh_compression'],extensionsRequired=['KHR_draco_mesh_compression'])
        with self.assertRaisesRegex(RigError,'Unsupported glTF extensions'):SourceSnapshot(source,self.root/'snapshot')

    def test_cancellation_leaves_library_empty(self):
        pipeline=ImportPipeline(self.library,cancelled=lambda:True)
        with self.assertRaises(RigError):pipeline.import_asset(self.source,**self.request())
        with sqlite3.connect(self.library.database) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)

    def test_untrusted_blend_and_unavailable_tool(self):
        source=self.root/'unsafe.blend';source.write_bytes(b'BLENDER-v300'+b'\0'*128)
        with self.assertRaisesRegex(RigError,'Untrusted Blender'):
            self.pipeline.import_asset(source,**self.request())
        unavailable=ImportPipeline(self.library,blender='/does/not/exist/blender')
        self.assertEqual(unavailable.capabilities(probe_blender=True)['blender']['status'],'unavailable')

    @unittest.skipUnless(hasattr(os,'symlink'),'Symlinks unavailable')
    def test_source_ancestor_symlink_is_rejected(self):
        target=self.root/'original';target.mkdir();(target/'asset.glb').write_bytes(self.source.read_bytes())
        link=self.root/'linked'
        try:link.symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('No symlink permission')
        with self.assertRaises((RigError,OSError)):SourceSnapshot(link/'asset.glb',self.root/'linked-snapshot')

    def test_corrupt_glb_and_case_aliases(self):
        corrupt=self.root/'broken.glb';corrupt.write_bytes(self.source.read_bytes()[:-1])
        with self.assertRaises(RigError):SourceSnapshot(corrupt,self.root/'broken-snapshot')
        source=self.external();document=json.loads(source.read_text())
        document['buffers'].append(dict(byteLength=1,uri='GEOMETRY.BIN'))
        (self.root/'GEOMETRY.BIN').write_bytes(b'x');source.write_text(json.dumps(document))
        with self.assertRaises(RigError):SourceSnapshot(source,self.root/'alias-snapshot')

    @unittest.skipUnless(__import__('sys').platform.startswith('linux'),'Linux process enforcement')
    def test_actual_subprocess_timeout(self):
        from asset_library_import import BlenderAdapter
        import sys,time
        adapter=BlenderAdapter(sys.executable,timeout=1)
        target=self.root/'timed';target.mkdir()
        start=time.monotonic()
        with self.assertRaisesRegex(RigError,'timeout'):
            adapter._run([sys.executable,'-c','import time;time.sleep(10)'],target)
        self.assertLess(time.monotonic()-start,4)

    def test_structural_json_and_obj_records_are_bounded_before_decode(self):
        from asset_library_import import _json
        deep=b'['*129+b'0'+b']'*129
        with self.assertRaisesRegex(RigError,'nesting'):_json(deep)
        broad=b'['+b'{},'*260000+b'{}]'
        with self.assertRaisesRegex(RigError,'complexity'):_json(broad)
        source=self.root/'long-line.obj';source.write_text('o '+('x'*65536)+'\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        with self.assertRaisesRegex(RigError,'record budget'):self.pipeline.import_asset(source,**self.request())

    def test_actual_missing_optional_environment_is_structured(self):
        import subprocess,sys
        result=subprocess.run([sys.executable,'-S',str(ROOT/'scripts/asset_library_import.py'),'probe','--library',str(self.library.root),'--source',str(self.source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)
        report=json.loads(result.stdout)
        self.assertEqual(report['diagnostic']['code'],'LIBRARY_IMPORT_DEPENDENCY')
        self.assertIn('requirements-rig.txt',report['diagnostic']['message'])
        self.assertNotIn('Traceback',result.stderr)
        with sqlite3.connect(self.library.database) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)

    def test_request_and_path_bounds(self):
        for value in (True,1.1,float('inf'),-1):
            with self.assertRaises(RigError):integer(value,0,8)
        for name in ('CON.obj','COM¹.bin','x/../y','x\\y',' x.bin','x.','cafe\u0301.bin'):
            with self.assertRaises((RigError,ValueError)):relative(name)
        with self.assertRaises(RigError):self.pipeline.import_batch([])

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'),'Pinned Blender optional integration not configured')
    def test_actual_blender_roundtrip_and_procedural_generation(self):
        result=self.pipeline.import_asset(self.source,**self.request())
        manifest=self.library.resolve(result['reference'])
        paths={entry['path'] for entry in manifest['files']}
        self.assertIn('native/asset.blend',paths)
        packed=self.root/'packed.blend';packed.write_bytes(self.library.read_file(result['reference'],'native/asset.blend'))
        with self.assertRaises(RigError):self.pipeline.import_asset(packed,**self.request(version='1.0.1',fixture=False,trusted_packed_blend=True))
        derivative=self.root/'roundtrip.glb';derivative.write_bytes(self.library.read_file(result['reference'],'native/roundtrip.glb'))
        with self.assertRaises(RigError):self.pipeline.import_asset(derivative,**self.request(version='1.0.2',fixture=False,blender_roundtrip=False))
        trusted=self.pipeline.import_asset(packed,**self.request(version='1.1.0',trusted_packed_blend=True))
        self.assertGreater(trusted['report']['native_observation']['vertices'],0)
        scripted=self.root/'scripted.blend';script=self.root/'make_scripted.py'
        script.write_text('import bpy\nbpy.data.texts.new("forbidden.py").write("raise RuntimeError(\"must not execute\")")\nbpy.ops.wm.save_as_mainfile(filepath='+repr(str(scripted))+')\n')
        workspace=self.root/'script-fixture';workspace.mkdir()
        self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(script)],workspace)
        with self.assertRaises(RigError):self.pipeline.import_asset(scripted,**self.request(version='1.2.0',trusted_packed_blend=True))
        procedural=self.root/'procedural.blend';shader_script=self.root/'make_shader.py'
        shader_script.write_text('import bpy\nbpy.ops.mesh.primitive_cube_add()\nm=bpy.data.materials.new("Noise");m.use_nodes=True\nn=m.node_tree.nodes.new("ShaderNodeTexNoise")\nm.node_tree.links.new(n.outputs["Color"],m.node_tree.nodes["Principled BSDF"].inputs["Base Color"])\nbpy.context.object.data.materials.append(m)\nbpy.ops.wm.save_as_mainfile(filepath='+repr(str(procedural))+')\n')
        shader_workspace=self.root/'shader-fixture';shader_workspace.mkdir()
        self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(shader_script)],shader_workspace)
        with self.assertRaises(RigError):self.pipeline.import_asset(procedural,**self.request(version='1.3.0',trusted_packed_blend=True))
        subsurface=self.root/'subsurface.blend';subsurface_script=self.root/'make_subsurface.py'
        subsurface_script.write_text('import bpy\nbpy.ops.mesh.primitive_cube_add()\nm=bpy.data.materials.new("Subsurface");m.use_nodes=True\nm.node_tree.nodes["Principled BSDF"].inputs["Subsurface Weight"].default_value=1.0\nbpy.context.object.data.materials.append(m)\nbpy.ops.wm.save_as_mainfile(filepath='+repr(str(subsurface))+')\n')
        subsurface_workspace=self.root/'subsurface-fixture';subsurface_workspace.mkdir()
        self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(subsurface_script)],subsurface_workspace)
        with self.assertRaises(RigError):self.pipeline.import_asset(subsurface,**self.request(version='1.4.0',trusted_packed_blend=True))
        animated=self.root/'animated-material.blend';animated_script=self.root/'make_animated_material.py'
        animated_script.write_text('import bpy\nbpy.ops.mesh.primitive_cube_add()\nm=bpy.data.materials.new("AnimatedDiffuse");m.use_nodes=False\nm.diffuse_color=(1,0,0,1);m.keyframe_insert(data_path="diffuse_color",frame=1)\nm.diffuse_color=(0,1,0,1);m.keyframe_insert(data_path="diffuse_color",frame=24)\nbpy.context.object.data.materials.append(m)\nbpy.ops.wm.save_as_mainfile(filepath='+repr(str(animated))+')\n')
        animated_workspace=self.root/'animated-material-fixture';animated_workspace.mkdir()
        self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(animated_script)],animated_workspace)
        with self.assertRaises(RigError):self.pipeline.import_asset(animated,**self.request(version='1.5.0',trusted_packed_blend=True))
        for kind,body in (
            ('metaball','bpy.ops.object.metaball_add();bpy.context.object.name="RequiredMetaball"'),
            ('visibility','o=bpy.context.object;o.hide_render=False;o.keyframe_insert(data_path="hide_render",frame=1);o.hide_render=True;o.keyframe_insert(data_path="hide_render",frame=24)'),
            ('component','o=bpy.context.object;o.keyframe_insert(data_path="location",frame=1);f=o.animation_data.action.fcurves.new(data_path="location",index=9);f.keyframe_points.insert(1,3)')):
            rejected=self.root/(kind+'.blend');creation=self.root/('make_'+kind+'.py')
            creation.write_text('import bpy\nbpy.ops.object.select_all(action="SELECT");bpy.ops.object.delete(use_global=False)\nbpy.ops.mesh.primitive_cube_add()\n'+body+'\nbpy.ops.wm.save_as_mainfile(filepath='+repr(str(rejected))+')\n')
            workspace=self.root/(kind+'-fixture');workspace.mkdir()
            self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(creation)],workspace)
            with self.assertRaises(RigError):self.pipeline.import_asset(rejected,**self.request(version='1.6.0',trusted_packed_blend=True))
        for kind,options in [('envelope','mod.use_vertex_groups=False;mod.use_bone_envelopes=True'),('dual-quaternion','mod.use_deform_preserve_volume=True'),('bendy','rig.data.bones[0].bbone_segments=8;rig.pose.bones[0].bbone_curveinx=2;rig.pose.bones[0].bbone_curveoutx=-2;group=obj.vertex_groups.new(name=rig.data.bones[0].name);group.add(list(range(len(obj.data.vertices))),1,"REPLACE")')]:
            rejected=self.root/(kind+'.blend');creation=self.root/('make_'+kind+'.py')
            creation.write_text('import bpy\nbpy.ops.object.select_all(action="SELECT");bpy.ops.object.delete(use_global=False)\nbpy.ops.mesh.primitive_cube_add();obj=bpy.context.object\nbpy.ops.object.armature_add();rig=bpy.context.object\nmod=obj.modifiers.new("UnsupportedSkin","ARMATURE");mod.object=rig\n'+options+'\nbpy.ops.wm.save_as_mainfile(filepath='+repr(str(rejected))+')\n')
            workspace=self.root/(kind+'-fixture');workspace.mkdir()
            self.pipeline.blender._run([str(self.pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(creation)],workspace)
            with self.assertRaisesRegex(RigError,'Unsupported (armature|native bone) deformation'):self.pipeline.import_asset(rejected,**self.request(version='1.7.0',trusted_packed_blend=True))
        generated=self.pipeline.generate(**self.request(version='2.0.0',parameters=dict(primitive='box',dimensions=[1,2,3],color=[.2,.4,.6,1],animated=True)))
        self.assertEqual(generated['report']['native_observation']['triangles'],12)
        self.assertEqual(self.library.resolve(generated['reference'])['provenance']['method'],'fixture')


class NormalizationPrecisionTests(unittest.TestCase):
    """Decode actual GLB positions/world matrices without the native scene reader."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.library=AssetLibrary(self.root/'library',create=True)
        self.pipeline=ImportPipeline(self.library)
        self.license=self.root/'license.txt';self.license.write_text('CC0 engineering fixture.\n')
        self.base=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())

    def source(self, points):
        from rig_gltf import GltfBuilder
        builder=GltfBuilder()
        position=builder.accessor(points,'VEC3',bounds=True)
        builder.json.update(meshes=[dict(primitives=[dict(attributes=dict(POSITION=position))])],
                            nodes=[dict(mesh=0)],scenes=[dict(nodes=[0])])
        builder.json['asset']['extras']=dict(hatchAssetSourceFixture=True)
        source=self.root/'precision.glb';builder.write(source)
        return source

    def bounds(self, payload):
        import numpy as np
        magic,version,size=struct.unpack_from('<4sII',payload)
        self.assertEqual((magic,version,size),(b'glTF',2,len(payload)))
        length,kind=struct.unpack_from('<II',payload,12);self.assertEqual(kind,0x4E4F534A)
        document=json.loads(payload[20:20+length])
        offset=20+length;binary_length,kind=struct.unpack_from('<II',payload,offset)
        self.assertEqual(kind,0x004E4942);binary=payload[offset+8:offset+8+binary_length]
        positions=[]
        def visit(index,parent):
            node=document['nodes'][index]
            if 'matrix' in node:
                local=np.asarray(node['matrix'],dtype=float).reshape(4,4).T
            else:
                self.assertFalse({'rotation','scale'} & node.keys())
                local=np.eye(4);local[:3,3]=node.get('translation',[0,0,0])
            world=parent@local
            if 'mesh' in node:
                for primitive in document['meshes'][node['mesh']]['primitives']:
                    accessor=document['accessors'][primitive['attributes']['POSITION']]
                    self.assertEqual((accessor['componentType'],accessor['type']),(5126,'VEC3'))
                    view=document['bufferViews'][accessor['bufferView']]
                    self.assertEqual(view['buffer'],0);self.assertNotIn('byteStride',view)
                    start=view.get('byteOffset',0)+accessor.get('byteOffset',0)
                    values=struct.unpack_from('<'+'f'*(accessor['count']*3),binary,start)
                    points=np.asarray(values,dtype=float).reshape(-1,3)
                    positions.extend((np.column_stack((points,np.ones(len(points))))@world.T)[:,:3])
            for child in node.get('children',[]):visit(child,world)
        for index in document['scenes'][document.get('scene',0)]['nodes']:visit(index,np.eye(4))
        self.assertTrue(positions)
        points=np.asarray(positions)
        return document,np.asarray([points.min(axis=0),points.max(axis=0)])

    def imported(self, source, version='1.0.0', **options):
        original=source.read_bytes()
        result=self.pipeline.import_asset(source,identity=self.base['identity'],version=version,
            metadata=self.base['metadata'],license_file=self.license,fixture=True,
            blender_roundtrip=False,**options)
        self.assertEqual(source.read_bytes(),original)
        restored=self.root/('restored-'+version)
        self.pipeline.restore_original(result['reference'],restored)
        self.assertEqual((restored/source.name).read_bytes(),original)
        return self.bounds(self.library.read_file(result['reference'],'runtime/asset.glb')),result

    def test_invalid_numeric_scales_reject_without_publication_or_source_changes(self):
        source=self.source([[.25,.125,-.5],[2.,.125,-.5],[.25,1.5,.75]])
        original=source.read_bytes();license_bytes=self.license.read_bytes()
        huge=json.loads('1'+'0'*400)
        for scale in (huge,-huge,True,False,float('nan'),float('inf'),float('-inf'),
                      0,-1,1e-7,1e6+1.,'1'):
            with self.subTest(scale=repr(scale)):
                with self.assertRaises(RigError) as caught:
                    self.pipeline.import_asset(source,identity=self.base['identity'],version='1.0.0',
                        metadata=self.base['metadata'],license_file=self.license,fixture=True,
                        blender_roundtrip=False,unit_scale=scale)
                self.assertEqual(caught.exception.report['code'],'LIBRARY_IMPORT_NORMALIZATION')
                self.assertEqual(source.read_bytes(),original)
                self.assertEqual(self.license.read_bytes(),license_bytes)
                with sqlite3.connect(self.library.database) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0],0)

    def test_cli_huge_json_scale_returns_structured_failure_without_publication(self):
        import subprocess
        import sys
        source=self.source([[.25,.125,-.5],[2.,.125,-.5],[.25,1.5,.75]])
        original=source.read_bytes();license_bytes=self.license.read_bytes()
        request=dict(identity=self.base['identity'],version='1.0.0',metadata=self.base['metadata'],
                     license_file=str(self.license),fixture=True,blender_roundtrip=False,
                     unit_scale=json.loads('1'+'0'*400))
        request_path=self.root/'request.json';request_path.write_text(json.dumps(request))
        completed=subprocess.run([sys.executable,str(ROOT/'scripts/asset_library_import.py'),
            'import','--library',str(self.library.root),'--source',str(source),
            '--request',str(request_path)],capture_output=True,text=True,timeout=30,cwd=ROOT)
        self.assertEqual(completed.returncode,2,completed.stderr)
        result=json.loads(completed.stdout)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['diagnostic']['code'],'LIBRARY_IMPORT_NORMALIZATION')
        self.assertNotIn('Traceback',completed.stdout+completed.stderr)
        self.assertEqual(source.read_bytes(),original)
        self.assertEqual(self.license.read_bytes(),license_bytes)
        with sqlite3.connect(self.library.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0],0)

    def test_finite_integer_and_float_scales_preserve_explicit_world_bounds(self):
        import numpy as np
        source=self.source([[.25,.125,-.5],[2.,.125,-.5],[.25,1.5,.75]])
        _,before=self.bounds(source.read_bytes())
        for index,scale in enumerate((1,2,2.,.5),1):
            with self.subTest(scale=scale,numeric_type=type(scale).__name__):
                (_,after),result=self.imported(source,str(index)+'.0.0',unit_scale=scale)
                self.assertEqual(result['report']['normalization']['source_unit_scale'],float(scale))
                np.testing.assert_allclose(after,before*scale,rtol=0,atol=1e-14)

    def test_near_unit_scales_retain_explicit_world_bounds(self):
        import numpy as np
        source=self.source([[.25,.125,-.5],[2.,.125,-.5],[.25,1.5,.75]])
        _,before=self.bounds(source.read_bytes())
        for version,scale in [('1.0.0',1.000001),('2.0.0',1.00001)]:
            with self.subTest(scale=scale):
                (_,after),_=self.imported(source,version,unit_scale=scale)
                np.testing.assert_allclose(after,before*scale,rtol=0,atol=1e-14)
                self.assertNotEqual(after[1,0],before[1,0])

    def test_tiny_nonzero_ground_shift_reaches_actual_zero(self):
        import numpy as np
        source=self.source([[0,1e-9,0],[2,1e-9,0],[0,1,1]])
        _,before=self.bounds(source.read_bytes());self.assertGreater(before[0,1],0.)
        (_,after),result=self.imported(source,ground=True)
        expected=before.copy();expected[:,1]-=before[0,1]
        np.testing.assert_allclose(after,expected,rtol=0,atol=1e-14)
        self.assertEqual(after[0,1],0.)
        self.assertLess(result['report']['normalization']['ground_offset'],0.)

    def test_exact_identity_retains_original_nodes_and_geometry(self):
        import numpy as np
        source=self.source([[.25,.125,-.5],[2.,.125,-.5],[.25,1.5,.75]])
        original,before=self.bounds(source.read_bytes())
        (converted,after),_=self.imported(source,unit_scale=1.,ground=False)
        self.assertEqual(converted['nodes'],original['nodes'])
        np.testing.assert_array_equal(after,before)

if __name__=='__main__':unittest.main()
