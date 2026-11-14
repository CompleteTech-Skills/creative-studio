"""Portable admission plus real descriptor-backed byte installation."""
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import unittest
from unittest import mock
import zipfile

from atomic_install import Limits, install_directory, install_file
import atomic_install as installer


class AdmissionTests(unittest.TestCase):
    def test_limits_are_explicit_strict_and_bounded(self):
        for values in ((True, 1, 1, 1), (1, True, 1, 1), (1, 1, True, 1),
                       (1, 1, 1, True), (0, 1, 1, 1), (1, -1, 1, 1), (1, 1, 1, 65)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Limits(*values)

    def test_inventory_is_rejected_before_authority_io(self):
        digest = hashlib.sha256(b'x').hexdigest()
        entry = dict(path='x', sha256=digest, bytes=1)
        limits = Limits(2, 4, 4, 3)
        for inventory, directories in (([entry, entry], ()), ([dict(entry, path='../x')], ()),
                                       ([dict(entry, bytes=True)], ()), ([dict(entry, extra=1)], ()),
                                       ([dict(entry, sha256='BAD')], ()), ([entry], ('x',)),
                                       ([dict(entry, path='X/x')], ('x',)), ([entry] * 3, ())):
            with self.subTest(inventory=inventory), mock.patch.object(installer, '_authority') as authority:
                with self.assertRaises(ValueError):
                    install_directory(object(), object(), 'output', inventory, limits=limits, directories=directories)
                authority.assert_not_called()

    def test_inventory_iterator_admission_is_bounded(self):
        calls = []
        def infinite():
            for index in range(10000):
                calls.append(index)
                yield dict(path=str(index), bytes=0, sha256=hashlib.sha256(b'').hexdigest())
        with self.assertRaises(ValueError):
            install_directory(None, None, 'out', infinite(), limits=Limits(2, 0, 0, 1))
        self.assertEqual(len(calls), 3)




if __name__ == '__main__': unittest.main()
