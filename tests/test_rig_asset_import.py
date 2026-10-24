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
        texture.unlink()
        with self.assertRaises((RigError,OSError)):SourceSnapshot(source,self.root/'missing-texture')

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
        generated=self.pipeline.generate(**self.request(version='2.0.0',parameters=dict(primitive='box',dimensions=[1,2,3],color=[.2,.4,.6,1],animated=True)))
        self.assertEqual(generated['report']['native_observation']['triangles'],12)
        self.assertEqual(self.library.resolve(generated['reference'])['provenance']['method'],'fixture')

if __name__=='__main__':unittest.main()
