"""Portable shared-publication contracts: required discovery has no Linux skips."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import atomic_publish as atomic


class PortablePublicationTests(unittest.TestCase):
    def test_default_legacy_rename_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';target=root/'target'
            source.write_bytes(b'legacy source');atomic._rename_no_replace(source,target)
            self.assertEqual(target.read_bytes(),b'legacy source');self.assertFalse(source.exists())
            source.write_bytes(b'preserve source')
            with self.assertRaises(FileExistsError):atomic._rename_no_replace(source,target)
            self.assertEqual(source.read_bytes(),b'preserve source');self.assertEqual(target.read_bytes(),b'legacy source')

    def test_unsupported_capability_never_creates_staging_or_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'original').write_text('user data')
            with mock.patch.object(atomic,'private_stage_supported',return_value=False):
                with self.assertRaises(OSError) as error:
                    with atomic.private_stage(0):self.fail('unsupported stage yielded')
                self.assertEqual(error.exception.errno,errno.ENOTSUP)
                with self.assertRaises(OSError) as error:
                    with atomic.pinned_directory(root):self.fail('unsupported directory yielded')
                self.assertEqual(error.exception.errno,errno.ENOTSUP)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['original'])
            self.assertEqual((root/'original').read_text(),'user data')

    def test_actual_platform_capability_contract_without_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            if atomic.private_stage_supported():
                with atomic.pinned_directory(root) as parent:
                    with atomic.private_stage(parent) as (stage,fd):
                        self.assertEqual(stage,Path('/proc/self/fd')/str(fd))
                        (stage/'owned').write_text('temporary');actual=Path(os.readlink(stage))
                self.assertTrue(actual.is_dir());self.assertEqual(list(actual.iterdir()),[])
            else:
                with self.assertRaises(OSError) as caught:
                    with atomic.private_stage(0):self.fail('unsupported stage yielded')
                self.assertEqual(caught.exception.errno,errno.ENOTSUP)
                self.assertEqual(list(root.iterdir()),[])

    def test_helper_import_has_no_optional_runtime_dependencies(self):
        scripts=str(Path(__file__).resolve().parents[1]/'scripts')
        code='import sys; sys.path.insert(0,sys.argv[1]); import atomic_publish; assert not any(n in sys.modules for n in ("PIL","numpy","cv2","bpy"))'
        result=subprocess.run([sys.executable,'-c',code,scripts],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
