"""Descriptor-only POSIX staging; Windows explicitly tests unsupported policy."""
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_publish as atomic


class DescriptorStageTests(unittest.TestCase):
    def unsupported(self):
        if atomic.private_stage_fd_supported():
            return False
        with self.assertRaises(OSError) as caught:
            with atomic.private_stage_fd(0):
                self.fail('unsupported descriptor staging yielded')
        self.assertEqual(caught.exception.errno, errno.ENOTSUP)
        return True

    def write(self, directory, name, payload=b'owned bytes'):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)

    def test_explicit_unsupported_never_touches_disk(self):
        with mock.patch.object(atomic, 'private_stage_fd_supported', return_value=False):
            self.assertTrue(self.unsupported())

    def test_actual_fd_stage_closes_and_keeps_only_empty_wrapper(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with atomic.private_stage_fd(parent) as fd:
                    self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
                    self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o700)
                    self.write(fd, 'file')
                with self.assertRaises(OSError): os.fstat(fd)
                wrappers = list(Path(directory).iterdir())
                self.assertEqual(len(wrappers), 1)
                self.assertEqual(list(wrappers[0].iterdir()), [])
                self.assertTrue(stat.S_ISDIR(os.fstat(parent).st_mode))
            finally: os.close(parent)

    def test_namespace_swap_cleans_owned_inode_and_preserves_foreign(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with atomic.private_stage_fd(parent) as fd:
                    wrapper = next(root.iterdir())
                    wrapper.rename(root / 'original')
                    wrapper.mkdir()
                    (wrapper / 'foreign').write_bytes(b'user data')
                    self.write(fd, 'owned')
                self.assertEqual((wrapper / 'foreign').read_bytes(), b'user data')
                self.assertEqual(list((root / 'original').iterdir()), [])
            finally: os.close(parent)

    def test_cleanup_cannot_follow_symlink_to_unrelated_directory(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); foreign = root / 'foreign'; foreign.mkdir()
            (foreign / 'original').write_bytes(b'user data')
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with atomic.private_stage_fd(parent) as fd:
                    os.symlink(foreign, 'link', dir_fd=fd)
                    os.mkdir('nested', mode=0o700, dir_fd=fd)
                    nested = os.open('nested', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                    try: self.write(nested, 'owned')
                    finally: os.close(nested)
                self.assertEqual((foreign / 'original').read_bytes(), b'user data')
                self.assertEqual(list(next(p for p in root.iterdir() if p != foreign).iterdir()), [])
            finally: os.close(parent)

    def test_error_retention_and_exception_identity(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            failure = RuntimeError('owned diagnostic')
            try:
                with self.assertRaises(RuntimeError) as caught:
                    with atomic.private_stage_fd(parent, cleanup_on_error=False) as fd:
                        self.write(fd, 'diagnostic'); raise failure
                self.assertIs(caught.exception, failure)
                self.assertEqual((next(root.iterdir()) / 'diagnostic').read_bytes(), b'owned bytes')
                with self.assertRaises(OSError): os.fstat(fd)
            finally: os.close(parent)

    def test_directory_stream_offset_does_not_hide_later_contents(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with atomic.private_stage_fd(parent) as fd:
                    self.write(fd, 'first'); os.listdir(fd)
                    self.write(fd, 'second'); os.listdir(fd)
                self.assertEqual(list(next(root.iterdir()).iterdir()), [])
            finally: os.close(parent)

    def test_closed_and_reused_stage_descriptor_never_cleans_or_closes_foreign(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); foreign = root / 'foreign'; foreign.mkdir()
            (foreign / 'original').write_bytes(b'user data')
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            foreign_fd = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
            reused = None
            try:
                with self.assertRaises(OSError) as caught:
                    with atomic.private_stage_fd(parent) as fd:
                        reused = fd
                        os.close(fd); os.dup2(foreign_fd, fd)
                self.assertEqual(caught.exception.errno, errno.ESTALE)
                self.assertEqual(os.fstat(reused).st_ino, os.fstat(foreign_fd).st_ino)
                self.assertEqual((foreign / 'original').read_bytes(), b'user data')
            finally:
                if reused is not None: os.close(reused)
                os.close(foreign_fd); os.close(parent)

    def test_parent_descriptor_reuse_is_rejected_before_any_stage_creation(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); foreign = root / 'foreign'; foreign.mkdir()
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            foreign_fd = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
            created = []; original_dup = os.dup
            def replace_before_duplicate(descriptor):
                os.dup2(foreign_fd, parent)
                duplicate = original_dup(descriptor); created.append(duplicate)
                return duplicate
            try:
                with mock.patch.object(atomic.os, 'dup', side_effect=replace_before_duplicate):
                    with self.assertRaises(OSError) as caught:
                        with atomic.private_stage_fd(parent): self.fail('changed parent admitted')
                self.assertEqual(caught.exception.errno, errno.ESTALE)
                with self.assertRaises(OSError): os.fstat(created[0])
                self.assertEqual(list(foreign.iterdir()), [])
                self.assertEqual([p.name for p in root.iterdir()], ['foreign'])
            finally: os.close(foreign_fd); os.close(parent)

    def test_inner_payload_publication_is_exclusive(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with atomic.private_stage_fd(parent) as fd:
                    os.mkdir('payload', mode=0o700, dir_fd=fd)
                    atomic.rename_no_replace_at(fd, 'payload', parent, 'result')
                    os.mkdir('payload', mode=0o700, dir_fd=fd)
                    with self.assertRaises(FileExistsError):
                        atomic.rename_no_replace_at(fd, 'payload', parent, 'result')
                    self.assertTrue((root / 'result').is_dir())
            finally: os.close(parent)

    def test_invalid_policies_and_collision_budget_do_not_publish(self):
        if self.unsupported(): return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for options in ({'prefix': '../escape'}, {'cleanup': 1}, {'cleanup_on_error': None}):
                    with self.assertRaises(ValueError):
                        with atomic.private_stage_fd(parent, **options): self.fail('invalid policy')
                (root / '.publish-collision').mkdir()
                with mock.patch.object(atomic.secrets, 'token_hex', return_value='collision'):
                    with self.assertRaises(FileExistsError):
                        with atomic.private_stage_fd(parent): self.fail('collision admitted')
                self.assertEqual([p.name for p in root.iterdir()], ['.publish-collision'])
            finally: os.close(parent)


if __name__ == '__main__': unittest.main()
