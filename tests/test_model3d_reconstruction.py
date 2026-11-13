"""Independent numerical/format/failure checks for bounded local reconstruction."""
from pathlib import Path
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_reconstruction import read_image,mask_from_image,publish
from model3d_reconstruction_solids import extrude_contour,extrude_mask,revolve_profile,heightfield
from rig_errors import RigError
from rig_import import load_character_model
from model3d_topology import topology_report


def volume(mesh):
    p=mesh.positions-mesh.positions.mean(axis=0);t=mesh.triangles
    return np.einsum('ij,ij->',p[t[:,0]],np.cross(p[t[:,1]],p[t[:,2]]))/6

class ReconstructionTests(unittest.TestCase):
    def assert_closed(self,result):
        quality=result.report['topology']
        self.assertFalse(quality['boundary_edges']);self.assertFalse(quality['nonmanifold_edges'])
        self.assertFalse(quality['inconsistent_edges']);self.assertFalse(quality['degenerate_faces'])
        self.assertGreater(volume(result.geometry.meshes[0]),0)

    def test_exact_concave_contour_and_multiple_holes_area_volume(self):
        outer=[[0,0],[4,0],[4,4],[2,4],[2,3],[0,3]]
        holes=[[[.2,.3],[1.2,.3],[1.2,1.3],[.2,1.3]],[[2.5,2],[3.5,2],[3.5,3],[2.5,3]]]
        for reverse in (False,True):
            result=extrude_contour(outer[::-1] if reverse else outer,holes=holes,thickness=.5)
            self.assert_closed(result);self.assertAlmostEqual(volume(result.geometry.meshes[0]),6.)
            self.assertEqual(result.report['completeness'],'partial')
            self.assertEqual(len(result.report['parameters']['holes']),2)

    def test_slanted_concave_nonuniform_slabs_no_tjunctions(self):
        result=extrude_contour([[0,0],[5,0],[4,4],[3,2],[0,3]],holes=[[[1,1],[1.5,.8],[2,1.2],[1.5,1.5]]],thickness=.1)
        self.assert_closed(result)
        mesh=result.geometry.meshes[0]
        self.assertTrue(np.isfinite(mesh.attributes['NORMAL']).all())

    def test_crossing_touching_nested_open_ambiguous_contours_rejected(self):
        outer=[[0,0],[3,0],[3,3],[0,3]]
        cases=[dict(outer=[[0,0],[2,2],[0,2],[2,0]]),dict(outer=[[0,0],[1,0],[0,0]]),
               dict(outer=outer,holes=[[[0,0],[1,0],[1,1],[0,1]]]),
               dict(outer=outer,holes=[[[4,4],[5,4],[5,5],[4,5]]]),
               dict(outer=outer,holes=[[[.2,.2],[2,.2],[2,2],[.2,2]],[[.5,.5],[1,.5],[1,1],[.5,1]]])]
        for kw in cases:
            with self.subTest(kw=kw),self.assertRaises(RigError):extrude_contour(thickness=.2,**kw)
        for thickness in (True,0,-1,float('nan'),10**5000):
            with self.assertRaises(RigError):extrude_contour(outer,thickness=thickness)

    def test_mask_holes_disconnected_components_and_no_cleanup(self):
        mask=np.ones((5,8),dtype=bool);mask[1:4,1:4]=False;mask[:,5:7]=False
        result=extrude_mask(mask,pixel_size=.2,thickness=.4)
        self.assert_closed(result)
        self.assertAlmostEqual(volume(result.geometry.meshes[0]),mask.sum()*.2**2*.4)
        self.assertEqual(len(result.report['topology']['face_components']),2)
        self.assertEqual(result.report['parameters']['mask_sha256'],hashlib.sha256(mask.tobytes()).hexdigest())
        with self.assertRaises(RigError):extrude_mask(np.eye(3,dtype=bool),pixel_size=.1,thickness=.2)
        with self.assertRaises(RigError):extrude_mask(np.ones((2,2),dtype=int),pixel_size=.1,thickness=.2)

    def test_revolution_independent_prism_volume_uv_seam_and_axis_tips(self):
        n=32;result=revolve_profile([[1,0],[1,2]],segments=n)
        self.assert_closed(result)
        self.assertAlmostEqual(volume(result.geometry.meshes[0]),n*math.sin(2*math.pi/n))
        uv=result.geometry.meshes[0].attributes['TEXCOORD_0']
        self.assertEqual(uv.shape[1],2);self.assertEqual(uv[:,0].max(),1)
        for profile in ([[0,0],[1,1],[0,2]],[[1,0],[.5,1],[2,2]]):self.assert_closed(revolve_profile(profile))
        open_result=revolve_profile([[1,0],[1,1]],cap_ends=False)
        self.assertFalse(open_result.report['closed']);self.assertTrue(open_result.report['topology']['boundary_edges'])

    def test_revolution_invalid_profiles_and_preallocation_bound(self):
        for profile in ([[1,0],[1,0]],[[1,0],[-1,1]],[[1,0],[0,1],[1,2]],[[0,0],[0,1]]):
            with self.assertRaises(RigError):revolve_profile(profile)
        with self.assertRaises(RigError):revolve_profile([[1,i] for i in range(512)],segments=512)
        with self.assertRaises(RigError):revolve_profile([[1,0],[1,1]],segments=True)

    def test_heightfield_finite_base_metric_volume_and_immutable_input(self):
        heights=np.array([[0.,1.,2.],[1.,2.,3.]])
        original=heights.copy();result=heightfield(heights,spacing=[.5,2],base_thickness=.25)
        self.assert_closed(result);np.testing.assert_array_equal(heights,original)
        self.assertAlmostEqual(volume(result.geometry.meshes[0]),2*1.75)
        self.assertEqual(result.geometry.meshes[0].positions[:,1].min(),-.25)
        self.assertEqual(result.report['parameters']['grid_shape'],[2,3])
        for bad in ([[0]],[[0,float('nan')],[0,0]],[[False,True],[True,False]]):
            with self.assertRaises(RigError):heightfield(bad,spacing=[1,1],base_thickness=.1)
        with self.assertRaises(RigError):heightfield([[-1,0],[0,0]],spacing=[1,1],base_thickness=.2)

    def test_image_formats_original_bytes_and_explicit_masks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for fmt,suffix in [('PNG','.png'),('JPEG','.jpg'),('WEBP','.webp')]:
                path=root/('source'+suffix);Image.new('RGB',(12,9),(255,128,20)).save(path,format=fmt)
                before=path.read_bytes();evidence=read_image(path)
                self.assertEqual(evidence.payload,before);self.assertEqual(evidence.descriptor()['dimensions_px'],[12,9])
                self.assertEqual(evidence.sha256,hashlib.sha256(before).hexdigest())
                with self.assertRaises(RigError):mask_from_image(evidence,mode='alpha')
                self.assertEqual(mask_from_image(evidence,mode='luminance',threshold=40).shape,(9,12))
                self.assertEqual(path.read_bytes(),before)
            path=root/'alpha.png';Image.new('RGBA',(4,3),(1,2,3,128)).save(path)
            evidence=read_image(path);self.assertTrue(mask_from_image(evidence,mode='alpha',threshold=127).all())
            self.assertFalse(mask_from_image(evidence,mode='alpha',threshold=128).any())
            with patch('model3d_reconstruction.MAX_IMAGE_PIXELS',2):
                with self.assertRaises(RigError):read_image(path)

    def test_actual_all_methods_export_reimport_and_exact_source_publication(self):
        results=[extrude_contour([[0,0],[2,0],[2,2],[0,2]],holes=[[[.5,.5],[1.5,.5],[1.5,1.5],[.5,1.5]]],thickness=.3),
                 revolve_profile([[0,0],[1,.5],[.8,1],[0,1.5]]),heightfield([[0,.1],[.2,.3]],spacing=[1,1],base_thickness=.5)]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'art.png';Image.new('RGBA',(4,3),(100,20,90,128)).save(source)
            evidence=read_image(source)
            for i,result in enumerate(results):
                for fmt in ('glb','gltf','obj'):
                    destination=root/(str(i)+fmt);receipt=publish(result,destination,evidence=[evidence],format=fmt)
                    model=load_character_model(destination/'mesh'/('model.'+fmt)).geometry
                    self.assertEqual(model.triangle_count,result.geometry.triangle_count)
                    self.assertAlmostEqual(volume(model.meshes[0]),volume(result.geometry.meshes[0]),places=5)
                    for name,record in receipt['files'].items():
                        self.assertEqual(hashlib.sha256((destination/name).read_bytes()).hexdigest(),record['sha256'])
                    self.assertEqual((destination/'sources/00-art.png').read_bytes(),evidence.payload)
                    self.assertEqual(json.loads((destination/'reconstruction.json').read_text())['review'],'pending')
                    with self.assertRaises(RigError):publish(result,destination)

    def test_ragged_arrays_and_cyclic_evidence_reject_structurally(self):
        with self.assertRaises(RigError):extrude_contour([[0,0],[1],[0,1]],thickness=.1)
        with self.assertRaises(RigError):heightfield([[0,1],[0]],spacing=[1,1],base_thickness=.1)
        with self.assertRaises(RigError):extrude_mask([[True],[False,True]],pixel_size=.1,thickness=.2)
        cycle=[];cycle.append(cycle)
        with self.assertRaises(RigError):extrude_contour([[0,0],[1,0],[0,1]],thickness=.1,evidence=cycle)
        with self.assertRaises(RigError):extrude_contour([[0,0],[1,0],[-1,0],[0,1]],thickness=.1)

    def test_edited_geometry_or_report_cannot_publish_stale_method_evidence(self):
        for mutation in ('geometry','report'):
            result=extrude_contour([[0,0],[1,0],[1,1],[0,1]],thickness=.2)
            if mutation=='geometry':result.geometry.meshes[0].positions[:,2]*=10
            else:result.report['parameters']['thickness_m']=.5
            with tempfile.TemporaryDirectory() as temporary:
                destination=Path(temporary)/'out'
                with self.assertRaises(RigError) as caught:publish(result,destination)
                self.assertEqual(caught.exception.report['code'],'RECON_STALE')
                self.assertFalse(destination.exists())

    def test_failed_or_racing_publication_preserves_dest_and_removes_only_owned_stage(self):
        result=extrude_contour([[0,0],[1,0],[1,1],[0,1]],thickness=.1)
        from atomic_publish import _rename_no_replace
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);dest=root/'out'
            def racing(stage,target):
                target.mkdir();(target/'owned').write_bytes(b'keep');_rename_no_replace(stage,target)
            with patch('model3d_reconstruction._rename_no_replace',racing):
                with self.assertRaises(OSError):publish(result,dest)
            self.assertEqual((dest/'owned').read_bytes(),b'keep')
            self.assertEqual(list(root.iterdir()),[dest])

import math
if __name__=='__main__':unittest.main()
