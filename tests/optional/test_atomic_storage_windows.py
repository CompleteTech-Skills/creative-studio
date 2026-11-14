"""Actual Windows byte readers, real-file publication and kernel writer locks."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import atomic_windows as windows
import atomic_publish as atomic


class ActualStoragePrimitives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not windows.supported():raise RuntimeError('actual Windows storage gate requires Windows')

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()

    def test_registered_reader_and_incremental_inventory(self):
        (self.root/'file').write_bytes(b'actual bytes');(self.root/'directory').mkdir()
        with windows.pinned_directory(self.root) as pin:
            self.assertIs(windows.require_directory_pin(pin),pin)
            with pin.entries() as entries:self.assertEqual(dict(entries),{'file':'file','directory':'directory'})
            with pin.pin_file('file') as (stream,info):
                self.assertEqual(stream.read(),b'actual bytes');self.assertEqual(info.st_size,12)
                with self.assertRaises(OSError):(self.root/'file').rename(self.root/'replacement')
        with self.assertRaises(ValueError):windows.require_directory_pin(pin)
        with self.assertRaises(ValueError):windows.require_directory_pin(self.root)

    def test_real_zip_file_is_hash_verified_and_exclusively_published(self):
        with atomic.windows_private_stage(self.root) as stage:
            source=stage.path/'archive'
            with zipfile.ZipFile(source,'w') as archive:archive.writestr('original.txt',b'actual original')
            raw=source.read_bytes();digest=hashlib.sha256(raw).hexdigest()
            with self.assertRaises(ValueError):stage.publish_file('archive',self.root/'bad.zip',sha256='0'*64,bytes=len(raw),max_bytes=10000)
            self.assertFalse((self.root/'bad.zip').exists())
            stage.publish_file('archive',self.root/'result.zip',sha256=digest,bytes=len(raw),max_bytes=10000)
        self.assertTrue((self.root/'result.zip').is_file())
        self.assertEqual((self.root/'result.zip').read_bytes(),raw)
        with zipfile.ZipFile(self.root/'result.zip') as archive:self.assertEqual(archive.read('original.txt'),b'actual original')

    def test_writer_lock_is_nonblocking_and_inode_persists(self):
        command=[sys.executable,'-c',
            "import sys;sys.path.insert(0,sys.argv[1]);import atomic_publish as a\n"
            "try:\n with a.windows_writer_lock(sys.argv[2]):pass\n"
            "except BlockingIOError:sys.exit(7)\n",str(Path(atomic.__file__).parent),str(self.root)]
        with atomic.windows_writer_lock(self.root) as pin:
            self.assertEqual(pin.path,self.root)
            before=(self.root/'.execution-retention.lock').stat()
            result=subprocess.run(command,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,7,result.stderr)
            with self.assertRaises(OSError):(self.root/'.execution-retention.lock').rename(self.root/'replacement')
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        after=(self.root/'.execution-retention.lock').stat()
        self.assertEqual((before.st_dev,before.st_ino),(after.st_dev,after.st_ino))


if __name__=='__main__':unittest.main()
