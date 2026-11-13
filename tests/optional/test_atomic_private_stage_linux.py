"""Filesystem regressions for additive shared staging capabilities; stdlib only."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import atomic_publish as atomic


class PrivateStageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not atomic.private_stage_supported():
            raise RuntimeError('Dedicated Linux private-stage suite requires supported procfs/dirfd capabilities')

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def test_legacy_default_exclusive_rename_remains_unchanged(self):
        source=self.root/'source';source.write_bytes(b'original bytes')
        destination=self.root/'destination';atomic._rename_no_replace(source,destination)
        self.assertEqual(destination.read_bytes(),b'original bytes');self.assertFalse(source.exists())
        source.write_bytes(b'new source');existing=self.root/'existing';existing.mkdir()
        with self.assertRaises(FileExistsError):atomic._rename_no_replace(source,existing)
        self.assertEqual(source.read_bytes(),b'new source');self.assertTrue(existing.is_dir())

    def test_private_descriptor_precedes_io_and_cleans_owned_contents(self):
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent) as (stage,descriptor):
                self.assertEqual(stage,Path('/proc/self/fd')/str(descriptor))
                (stage/'frames').mkdir();(stage/'frames/owned.txt').write_bytes(b'temporary')
                (stage/'owned.txt').write_bytes(b'temporary')
                original=Path(os.readlink(stage));self.assertEqual(original.stat().st_mode&0o777,0o700)
            with self.assertRaises(OSError):os.fstat(descriptor)
        self.assertTrue(original.is_dir());self.assertEqual(list(original.iterdir()),[])

    def test_fresh_cleanup_stream_ignores_borrowed_directory_offset(self):
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent) as (stage,descriptor):
                (stage/'owned.txt').write_text('temporary')
                os.listdir(descriptor);os.listdir(descriptor)
                original=Path(os.readlink(stage))
            self.assertEqual(list(original.iterdir()),[])

    def test_outer_basename_symlink_replacement_never_cleans_foreign_tree(self):
        outside=self.root/'outside';outside.mkdir();(outside/'keep.txt').write_text('unrelated')
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent,prefix='.stage-') as (stage,descriptor):
                (stage/'owned.txt').write_text('temporary')
                original=Path(os.readlink(stage));moved=self.root/'moved-owned'
                original.rename(moved);original.symlink_to(outside,target_is_directory=True)
            self.assertEqual((outside/'keep.txt').read_text(),'unrelated')
            self.assertTrue(original.is_symlink());self.assertEqual(list(moved.iterdir()),[])

    def test_foreign_empty_outer_replacement_is_not_removed(self):
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent) as (stage,_):
                (stage/'owned.txt').write_text('temporary')
                original=Path(os.readlink(stage));original.rename(self.root/'moved')
                original.mkdir()
            self.assertTrue(original.is_dir());self.assertEqual(list(original.iterdir()),[])
            self.assertEqual(list((self.root/'moved').iterdir()),[])

    def test_cleanup_never_calls_rmdir_on_outer_basename(self):
        real_rmdir=os.rmdir
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent) as (stage,_):
                original=Path(os.readlink(stage));(stage/'owned.txt').write_text('temporary')
                def reject_outer(name,*args,**kwargs):
                    self.assertNotEqual(os.fspath(name),original.name,'outer stat/rmdir race reintroduced')
                    return real_rmdir(name,*args,**kwargs)
            # Check default path without relying on a lucky mutation timing.
            self.assertTrue(original.exists())
            with mock.patch.object(atomic.os,'rmdir',side_effect=reject_outer):
                with atomic.private_stage(parent) as (other,_):
                    other_name=Path(os.readlink(other)).name
                    (other/'file').write_text('temporary')
            self.assertTrue((self.root/other_name).exists())

    def test_file_publication_uses_pinned_stage_and_destination_parents(self):
        destination_parent=self.root/'destination';destination_parent.mkdir()
        outside=self.root/'outside';outside.mkdir()
        with atomic.pinned_directory(destination_parent) as parent:
            with atomic.private_stage(parent) as (stage,stage_fd):
                (stage/'partial').write_bytes(b'verified bytes')
                outer=Path(os.readlink(stage));outer.rename(destination_parent/'moved-stage')
                outer.mkdir();(outer/'partial').write_bytes(b'foreign bytes')
                destination_parent.rename(self.root/'pinned-destination')
                destination_parent.symlink_to(outside,target_is_directory=True)
                atomic._rename_no_replace('partial','final.bin',source_dir_fd=stage_fd,destination_dir_fd=parent)
        self.assertEqual((self.root/'pinned-destination/final.bin').read_bytes(),b'verified bytes')
        self.assertFalse((outside/'final.bin').exists())
        self.assertEqual((self.root/'pinned-destination'/outer.name/'partial').read_bytes(),b'foreign bytes')

    def test_directory_publication_uses_fresh_inner_payload(self):
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent) as (stage,stage_fd):
                (stage/'payload').mkdir();(stage/'payload/frame.png').write_bytes(b'verified-frame-fixture')
                outer=Path(os.readlink(stage));outer.rename(self.root/'moved')
                outer.mkdir();(outer/'payload').mkdir();(outer/'payload/foreign.txt').write_text('keep')
                atomic._rename_no_replace('payload','delivery',source_dir_fd=stage_fd,destination_dir_fd=parent)
        self.assertEqual((self.root/'delivery/frame.png').read_bytes(),b'verified-frame-fixture')
        self.assertEqual((outer/'payload/foreign.txt').read_text(),'keep')

    def test_worker_inherits_exact_descriptor_and_procfd_path(self):
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent) as (stage,descriptor):
                (stage/'input').write_bytes(b'original source')
                original=Path(os.readlink(stage));original.rename(self.root/'moved-stage')
                original.mkdir();(original/'input').write_bytes(b'foreign')
                result=subprocess.run([sys.executable,'-c',
                    'from pathlib import Path; import sys; p=Path(sys.argv[1]); assert (p/"input").read_bytes()==b"original source"; (p/"output").write_bytes(b"worker result")',str(stage)],
                    pass_fds=(descriptor,),capture_output=True,text=True,check=True)
                self.assertEqual((stage/'output').read_bytes(),b'worker result')
                self.assertEqual((original/'input').read_bytes(),b'foreign')

    def test_error_diagnostics_can_be_retained_and_descriptor_closes(self):
        with atomic.pinned_directory(self.root) as parent:
            with self.assertRaisesRegex(RuntimeError,'worker failed'):
                with atomic.private_stage(parent,cleanup_on_error=False) as (stage,descriptor):
                    (stage/'worker.log').write_text('actual failure diagnostics')
                    retained=Path(os.readlink(stage));raise RuntimeError('worker failed')
            self.assertEqual((retained/'worker.log').read_text(),'actual failure diagnostics')
            with self.assertRaises(OSError):os.fstat(descriptor)

    def test_normal_error_cleans_files_but_keeps_original_exception(self):
        with atomic.pinned_directory(self.root) as parent:
            with self.assertRaisesRegex(RuntimeError,'original error'):
                with atomic.private_stage(parent) as (stage,_):
                    (stage/'file').write_text('owned');retained=Path(os.readlink(stage));raise RuntimeError('original error')
            self.assertEqual(list(retained.iterdir()),[])
            with mock.patch.object(atomic,'_clear_private_directory',side_effect=PermissionError('cleanup failed')):
                with self.assertRaisesRegex(RuntimeError,'original'):
                    with atomic.private_stage(parent):raise RuntimeError('original')

    def test_retain_all_contents_and_owned_parent_fd_lifetime(self):
        with atomic.pinned_directory(self.root) as parent:
            duplicate=os.dup(parent)
            with atomic.private_stage(duplicate,cleanup=False) as (stage,_):
                os.close(duplicate);(stage/'file').write_text('keep');retained=Path(os.readlink(stage))
            self.assertEqual((retained/'file').read_text(),'keep')

    def test_recursive_cleanup_does_not_follow_nested_symlink(self):
        outside=self.root/'outside';outside.mkdir();(outside/'original').write_text('keep')
        with atomic.pinned_directory(self.root) as parent:
            with atomic.private_stage(parent) as (stage,_):
                (stage/'nested').mkdir();(stage/'nested/link').symlink_to(outside,target_is_directory=True)
                original=Path(os.readlink(stage))
            self.assertEqual((outside/'original').read_text(),'keep');self.assertEqual(list(original.iterdir()),[])

    def test_invalid_prefix_policy_descriptor_and_path_are_rejected(self):
        with atomic.pinned_directory(self.root) as parent:
            for prefix in ('../escape','a/b','x'*65,'a\\b','a\0b',None):
                with self.assertRaises(ValueError):
                    with atomic.private_stage(parent,prefix=prefix):pass
            with self.assertRaises(ValueError):
                with atomic.private_stage(parent,cleanup=1):pass
        file=self.root/'file';file.write_text('not directory');fd=os.open(file,os.O_RDONLY)
        try:
            with self.assertRaises(ValueError):
                with atomic.private_stage(fd):pass
        finally:os.close(fd)
        link=self.root/'link';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(OSError):
            with atomic.pinned_directory(link):pass

    def test_unsupported_platform_fails_before_creating_any_stage(self):
        with atomic.pinned_directory(self.root) as parent:
            before=set(self.root.iterdir())
            with mock.patch.object(atomic.sys,'platform','darwin'):
                with self.assertRaises(OSError) as caught:
                    with atomic.private_stage(parent):pass
                self.assertEqual(caught.exception.errno,errno.ENOTSUP)
            self.assertEqual(set(self.root.iterdir()),before)

    def test_lookdev_anchored_entrypoint_remains_compatible(self):
        (self.root/'part').write_bytes(b'lookdev-owned fixture')
        with atomic.pinned_directory(self.root) as fd:
            atomic.rename_no_replace_at(fd,'part',fd,'complete')
            self.assertEqual((self.root/'complete').read_bytes(),b'lookdev-owned fixture')
            (self.root/'part').write_bytes(b'new source')
            with self.assertRaises(FileExistsError):atomic.rename_no_replace_at(fd,'part',fd,'complete')
            with self.assertRaises(ValueError):atomic.rename_no_replace_at(fd,'../escape',fd,'another')
        self.assertEqual((self.root/'part').read_bytes(),b'new source')

    def test_creation_replacement_with_shared_permissions_is_rejected_before_io(self):
        real_mkdtemp=atomic.tempfile.mkdtemp;created=[]
        def replace_permissions(*args,**kwargs):
            name=real_mkdtemp(*args,**kwargs);Path(name).chmod(0o777);created.append(Path(name).resolve());return name
        with atomic.pinned_directory(self.root) as parent:
            with mock.patch.object(atomic.tempfile,'mkdtemp',side_effect=replace_permissions):
                with self.assertRaises(OSError) as caught:
                    with atomic.private_stage(parent):self.fail('untrusted creation yielded a writable path')
                self.assertEqual(caught.exception.errno,errno.EPERM)
        self.assertEqual(len(created),1);self.assertEqual(list(created[0].iterdir()),[])

    def test_creation_different_owner_is_rejected_before_io(self):
        real_fstat=os.fstat
        def foreign_owner(descriptor):
            value=real_fstat(descriptor)
            target=os.readlink('/proc/self/fd/'+str(descriptor))
            if Path(target).name.startswith('.owner-'):
                fields=list(value);fields[4]=os.geteuid()+1;return os.stat_result(fields)
            return value
        with atomic.pinned_directory(self.root) as parent:
            with mock.patch.object(atomic.os,'fstat',side_effect=foreign_owner):
                with self.assertRaises(OSError) as caught:
                    with atomic.private_stage(parent,prefix='.owner-'):self.fail('different-owner stage yielded')
                self.assertEqual(caught.exception.errno,errno.EPERM)
        self.assertTrue(all(not list(path.iterdir()) for path in self.root.iterdir()))

    def test_relative_rename_rejects_escaping_or_unpaired_descriptors(self):
        with atomic.pinned_directory(self.root) as parent:
            for source,destination in (('../escape','out'),('file','../out'),('/absolute','out'),('','out')):
                with self.assertRaises(ValueError):atomic._rename_no_replace(source,destination,source_dir_fd=parent,destination_dir_fd=parent)
            with self.assertRaises(ValueError):atomic._rename_no_replace('file','out',source_dir_fd=parent)


if __name__=='__main__':unittest.main()
