"""Portable spelling stays identical while native workers need only stdlib."""
import importlib.util
import pathlib
import subprocess
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import portable_paths
import decomposition_package
import rig_errors

class PortablePathTests(unittest.TestCase):
    def test_transport_reexports_single_authority(self):
        self.assertIs(decomposition_package._relative, portable_paths.existing_relative)
        self.assertIs(decomposition_package._RESERVED, portable_paths._RESERVED)
        for name in ['mesh.glb', 'models/Milo rig/asset_1.obj', 'café/数据.bin', 'a..b']:
            self.assertEqual(portable_paths.existing_relative(name), name)
            self.assertEqual(rig_errors.portable_name(name), name)

    def test_rejections_match_both_public_owners(self):
        for name in [None, 1, '', '.', '..', '../escape', '/absolute', './alias', 'a//b',
                     'a/', 'a\\b', 'C:drive', 'a:stream', 'tail.', 'tail ', 'nul.txt',
                     'COM1.foo', 'x/LPT9', 'conin$', 'a\x00b', 'a\nb', 'a\x7fb',
                     'a?b', 'a*b', 'a|b', 'a<b', 'a>b', 'a"b', 'a\ud800b']:
            with self.subTest(name=repr(name)):
                with self.assertRaises(ValueError): portable_paths.existing_relative(name)
                with self.assertRaises(ValueError): decomposition_package._relative(name)
                with self.assertRaises(rig_errors.RigError): rig_errors.portable_name(name)

    def test_stdlib_worker_opens_payload_without_imaging_dependencies(self):
        scripts=str(pathlib.Path(__file__).resolve().parents[1] / 'scripts')
        code='''import pathlib,sys,tempfile
sys.path.insert(0,sys.argv[1])
import rig_errors
with tempfile.TemporaryDirectory() as folder:
    root=pathlib.Path(folder).resolve(); (root/'payload.bin').write_bytes(b'actual stdlib worker bytes')
    with rig_errors.open_payload(root,'payload.bin') as (stream, _):
        assert stream.read()==b'actual stdlib worker bytes'
assert not any(name=='PIL' or name.startswith('PIL.') for name in sys.modules)
assert 'decomposition_package' not in sys.modules
'''
        result=subprocess.run([sys.executable,'-S','-c',code,scripts],text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__': unittest.main()
