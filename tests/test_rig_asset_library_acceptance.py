"""Real mandatory acceptance integration; enable only with the pinned native tools."""
import os
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('HATCH_ASSET_LIBRARY_ACCEPTANCE') == '1',
                     'Set HATCH_ASSET_LIBRARY_ACCEPTANCE=1 with pinned Blender/Khronos for full acceptance')
class AssetLibraryAcceptanceTests(unittest.TestCase):
    def test_actual_two_scene_restore_render_and_missing_payload_repair(self):
        from demonstrate_asset_library_acceptance import demonstrate
        with tempfile.TemporaryDirectory(prefix='asset-library-acceptance-') as temporary:
            output=Path(temporary)/'evidence'
            result=demonstrate(output,os.environ['HATCH_BLENDER'])
            self.assertTrue(result['passed'])
            self.assertEqual(len(result['references']),9)
            self.assertTrue(result['negative']['exact_recovery'])
            self.assertFalse(result['negative']['diagnostic']['verified'])
            self.assertGreater(result['source_fixture']['deformation_displacement_m'],.01)
            for phase in ('original','restored'):
                self.assertEqual(sum(map(len,result['renders'][phase].values())),4)
            for metrics in result['metrics'].values():
                self.assertGreater(metrics['motion_mean_pixel_delta'],.01)
                self.assertEqual(metrics['restored_max_pixel_delta'],0)
            self.assertTrue((output/'manifest.json').is_file())


if __name__=='__main__':unittest.main()
