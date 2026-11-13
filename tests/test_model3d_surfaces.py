"""Real UV, texture, PBR and calibrated occlusion regressions."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))

from camera3d_math import look_at
from model3d_materials import FAMILIES,material,assign_material,procedural_texture,bind_texture,validate_texture
from model3d_uv import generate_uv
from model3d_projection import project_image,project_views
from model3d_primitives import primitive,editable_mesh
from model3d_interchange import export_mesh
from rig_errors import RigError
from rig_models import Geometry,TextureImage
from rig_import import load_character_model
from rig_math import compose


def source(color):
    data=io.BytesIO();Image.new('RGBA',(8,8),color).save(data,format='PNG')
    return TextureImage('observed','image/png',data.getvalue())


def projection():
    f=1/np.tan(np.pi/6);near=.1;far=100.
    return np.array([[f,0,0,0],[0,f,0,0],[0,0,(far+near)/(near-far),2*far*near/(near-far)],[0,0,-1,0.]])


def world():
    return compose((0,0,3),look_at((0,0,3),(0,0,0)))


class SurfaceTests(unittest.TestCase):
    def test_real_seeded_pngs_and_explicit_pbr_families(self):
        for family in FAMILIES:
            value=material(family,color=(.2,.4,.6,1.))
            self.assertEqual(value['pbrMetallicRoughness']['baseColorFactor'],[.2,.4,.6,1.])
            self.assertIn('roughnessFactor',value['pbrMetallicRoughness'])
        a=procedural_texture('stone',width=16,height=8,seed=7)
        b=procedural_texture('stone',width=16,height=8,seed=7)
        c=procedural_texture('stone',width=16,height=8,seed=8)
        self.assertEqual(a.data,b.data);self.assertNotEqual(a.data,c.data)
        report=validate_texture(a);self.assertEqual(report['pixels'],128)
        with Image.open(io.BytesIO(a.data)) as image:self.assertEqual(image.size,(16,8))
        with self.assertRaises(RigError):material('metal',roughness=True)
        with self.assertRaises(RigError):procedural_texture(width=4096,height=4096)
        with self.assertRaises(RigError):validate_texture(TextureImage('bad','image/png',a.data[:-12]))

    def test_padded_box_islands_preserve_native_attributes_and_morphs(self):
        geometry=primitive('box').geometry();mesh=geometry.meshes[0]
        delta=np.arange(len(mesh.positions)*3).reshape(-1,3)*.001
        mesh.morphs={'test':{'POSITION':delta.copy()}};before=mesh.positions.copy()
        mapped,report=generate_uv(geometry,method='box',resolution=(256,256),padding=2)
        result=mapped.meshes[0];indices=mesh.triangles.reshape(-1)
        self.assertEqual(len(result.positions),36)
        np.testing.assert_array_equal(result.positions,before[indices])
        np.testing.assert_array_equal(result.attributes['NORMAL'],mesh.attributes['NORMAL'][indices])
        np.testing.assert_array_equal(result.morphs['test']['POSITION'],delta[indices])
        np.testing.assert_array_equal(mesh.positions,before)
        self.assertNotIn('TEXCOORD_0',mesh.attributes)
        uv=result.attributes['TEXCOORD_0'];self.assertTrue(np.all(uv>=2/256));self.assertTrue(np.all(uv<=1-2/256))
        self.assertEqual(report['islands'],12);self.assertEqual(report['overlap']['interior_pairs'],0)
        self.assertTrue(0<report['uv_area']<1)
        with self.assertRaises(RigError):generate_uv(mapped)

    def test_mapping_modes_and_analytic_planar_coordinates(self):
        geometry=editable_mesh([[0,0,0],[2,0,0],[0,2,0]],[[0,1,2]]).geometry()
        mapped,report=generate_uv(geometry,method='planar',resolution=(100,100),padding=1)
        np.testing.assert_allclose(mapped.meshes[0].attributes['TEXCOORD_0'],[[.01,.01],[.99,.01],[.01,.99]])
        self.assertAlmostEqual(report['surface_area_m2'],2)
        for kind,method in [('cylinder','cylindrical'),('sphere','spherical')]:
            mapped,report=generate_uv(primitive(kind,{'segments':8}).geometry(),method=method)
            self.assertTrue(np.isfinite(mapped.meshes[0].attributes['TEXCOORD_0']).all())
            self.assertEqual(report['overlap']['interior_pairs'],0)
        with self.assertRaises(RigError):generate_uv(primitive('box').geometry(),method='planar')

    def test_unsupported_tangents_and_atlas_limits_fail_before_copy(self):
        geometry=primitive('box').geometry();geometry.meshes[0].attributes['TANGENT']=np.tile([1.,0,0,1],(8,1))
        with self.assertRaises(RigError):generate_uv(geometry)
        del geometry.meshes[0].attributes['TANGENT']
        with patch('model3d_uv.deepcopy',side_effect=AssertionError('allocated before preflight')):
            with self.assertRaises(RigError):generate_uv(geometry,resolution=(2,2),padding=1)
            with self.assertRaises(RigError):generate_uv(geometry,limits={'vertices':16})

    def test_thin_uv_islands_cannot_silently_collapse_at_native_float32_export(self):
        mesh=editable_mesh([[0.,0,0],[1,0,0],[0,1e-11,0]],[[0,1,2]]).geometry()
        with self.assertRaises(RigError) as caught:generate_uv(mesh)
        self.assertEqual(caught.exception.report['code'],'MODEL3D_UV_PRECISION')
        geometry=primitive('box').geometry();mapped,report=generate_uv(geometry)
        uv=mapped.meshes[0].attributes['TEXCOORD_0'].astype(np.float32).astype(float)
        for triangle in mapped.meshes[0].triangles:
            a,b,c=uv[triangle];self.assertGreater((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]),0)
        self.assertLessEqual(report['portable_uv_precision']['maximum_relative_edge_error'],.001)

    def test_actual_textured_glb_retains_png_material_and_uv(self):
        geometry,_=generate_uv(primitive('box').geometry())
        geometry=assign_material(geometry,['box'],material('ceramic',color=(1.,1.,1.,1.)))
        image=procedural_texture('checker',width=16,height=16)
        geometry=bind_texture(geometry,geometry.meshes[0].material,image)
        original_positions=geometry.meshes[0].positions.copy()
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary)/'export';export_mesh(geometry,destination)
            imported=load_character_model(destination/'model.glb').geometry
            self.assertEqual(imported.images[0].data,image.data)
            self.assertIn('baseColorTexture',imported.materials[imported.meshes[0].material]['pbrMetallicRoughness'])
            np.testing.assert_allclose(imported.meshes[0].attributes['TEXCOORD_0'],geometry.meshes[0].attributes['TEXCOORD_0'],atol=1e-7)
        np.testing.assert_array_equal(geometry.meshes[0].positions,original_positions)
        orm=bind_texture(geometry,geometry.meshes[0].material,procedural_texture('orm'),role='orm')
        value=orm.materials[orm.meshes[0].material]
        self.assertEqual(value['occlusionTexture']['index'],value['pbrMetallicRoughness']['metallicRoughnessTexture']['index'])

    def test_repeated_compressed_images_charge_native_aggregate_decoding_budget(self):
        geometry,_=generate_uv(primitive('box').geometry())
        image=procedural_texture('checker',width=1024,height=1024)
        geometry=bind_texture(geometry,0,image,limits={'memory_bytes':64*1024**2})
        geometry=bind_texture(geometry,0,image,role='occlusion',limits={'memory_bytes':64*1024**2})
        before=len(geometry.images)
        with self.assertRaises(RigError) as caught:
            bind_texture(geometry,0,image,role='emissive',limits={'memory_bytes':64*1024**2})
        self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_TEXTURE')
        self.assertEqual(len(geometry.images),before)

    def test_texture_receipt_hashes_exact_selected_decoded_bytes(self):
        import rig_import
        image=procedural_texture('checker',width=16,height=16)
        selected=image.data;real=rig_import._decode_texture
        def mutate_after_decode(data,*args,**kwargs):
            result=real(data,*args,**kwargs)
            image.data=b'not-image-bytes';image.mime_type='image/jpeg'
            return result
        with patch('rig_import._decode_texture',side_effect=mutate_after_decode):
            receipt=validate_texture(image)
        self.assertEqual(receipt['mime_type'],'image/png')
        self.assertEqual(receipt['sha256'],hashlib.sha256(selected).hexdigest())
        self.assertEqual(image.data,b'not-image-bytes')

    def test_texture_binding_decodes_selected_private_resources_before_publication(self):
        import rig_import
        geometry,_=generate_uv(primitive('box').geometry())
        initial=procedural_texture('checker',width=16,height=16)
        geometry=bind_texture(geometry,0,initial)
        incoming=procedural_texture('stone',width=16,height=16,seed=4)
        retained=geometry.images[0].data;added=incoming.data;real=rig_import._decode_texture
        changed=[]
        def mutate_callers_after_decode(data,*args,**kwargs):
            result=real(data,*args,**kwargs)
            if not changed:
                geometry.images[0].data=b'bad-retained';incoming.data=b'bad-incoming';changed.append(True)
            return result
        with patch('rig_import._decode_texture',side_effect=mutate_callers_after_decode):
            result=bind_texture(geometry,0,incoming,role='occlusion')
        self.assertEqual(result.images[0].data,retained);self.assertEqual(result.images[1].data,added)
        validate_texture(result.images[0]);validate_texture(result.images[1])
        self.assertEqual(geometry.images[0].data,b'bad-retained');self.assertEqual(incoming.data,b'bad-incoming')

    def occlusion_scene(self):
        def panel(z,name):
            return editable_mesh([[-.5,-.5,z],[.5,-.5,z],[.5,.5,z],[-.5,.5,z]],[[0,1,2,3]],name=name).mesh
        return Geometry([panel(0.,'front'),panel(-1.,'back')],materials=[{}])

    def test_projection_uses_actual_occlusion_and_retains_inferred_back(self):
        geometry=self.occlusion_scene();image=source((255,0,0,255))
        result,report=project_image(geometry,image,camera_world=world(),projection=projection(),
                    calibration=dict(status='supplied',reference='synthetic known camera'),fallback_srgb=(0.,1.,0.,1.))
        np.testing.assert_allclose(result.meshes[0].attributes['COLOR_0'],np.tile([1,0,0,1],(4,1)))
        np.testing.assert_allclose(result.meshes[1].attributes['COLOR_0'],np.tile([0,1,0,1],(4,1)))
        self.assertEqual(report['observed_vertices'],4);self.assertEqual(report['inferred_vertices'],4)
        self.assertEqual(report['views'][0]['occluded_vertices'],4)
        self.assertEqual(report['views'][0]['source_sha256'],hashlib.sha256(image.data).hexdigest())
        self.assertNotEqual(report['source_geometry_sha256'],report['result_geometry_sha256'])
        self.assertNotIn('COLOR_0',geometry.meshes[0].attributes)
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary)/'projected';export_mesh(result,destination)
            imported=load_character_model(destination/'model.glb').geometry
            np.testing.assert_allclose(imported.meshes[0].attributes['COLOR_0'],result.meshes[0].attributes['COLOR_0'],atol=1e-7)

    def test_multiview_linear_color_mean_and_transparent_regions(self):
        geometry=Geometry([self.occlusion_scene().meshes[0]],materials=[{}])
        common=dict(camera_world=world(),projection=projection(),calibration=dict(status='measured',reference='known'))
        result,report=project_views(geometry,[dict(image=source((255,0,0,255)),**common),dict(image=source((0,0,255,255)),**common)])
        np.testing.assert_allclose(result.meshes[0].attributes['COLOR_0'],np.tile([.5,0,.5,1],(4,1)))
        self.assertEqual(report['parts'][0]['observation_counts'],[2,2,2,2])
        result,report=project_image(geometry,source((255,0,0,0)),**common)
        self.assertEqual(report['observed_vertices'],0);self.assertEqual(report['inferred_vertices'],4)
        self.assertEqual(report['views'][0]['transparent_source_vertices'],4)

    def test_projection_missing_calibration_and_work_exhaustion_fail(self):
        geometry=self.occlusion_scene();image=source((255,0,0,255))
        common=dict(camera_world=world(),projection=projection())
        with self.assertRaises(RigError):project_image(geometry,image,calibration=dict(status='unknown',reference='none'),**common)
        with patch('model3d_projection.deepcopy',side_effect=AssertionError('allocated before work preflight')):
            with self.assertRaises(RigError):project_image(geometry,image,calibration=dict(status='supplied',reference='fixture'),max_tests=1,**common)
        with self.assertRaises(RigError):project_image(geometry,TextureImage('bad','image/png',b'bad'),calibration=dict(status='supplied',reference='fixture'),**common)

    def test_thin_occluder_gaps_are_not_erased_by_a_world_distance_cutoff(self):
        geometry=self.occlusion_scene();geometry.meshes[1].positions=geometry.meshes[1].positions.copy()
        geometry.meshes[1].positions[:,2]=-1e-9
        common=dict(camera_world=world(),projection=projection(),calibration=dict(status='supplied',reference='thin panels'))
        _,report=project_image(geometry,source((255,0,0,255)),**common)
        self.assertEqual(report['observed_vertices'],4);self.assertEqual(report['views'][0]['occluded_vertices'],4)
        geometry.meshes[1].positions[:,2]=-1e-16
        _,report=project_image(geometry,source((255,0,0,255)),**common)
        self.assertEqual(report['parts'][1]['observed'],[])
        self.assertGreater(report['views'][0]['unresolved_depth_vertices'],0)

    def test_uv_seam_duplicates_do_not_become_self_occluding_vertices(self):
        geometry,_=generate_uv(self.occlusion_scene(),method='box')
        result,report=project_image(geometry,source((255,0,0,255)),camera_world=world(),projection=projection(),
                    calibration=dict(status='supplied',reference='UV seam integration'))
        self.assertEqual(len(result.meshes[0].positions),6)
        self.assertEqual(report['observed_vertices'],6)
        self.assertEqual(report['inferred_vertices'],6)
        self.assertEqual(report['views'][0]['unresolved_depth_vertices'],0)

    def test_mutable_inputs_cannot_change_selected_geometry_or_camera_mid_projection(self):
        geometry=Geometry([self.occlusion_scene().meshes[0]],materials=[{}])
        selected=geometry.meshes[0].positions.copy();matrix=world();image=source((255,0,0,255))
        original_hash=hashlib.sha256(image.data).hexdigest()
        import model3d_projection as owner
        real=owner.camera.project;changed=[]
        def mutate_then_project(*args,**kwargs):
            if not changed:
                geometry.meshes[0].positions=geometry.meshes[0].positions+100
                matrix[0,3]=100;image.data=b'changed';changed.append(True)
            return real(*args,**kwargs)
        with patch('model3d_projection.camera.project',side_effect=mutate_then_project):
            result,report=project_image(geometry,image,camera_world=matrix,projection=projection(),
                    calibration=dict(status='supplied',reference='selected before edit'))
        np.testing.assert_array_equal(result.meshes[0].positions,selected)
        self.assertEqual(report['observed_vertices'],4)
        self.assertEqual(report['views'][0]['camera_world'][0][3],0)
        self.assertEqual(report['views'][0]['source_sha256'],original_hash)


if __name__=='__main__':unittest.main()
