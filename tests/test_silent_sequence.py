"""Sequence mechanics fixtures; native integration is an explicit separate gate."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import silent_sequence as sequence

try:
    import render3d_verify
    import png
    import OpenEXR
    import numpy as np
    DECODER=True
except ImportError:
    DECODER=False


class SequenceContracts(unittest.TestCase):
    def test_schemas_are_standalone_and_strict(self):
        value=sequence.schemas()['source']
        self.assertFalse(value['additionalProperties'])
        self.assertEqual(value['properties']['output']['properties']['format']['enum'],['png','exr'])
        for name,schema in sequence.schemas().items():
            self.assertEqual(schema,json.loads((Path(__file__).resolve().parents[1]/f'profiles/silent-sequence-{name}.schema.json').read_text()))

    def test_exclusive_write_preserves_preexisting_foreign_partial(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);partial=root/'.plan.json.partial'
            partial.write_bytes(b'preexisting unrelated file')
            with self.assertRaises(FileExistsError):sequence._write(root,'plan.json',b'new plan')
            self.assertEqual(partial.read_bytes(),b'preexisting unrelated file')
            self.assertFalse((root/'plan.json').exists())

    def test_preflight_rejects_fake_precision_and_excess_before_decode(self):
        request={'settings':{'resolution':[40000,40000],'output':{'format':'exr','bit_depth':8,
            'alpha':'straight','color_space':'srgb','passes':['combined']}},'fps':{'numerator':24,'denominator':1},
            'frame_range':{'start':0,'end':1}}
        result={'images':[{'frame':0,'pass_name':'combined','file':{'path':'frames/000000.exr','sha256':'a'*64,'bytes':1}}]}
        with self.assertRaises(ValueError):sequence.native_source(request,result,'.',snapshot_root='.',expected_backend={})


@unittest.skipUnless(DECODER,'optional canonical image verifier + pypng/OpenEXR/numpy required')
class SequenceImages(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.identity={'resolution':[4,2],'fixture':'Synthetic precision/alpha regression; no native renderer/semantic approval'}
        self.fps={'numerator':24000,'denominator':1001}

    def source(self,fmt='png',bits=8,count=3):
        paths=[]
        for i in range(count):
            path=self.root/f'input-{i}.{fmt}'
            if fmt=='png':
                maximum=2**bits-1
                pixels=[[maximum,0,maximum,0, maximum//2,maximum,0,maximum//2, 0,maximum//2,maximum,maximum, maximum,maximum,maximum,maximum]]*2
                with path.open('wb') as output:
                    png.Writer(4,2,alpha=True,greyscale=False,bitdepth=bits).write(output,pixels)
            else:
                dtype=np.float16 if bits==16 else np.float32
                values=np.array([[0,.25,2,4],[1,1.5,3,8]],dtype=dtype)
                header={'compression':OpenEXR.ZIP_COMPRESSION,'type':OpenEXR.scanlineimage}
                channels={'R':values,'G':values*.5,'B':values*.25,'A':np.array([[0,.5,1,1],[1,.5,0,1]],dtype=dtype)}
                OpenEXR.File(header,channels).write(str(path))
            paths.append(path.name)
        output={'format':fmt,'bit_depth':bits,'passes':['combined'],'alpha':'straight','color_space':'srgb' if fmt=='png' else 'linear'}
        return sequence.build_source(self.root,paths,fps=self.fps,output=output,identity=self.identity)

    def test_all_four_precision_formats_preserve_exact_bytes_alpha_and_hdr(self):
        for fmt,bits in [('png',8),('png',16),('exr',16),('exr',32)]:
            with self.subTest(format=fmt,bits=bits):
                source=self.source(fmt,bits);destination=self.root/f'{fmt}-{bits}'
                report=sequence.export_sequence(source,self.root,destination,provenance=self.identity)
                verified=sequence.verify_sequence(destination,expected_receipt_sha256=hashlib.sha256((destination/'sequence.json').read_bytes()).hexdigest())
                self.assertEqual(report,verified)
                for frame,decoded in zip(source['frames'],report['decoded']):
                    self.assertEqual((destination/frame['path']).read_bytes(),(self.root/frame['source_path']).read_bytes())
                    self.assertEqual(decoded['bit_depth'],bits)
                    self.assertEqual(set(decoded['channels']),set('RGBA'))
                    self.assertEqual(decoded['channels']['A']['minimum'],0)
                    self.assertEqual(decoded['channels']['A']['maximum'],1)
                    if fmt=='exr':self.assertGreater(decoded['channels']['R']['maximum'],1)
                self.assertEqual([f['path'] for f in source['frames']],[f'frames/{i:06d}.{fmt}' for i in range(3)])

    def test_cancellation_retains_verified_cache_and_resume_does_not_overwrite(self):
        source=self.source();destination=self.root/'resume'
        calls=[0]
        def cancelled():calls[0]+=1;return calls[0]==2
        with self.assertRaisesRegex(ValueError,'cancelled'):
            sequence.export_sequence(source,self.root,destination,provenance=self.identity,cancel=cancelled)
        stage=self.root/'.resume.sequence-stage';frame=stage/'frames/000000.png'
        self.assertTrue(frame.is_file());before=frame.stat().st_mtime_ns
        sequence.export_sequence(source,self.root,destination,provenance=self.identity)
        self.assertEqual((stage/'frames/000000.png').stat().st_mtime_ns,before)
        self.assertEqual((destination/'frames/000000.png').read_bytes(),frame.read_bytes())
        self.assertTrue(stage.exists())  # persistent verified cache remains reusable
        with self.assertRaises(ValueError):sequence.export_sequence(source,self.root,destination,provenance=self.identity)

    def test_public_cache_basename_swap_cannot_replace_verified_publication(self):
        source=self.source(count=1);destination=self.root/'swap'
        stage=self.root/'.swap.sequence-stage';calls=[0]
        def swap_at_commit():
            calls[0]+=1
            if calls[0]==2:
                stage.rename(self.root/'original-verified-cache');stage.mkdir()
                (stage/'unverified.txt').write_text('unrelated replacement')
            return False
        report=sequence.export_sequence(source,self.root,destination,provenance=self.identity,cancel=swap_at_commit)
        self.assertEqual(sequence.verify_sequence(destination),report)
        self.assertFalse((destination/'unverified.txt').exists())
        self.assertEqual((stage/'unverified.txt').read_text(),'unrelated replacement')
        self.assertTrue((self.root/'original-verified-cache/frames/000000.png').is_file())

    @unittest.skipUnless(os.name=='posix','requires POSIX directory handles')
    def test_partial_cleanup_pins_parent_across_directory_symlink_swap(self):
        stage=self.root/'cleanup';stage.mkdir();(stage/'frames').mkdir()
        unrelated=self.root/'unrelated';unrelated.mkdir();name='.000000.png.partial'
        (stage/'frames'/name).write_bytes(b'owned incomplete write')
        (unrelated/name).write_bytes(b'unrelated original')
        real_open=os.open;swapped=[False]
        def replace_after_open(path,*args,**kwargs):
            fd=real_open(path,*args,**kwargs)
            if path==name and not swapped[0]:
                swapped[0]=True
                (stage/'frames').rename(stage/'original-frames')
                (stage/'frames').symlink_to(unrelated,target_is_directory=True)
            return fd
        with mock.patch.object(sequence.os,'open',side_effect=replace_after_open):
            sequence._clear_partial(stage,'frames/'+name)
        self.assertTrue(swapped[0]);self.assertEqual((unrelated/name).read_bytes(),b'unrelated original')
        self.assertFalse((stage/'original-frames'/name).exists())

    def test_partial_write_and_missing_frame_directory_resume(self):
        source=self.source();destination=self.root/'partial'
        with self.assertRaises(ValueError):
            sequence.export_sequence(source,self.root,destination,provenance=self.identity,cancel=lambda:True)
        stage=self.root/'.partial.sequence-stage'
        (stage/'frames/.000000.png.partial').write_bytes(b'')
        sequence.export_sequence(source,self.root,destination,provenance=self.identity)
        self.assertTrue(sequence.verify_sequence(destination))

    def test_corrupt_missing_extra_duplicate_stale_and_mismatched_frames_rejected(self):
        source=self.source()
        for index,mutate in enumerate([lambda x:x['frames'].__setitem__(1,copy.deepcopy(x['frames'][0])),
            lambda x:x['frames'][0].update(source_path='../escape'),
            lambda x:x.update(resolution=[5,2]),lambda x:x['frames'][0].update(sha256='0'*64),
            lambda x:x['output'].update(format='exr',bit_depth=8)]):
            value=copy.deepcopy(source);mutate(value)
            with self.assertRaises(ValueError):sequence.export_sequence(value,self.root,self.root/f'bad-{index}',provenance=self.identity)
        destination=self.root/'verified';sequence.export_sequence(source,self.root,destination,provenance=self.identity)
        (destination/'frames/000001.png').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):sequence.verify_sequence(destination)
        (destination/'frames/000001.png').unlink()
        with self.assertRaises(ValueError):sequence.verify_sequence(destination)
        (destination/'frames/000001.png').write_bytes((self.root/'input-1.png').read_bytes())
        (destination/'frames/extra.png').write_bytes(b'extra')
        with self.assertRaises(ValueError):sequence.verify_sequence(destination)

    def test_lossy_exr_container_is_not_labeled_lossless_master(self):
        source=self.source('exr',32,count=1)
        path=self.root/'input-0.exr'
        values=np.ones((2,4),dtype=np.float32)
        OpenEXR.File({'compression':OpenEXR.DWAA_COMPRESSION,'type':OpenEXR.scanlineimage},
            {name:values for name in 'RGBA'}).write(str(path))
        source['frames'][0].update(bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError,'lossy'):
            sequence.export_sequence(source,self.root,self.root/'lossy',provenance=self.identity)

    def test_wrong_bit_depth_and_nonopaque_claim_fail(self):
        source=self.source('png',16)
        for key,value in [('bit_depth',8),('alpha','opaque')]:
            changed=copy.deepcopy(source);changed['output'][key]=value
            with self.assertRaises(ValueError):sequence.export_sequence(changed,self.root,self.root/key,provenance=self.identity)

    def test_manifest_and_provenance_tampering_fail_hash_bound_verification(self):
        source=self.source();destination=self.root/'receipt'
        sequence.export_sequence(source,self.root,destination,provenance=self.identity)
        receipt=destination/'sequence.json';digest=hashlib.sha256(receipt.read_bytes()).hexdigest()
        report=json.loads(receipt.read_text());report['provenance']['fixture']='changed';receipt.write_text(json.dumps(report))
        with self.assertRaises(ValueError):sequence.verify_sequence(destination)
        with self.assertRaises(ValueError):sequence.verify_sequence(destination,expected_receipt_sha256=digest)

    @unittest.skipUnless(os.name=='posix','requires POSIX special files')
    def test_fifo_symlink_and_foreign_staging_rejected_without_blocking(self):
        source=self.source();destination=self.root/'foreign'
        with self.assertRaises(ValueError):sequence.export_sequence(source,self.root,destination,provenance=self.identity,cancel=lambda:True)
        stage=self.root/'.foreign.sequence-stage';(stage/'frames/000000.png').symlink_to(self.root/'input-0.png')
        with self.assertRaises(ValueError):sequence.export_sequence(source,self.root,destination,provenance=self.identity)
        (stage/'frames/000000.png').unlink();os.mkfifo(stage/'frames/000000.png')
        with self.assertRaises(ValueError):sequence.export_sequence(source,self.root,destination,provenance=self.identity)
        (stage/'frames/000000.png').unlink();changed=copy.deepcopy(self.identity);changed['foreign']=True
        with self.assertRaises(ValueError):sequence.export_sequence(source,self.root,destination,provenance=changed)


if __name__=='__main__':unittest.main()
