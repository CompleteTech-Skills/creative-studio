"""Actual payload export/reimport, failures and exclusive publication controls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from asset_library import AssetLibrary
from asset_library_export import ExportPipeline
from asset_library_import import ImportPipeline, _gltf_json
from asset_library_contracts import canonical_bytes, seal
from rig_errors import RigError, object_hash
from rig_gltf import export_gltf, read_gltf
from rig_test_helpers import fixture

ROOT=Path(__file__).resolve().parents[1]

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        rig=fixture(self.root)
        self.source=self.root/'source.glb';export_gltf(rig,self.source)
        self.license=self.root/'license.txt';self.license.write_text('CC0 synthetic engineering fixture.\n')
        self.base=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
        self.library=AssetLibrary(self.root/'library',create=True)
        self.importer=ImportPipeline(self.library,blender=os.environ.get('HATCH_BLENDER'))
        self.result=self.importer.import_asset(self.source,identity=self.base['identity'],version='1.0.0',
            metadata=self.base['metadata'],license_file=self.license,fixture=True,blender_roundtrip=False)
        self.ref=self.result['reference'];self.pipeline=ExportPipeline(self.library,blender=os.environ.get('HATCH_BLENDER'))

    def test_glb_retains_exact_runtime_and_complete_receipt(self):
        destination=self.root/'export'
        receipt=self.pipeline.export(self.ref,destination,required_capabilities=['geometry','skins','animation'])
        self.assertEqual((destination/'asset.glb').read_bytes(),self.library.read_file(self.ref,'runtime/asset.glb'))
        saved=json.loads((destination/'export.json').read_text())
        self.assertEqual(saved,receipt)
        signature=saved.pop('receipt_sha256');self.assertEqual(signature,object_hash(saved))
        self.assertTrue(receipt['input_validation']['ok']);self.assertTrue(receipt['output_validation']['ok'])
        self.assertFalse(receipt['production_approved'])
        self.assertEqual({x['path'] for x in receipt['inventory']},{'asset.glb'})

    def test_gltf_external_buffer_contains_actual_geometry_skin_and_clip(self):
        destination=self.root/'external'
        receipt=self.pipeline.export(self.ref,destination,format='gltf')
        document=json.loads((destination/'asset.gltf').read_text())
        self.assertEqual(document['buffers'][0]['uri'],'asset.bin')
        before=read_gltf(self.source);after=read_gltf(destination/'asset.gltf')
        self.assertEqual(len(before.json['skins']),len(after.json['skins']))
        self.assertEqual(before.json['animations'],after.json['animations'])
        self.assertEqual(before.buffers,after.buffers)
        self.assertGreater(len(after.accessor(0)),0)
        self.assertTrue(receipt['comparison']['ok'])

    def test_missing_or_corrupt_payload_cannot_publish(self):
        with sqlite3.connect(self.library.database) as db:
            digest=db.execute('SELECT sha256 FROM blobs LIMIT 1').fetchone()[0]
            db.execute('UPDATE blobs SET payload=? WHERE sha256=?',(b'broken',digest))
        with self.assertRaises(RigError):self.pipeline.export(self.ref,self.root/'absent')
        self.assertFalse((self.root/'absent').exists())

    def test_unsupported_capability_format_and_native_policy_fail_before_publish(self):
        for options in [dict(format='fbx'),dict(required_capabilities=['not-present']),dict(format='blend'),
                        dict(format='blend',trusted_native=True)]:
            with self.subTest(options=options):
                with self.assertRaises(RigError):self.pipeline.export(self.ref,self.root/'absent',**options)
                self.assertFalse((self.root/'absent').exists())

    def test_existing_destination_racing_destination_and_cancel_preserve_bytes(self):
        existing=self.root/'existing';existing.mkdir();(existing/'user.txt').write_text('retained')
        with self.assertRaises(RigError):self.pipeline.export(self.ref,existing)
        self.assertEqual((existing/'user.txt').read_text(),'retained')
        import atomic_publish
        original=atomic_publish._rename_no_replace
        def race(stage,destination):
            destination.mkdir();(destination/'user.txt').write_text('racing')
            return original(stage,destination)
        destination=self.root/'racing'
        with patch.object(atomic_publish,'_rename_no_replace',race):
            with self.assertRaises(OSError):self.pipeline.export(self.ref,destination)
        self.assertEqual((destination/'user.txt').read_text(),'racing')
        calls=[]
        self.pipeline.cancelled=lambda:len(calls)>0
        import asset_library_export
        real=asset_library_export._equivalent
        def cancel(*args):calls.append(1);return real(*args)
        with patch.object(asset_library_export,'_equivalent',cancel):
            with self.assertRaisesRegex(RigError,'Cancelled'):self.pipeline.export(self.ref,self.root/'cancelled')
        self.assertFalse((self.root/'cancelled').exists())
        self.assertFalse(list(self.root.glob('.hatch-export-*')))

    @unittest.skipUnless(hasattr(os,'symlink'),'Symlinks unavailable')
    def test_destination_symlink_ancestor_rejected(self):
        link=self.root/'link';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(RigError):self.pipeline.export(self.ref,link/'absent')

    def test_reimport_tamper_never_publishes(self):
        import asset_library_export
        original=asset_library_export._equivalent
        def tamper(source,output):
            output.json['nodes'][0]['name']='changed'
            return original(source,output)
        with patch.object(asset_library_export,'_equivalent',tamper):
            with self.assertRaisesRegex(RigError,'changed'):self.pipeline.export(self.ref,self.root/'changed')
        self.assertFalse((self.root/'changed').exists())

    def test_batch_is_bounded_and_keeps_completed_exports(self):
        with self.assertRaises(RigError):self.pipeline.export_batch([])
        with self.assertRaises(RigError):self.pipeline.export_batch([{}]*9)
        requests=[dict(reference=self.ref,destination=self.root/'one'),dict(reference=self.ref,destination=self.root/'two',format='usd')]
        with self.assertRaises(RigError):self.pipeline.export_batch(requests)
        self.assertTrue((self.root/'one'/'asset.glb').is_file());self.assertFalse((self.root/'two').exists())

    def test_same_bounds_changed_interior_geometry_or_material_rejected(self):
        from rig_gltf import GltfBuilder
        from asset_library_export import _native_compare
        from rig_contracts import DEFAULT_LIMITS
        def write(path,position,color):
            builder=GltfBuilder()
            positions=builder.accessor([[0,0,0],[1,0,0],[0,1,1],position],'VEC3',bounds=True)
            indices=builder.accessor([[0],[1],[3],[0],[3],[2]],'SCALAR',5123)
            builder.json.update(materials=[dict(pbrMetallicRoughness=dict(baseColorFactor=color))],
                meshes=[dict(primitives=[dict(attributes=dict(POSITION=positions),indices=indices,material=0)])],
                nodes=[dict(mesh=0)],scenes=[dict(nodes=[0])],scene=0)
            builder.write(path)
        source=self.root/'before.glb';output=self.root/'after.glb'
        write(source,[.2,.2,.2],[1,0,0,1])
        for position,color in [([.8,.2,.2],[1,0,0,1]),([.2,.2,.2],[0,1,0,1])]:
            with self.subTest(position=position,color=color):
                write(output,position,color)
                with self.assertRaisesRegex(RigError,'decoded triangle geometry'):
                    _native_compare(source,output,DEFAULT_LIMITS)

    def test_native_normals_and_static_joint_parent_changes_are_rejected(self):
        from rig_gltf import GltfBuilder
        from asset_library_export import _native_compare
        from rig_contracts import DEFAULT_LIMITS
        from asset_library_import import _pack_glb
        from rig_gltf import read_gltf
        # Real static rig: keep every joint world rest unchanged while changing hierarchy.
        static=fixture(self.root,name='static',animated=False)
        source=self.root/'static-source.glb';export_gltf(static,source)
        doc=read_gltf(source);changed=deepcopy(doc.json)
        byname={node.get('name'):i for i,node in enumerate(changed['nodes'])}
        a,b,c=(byname[name] for name in ['b0','b1','b2'])
        changed['nodes'][a]['children'].append(c)
        changed['nodes'][b]['children'].remove(c)
        changed['nodes'][c]['translation']=[0,2,0]
        # Pack an independently modified actual GLB, preserving source buffers.
        from rig_gltf import GltfBuilder
        altered=GltfBuilder();altered.json=changed;altered.binary=bytearray(doc.buffers[0])
        # GltfBuilder stores its bytes as .data in this version.
        altered.data=bytearray(doc.buffers[0]);output=self.root/'changed-parent.glb';altered.write(output)
        with self.assertRaisesRegex(RigError,'joint hierarchy'):_native_compare(source,output,DEFAULT_LIMITS)
        # Geometry shape is identical but the actual normal attribute reverses.
        def normals(path,value):
            builder=GltfBuilder();p=builder.accessor([[0,0,0],[1,0,0],[0,1,0]],'VEC3',bounds=True)
            n=builder.accessor([value]*3,'VEC3')
            builder.json.update(meshes=[dict(primitives=[dict(attributes=dict(POSITION=p,NORMAL=n))])],
                nodes=[dict(mesh=0)],scenes=[dict(nodes=[0])],scene=0);builder.write(path)
        before=self.root/'normal-before.glb';after=self.root/'normal-after.glb'
        normals(before,[0,0,1]);normals(after,[0,0,-1])
        with self.assertRaisesRegex(RigError,'decoded triangle geometry'):_native_compare(before,after,DEFAULT_LIMITS)

    def test_optional_geometry_dependencies_have_stable_diagnostic(self):
        import subprocess,sys
        code="""
