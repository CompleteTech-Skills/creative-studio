"""Reproduce real admission failures with exact baseline functions in memory."""
import ast
import io
import subprocess
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import rig_import, rig_obj
from test_rig_texture_integrity import TextureIntegrityTests, image_bytes, png_with_bad_pixels
BASE = 'bae65356567a10915658a6371c14ee9a4553db36'

def old_function(path, name, namespace):
    source = subprocess.check_output(['git', 'show', BASE + ':' + path], cwd=ROOT, text=True)
    function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[function], type_ignores=[])
    scope = dict(namespace)
    exec(compile(module, BASE + ':' + path, 'exec'), scope)
    return scope[name]
rig_import._images = old_function('scripts/rig_import.py', '_images', rig_import.__dict__)
rig_obj.import_obj = old_function('scripts/rig_obj.py', 'import_obj', rig_obj.__dict__)
class Control(unittest.TestCase):
    def test_baseline_accepts_real_damaged_pixel_sources(self):
        fixture = TextureIntegrityTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        damaged = image_bytes(fmt='JPEG')[:-2]
        sources = [fixture.model(damaged, '.glb', mime='image/jpeg'),
                   fixture.model(damaged, '.gltf', mime='image/jpeg', external=True),
                   fixture.model(png_with_bad_pixels(), '.gltf'),
                   fixture.obj(damaged, '.jpg'), fixture.obj(png_with_bad_pixels())]
        for path in sources:
            with self.subTest(path=path.name):
                # Expected failure of the security regression under old code.
                with self.assertRaises(rig_import.RigError): rig_import.load_character_model(path)
if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Control))
    sys.exit(not result.wasSuccessful())
