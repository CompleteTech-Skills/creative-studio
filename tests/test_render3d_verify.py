"""Real image decoding and source-bound result verification; fixtures are synthetic."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from render3d_verify import verify_image,verify_result,decode_image
from render3d_contracts import Render3DError
from render3d_snapshot import Scene3DAdapter,create_snapshot
from rig_errors import object_hash
from scene3d_example import build_example

try:
    import png
    import numpy as np
    import OpenEXR
    DEPENDENCIES=True
except ImportError:
    DEPENDENCIES=False


def output(format='png',depth=8,alpha='straight',passes=None):
    return dict(format=format,bit_depth=depth,alpha=alpha,passes=passes or ['combined'],color_space='srgb' if format=='png' else 'linear')


@unittest.skipUnless(DEPENDENCIES,'optional OpenEXR/pypng decoder packages unavailable')
class ImageVerification(unittest.TestCase):
    def png(self,path,depth=8,alpha=True):
        value=(1<<depth)-1
        writer=png.Writer(3,2,greyscale=False,alpha=alpha,bitdepth=depth)
        row=[value,0,value,value//2] * 3 if alpha else [value,0,value]*3
        with path.open('wb') as stream:writer.write(stream,[row,row])

    def exr(self,path,depth=32,channels=None):
        dtype=np.float16 if depth==16 else np.float32
        data=np.array([[0,2,10],[.25,.5,1]],dtype=dtype)
        values=channels or dict(R=data,G=data,B=data,A=np.ones((2,3),dtype=dtype))
        OpenEXR.File({},values).write(str(path))

    def test_png8_and_png16_decode_true_precision(self):
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (8,16):
                path=Path(temporary)/f'{depth}.png';self.png(path,depth)
                report=verify_image(path,output=output(depth=depth),resolution=[3,2])
                self.assertEqual(report['bit_depth'],depth)
                self.assertEqual(report['channels']['R']['maximum'],1)
                self.assertAlmostEqual(report['channels']['A']['minimum'],((1<<depth)-1)//2/((1<<depth)-1))
                self.assertEqual(report['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())

    def test_half_full_exr_decode_hdr_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (16,32):
                path=Path(temporary)/f'{depth}.exr';self.exr(path,depth)
                report=verify_image(path,output=output('exr',depth),resolution=[3,2])
                self.assertEqual(report['channels']['R']['maximum'],10)
                self.assertEqual(report['channels']['R']['type'],'half' if depth==16 else 'float')

    def test_preview_arrays_retain_png16_and_hdr_precision(self):
        with tempfile.TemporaryDirectory() as temporary:
            png_path=Path(temporary)/'test.png';self.png(png_path,16)
            report,pixels=decode_image(png_path,output=output(depth=16),resolution=[3,2])
            self.assertEqual(pixels['A'].shape,(2,3))
            self.assertEqual(float(pixels['A'][0,0]),32767/65535)
            exr_path=Path(temporary)/'test.exr';self.exr(exr_path)
            report,pixels=decode_image(exr_path,output=output('exr',32),resolution=[3,2])
            self.assertEqual(float(pixels['R'][0,2]),10)

    @unittest.skipUnless(hasattr(__import__('os'),'mkfifo'),'FIFO support required')
    def test_fifo_rejects_without_waiting_for_writer(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'pipe';os.mkfifo(path)
            with self.assertRaises(Render3DError):verify_image(path,output=output(),resolution=[3,2])

    def test_malformed_wrong_size_precision_and_alpha_reject(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test.png';self.png(path)
            for kwargs in (dict(output=output(),resolution=[1,2]),dict(output=output(depth=16),resolution=[3,2]),dict(output=output(alpha='opaque'),resolution=[3,2])):
                with self.assertRaises(Render3DError):verify_image(path,**kwargs)
            original=path.read_bytes()
            for payload in (original[:30],original+b'extra',b'not an image',original[:50]+b'bad'+original[53:]):
                path.write_bytes(payload)
                with self.assertRaises(Render3DError):verify_image(path,output=output(),resolution=[3,2])

    def test_image_resource_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test.png';self.png(path)
            for kwargs in (dict(max_bytes=2),dict(memory_bytes=1)):
                with self.assertRaises(Render3DError):verify_image(path,output=output(),resolution=[3,2],**kwargs)

    def test_nonfinite_exr_and_wrong_channels_reject(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test.exr'
            self.exr(path,channels={'Z':np.full((2,3),np.inf,dtype=np.float32)})
            with self.assertRaises(Render3DError):verify_image(path,output=output('exr',32,passes=['combined','depth']),resolution=[3,2],pass_name='depth')
            self.exr(path,channels={'Z':np.ones((2,3),dtype=np.float32)})
            with self.assertRaises(Render3DError):verify_image(path,output=output('exr',32),resolution=[3,2])
            report=verify_image(path,output=output('exr',32,passes=['combined','depth']),resolution=[3,2],pass_name='depth')
            self.assertEqual(set(report['channels']),{'Z'})

    def test_standalone_configuration_rejects_invalid_precision_and_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test.exr';self.exr(path)
            for options in (dict(output=output('exr',8),resolution=[3,2]),
                            dict(output=output('exr',32),resolution=[3.0,2]),
                            dict(output=output('exr',32),resolution=[3,2],max_bytes=True),
                            dict(output=output('exr',32),resolution=[3,2],memory_bytes=0)):
                with self.subTest(options=options),self.assertRaises(Render3DError):verify_image(path,**options)

    def test_rgb_color_key_transparency_and_interrupted_idat_reject(self):
        import zlib
        def chunk(kind,payload):
            return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test.png';self.png(path,alpha=False);original=path.read_bytes()
            path.write_bytes(original[:33]+chunk(b'tRNS',struct.pack('>HHH',255,0,255))+original[33:])
            with self.assertRaises(Render3DError):verify_image(path,output=output(alpha='opaque'),resolution=[3,2])
            length=struct.unpack('>I',original[33:37])[0];payload=original[41:41+length];middle=len(payload)//2
            path.write_bytes(original[:33]+chunk(b'IDAT',payload[:middle])+chunk(b'tEXt',b'note\0interrupt')+
                             chunk(b'IDAT',payload[middle:])+original[45+length:])
            with self.assertRaises(Render3DError):verify_image(path,output=output(alpha='opaque'),resolution=[3,2])

    def test_multipart_cropped_windows_and_trailing_exr_reject(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test.exr';a=np.ones((2,3),np.float32)
            OpenEXR.File([OpenEXR.Part({},dict(R=a,G=a,B=a,A=a)),OpenEXR.Part({},dict(Z=a*np.nan))]).write(str(path))
            with self.assertRaises(Render3DError):verify_image(path,output=output('exr',32),resolution=[3,2])
            header={'dataWindow':(np.array([100,200],np.int32),np.array([102,201],np.int32)),
                    'displayWindow':(np.array([0,0],np.int32),np.array([999,999],np.int32))}
            OpenEXR.File(header,dict(R=a,G=a,B=a,A=a)).write(str(path))
            with self.assertRaises(Render3DError):verify_image(path,output=output('exr',32),resolution=[3,2])
            self.exr(path);path.write_bytes(path.read_bytes()+b'extra')
            with self.assertRaises(Render3DError):verify_image(path,output=output('exr',32),resolution=[3,2])

    def test_supported_exr_compression_block_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'test.exr';a=np.ones((67,3),np.float32)
            for name in ('NO','RLE','ZIPS','ZIP','PIZ','PXR24','B44','B44A','DWAA','DWAB'):
                with self.subTest(compression=name):
                    OpenEXR.File({'compression':getattr(OpenEXR,name+'_COMPRESSION')},
                                 dict(R=a,G=a,B=a,A=a)).write(str(path))
                    report=verify_image(path,output=output('exr',32),resolution=[3,67])
                    self.assertEqual(report['resolution'],[3,67])

    def scene_result(self,root):
        source=root/'source';snapshot_root=root/'snapshot';build_example(source)
        snapshot=create_snapshot(source,snapshot_root,Scene3DAdapter());raw=(snapshot_root/'snapshot.json').read_bytes()
        request=dict(schema_version=1,kind='render3d-request',id='test',project={'id':'project','version':'1.0.0'},
            scene={'id':'example-scene','version':'1.0.0'},shot={'id':'example-shot','version':'1.0.0'},mode='3d',
            snapshot={'path':'snapshot.json','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)},fps={'numerator':24,'denominator':1},
            frame_range={'start':0,'end':2},source_start={'numerator':0,'denominator':1},playback_rate={'numerator':1,'denominator':1},camera='camera',
            settings=dict(engine='cycles',device_policy='cpu',device=None,allow_cpu_fallback=False,resolution=[3,2],samples=1,seed=0,denoise=False,
                output=output(),exposure=0,view_transform='standard',resources={'memory_bytes':1024**3,'storage_bytes':1024**3,'timeout_seconds':60,'workers':1,'max_frames':2}),
            priority=0,max_attempts=1,cache_policy='verify-reuse',destination={'path':'output','owner_token':'test'})
        backend=dict(id='blender',adapter_version='1.0.0',application_version='fixture',executable_sha256='0'*64,worker_sha256='1'*64,platform='fixture',engine='cycles',device='cpu')
        destination=root/'output';(destination/'frames').mkdir(parents=True)
        images=[]
        for n in range(2):
            path=destination/'frames'/f'{n:06d}.png';self.png(path)
            images.append(dict(frame=n,file={'path':f'frames/{n:06d}.png','bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},format='png',bit_depth=8,pass_name='combined',resolution=[3,2]))
        result=dict(schema_version=1,kind='render3d-result',job='test',request_sha256=object_hash(request),snapshot_sha256=snapshot['snapshot_sha256'],
            backend=backend,status='complete',requested_frames=[0,1],successful=[0,1],failed=[],skipped=[],reused=[],images=images,diagnostics=[],
            usage={'elapsed_seconds':0,'peak_memory_bytes':None,'output_bytes':None,'measurement':'unavailable'},logs=[],manifest=None,
            verification={'status':'not-run','verifier':None,'evidence':None})
        return request,result,destination,snapshot_root,backend

    def test_result_full_decode_provisional_status_and_input_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            before=deepcopy(result)
            report=verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(report['decoded_frames'],2);self.assertEqual(report['decoded_images'],2)
            self.assertEqual(result,before)
            self.assertEqual(report['request_sha256'],object_hash(request))
            self.assertEqual(report['settings_sha256'],object_hash(request['settings']))
            self.assertEqual(report['images'][1]['presentation_time'],{'numerator':1,'denominator':24})
            bad=deepcopy(result);bad['status']='invented'
            with self.assertRaises(Render3DError):verify_result(request,bad,root,snapshot_root=snapshot,expected_backend=backend)

    def test_result_rejects_missing_corrupt_extra_and_wrong_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            for key in ('request_sha256','snapshot_sha256'):
                bad=deepcopy(result);bad[key]='2'*64
                with self.assertRaises(Render3DError):verify_result(request,bad,root,snapshot_root=snapshot,expected_backend=backend)
            bad=deepcopy(result);bad['backend']['application_version']='different'
            with self.assertRaises(Render3DError):verify_result(request,bad,root,snapshot_root=snapshot,expected_backend=backend)
            extra=root/'frames'/'unexpected.png';extra.write_bytes((root/'frames/000000.png').read_bytes())
            with self.assertRaises(Render3DError):verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            extra.unlink();(root/'frames/000000.png').write_bytes(b'corrupt')
            with self.assertRaises(Render3DError):verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            (root/'frames/000000.png').unlink()
            with self.assertRaises(Render3DError):verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)

    def test_result_rejects_symlink_image_and_changed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            image=root/'frames/000000.png';target=root/'copy.png';image.rename(target);image.symlink_to(target)
            with self.assertRaises(Render3DError):verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            image.unlink();target.rename(image)
            (snapshot/'triangle.obj').write_bytes(b'changed')
            with self.assertRaises(Render3DError):verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)

    def test_result_snapshot_disappearing_between_owner_reads_is_typed(self):
        import shutil
        from unittest.mock import patch
        import render3d_snapshot
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            original=render3d_snapshot.verify_snapshot
            def disappearing(*args,**kwargs):
                verified=original(*args,**kwargs)
                shutil.rmtree(snapshot)
                return verified
            with patch.object(render3d_snapshot,'verify_snapshot',side_effect=disappearing):
                with self.assertRaises(Render3DError) as error:
                    verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(error.exception.report['category'],'stale-asset')

    def test_unexpected_directory_and_nonregular_inventory_reject(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            extra=root/'frames'/'unexpected';extra.mkdir()
            with self.assertRaises(Render3DError):verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            extra.rmdir()
            if hasattr(os,'mkfifo'):
                os.mkfifo(extra)
                with self.assertRaises(Render3DError):verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
