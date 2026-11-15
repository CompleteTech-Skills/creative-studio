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
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import silent_sequence as sequence

import render3d_verify
import png
import OpenEXR
import numpy as np


class SequenceImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from rig_errors import directory_capability
        if not sequence.publication_supported():raise RuntimeError('Linux sequence suite requires shared private staging')

    def test_exclusive_write_preserves_preexisting_foreign_partial(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);partial=root/'.plan.json.partial'
            partial.write_bytes(b'preexisting unrelated file')
            with self.assertRaises(FileExistsError):sequence._write(root,'plan.json',b'new plan')
            self.assertEqual(partial.read_bytes(),b'preexisting unrelated file')
            self.assertFalse((root/'plan.json').exists())

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

    def test_rgb_png_eight_and_sixteen_bit_sources_preserve_channels_and_bytes(self):
        for bits in (8,16):
            with self.subTest(bits=bits):
                maximum=2**bits-1
                path=self.root/f'rgb-{bits}.png'
                row=[maximum,0,0,0,maximum,0,0,0,maximum,maximum//2,maximum//2,maximum//2]
                with path.open('wb') as stream:
                    png.Writer(4,2,alpha=False,greyscale=False,bitdepth=bits).write(stream,[row,row])
                output={'format':'png','bit_depth':bits,'passes':['combined'],
                        'alpha':'opaque','color_space':'srgb'}
                source=sequence.build_source(self.root,[path.name],fps=self.fps,
                                             output=output,identity=self.identity)
                destination=self.root/f'rgb-output-{bits}'
                report=sequence.export_sequence(source,self.root,destination,provenance=self.identity)
                self.assertEqual(sequence.verify_sequence(destination),report)
                self.assertEqual(set(report['decoded'][0]['channels']),set('RGB'))
                self.assertEqual(report['decoded'][0]['bit_depth'],bits)
                self.assertEqual((destination/'frames/000000.png').read_bytes(),path.read_bytes())

    def test_foreign_staging_directory_is_not_modified_even_with_lock_file(self):
        source=self.source(count=1);foreign=self.root/'foreign-stage';foreign.mkdir()
        (foreign/'user.txt').write_text('unrelated user data')
        with self.assertRaises(ValueError):
            sequence.export_sequence(source,self.root,self.root/'output',provenance=self.identity,stage_name=foreign.name)
        self.assertEqual(sorted(p.name for p in foreign.iterdir()),['user.txt'])
        self.assertEqual((foreign/'user.txt').read_text(),'unrelated user data')

    def test_progress_counts_verified_reuse_after_owned_partial_recovery(self):
        source=self.source();destination=self.root/'progress-resume';checks=[0]
        def cancel_after_first():
            checks[0]+=1
            return checks[0]==2
        with self.assertRaisesRegex(ValueError,'cancelled'):
            sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                     cancel=cancel_after_first)
        stage=self.root/'.progress-resume.sequence-stage'
        original=stage/'frames/000000.png';mtime=original.stat().st_mtime_ns
        partial=stage/'frames/.000001.png.partial';partial.write_bytes(b'interrupted owned write')
        events=[]
        report=sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                        progress=events.append)
        self.assertEqual(events,[{'stage':'preparing','action':action,'frame':index+1,'total_frames':3}
                                  for index,action in enumerate(('reused','copied','copied'))])
        self.assertEqual(original.stat().st_mtime_ns,mtime)
        self.assertFalse(partial.exists())
        self.assertEqual(sequence.verify_sequence(destination),report)

    def test_matching_bootstrap_partial_recovers_before_progress_is_counted(self):
        source=self.source();destination=self.root/'bootstrap-progress'
        stage=self.root/'.bootstrap-progress.sequence-stage';stage.mkdir(mode=0o700)
        (stage/'lock').write_bytes(b'')
        partial=stage/'.plan.json.partial'
        partial.write_bytes(sequence._json_bytes({'source':source,'provenance':self.identity}))
        events=[]
        report=sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                        progress=events.append)
        self.assertEqual([event['action'] for event in events],['copied']*3)
        self.assertFalse(partial.exists())
        self.assertEqual(sequence.verify_sequence(destination),report)

    def test_observer_failure_retains_verified_cache_for_counted_resume(self):
        source=self.source();destination=self.root/'observer-failure'
        def stop_after_frame(event):raise RuntimeError('observer stopped controller')
        with self.assertRaisesRegex(RuntimeError,'observer stopped controller'):
            sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                     progress=stop_after_frame)
        self.assertFalse(destination.exists())
        original=self.root/'.observer-failure.sequence-stage/frames/000000.png'
        mtime=original.stat().st_mtime_ns;events=[]
        sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                 progress=events.append)
        self.assertEqual([event['action'] for event in events],['reused','copied','copied'])
        self.assertEqual(original.stat().st_mtime_ns,mtime)

    def test_progress_observer_runs_with_exclusive_cache_ownership(self):
        source=self.source(count=1);destination=self.root/'observer-lock';events=[]
        def inspect_lock(event):
            events.append(event)
            with self.assertRaisesRegex(ValueError,'already owned'):
                sequence.export_sequence(source,self.root,destination,provenance=self.identity)
        sequence.export_sequence(source,self.root,destination,provenance=self.identity,progress=inspect_lock)
        self.assertEqual([event['action'] for event in events],['copied'])

    def test_unknown_bootstrap_partial_is_preserved_without_progress(self):
        source=self.source(count=1);destination=self.root/'unknown-bootstrap'
        stage=self.root/'.unknown-bootstrap.sequence-stage';stage.mkdir(mode=0o700)
        (stage/'lock').write_bytes(b'')
        partial=stage/'.plan.json.partial';partial.write_bytes(b'unknown incomplete plan')
        events=[]
        with self.assertRaisesRegex(ValueError,'does not match'):
            sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                     progress=events.append)
        self.assertEqual(partial.read_bytes(),b'unknown incomplete plan')
        self.assertEqual(events,[]);self.assertFalse(destination.exists())

    def test_deadline_expiring_at_private_publication_entry_keeps_cache(self):
        source=self.source(count=1);destination=self.root/'deadline-publication'
        clock=[100.0];original=sequence._publish_cache
        def delayed_publication(*args,**kwargs):
            self.assertEqual(kwargs['deadline'],101.0)
            clock[0]=102.0  # storage delay after the controller's final checkpoint
            return original(*args,**kwargs)
        with mock.patch.object(sequence.time,'monotonic',side_effect=lambda:clock[0]), \
                mock.patch.object(sequence,'_publish_cache',side_effect=delayed_publication):
            with self.assertRaisesRegex(ValueError,'deadline exceeded'):
                sequence.export_sequence(source,self.root,destination,provenance=self.identity,deadline=101.0)
        self.assertFalse(destination.exists())
        self.assertEqual((self.root/'.deadline-publication.sequence-stage/frames/000000.png').read_bytes(),
                         (self.root/source['frames'][0]['source_path']).read_bytes())
        events=[]
        sequence.export_sequence(source,self.root,destination,provenance=self.identity,progress=events.append)
        self.assertEqual([event['action'] for event in events],['reused'])

    def test_real_wall_deadline_rejects_delayed_publication_of_actual_png(self):
        source=self.source(count=1);destination=self.root/'real-wall-deadline';entered=[]
        original=sequence._publish_cache
        def slow_publication(*args,**kwargs):
            entered.append(True)
            sequence.time.sleep(1.2)
            return original(*args,**kwargs)
        with mock.patch.object(sequence,'_publish_cache',side_effect=slow_publication):
            with self.assertRaisesRegex(ValueError,'deadline exceeded'):
                sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                         deadline=sequence.time.monotonic()+1)
        self.assertEqual(entered,[True]);self.assertFalse(destination.exists())

    def test_deadline_expiring_during_private_copy_prevents_publication(self):
        source=self.source(count=1);destination=self.root/'deadline-copy';clock=[100.0];copied=[]
        original=sequence._write
        def slow_write(root,name,data):
            result=original(root,name,data)
            if name.startswith('frames/') and '.sequence-publish-' in os.readlink(root):
                copied.append(name);clock[0]=102.0
            return result
        with mock.patch.object(sequence.time,'monotonic',side_effect=lambda:clock[0]), \
                mock.patch.object(sequence,'_write',side_effect=slow_write):
            with self.assertRaisesRegex(ValueError,'deadline exceeded'):
                sequence.export_sequence(source,self.root,destination,provenance=self.identity,deadline=101.0)
        self.assertEqual(copied,['frames/000000.png']);self.assertFalse(destination.exists())

    def test_deadline_checks_after_decode_for_export_and_offline_verification(self):
        source=self.source(count=1);original=sequence._verify_frame
        for offline in (False,True):
            with self.subTest(offline=offline):
                destination=self.root/f'deadline-decode-{offline}';clock=[100.0];decoded=[]
                if offline:sequence.export_sequence(source,self.root,destination,provenance=self.identity)
                def slow_decode(root,*args):
                    result=original(root,*args)
                    if offline or '.sequence-publish-' in os.readlink(root):
                        decoded.append(True);clock[0]=102.0
                    return result
                with mock.patch.object(sequence.time,'monotonic',side_effect=lambda:clock[0]), \
                        mock.patch.object(sequence,'_verify_frame',side_effect=slow_decode):
                    with self.assertRaisesRegex(ValueError,'deadline exceeded'):
                        if offline:sequence.verify_sequence(destination,deadline=101.0)
                        else:sequence.export_sequence(source,self.root,destination,provenance=self.identity,deadline=101.0)
                self.assertEqual(decoded,[True]);self.assertEqual(destination.exists(),offline)

    def test_deadline_fence_after_private_fsync_precedes_exclusive_commit(self):
        source=self.source(count=1);destination=self.root/'deadline-commit';clock=[100.0];synced=[]
        original=sequence.os.fsync
        def slow_sync(descriptor):
            result=original(descriptor)
            target=os.readlink(f'/proc/self/fd/{descriptor}')
            if '.sequence-publish-' in target and target.endswith('/payload'):
                synced.append(True);clock[0]=102.0
            return result
        with mock.patch.object(sequence.time,'monotonic',side_effect=lambda:clock[0]), \
                mock.patch.object(sequence.os,'fsync',side_effect=slow_sync):
            with self.assertRaisesRegex(ValueError,'deadline exceeded'):
                sequence.export_sequence(source,self.root,destination,provenance=self.identity,deadline=101.0)
        self.assertTrue(synced);self.assertFalse(destination.exists())

    def test_private_copy_cancellation_and_callback_mutation_are_checked_before_commit(self):
        source=self.source(count=1)
        for mutate in (False,True):
            with self.subTest(mutate=mutate):
                destination=self.root/f'private-callback-{mutate}';observed=[]
                def callback():
                    frames=[path for path in self.root.rglob('000000.png')
                            if any(part.startswith('.sequence-publish-') for part in path.parts)]
                    if frames:
                        observed.append(True)
                        if mutate:frames[0].write_bytes(b'changed after private copy')
                        return not mutate
                    return False
                with self.assertRaisesRegex(ValueError,'checksum' if mutate else 'cancelled'):
                    sequence.export_sequence(source,self.root,destination,provenance=self.identity,cancel=callback)
                self.assertTrue(observed);self.assertFalse(destination.exists())

    def test_observer_cache_mutation_is_rejected_before_publication(self):
        source=self.source();destination=self.root/'observer-mutation'
        def mutate_after_last(event):
            if event['frame']==event['total_frames']:
                (self.root/'.observer-mutation.sequence-stage/frames/000000.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):
            sequence.export_sequence(source,self.root,destination,provenance=self.identity,
                                     progress=mutate_after_last)
        self.assertFalse(destination.exists())
        for frame in source['frames']:
            self.assertEqual(hashlib.sha256((self.root/frame['source_path']).read_bytes()).hexdigest(),frame['sha256'])

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

    def test_nested_destination_descriptor_pins_publication_across_parent_rename(self):
        from rig_errors import directory_capability
        source=self.source(count=1)
        parent=self.root/'nested';parent.mkdir()
        descriptor=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        swapped=[False]
        def rename_parent():
            if not swapped[0]:
                swapped[0]=True
                parent.rename(self.root/'original-parent');parent.mkdir()
                (parent/'foreign.txt').write_text('unrelated replacement')
            return False
        try:
            report=sequence.export_sequence(source,self.root,'verified',provenance=self.identity,
                destination_dir_fd=descriptor,cancel=rename_parent)
            with directory_capability(descriptor) as authority:
                self.assertEqual(sequence.verify_sequence(authority.path/'verified'),report)
            self.assertTrue(os.fstat(descriptor))
            self.assertTrue((self.root/'original-parent/verified/frames/000000.png').is_file())
            self.assertEqual([path.name for path in parent.iterdir()],['foreign.txt'])
            self.assertEqual((parent/'foreign.txt').read_text(),'unrelated replacement')
        finally:os.close(descriptor)

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
