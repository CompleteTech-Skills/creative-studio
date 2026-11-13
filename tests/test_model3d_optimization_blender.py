"""Native planar optimization checks; engine gate is explicit and optional."""
import copy
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import numpy as np
from PIL import Image
from rig_models import Mesh,Geometry,TextureImage,load_geometry
from rig_errors import RigError
from model3d_optimization import mesh_hash
from model3d_optimization_blender import decimate_blender,_uv_error,_preflight,_protected_points
from rig_contracts import DEFAULT_LIMITS


def textured_cube(segments=8):
    positions=[];triangles=[];uv=[];normals=[]
    faces=[([1,0,0],[0,1,0],[0,0,1]),([-1,0,0],[0,0,1],[0,1,0]),
           ([0,1,0],[0,0,1],[1,0,0]),([0,-1,0],[1,0,0],[0,0,1]),
           ([0,0,1],[1,0,0],[0,1,0]),([0,0,-1],[0,1,0],[1,0,0])]
    for normal,u,v in faces:
        start=len(positions);normal=np.array(normal);u=np.array(u);v=np.array(v)
        for y in range(segments+1):
            for x in range(segments+1):
                positions.append(normal*.5+u*(x/segments-.5)+v*(y/segments-.5));normals.append(normal);uv.append([x/segments,y/segments])
        for y in range(segments):
            for x in range(segments):
                a=start+y*(segments+1)+x;b=a+1;c=b+segments+1;d=a+segments+1
                triangles.extend([[a,b,c],[a,c,d]])
    mesh=Mesh('box',np.array(positions,dtype=float),np.array(triangles),attributes={'NORMAL':np.array(normals,dtype=float),'TEXCOORD_0':np.array(uv)},name='Textured cube',region='prop')
    image=Image.new('RGBA',(8,8));image.putdata([(220,50,90,255) if (x+y)%2 else (30,120,220,255) for y in range(8) for x in range(8)])
    encoded=io.BytesIO();image.save(encoded,format='PNG')
    return Geometry([mesh],[dict(name='clay',pbrMetallicRoughness=dict(baseColorFactor=[.8,.9,1.,1.],metallicFactor=0.,roughnessFactor=.65,baseColorTexture={'index':0}))],
                    images=[TextureImage('checker','image/png',encoded.getvalue())],textures=[{'source':0}])


