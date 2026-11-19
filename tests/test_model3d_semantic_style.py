from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_primitives import primitive
from model3d_semantic import create_asset,define_pose,evaluated_geometry,part_signatures
from model3d_semantic_evidence import map_states
from model3d_semantic_io import export_asset
from model3d_semantic_style import apply_style_materials,apply_catalog_state
from rig_models import Geometry
from rig_import import load_character_model
from rig_errors import RigError


class StyleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        a=primitive('box',name='body').mesh;b=primitive('box',name='head').mesh
        self.asset=create_asset(Geometry([a,b],[dict(name='source-metal',pbrMetallicRoughness=dict(
            baseColorFactor=[.2,.4,.6,1.],metallicFactor=.8,roughnessFactor=.1))]),fixture=True)

    def test_registry_style_changes_actual_exported_pbr_preserving_source_geometry_color(self):
        original=part_signatures(self.asset);styled=apply_style_materials(self.asset,'clay',parts=['body'])
        materials=styled.geometry.materials;body=styled.geometry.by_id()['body'];head=styled.geometry.by_id()['head']
        self.assertEqual(materials[body.material]['pbrMetallicRoughness']['roughnessFactor'],.92)
        self.assertEqual(materials[head.material]['pbrMetallicRoughness']['roughnessFactor'],.1)
        self.assertEqual(materials[body.material]['pbrMetallicRoughness']['baseColorFactor'],[.2,.4,.6,1.])
        export_asset(styled,self.root/'export');decoded=load_character_model(self.root/'export/model.glb')
        material=decoded.geometry.materials[decoded.geometry.by_id()['body'].material]
        self.assertEqual(material['pbrMetallicRoughness']['roughnessFactor'],.92)
        self.assertEqual(material['pbrMetallicRoughness']['metallicFactor'],0.)
        np.testing.assert_array_equal(decoded.geometry.by_id()['body'].positions,self.asset.geometry.by_id()['body'].positions)
        self.assertEqual(part_signatures(self.asset),original);self.assertTrue(decoded.report['fixture'])

    def test_catalog_state_executes_actual_pose_and_unavailable_does_not_fallback(self):
        matrix=np.eye(4);matrix[0,3]=.75
        asset=define_pose(self.asset,'wave',{'head':matrix});asset=map_states(asset,{'waving':'wave','running':None})
        posed=apply_catalog_state(asset,'waving')
        before=evaluated_geometry(asset).by_id()['head'].positions;after=evaluated_geometry(posed).by_id()['head'].positions
        np.testing.assert_allclose(after-before,np.tile([.75,0,0],(len(before),1)))
        self.assertEqual(posed.history[-1]['operation'],'apply-existing-catalog-state:waving')
        with self.assertRaises(RigError):apply_catalog_state(asset,'running')
        stale=deepcopy(asset);stale.states['waving']['catalog_sha256']='0'*64
        with self.assertRaises(RigError):apply_catalog_state(stale,'waving')

    def test_unsupported_style_or_shader_fails_without_partial_mutation(self):
        before=part_signatures(self.asset)
        with self.assertRaises(RigError):apply_style_materials(self.asset,'pixel')
        shader=deepcopy(self.asset);shader.geometry.materials[0]['extensions']={'KHR_materials_unlit':{}}
        shader.geometry.extensions_used=['KHR_materials_unlit']
        with self.assertRaises(RigError):apply_style_materials(shader,'clay')
        self.assertEqual(part_signatures(self.asset),before)
        opaque=deepcopy(self.asset);opaque.geometry.materials[0]['extras']=['opaque',{'customTexture':999}]
        styled=apply_style_materials(opaque,'clay')
        slot=styled.geometry.by_id()['body'].material
        self.assertEqual(styled.geometry.materials[slot]['extras'],['opaque',{'customTexture':999}])
        with self.assertRaises(RigError):apply_style_materials(self.asset,'clay',parts=[{}])

    def test_returned_evidence_cannot_mutate_future_recipe_execution(self):
        first=apply_style_materials(self.asset,'clay')
        first.parts['body']['evidence'][-1]['material_changes'][0]['after']['roughness']=.05
        self.assertEqual(first.parts['head']['evidence'][-1]['material_changes'][0]['after']['roughness'],.92)
        second=apply_style_materials(self.asset,'clay')
        slot=second.geometry.by_id()['body'].material
        self.assertEqual(second.geometry.materials[slot]['pbrMetallicRoughness']['roughnessFactor'],.92)


    def test_metallic_roughness_texture_is_refused_not_misrecorded(self):
        from io import BytesIO
        from PIL import Image
        from rig_models import TextureImage
        png=BytesIO();Image.new('RGB',(2,2),(0,128,255)).save(png,format='PNG')
        textured=deepcopy(self.asset)
        textured.geometry.images=[TextureImage('fixture','image/png',png.getvalue())]
        textured.geometry.textures=[{'source':0}]
        for mesh in textured.geometry.meshes:
            mesh.attributes['TEXCOORD_0']=np.zeros((len(mesh.positions),2))
        textured.geometry.materials[0]['pbrMetallicRoughness']['metallicRoughnessTexture']={'index':0}
        with self.assertRaises(RigError) as caught:apply_style_materials(textured,'plush')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_STYLE_UNSUPPORTED')

    def test_unknown_style_is_a_structured_refusal(self):
        with self.assertRaises(RigError) as caught:apply_style_materials(self.asset,'no-such-style')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_STYLE_UNSUPPORTED')

    def test_catalog_state_refuses_scaled_sheared_or_mirrored_poses(self):
        for label,edit in (('scale',lambda m:m.__setitem__((0,0),3.)),('shear',lambda m:m.__setitem__((0,1),2.)),
                           ('mirror',lambda m:m.__setitem__((0,0),-1.))):
            with self.subTest(label=label):
                matrix=np.eye(4);edit(matrix)
                asset=map_states(define_pose(self.asset,'odd',{'head':matrix}),{'waving':'odd'})
                with self.assertRaises(RigError) as caught:apply_catalog_state(asset,'waving')
                self.assertEqual(caught.exception.report['code'],'MODEL3D_STATE_NOT_RIGID')

if __name__=='__main__':unittest.main()
