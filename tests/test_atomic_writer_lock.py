"""Cooperative storage locks, with explicit unsupported policy on Windows."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_publish as atomic


class StorageWriterLocks(unittest.TestCase):
    def unsupported(self):
        if os.name == 'posix': return False
        with self.assertRaises(OSError) as caught:
            with atomic.writer_lock_fd(0): self.fail('unsupported lock yielded')
        self.assertEqual(caught.exception.errno, errno.ENOTSUP)
        return True

    def test_peer_process_is_busy_then_same_inode_is_reused(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            command = [sys.executable, '-c',
                "import sys;sys.path.insert(0,sys.argv[1]);import atomic_publish as a\n"
                "try:\n with a.pinned_directory_fd(sys.argv[2]) as fd:\n"
                "  with a.writer_lock_fd(fd):pass\n"
                "except BlockingIOError:sys.exit(7)\n", str(Path(atomic.__file__).parent), str(root)]
            with atomic.pinned_directory_fd(root) as parent:
                with atomic.writer_lock_fd(parent):
                    before = (root / '.execution-retention.lock').stat()
                    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 7, result.stderr)
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                after = (root / '.execution-retention.lock').stat()
                self.assertEqual((before.st_dev,before.st_ino),(after.st_dev,after.st_ino))

    def test_lock_remains_bound_after_parent_namespace_substitution(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'; source.mkdir()
            with atomic.pinned_directory_fd(source) as parent:
                with atomic.writer_lock_fd(parent):
                    source.rename(root/'original');source.mkdir();(source/'foreign').write_bytes(b'original')
                    with self.assertRaises(BlockingIOError):
                        with atomic.writer_lock_fd(parent):self.fail('second writer entered')
                with atomic.writer_lock_fd(parent):pass
            self.assertEqual([p.name for p in source.iterdir()],['foreign'])
            self.assertEqual((source/'foreign').read_bytes(),b'original')

    def test_symlink_fifo_and_hardlink_are_rejected_without_source_mutation(self):
        if self.unsupported(): return
        for kind in ('symlink','fifo','hardlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();original=root/'original';original.write_bytes(b'user bytes')
                original.chmod(0o600);name=root/'.execution-retention.lock'
                if kind=='symlink':name.symlink_to(original)
                elif kind=='fifo':os.mkfifo(name,0o600)
                else:os.link(original,name)
                with atomic.pinned_directory_fd(root) as parent:
                    with self.assertRaises((OSError,ValueError)):
                        with atomic.writer_lock_fd(parent):self.fail('unsafe lock admitted')
                self.assertEqual(original.read_bytes(),b'user bytes')

    def test_shared_parent_and_nonprivate_existing_lock_fail_before_write(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();root.chmod(0o777)
            with atomic.pinned_directory_fd(root) as parent:
                with self.assertRaises(ValueError):
                    with atomic.writer_lock_fd(parent):self.fail('shared writer root admitted')
            self.assertEqual(list(root.iterdir()),[]);root.chmod(0o700)
            lock=root/'.execution-retention.lock';lock.write_bytes(b'original');lock.chmod(0o644)
            with atomic.pinned_directory_fd(root) as parent:
                with self.assertRaises(ValueError):
                    with atomic.writer_lock_fd(parent):self.fail('unsafe permissions admitted')
            self.assertEqual(lock.read_bytes(),b'original')

    def test_replacement_is_detected_and_foreign_entry_preserved(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            with atomic.pinned_directory_fd(root) as parent:
                with self.assertRaises(ValueError):
                    with atomic.writer_lock_fd(parent):
                        (root/'.execution-retention.lock').rename(root/'original-lock')
                        (root/'.execution-retention.lock').write_bytes(b'foreign')
            self.assertEqual((root/'.execution-retention.lock').read_bytes(),b'foreign')


if __name__=='__main__':unittest.main()
