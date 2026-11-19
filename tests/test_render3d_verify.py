"""Real image decoding and source-bound result verification; fixtures are synthetic."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

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


class EXREnvelopeAdmission(unittest.TestCase):
    """Header refusals must happen without constructing any native decoder."""

    def reject_before_decoder(self, payload, message, **options):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'envelope.exr'
            path.write_bytes(payload)
            decoder=mock.Mock()
            with mock.patch.dict(sys.modules, {'OpenEXR':decoder}):
                for decode in (verify_image,decode_image):
                    with self.subTest(decode=decode.__name__):
                        with self.assertRaisesRegex(Render3DError,message):
                            decode(path,output=output('exr',32),resolution=[3,2],**options)
                decoder.InputFile.assert_not_called()
                decoder.File.assert_not_called()

    def test_unsupported_flags_and_versions_reject_before_native_constructor(self):
        # Flags from pinned OpenEXR 3.5.4 ImfVersion.h; unknown bits fail closed.
        for flags in (0x200,0x800,0x1000,0x1800,0x600,0x100,0x2000,0x80000000):
            with self.subTest(flags=flags):
                self.reject_before_decoder(struct.pack('<II',20000630,2|flags), 'unsupported flags')
        for version in (0,1,3,255):
            with self.subTest(version=version):
                self.reject_before_decoder(struct.pack('<II',20000630,version), 'format version')
        self.reject_before_decoder(b'notexr!!', 'Not an EXR')
        for size in range(1,8):
            self.reject_before_decoder(struct.pack('<II',20000630,2)[:size], 'Truncated EXR envelope')

    def test_storage_attributes_cannot_bypass_flags(self):
        prefix=struct.pack('<II',20000630,2)
        for storage in (b'deepscanline',b'deeptile',b'tiledimage',b'unknown'):
            attribute=b'type\x00string\x00'+struct.pack('<i',len(storage))+storage+b'\x00'
            self.reject_before_decoder(prefix+attribute, 'Only EXR scanlineimage')
        # Tile level modes: ONE_LEVEL=0, MIPMAP_LEVELS=1, RIPMAP_LEVELS=2.
        # These tiny malformed envelopes deliberately omit the tiled flag.
        for mode in (0,1,2):
            tiles=b'tiles\x00tiledesc\x00'+struct.pack('<iIIB',9,2,2,mode)+b'\x00'
            self.reject_before_decoder(prefix+tiles, 'Tiled and multilevel')

    def test_malformed_and_overbudget_header_rejects_before_native_constructor(self):
        prefix=struct.pack('<II',20000630,2)
        attribute=b'comment\x00string\x00'+struct.pack('<i',1)+b'x'
        cases=(
            (prefix, 'Truncated EXR header'),
            (prefix+b'x'*32+b'\x00', 'name exceeds'),
            (prefix+b'x\x00\x00', 'Empty EXR attribute type'),
            (prefix+b'x\x00string\x00'+struct.pack('<i',-1), 'Invalid EXR attribute size'),
            (prefix+b'x\x00string\x00'+struct.pack('<i',100), 'Invalid EXR attribute size'),
            (prefix+attribute+attribute+b'\x00', 'Duplicate EXR header attribute'),
        )
        for payload,message in cases:
            with self.subTest(message=message):
                self.reject_before_decoder(payload,message)
        self.reject_before_decoder(prefix+attribute+b'\x00', 'EXR header exceeds',memory_bytes=16)
        payload=b'x'*(1024**2)
        self.reject_before_decoder(prefix+b'x\x00string\x00'+struct.pack('<i',len(payload))+payload+b'\x00',
                                   'EXR header exceeds')


    def test_channel_admission_limits_precede_native_constructor(self):
        prefix=struct.pack('<II',20000630,2)
        def entry(name, precision=2, sampling=1):
            return name+b'\x00'+struct.pack('<iB3sii',precision,0,b'\x00'*3,sampling,sampling)
        def envelope(channels):
            return prefix+b'channels\x00chlist\x00'+struct.pack('<i',len(channels))+channels+b'\x00'
        valid=entry(b'R')
        cases=[(b'\x00','Malformed EXR channel list'),
               (valid,'Unterminated EXR channel list'),
               (valid+b'R\x00','Duplicate EXR channel name'),
               (b'R\x00'+b'\x00'*15,'Truncated EXR channel metadata'),
               (entry(b'R',1)+b'\x00','precision or sampling'),
               (entry(b'R',sampling=2)+b'\x00','precision or sampling'),
               (valid+b'\x00extra','Malformed EXR channel list'),
               (b''.join(entry(f'C{n}'.encode()) for n in range(65))+b'\x00','channel count')]
        for channels,message in cases:
            with self.subTest(message=message):
                self.reject_before_decoder(envelope(channels),message)
        # The same bounded header fits; its declared decoded samples do not.
        self.reject_before_decoder(envelope(b''.join(entry(f'C{n}'.encode()) for n in range(64))+b'\x00'),
                                   'decoded channels exceed|preview arrays exceed',memory_bytes=1500)


@unittest.skipUnless(DEPENDENCIES,'optional OpenEXR/pypng decoder packages unavailable')
class ImageVerification(unittest.TestCase):
    def png(self,path,depth=8,alpha=True):
        value=(1<<depth)-1
        writer=png.Writer(3,2,greyscale=False,alpha=alpha,bitdepth=depth)
        row=[value,0,value,value//2] * 3 if alpha else [value,0,value]*3
        with path.open('wb') as stream:writer.write(stream,[row,row])

    def exr(self,path,depth=32,channels=None,header=None):
        dtype=np.float16 if depth==16 else np.float32
        data=np.array([[0,2,10],[.25,.5,1]],dtype=dtype)
        values=channels or dict(R=data,G=data,B=data,A=np.ones((2,3),dtype=dtype))
        with OpenEXR.File(header or {},values) as fixture:
            fixture.write(str(path))

    def test_unverified_compressions_reject_actual_files_before_decoder(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'new-compression.exr'
            for name in ('HTJ2K32_COMPRESSION','HTJ2K256_COMPRESSION','LJ2K_COMPRESSION','ZSTD_COMPRESSION'):
                self.exr(path,header={'compression':getattr(OpenEXR,name)})
                original=path.read_bytes()
                # Genuine codec output and trailing bytes both require refusal:
                # native decode support alone does not verify the chunk envelope.
                for payload in (original,original+b'unreferenced'):
                    path.write_bytes(payload)
                    for decode in (verify_image,decode_image):
                        with self.subTest(compression=name,decode=decode.__name__,trailing=payload!=original):
                            with mock.patch.object(OpenEXR,'InputFile') as constructor:
                                with self.assertRaisesRegex(Render3DError,'compression envelope'):
                                    decode(path,output=output('exr',32),resolution=[3,2])
                                constructor.assert_not_called()

    def test_standalone_contract_rejects_before_file_capture(self):
        import render3d_verify
        invalid=[(output('exr',depth),[3,2],'combined') for depth in (8,17,True,32.0)]
        invalid += [(dict(output('exr',32),alpha='premultiplied'),[3,2],'combined'),
                    (dict(output('exr',32),color_space='srgb'),[3,2],'combined'),
                    (dict(output(),passes=['depth']),[3,2],'depth'),
                    (dict(output('exr',32),passes=['combined','invented']),[3,2],'combined'),
                    (dict(output('exr',32),passes=['combined','combined']),[3,2],'combined'),
                    (output(),[0,2],'combined'),(output(),[3,2,1],'combined'),
                    (output(),[True,2],'combined'),(output(),[3,16385],'combined'),
                    (output(),[3,2],'depth'),({},[3,2],'combined')]
        for contract,resolution,selected in invalid:
            for decode in (verify_image,decode_image):
                with self.subTest(output=contract,resolution=resolution,pass_name=selected,decode=decode.__name__):
                    with mock.patch.object(render3d_verify,'_captured') as capture:
                        with self.assertRaises(Render3DError):
                            decode('must-not-open',output=contract,resolution=resolution,pass_name=selected)
                        capture.assert_not_called()

    def test_invalid_verification_budgets_reject_before_file_capture(self):
        import render3d_verify
        for option in ('max_bytes','memory_bytes'):
            for value in (-2,-1,0,True,False,1.0,float('nan'),float('inf'),2**40+1):
                for decode in (verify_image,decode_image):
                    with self.subTest(option=option,value=value,decode=decode.__name__):
                        with mock.patch.object(render3d_verify,'_captured') as capture:
                            with self.assertRaises(Render3DError):
                                decode('must-not-open',output=output(),resolution=[3,2],**{option:value})
                            capture.assert_not_called()

    def test_float_exr_cannot_claim_unsupported_precision(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'float.exr';self.exr(path)
            for depth in (8,17):
                for decode in (verify_image,decode_image):
                    with self.subTest(depth=depth,decode=decode.__name__):
                        with self.assertRaises(Render3DError):
                            decode(path,output=output('exr',depth),resolution=[3,2])

    def test_scanline_long_names_hdr_alpha_and_precision_remain_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (16,32):
                path=Path(temporary)/f'long-{depth}.exr'
                self.exr(path,depth,header={'synthetic_attribute_name_longer_than_thirty_one_bytes':'fixture'})
                self.assertTrue(struct.unpack('<I',path.read_bytes()[4:8])[0]&0x400)
                for decode in (verify_image,decode_image):
                    decoded=decode(path,output=output('exr',depth),resolution=[3,2])
                    report=decoded[0] if decode is decode_image else decoded
                    self.assertEqual(report['channels']['R']['maximum'],10)
                    self.assertEqual(report['channels']['A']['minimum'],1)
                    self.assertEqual(report['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
                    if decode is decode_image:
                        self.assertEqual(float(decoded[1]['R'][0,2]),10)
                    with self.assertRaisesRegex(Render3DError,'precision or sampling'):
                        decode(path,output=output('exr',32 if depth==16 else 16),resolution=[3,2])

    def test_actual_multipart_rejects_valid_first_part_with_invalid_second(self):
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (16,32):
                dtype=np.float16 if depth==16 else np.float32
                rgba={name:np.ones((2,3),dtype=dtype) for name in 'RGBA'}
                for invalid in (np.full((2,3),np.inf,dtype=dtype),np.ones((2,3),dtype=np.uint32)):
                    with self.subTest(depth=depth,second_dtype=invalid.dtype):
                        path=Path(temporary)/'multipart.exr'
                        parts=[OpenEXR.Part({},rgba,'valid-beauty'),
                               OpenEXR.Part({},dict(Z=invalid),'invalid-extra')]
                        with OpenEXR.File(parts) as fixture_writer:
                            fixture_writer.write(str(path))
                        with OpenEXR.File(str(path),separate_channels=True) as fixture:
                            self.assertEqual(len(fixture.parts),2)
                            self.assertEqual(set(fixture.channels(0)),set('RGBA'))
                            self.assertEqual(fixture.channels(1)['Z'].pixels.dtype,invalid.dtype)
                            np.testing.assert_array_equal(fixture.channels(1)['Z'].pixels,invalid)
                        for decode in (verify_image,decode_image):
                            with mock.patch.object(OpenEXR,'InputFile') as constructor:
                                with self.assertRaisesRegex(Render3DError,'unsupported flags'):
                                    decode(path,output=output('exr',depth),resolution=[3,2])
                                constructor.assert_not_called()

    def test_actual_tiled_and_deep_exrs_reject_before_native_constructor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'unsupported.exr'
            values={name:np.ones((2,3),dtype=np.float32) for name in 'RGBA'}
            tiled={'type':OpenEXR.tiledimage,'tiles':OpenEXR.TileDescription()}
            self.exr(path,channels=values,header=tiled)
            tiled_bytes=path.read_bytes()
            deep=np.empty((2,3),dtype=object)
            for index in np.ndindex(deep.shape):
                deep[index]=np.array([.25,.75],dtype=np.float32)
            self.exr(path,channels={name:deep.copy() for name in 'RGBA'},
                     header={'type':OpenEXR.deepscanline,'compression':OpenEXR.ZIPS_COMPRESSION})
            deep_bytes=path.read_bytes()
            for label,payload in (('tiled',tiled_bytes),('deep',deep_bytes)):
                for flags_intact in (True,False):
                    # Cleared flags produce an intentionally inconsistent envelope;
                    # the storage attributes still must reject before native code.
                    path.write_bytes(payload if flags_intact else payload[:4]+struct.pack('<I',2)+payload[8:])
                    for decode in (verify_image,decode_image):
                        with self.subTest(storage=label,flags_intact=flags_intact,decode=decode.__name__):
                            with mock.patch.object(OpenEXR,'InputFile') as constructor:
                                with self.assertRaisesRegex(Render3DError,'unsupported flags|Tiled and multilevel|Only EXR scanlineimage'):
                                    decode(path,output=output('exr',32),resolution=[3,2])
                                constructor.assert_not_called()

    def test_png8_and_png16_decode_true_precision(self):
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (8,16):
                path=Path(temporary)/f'{depth}.png';self.png(path,depth)
                report=verify_image(path,output=output(depth=depth),resolution=[3,2])
                self.assertEqual(report['bit_depth'],depth)
                self.assertEqual(report['channels']['R']['maximum'],1)
                self.assertAlmostEqual(report['channels']['A']['minimum'],((1<<depth)-1)//2/((1<<depth)-1))
                self.assertEqual(report['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())


    def test_empty_idat_after_eof_accepts_but_trailing_compressed_data_rejects(self):
        def chunk(kind,payload):
            return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (8,16):
                path=Path(temporary)/f'{depth}.png';self.png(path,depth)
                original=path.read_bytes()
                self.assertEqual(original[-8:-4],b'IEND')
                for payloads in ((b'',),(b'',b''),(b'garbage',),(b'',b'garbage'),(zlib.compress(b''),)):
                    with self.subTest(depth=depth,payloads=payloads):
                        path.write_bytes(original[:-12]+b''.join(chunk(b'IDAT',data) for data in payloads)+original[-12:])
                        for decode in (verify_image,decode_image):
                            if any(payloads):
                                with self.assertRaisesRegex(Render3DError,'Trailing compressed PNG stream'):
                                    decode(path,output=output(depth=depth),resolution=[3,2])
                            else:
                                decoded=decode(path,output=output(depth=depth),resolution=[3,2])
                                report=decoded[0] if decode is decode_image else decoded
                                self.assertEqual(report['channels']['R']['maximum'],1)
                                self.assertEqual(report['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())


    def test_png_idat_run_cannot_restart_after_ancillary_chunk(self):
        def chunk(kind,payload):
            return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
        text_chunk=chunk(b'tEXt',b'Comment\x00synthetic fixture')
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (8,16):
                path=Path(temporary)/f'{depth}.png';self.png(path,depth)
                original=path.read_bytes()
                # Ancillary chunks before or after the one IDAT run remain valid.
                valid=(original[:33]+text_chunk+original[33:],original[:-12]+text_chunk+original[-12:])
                for payload in valid:
                    path.write_bytes(payload)
                    for decode in (verify_image,decode_image):
                        decode(path,output=output(depth=depth),resolution=[3,2])
                for tail in (b'',b'trailing data'):
                    path.write_bytes(original[:-12]+text_chunk+chunk(b'IDAT',tail)+original[-12:])
                    for decode in (verify_image,decode_image):
                        with self.subTest(depth=depth,tail=tail,decode=decode.__name__):
                            with self.assertRaisesRegex(Render3DError,'PNG IDAT chunks must be consecutive'):
                                decode(path,output=output(depth=depth),resolution=[3,2])

    def test_png_color_key_transparency_rejects_before_opaque_or_preview_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (8,16):
                path=Path(temporary)/f'{depth}.png';self.png(path,depth,alpha=False)
                original=path.read_bytes()
                # Normal RGB remains supported as opaque output and preview input.
                for decode in (verify_image,decode_image):
                    decoded=decode(path,output=output(depth=depth,alpha='opaque'),resolution=[3,2])
                    report=decoded[0] if decode is decode_image else decoded
                    self.assertEqual(set(report['channels']),set('RGB'))
                key=struct.pack('>HHH',(1<<depth)-1,0,(1<<depth)-1)
                payload=b'tRNS'+key
                path.write_bytes(original[:33]+struct.pack('>I',len(key))+payload+
                                 struct.pack('>I',zlib.crc32(payload)&0xffffffff)+original[33:])
                # The fixture really displays transparent pixels in a PNG decoder.
                with path.open('rb') as fixture_stream:
                    _,_,rows,metadata=png.Reader(file=fixture_stream).asRGBA()
                    self.assertEqual(metadata['planes'],4)
                    self.assertTrue(all(sample == 0 for row in rows for sample in row[3::4]))
                for alpha in ('opaque','straight'):
                    for decode in (verify_image,decode_image):
                        with self.subTest(depth=depth,alpha=alpha,decode=decode.__name__):
                            with self.assertRaisesRegex(Render3DError,'PNG color-key transparency is unsupported'):
                                decode(path,output=output(depth=depth,alpha=alpha),resolution=[3,2])

    def test_exr_requires_matching_zero_origin_data_and_display_windows(self):
        def window(x,y,width=3,height=2):
            return (np.array([x,y],dtype=np.int32),np.array([x+width-1,y+height-1],dtype=np.int32))
        full=window(0,0)
        cases=(
            ('full',full,full,True),
            ('larger-canvas',full,window(0,0,4,3),False),
            ('shifted-data',window(1,1),full,False),
            ('shifted-display',full,window(1,1),False),
            ('negative-origin',window(-1,-1),window(-1,-1),False),
            ('shifted-both',window(1,1),window(1,1),False),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (16,32):
                for label,data,display,accepted in cases:
                    with self.subTest(depth=depth,case=label):
                        path=Path(temporary)/f'{depth}-{label}.exr'
                        self.exr(path,depth,header={'dataWindow':data,'displayWindow':display})
                        for decode in (verify_image,decode_image):
                            if accepted:
                                decoded=decode(path,output=output('exr',depth),resolution=[3,2])
                                report=decoded[0] if decode is decode_image else decoded
                                self.assertEqual(report['resolution'],[3,2])
                                self.assertEqual(report['channels']['R']['maximum'],10)
                                if decode is decode_image:
                                    self.assertEqual(decoded[1]['R'].shape,(2,3))
                                    self.assertEqual(float(decoded[1]['R'][0,2]),10)
                            else:
                                with self.assertRaisesRegex(Render3DError,'zero-origin requested full frame'):
                                    decode(path,output=output('exr',depth),resolution=[3,2])

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
            with self.assertRaises(Render3DError):verify_image(path,output=output('exr',32,passes=['depth']),resolution=[3,2],pass_name='depth')
            self.exr(path,channels={'Z':np.ones((2,3),dtype=np.float32)})
            with self.assertRaises(Render3DError):verify_image(path,output=output('exr',32),resolution=[3,2])
            report=verify_image(path,output=output('exr',32,passes=['depth']),resolution=[3,2],pass_name='depth')
            self.assertEqual(set(report['channels']),{'Z'})

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

    def test_result_enforces_requested_engine_device_and_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            cases=[('cpu',None,False,'cpu','eevee',False),
                   ('cpu',None,False,'cuda','cycles',False),
                   ('gpu-required','cuda',False,'cpu','cycles',False),
                   ('gpu-required','cuda',False,'metal','cycles',False),
                   ('gpu-required','cuda',False,'cuda','cycles',True),
                   ('gpu-required',None,False,'cuda','cycles',True),
                   ('gpu-preferred','cuda',False,'cpu','cycles',False),
                   ('gpu-preferred','cuda',True,'cpu','cycles',True),
                   ('gpu-preferred','cuda',True,'metal','cycles',False),
                   ('gpu-preferred','cuda',False,'cuda','cycles',True),
                   ('cpu',None,False,'cpu','cycles',True)]
            for policy,device,fallback,actual,engine,accepted in cases:
                req=deepcopy(request);res=deepcopy(result);observed=deepcopy(backend)
                req['settings'].update(device_policy=policy,device=device,allow_cpu_fallback=fallback)
                observed.update(device=actual,engine=engine)
                res.update(request_sha256=object_hash(req),backend=observed)
                with self.subTest(policy=policy,device=device,actual=actual,engine=engine,fallback=fallback):
                    if accepted:
                        self.assertEqual(verify_result(req,res,root,snapshot_root=snapshot,expected_backend=observed)['status'],'passed')
                    else:
                        with self.assertRaises(Render3DError):
                            verify_result(req,res,root,snapshot_root=snapshot,expected_backend=observed)

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

    def test_result_decodes_from_a_scoped_output_directory(self):
        from atomic_publish import pinned_directory, private_stage_supported
        from rig_errors import directory_capability

        if not private_stage_supported():
            self.skipTest('Scoped output requires the Linux descriptor adapter.')
        with tempfile.TemporaryDirectory() as temporary:
            request, result, root, snapshot, backend = self.scene_result(Path(temporary))
            with pinned_directory(root) as descriptor, directory_capability(descriptor) as capability:
                report = verify_result(request, result, capability.path, snapshot_root=snapshot,
                                       expected_backend=backend)
            self.assertEqual(report['decoded_frames'], 2)
            self.assertEqual([image['sha256'] for image in report['images']],
                             [image['file']['sha256'] for image in result['images']])

    def test_optional_source_budget_fails_closed(self):
        import render3d_snapshot

        with tempfile.TemporaryDirectory() as temporary:
            request, result, root, snapshot, backend = self.scene_result(Path(temporary))
            with self.assertRaises(Render3DError) as caught:
                verify_result(request, result, root, snapshot_root=snapshot,
                              expected_backend=backend, max_total_bytes=1)
            category = ('storage-exhaustion' if hasattr(render3d_snapshot, 'native_memory_budget')
                        else 'unsupported-feature')
            self.assertEqual(caught.exception.report['category'], category)

    def test_scoped_source_requires_the_bounded_snapshot_owner(self):
        from atomic_publish import pinned_directory, private_stage_supported
        from rig_errors import directory_capability

        if not private_stage_supported():
            self.skipTest('Scoped source requires the Linux descriptor adapter.')
        with tempfile.TemporaryDirectory() as temporary:
            request, result, root, snapshot, backend = self.scene_result(Path(temporary))
            legacy_owner = mock.Mock(spec=['verify_snapshot'])
            with (pinned_directory(snapshot) as descriptor,
                  directory_capability(descriptor) as capability,
                  mock.patch.dict(sys.modules, {'render3d_snapshot': legacy_owner}),
                  self.assertRaises(Render3DError) as caught):
                verify_result(request, result, root, snapshot_root=capability.path,
                              expected_backend=backend)
            self.assertEqual(caught.exception.report['category'], 'unsupported-feature')
            legacy_owner.verify_snapshot.assert_not_called()

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


    def test_preserved_policy_probe_requires_affirmative_device_class(self):
        # Preserve all six cases from the independent policy probe, then cover
        # opaque selected IDs, ambiguous spellings, and explicit fallback.
        cases=[('cpu',None,False,'cpu',True),
               ('gpu-required',None,False,'cuda',True),
               ('gpu-required',None,False,'cpu',False),
               ('gpu-required',None,False,'CPU',False),
               ('gpu-required',None,False,'unknown',False),
               ('gpu-preferred',None,False,'unavailable',False),
               ('gpu-preferred',None,True,'unknown',False),
               ('gpu-preferred',None,True,'CPU',False),
               ('gpu-required','unknown',False,'unknown',False),
               ('gpu-required','CUDA:0',False,'CUDA:0',False),
               ('gpu-required',None,False,'CUDA',False),
               ('gpu-preferred',None,True,'cpu',True),
               ('gpu-preferred','cuda',True,'cpu',True),
               ('gpu-preferred','cuda',False,'cuda',True)]
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            for policy,selected,fallback,actual,accepted in cases:
                req,res,observed=deepcopy(request),deepcopy(result),deepcopy(backend)
                req['settings'].update(device_policy=policy,device=selected,allow_cpu_fallback=fallback)
                observed['device']=actual
                res.update(request_sha256=object_hash(req),backend=observed)
                with self.subTest(policy=policy,selected=selected,fallback=fallback,actual=actual):
                    if accepted:
                        report=verify_result(req,res,root,snapshot_root=snapshot,expected_backend=observed)
                        self.assertEqual(report['status'],'passed')
                        self.assertEqual(report['decoded_images'],2)
                        self.assertEqual(report['backend'],observed)
                    else:
                        with self.assertRaises(Render3DError) as caught:
                            verify_result(req,res,root,snapshot_root=snapshot,expected_backend=observed)
                        self.assertEqual(caught.exception.report['category'],'unsupported-device')

    def test_real_parent_symlink_replacement_between_validation_and_capture_rejects(self):
        import render3d_verify
        original=render3d_verify._image_source
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            control=verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(control['decoded_images'],2)
            replacement=root.parent/'replacement';replacement.mkdir()
            for image in (root/'frames').iterdir():
                (replacement/image.name).write_bytes(image.read_bytes())
            scheduled=[]
            @contextmanager
            def replace_parent(path, **options):
                replaced=False
                if path.parent == root/'frames' and not scheduled:
                    (root/'frames').rename(root/'held-frames')
                    (root/'frames').symlink_to(replacement,target_is_directory=True)
                    scheduled.append(True);replaced=True
                try:
                    with original(path,**options) as source:
                        yield source
                finally:
                    if replaced:
                        (root/'frames').unlink();(root/'held-frames').rename(root/'frames')
            with mock.patch.object(render3d_verify,'_image_source',replace_parent):
                with self.assertRaises(Render3DError) as caught:
                    verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(scheduled,[True])
            self.assertEqual(caught.exception.report['category'],'corrupt-output')
            restored=verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(restored['images'],control['images'])

    def test_real_parent_replacement_during_read_rejects(self):
        import render3d_verify
        original=render3d_verify.open_payload
        with tempfile.TemporaryDirectory() as temporary:
            parent=Path(temporary)/'images';parent.mkdir()
            path=parent/'image.png';self.png(path)
            reference=hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(verify_image(path,output=output(),resolution=[3,2])['sha256'],reference)
            scheduled=[]
            @contextmanager
            def replace_parent(root,name,**options):
                with original(root,name,**options) as opened:
                    scheduled.append(True)
                    parent.rename(parent.with_name('held-images'))
                    parent.mkdir();self.png(path)
                    yield opened
            with mock.patch.object(render3d_verify,'open_payload',replace_parent):
                with self.assertRaises(Render3DError) as caught:
                    verify_image(path,output=output(),resolution=[3,2])
            self.assertEqual(scheduled,[True])
            self.assertEqual(caught.exception.report['category'],'corrupt-output')
            # Replacement bytes are valid; refusal is directory/file identity.
            self.assertEqual(verify_image(path,output=output(),resolution=[3,2])['sha256'],reference)

    def test_scoped_output_retains_authority_after_ordinary_name_replacement(self):
        from atomic_publish import pinned_directory, private_stage_supported
        from rig_errors import directory_capability
        if not private_stage_supported():
            self.skipTest('Scoped output requires the Linux descriptor adapter.')
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            with pinned_directory(root) as descriptor, directory_capability(descriptor) as capability:
                root.rename(root.with_name('held-output'))
                root.mkdir();(root/'frames').mkdir()
                (root/'frames/000000.png').write_bytes(b'not the scoped image')
                report=verify_result(request,result,capability.path,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(report['decoded_images'],2)
            self.assertEqual([image['sha256'] for image in report['images']],
                             [image['file']['sha256'] for image in result['images']])

    def test_inventory_root_replacement_is_not_accepted(self):
        import render3d_verify
        original=render3d_verify._image_inventory
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            scheduled=[]
            def replace_root(path,descriptor,passes,limit):
                scheduled.append(True)
                root.rename(root.with_name('held-output'))
                root.mkdir();(root/'frames').mkdir()
                for image in root.with_name('held-output').joinpath('frames').iterdir():
                    (root/'frames'/image.name).write_bytes(image.read_bytes())
                return original(path,descriptor,passes,limit)
            with mock.patch.object(render3d_verify,'_image_inventory',replace_root):
                with self.assertRaises(Render3DError) as caught:
                    verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(scheduled,[True])
            self.assertEqual(caught.exception.report['category'],'corrupt-output')

    def test_actual_exr_channels_and_decoded_budget_reject_before_constructor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'channels.exr'
            for count,resolution,budget,decode in ((65,[3,2],1024**3,verify_image),
                                                  (64,[64,32],100000,verify_image),
                                                  (64,[64,32],600000,decode_image)):
                values={f'C{n}':np.ones((resolution[1],resolution[0]),dtype=np.float32) for n in range(count)}
                self.exr(path,channels=values)
                with self.subTest(channels=count,resolution=resolution,decode=decode.__name__):
                    with mock.patch.object(OpenEXR,'InputFile',wraps=OpenEXR.InputFile) as constructor:
                        with self.assertRaisesRegex(Render3DError,'channel count|channels exceed|preview arrays exceed'):
                            decode(path,output=output('exr',32),resolution=resolution,memory_bytes=budget)
                        constructor.assert_not_called()

    def test_public_capture_diagnostics_do_not_expose_caller_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'private-caller-namespace';root.mkdir()
            image=root/'private-arbitrary-basename.png';self.png(image)
            empty=root/'private-empty-basename.png';empty.write_bytes(b'')
            for path,budget,category in ((image,1,'storage-exhaustion'),
                                         (empty,65536,'corrupt-output'),
                                         (root/'private-missing-basename.png',65536,'corrupt-output')):
                for decode in (verify_image,decode_image):
                    with self.subTest(case=category,decode=decode.__name__):
                        with self.assertRaises(Render3DError) as caught:
                            decode(path,output=output(),resolution=[3,2],max_bytes=budget)
                        report=caught.exception.report
                        self.assertEqual(report['category'],category)
                        self.assertEqual(report['code'],'RENDER3D_'+category.upper().replace('-','_'))
                        self.assertEqual(report['path'],'image')
                        self.assertNotIn(str(root),json.dumps(report))
                        self.assertNotIn('private-',json.dumps(report))

    def test_native_and_external_owner_failures_have_fixed_public_diagnostics(self):
        import render3d_verify
        import render3d_snapshot
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            private=str(root/'private-native-detail.png')
            for native in (OSError(private),ValueError(private),RuntimeError(private)):
                with self.subTest(native=type(native).__name__):
                    with mock.patch.object(render3d_verify,'_png',side_effect=native):
                        with self.assertRaises(Render3DError) as caught:
                            verify_image(root/'frames/000000.png',output=output(),resolution=[3,2])
                    self.assertEqual(caught.exception.report['category'],'corrupt-output')
                    self.assertEqual(caught.exception.report['path'],'image')
                    self.assertNotIn(private,json.dumps(caught.exception.report))
                    self.assertNotIn(private,str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)
            failures=[(render3d_verify,'_image_inventory',OSError(private),'corrupt-output',''),
                      (render3d_snapshot,'verify_snapshot',Render3DError('stale-asset',private,path=private),'stale-asset','snapshot.json')]
            for owner,operation,error,category,path in failures:
                with self.subTest(operation=operation):
                    with mock.patch.object(owner,operation,side_effect=error):
                        with self.assertRaises(Render3DError) as caught:
                            verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
                    self.assertEqual(caught.exception.report['category'],category)
                    self.assertEqual(caught.exception.report['path'],path)
                    self.assertNotIn(private,json.dumps(caught.exception.report))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)
            invalid=deepcopy(request);invalid['id']=private
            with self.assertRaises(Render3DError) as caught:
                verify_result(invalid,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(caught.exception.report['category'],'invalid-input')
            self.assertNotIn(private,json.dumps(caught.exception.report))
            with self.assertRaises(Render3DError) as caught:
                verify_image('must-not-open',output=dict(output(),color_space=private),resolution=[3,2])
            self.assertEqual(caught.exception.report['category'],'invalid-input')
            self.assertNotIn(private,json.dumps(caught.exception.report))

    def test_result_aggregate_budget_exhaustion_stops_before_next_capture(self):
        import render3d_verify
        original=render3d_verify._image_source
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            first=result['images'][0]['file']['bytes']
            total=sum(image['file']['bytes'] for image in result['images'])
            for budget,expected,captures in ((1,'frames/000000.png',1),
                                             (first,'frames/000001.png',1),
                                             (total-1,'frames/000001.png',2)):
                req,res=deepcopy(request),deepcopy(result)
                req['settings']['resources']['storage_bytes']=budget
                res['request_sha256']=object_hash(req)
                captured=[]
                @contextmanager
                def observe(path,**options):
                    if path.parent == root/'frames':captured.append(path.name)
                    with original(path,**options) as source:
                        yield source
                with self.subTest(budget=budget):
                    with mock.patch.object(render3d_verify,'_image_source',observe):
                        with self.assertRaises(Render3DError) as caught:
                            verify_result(req,res,root,snapshot_root=snapshot,expected_backend=backend)
                    self.assertEqual(caught.exception.report['category'],'storage-exhaustion')
                    self.assertEqual(caught.exception.report['path'],expected)
                    self.assertNotIn(str(root),json.dumps(caught.exception.report))
                    self.assertEqual(len(captured),captures)
            request['settings']['resources']['storage_bytes']=total
            result['request_sha256']=object_hash(request)
            report=verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(report['status'],'passed')
            self.assertEqual(report['output_bytes'],total)

    def test_inventory_stream_stops_at_first_excess_entry_and_closes(self):
        import render3d_verify
        original=render3d_verify.os.scandir
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            for index in range(32):
                (root/'frames'/f'extra-{index:03d}.png').write_bytes(b'extra')
            visited=[];closed=[]
            @contextmanager
            def streaming(path):
                with original(path) as entries:
                    def observed():
                        for entry in entries:
                            visited.append(entry.name)
                            if len(visited)>3:
                                raise AssertionError('Inventory consumed beyond its first excess entry.')
                            yield entry
                    try:
                        yield observed()
                    finally:
                        closed.append(True)
            with mock.patch.object(render3d_verify.os,'scandir',streaming):
                with self.assertRaises(Render3DError) as caught:
                    verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(caught.exception.report['category'],'incomplete-output')
            self.assertEqual(len(visited),3)
            self.assertEqual(closed,[True])

    def test_image_hashes_describe_capture_and_do_not_freeze_live_destination(self):
        import render3d_verify
        original=render3d_verify._image_inventory
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            captured=result['images'][0]['file']['sha256']
            def change_after_capture(path,descriptor,passes,limit):
                (root/'frames/000000.png').write_bytes(b'changed after captured verification')
                return original(path,descriptor,passes,limit)
            with mock.patch.object(render3d_verify,'_image_inventory',change_after_capture):
                report=verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(report['images'][0]['sha256'],captured)
            self.assertNotEqual(hashlib.sha256((root/'frames/000000.png').read_bytes()).hexdigest(),captured)

    def test_png_declared_chunk_fits_capture_before_payload_read(self):
        import render3d_verify
        def chunk(kind,payload):
            return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
        signature=b'\x89PNG\r\n\x1a\n'
        ihdr=chunk(b'IHDR',struct.pack('>IIBBBBB',3,2,8,6,0,0,0))
        class BoundedReads(io.BytesIO):
            def __init__(self,payload):
                super().__init__(payload);self.requests=[]
            def read(self,size=-1):
                self.requests.append(size)
                if not 0 <= size <= 64:
                    raise AssertionError('Parser requested data beyond this tiny captured extent.')
                return super().read(size)
        cases=[signature+struct.pack('>I4s',2**28,b'IHDR'),
               signature+ihdr+struct.pack('>I4s',2**28,b'IDAT'),
               signature+struct.pack('>I4s',13,b'IHDR')+b'\x00'*13+b'\x00'*3,
               signature+ihdr+struct.pack('>I4s',1,b'IDAT')+b'x'+b'\x00'*3]
        for payload in cases:
            stream=BoundedReads(payload)
            with self.subTest(captured_bytes=len(payload)):
                with self.assertRaisesRegex(Render3DError,'Truncated PNG payload'):
                    render3d_verify._png(stream,[3,2],8,2**29)
                self.assertEqual(max(stream.requests),13 if payload.startswith(signature+ihdr) else 8)
                self.assertNotIn(2**28,stream.requests)
        with tempfile.TemporaryDirectory() as temporary:
            for depth in (8,16):
                path=Path(temporary)/f'positive-{depth}.png';self.png(path,depth)
                report=verify_image(path,output=output(depth=depth),resolution=[3,2])
                self.assertEqual(report['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(report['bit_depth'],depth)

    def test_result_rejects_actual_hardlink_aliases_but_accepts_equal_copies(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            first=root/'frames/000000.png';second=root/'frames/000001.png'
            expected=first.read_bytes()
            self.assertEqual(second.read_bytes(),expected)
            self.assertNotEqual(first.stat().st_ino,second.stat().st_ino)
            control=verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(control['decoded_images'],2)
            second.unlink();os.link(first,second)
            self.assertEqual(first.stat().st_nlink,2)
            with self.assertRaises(Render3DError) as caught:
                verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(caught.exception.report['category'],'corrupt-output')
            self.assertEqual(caught.exception.report['path'],'frames/000000.png')
            # Standalone inspection intentionally accepts an explicitly supplied
            # linked source; unique materialization is a result-output policy.
            for decode in (verify_image,decode_image):
                decoded=decode(first,output=output(),resolution=[3,2])
                report=decoded[0] if decode is decode_image else decoded
                self.assertEqual(report['sha256'],hashlib.sha256(expected).hexdigest())
            second.unlink();second.write_bytes(expected)
            restored=verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(restored['images'],control['images'])
            external=root.parent/'external-alias.png';os.link(first,external)
            with self.assertRaises(Render3DError) as caught:
                verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(caught.exception.report['category'],'corrupt-output')
            external.unlink()
            self.assertEqual(verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)['decoded_images'],2)

    def test_result_link_policy_uses_original_opened_stat(self):
        import os
        import render3d_verify
        original=render3d_verify.open_payload
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            first=root/'frames/000000.png';alias=root.parent/'external-alias.png'
            os.link(first,alias)
            opened_counts=[]
            @contextmanager
            def unlink_after_open(parent,name,**options):
                with original(parent,name,**options) as opened:
                    if parent == root and name == 'frames/000000.png':
                        opened_counts.append(opened[1].st_nlink)
                        alias.unlink()
                        self.assertEqual(first.stat().st_nlink,1)
                    yield opened
            with mock.patch.object(render3d_verify,'open_payload',unlink_after_open):
                with self.assertRaisesRegex(Render3DError,'independently materialized'):
                    verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(opened_counts,[2])
            self.assertEqual(verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)['decoded_images'],2)

    def test_final_result_inventory_refuses_a_new_external_hardlink(self):
        import os
        import render3d_verify
        original=render3d_verify._image_inventory
        with tempfile.TemporaryDirectory() as temporary:
            request,result,root,snapshot,backend=self.scene_result(Path(temporary))
            first=root/'frames/000000.png';alias=root.parent/'external-alias.png'
            def link_before_inventory(path,descriptor,passes,limit):
                os.link(first,alias)
                return original(path,descriptor,passes,limit)
            with mock.patch.object(render3d_verify,'_image_inventory',link_before_inventory):
                with self.assertRaisesRegex(Render3DError,'independently materialized') as caught:
                    verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)
            self.assertEqual(caught.exception.report['category'],'corrupt-output')
            alias.unlink()
            self.assertEqual(verify_result(request,result,root,snapshot_root=snapshot,expected_backend=backend)['decoded_images'],2)
