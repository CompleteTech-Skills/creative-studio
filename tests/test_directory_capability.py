"""Actual stdlib/filesystem proofs for explicitly scoped directory descriptors."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextvars import copy_context

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import rig_errors
from rig_errors import (RigError,directory_capability,normalize_root,open_payload,safe_path)


SUPPORTED = (os.name == 'posix' and Path('/proc/self/fd').is_dir()
             and os.open in os.supports_dir_fd and hasattr(os,'O_NOFOLLOW'))


@unittest.skipUnless(SUPPORTED,'Explicit Linux directory descriptor capability unavailable.')
class DirectoryCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.stage=self.root/'stage';self.stage.mkdir()
        (self.stage/'payload').mkdir();(self.stage/'payload/data.bin').write_bytes(b'original')
        self.fd=os.open(self.stage,os.O_RDONLY|os.O_DIRECTORY)
        self.addCleanup(lambda:os.close(self.fd) if self.fd is not None else None)

    def read(self,root,name='data.bin',**kwargs):
        with open_payload(root,name,**kwargs) as (stream,info):return stream.read(),info

    def test_scoped_root_normalizer_and_borrowed_worker_alias(self):
        raw=Path('/proc/self/fd')/str(self.fd)
        self.assertEqual(normalize_root(raw),self.stage)
        with self.assertRaises(RigError):self.read(raw/'payload')
        with directory_capability(self.fd) as cap:
            self.assertNotEqual(cap.fd,self.fd)
            self.assertEqual(cap.identity,(self.stage.stat().st_dev,self.stage.stat().st_ino))
            self.assertEqual(normalize_root(raw/'payload'),cap.path/'payload')
            self.assertEqual(self.read(raw/'payload')[0],b'original')
            self.assertEqual(safe_path(cap.path/'payload','data.bin',exists=True),cap.path/'payload/data.bin')
        self.assertEqual(normalize_root(raw),self.stage)
        self.assertTrue(os.fstat(self.fd))

    def test_ancestor_and_stage_replacement_cannot_redirect_pinned_bytes(self):
        with directory_capability(self.fd) as cap:
            self.stage.rename(self.root/'kept')
            self.stage.mkdir();(self.stage/'payload').mkdir()
            (self.stage/'payload/data.bin').write_bytes(b'foreign')
            self.assertEqual(self.read(cap.path/'payload')[0],b'original')
            self.assertEqual((self.stage/'payload/data.bin').read_bytes(),b'foreign')
            self.assertEqual(normalize_root(cap.path),cap.path)

    def test_nested_capabilities_keep_outer_active(self):
        other=self.root/'other';other.mkdir();(other/'data.bin').write_bytes(b'other')
        otherfd=os.open(other,os.O_RDONLY|os.O_DIRECTORY)
        try:
            with directory_capability(self.fd) as outer:
                outer_path=outer.path
                with directory_capability(otherfd) as inner:
                    self.assertEqual(self.read(outer.path/'payload')[0],b'original')
                    self.assertEqual(self.read(inner.path)[0],b'other')
                    innerfd=inner.fd
                self.assertEqual(normalize_root(outer_path),outer_path)
                self.assertEqual(self.read(outer_path/'payload')[0],b'original')
                with self.assertRaises(OSError):os.fstat(innerfd)
        finally:os.close(otherfd)

    def test_closed_and_non_directory_fd_admission_never_closes_caller(self):
        filefd=os.open(self.stage/'payload/data.bin',os.O_RDONLY)
        try:
            with self.assertRaises(RigError):
                with directory_capability(filefd):self.fail('file descriptor admitted')
            self.assertTrue(os.fstat(filefd))
        finally:os.close(filefd)
        closed=os.dup(self.fd);os.close(closed)
        with self.assertRaises(RigError):
            with directory_capability(closed):self.fail('closed descriptor admitted')
        for invalid in (True,-1,'4',None):
            with self.subTest(invalid=invalid),self.assertRaises(RigError):
                with directory_capability(invalid):self.fail('invalid descriptor admitted')

    def test_context_owned_duplicate_closes_but_borrowed_remains(self):
        with directory_capability(self.fd) as cap:owned=cap.fd
        with self.assertRaises(OSError):os.fstat(owned)
        with self.assertRaises(RigError):_ = cap.path
        self.assertTrue(os.fstat(self.fd))

    def test_rejected_borrowed_fd_race_closes_unexposed_owned_duplicate(self):
        other=self.root/'other';other.mkdir()
        otherfd=os.open(other,os.O_RDONLY|os.O_DIRECTORY)
        borrowed=os.dup(self.fd);created=[];real_dup=os.dup
        def racing_dup(fd):
            os.dup2(otherfd,borrowed)
            owned=real_dup(fd);created.append(owned);return owned
        try:
            before=len(list(Path('/proc/self/fd').iterdir()))
            with patch.object(rig_errors.os,'dup',side_effect=racing_dup):
                with self.assertRaises(RigError) as caught:
                    with directory_capability(borrowed):self.fail('changed descriptor admitted')
            self.assertEqual(caught.exception.report['code'],'RIG_DIRECTORY_CHANGED')
            self.assertEqual(len(created),1)
            with self.assertRaises(OSError):os.fstat(created[0])
            self.assertEqual(len(list(Path('/proc/self/fd').iterdir())),before)
            self.assertEqual(os.fstat(borrowed).st_ino,os.fstat(otherfd).st_ino)
        finally:os.close(borrowed);os.close(otherfd)

    def test_exposed_owned_descriptor_reuse_never_closes_foreign_object(self):
        other=self.root/'other';other.mkdir()
        otherfd=os.open(other,os.O_RDONLY|os.O_DIRECTORY)
        reused=None
        try:
            with directory_capability(self.fd) as cap:
                reused=cap.fd
                os.close(reused);os.dup2(otherfd,reused)
                with self.assertRaises(RigError):_ = cap.path
            self.assertEqual(os.fstat(reused).st_ino,os.fstat(otherfd).st_ino)
        finally:
            if reused is not None:os.close(reused)
            os.close(otherfd)

    def test_ordinary_root_rename_after_admission_reports_structured_io_error(self):
        original=rig_errors.safe_path
        def admitting_then_renaming(root,name,**kwargs):
            result=original(root,name,**kwargs)
            self.stage.rename(self.root/'moved')
            return result
        with patch.object(rig_errors,'safe_path',side_effect=admitting_then_renaming):
            with self.assertRaises(RigError) as caught:self.read(self.stage/'payload')
        self.assertEqual(caught.exception.report['code'],'RIG_PAYLOAD_IO')

    def test_copied_context_cannot_resurrect_expired_capability(self):
        with directory_capability(self.fd) as cap:
            path=cap.path;copied=copy_context()
        with self.assertRaises(RigError):copied.run(normalize_root,path)

    def test_fifo_rejection_and_repeated_reads_do_not_leak_descriptors(self):
        os.mkfifo(self.stage/'payload/fifo')
        with directory_capability(self.fd) as cap:
            before=len(list(Path('/proc/self/fd').iterdir()))
            for _ in range(25):
                self.assertEqual(self.read(cap.path/'payload')[0],b'original')
                with self.assertRaises(RigError):self.read(cap.path/'payload','fifo')
            after=len(list(Path('/proc/self/fd').iterdir()))
            self.assertEqual(before,after)

    def test_borrowed_alias_closed_or_reused_fails_closed(self):
        borrowed=os.dup(self.fd);raw=Path('/proc/self/fd')/str(borrowed)
        other=self.root/'other';other.mkdir();(other/'data.bin').write_bytes(b'foreign')
        with directory_capability(borrowed) as cap:
            os.close(borrowed)
            with self.assertRaises(RigError):normalize_root(raw)
            otherfd=os.open(other,os.O_RDONLY|os.O_DIRECTORY)
            try:
                os.dup2(otherfd,borrowed)
                with self.assertRaises(RigError):self.read(raw)
                self.assertEqual(self.read(cap.path/'payload')[0],b'original')
            finally:
                if otherfd != borrowed:os.close(otherfd)
                os.close(borrowed)

    def test_unregistered_proc_anchor_is_not_ambiently_admitted(self):
        other=self.root/'other';other.mkdir();(other/'data.bin').write_bytes(b'foreign')
        otherfd=os.open(other,os.O_RDONLY|os.O_DIRECTORY)
        try:
            raw=Path('/proc/self/fd')/str(otherfd)
            with directory_capability(self.fd):
                self.assertEqual(normalize_root(raw),other)
                with self.assertRaises(RigError):self.read(raw)
        finally:os.close(otherfd)

    def test_descendant_symlinks_and_parent_traversal_rejected(self):
        outside=self.root/'outside';outside.mkdir();(outside/'data.bin').write_bytes(b'foreign')
        (self.stage/'link').symlink_to(outside,target_is_directory=True)
        (self.stage/'payload/link.bin').symlink_to(outside/'data.bin')
        with directory_capability(self.fd) as cap:
            for root,name in ((cap.path/'link','data.bin'),(cap.path/'payload','link.bin'),
                              (cap.path/'payload','../data.bin'),(cap.path/'..','data.bin')):
                with self.subTest(root=str(root),name=name),self.assertRaises(RigError):self.read(root,name)
            with self.assertRaises(RigError):normalize_root(cap.path/'link')
            with self.assertRaises(RigError):normalize_root(cap.path/'..')

    def test_root_identity_and_payload_mutation_checks_remain_active(self):
        with directory_capability(self.fd) as cap:
            info=(cap.path/'payload').stat();identity=(info.st_dev,info.st_ino)
            self.assertEqual(self.read(cap.path/'payload',root_identity=identity)[0],b'original')
            with self.assertRaises(RigError):self.read(cap.path/'payload',root_identity=(0,0))
            with self.assertRaises(RigError):
                with open_payload(cap.path/'payload','data.bin') as (stream,_):
                    self.assertEqual(stream.read(),b'original')
                    (cap.path/'payload/data.bin').write_bytes(b'changed')

    def test_worker_exec_requires_explicit_activation_and_rebases_inherited_path(self):
        code='''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from rig_errors import directory_capability,open_payload,RigError,normalize_root
fd=int(sys.argv[1]);raw=Path('/proc/self/fd')/str(fd)
try:
 with open_payload(raw/'payload','data.bin'):raise AssertionError('ambient capability leaked through exec')
except RigError:pass
with directory_capability(fd) as cap:
 assert normalize_root(raw/'payload')==cap.path/'payload'
 with open_payload(raw/'payload','data.bin') as (stream,_):assert stream.read()==b'original'
print('worker-capability-pass')
'''
        with directory_capability(self.fd) as cap:
            result=subprocess.run([sys.executable,'-c',code,str(cap.fd),str(SCRIPTS)],pass_fds=(cap.fd,),
                                  capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('worker-capability-pass',result.stdout)


if __name__=='__main__':unittest.main()
