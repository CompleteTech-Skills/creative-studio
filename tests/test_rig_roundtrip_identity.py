from copy import deepcopy
import io
import os
from pathlib import Path
import tempfile
import unittest
import numpy as np
from PIL import Image
from rig_errors import RigError
from rig_gltf import export_gltf
from rig_import import load_character_model
from rig_interop import compare_imported,validate_gltf
from rig_models import TextureImage
from rig_test_helpers import fixture


class RoundtripIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.rig=fixture(self.temp.name)

    def encoded(self,rig,label,extension='glb'):
        path=Path(self.temp.name)/(label+'.'+extension);export_gltf(rig,path)
        return load_character_model(path),path

    def test_actual_glb_gltf_and_vertex_face_resource_reindexing_preserve_identity(self):
        for extension in ('glb','gltf'):
            imported,_=self.encoded(self.rig,'valid-'+extension,extension)
            self.assertTrue(compare_imported(self.rig,imported)['identity']['material_bindings'])
        changed=deepcopy(self.rig);mesh=changed.geometry.meshes[0]
        order=np.array([2,0,3,1]);inverse=np.argsort(order)
        mesh.positions=mesh.positions[order];mesh.triangles=inverse[mesh.triangles[::-1]][:,[1,2,0]]
        mesh.attributes={key:values[order] for key,values in mesh.attributes.items()}
        mesh.morphs={name:{key:values[order] for key,values in fields.items()} for name,fields in mesh.morphs.items()}
        skin=changed.weights[mesh.id];skin.indices=skin.indices[order];skin.weights=skin.weights[order]
        changed.geometry.materials.insert(0,dict(name='unused slot'));mesh.material+=1
        imported,_=self.encoded(changed,'reindexed')
        report=compare_imported(self.rig,imported)
        self.assertTrue(report['ok']);self.assertLess(report['max_vertex_error_m'],1e-5)

    def test_integer_native_fields_and_wider_float_storage_roundtrip(self):
        for dtype,label in ((np.int64,'integer'),(np.longdouble,'wide-float')):
            changed=deepcopy(self.rig);mesh=changed.geometry.meshes[0]
            mesh.positions=(mesh.positions*10).astype(dtype)
            mesh.attributes['TEXCOORD_0']=np.array([[0,0],[1,0],[0,1],[1,1]],dtype=dtype)
            mesh.morphs['blink']['POSITION']=np.zeros_like(mesh.positions,dtype=dtype)
            imported,_=self.encoded(changed,label)
            self.assertTrue(compare_imported(changed,imported)['ok'])

    def test_actual_encoded_winding_and_material_assignment_changes_reject(self):
        for label in ('winding','assignment','factor'):
            changed=deepcopy(self.rig);mesh=changed.geometry.meshes[0]
            if label=='winding':mesh.triangles=mesh.triangles[:,::-1].copy()
            elif label=='assignment':
                changed.geometry.materials.append(dict(name='red',pbrMetallicRoughness=dict(baseColorFactor=[1.,0.,0.,1.])))
                mesh.material=1
            else:changed.geometry.materials[0]['doubleSided']=True
            imported,_=self.encoded(changed,label)
            with self.subTest(label=label),self.assertRaises(RigError):compare_imported(self.rig,imported)

    def test_static_rest_and_unplayed_morphs_are_checked(self):
        self.rig.clips={};imported,_=self.encoded(self.rig,'static')
        report=compare_imported(self.rig,imported)
        self.assertTrue(report['static_rest_sample']);self.assertGreater(report['mesh_samples'],0)
        imported.geometry.meshes[0].morphs['blink']['POSITION'][:,2]+=.1
        with self.assertRaises(RigError):compare_imported(self.rig,imported)
        imported,_=self.encoded(self.rig,'static-second')
        imported.geometry.meshes[0].triangles=imported.geometry.meshes[0].triangles[:,::-1].copy()
        with self.assertRaises(RigError):compare_imported(self.rig,imported)

    def test_referenced_vertex_split_is_valid_but_loose_geometry_is_not_ignored(self):
        changed=deepcopy(self.rig);mesh=changed.geometry.meshes[0];skin=changed.weights[mesh.id]
        mesh.positions=np.vstack([mesh.positions,mesh.positions[1]])
        mesh.attributes={key:np.vstack([values,values[1]]) for key,values in mesh.attributes.items()}
        mesh.morphs={name:{key:np.vstack([values,values[1]]) for key,values in fields.items()}
                     for name,fields in mesh.morphs.items()}
        skin.indices=np.vstack([skin.indices,skin.indices[1]]);skin.weights=np.vstack([skin.weights,skin.weights[1]])
        mesh.triangles[0,1]=4
        imported,_=self.encoded(changed,'split-vertex')
        self.assertTrue(compare_imported(self.rig,imported)['ok'])
        # Leave the new vertex unreferenced and treat it as a separate inventory.
        mesh.triangles[0,1]=1
        imported,_=self.encoded(changed,'loose-vertex')
        self.assertEqual(compare_imported(changed,imported)['identity']['meshes']['strip']['loose_vertices'],1)
        with self.assertRaises(RigError):compare_imported(self.rig,imported)
        imported.geometry.meshes[0].positions[4,0]+=.2
        with self.assertRaises(RigError):compare_imported(changed,imported)

    def test_texture_budget_keeps_actionable_resource_diagnostic(self):
        self.textured()
        with self.assertRaises(RigError) as caught:compare_imported(self.rig,self.rig,memory_bytes=40)
        self.assertEqual(caught.exception.report['code'],'RIG_ROUNDTRIP_COMPARE_LIMIT')

    def textured(self):
        mesh=self.rig.geometry.meshes[0]
        mesh.attributes['TEXCOORD_0']=np.array([[0.,0.],[1.,0.],[0.,1.],[1.,1.]])
        encoded=io.BytesIO();Image.new('RGBA',(2,2),(240,100,20,255)).save(encoded,format='PNG',compress_level=0)
        self.rig.geometry.images=[TextureImage('test','image/png',encoded.getvalue())]
        self.rig.geometry.textures=[dict(source=0)]
        self.rig.geometry.materials[0]['pbrMetallicRoughness']=dict(baseColorTexture=dict(index=0))

    def test_material_resource_reorder_default_elision_and_lossless_reencoding(self):
        self.textured();changed=deepcopy(self.rig)
        # Resource table indices are storage details. Follow the references and
        # compare decoded pixels, retaining both original encoded hashes.
        encoded=io.BytesIO();Image.new('RGBA',(2,2),(240,100,20,255)).save(encoded,format='PNG',compress_level=9)
        self.assertNotEqual(encoded.getvalue(),changed.geometry.images[0].data)
        changed.geometry.images.insert(0,TextureImage('unused','image/png',encoded.getvalue()))
        changed.geometry.images[1].data=encoded.getvalue()
        changed.geometry.textures.insert(0,dict(source=0))
        changed.geometry.textures[1]['source']=1
        changed.geometry.materials[0]['pbrMetallicRoughness']['baseColorTexture'].update(index=1,texCoord=0)
        changed.geometry.materials[0].update(alphaMode='OPAQUE',doubleSided=False,emissiveFactor=[0.,0.,0.])
        imported,_=self.encoded(changed,'resources')
        report=compare_imported(self.rig,imported)
        self.assertTrue(report['ok']);self.assertIn('exact decoded RGBA',report['identity']['texture_comparison'])
        evidence=report['identity']['meshes']['strip']
        self.assertNotEqual(evidence['source_images']['pbr.baseColorTexture']['encoded_sha256'],
                            evidence['imported_images']['pbr.baseColorTexture']['encoded_sha256'])

    def test_texture_uv_binding_and_pixel_changes_reject(self):
        self.textured()
        for label in ('pixels','uv','sampler'):
            changed=deepcopy(self.rig)
            if label=='pixels':
                encoded=io.BytesIO();Image.new('RGBA',(2,2),(20,100,240,255)).save(encoded,format='PNG')
                changed.geometry.images[0].data=encoded.getvalue()
            elif label=='uv':changed.geometry.meshes[0].attributes['TEXCOORD_0'][0]=[.2,.2]
            else:
                changed.geometry.samplers=[dict(wrapS=33071)];changed.geometry.textures[0]['sampler']=0
            imported,_=self.encoded(changed,label)
            with self.subTest(label=label),self.assertRaises(RigError):compare_imported(self.rig,imported)

    @unittest.skipUnless(os.environ.get('HATCH_RIG_INTEROP_TEST')=='1','Enable real independent glTF validation.')
    def test_independent_validator_accepts_changed_valid_assets_identity_still_rejects(self):
        from pygltflib import GLTF2
        for label in ('winding','material'):
            changed=deepcopy(self.rig)
            if label=='winding':changed.geometry.meshes[0].triangles=changed.geometry.meshes[0].triangles[:,::-1].copy()
            else:changed.geometry.materials[0]['pbrMetallicRoughness']=dict(baseColorFactor=[1.,0.,0.,1.])
            imported,path=self.encoded(changed,'independent-'+label)
            independent=GLTF2().load(str(path));self.assertTrue(independent.meshes)
            self.assertTrue(validate_gltf(path)['ok'])
            with self.assertRaises(RigError):compare_imported(self.rig,imported)
