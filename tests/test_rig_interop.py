from copy import deepcopy
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from rig_test_helpers import fixture
from rig_errors import RigError,atomic_json,sha256
from rig_gltf import export_gltf,export_geometry,read_gltf,GltfBuilder
from rig_import import load_character_model
from rig_interop import compare_imported,validate_gltf
from rig_models import TextureImage, RigAsset
from rig_jobs import create_run, save_rig
from rig_contracts import DEFAULT_LIMITS
from rig_skinning import generate_skin_weights, evaluate_deformation
from rig_skeleton import neutral_pose
from test_rig_skinning import pruning_fixture


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
        self.assertIs(report['issues']['truncated'], False)
        self.assertEqual(report['asset_sha256'], sha256(path))
        gltf=GLTF2().load(str(path));self.assertGreater(len(gltf.skins[0].joints),0);self.assertGreater(len(gltf.animations[0].channels),0)


    def eight_influence_rig(self):
        geometry, skeleton = pruning_fixture()
        parent = Path(self.temp.name)
        source = parent/'eight-source.glb'
        export_geometry(geometry, source, provenance={'fixture': True})
        rig = create_run(source, parent/'eight-fixture', identifier='eight-fixture',
                         fixture=True, limits={**DEFAULT_LIMITS, 'influences': 8})
        rig.manifest['skeleton'] = skeleton
        rig.weights = generate_skin_weights(rig.geometry, skeleton, method='distance',
                                             limits=rig.manifest['limits'])
        save_rig(rig, operation='eight-influence-test-fixture')
        return rig

    def test_eight_influence_sets_roundtrip_with_actual_second_set_deformation(self):
        rig = self.eight_influence_rig()
        source = rig.root/rig.manifest['source']['path']
        source_hash = sha256(source)
        expected_indices = [1, 4, 8, 0, 2, 3, 5, 6]
        expected_weights = np.array([2, 2, 2, 1, 1, 1, 1, 1])/11
        for extension in ('glb', 'gltf'):
            with self.subTest(format=extension):
                path = Path(self.temp.name)/('eight.'+extension)
                export_gltf(rig, path)
                document = read_gltf(path)
                attributes = document.json['meshes'][0]['primitives'][0]['attributes']
                self.assertTrue({'JOINTS_0', 'WEIGHTS_0', 'JOINTS_1', 'WEIGHTS_1'} <= set(attributes))
                for influence_set in (0, 1):
                    start = 4*influence_set
                    np.testing.assert_array_equal(
                        document.accessor(attributes['JOINTS_'+str(influence_set)]),
                        np.tile(expected_indices[start:start+4], (3, 1)))
                    np.testing.assert_allclose(
                        document.accessor(attributes['WEIGHTS_'+str(influence_set)]),
                        np.tile(expected_weights[start:start+4], (3, 1)), atol=1e-7)
                # The ordinary four-influence consumer profile must reject extra sets.
                with self.assertRaises(RigError) as caught:
                    load_character_model(path)
                self.assertEqual(caught.exception.report['code'], 'RIG_INFLUENCE_PROFILE')
                imported = load_character_model(path, limits=rig.manifest['limits'])
                skin = imported.weights['pruning']
                np.testing.assert_array_equal(skin.indices, np.tile(expected_indices, (3, 1)))
                np.testing.assert_allclose(skin.weights, np.tile(expected_weights, (3, 1)), atol=1e-7)
                rebound = RigAsset({'skeleton': imported.skeleton}, imported.geometry, imported.weights)
                np.testing.assert_allclose(evaluate_deformation(rebound)['pruning']['positions'],
                                           rig.geometry.meshes[0].positions, atol=1e-7)
                pose = neutral_pose(imported.skeleton)
                translation = next(b['rest']['translation'] for b in imported.skeleton['bones'] if b['id'] == 'j6')
                pose['bones']['j6'] = {'translation': [translation[0]+11, translation[1], translation[2]]}
                # j6 is the LAST slot in WEIGHTS_1 and contributes 1/11.
                np.testing.assert_allclose(evaluate_deformation(rebound, pose)['pruning']['positions'],
                                           rig.geometry.meshes[0].positions+[1, 0, 0], atol=1e-6)
                if extension == 'glb':
                    # Decode actual exported bytes independently of the native accessor reader.
                    payload = path.read_bytes()
                    self.assertEqual(payload[:4], b'glTF')
                    json_length, json_kind = struct.unpack_from('<II', payload, 12)
                    self.assertEqual(json_kind, 0x4e4f534a)
                    raw = json.loads(payload[20:20+json_length])
                    binary_start = 20+json_length+8
                    for semantic, expected, format_code in (
                        ('JOINTS_1', expected_indices[4:], 'H'),
                        ('WEIGHTS_1', expected_weights[4:], 'f'),
                    ):
                        accessor = raw['accessors'][attributes[semantic]]
                        view = raw['bufferViews'][accessor['bufferView']]
                        offset = binary_start+view.get('byteOffset', 0)+accessor.get('byteOffset', 0)
                        actual = struct.unpack_from('<'+format_code*12, payload, offset)
                        np.testing.assert_allclose(np.array(actual).reshape(3, 4),
                                                   np.tile(expected, (3, 1)), atol=1e-7)
        self.assertEqual(sha256(source), source_hash)
        invalid = deepcopy(rig)
        invalid.manifest['limits']['influences'] = 4
        with self.assertRaises(RigError) as caught:
            export_gltf(invalid, Path(self.temp.name)/'four-profile.glb')
        self.assertEqual(caught.exception.report['code'], 'RIG_SKIN_SHAPE')

    @unittest.skipUnless(os.environ.get('HATCH_RIG_INTEROP_TEST')=='1',
                         'Enable HATCH_RIG_INTEROP_TEST=1 for independent optional validators.')
    def test_khronos_and_pygltflib_accept_eight_influence_export(self):
        from pygltflib import GLTF2
        rig = self.eight_influence_rig()
        path = Path(self.temp.name)/'eight-independent.glb'
        export_gltf(rig, path)
        report = validate_gltf(path)
        self.assertTrue(report['ok'])
        self.assertEqual(report['issues']['numErrors'], 0)
        self.assertIs(report['issues']['truncated'], False)
        self.assertEqual(report['asset_sha256'], sha256(path))
        gltf = GLTF2().load(str(path))
        attributes = gltf.meshes[0].primitives[0].attributes
        self.assertIsNotNone(attributes.JOINTS_1)
        self.assertIsNotNone(attributes.WEIGHTS_1)
        self.assertEqual(len(gltf.skins[0].joints), 10)


class KhronosReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'model.gltf'
        self.path.write_text('{"asset":{"version":"2.0"}}', encoding='utf-8')
        self.issues = {'truncated': False, 'numErrors': 0, 'numWarnings': 0}

    def validate_report(self, report, *, returncode=0):
        result = subprocess.CompletedProcess([], returncode, json.dumps(report), '')
        with patch('rig_interop.shutil.which', return_value='node'), patch(
            'rig_interop.subprocess.run', return_value=result
        ):
            return validate_gltf(self.path)

    def test_complete_report_preserves_details_and_exact_hash(self):
        report = {
            'validator': 'synthetic',
            'issues': {**self.issues, 'numWarnings': 1, 'messages': [{'severity': 1}]},
            'info': {'version': '2.0'},
        }
        self.assertEqual(
            self.validate_report(report),
            {**report, 'ok': True, 'asset_sha256': sha256(self.path)},
        )

    def test_incomplete_or_malformed_reports_fail_closed(self):
        cases = []
        for field, invalid_values in (
            ('truncated', (True, None, 0, 'false')),
            ('numErrors', (False, None, -1, '0', 0.0)),
            ('numWarnings', (False, None, -1, '0', 0.0)),
        ):
            cases.append({'issues': {key: value for key, value in self.issues.items() if key != field}})
            cases.extend({'issues': {**self.issues, field: value}} for value in invalid_values)
        for report in cases:
            with self.subTest(report=report):
                self.assertIs(self.validate_report({**report, 'ok': True})['ok'], False)

    def test_report_with_errors_or_unsuccessful_exit_does_not_pass(self):
        self.assertIs(self.validate_report({'issues': {**self.issues, 'numErrors': 1}})['ok'], False)
        self.assertIs(self.validate_report({'issues': self.issues}, returncode=1)['ok'], False)

    def test_invalid_report_structure_raises_structured_error(self):
        for report in ([], {}, {'issues': None}, {'issues': []}):
            with self.subTest(report=report):
                with self.assertRaises(RigError) as caught:
                    self.validate_report(report)
                self.assertEqual(caught.exception.report['code'], 'RIG_VALIDATOR_REPORT')


class RigExternalIntegrationTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.rig=fixture(self.temp.name)
    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_reopen_has_actual_armature_and_deformation(self):
        from rig_blender import export_blender
        report=export_blender(self.rig,Path(self.temp.name)/'editable.blend');self.assertTrue(report['ok']);self.assertTrue(report['reopened'])
        self.assertLess(report['max_vertex_error_m'],.01);self.assertGreater(report['compared_vertices'],0)
        self.assertEqual(report['schema_version'],1);self.assertEqual(report['format'],'blend')
    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST')=='1','Enable HATCH_RIG_BLENDER_TEST=1 for pinned Blender integration.')
    def test_blender_creation_receipt_does_not_claim_reopen(self):
        from rig_blender import export_blender
        report=export_blender(self.rig,Path(self.temp.name)/'created.blend',verify=False)
        self.assertTrue(report['ok']);self.assertFalse(report['reopened'])
        self.assertEqual(report['schema_version'],1);self.assertEqual(report['format'],'blend')
        self.assertNotIn('roundtrip_sha256',report);self.assertNotIn('compared_vertices',report)
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
