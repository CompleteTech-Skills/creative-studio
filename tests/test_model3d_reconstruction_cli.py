"""Actual CLI/request geometry, evidence, path safety and cancellation contracts."""
from pathlib import Path
from copy import deepcopy
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_reconstruction_cli import schema,schemas,capabilities,validate_request,execute_request,main
from rig_errors import RigError
from rig_import import load_character_model


def request(method,**fields):
    return dict(schema_version=1,kind='hatch-model3d-reconstruction-request',method=method,**fields)


def contour():return request('contour-extrusion',outer=[[0,0],[3,0],[3,3],[0,3]],holes=[[[1,1],[2,1],[2,2],[1,2]]],thickness_m=.5)


def volume(mesh):
    p=mesh.positions-mesh.positions.mean(axis=0);t=mesh.triangles
    return np.einsum('ij,ij->',p[t[:,0]],np.cross(p[t[:,1]],p[t[:,2]]))/6


class ReconstructionRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.sources=self.root/'sources';self.sources.mkdir()
    def invoke(self,args):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=main(args)
        return code,json.loads(output.getvalue())
    def write(self,value):
        path=self.root/'request.json';path.write_text(json.dumps(value,indent=2)+'\n');return path
    def image(self,name='source.png',mask=None):
        path=self.sources/name;path.parent.mkdir(parents=True,exist_ok=True)
        if mask is None:
            image=Image.new('RGBA',(8,8),(90,40,20,0))
            for y in range(1,7):
                for x in range(1,7):image.putpixel((x,y),(90,40,20,255))
        else:image=Image.fromarray(mask.astype('uint8')*255)
        image.save(path);return path
    def hull(self):
        mask=np.zeros((16,16),bool);mask[4:12,4:12]=True
        self.image('front.png',mask);self.image('side.png',mask)
        front=np.eye(4);front[2,3]=3
        side=np.array([[0,0,1,3],[0,1,0,0],[-1,0,0,0],[0,0,0,1]])
        views=[]
        for name,matrix in [('front',front),('side',side)]:
            views.append(dict(name=name,mask=dict(path=name+'.png',role='mask'),interpretation=dict(channel='luminance',threshold=127),
                camera=dict(calibration='known',camera_to_world=matrix.tolist(),orthographic=[-1,1,-1,1],near_m=.1,far_m=10)))
        return request('visual-hull',views=views,bounds_m=[[-1,-1,-1],[1,1,1]],resolution=[4,4,4])

    def test_schema_discovery_is_fresh_valid_and_numerical_dependency_free(self):
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema());self.assertEqual(set(schemas()),{'model3d-reconstruction-request.schema.json'})
        modified=schema();modified.clear();self.assertIn('oneOf',schema())
        path=Path(__file__).resolve().parents[1]/'scripts/model3d_reconstruction_cli.py'
        for command in ('schema','capabilities'):
            output=subprocess.run([sys.executable,'-S',str(path),command],capture_output=True,text=True)
            self.assertEqual(output.returncode,0,output.stderr);self.assertIsInstance(json.loads(output.stdout),dict)
        self.assertFalse(capabilities()['provider_calls'])

    def test_contour_cli_hole_geometry_export_and_exact_request_hashes(self):
        value=contour();path=self.write(value);original=path.read_bytes();destination=self.root/'out'
        code,result=self.invoke(['request','--file',str(path),'--root',str(self.sources),'--destination',str(destination),'--export-format','gltf','--report'])
        self.assertEqual(code,0);self.assertTrue(result['ok']);self.assertEqual(result['export_format'],'gltf')
        mesh=load_character_model(destination/'mesh/model.gltf').geometry.meshes[0]
        self.assertAlmostEqual(volume(mesh),4.,places=6)
        self.assertEqual((destination/'request.json').read_bytes(),original)
        for name,record in result['acceptance']['files'].items():
            payload=(destination/name).read_bytes();self.assertEqual(hashlib.sha256(payload).hexdigest(),record['sha256'])
        self.assertEqual(result['report']['request_binding']['sha256'],hashlib.sha256(original).hexdigest())
        self.assertEqual(path.read_bytes(),original)

    def test_revolution_and_heightfield_requests_export_real_meshes(self):
        values=[request('profile-revolution',profile=[[1,0],[1,1]],segments=16,cap_ends=True,export_format='obj'),request('heightfield',heights_m=[[0,.2],[.2,.4]],spacing_m=[1,1],base_thickness_m=.5)]
        for i,value in enumerate(values):
            path=self.write(value);destination=self.root/('out'+str(i))
            code,result=self.invoke(['request','--file',str(path),'--root',str(self.sources),'--destination',str(destination)])
            self.assertEqual(code,0,result)
            mesh=load_character_model(destination/'mesh'/('model.'+result['export_format'])).geometry.meshes[0]
            self.assertGreater(volume(mesh),0);self.assertGreater(len(mesh.triangles),4)

    def test_image_and_explicit_mask_request_preserves_source_resolution_and_bytes(self):
        source=self.image();mask=self.image('segmentation.png',np.pad(np.ones((4,4),bool),2))
        value=request('mask-extrusion',source=dict(path=source.name,role='source'),mask=dict(path=mask.name,role='mask'),
                      interpretation=dict(channel='luminance',threshold=127,stride=2),pixel_size_m=.1,thickness_m=.2)
        before={p.name:p.read_bytes() for p in (source,mask)}
        result=execute_request(value,root=self.sources,destination=self.root/'out')
        mesh=load_character_model(self.root/'out/mesh/model.glb').geometry.meshes[0]
        self.assertAlmostEqual(volume(mesh),4*.2*.2*.2,places=7)
        self.assertEqual(result['report']['parameters']['pixel_size_m'],.2)
        for descriptor in result['report']['sources']:
            self.assertEqual(descriptor['dimensions_px'],[8,8]);self.assertEqual((self.root/'out'/descriptor['path']).read_bytes(),before[descriptor['name']])
        for path in (source,mask):self.assertEqual(path.read_bytes(),before[path.name])

    def test_calibrated_multiview_request_has_actual_reprojection_and_occupancy(self):
        value=self.hull();result=execute_request(value,root=self.sources,destination=self.root/'hull')
        self.assertEqual([m['iou'] for m in result['report']['reprojection']],[1.,1.])
        occupancy=np.frombuffer((self.root/'hull/occupancy.bin').read_bytes(),dtype=np.uint8).reshape(4,4,4)
        self.assertEqual(int(occupancy.sum()),8)
        mesh=load_character_model(self.root/'hull/mesh/model.glb').geometry.meshes[0]
        self.assertAlmostEqual(volume(mesh),1.)
        self.assertEqual(result['report']['request_binding']['method'],'visual-hull')

    def test_unknown_methods_versions_and_nested_fields_reject_before_source_read(self):
        cases=[]
        for key,value in [('method','provider-command'),('schema_version',2),('python_command','echo nope')]:
            wrong=contour();wrong[key]=value;cases.append(wrong)
        wrong=self.hull();wrong['views'][0]['camera']['execute']='arbitrary';cases.append(wrong)
        wrong=request('profile-revolution',profile=[[True,0],[1,1]],segments=16,cap_ends=True);cases.append(wrong)
        with patch('model3d_reconstruction.read_image',side_effect=AssertionError('source read before validation')):
            for value in cases:
                with self.subTest(value=value),self.assertRaises(RigError):execute_request(value,root=self.sources,destination=self.root/'bad')
                self.assertFalse((self.root/'bad').exists())

    def test_absolute_traversal_device_and_symlink_sources_reject(self):
        source=self.image()
        base=request('mask-extrusion',source=dict(path=source.name,role='source'),interpretation=dict(channel='alpha',threshold=127),pixel_size_m=.1,thickness_m=.2)
        for name in ('../escape.png','/absolute.png','C:/escape.png','NUL','a\\b.png'):
            wrong=deepcopy(base);wrong['source']['path']=name
            with self.subTest(name=name),self.assertRaises(RigError):execute_request(wrong,root=self.sources,destination=self.root/'bad')
        alias=self.sources/'alias.png'
        try:alias.symlink_to(source)
        except OSError:self.skipTest('Host symlink creation unavailable.')
        wrong=deepcopy(base);wrong['source']['path']=alias.name
        with self.assertRaises(RigError):execute_request(wrong,root=self.sources,destination=self.root/'bad')
        self.assertTrue(alias.is_symlink());self.assertFalse((self.root/'bad').exists())

    def test_portable_source_aliases_and_prefix_collisions_reject_before_capture(self):
        for sources in ([dict(path='A.png',role='source'),dict(path='a.png',role='source')],
                        [dict(path='a.png',role='source'),dict(path='a.png/nested.png',role='source')],
                        [dict(path='cafe\u0301.png',role='source')]):
            value=contour();value['sources']=sources
            with self.assertRaises(RigError) as caught:validate_request(value)
            self.assertEqual(caught.exception.report['code'],'RECON_REQUEST_PATH')

    def test_source_closure_is_frozen_before_compute_even_if_original_changes(self):
        source=self.image();original=source.read_bytes()
        value=request('mask-extrusion',source=dict(path=source.name,role='source'),interpretation=dict(channel='alpha',threshold=127),pixel_size_m=.1,thickness_m=.2)
        def change(stage):
            if stage=='compute':source.write_bytes(b'original changed after capture')
        result=execute_request(value,root=self.sources,destination=self.root/'out',on_stage=change)
        retained=self.root/'out'/result['report']['sources'][0]['path']
        self.assertEqual(retained.read_bytes(),original)
        self.assertAlmostEqual(volume(load_character_model(self.root/'out/mesh/model.glb').geometry.meshes[0]),36*.1*.1*.2,places=6)

    def test_cooperative_cancellation_at_each_boundary_never_publishes_partial_output(self):
        for stopped in ('capture','compute','export','publish'):
            destination=self.root/stopped
            def cancel(stage):
                if stage==stopped:raise RigError('TEST_CANCEL','Requested cancellation')
            with self.subTest(stage=stopped),self.assertRaises(RigError):execute_request(contour(),root=self.sources,destination=destination,on_stage=cancel)
            self.assertFalse(destination.exists());self.assertFalse(list(self.root.glob('.recon-request-*')))

    def test_publication_race_preserves_other_writer_and_stage_cleanup(self):
        destination=self.root/'out'
        def race(stage):
            if stage=='publish':destination.mkdir();(destination/'owned').write_bytes(b'keep')
        with self.assertRaises(RigError) as caught:execute_request(contour(),root=self.sources,destination=destination,on_stage=race)
        self.assertEqual(caught.exception.report['code'],'RECON_EXISTS')
        self.assertEqual((destination/'owned').read_bytes(),b'keep');self.assertFalse(list(self.root.glob('.recon-request-*')))

    def test_unknown_calibration_and_mismatched_masks_fail_without_publication(self):
        unknown=self.hull();unknown['views'][0]['camera']['calibration']='unknown'
        with self.assertRaises(RigError) as caught:execute_request(unknown,root=self.sources,destination=self.root/'unknown')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_HULL_CALIBRATION');self.assertFalse((self.root/'unknown').exists())
        source=self.image();self.image('wrong.png',np.ones((3,3),bool))
        value=request('mask-extrusion',source=dict(path=source.name,role='source'),mask=dict(path='wrong.png',role='mask'),interpretation=dict(channel='luminance',threshold=127),pixel_size_m=.1,thickness_m=.2)
        with self.assertRaises(RigError):execute_request(value,root=self.sources,destination=self.root/'wrong')
        self.assertFalse((self.root/'wrong').exists())

    def test_request_decoder_limits_duplicate_keys_and_original_byte_mismatch(self):
        path=self.root/'request.json';path.write_text('{"method":"contour-extrusion","method":"bad"}')
        code,result=self.invoke(['validate-request','--file',str(path)])
        self.assertEqual(code,2);self.assertEqual(result['error']['code'],'RIG_JSON_DUPLICATE')
        with self.assertRaises(RigError):execute_request(contour(),root=self.sources,destination=self.root/'bad',request_bytes=b'{}')
        with patch('model3d_reconstruction_cli.MAX_REQUEST_BYTES',100):
            with self.assertRaises(RigError):validate_request(contour())
        self.assertFalse((self.root/'bad').exists())

    def test_original_request_boolean_cannot_compare_equal_to_valid_numeric_field(self):
        value=contour()
        for field in ('schema_version','thickness_m'):
            raw=deepcopy(value);raw[field]=True
            with self.subTest(field=field),self.assertRaises(RigError) as caught:
                execute_request(value,root=self.sources,destination=self.root/'bad',request_bytes=json.dumps(raw).encode())
            self.assertEqual(caught.exception.report['code'],'RECON_REQUEST_SCHEMA')
            self.assertFalse((self.root/'bad').exists())
        raw=deepcopy(value);raw['schema_version']=1.0
        with self.assertRaises(RigError) as caught:execute_request(value,root=self.sources,destination=self.root/'bad',request_bytes=json.dumps(raw).encode())
        self.assertEqual(caught.exception.report['code'],'RECON_REQUEST_CHANGED')

    def test_validate_request_has_no_source_reads_and_help_executes(self):
        path=self.write(request('mask-extrusion',source=dict(path='missing.png',role='source'),interpretation=dict(channel='alpha',threshold=127),pixel_size_m=.1,thickness_m=.2))
        code,result=self.invoke(['validate-request','--file',str(path)])
        self.assertEqual(code,0);self.assertFalse(result['source_read'])
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as caught:main(['request','--help'])
        self.assertEqual(caught.exception.code,0)

if __name__=='__main__':unittest.main()
