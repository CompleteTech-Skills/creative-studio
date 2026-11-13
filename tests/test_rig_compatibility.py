import hashlib
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
FROZEN={
 'profiles/codex-v1.json':'f4e0279f8634ea53ef544ecb49b9a71ec2565c61ec5b8e63163ecc5ead478a6b',
 'profiles/profile.schema.json':'afba01bc16c4f6c297d1b95b62803c7a5b989e0fa9b967c33e5d50471091163c',
 'assets/playback.js':'9aa7d5228de0a57cd443b70c14b160cb22238d21a861a0fb06f6b4bcc6ccbc79',
 'state-library/catalog.json':'970139db7291c4233ef75e37ffd6abe0385c8f165dd37b339326a4e8647f3de9'}


class RigCompatibilityTests(unittest.TestCase):
    def test_existing_contract_bytes_are_unchanged(self):
        for name,digest in FROZEN.items():self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),digest,name)
    def test_archive_imports_preserve_optional_dependency_boundaries(self):
        code="""import sys,importlib.abc
sys.path.insert(0,'scripts')
blocked={'PIL','numpy','jsonschema','bpy','playwright','hatch','production_workflow','production_sources'}
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in blocked:raise ImportError('Unrelated archive dependency: '+fullname)
sys.meta_path.insert(0,Block())
import atomic_publish
blocked.difference_update(('numpy','jsonschema'))
import rig_package
"""
        process=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
    def test_existing_capabilities_work_with_3d_imports_blocked(self):
        code="""import sys,importlib.abc
sys.path.insert(0,'scripts')
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in ('numpy','jsonschema','bpy','playwright'):raise ImportError('3D import forbidden: '+fullname)
sys.meta_path.insert(0,Block())
import hatch
sys.argv=['hatch.py','capabilities'];hatch.main()
"""
        process=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stderr);self.assertIn('rig_3d',process.stdout)
    def test_cli_failure_is_nonzero_and_structured(self):
        process=subprocess.run([sys.executable,'scripts/rig.py','status','--run-dir','/nonexistent/hatch-test-rig'],cwd=ROOT,capture_output=True,text=True)
        self.assertNotEqual(process.returncode,0);self.assertIn('"ok": false',process.stdout);self.assertIn('"error"',process.stdout)
    def test_public_delivery_discovers_all_2d_tests_without_3d_dependencies(self):
        code="""import sys,importlib.abc,unittest
sys.path.insert(0,'scripts')
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in ('numpy','pygltflib','bpy'):raise ImportError('3D import forbidden: '+fullname)
sys.meta_path.insert(0,Block())
from verify_public_delivery import build_regression_suite
def flatten(suite):
 for item in suite:
  if isinstance(item,unittest.TestSuite):yield from flatten(item)
  else:yield item
cases=list(flatten(build_regression_suite()))
ids=[case.id() for case in cases]
failed=[case for case in cases if type(case).__name__=='_FailedTest']
assert not failed,[(case.id(),str(case._exception)) for case in failed]
assert len(ids)>=790,len(ids)
assert len(ids)==len(set(ids)),'duplicate test discovery'
assert not any(name.startswith(('test_rig_', 'test_model3d_', 'test_render3d_')) for name in ids),ids
assert sum(name.startswith('test_production_pilot.') for name in ids)==13
assert any(name.startswith('test_production_workflow.') for name in ids)
assert any(name.startswith('test_decomposition_review.') for name in ids)
assert any(name.startswith('test_production_preview.') for name in ids)
assert not any(name in sys.modules for name in ('numpy','pygltflib','bpy'))
print('Discovered',len(ids),'2D tests without optional 3D dependencies')
"""
        process=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        print(process.stdout.strip())
