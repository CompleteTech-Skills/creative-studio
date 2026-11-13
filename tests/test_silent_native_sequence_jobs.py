import sys
from pathlib import Path
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import silent_native_sequence_jobs as native

class NativeSequenceJobContracts(unittest.TestCase):
    def test_unavailable_native_boundary_fails_before_snapshot_access(self):
        with mock.patch.object(native.sequence,'publication_supported',return_value=False),mock.patch.object(native.sequence,'native_source',side_effect=AssertionError('no source access')):
            with self.assertRaisesRegex(ValueError,'unavailable'):
                native.create_native({}, {},'.','job','delivery',snapshot_root='.',expected_backend={})

    def test_invalid_native_resource_inventory_is_typed(self):
        request={'settings':{'resolution':[64,64]}}
        for count in (True,0,10**400,1.5):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):native._admit(request,{'images':[{'file':{'bytes':count}}]},native.controls.DEFAULT_BUDGET)
        with self.assertRaises(ValueError):native._admit({'settings':{'resolution':[10**400,64]}},{'images':[]},native.controls.DEFAULT_BUDGET)

if __name__=='__main__':unittest.main()
