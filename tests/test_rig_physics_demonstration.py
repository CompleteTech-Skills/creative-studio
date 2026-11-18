"""Real native demonstration programs, including their final publication."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import atomic_publish
import physics_backends as core


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Explicit real physics demonstration gate')
class NativeDemonstrationTests(unittest.TestCase):
    @contextmanager
    def workspace(self):
        evidence=os.environ.get('HATCH_PHYSICS_DEMO_EVIDENCE')
        if evidence:
            root=Path(evidence);root.mkdir(parents=True,exist_ok=True);yield root
        else:
            with tempfile.TemporaryDirectory() as folder:yield Path(folder)

    def run_case(self,kind):
        backend=core.BlenderBackend() if kind=='blender' else core.PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
        with self.workspace() as root:
            destination=root/(kind+'-demo');changed=[];original=atomic_publish.rename_no_replace_at
            def rename(source_fd,source_name,destination_fd,destination_name):
                if destination_name==destination.name:
                    info=os.fstat(source_fd)
                    stage=next(p for p in root.iterdir() if (p.stat().st_dev,p.stat().st_ino)==(info.st_dev,info.st_ino))
                    stage.rename(root/(kind+'-owned-wrapper'));stage.mkdir()
                    (stage/'foreign-marker').write_bytes(b'preserve replacement');changed.append(stage)
                return original(source_fd,source_name,destination_fd,destination_name)
            with patch.object(atomic_publish,'rename_no_replace_at',side_effect=rename):
                report=core.demonstrate(destination,backend)
            self.assertEqual(report['status'],'executed-and-verified')
            self.assertEqual(len(changed),1)
            self.assertEqual((changed[0]/'foreign-marker').read_bytes(),b'preserve replacement')
            self.assertFalse((destination/'foreign-marker').exists())
            cache=destination/'cache';core.verify_cache(cache)
            captured=core._read_cache(cache)
            exported=core.parse((destination/'animation/transforms.json').read_bytes())
            self.assertEqual(exported['samples'],captured[2]['samples'])
            acceptance=core.parse((destination/'acceptance.json').read_bytes())
            self.assertEqual(acceptance['cache_manifest_sha256'],core.digest((cache/'cache.json').read_bytes()))
            self.assertLessEqual(acceptance['numerical']['continuous_freefall_error_m'],.02)
            self.assertLessEqual(acceptance['numerical']['resting_height_error_m'],.03)
            if kind=='blender':
                self.assertTrue(acceptance['native_consumption_control']['consumption_demonstrated'])
            core._write(root/(kind+'-demo.json'),core.canonical(report))

    def test_real_blender_program_publishes_its_verified_original(self):
        self.run_case('blender')

    def test_real_direct_program_publishes_its_verified_original(self):
        self.run_case('pybullet')
