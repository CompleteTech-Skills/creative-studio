"""Actual bounded-file publication; native desktop gates must execute it."""
import errno
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_publish as atomic


class VerifiedFilePublication(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = b'actual archive bytes\0' * 10000
        self.source = self.root / 'source'
        self.source.write_bytes(self.data)
        self.fd = os.open(self.source, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        self.addCleanup(os.close, self.fd)
        self.output = self.root / 'archive.zip'
        self.digest = hashlib.sha256(self.data).hexdigest()

    def publish(self, **overrides):
        options = dict(expected_sha256=self.digest, expected_size=len(self.data), max_bytes=1048576)
        options.update(overrides)
        return atomic.publish_verified_file(self.fd, self.output, **options)

    def assert_no_output(self):
        self.assertFalse(self.output.exists())
        for entry in self.root.iterdir():
            if entry.name.startswith('.file-'):
                self.assertEqual(list(entry.iterdir()), [])

    def test_actual_file_is_exact_and_source_fd_and_cursor_are_preserved(self):
        os.lseek(self.fd, 7, os.SEEK_SET)
        self.assertEqual(self.publish(), {'sha256': self.digest, 'byte_size': len(self.data)})
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 7)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_empty_regular_file_is_published(self):
        empty = self.root / 'empty'; empty.touch()
        fd = os.open(empty, os.O_RDONLY)
        try:
            result = atomic.publish_verified_file(fd, self.output,
                expected_sha256=hashlib.sha256(b'').hexdigest(), expected_size=0, max_bytes=0)
        finally: os.close(fd)
        self.assertEqual(result['byte_size'], 0)
        self.assertEqual(self.output.read_bytes(), b'')

    def test_bad_digest_never_publishes_and_cleans_owned_bytes(self):
        with self.assertRaises(ValueError): self.publish(expected_sha256='0' * 64)
        self.assert_no_output()





    def test_wrong_size_and_bound_fail_without_publication(self):
        for options in ({'expected_size': len(self.data)-1}, {'expected_size': len(self.data)+1},
                        {'max_bytes': len(self.data)-1}, {'expected_size': True},
                        {'max_bytes': 2**40+1}, {'max_bytes': -1},
                        {'expected_sha256': self.digest.upper()}):
            with self.subTest(options=options), self.assertRaises(ValueError): self.publish(**options)
            self.assert_no_output()

    def test_existing_destination_preserves_exact_original(self):
        self.output.write_bytes(b'unrelated original')
        with self.assertRaises(OSError): self.publish()
        self.assertEqual(self.output.read_bytes(), b'unrelated original')

    def test_existing_directory_is_never_replaced(self):
        self.output.mkdir(); (self.output / 'foreign').write_bytes(b'original')
        with self.assertRaises(OSError): self.publish()
        self.assertEqual((self.output / 'foreign').read_bytes(), b'original')

    def test_partial_writes_are_completed(self):
        write = os.write
        with mock.patch.object(atomic.os, 'write', side_effect=lambda fd, data: write(fd, data[:13])):
            self.publish()
        self.assertEqual(self.output.read_bytes(), self.data)

    def test_failed_write_cleans_and_keeps_source_open(self):
        with mock.patch.object(atomic.os, 'write', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError): self.publish()
        self.assert_no_output()
        self.assertEqual(os.fstat(self.fd).st_size, len(self.data))

    def test_zero_write_cannot_loop_forever(self):
        with mock.patch.object(atomic.os, 'write', return_value=0):
            with self.assertRaises(OSError): self.publish()
        self.assert_no_output()

    def test_source_mutation_during_copy_is_rejected(self):
        read = os.read; changed = False
        def mutate(fd, count):
            nonlocal changed
            block = read(fd, count)
            if not changed:
                changed = True
                self.source.write_bytes(b'x' * len(self.data))
            return block
        with mock.patch.object(atomic.os, 'read', side_effect=mutate):
            with self.assertRaises(ValueError): self.publish()
        self.assert_no_output()

    def test_posix_payload_replacement_cannot_publish_unverified_bytes(self):
        if os.name == 'nt': return  # Windows denies replacement through sharing modes.
        copier = atomic._copy_verified_file
        def replace_after_verification(source, target, digest, size):
            copier(source, target, digest, size)
            wrapper = next(p for p in self.root.iterdir() if p.name.startswith('.file-'))
            (wrapper / 'payload').rename(wrapper / 'held-original')
            (wrapper / 'payload').write_bytes(b'UNVERIFIED REPLACEMENT')
        with mock.patch.object(atomic, '_copy_verified_file', side_effect=replace_after_verification):
            with self.assertRaises(OSError): self.publish()
        self.assert_no_output()

    def test_target_bytes_are_independently_re_read_after_fsync(self):
        sync = os.fsync
        def corrupt(fd):
            os.lseek(fd, 0, os.SEEK_SET); os.write(fd, b'CORRUPTED')
            sync(fd)
        with mock.patch.object(atomic.os, 'fsync', side_effect=corrupt):
            with self.assertRaises(ValueError): self.publish()
        self.assert_no_output()

    def test_failed_fsync_does_not_publish(self):
        with mock.patch.object(atomic.os, 'fsync', side_effect=OSError(errno.EIO, 'fsync failed')):
            with self.assertRaises(OSError): self.publish()
        self.assert_no_output()

    def test_directory_source_and_unsafe_destination_are_rejected(self):
        if os.name != 'nt':
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(ValueError):
                    atomic.publish_verified_file(fd, self.output, expected_sha256=self.digest,
                                                 expected_size=len(self.data), max_bytes=1048576)
            finally: os.close(fd)
        self.output = self.root / '..' / self.root.name / 'escape'
        with self.assertRaises(ValueError): self.publish()
        self.assertFalse((self.root / 'escape').exists())

    def test_pipe_source_is_rejected_before_any_read(self):
        reader, writer = os.pipe()
        try:
            with self.assertRaises(ValueError):
                atomic.publish_verified_file(reader, self.output, expected_sha256=self.digest,
                                             expected_size=len(self.data), max_bytes=1048576)
        finally: os.close(reader); os.close(writer)
        self.assert_no_output()

    def test_posix_fifo_source_is_rejected_before_any_read(self):
        if os.name == 'nt': return  # Windows named pipes use the separate native API.
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with self.assertRaises(ValueError):
                atomic.publish_verified_file(fd, self.output, expected_sha256=self.digest,
                                             expected_size=len(self.data), max_bytes=1048576)
        finally: os.close(fd)
        self.assert_no_output()

    def test_posix_source_name_replacement_cannot_change_open_bytes(self):
        if os.name == 'nt': return  # Windows source sharing policies are caller-owned.
        self.source.rename(self.root / 'original-source')
        self.source.write_bytes(b'foreign replacement')
        self.publish()
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(self.source.read_bytes(), b'foreign replacement')

    def test_posix_parent_and_stage_swaps_preserve_foreign_replacements(self):
        if os.name == 'nt': return  # Actual Windows deny-delete tests run separately.
        parent = self.root / 'parent'; parent.mkdir(); self.output = parent / 'archive.zip'
        copier = atomic._copy_verified_file
        def swap(source, target, digest, size):
            wrapper = next(parent.iterdir())
            wrapper.rename(parent / 'owned-wrapper')
            wrapper.mkdir(); (wrapper / 'foreign').write_bytes(b'user wrapper data')
            parent.rename(self.root / 'original-parent'); parent.mkdir()
            (parent / 'foreign').write_bytes(b'user parent data')
            copier(source, target, digest, size)
        with mock.patch.object(atomic, '_copy_verified_file', side_effect=swap): self.publish()
        self.assertEqual((self.root / 'original-parent/archive.zip').read_bytes(), self.data)
        self.assertEqual((parent / 'foreign').read_bytes(), b'user parent data')
        original = self.root / 'original-parent'
        foreign_wrapper = next(p for p in original.iterdir() if p.name.startswith('.file-'))
        self.assertEqual((foreign_wrapper / 'foreign').read_bytes(), b'user wrapper data')
        self.assertEqual(list((original / 'owned-wrapper').iterdir()), [])

    def test_posix_symlink_parent_and_destination_preserve_targets(self):
        if os.name == 'nt': return  # Actual junction regressions are native-only.
        parent = self.root / 'parent'; parent.mkdir()
        link = self.root / 'link'; link.symlink_to(parent, target_is_directory=True)
        self.output = link / 'output'
        with self.assertRaises(OSError): self.publish()
        self.assertEqual(list(parent.iterdir()), [])
        self.output = self.root / 'archive.zip'
        self.output.symlink_to(self.source)
        with self.assertRaises(OSError): self.publish()
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.data)



    def test_posix_cleanup_failure_reports_committed_verified_receipt(self):
        if os.name == 'nt': return  # Native HANDLE cleanup is exercised separately.
        with mock.patch.object(atomic, '_clear_private_directory_fd',
                               side_effect=OSError(errno.EIO, 'cleanup failed')):
            with self.assertRaises(atomic.VerifiedFileCleanupError) as caught: self.publish()
        self.assertTrue(caught.exception.committed)
        self.assertEqual(caught.exception.receipt,
                         {'sha256': self.digest, 'byte_size': len(self.data)})
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(os.fstat(self.fd).st_size, len(self.data))

    def _assert_posix_final_stage_close_failure_reports_committed_receipt(self):
        if os.name == 'nt': return  # POSIX owned stage FD; native HANDLE tests are separate.
        close = os.close
        stage_closes = []
        def fail_after_actual_final_stage_close(fd):
            info = os.fstat(fd)
            wrappers = [p for p in self.root.iterdir() if p.name.startswith('.file-')]
            matching = any((info.st_dev, info.st_ino) == (p.stat().st_dev, p.stat().st_ino)
                           for p in wrappers)
            close(fd)
            if matching:
                stage_closes.append(fd)
                # First close is a fresh cleanup directory stream; second is
                # the owned stage itself, after the output is committed.
                if len(stage_closes) == 2:
                    raise OSError(errno.EIO, 'final stage close failed after actual close')
        with mock.patch.object(atomic.os, 'close', side_effect=fail_after_actual_final_stage_close):
            with self.assertRaises(atomic.VerifiedFileCleanupError) as caught: self.publish()
        self.assertEqual(len(stage_closes), 2)
        self.assertEqual(caught.exception.receipt,
                         {'sha256': self.digest, 'byte_size': len(self.data)})
        self.assertTrue(caught.exception.committed)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(os.fstat(self.fd).st_size, len(self.data))

    def test_posix_final_stage_close_failure_reports_committed_receipt(self):
        self._assert_posix_final_stage_close_failure_reports_committed_receipt()

    def test_posix_final_stage_close_error_inside_unrelated_exception_handler(self):
        try:
            raise RuntimeError('earlier operation already handled by caller')
        except RuntimeError as previous:
            self._assert_posix_final_stage_close_failure_reports_committed_receipt()
            self.assertFalse(getattr(previous, '__notes__', []))

    def test_linux_descriptor_lifetimes_on_success_and_failure(self):
        if not sys.platform.startswith('linux'): return
        before = len(list(Path('/proc/self/fd').iterdir()))
        self.publish()
        for _ in range(3):
            with self.assertRaises(OSError): self.publish()
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)


if __name__ == '__main__': unittest.main()
