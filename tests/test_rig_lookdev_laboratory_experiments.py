"""Safety gates preserve the evidence needed to diagnose failed native jobs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import lookdev_laboratory_experiments as lab
from rig_errors import RigError

class ExperimentGates(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.source=self.root/'repo'
        for name,data in {'scripts/helper.py':b'FROZEN=1\n','profiles/example.json':b'{}',
            'examples/lookdev-contracts/ceramic.json':b'{}','tests/fixtures/lookdev_laboratory_shadows.py':b'# fixed fixture\n'}.items():
            p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        self.output=self.root/'evidence'

    def test_asset_selected_python_path_is_not_an_experiment(self):
        with self.assertRaises(RigError):lab.run('../../asset.py',self.output,'blender')
        self.assertFalse(self.output.exists())

    def test_timeout_retains_partial_native_log(self):
        failure=subprocess.TimeoutExpired(['blender'],1200,output=b'frame0 rendered\n')
        with patch.object(lab,'ROOT',self.source),patch.object(lab.subprocess,'run',side_effect=failure):
            with self.assertRaises(RigError) as raised:
                lab.run('shadows',self.output,'blender',temporary_root=self.root/'owned-tmp')
        self.assertEqual(raised.exception.report['code'],'LOOKDEV_LAB_TIMEOUT')
        self.assertEqual((self.output/'render.log').read_bytes(),b'frame0 rendered\n')

    def test_execution_uses_snapshot_and_rejects_concurrent_source_change(self):
        def execute(command,**kwargs):
            job=json.loads((self.output/'job.json').read_text())
            self.assertEqual(Path(job['scripts']),self.output/'source/scripts')
            self.assertEqual(Path(command[command.index('--python')+1]),self.output/'source/tests/fixtures/lookdev_laboratory_shadows.py')
            self.assertEqual((self.output/'source/scripts/helper.py').read_bytes(),b'FROZEN=1\n')
            self.assertTrue(Path(kwargs['env']['TMPDIR']).is_dir())
            (self.source/'scripts/helper.py').write_bytes(b'CHANGED=2\n')
            (self.output/'report.json').write_text('{}')
            return SimpleNamespace(returncode=0,stdout=b'actual worker log')
        with patch.object(lab,'ROOT',self.source),patch.object(lab.subprocess,'run',side_effect=execute):
            with self.assertRaises(RigError) as raised:
                lab.run('shadows',self.output,'blender',temporary_root=self.root/'owned-tmp')
        self.assertEqual(raised.exception.report['code'],'LOOKDEV_LAB_SOURCE_CHANGED')
        self.assertEqual((self.output/'render.log').read_bytes(),b'actual worker log')

if __name__=='__main__':unittest.main()
