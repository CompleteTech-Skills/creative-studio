"""Actual Windows handle checks; invoke explicitly on the Windows CI runner."""
import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import decomposition_analysis as analysis


class NativeWindowsRecordTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'win32':
            raise RuntimeError('Windows handle suite requires a Windows runner')
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home=Path(temporary.name)

    def test_windows_native_record_reader_preserves_bytes_and_rejects_nonfiles(self):
        record=self.home/'native.json';payload=b'{"line":"first\\nsecond"}\r\n'
        record.write_bytes(payload)
        for _ in range(32):
            self.assertEqual(analysis._record_bytes(record),payload)
        # Successful read handles must close exactly once and release share locks.
        record.rename(self.home/'renamed.json')
        with self.assertRaises(ValueError):
            analysis._record_bytes(self.home)
        link=self.home/'record-link.json'
        try:
            link.symlink_to(self.home/'renamed.json')
        except (OSError,NotImplementedError):
            self.skipTest('Windows symlink creation privilege unavailable; native reads/directory rejection passed')
        with self.assertRaises(ValueError):
            analysis._record_bytes(link)


if __name__=='__main__':unittest.main()
