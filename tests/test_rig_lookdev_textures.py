"""Actual raster/UV execution; optional dependencies only inside native tests."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import numpy as np
from PIL import Image
from rig_errors import RigError
import lookdev_textures as t


def png(array):
    output=io.BytesIO();Image.fromarray(array).save(output,format='PNG');return output.getvalue()


class TextureTests(unittest.TestCase):
    def test_srgb_is_decoded_once_and_alpha_is_linear(self):
        data=png(np.array([[[128,128,128,64]]],dtype=np.uint8))
        image=t.decode_texture(data,role='base-color',encoding='srgb')
        self.assertAlmostEqual(float(image.pixels[0,0,0]),0.2158605,places=6)
        self.assertAlmostEqual(float(image.pixels[0,0,3]),64/255,places=6)
        again=t.decode_texture(t.encode_png(image.pixels,encoding='srgb'),role='base-color',encoding='srgb')
        np.testing.assert_allclose(again.pixels,image.pixels)
        self.assertEqual(image.source_sha256,hashlib.sha256(data).hexdigest())

    def test_data_stays_linear(self):
        image=t.decode_texture(png(np.full((2,3,3),128,np.uint8)),role='roughness',encoding='data')
        self.assertAlmostEqual(float(image.pixels[0,0,0]),128/255,places=6)
        with self.assertRaises(RigError):t.decode_texture(png(np.zeros((1,1,3),np.uint8)),role='roughness',encoding='srgb')

    def test_corrupt_png_and_limit_fail(self):
        data=png(np.zeros((4,4,3),np.uint8))
        for damaged in (data[:-12],data[:45]+bytes([data[45]^0x40])+data[46:]):
            with self.assertRaises(RigError):t.decode_texture(damaged,role='base-color',encoding='srgb')
        with self.assertRaises(RigError):t.decode_texture(data,role='base-color',encoding='srgb',limits=t.Limits(pixels=15))
        with self.assertRaises(RigError):t.decode_texture(data,role='base-color',encoding='srgb',limits=t.Limits(encoded_bytes=10))

    def test_jpeg_webp_actual_decode(self):
        for fmt in ('JPEG','WEBP'):
            output=io.BytesIO();Image.new('RGB',(8,5),(42,71,92)).save(output,format=fmt)
            image=t.decode_texture(output.getvalue(),role='base-color',encoding='srgb')
            self.assertEqual((image.width,image.height,image.format),(8,5,fmt))
            with self.assertRaises(RigError):t.decode_texture(output.getvalue()[:-4],role='base-color',encoding='srgb')

    def test_hdr_rle_has_unclipped_values_and_correct_orientation(self):
        # Two constant RGBE scanlines: red=2, green=1, blue=0.5.
        header=b'#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 2 +X 8\n'
        row=b'\x02\x02\x00\x08'+bytes([136,128,136,64,136,32,136,130])
        image=t.decode_texture(header+row+row,role='environment',encoding='linear-srgb')
        np.testing.assert_allclose(image.pixels[0,0],[2,1,.5,1])
        with self.assertRaises(RigError):t.encode_png(image.pixels)
        for bad in (header+row[:-1],header+row+row+b'x',header+row[:4]+bytes([137,128])):
            with self.assertRaises(RigError):t.decode_texture(bad,role='environment',encoding='linear-srgb')

    def test_hdr_header_allocation_guard(self):
        data=b'#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 100000 +X 100000\n'
        with self.assertRaises(RigError):t.decode_texture(data,role='environment',encoding='linear-srgb')

    def test_all_procedural_materials_reproducible_actual_maps(self):
        signatures=[]
        for kind in t.KINDS:
            a=t.procedural_maps(kind,width=16,height=16,seed=7)
            b=t.procedural_maps(kind,width=16,height=16,seed=7)
            self.assertEqual(a,b)
            self.assertEqual(set(a['maps']),{'base-color','height','normal','roughness','metallic'})
            for role,data in a['maps'].items():
                image=t.decode_texture(data,role=role,encoding='srgb' if role=='base-color' else 'data')
                self.assertEqual((image.width,image.height),(16,16))
            signatures.append(hashlib.sha256(a['maps']['height']).hexdigest())
        self.assertGreater(len(set(signatures)),9)

    def test_seed_changes_actual_texture(self):
        a=t.procedural_maps('wood',width=16,height=16,seed=1)
        b=t.procedural_maps('wood',width=16,height=16,seed=2)
        self.assertNotEqual(a['maps']['base-color'],b['maps']['base-color'])
        for kwargs in ({'seed':True},{'frequency':float('nan')},{'width':0},{'color_low':[-1,0,0]}):
            with self.assertRaises((RigError,ValueError)):t.procedural_maps('wood',**kwargs)

    def test_flat_and_sloped_height_tangent_normals(self):
        flat=t.normal_from_height(np.zeros((3,4),np.float32))
        np.testing.assert_allclose(flat[0,0],[.5,.5,1,1])
        slope=np.tile(np.arange(4,dtype=np.float32),(3,1))/4
        normal=t.normal_from_height(slope,wrap='clamp')
        self.assertLess(normal[1,1,0],.5)
        np.testing.assert_allclose(np.linalg.norm(normal[:,:,:3]*2-1,axis=-1),1,atol=1e-6)

    def test_mipmaps_odd_dimension_average_and_normals(self):
        values=np.arange(15,dtype=np.float32).reshape(3,5)/14
        pixels=np.concatenate((np.repeat(values[:,:,None],3,axis=2),np.ones((3,5,1),np.float32)),axis=2)
        image=t.DecodedTexture(pixels,'PNG','0'*64,'roughness','data')
        chain=t.mipmaps(image)
        self.assertEqual([x.shape for x in chain],[(1,2,4),(1,1,4)])
        self.assertAlmostEqual(float(chain[-1][0,0,0]),float(values.mean()),places=6)
        pixels[:,:,:3]=t.normal_from_height(values)[:,:,:3]
        image.role='normal'
        for level in t.mipmaps(image):np.testing.assert_allclose(np.linalg.norm(level[:,:,:3]*2-1,axis=-1),1,atol=1e-5)

    def test_orm_packing_and_dimension_validation(self):
        maps=[t.decode_texture(png(np.full((2,2,3),v,np.uint8)),role=role,encoding='data') for v,role in zip([32,128,255],['occlusion','roughness','metallic'])]
        result=t.decode_texture(t.pack_channels(maps),role='roughness',encoding='data')
        np.testing.assert_allclose(result.pixels[0,0],[32/255,128/255,1,1],atol=1e-6)
        maps[-1]=t.decode_texture(png(np.zeros((1,1,3),np.uint8)),role='metallic',encoding='data')
        with self.assertRaises(RigError):t.pack_channels(maps)

    def test_uv_transform_order_and_wrapping(self):
        result=t.transform_uv([[1,0]],scale=(2,1),rotation=np.pi/2,offset=(.2,.3),wrap='none')
        np.testing.assert_allclose(result,[[.2,2.3]],atol=1e-10)
        np.testing.assert_allclose(t.transform_uv([[-.2,1.2]],wrap='mirror'),[[.2,.8]])
        with self.assertRaises(RigError):t.transform_uv([[float('nan'),0]])

    def test_uv_overlap_and_mirrored_reports(self):
        uv=np.array([[0,0],[1,0],[0,1],[0,0],[0,1],[1,0]],float)
        report=t.validate_uvs([uv],[[0,1,2],[3,4,5]],tangents=np.tile([1,0,0,1],(6,1)))
        self.assertEqual(report['sets'][0]['mirrored_triangles'],[1])
        self.assertEqual(report['sets'][0]['overlap_pairs'],[[0,1]])
        self.assertTrue(t.validate_uvs([],[[0,1,2]])['missing'])
        with self.assertRaises(RigError):t.validate_uvs([uv],[[0,1,9]])
        with self.assertRaises(RigError):t.validate_uvs([uv],[[0,1,2]],tangents=np.zeros((6,4)))

    def test_uv_adjacent_edges_are_not_overlap_and_work_is_bounded(self):
        uv=[[0,0],[1,0],[0,1],[1,1]]
        self.assertEqual(t.validate_uvs([uv],[[0,1,2],[1,3,2]])['sets'][0]['overlap_pairs'],[])
        report=t.validate_uvs([uv],[[0,1,2]]*20,max_pairs=3)['sets'][0]
        self.assertFalse(report['overlap_complete']);self.assertEqual(report['overlap_pairs_tested'],3)

    def test_projection_and_udim_actual_pixels(self):
        np.testing.assert_allclose(t.project_uvs([[1,2,3]],mode='planar'),[[1,2]])
        with self.assertRaises(RigError):t.project_uvs([[0,0,0]],mode='spherical')
        red=t.decode_texture(png(np.array([[[255,0,0]]],np.uint8)),role='base-color',encoding='srgb')
        blue=t.decode_texture(png(np.array([[[0,0,255]]],np.uint8)),role='base-color',encoding='srgb')
        atlas=t.atlas_udim({1001:red,1011:blue})
        actual=t.decode_texture(atlas['png'],role='base-color',encoding='srgb')
        np.testing.assert_allclose(actual.pixels[0,0],[0,0,1,1]);np.testing.assert_allclose(actual.pixels[1,0],[1,0,0,1])
        self.assertEqual(atlas['uv_scale'],[1,.5])

    def test_source_publication_is_exact_and_never_overwrites(self):
        data=png(np.zeros((2,2,3),np.uint8))
        with tempfile.TemporaryDirectory() as root:
            destination=Path(root)/'new'
            receipt=t.publish_texture(data,destination,role='base-color',encoding='srgb',attribution='self-authored fixture',license_id='CC0')
            self.assertEqual((destination/'source.bin').read_bytes(),data)
            self.assertEqual(receipt['source']['sha256'],hashlib.sha256(data).hexdigest())
            with self.assertRaises(RigError):t.publish_texture(data,destination,role='base-color',encoding='srgb',attribution='fixture',license_id='CC0')
            link=Path(root)/'link'
            try:link.symlink_to(destination,target_is_directory=True)
            except OSError:return
            with self.assertRaises(RigError):t.publish_texture(data,link/'escape',role='base-color',encoding='srgb',attribution='fixture',license_id='CC0')

if __name__=='__main__':unittest.main()
