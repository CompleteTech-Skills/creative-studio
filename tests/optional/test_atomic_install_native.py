"""Portable admission plus real descriptor-backed byte installation."""
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import unittest
from unittest import mock
import zipfile

from atomic_install import Limits, install_directory, install_file
import atomic_install as installer


class DescriptorInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        self.target = self.root / 'target'; self.target.mkdir()
        self.source_fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        self.target_fd = os.open(self.target, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.source_fd); self.addCleanup(os.close, self.target_fd)
        self.limits = Limits(20, 1024 * 1024, 2 * 1024 * 1024, 8)

    def inventory(self, **files):
        result = []
        for name, payload in files.items():
            path = self.source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
            result.append(dict(path=name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)))
        return result

    def install(self, inventory, **kwargs):
        install_directory(self.source_fd, self.target_fd, 'installed', inventory, limits=self.limits, **kwargs)

    def assert_unpublished(self):
        self.assertFalse((self.target / 'installed').exists())
        for wrapper in self.target.glob('.install-*'):
            self.assertEqual(list(wrapper.iterdir()), [])

    def test_real_closed_tree_with_empty_directories_and_unchanged_source(self):
        inventory = self.inventory(**{'data.bin': b'actual bytes', 'nested/zero': b''})
        (self.source / 'empty').mkdir()
        self.install(inventory, directories=['empty'])
        self.assertEqual((self.target / 'installed/data.bin').read_bytes(), b'actual bytes')
        self.assertEqual((self.target / 'installed/nested/zero').read_bytes(), b'')
        self.assertTrue((self.target / 'installed/empty').is_dir())
        self.assertEqual((self.source / 'data.bin').read_bytes(), b'actual bytes')
        self.assertEqual(list((self.target / 'installed/empty').iterdir()), [])

    def test_missing_extra_corrupt_and_extra_empty_directory_fail_closed(self):
        for mutation in ('missing', 'extra', 'corrupt', 'directory'):
            with self.subTest(mutation=mutation):
                for entry in self.source.iterdir():
                    if entry.is_dir(): entry.rmdir()
                    else: entry.unlink()
                inventory = self.inventory(data=b'original')
                if mutation == 'missing': (self.source / 'data').unlink()
                elif mutation == 'extra': (self.source / 'extra').write_bytes(b'user')
                elif mutation == 'corrupt': (self.source / 'data').write_bytes(b'corrupt!')
                else: (self.source / 'extra').mkdir()
                with self.assertRaises(ValueError): self.install(inventory)
                self.assert_unpublished()

    def test_symlink_and_fifo_rejected_without_following_or_blocking(self):
        foreign = self.root / 'foreign'; foreign.write_bytes(b'user bytes')
        for kind in ('symlink', 'fifo'):
            with self.subTest(kind=kind):
                path = self.source / 'unsafe'
                if kind == 'symlink': path.symlink_to(foreign)
                else: os.mkfifo(path)
                with self.assertRaises(ValueError): self.install([])
                path.unlink()
                self.assertEqual(foreign.read_bytes(), b'user bytes')
                self.assert_unpublished()

    def test_namespace_replacement_cannot_redirect_source_or_parent(self):
        inventory = self.inventory(data=b'original')
        self.source.rename(self.root / 'retained-source'); self.source.mkdir()
        (self.source / 'data').write_bytes(b'foreign')
        self.target.rename(self.root / 'retained-target'); self.target.mkdir()
        self.install(inventory)
        self.assertEqual((self.root / 'retained-target/installed/data').read_bytes(), b'original')
        self.assertFalse((self.target / 'installed').exists())
        self.assertEqual((self.source / 'data').read_bytes(), b'foreign')

    def test_inventory_is_captured_before_filesystem_work(self):
        inventory = self.inventory(data=b'original')
        original = installer._stage
        @contextmanager
        def mutate_after_admission(parent):
            inventory[0]['sha256'] = '0' * 64
            inventory[0]['bytes'] = 999
            inventory.append(dict(path='foreign', sha256='0' * 64, bytes=1))
            with original(parent) as stage:
                yield stage
        with mock.patch.object(installer, '_stage', mutate_after_admission): self.install(inventory)
        self.assertEqual((self.target / 'installed/data').read_bytes(), b'original')
        self.assertFalse((self.target / 'installed/foreign').exists())

    def test_directory_child_namespace_swap_is_detected(self):
        (self.source / 'child').mkdir()
        node = installer._PosixDirectory(self.source_fd)
        with self.assertRaises(ValueError):
            with node.child('child'):
                (self.source / 'child').rename(self.source / 'kept')
                (self.source / 'child').mkdir()
        self.assertTrue((self.source / 'kept').is_dir())

    def test_destination_inside_source_rejected_before_staging(self):
        inventory = self.inventory(data=b'original')
        (self.source / 'child').mkdir()
        child = os.open(self.source / 'child', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for parent in (self.source_fd, child):
                with self.assertRaises(ValueError):
                    install_directory(self.source_fd, parent, 'installed', inventory,
                                      directories=['child'], limits=self.limits)
            self.assertEqual(sorted(p.name for p in self.source.iterdir()), ['child', 'data'])
            self.assertEqual(list((self.source / 'child').iterdir()), [])
        finally: os.close(child)

    def test_sibling_file_publication_preserves_original_identity(self):
        payload = b'original'
        self.inventory(data=payload)
        before = (self.source / 'data').stat()
        install_file(self.source_fd, 'data', self.source_fd, 'copy',
                     sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload), limits=self.limits)
        after = (self.source / 'data').stat()
        self.assertEqual((before.st_dev, before.st_ino, before.st_size),
                         (after.st_dev, after.st_ino, after.st_size))
        self.assertEqual((self.source / 'data').read_bytes(), payload)
        self.assertEqual((self.source / 'copy').read_bytes(), payload)

    def test_immutable_destination_is_preserved(self):
        inventory = self.inventory(data=b'original')
        target = self.target / 'installed'; target.mkdir(); (target / 'foreign').write_bytes(b'user')
        with self.assertRaises(FileExistsError): self.install(inventory)
        self.assertEqual((target / 'foreign').read_bytes(), b'user')
        self.assertEqual(list(target.iterdir()), [target / 'foreign'])
        self.assertEqual((self.source / 'data').read_bytes(), b'original')

    def test_source_and_payload_final_validation_prevent_commit(self):
        inventory = self.inventory(data=b'original')
        original = installer._copy
        for changed in ('source', 'payload'):
            with self.subTest(changed=changed):
                (self.source / 'data').write_bytes(b'original')
                def corrupt(source, source_name, output, output_name, digest, size):
                    original(source, source_name, output, output_name, digest, size)
                    selected = source if changed == 'source' else output
                    fd = os.open(source_name, os.O_WRONLY, dir_fd=selected.fd)
                    with os.fdopen(fd, 'wb') as stream: stream.write(b'corrupt!')
                with mock.patch.object(installer, '_copy', side_effect=corrupt):
                    with self.assertRaises(ValueError): self.install(inventory)
                self.assert_unpublished()

    def test_zip_is_a_real_file_and_exclusive(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive: archive.writestr('payload.txt', b'actual ZIP member')
        payload = buffer.getvalue(); self.inventory(**{'source.zip': payload})
        options = dict(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload), limits=self.limits)
        install_file(self.source_fd, 'source.zip', self.target_fd, 'result.zip', **options)
        result = self.target / 'result.zip'
        self.assertTrue(result.is_file())
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.read('payload.txt'), b'actual ZIP member')
        with self.assertRaises(FileExistsError):
            install_file(self.source_fd, 'source.zip', self.target_fd, 'result.zip', **options)
        self.assertEqual(result.read_bytes(), payload)
        self.assertEqual((self.source / 'source.zip').read_bytes(), payload)

    def test_file_rejects_bad_digest_overflow_and_unsafe_source(self):
        self.inventory(data=b'original')
        for digest, size in ((hashlib.sha256(b'original').hexdigest(), 2), ('0' * 64, 8)):
            with self.assertRaises(ValueError):
                install_file(self.source_fd, 'data', self.target_fd, 'result',
                             sha256=digest, bytes=size, limits=self.limits)
            self.assertFalse((self.target / 'result').exists())
        (self.source / 'link').symlink_to(self.source / 'data')
        with self.assertRaises(OSError):
            install_file(self.source_fd, 'link', self.target_fd, 'result', sha256='0' * 64, bytes=8, limits=self.limits)

    def test_descriptor_authority_rejects_paths_bools_and_non_directories(self):
        for value in (self.source, str(self.source), True, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                install_directory(value, self.target_fd, 'installed', [], limits=self.limits)
        self.inventory(data=b'x')
        fd = os.open(self.source / 'data', os.O_RDONLY)
        try:
            with self.assertRaises(ValueError): install_directory(fd, self.target_fd, 'installed', [], limits=self.limits)
        finally: os.close(fd)

    def test_repeated_failed_install_does_not_leak_descriptors(self):
        inventory = self.inventory(data=b'original')
        inventory[0]['sha256'] = '0' * 64
        if not Path('/proc/self/fd').is_dir(): return
        before = len(list(Path('/proc/self/fd').iterdir()))
        for _ in range(25):
            with self.assertRaises(ValueError): self.install(inventory)
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_read_chunks_are_bounded_and_extra_scan_stops_immediately(self):
        inventory = self.inventory(data=b'x' * (installer._CHUNK * 3))
        original = installer._PosixDirectory.reader
        requests = []
        @contextmanager
        def bounded(directory, name):
            with original(directory, name) as (stream, info):
                class Stream:
                    def read(self, count):
                        requests.append(count)
                        self_outer.assertLessEqual(count, installer._CHUNK)
                        return stream.read(count)
                yield Stream(), info
        self_outer = self
        with mock.patch.object(installer._PosixDirectory, 'reader', bounded): self.install(inventory)
        self.assertGreater(len(requests), 3)
        consumed = []
        @contextmanager
        def entries(directory):
            def items():
                for index in range(10000):
                    consumed.append(index); yield 'unexpected' + str(index), 'file'
            yield items()
        with mock.patch.object(installer._PosixDirectory, 'entries', entries):
            with self.assertRaises(ValueError): self.install(inventory)
        self.assertEqual(consumed, [0])


class WindowsInstallTests(unittest.TestCase):
    def setUp(self):
        import atomic_windows
        if not atomic_windows.supported():
            raise RuntimeError('Windows installer gate requires real Windows APIs')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        self.target = self.root / 'target'; self.target.mkdir()
        self.limits = Limits(20, 1024 * 1024, 2 * 1024 * 1024, 8)

    @contextmanager
    def authorities(self):
        from atomic_windows import pinned_directory
        with pinned_directory(self.source) as source, pinned_directory(self.target) as target:
            yield source, target

    def inventory(self, **files):
        result = []
        for name, payload in files.items():
            path = self.source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
            result.append(dict(path=name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)))
        return result

    def test_actual_windows_closed_directory_and_empty_dirs(self):
        inventory = self.inventory(**{'data': b'original', 'nested/zero': b''})
        (self.source / 'empty').mkdir()
        with self.authorities() as (source, target):
            install_directory(source, target, 'installed', inventory, directories=['empty'], limits=self.limits)
        self.assertEqual((self.target / 'installed/data').read_bytes(), b'original')
        self.assertTrue((self.target / 'installed/empty').is_dir())
        self.assertEqual((self.source / 'data').read_bytes(), b'original')

    def test_windows_native_zip_file_and_exclusive_publication(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive: archive.writestr('data', b'real ZIP')
        payload = buffer.getvalue(); self.inventory(**{'source.zip': payload})
        options = dict(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload), limits=self.limits)
        with self.authorities() as (source, target):
            install_file(source, 'source.zip', target, 'result.zip', **options)
            with self.assertRaises(OSError): install_file(source, 'source.zip', target, 'result.zip', **options)
        with zipfile.ZipFile(self.target / 'result.zip') as archive:
            self.assertEqual(archive.read('data'), b'real ZIP')
        self.assertEqual((self.source / 'source.zip').read_bytes(), payload)

    def test_windows_missing_extra_and_corrupt_are_not_published(self):
        for mutation in ('missing', 'extra', 'corrupt', 'directory'):
            with self.subTest(mutation=mutation):
                for path in self.source.iterdir():
                    if path.is_dir(): path.rmdir()
                    else: path.unlink()
                inventory = self.inventory(data=b'original')
                if mutation == 'missing': (self.source / 'data').unlink()
                elif mutation == 'extra': (self.source / 'extra').write_bytes(b'foreign')
                elif mutation == 'corrupt': (self.source / 'data').write_bytes(b'corrupt!')
                else: (self.source / 'extra').mkdir()
                with self.authorities() as (source, target):
                    with self.assertRaises(ValueError):
                        install_directory(source, target, 'installed', inventory, limits=self.limits)
                self.assertFalse((self.target / 'installed').exists())

    def test_windows_ancestors_stay_pinned_and_existing_destination_survives(self):
        inventory = self.inventory(data=b'original')
        (self.target / 'installed').mkdir(); (self.target / 'installed/foreign').write_bytes(b'user')
        with self.authorities() as (source, target):
            with self.assertRaises(OSError): self.source.rename(self.root / 'replaced-source')
            with self.assertRaises(OSError): self.target.rename(self.root / 'replaced-target')
            with self.assertRaises(OSError):
                install_directory(source, target, 'installed', inventory, limits=self.limits)
        self.assertEqual((self.target / 'installed/foreign').read_bytes(), b'user')

    def test_windows_reparse_closed_inventory_preserves_target(self):
        import subprocess
        foreign = self.root / 'foreign'; foreign.mkdir(); (foreign / 'data').write_bytes(b'user')
        result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(self.source / 'link'), str(foreign)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            with self.authorities() as (source, target):
                with self.assertRaises(ValueError):
                    install_directory(source, target, 'installed', [], limits=self.limits)
        finally: (self.source / 'link').rmdir()
        self.assertEqual((foreign / 'data').read_bytes(), b'user')

    def test_windows_requires_live_canonical_pins(self):
        with self.authorities() as (source, target):
            for invalid in (self.source, str(self.source), 3, True):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    install_directory(invalid, target, 'installed', [], limits=self.limits)
        with self.assertRaises(ValueError):
            install_directory(source, target, 'installed', [], limits=self.limits)


def load_tests(loader, tests, pattern):
    # Dedicated native workflow selects the host's real OS contract, with no skips.
    return loader.loadTestsFromTestCase(WindowsInstallTests if os.name == 'nt' else DescriptorInstallTests)


if __name__ == '__main__': unittest.main()