import sys,importlib.abc
from asset_library import AssetLibrary
from asset_library_export import ExportPipeline
from rig_errors import RigError
import json
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0]=='numpy':raise ImportError('optional numpy intentionally unavailable')
sys.meta_path.insert(0,Block())
try:ExportPipeline(AssetLibrary(sys.argv[1])).export(json.loads(sys.argv[2]),sys.argv[3])
except RigError as e:
 assert e.report['code']=='LIBRARY_EXPORT_DEPENDENCY',e.report
else:raise AssertionError('missing dependency accepted')
"""
        result=subprocess.run([sys.executable,'-c',code,str(self.library.root),json.dumps(self.ref),str(self.root/'without-optional')],
            capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.root/'without-optional').exists())

    @unittest.skipUnless(os.environ.get('HATCH_BLENDER'),'Pinned Blender native integration not configured')
    def test_real_native_export_reopens_exact_registered_blender_bytes(self):
        imported=self.importer.import_asset(self.source,identity=self.base['identity'],version='2.0.0',
            metadata=self.base['metadata'],license_file=self.license,fixture=True,blender_roundtrip=True)
        losses=imported['report']['native_derivative_losses']
        if losses:
            with self.assertRaisesRegex(RigError,'losses need explicit approval'):
                self.pipeline.export(imported['reference'],self.root/'rejected',format='blend',trusted_native=True)
        destination=self.root/'native'
        receipt=self.pipeline.export(imported['reference'],destination,format='blend',trusted_native=True,allowed_losses=losses)
        self.assertEqual((destination/'asset.blend').read_bytes(),self.library.read_file(imported['reference'],'native/asset.blend'))
        self.assertTrue(receipt['native']['saved_and_reopened']);self.assertTrue(receipt['comparison']['ok'])
        self.assertGreater(receipt['native']['inspection']['vertices'],0)
        self.assertTrue(receipt['output_validation']['ok'])

if __name__=='__main__':unittest.main()
