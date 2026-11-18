"""Actual filesystem checks for the explicit internal physics directory bridge."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as physics


class DescriptorPlatformTests(unittest.TestCase):
    def test_platform_policy_is_explicit(self):
        if os.name!='posix':
            with self.assertRaises(physics.BackendError) as error:physics._Reader.from_descriptor(0)
            self.assertEqual(error.exception.report['code'],'PHYSICS_PLATFORM_UNSUPPORTED')
        else:
            with tempfile.TemporaryDirectory() as root:
                fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
                try:
                    with physics._Reader.from_descriptor(fd) as reader:self.assertEqual(reader.names(),set())
                finally:os.close(fd)


@unittest.skipUnless(os.name=='posix','The secure physics descriptor profile is POSIX; Windows rejects it explicitly.')
class DirectoryDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'owned';self.root.mkdir()
        self.fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)

    def tearDown(self):
        if self.fd is not None:os.close(self.fd)
        self.temp.cleanup()

    def test_owned_duplicate_survives_borrower_close_and_root_replacement(self):
        physics._write_relative(self.fd,'nested/a.bin',b'actual payload')
        with physics._Reader.from_descriptor(self.fd) as reader:
            os.close(self.fd);self.fd=None
            self.root.rename(self.root.with_name('original'))
            self.root.mkdir();(self.root/'foreign.txt').write_bytes(b'foreign')
            self.assertEqual(reader.read('nested/a.bin'),b'actual payload')
            self.assertEqual(reader.names(),{'nested/a.bin'})
            self.assertEqual(reader.names(),{'nested/a.bin'})
            self.assertEqual(reader.stat('nested/a.bin').st_size,14)
        self.assertEqual((self.root/'foreign.txt').read_bytes(),b'foreign')

    def test_invalid_file_closed_and_noninteger_descriptors_fail(self):
        file=self.root/'file';file.write_bytes(b'x')
        fd=os.open(file,os.O_RDONLY)
        try:
            with self.assertRaises(physics.BackendError):physics._Reader.from_descriptor(fd)
            self.assertEqual(os.read(fd,1),b'x')
        finally:os.close(fd)
        for bad in (fd,-1,True,'3'):
            with self.subTest(descriptor=bad),self.assertRaises(physics.BackendError):physics._Reader.from_descriptor(bad)

    def test_context_close_keeps_borrower_open_and_rejects_expired_reader(self):
        with physics._Reader.from_descriptor(self.fd) as reader:pass
        self.assertTrue(os.fstat(self.fd))
        with self.assertRaises(physics.BackendError):reader.names()
        reader.__exit__(None,None,None)
        self.assertTrue(os.fstat(self.fd))

    def test_reused_owned_fd_does_not_read_or_close_foreign_directory(self):
        other=Path(self.temp.name)/'other';other.mkdir()
        with physics._Reader.from_descriptor(self.fd) as reader:
            owned=reader.fd;os.close(owned)
            replacement=os.open(other,os.O_RDONLY|os.O_DIRECTORY)
            if replacement!=owned:
                os.dup2(replacement,owned);os.close(replacement)
            with self.assertRaises(physics.BackendError):reader.names()
        try:self.assertTrue(os.fstat(owned))
        finally:os.close(owned)

    def test_relative_writes_are_exclusive_and_paths_cannot_escape(self):
        physics._write_relative(self.fd,'folder/data',b'first')
        with self.assertRaises(physics.BackendError):physics._write_relative(self.fd,'folder/data',b'second')
        self.assertEqual((self.root/'folder/data').read_bytes(),b'first')
        for name in ('../escape','/absolute','folder/../escape','folder\\escape','CON'):
            with self.subTest(name=name),self.assertRaises(physics.BackendError):physics._write_relative(self.fd,name,b'x')
        self.assertFalse((Path(self.temp.name)/'escape').exists())

    def test_link_and_special_file_inputs_fail_without_following(self):
        foreign=Path(self.temp.name)/'foreign';foreign.mkdir();(foreign/'data').write_bytes(b'preserve')
        (self.root/'link').symlink_to(foreign,target_is_directory=True)
        with self.assertRaises(physics.BackendError):physics._write_relative(self.fd,'link/data',b'changed')
        with physics._Reader.from_descriptor(self.fd) as reader:
            with self.assertRaises(physics.BackendError):reader.read('link/data')
            with self.assertRaises(physics.BackendError):reader.names()
        self.assertEqual((foreign/'data').read_bytes(),b'preserve')
        (self.root/'link').unlink();os.mkfifo(self.root/'fifo')
        with physics._Reader.from_descriptor(self.fd) as reader:
            with self.assertRaises(physics.BackendError):reader.read('fifo')
        with self.assertRaises(physics.BackendError):physics._tree_size_relative(self.fd)

    def test_inventory_is_sorted_bounded_and_read_order_independent(self):
        physics._write_relative(self.fd,'z/file',b'xyz')
        physics._write_relative(self.fd,'a',b'abc')
        first=physics._inventory_relative(self.fd)
        self.assertEqual([f['path'] for f in first],['a','z/file'])
        self.assertEqual(first,physics._inventory_relative(self.fd))
        self.assertEqual(physics._tree_size_relative(self.fd),6)
        self.assertEqual(first[0]['sha256'],physics.digest(b'abc'))
        with patch.object(physics,'MAX_CACHE_BYTES',4),self.assertRaises(physics.BackendError):physics._inventory_relative(self.fd)
        with patch.object(physics,'MAX_FILES',1),self.assertRaises(physics.BackendError):physics._inventory_relative(self.fd)
        with patch.object(physics,'MAX_CACHE_BYTES',2),self.assertRaises(physics.BackendError):physics._write_relative(self.fd,'large',b'abc')
        self.assertFalse((self.root/'large').exists())
