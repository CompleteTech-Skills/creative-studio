"""Actual canonical registered descriptor consumers, required by Linux CI."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import atomic_publish as atomic
from rig_errors import directory_capability, RigError


class RegisteredDirectoryConsumerTests(unittest.TestCase):
    def test_registered_nested_directory_stays_pinned_after_namespace_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'; source.mkdir()
            (source / 'nested').mkdir()
            fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with directory_capability(fd) as cap:
                    path = cap.path / 'nested'
                    source.rename(root / 'original'); source.mkdir()
                    (source / 'nested').mkdir()
                    with atomic.pinned_directory(path) as selected:
                        output = os.open('owned', os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                         0o600, dir_fd=selected)
                        os.close(output)
                    self.assertTrue((root / 'original/nested/owned').is_file())
                    self.assertEqual(list((source / 'nested').iterdir()), [])
                with self.assertRaises((OSError, ValueError)):
                    with atomic.pinned_directory(path): self.fail('expired capability admitted')
                self.assertTrue(os.fstat(fd))
            finally: os.close(fd)

    def test_unregistered_proc_alias_and_capability_symlink_remain_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                alias = Path('/proc/self/fd') / str(fd)
                with self.assertRaises((OSError, ValueError)):
                    with atomic.pinned_directory(alias): self.fail('raw proc alias admitted')
                (root / 'link').symlink_to(root, target_is_directory=True)
                with directory_capability(fd) as cap:
                    with self.assertRaises(RigError):
                        with atomic.pinned_directory(cap.path / 'link'):
                            self.fail('symlink descendant admitted')
            finally: os.close(fd)


if __name__ == '__main__': unittest.main()