class BlenderOptimizationTests(unittest.TestCase):
    def test_unsupported_profiles_and_policies_reject_before_blender(self):
        for change in ('morph','tangent','texture-transform','sampler','budget','feature','normal-policy'):
            g=textured_cube(2);kwargs=dict(recompute_normals=True)
            if change=='morph':g.meshes[0].morphs={'shift':{'POSITION':np.zeros_like(g.meshes[0].positions)}}
            if change=='tangent':g.meshes[0].attributes['TANGENT']=np.tile([1.,0.,0.,1.],(len(g.meshes[0].positions),1))
            if change=='texture-transform':g.materials[0]['pbrMetallicRoughness']['baseColorTexture']['extensions']={'KHR_texture_transform':{}}
            if change=='sampler':g.samplers=[{}];g.textures[0]['sampler']=0
            if change=='budget':kwargs['budgets']={'triangles':True}
            if change=='feature':kwargs['protected_vertices']={'unknown':[0]}
            if change=='normal-policy':kwargs['recompute_normals']=False
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp,patch('model3d_optimization_blender._run') as run:
                with self.assertRaises(RigError):decimate_blender(g,Path(tmp)/'out',**kwargs)
                run.assert_not_called()
    def test_uv_metric_detects_mapping_loss(self):
        source=textured_cube(2).meshes[0];target=copy.deepcopy(source)
        self.assertLess(_uv_error(source,target)['max_error'],1e-8)
        target.attributes['TEXCOORD_0']+=2
        self.assertGreater(_uv_error(source,target)['max_error'],1.)
    def test_adjacent_shell_cannot_authenticate_wrong_uv_chart(self):
        positions=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
        uv=np.array([[0.,0.],[1.,0.],[0.,1.]])
        for separation in [.0001,1e-10]:
            source=Mesh('shells',np.concatenate([positions,positions+[0.,0.,separation]]),
                        np.array([[0,1,2],[3,4,5]]),attributes={'TEXCOORD_0':np.concatenate([uv,uv+2])})
            target=copy.deepcopy(source);target.attributes['TEXCOORD_0']=np.concatenate([uv+2,uv])
            self.assertGreater(_uv_error(source,target,distance_tolerance=.001)['max_error'],2.8)
    def test_translated_oblique_adjacent_sheets_cannot_swap_uv_charts(self):
        positions=np.array([[9998.,0.,0.],[9999.,0.,.5],[9998.,1.,0.]])
        uv=np.array([[0.,0.],[1.,0.],[0.,1.]])
        source=Mesh('sheets',np.concatenate([positions,positions+[0.,0.,1e-10]]),np.array([[0,1,2],[3,4,5]]),
                    attributes={'TEXCOORD_0':np.concatenate([uv,uv+2])})
        target=copy.deepcopy(source);target.attributes['TEXCOORD_0']=np.concatenate([uv+2,uv])
        self.assertGreater(_uv_error(source,target,distance_tolerance=.001)['max_error'],2.8)
        self.assertLess(_uv_error(source,source,distance_tolerance=.001)['max_error'],1e-8)
    def test_unsigned_positions_have_identical_geometric_hard_edges(self):
        positions=np.array([[10,10,10],[20,10,10],[10,20,11],[10,0,11]],dtype=np.uint32)
        triangles=np.array([[0,1,2],[1,0,3]])
        unsigned=Mesh('fold',positions,triangles);floating=copy.deepcopy(unsigned);floating.positions=positions.astype(float)
        a=_protected_points(unsigned,[],30);b=_protected_points(floating,[],30)
        self.assertEqual(len(a['edges']),4)
        np.testing.assert_array_equal(a['edges'],b['edges']);np.testing.assert_array_equal(a['points'],b['points'])
    def test_resource_and_numeric_admission(self):
        g=textured_cube(2)
        with self.assertRaises(RigError):_preflight(g,dict(DEFAULT_LIMITS,memory_bytes=1))
        with tempfile.TemporaryDirectory() as tmp,patch('model3d_optimization_blender._run') as run:
            for angle in [True,0,float('nan'),10**5000]:
                with self.assertRaises(RigError):decimate_blender(g,Path(tmp)/'out',angle_degrees=angle,recompute_normals=True)
            run.assert_not_called()
    @unittest.skipUnless(os.environ.get('HATCH_MODEL3D_DECIMATE_TEST')=='1','Enable real optional Blender planar optimization gate')
    def test_actual_texture_hardedge_save_reopen_export_and_reimport(self):
        from rig_import import load_character_model
        from rig_errors import read_json
        g=textured_cube();source_hash=mesh_hash(g.meshes[0])
        second=copy.deepcopy(g.meshes[0]);second.id='other';second.name='Second cube';second.positions[:,0]+=2
        g.meshes.append(second)
        owned=os.environ.get('HATCH_MODEL3D_DECIMATE_EVIDENCE')
        context=tempfile.TemporaryDirectory() if not owned else None
        try:
            root=Path(owned) if owned else Path(context.name);root.mkdir(parents=True,exist_ok=True)
            output=root/'verified'
            try:
                report=decimate_blender(g,output,recompute_normals=True,protected_vertices={'box':[40]},budgets={'triangles':1400},
                    executable=os.environ.get('HATCH_BLENDER'))
            except RigError as error:
                print(json.dumps(error.report,indent=2),flush=True);raise
            self.assertEqual(report['policy'],dict(max_distance_m=.001,min_silhouette_iou=.99,uv_tolerance=1e-4,
                point_tolerance_m=1e-5,recompute_normals=True,normal_mode='flat',protected_vertices={'box':[40]}))
            self.assertEqual(read_json(output/'optimization.json')['policy'],report['policy'])
            self.assertTrue(report['save_reopen']);self.assertEqual(len(report['parts']),2)
            self.assertLess(report['triangles'],g.triangle_count);self.assertTrue((output/'editable.blend').is_file())
            imported=load_character_model(output/'optimized.glb')
            self.assertEqual(len(imported.geometry.meshes),2)
            self.assertEqual(source_hash,mesh_hash(g.meshes[0]))
            source=load_geometry(output,read_json(output/'source.json'))
            np.testing.assert_array_equal(source.meshes[0].positions,g.meshes[0].positions)
            for part in report['parts']:
                self.assertLessEqual(part['uv']['max_error'],1e-4);self.assertLessEqual(part['protected_max_error_m'],1e-5)
            before=(output/'optimization.json').read_bytes()
            with self.assertRaises(RigError):decimate_blender(g,output,recompute_normals=True,executable=os.environ.get('HATCH_BLENDER'))
            self.assertEqual((output/'optimization.json').read_bytes(),before)
        finally:
            if context:context.cleanup()

if __name__=='__main__':unittest.main()
