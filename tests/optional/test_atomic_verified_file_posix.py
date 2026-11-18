"""POSIX descriptor cleanup and portable Windows adapter simulations."""
import contextlib
import errno
import hashlib
import os
import stat
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import atomic_publish as atomic
import atomic_windows as windows

if not (sys.platform.startswith('linux') or sys.platform == 'darwin'):
    raise RuntimeError('This module requires Linux or macOS')


class VerifiedFilePosixTests(unittest.TestCase):
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

    def test_payload_close_failure_preserves_precommit_error(self):
        for collision in (False, True):
            with self.subTest(collision=collision):
                if collision:
                    self.output.write_bytes(b'original destination')
                close = os.close
                payload_fds = []
                def fail_payload(fd):
                    info = os.fstat(fd)
                    is_payload = stat.S_ISREG(info.st_mode) and info.st_ino != os.fstat(self.fd).st_ino
                    close(fd)
                    if is_payload:
                        payload_fds.append(fd)
                        raise OSError(errno.EIO, 'payload close failed')
                with mock.patch.object(atomic.os, 'close', side_effect=fail_payload):
                    with self.assertRaises(FileExistsError if collision else ValueError) as caught:
                        self.publish(**({} if collision else {'expected_sha256': '0' * 64}))
                self.assertEqual(len(payload_fds), 1)
                self.assertIn('payload close failed', ' '.join(getattr(caught.exception, '__notes__', [])))
                if collision:
                    self.assertEqual(self.output.read_bytes(), b'original destination')
                    self.output.unlink()
                else:
                    self.assert_no_output()

    def test_multiple_close_failures_preserve_operation_and_all_notes(self):
        close = os.close
        failed = []
        def fail_owned(fd):
            info = os.fstat(fd)
            is_payload = stat.S_ISREG(info.st_mode) and info.st_ino != os.fstat(self.fd).st_ino
            is_stage = any((info.st_dev, info.st_ino) == (p.stat().st_dev, p.stat().st_ino)
                           for p in self.root.glob('.file-*'))
            close(fd)
            if is_payload or is_stage:
                failed.append(fd)
                raise OSError(errno.EIO, 'injected close %d' % len(failed))
        with mock.patch.object(atomic.os, 'close', side_effect=fail_owned):
            with self.assertRaises(ValueError) as caught:
                self.publish(expected_sha256='0' * 64)
        self.assertEqual(len(failed), 3)  # payload, cleanup stream, stage
        notes = ' '.join(getattr(caught.exception, '__notes__', []))
        for number in range(1, 4):
            self.assertIn('injected close %d' % number, notes)
        self.assertFalse(self.output.exists())

    def test_pinned_parent_close_preserves_collision(self):
        self.output.write_bytes(b'original destination')
        close = os.close
        parent_closes = []
        root_info = self.root.stat()
        def fail_last_parent(fd):
            info = os.fstat(fd)
            is_parent = (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino)
            close(fd)
            if is_parent:
                parent_closes.append(fd)
                if len(parent_closes) == 2:
                    raise OSError(errno.EIO, 'pinned parent close failed')
        with mock.patch.object(atomic.os, 'close', side_effect=fail_last_parent):
            with self.assertRaises(FileExistsError) as caught:
                self.publish()
        self.assertEqual(len(parent_closes), 2)
        self.assertIn('pinned parent close failed', ' '.join(getattr(caught.exception, '__notes__', [])))
        self.assertEqual(self.output.read_bytes(), b'original destination')

    def test_payload_close_after_commit_inside_ambient_exception(self):
        close = os.close
        calls = []
        def fail_payload(fd):
            info = os.fstat(fd)
            is_payload = stat.S_ISREG(info.st_mode) and info.st_ino != os.fstat(self.fd).st_ino
            close(fd)
            if is_payload:
                calls.append(fd)
                raise OSError(errno.EIO, 'committed payload close')
        try:
            raise RuntimeError('ambient')
        except RuntimeError as ambient:
            with mock.patch.object(atomic.os, 'close', side_effect=fail_payload):
                with self.assertRaises(atomic.VerifiedFileCleanupError) as caught:
                    self.publish()
            self.assertFalse(getattr(ambient, '__notes__', []))
        self.assertEqual(len(calls), 1)
        self.assertEqual(caught.exception.receipt, {'sha256': self.digest, 'byte_size': len(self.data)})
        self.assertIn('committed payload close', str(caught.exception.__cause__))
        self.assertEqual(self.output.read_bytes(), self.data)

    def test_windows_rename_commit_survives_nested_cleanup_errors(self):
        staged = self.root / 'staged'
        error = OSError(errno.EIO, 'destination pin close failed')
        fake_os = types.SimpleNamespace(**vars(os))
        fake_os.name = 'nt'

        @contextlib.contextmanager
        def pin(parent):
            yield types.SimpleNamespace(path=parent)
            raise error

        @contextlib.contextmanager
        def private(parent):
            fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            try:
                yield windows._PrivateFile(fd)
            except BaseException as exc:
                try:
                    raise
                finally:
                    windows._finish_cleanups(exc, [lambda: os.close(fd),
                                                  lambda: (_ for _ in ()).throw(OSError('outer close failed'))])
            else:
                os.close(fd)

        with mock.patch.object(atomic, 'os', fake_os), \
             mock.patch.object(windows, 'private_file', private), \
             mock.patch.object(windows, '_path', Path), \
             mock.patch.object(windows, 'pinned_directory', pin), \
             mock.patch.object(windows, '_rename_handle', side_effect=lambda fd, dst: os.rename(staged, dst)), \
             mock.patch.dict(sys.modules, {'msvcrt': types.SimpleNamespace(get_osfhandle=lambda fd: fd)}):
            with self.assertRaises(atomic.VerifiedFileCleanupError) as caught:
                self.publish()
        self.assertIs(caught.exception.__cause__, error)
        self.assertTrue(any('outer close failed' in note for note in error.__notes__))
        self.assertEqual(caught.exception.receipt, {'sha256': self.digest, 'byte_size': len(self.data)})
        self.assertEqual(self.output.read_bytes(), self.data)

    def test_windows_failed_rename_is_not_reported_as_committed(self):
        staged = self.root / 'staged'
        error = OSError(errno.EIO, 'rename failed')
        fake_os = types.SimpleNamespace(**vars(os)); fake_os.name = 'nt'

        @contextlib.contextmanager
        def private(parent):
            fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            try:
                yield windows._PrivateFile(fd)
            finally:
                os.close(fd)

        @contextlib.contextmanager
        def pin(parent):
            yield types.SimpleNamespace(path=parent)

        with mock.patch.object(atomic, 'os', fake_os), \
             mock.patch.object(windows, 'private_file', private), \
             mock.patch.object(windows, '_path', Path), \
             mock.patch.object(windows, 'pinned_directory', pin), \
             mock.patch.object(windows, '_rename_handle', side_effect=error), \
             mock.patch.dict(sys.modules, {'msvcrt': types.SimpleNamespace(get_osfhandle=lambda fd: fd)}):
            with self.assertRaises(OSError) as caught:
                self.publish()
        self.assertIs(caught.exception, error)
        self.assertFalse(self.output.exists())


if __name__ == '__main__': unittest.main()
