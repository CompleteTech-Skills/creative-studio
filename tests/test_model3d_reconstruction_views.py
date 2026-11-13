"""Numerical calibrated hull fixtures; genuine surface export and reprojection."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_reconstruction_views import calibrated_view,carve_visual_hull,rasterize_silhouette,_surface,_budgets
from camera3d_math import look_at,world_transform,project
from model3d_interchange import export_mesh
from rig_import import load_character_model
from rig_errors import RigError
from rig_models import Geometry,Mesh


def world(position):
    return world_transform(dict(translation=position,rotation=look_at(position,[0,0,0]).tolist(),scale=[1,1,1]))


def view(name,position,mask=None,**kw):
    if mask is None:
        mask=np.zeros((16,16),dtype=bool);mask[4:12,4:12]=True
    return calibrated_view(mask,name=name,calibration='known',evidence=dict(source_sha256='0'*64,fixture=True),
                           camera_to_world=world(position),orthographic=[-1,1,-1,1],near=.1,far=10,**kw)


class VisualHullTests(unittest.TestCase):
    def test_two_views_reconstruct_cube_real_surface_volume_and_iou(self):
        views=[view('front',[0,0,3]),view('right',[3,0,0])]
        original=[v.mask.copy() for v in views]
        result=carve_visual_hull(views,[[-1,-1,-1],[1,1,1]],[4,4,4])
        self.assertEqual(int(result.occupancy.sum()),8)
        mesh=result.geometry.meshes[0]
        np.testing.assert_allclose(mesh.positions.min(axis=0),[-.5,-.5,-.5])
        np.testing.assert_allclose(mesh.positions.max(axis=0),[.5,.5,.5])
        p=mesh.positions[mesh.triangles]
        self.assertAlmostEqual(np.einsum('ij,ij->',p[:,0],np.cross(p[:,1],p[:,2]))/6,1.)
        self.assertTrue(result.report['watertight_manifold'])
        self.assertEqual([r['iou'] for r in result.report['reprojection']],[1.,1.])
        self.assertEqual([r['centroid_residual_px'] for r in result.report['reprojection']],[0.,0.])
        for v,initial in zip(views,original):np.testing.assert_array_equal(v.mask,initial)
        self.assertFalse(result.occupancy.flags.writeable)
        self.assertEqual(result.report['classification'],'derived-from-multiple-observations')
        self.assertEqual(result.report['review_status'],'pending')
        self.assertEqual(result.report['view_geometry']['axis_rank'],2)
        self.assertEqual(result.report['completeness'],'partial')
        self.assertEqual(result.report['provider_calls'],0)

    def test_true_surface_reprojection_detects_unsupported_foreground(self):
        mask=np.zeros((16,16),bool);mask[4:12,4:12]=True;mask[0,0]=True
        result=carve_visual_hull([view('front',[0,0,3],mask),view('right',[3,0,0])],[[-1]*3,[1]*3],[4]*3)
        observed=result.report['reprojection'][0]
        self.assertEqual(observed['missing_foreground_pixels'],1)
        self.assertAlmostEqual(observed['iou'],64/65)
        self.assertTrue(observed['contradicts_observation'])

    def test_explicit_perspective_intrinsics_projection_matches_camera_authority(self):
        mask=np.ones((20,30),bool)
        k=[[40,2,14],[0,35,8],[0,0,1]]
        camera=calibrated_view(mask,name='photo',calibration='estimated',evidence={'source':'photo'},
                              world_to_camera=np.eye(4),intrinsics=k,near=.1,far=10)
        for point in [[.1,.2,-2],[-.1,-.2,-1]]:
            pixel=project(point,camera.world,camera.projection,30,20)['pixel']
            expected=[40*point[0]/-point[2]-2*point[1]/-point[2]+14,-35*point[1]/-point[2]+8]
            np.testing.assert_allclose(pixel,expected)
        self.assertEqual(camera.camera['projection'],'perspective')
        self.assertEqual(camera.calibration,'estimated')

    def test_perspective_hull_and_actual_glb_gltf_obj_roundtrip(self):
        mask=np.zeros((16,16),bool);mask[3:13,3:13]=True
        cameras=[calibrated_view(mask,name=name,calibration='estimated',evidence={'fixture':True},
                                camera_to_world=world(position),intrinsics=[[16,0,8],[0,16,8],[0,0,1]],near=.1,far=10)
                 for name,position in [('front',[0,0,3]),('side',[3,0,0])]]
        result=carve_visual_hull(cameras,[[-.5]*3,[.5]*3],[2]*3)
        self.assertEqual(result.report['calibration_status'],'estimated')
        self.assertGreater(result.report['reprojection'][0]['missing_foreground_pixels'],0)
        with tempfile.TemporaryDirectory() as temp:
            for fmt in ['glb','gltf','obj']:
                dest=Path(temp)/fmt;receipt=export_mesh(result.geometry,dest,format=fmt)
                decoded=load_character_model(dest/('model.'+fmt)).geometry
                self.assertEqual(decoded.triangle_count,result.geometry.triangle_count)
                np.testing.assert_allclose(decoded.meshes[0].positions[decoded.meshes[0].triangles],
                                           result.geometry.meshes[0].positions[result.geometry.meshes[0].triangles])
                for name,record in receipt['files'].items():
                    self.assertEqual(hashlib.sha256((dest/name).read_bytes()).hexdigest(),record['sha256'])
                np.testing.assert_array_equal(rasterize_silhouette(decoded,cameras[0]),
                                              rasterize_silhouette(result.geometry,cameras[0]))

    def test_no_consistent_volume_is_a_structured_contradiction(self):
        left=np.zeros((16,16),bool);left[:,0:3]=True
        right=np.zeros((16,16),bool);right[:,13:16]=True
        with self.assertRaises(RigError) as error:
            carve_visual_hull([view('left',[0,0,3],left),view('right',[0,0,3],right)],[[-1]*3,[1]*3],[4]*3)
        self.assertEqual(error.exception.report['code'],'MODEL3D_HULL_CONTRADICTION')
        self.assertEqual(error.exception.report['observed'][-1]['remaining_voxels'],0)

    def test_unknown_or_malformed_calibration_rejects(self):
        mask=np.ones((8,8),bool)
        base=dict(name='front',calibration='known',evidence={'fixture':True},camera_to_world=np.eye(4),orthographic=[-1,1,-1,1])
        cases=[dict(calibration='unknown'),dict(calibration=True),dict(camera_to_world=np.diag([2,1,1,1])),
               dict(world_to_camera=np.eye(4)),dict(orthographic=[1,1,-1,1]),dict(evidence={}),
               dict(near=True),dict(far=float('nan')),dict(name='unsafe/name')]
        for mutation in cases:
            with self.subTest(mutation=mutation),self.assertRaises(RigError):calibrated_view(mask,**dict(base,**mutation))
        for wrong in [mask.astype(int),np.zeros((8,8),bool),np.ones((1,1,1),bool)]:
            with self.assertRaises(RigError):calibrated_view(wrong,**base)
        for k in [[[1,0,0],[0,-1,0],[0,0,1]],[[1,0,0],[0,1,0],[0,1,1]]]:
            fields=dict(base);fields.pop('orthographic');fields['intrinsics']=k
            with self.assertRaises(RigError):calibrated_view(mask,**fields)

    def test_voxel_and_surface_budgets_reject_before_large_work(self):
        cameras=[view('front',[0,0,3]),view('side',[3,0,0])]
        for resolution in [[1000]*3,[64,64,65],[True,2,2],[2.,2,2]]:
            with self.assertRaises(RigError):carve_visual_hull(cameras,[[-1]*3,[1]*3],resolution)
        with self.assertRaises(RigError):carve_visual_hull(cameras,[[-1]*3,[1]*3],[4]*3,limits={'voxels':1})
        with self.assertRaises(RigError):carve_visual_hull(cameras,[[-1]*3,[1]*3],[4]*3,limits={'triangles':1})
        with self.assertRaises(RigError):carve_visual_hull(cameras,[[-1]*3,[1]*3],[4]*3,limits={'raster_samples':1})
        with self.assertRaises(RigError):carve_visual_hull(cameras,[[1]*3,[-1]*3],[4]*3)

    def test_diagonal_contacts_fail_instead_of_claiming_watertightness(self):
        for second in [(1,1,0),(1,1,1)]:
            cells=np.zeros((2,2,2),bool);cells[0,0,0]=True;cells[second]=True
            with self.subTest(second=second),self.assertRaises(RigError) as error:
                _surface(cells,np.array([[0.,0,0],[1.,1,1]]),_budgets(None))
            self.assertEqual(error.exception.report['code'],'MODEL3D_HULL_NONMANIFOLD')
        with self.assertRaises(RigError) as error:
            _surface(np.ones((2,1,1),bool),np.array([[1.,0,0],[np.nextafter(1.,2.),1.,1.]]),_budgets(None))
        self.assertEqual(error.exception.report['code'],'MODEL3D_HULL_NUMERIC')

    def test_mask_receipt_and_calibration_are_independent_copies(self):
        mask=np.ones((8,8),bool);evidence={'source':{'sha256':'0'*64}}
        camera=calibrated_view(mask,name='front',calibration='known',evidence=evidence,
                              camera_to_world=world([0,0,3]),orthographic=[-1,1,-1,1])
        evidence['source']['sha256']='changed';mask[:]=False
        self.assertTrue(camera.mask.all());self.assertEqual(camera.evidence['source']['sha256'],'0'*64)
        self.assertEqual(camera.evidence['mask']['sha256'],hashlib.sha256(np.ones((8,8),bool).tobytes()).hexdigest())
        self.assertFalse(camera.world.flags.writeable)

    def test_extrinsic_direction_and_homogeneous_surface_clipping(self):
        mask=np.ones((16,16),bool)
        camera=calibrated_view(mask,name='clip',calibration='known',evidence={'fixture':True},
                              world_to_camera=np.eye(4),intrinsics=[[8,0,8],[0,8,8],[0,0,1]],near=.1,far=10)
        mesh=Mesh('crossing',np.array([[-1.,-1,-.05],[1,-1,-1],[0,1,-1]]),np.array([[0,1,2]]))
        geometry=Geometry([mesh],[{}])
        self.assertGreater(int(rasterize_silhouette(geometry,camera).sum()),0)
        mesh.positions[:,2]=1
        self.assertEqual(int(rasterize_silhouette(geometry,camera).sum()),0)
        a=view('forward',[0,0,3])
        b=calibrated_view(a.mask,name='inverse',calibration='known',evidence={'fixture':True},
                         world_to_camera=np.linalg.inv(a.world),orthographic=[-1,1,-1,1],near=.1,far=10)
        np.testing.assert_allclose(a.world,b.world)

    def test_evidence_limits_and_modified_view_receipt_reject(self):
        mask=np.ones((8,8),bool)
        for evidence in [{'too-long':'x'*9000},{'invalid':object()},{'nested':float('nan')}]:
            with self.subTest(evidence=str(evidence)[:60]),self.assertRaises(RigError):
                calibrated_view(mask,name='front',calibration='known',evidence=evidence,
                                camera_to_world=np.eye(4),orthographic=[-1,1,-1,1])
        camera=view('front',[0,0,3]);camera.mask.setflags(write=True);camera.mask[0,0]=True
        with self.assertRaises(RigError) as error:
            carve_visual_hull([camera,view('side',[3,0,0])],[[-1]*3,[1]*3],[4]*3)
        self.assertEqual(error.exception.report['code'],'MODEL3D_HULL_CAMERA')

    def test_duplicate_calibrations_keep_explicit_geometric_uncertainty(self):
        result=carve_visual_hull([view('a',[0,0,3]),view('b',[0,0,3])],[[-1]*3,[1]*3],[4]*3)
        self.assertEqual(result.report['view_geometry']['axis_rank'],1)
        self.assertEqual(result.report['view_geometry']['redundant_calibration_pairs'],[['a','b']])
        self.assertTrue(any('no independent' in text for text in result.report['uncertainties']))
        self.assertFalse(result.report['surface_classification']['directly_observed'])


if __name__=='__main__':unittest.main()
