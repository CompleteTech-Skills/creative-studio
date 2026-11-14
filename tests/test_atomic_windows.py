"""Portable Windows-publication admission; actual OS tests have a dedicated gate."""
import contextlib
import errno
from pathlib import Path
import sys
import tempfile
import types
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

    def test_pin_closes_all_handles_and_preserves_body_error(self):
        primary = ValueError('body failed')
        closed = []
        def close(handle):
            closed.append(handle)
            if handle == 3:
                raise OSError('close failed')
        with mock.patch.object(windows, '_api'), \
             mock.patch.object(windows, '_path', return_value=Path('/a/b')), \
             mock.patch.object(windows, '_open', side_effect=[1, 2, 3]), \
             mock.patch.object(windows, '_attributes', return_value=windows._DIRECTORY), \
             mock.patch.object(windows, '_close', side_effect=close):
            with self.assertRaises(ValueError) as caught:
                with windows.pinned_directory('/a/b'):
                    raise primary
        self.assertIs(caught.exception, primary)
        self.assertEqual(closed, [3, 2, 1])
        self.assertTrue(any('close failed' in note for note in primary.__notes__))

    def test_private_file_close_preserves_body_error(self):
        primary = OSError('destination pin failed')
        @contextlib.contextmanager
        def stage(parent, **kwargs):
            yield types.SimpleNamespace(path=Path('/stage'))
        api = types.SimpleNamespace(CreateFileW=lambda *args: 123)
        crt = types.SimpleNamespace(open_osfhandle=lambda handle, flags: 456)
        with mock.patch.object(windows, '_api', return_value=api), \
             mock.patch.object(windows, 'private_stage', stage), \
             mock.patch.object(windows, '_attributes', return_value=0), \
             mock.patch.object(windows.os, 'O_BINARY', 0, create=True), \
             mock.patch.object(windows.os, 'close', side_effect=OSError('descriptor close failed')), \
             mock.patch.dict(sys.modules, {'msvcrt': crt}):
            with self.assertRaises(OSError) as caught:
                with windows.private_file('/parent'):
                    raise primary
        self.assertIs(caught.exception, primary)
        self.assertTrue(any('descriptor close failed' in note for note in primary.__notes__))

    def test_publication_names_reject_traversal_devices_and_streams(self):
        for name in ('', '.', '..', '../target', 'a/b', 'a\\b', 'a:stream',
                     'NUL', 'CON.json', 'target.', 'target ', 'a\0b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                windows._component(name)
        self.assertEqual(windows._component('target-01.json'), 'target-01.json')


if __name__ == '__main__': unittest.main()
