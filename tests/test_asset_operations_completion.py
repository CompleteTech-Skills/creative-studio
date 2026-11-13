"""Canonical duplication and unbind facade acceptance with exact immutable bytes."""
from copy import deepcopy
import os
import subprocess
import sys
import unittest
import test_asset_operations as operations
from asset_library_operations import AssetOperations
from rig_errors import RigError


class CompletionOperationsTests(unittest.TestCase):
    setUp=operations.AssetOperationsTests.setUp

    def test_duplicate_process_exit_preserves_exact_source_and_reuses_new_identity(self):
        ref=self.fixture.register();identity=dict(self.fixture.manifest['identity'],id='triangle-copy',slug='triangle-copy',name='Independent copy')
        result=self.owner.execute('version.duplicate',dict(reference=ref,identity=identity),apply=True)
        job=result['result'];self.assertEqual(job['status'],'queued')
        code='''import os,sys
from asset_library_operations import AssetOperations
from asset_library_management import ManagementAdapter
owner=AssetOperations(sys.argv[1]);record=owner.job_status(sys.argv[2])
class ExitAfterPublish(ManagementAdapter):
 def publish(self,*args):
  result=super().publish(*args);os._exit(77)
owner.jobs.run(record['id'],ExitAfterPublish(owner,'version',record['parameters']))
'''
        process=subprocess.run([sys.executable,'-c',code,str(self.library.root),job['id']],env=os.environ.copy(),check=False)
        self.assertEqual(process.returncode,77)
        self.owner.resume_job(job['id']);completed=self.owner.run_job(job['id'])
        self.assertEqual(completed['status'],'published')
        copied=completed['receipt']['references'][0];manifest=self.library.resolve(copied)
        self.assertEqual(manifest['identity'],identity)
        self.assertIn(ref,manifest['provenance']['parents'])
        self.assertEqual(manifest['provenance']['fixture'],self.fixture.manifest['provenance']['fixture'])
        self.assertEqual(manifest['provenance']['license_evidence'],self.fixture.manifest['provenance']['license_evidence'])
        self.assertEqual(manifest['previews'],[])
        self.assertEqual(manifest['publication']['integration'],'not-exercised')
        for file in self.fixture.manifest['files']:
            self.assertEqual(self.library.read_file(copied,file['path']),self.library.read_file(ref,file['path']))
        self.assertEqual(self.library.resolve(ref),self.fixture.manifest)
        self.assertEqual(self.owner.run_job(job['id'])['receipt'],completed['receipt'])
        self.assertEqual(len(self.library.list_references()),2)

    def test_duplicate_cannot_change_anatomy_attribution_or_overwrite_identity(self):
        ref=self.fixture.register()
        for patch in [dict(kind='character'),dict(attribution='new attribution'),dict(id='triangle')]:
            identity=dict(self.fixture.manifest['identity'],id='new-copy',slug='new-copy',name='Copy');identity.update(patch)
            job=self.owner.submit('version',dict(action='duplicate',reference=ref,identity=identity))
            with self.assertRaises(RigError):self.owner.run_job(job['id'])
            self.owner.cancel_job(job['id'])
        self.assertEqual(self.library.list_references(),[ref])
