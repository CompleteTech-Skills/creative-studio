from copy import deepcopy
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import numpy as np
from PIL import Image
from rig_test_helpers import fixture
from rig_errors import RigError,atomic_json,sha256
from rig_gltf import export_gltf,export_geometry,read_gltf,GltfBuilder
from rig_import import load_character_model
from rig_interop import compare_imported,validate_gltf
from rig_models import TextureImage


class RigInterchangeTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.rig=fixture(self.temp.name)
    def test_glb_and_gltf_preserve_actual_skin_morph_material_animation(self):
        for extension in ('glb','gltf'):
            output=Path(self.temp.name)/('out.'+extension);export_gltf(self.rig,output)
            imported=load_character_model(output)
            self.assertIsNotNone(imported.skeleton);self.assertEqual(set(imported.clips),set(self.rig.clips))
            self.assertEqual(len(imported.geometry.materials),len(self.rig.geometry.materials));self.assertIn('blink',imported.geometry.meshes[0].morphs)
            self.assertTrue(compare_imported(self.rig,imported)['ok'])
            doc=read_gltf(output).json;attrs=doc['meshes'][0]['primitives'][0]['attributes']
            self.assertIn('JOINTS_0',attrs);self.assertIn('WEIGHTS_0',attrs);self.assertIn('inverseBindMatrices',doc['skins'][0])
            self.assertGreater(len(doc['animations'][0]['channels']),0)
    def test_embedded_texture_bytes_and_uv_preserved(self):
        geometry=deepcopy(self.rig.geometry);buffer=io.BytesIO();Image.new('RGBA',(2,2),(255,30,50,255)).save(buffer,format='PNG')
        geometry.images=[TextureImage('fixture.png','image/png',buffer.getvalue())];geometry.textures=[dict(source=0)]
        geometry.meshes[0].attributes['TEXCOORD_0']=np.array([[0,0],[1,0],[0,1],[1,1]],float)
        geometry.materials[0]['pbrMetallicRoughness']={'baseColorTexture':{'index':0}}
        path=Path(self.temp.name)/'textured.glb';export_geometry(geometry,path);result=load_character_model(path)
        self.assertEqual(result.geometry.images[0].data,buffer.getvalue());np.testing.assert_array_equal(result.geometry.meshes[0].attributes['TEXCOORD_0'],geometry.meshes[0].attributes['TEXCOORD_0'])
    def test_truncated_container_and_external_path_escape_fail(self):
        bad=Path(self.temp.name)/'bad.glb';bad.write_bytes(b'glTF'+b'\0'*20)
        with self.assertRaises(RigError):load_character_model(bad)
        path=Path(self.temp.name)/'bad.gltf';atomic_json(path,{'asset':{'version':'2.0'},'buffers':[{'uri':'../escape.bin','byteLength':4}]})
        with self.assertRaises(RigError):read_gltf(path)
    def test_required_extension_and_encoded_budget_fail(self):
        path=Path(self.temp.name)/'bad.gltf';atomic_json(path,{'asset':{'version':'2.0'},'extensionsRequired':['KHR_draco_mesh_compression']})
        with self.assertRaises(RigError):read_gltf(path)
        from rig_contracts import DEFAULT_LIMITS
        with self.assertRaises(RigError):load_character_model(self.rig.root/self.rig.manifest['source']['path'],limits={**DEFAULT_LIMITS,'source_bytes':8})
    def test_sparse_accessor_and_interleaved_stride(self):
        builder=GltfBuilder();view=builder.view(np.array([[1,2,3,99],[4,5,6,99]],dtype='<f4').tobytes());builder.json['bufferViews'][view]['byteStride']=16
        builder.json['accessors']=[dict(bufferView=view,componentType=5126,count=2,type='VEC3')]
        output=Path(self.temp.name)/'stride.glb';builder.write(output);np.testing.assert_array_equal(read_gltf(output).accessor(0),[[1,2,3],[4,5,6]])
        builder=GltfBuilder();indices=builder.view(bytes([1]));values=builder.view(np.array([[.2,.4,.6]],dtype='<f4').tobytes())
        builder.json['accessors']=[dict(componentType=5126,count=3,type='VEC3',sparse=dict(count=1,indices=dict(bufferView=indices,componentType=5121),values=dict(bufferView=values)))]
        builder.write(output);np.testing.assert_allclose(read_gltf(output).accessor(0),[[0,0,0],[.2,.4,.6],[0,0,0]])
    def test_concave_obj_negative_indices_and_mtl(self):
        parent=Path(self.temp.name);(parent/'model.mtl').write_text('newmtl copper\nKd 0.8 0.3 0.1\n')
        model=parent/'model.obj';model.write_text('mtllib model.mtl\nv 0 0 0\nv 2 0 0\nv 2 2 0\nv 1 1 0\nv 0 2 0\nusemtl copper\nf -5 -4 -3 -2 -1\n')
        result=load_character_model(model);mesh=result.geometry.meshes[0];points=mesh.positions[mesh.triangles]
        area=np.linalg.norm(np.cross(points[:,1]-points[:,0],points[:,2]-points[:,0]),axis=1).sum()/2
        self.assertAlmostEqual(area,3);self.assertEqual(len(mesh.triangles),3);self.assertEqual(len(result.source_files),2)
    def test_self_intersecting_obj_and_symlink_inputs_fail(self):
        path=Path(self.temp.name)/'cross.obj';path.write_text('v 0 0 0\nv 2 2 0\nv 0 2 0\nv 2 0 0\nf 1 2 3 4\n')
        with self.assertRaises(RigError):load_character_model(path)
        link=Path(self.temp.name)/'alias.glb';link.symlink_to(self.rig.root/self.rig.manifest['source']['path'])
        with self.assertRaises(RigError):read_gltf(link)
    @unittest.skipUnless(os.environ.get('HATCH_RIG_INTEROP_TEST')=='1','Enable HATCH_RIG_INTEROP_TEST=1 for independent optional validators.')
    def test_khronos_and_independent_pygltflib_read_actual_skin(self):
        from pygltflib import GLTF2
        path=Path(self.temp.name)/'independent.glb';export_gltf(self.rig,path);report=validate_gltf(path)
        self.assertTrue(report['ok']);self.assertEqual(report['issues']['numErrors'],0)
        gltf=GLTF2().load(str(path));self.assertGreater(len(gltf.skins[0].joints),0);self.assertGreater(len(gltf.animations[0].channels),0)


class RigExternalIntegrationTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.rig=fixture(self.temp.name)
    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_reopen_has_actual_armature_and_deformation(self):
        from rig_blender import export_blender
        report=export_blender(self.rig,Path(self.temp.name)/'editable.blend');self.assertTrue(report['ok']);self.assertTrue(report['reopened'])
        self.assertLess(report['max_vertex_error_m'],.01);self.assertGreater(report['compared_vertices'],0)
        self.assertEqual(report['schema_version'],1);self.assertEqual(report['format'],'blend')
    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_export_and_preview_accept_canonical_parent_alias(self):
        from rig_blender import export_blender,render_blender_preview
        root=Path(self.temp.name);actual=root/'actual';actual.mkdir();alias=root/'alias'
        try:alias.symlink_to(actual,target_is_directory=True)
        except OSError as exc:
            if getattr(exc,'winerror',None)==1314:self.skipTest('Symlink privilege unavailable.')
            raise
        receipt=export_blender(self.rig,alias/'editable.blend')
        self.assertTrue(receipt['ok']);self.assertEqual(Path(receipt['path']),actual.resolve()/'editable.blend')
        report=render_blender_preview(self.rig,alias/'preview',clip='bend',times=[0.],
            blend_path=receipt['path'],blend_sha256=receipt['sha256'],width=96,height=96)
        self.assertEqual(len(report['frames']),1)
        self.assertTrue(Path(report['frames'][0]['path']).is_file())

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_socket_preserves_full_native_transform_during_animation(self):
        from rig_blender import export_blender
        from rig_math import axis_angle
        from rig_attachments import socket_world
        from rig_animation import evaluate_animation
        self.rig.manifest['attachments'] = [dict(schema_version=1, id='grip.right', bone='b1',
            transform=dict(translation=[.13,.21,.34], rotation=axis_angle([1,2,3],.7).tolist(),
                           scale=[.7,1.2,1.6]), role='grip', owner=self.rig.manifest['character']['id'])]
        report=export_blender(self.rig,Path(self.temp.name)/'socket.blend')
        self.assertTrue(report['ok']);self.assertLess(report['max_socket_transform_error'],.005)
        for sample in report['samples']:
            pose=evaluate_animation(self.rig,self.rig.clips[sample['clip']],sample['time'])['pose']
            np.testing.assert_allclose(sample['sockets']['grip.right'],
                socket_world(self.rig,pose,'grip.right'),atol=.005)
        self.assertGreater(np.max(np.abs(np.asarray(report['samples'][0]['sockets']['grip.right']) -
                                         np.asarray(report['samples'][1]['sockets']['grip.right']))), .01)

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_unanimated_rig_reopens_and_samples_bind_pose(self):
        from rig_blender import export_blender
        self.rig.clips={}
        report=export_blender(self.rig,Path(self.temp.name)/'static.blend')
        self.assertTrue(report['ok']);self.assertTrue(report['reopened'])
        self.assertEqual(len(report['samples']),1)
        self.assertEqual(report['samples'][0]['clip'],'rest')
        self.assertEqual(report['samples'][0]['matched'],[])
        self.assertLess(report['max_vertex_error_m'],1e-5)
        self.assertGreater(report['compared_vertices'],0)

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_preserves_nonzero_defaults_without_morph_channels(self):
        from rig_blender import export_blender
        from rig_skinning import evaluate_deformation
        from rig_skeleton import neutral_pose
        # No animation and a skeletal-only action both must preserve the source
        # morph. The known 0.2m delta makes a silent zero reset observable.
        self.rig.geometry.meshes[0].morph_defaults['blink']=1.
        for clip in self.rig.clips.values():
            clip['tracks']=[track for track in clip['tracks'] if track['path']!='weights']
        skeletal_clips=deepcopy(self.rig.clips)
        for animated in (False,True):
            with self.subTest(animated=animated):
                self.rig.clips=deepcopy(skeletal_clips) if animated else {}
                expected=evaluate_deformation(self.rig,neutral_pose(self.rig.skeleton))
                report=export_blender(self.rig,Path(self.temp.name)/('defaults-%s.blend'%animated))
                self.assertTrue(report['ok']);self.assertTrue(report['reopened'])
                # Skeletal animation has the adapter's existing resampling
                # tolerance; the static default has a direct tight comparison.
                self.assertLess(report['max_vertex_error_m'],.01 if animated else 1e-5)
                imported=load_character_model(report['roundtrip_path'])
                mesh=imported.geometry.meshes[0]
                self.assertAlmostEqual(mesh.morph_defaults['blink'],1.)
                if not animated:
                    sample=report['samples'][0]['meshes'][mesh.id]
                    np.testing.assert_allclose(sample,expected[mesh.id]['positions'],atol=1e-5)
        # Creation-only exports must also save the source default, even when no
        # independent reopen is requested by the caller.
        self.rig.clips={}
        created=export_blender(self.rig,Path(self.temp.name)/'defaults-create-only.blend',verify=False)
        self.assertTrue(created['ok']);self.assertFalse(created['reopened'])
        self.assertLess(np.max(np.abs(np.asarray(created['samples'][0]['meshes'][self.rig.geometry.meshes[0].id])-
                                     expected[self.rig.geometry.meshes[0].id]['positions'])),1e-5)

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_creation_receipt_does_not_claim_reopen(self):
        from rig_blender import export_blender
        report=export_blender(self.rig,Path(self.temp.name)/'created.blend',verify=False)
        self.assertTrue(report['ok']);self.assertFalse(report['reopened'])
        self.assertEqual(report['schema_version'],1);self.assertEqual(report['format'],'blend')
        self.assertNotIn('roundtrip_sha256',report);self.assertNotIn('compared_vertices',report)
    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_preview_rejects_a_different_embedded_rig_revision(self):
        from rig_blender import export_blender,render_blender_preview
        blend=Path(self.temp.name)/'source.blend'
        receipt=export_blender(self.rig,blend,verify=False)
        self.rig.manifest['source']['attribution']+=' changed revision'
        destination=Path(self.temp.name)/'wrong-rig-preview'
        with self.assertRaisesRegex(RigError,'Blender failed'):
            render_blender_preview(self.rig,destination,clip='bend',blend_path=blend,blend_sha256=receipt['sha256'],width=96,height=96)
        self.assertFalse(destination.exists())
        self.assertTrue(blend.is_file())

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for actual Blender CPU rendering.')
    def test_blender_cpu_preview_renders_distinct_transparent_poses(self):
        from rig_blender import render_blender_preview
        from PIL import Image
        report=render_blender_preview(self.rig,Path(self.temp.name)/'rendered',clip='bend',times=[0.,.5],width=96,height=96)
        self.assertEqual(len(report['frames']),2);self.assertEqual(report['engine'],'Cycles CPU')
        images=[np.asarray(Image.open(frame['path']).convert('RGBA')) for frame in report['frames']]
        self.assertGreater(np.count_nonzero(images[0][:,:,3]),10);self.assertGreater(np.count_nonzero(images[0][:,:,3]==0),10)
        self.assertGreater(np.count_nonzero(images[0]!=images[1]),10)
    @unittest.skipUnless(os.environ.get('HATCH_RIG_BROWSER_TEST')=='1','Enable HATCH_RIG_BROWSER_TEST=1 for independent Three.js browser integration.')
    def test_browser_actual_vertices_and_ui_controls(self):
        from rig_viewer import create_review_ui
        from rig_browser import verify_browser
        path=Path(self.temp.name)/'viewer';create_review_ui(self.rig,path);report=verify_browser(self.rig,path)
        self.assertTrue(report['ok']);self.assertTrue(report['ui_playback']);self.assertTrue(report['ui_weights'])


class RigBlenderPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.rig=fixture(self.root)

    def test_existing_export_and_sidecar_are_preserved_before_worker(self):
        from unittest.mock import patch
        from rig_blender import export_blender
        target=self.root/'existing.blend';target.write_bytes(b'user blend')
        with patch('rig_blender._run') as worker:
            with self.assertRaises(RigError):export_blender(self.rig,target)
            worker.assert_not_called()
        self.assertEqual(target.read_bytes(),b'user blend')
        target.unlink();sidecar=target.with_suffix('.roundtrip.glb');sidecar.write_bytes(b'user glb')
        with patch('rig_blender._run') as worker:
            with self.assertRaises(RigError):export_blender(self.rig,target)
            worker.assert_not_called()
        self.assertEqual(sidecar.read_bytes(),b'user glb');self.assertFalse(target.exists())

    def test_racing_main_preserves_writer_and_retains_verified_sidecar(self):
        from rig_blender import _publish_outputs
        a=self.root/'stage-glb';b=self.root/'stage-blend'
        a.write_bytes(b'verified glb');b.write_bytes(b'verified blend')
        sidecar=self.root/'out.roundtrip.glb';target=self.root/'out.blend'
        target.mkdir()
        with self.assertRaises(RigError):_publish_outputs([(a,sidecar),(b,target)])
        self.assertTrue(target.is_dir());self.assertEqual(list(target.iterdir()),[])
        self.assertEqual(sidecar.read_bytes(),b'verified glb');self.assertEqual(b.read_bytes(),b'verified blend')

    def test_worker_failure_and_invalid_clip_do_not_create_preview_destination(self):
        from unittest.mock import patch
        from rig_blender import render_blender_preview
        target=self.root/'preview'
        with self.assertRaises(RigError):render_blender_preview(self.rig,target,clip='missing')
        self.assertFalse(target.exists())
        with patch('rig_blender.export_blender',side_effect=RigError('TEST_FAILURE','Injected worker failure.')):
            with self.assertRaises(RigError):render_blender_preview(self.rig,target,clip='bend')
        self.assertFalse(target.exists());self.assertEqual(list(self.root.glob('.hatch-blender-render-*')),[])

    def test_output_parent_alias_is_canonicalized_and_leaf_links_rejected(self):
        from rig_blender import _new_output
        alias=self.root/'output-parent-alias'
        actual=self.root/'actual-output-parent';actual.mkdir()
        try:alias.symlink_to(actual,target_is_directory=True)
        except OSError as exc:
            if getattr(exc,'winerror',None)==1314:self.skipTest('Symlink privilege unavailable.')
            raise
        target=_new_output(alias/'new-folder'/'new.blend','RIG_EXPORT_EXISTS')
        self.assertEqual(target,actual.resolve()/'new-folder'/'new.blend')
        self.assertFalse(target.exists())
        outside=self.root/'outside.blend';outside.write_bytes(b'preserved')
        leaf=actual/'leaf.blend';leaf.symlink_to(outside)
        with self.assertRaises(RigError) as caught:
            _new_output(alias/'leaf.blend','RIG_EXPORT_EXISTS')
        self.assertEqual(caught.exception.report['code'],'RIG_SYMLINK')
        self.assertEqual(outside.read_bytes(),b'preserved')
        dangling=actual/'dangling.blend';dangling.symlink_to(actual/'missing.blend')
        with self.assertRaises(RigError):
            _new_output(alias/'dangling.blend','RIG_EXPORT_EXISTS')
        self.assertTrue(dangling.is_symlink())

    def test_symlink_output_does_not_touch_referent(self):
        from rig_blender import export_blender
        real=self.root/'real.blend';real.write_bytes(b'preserved')
        link=self.root/'link.blend'
        try:link.symlink_to(real)
        except OSError as exc:
            if getattr(exc,'winerror',None)==1314:self.skipTest('Symlink privilege unavailable.')
            raise
        with self.assertRaises(RigError):export_blender(self.rig,link)
        self.assertEqual(real.read_bytes(),b'preserved');self.assertTrue(link.is_symlink())

    def test_supplied_blend_is_frozen_before_worker_and_receipt_hashing(self):
        from unittest.mock import patch
        from rig_blender import render_blender_preview
        original=self.root/'supplied.blend';original.write_bytes(b'original saved source')
        destination=self.root/'frozen-preview'
        def inspect(job,**kwargs):
            self.assertNotEqual(Path(job['input']),original)
            self.assertEqual(Path(job['input']).read_bytes(),b'original saved source')
            original.write_bytes(b'changed live source')
            self.assertEqual(Path(job['input']).read_bytes(),b'original saved source')
            raise RigError('TEST_DONE','Snapshot inspection complete.')
        with patch('rig_blender._run',side_effect=inspect):
            with self.assertRaisesRegex(RigError,'Snapshot inspection complete'):
                render_blender_preview(self.rig,destination,clip='bend',blend_path=original,blend_sha256=sha256(original))
        self.assertFalse(destination.exists())
        self.assertEqual(original.read_bytes(),b'changed live source')

    def test_supplied_blend_requires_unchanged_exact_reference(self):
        from unittest.mock import patch
        from rig_blender import render_blender_preview
        source=self.root/'changed.blend';source.write_bytes(b'original')
        expected=sha256(source);source.write_bytes(b'modified')
        destination=self.root/'untrusted-preview'
        with patch('rig_blender._run') as worker:
            with self.assertRaises(RigError):
                render_blender_preview(self.rig,destination,clip='bend',blend_path=source)
            with self.assertRaises(RigError):
                render_blender_preview(self.rig,destination,clip='bend',blend_path=source,blend_sha256=expected)
            worker.assert_not_called()
        self.assertFalse(destination.exists());self.assertEqual(source.read_bytes(),b'modified')
