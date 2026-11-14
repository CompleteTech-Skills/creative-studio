"""Portable Windows-publication admission; actual OS tests have a dedicated gate."""
import errno
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_windows as windows


class WindowsPublicationContracts(unittest.TestCase):
    def test_unsupported_stage_fails_before_touching_existing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'original').write_bytes(b'user bytes')
            windows._api.cache_clear()
            try:
                with mock.patch.object(windows, 'supported', return_value=False):
                    with self.assertRaises(OSError) as caught:
                        with windows.private_stage(root): self.fail('unsupported adapter yielded')
                    self.assertEqual(caught.exception.errno, errno.ENOTSUP)
            finally: windows._api.cache_clear()
            self.assertEqual([p.name for p in root.iterdir()], ['original'])
            self.assertEqual((root / 'original').read_bytes(), b'user bytes')

    def test_publication_names_reject_traversal_devices_and_streams(self):
        for name in ('', '.', '..', '../target', 'a/b', 'a\\b', 'a:stream',
                     'NUL', 'CON.json', 'target.', 'target ', 'a\0b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                windows._component(name)
        self.assertEqual(windows._component('target-01.json'), 'target-01.json')


if __name__ == '__main__': unittest.main()
