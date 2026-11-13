import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import model3d_jobs as jobs
from model3d_job_cli import native_primitive
from rig_errors import RigError, atomic_json, read_json, sha256


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.job = self.root/'job'
        self.request = dict(engine='test', parameters={})

    def create(self, **kwargs):
        return jobs.create_job(self.job, self.request, **kwargs)

    def handler(self, context):
        context.check(); (context.output/'mesh.bin').write_bytes(b'actual-output')

    def test_real_native_primitive_archive_restore_and_cache(self):
        request = dict(engine='native-primitive', parameters=dict(kind='box', parameters={}, name='box', normal_mode='smooth', format='glb'))
        jobs.create_job(self.job, request)
        result = jobs.run_job(self.job, native_primitive, handler_version='native-test1')
        output = self.job/result['current_revision']/'output/model/model.glb'
        self.assertTrue(output.exists()); original = sha256(output)
        with patch('model3d_jobs._inventory', wraps=jobs._inventory):
            cached = jobs.run_job(self.job, lambda _:self.fail('cached work reran'), handler_version='native-test1')
        self.assertEqual(cached['attempts'], 1)
        archive = self.root/'model.zip'; jobs.archive_job(self.job, archive)
        restored = jobs.restore_job(archive, self.root/'restored')
        self.assertEqual(restored, jobs.status(self.job))
        self.assertEqual(sha256(self.root/'restored'/result['current_revision']/'output/model/model.glb'), original)

    def test_input_capture_and_tamper_fail_before_callback(self):
        source = self.root/'source.bin'; source.write_bytes(b'original')
        self.create(inputs={'source.bin':source}); source.write_bytes(b'changed')
        self.assertEqual((self.job/'inputs/source.bin').read_bytes(), b'original')
        (self.job/'inputs/source.bin').write_bytes(b'tampered')
        with self.assertRaises(RigError):jobs.run_job(self.job, lambda _:self.fail('read corrupt source'), handler_version='v1')
        self.assertEqual(read_json(self.job/'job.json')['attempts'], 0)

    def test_failed_revision_preserves_active_payload_and_bounded_attempts(self):
        self.create(budgets={'attempts':2})
        first = jobs.run_job(self.job, self.handler, handler_version='v1')
        active = first['current_revision']; before = (self.job/active/'output/mesh.bin').read_bytes()
        def fail(context):
            (context.output/'incomplete').write_text('partial')
            raise RigError('MODEL3D_TEST_FAILURE', 'planned')
        with self.assertRaises(RigError):jobs.run_job(self.job, fail, handler_version='v2', retry=True)
        current = jobs.status(self.job)
        self.assertEqual(current['current_revision'], active)
        self.assertEqual((self.job/active/'output/mesh.bin').read_bytes(), before)
        self.assertEqual(current['status'], 'failed')
        with self.assertRaises(RigError):jobs.run_job(self.job, self.handler, handler_version='v1', retry=True)
        self.assertEqual(jobs.status(self.job)['attempts'], 2)

    def test_cancellation_and_timeout_never_publish_partial_outputs(self):
        self.create(budgets={'runtime_seconds':.01})
        def cancelled(context):
            jobs.cancel_job(self.job); (context.output/'partial').write_text('x'); context.check()
        with self.assertRaises(RigError) as caught:jobs.run_job(self.job, cancelled, handler_version='v1')
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_JOB_CANCELLED')
        self.assertEqual(jobs.status(self.job)['status'], 'cancelled')
        jobs.clear_cancellation(self.job)
        def timeout(context):
            time.sleep(.02); context.check()
        with self.assertRaises(RigError) as caught:jobs.run_job(self.job, timeout, handler_version='v1', retry=True)
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_JOB_TIMEOUT')
        self.assertIsNone(jobs.status(self.job)['current_revision'])

    def test_process_exit_after_checkpoint_recovers_without_replay(self):
        self.create()
        scripts = Path(jobs.__file__).parent
        code = '''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
import model3d_jobs as j
original=j._rename_no_replace
def crash(stage,target):
 original(stage,target)
 os._exit(74)
j._rename_no_replace=crash
def handler(c):(c.output/'mesh.bin').write_bytes(b'checkpoint')
j.run_job(sys.argv[1],handler,handler_version='v1')
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.job), str(scripts)], capture_output=True)
        self.assertEqual(result.returncode, 74, result.stderr.decode())
        self.assertEqual(read_json(self.job/'job.json')['status'], 'running')
        recovered = jobs.run_job(self.job, lambda _:self.fail('replayed published work'), handler_version='v1')
        self.assertEqual(recovered['status'], 'completed'); self.assertEqual(recovered['attempts'], 1)

    def test_process_exit_before_checkpoint_requires_explicit_retry(self):
        self.create(); scripts=Path(jobs.__file__).parent
        code="import os,sys;sys.path.insert(0,sys.argv[2]);import model3d_jobs as j;j.run_job(sys.argv[1],lambda c:os._exit(75),handler_version='v1')"
        result=subprocess.run([sys.executable,'-c',code,str(self.job),str(scripts)],capture_output=True)
        self.assertEqual(result.returncode,75)
        with self.assertRaises(RigError):jobs.run_job(self.job,self.handler,handler_version='v1')
        self.assertEqual(jobs.status(self.job)['status'],'interrupted')
        self.assertEqual(jobs.run_job(self.job,self.handler,handler_version='v1',retry=True)['attempts'],2)

    def test_transient_final_manifest_failure_keeps_published_checkpoint_recoverable(self):
        self.create(); original_save=jobs._save
        for after_write in (False,True):
            # Exercise both a write failure and a caller losing its success reply.
            if after_write:
                self.job=self.root/'after-write';self.create()
            failed=False
            def fail_once(root,value):
                nonlocal failed
                if value['status']=='completed' and not failed:
                    failed=True
                    if after_write:original_save(root,value)
                    raise OSError('transient final manifest failure')
                original_save(root,value)
            with self.subTest(after_write=after_write):
                with patch('model3d_jobs._save',side_effect=fail_once):
                    with self.assertRaises(OSError):jobs.run_job(self.job,self.handler,handler_version='v1')
                interrupted=jobs.status(self.job)
                self.assertEqual(interrupted['status'],'running')
                self.assertIsNotNone(interrupted['active_attempt'])
                self.assertEqual(interrupted['revisions'],[])
                recovered=jobs.run_job(self.job,lambda _:self.fail('published work replayed'),handler_version='v1')
                self.assertEqual(recovered['status'],'completed')
                self.assertEqual(recovered['attempts'],1)
                self.assertEqual(len(recovered['revisions']),1)

    def test_writer_lock_is_not_reentrant_and_batch_isolation(self):
        self.create(); second=self.root/'second'; jobs.create_job(second,self.request)
        def competing(context):
            with self.assertRaises(RigError) as caught:jobs.run_job(context.root,self.handler,handler_version='v1')
            self.assertEqual(caught.exception.report['code'],'RIG_BUSY'); self.handler(context)
        jobs.run_job(self.job,competing,handler_version='v1')
        def bad(context):
            if context.root == second:raise RigError('MODEL3D_TEST','bad sibling')
            self.handler(context)
        report=jobs.run_batch([self.job,second],bad,handler_version='v1')
        self.assertEqual(report['succeeded'],1);self.assertEqual(jobs.status(self.job)['status'],'completed')

    def test_batch_continues_after_ordinary_handler_exception(self):
        self.create(); second=self.root/'second'; jobs.create_job(second,self.request)
        def handler(context):
            if context.root == self.job:raise ValueError('bad model parameters')
            self.handler(context)
        report=jobs.run_batch([self.job,second],handler,handler_version='v1')
        self.assertEqual(report['succeeded'],1)
        self.assertEqual(report['results'][0]['error']['code'],'MODEL3D_JOB_HANDLER')
        self.assertEqual(jobs.status(self.job)['status'],'failed')
        self.assertEqual(jobs.status(second)['status'],'completed')
        self.assertEqual(jobs.status(second)['attempts'],1)

    def test_path_and_size_budgets_preserve_destination(self):
        with self.assertRaises(RigError):self.create(inputs={'../bad':self.root/'missing'})
        self.assertFalse(self.job.exists())
        self.create(budgets={'output_bytes':3})
        with self.assertRaises(RigError):jobs.run_job(self.job,self.handler,handler_version='v1')
        self.assertFalse((self.job/'revisions').exists())
        with self.assertRaises(RigError):jobs.restore_job(self.root/'missing.zip',self.job)

    @unittest.skipIf(os.name=='nt','symlink privilege is environment dependent')
    def test_output_symlink_rejected_and_archive_race_preserved(self):
        self.create()
        def symlink(context):(context.output/'escape').symlink_to(self.root)
        with self.assertRaises(RigError):jobs.run_job(self.job,symlink,handler_version='v1')
        jobs.run_job(self.job,self.handler,handler_version='v1',retry=True)
        destination=self.root/'archive.zip'
        real=jobs._rename_no_replace
        def race(stage,target):
            Path(target).write_bytes(b'competing writer');return real(stage,target)
        with patch('model3d_jobs._rename_no_replace',side_effect=race):
            with self.assertRaises(FileExistsError):jobs.archive_job(self.job,destination)
        self.assertEqual(destination.read_bytes(),b'competing writer')

    def test_archive_traversal_duplicate_tamper_and_restore_rejection(self):
        self.create();jobs.run_job(self.job,self.handler,handler_version='v1')
        good=self.root/'good.zip';jobs.archive_job(self.job,good)
        with zipfile.ZipFile(good) as archive:items={i.filename:archive.read(i.filename) for i in archive.infolist()}
        cases=[{**items,'../escape':b'x'},{**items,'job.json':b'tampered'}]
        for index,contents in enumerate(cases):
            target=self.root/f'bad{index}.zip'
            with zipfile.ZipFile(target,'w') as archive:
                for name,data in contents.items():archive.writestr(name,data)
            destination=self.root/f'bad{index}'
            with self.assertRaises(RigError):jobs.restore_job(target,destination)
            self.assertFalse(destination.exists())
        with self.assertRaises(RigError):jobs.archive_job(self.job,good)

    def test_corrupt_checkpoint_and_unsupported_stored_schema_are_structured(self):
        self.create(); result=jobs.run_job(self.job,self.handler,handler_version='v1')
        manifest=read_json(self.job/'job.json')
        for patch_ in ({'schema_version':True},{'status':{}},{'budgets':{}},
                       {'current_revision':{}},{'active_attempt':{}},{'revisions':[{}]},
                       {'current_revision':None},{'attempts':0}):
            atomic_json(self.job/'job.json',{**manifest,**patch_})
            with self.subTest(patch=patch_):
                with self.assertRaises(RigError):jobs.status(self.job)
        atomic_json(self.job/'job.json',manifest)
        (self.job/result['current_revision']/'output/mesh.bin').write_bytes(b'changed')
        with self.assertRaises(RigError) as caught:
            jobs.run_job(self.job,lambda _:self.fail('corrupt output reused'),handler_version='v1')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_JOB_CHECKPOINT')

    def test_cancel_queued_job_and_bounded_zip_directory(self):
        self.create();jobs.cancel_job(self.job)
        with self.assertRaises(RigError):jobs.run_job(self.job,self.handler,handler_version='v1')
        self.assertEqual(jobs.status(self.job)['status'],'cancelled')
        self.assertEqual(jobs.status(self.job)['attempts'],0)
        jobs.clear_cancellation(self.job)
        jobs.run_job(self.job,self.handler,handler_version='v1',retry=True)
        archive=self.root/'good.zip';jobs.archive_job(self.job,archive)
        encoded=bytearray(archive.read_bytes());offset=encoded.rfind(b'PK\x05\x06')
        # A hostile central-directory size is rejected before ZipFile allocation.
        encoded[offset+12:offset+16]=(9*1024**2).to_bytes(4,'little')
        hostile=self.root/'central-bomb.zip';hostile.write_bytes(encoded)
        with self.assertRaises(RigError) as caught:jobs.restore_job(hostile,self.root/'bad')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_ARCHIVE_LIMIT')
        self.assertFalse((self.root/'bad').exists())
        # ZIP64's locator would override the apparently small classic EOCD.
        original=archive.read_bytes();end=original.rfind(b'PK\x05\x06')
        _,_,_,count,_,size,start,_=struct.unpack_from('<4s4H2LH',original,end)
        end64=struct.pack('<4sQ2H2L4Q',b'PK\x06\x06',44,45,45,0,0,count,count,size,start)
        locator=struct.pack('<4sLQL',b'PK\x06\x07',0,end,1)
        classic=bytearray(original[end:]);classic[8:12]=struct.pack('<2H',2,2)
        classic[12:20]=struct.pack('<2L',1,0)
        hostile64=self.root/'zip64-override.zip';hostile64.write_bytes(original[:end]+end64+locator+classic)
        with patch('model3d_jobs.zipfile.ZipFile',side_effect=AssertionError('allocated before preflight')):
            with self.assertRaises(RigError) as caught:jobs.restore_job(hostile64,self.root/'bad64')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_ARCHIVE_LIMIT')
        self.assertFalse((self.root/'bad64').exists())
        # A maximal comment moves the true locator before the EOCD tail window.
        long_comment=bytearray(hostile64.read_bytes());end=long_comment.rfind(b'PK\x05\x06')
        long_comment[end+20:end+22]=(65535).to_bytes(2,'little')
        long_comment += b'a'*65535
        hostile_long=self.root/'zip64-long-comment.zip';hostile_long.write_bytes(long_comment)
        with patch('model3d_jobs.zipfile.ZipFile',side_effect=AssertionError('allocated before preflight')):
            with self.assertRaises(RigError) as caught:jobs.restore_job(hostile_long,self.root/'bad-long')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_ARCHIVE_LIMIT')
        self.assertFalse((self.root/'bad-long').exists())

    def test_unicode_normalized_input_output_and_directory_aliases_rejected(self):
        source=self.root/'source.bin';source.write_bytes(b'original')
        composed='caf\u00e9.txt';decomposed='cafe\u0301.txt'
        with self.assertRaises(RigError):self.create(inputs={composed:source,decomposed:source})
        self.assertFalse(self.job.exists())
        with self.assertRaises(RigError):self.create(inputs={'caf\u00e9/a.txt':source,'cafe\u0301/b.txt':source})
        self.assertFalse(self.job.exists())
        self.create()
        # A filesystem which itself rejects aliases is safe too; use a direct
        # synthetic directory inventory for host-independent alias enumeration.
        class Entry:
            def __init__(self,path):self.path=str(path)
            def is_symlink(self):return False
            def is_dir(self,follow_symlinks=False):return True
        from contextlib import nullcontext
        entries=[Entry(self.job/'caf\u00e9'),Entry(self.job/'cafe\u0301')]
        with patch('model3d_jobs.os.scandir',return_value=nullcontext(entries)):
            with self.assertRaises(RigError) as caught:jobs._inventory(self.job,files=10,bytes_=100)
        self.assertEqual(caught.exception.report['code'],'MODEL3D_JOB_PATH')
        def handler(context):
            # Works as a real two-file probe on normalization-sensitive Linux.
            (context.output/composed).write_bytes(b'first')
            try:
                with (context.output/decomposed).open('xb') as out:out.write(b'second')
            except FileExistsError:
                raise RigError('MODEL3D_JOB_PATH','Filesystem rejected the normalized alias.')
        with self.assertRaises(RigError):jobs.run_job(self.job,handler,handler_version='unicode-v1')
        self.assertIsNone(jobs.status(self.job)['current_revision'])

    def test_hostile_unicode_aliased_zip_rejected_before_restoration(self):
        self.create();jobs.run_job(self.job,self.handler,handler_version='v1')
        good=self.root/'good.zip';jobs.archive_job(self.job,good)
        with zipfile.ZipFile(good) as archive:items={i.filename:archive.read(i.filename) for i in archive.infolist()}
        items.update({'inputs/caf\u00e9.txt':b'first','inputs/cafe\u0301.txt':b'second'})
        hostile=self.root/'unicode-alias.zip'
        with zipfile.ZipFile(hostile,'w') as archive:
            for name,data in items.items():archive.writestr(name,data)
        with self.assertRaises(RigError) as caught:jobs.restore_job(hostile,self.root/'bad')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_ARCHIVE_LIMIT')
        self.assertFalse((self.root/'bad').exists())
        self.assertFalse(jobs._unique_names(['inputs/caf\u00e9/a.txt','inputs/cafe\u0301/b.txt']))
        self.assertTrue(jobs._unique_names(['inputs/caf\u00e9/a.txt','inputs/caf\u00e9/b.txt']))

    def test_empty_checkpoint_cannot_be_cached_or_recovered_as_success(self):
        self.create();completed=jobs.run_job(self.job,self.handler,handler_version='v1')
        revision=completed['current_revision'];folder=self.job/revision
        (folder/'output/mesh.bin').unlink()
        receipt=read_json(folder/'receipt.json');receipt['files']=[];atomic_json(folder/'receipt.json',receipt)
        with self.assertRaises(RigError) as caught:
            jobs.run_job(self.job,lambda _:self.fail('empty checkpoint replayed'),handler_version='v1')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_JOB_CHECKPOINT')
        pending=dict(completed,status='running',current_revision=None,revisions=[],
                     active_attempt=dict(token=revision.rsplit('-',1)[1],revision=revision,handler_version='v1'))
        atomic_json(self.job/'job.json',pending)
        with self.assertRaises(RigError) as caught:
            jobs.run_job(self.job,lambda _:self.fail('empty checkpoint recovered'),handler_version='v1')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_JOB_CHECKPOINT')
        self.assertIsNone(read_json(self.job/'job.json')['current_revision'])


if __name__=='__main__':unittest.main()
