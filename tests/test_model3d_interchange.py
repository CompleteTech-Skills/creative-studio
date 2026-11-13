"""Actual native-import roundtrips for standalone Model3D mesh export."""
import copy
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import numpy as np
from PIL import Image
from model3d_interchange import export_mesh
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from rig_import import load_character_model
from rig_models import Geometry, Mesh, TextureImage


def sample():
    positions=np.array([[0.,0.,0.],[2.,0.,0.],[0.,3.,1.]])
    mesh=Mesh('body',positions,np.array([[0,1,2]]),name='Body left eye',region='face')
    mesh.attributes={'NORMAL':np.tile(np.array([0.,-1.,3.])/np.sqrt(10),(3,1)),
                     'TEXCOORD_0':np.array([[0.,0.],[1.,0.],[0.,1.]])}
    return Geometry([mesh],[dict(name='Diffuse',pbrMetallicRoughness=dict(metallicFactor=0.,roughnessFactor=.65,
                                                                          baseColorFactor=[.3,.5,.7,1.]))])


class MeshInterchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_obj_actual_geometry_material_uv_normals_roundtrip_source_unchanged(self):
        geometry=sample();before=copy.deepcopy(geometry)
        manifest=export_mesh(geometry,self.root/'out',format='obj')
        result=load_character_model(self.root/'out/model.obj').geometry
        self.assertEqual(result.meshes[0].name,'Body left eye')
        np.testing.assert_allclose(result.meshes[0].positions,geometry.meshes[0].positions)
        for key in geometry.meshes[0].attributes:
            np.testing.assert_allclose(result.meshes[0].attributes[key],geometry.meshes[0].attributes[key])
        np.testing.assert_allclose(result.materials[result.meshes[0].material]['pbrMetallicRoughness']['baseColorFactor'],[.3,.5,.7,1.])
        self.assertEqual(result.materials[result.meshes[0].material]['name'],'Diffuse')
        np.testing.assert_array_equal(before.meshes[0].positions,geometry.meshes[0].positions)
        for name,record in manifest['files'].items():
            self.assertEqual(hashlib.sha256((self.root/'out'/name).read_bytes()).hexdigest(),record['sha256'])

    def test_obj_texture_original_bytes_and_multiple_semantic_objects(self):
        geometry=sample();image=io.BytesIO();Image.new('RGBA',(2,2),(20,40,60,255)).save(image,format='PNG')
        geometry.images=[TextureImage('source','image/png',image.getvalue())];geometry.textures=[dict(source=0)]
        geometry.materials[0]['pbrMetallicRoughness']['baseColorTexture']=dict(index=0)
        second=copy.deepcopy(geometry.meshes[0]);second.id='tail';second.name="Tail 'tip'";second.positions+=4
        geometry.meshes.append(second)
        export_mesh(geometry,self.root/'out',format='obj')
        imported=load_character_model(self.root/'out/model.obj').geometry
        self.assertEqual([m.name for m in imported.meshes],['Body left eye',"Tail 'tip'"])
        self.assertEqual(imported.images[0].data,image.getvalue())
        np.testing.assert_allclose(imported.meshes[1].positions,second.positions)

    def test_independent_obj_attribute_index_namespaces(self):
        geometry=sample();first=copy.deepcopy(geometry.meshes[0]);first.id='plain';first.name='Plain';first.attributes={}
        geometry.meshes.insert(0,first)
        export_mesh(geometry,self.root/'mixed',format='obj')
        imported=load_character_model(self.root/'mixed/model.obj').geometry
        np.testing.assert_allclose(imported.meshes[1].attributes['TEXCOORD_0'],geometry.meshes[1].attributes['TEXCOORD_0'])
        np.testing.assert_allclose(imported.meshes[1].attributes['NORMAL'],geometry.meshes[1].attributes['NORMAL'])

    def test_native_obj_reexport_ignores_unreferenced_default_material(self):
        (self.root/'source.mtl').write_text('newmtl "Eye diffuse"\nKd 0.2 0.4 0.6\n')
        (self.root/'source.obj').write_text('mtllib source.mtl\no "Left eye"\nusemtl "Eye diffuse"\n'
                                          'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        geometry=load_character_model(self.root/'source.obj').geometry
        self.assertEqual(geometry.meshes[0].material,1)
        manifest=export_mesh(geometry,self.root/'out',format='obj')
        self.assertEqual(manifest['exported_material_slots'],[dict(slot=1,name='Eye diffuse')])
        reimported=load_character_model(self.root/'out/model.obj').geometry
        self.assertEqual(reimported.meshes[0].name,'Left eye')
        np.testing.assert_allclose(reimported.meshes[0].positions,geometry.meshes[0].positions)
        material=reimported.materials[reimported.meshes[0].material]
        self.assertEqual(material['name'],'Eye diffuse')
        np.testing.assert_allclose(material['pbrMetallicRoughness']['baseColorFactor'],[.2,.4,.6,1.])

    def test_actual_glb_and_gltf_roundtrips(self):
        for fmt in ('glb','gltf'):
            with self.subTest(fmt=fmt):
                geometry=sample();export_mesh(geometry,self.root/fmt,format=fmt)
                imported=load_character_model(self.root/fmt/('model.'+fmt)).geometry
                self.assertEqual(imported.triangle_count,1)
                np.testing.assert_allclose(imported.meshes[0].positions,geometry.meshes[0].positions)
                np.testing.assert_allclose(imported.meshes[0].attributes['TEXCOORD_0'],geometry.meshes[0].attributes['TEXCOORD_0'])

    def test_existing_destination_and_symlink_preserved(self):
        target=self.root/'out';target.mkdir();(target/'sentinel').write_bytes(b'keep')
        with self.assertRaises(RigError):export_mesh(sample(),target,format='obj')
        self.assertEqual((target/'sentinel').read_bytes(),b'keep')
        link=self.root/'link'
        try:link.symlink_to(target,target_is_directory=True)
        except OSError:return
        with self.assertRaises(RigError):export_mesh(sample(),link,format='obj')
        self.assertTrue(link.is_symlink())

    def test_racing_destination_is_not_replaced_and_stage_removed(self):
        from atomic_publish import _rename_no_replace
        target=self.root/'out'
        def race(stage,destination):
            target.mkdir();(target/'sentinel').write_bytes(b'raced')
            return _rename_no_replace(stage,destination)
        with patch('model3d_interchange._rename_no_replace',side_effect=race),self.assertRaises(OSError):
            export_mesh(sample(),target,format='obj')
        self.assertEqual((target/'sentinel').read_bytes(),b'raced')
        self.assertEqual(list(self.root.iterdir()),[target])

    def test_json_integral_limits_copy_and_reject_invalid(self):
        limits={'vertices':3.0}
        export_mesh(sample(),self.root/'whole',format='obj',limits=limits)
        self.assertIs(type(limits['vertices']),float)
        for value in (True,3.5,float('inf'),0,10**5000):
            with self.subTest(value=value),self.assertRaises(RigError):
                export_mesh(sample(),self.root/'bad',format='obj',limits={'vertices':value})

    def test_losses_and_injection_rejected_without_publication(self):
        cases=[]
        g=sample();g.meshes[0].name='bad\no injected';cases.append(g)
        g=sample();g.meshes[0].morphs={'blink':{'POSITION':np.zeros((3,3))}};cases.append(g)
        g=sample();g.materials[0]['pbrMetallicRoughness']['metallicFactor']=.5;cases.append(g)
        g=sample();g.meshes[0].attributes['COLOR_0']=np.ones((3,4));cases.append(g)
        g=sample();g.materials=[{}];cases.append(g)
        g=sample();del g.materials[0]['pbrMetallicRoughness']['metallicFactor'];cases.append(g)
        g=sample();del g.materials[0]['pbrMetallicRoughness']['roughnessFactor'];cases.append(g)
        for i,g in enumerate(cases):
            with self.subTest(i=i),self.assertRaises(RigError):export_mesh(g,self.root/str(i),format='obj')
            self.assertFalse((self.root/str(i)).exists())

    def test_invalid_nonfinite_and_resource_caps(self):
        for bad in (float('nan'),float('inf')):
            g=sample();g.meshes[0].positions[0,0]=bad
            with self.assertRaises(RigError):export_mesh(g,self.root/'bad',format='obj')
        limits=dict(DEFAULT_LIMITS,memory_bytes=1024)
        with self.assertRaises(RigError):export_mesh(sample(),self.root/'cap',format='obj',limits=limits)
        limits=dict(DEFAULT_LIMITS,source_bytes=32)
        with self.assertRaises(RigError):export_mesh(sample(),self.root/'encoded',format='obj',limits=limits)
        self.assertEqual(list(self.root.iterdir()),[])


if __name__=='__main__':unittest.main()
