"""Expected failures: the first verify+load correction still repairs PNG rows."""
import ast,subprocess,sys,unittest,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
import rig_import
from test_rig_texture_integrity import TextureIntegrityTests,png_raster,png_chunk
BASE='fe467c0e0835a5aa06a989cc27bac2f25de9ba57'
source=subprocess.check_output(['git','show',BASE+':scripts/rig_import.py'],cwd=ROOT,text=True)
function=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_decode_texture')
scope=dict(rig_import.__dict__);exec(compile(ast.Module(body=[function],type_ignores=[]),BASE+':scripts/rig_import.py','exec'),scope)
rig_import._decode_texture=scope['_decode_texture']
class Control(unittest.TestCase):
 def test_incomplete_pngs_are_rejected(self):
  f=TextureIntegrityTests();f.setUp();self.addCleanup(f.doCleanups)
  row=b'\0'+b'\xff\0\0'*2; compressed=zlib.compress(row+row)
  valid=png_raster(2,2,compressed)
  cases=[png_raster(2,2,zlib.compress(row)),png_raster(2,2,compressed[:-4]), valid[:-1]+bytes([valid[-1]^1])]
  for data in cases:
   for path in [f.model(data),f.obj(data)]:
    with self.subTest(header=data[-8:].hex(),path=path.name):
     with self.assertRaises(rig_import.RigError):rig_import.load_character_model(path)
if __name__=='__main__':
 result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Control));sys.exit(not result.wasSuccessful())
