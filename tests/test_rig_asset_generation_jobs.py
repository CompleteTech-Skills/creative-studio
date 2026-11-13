"""Real Blender generated geometry, animation, provenance and job recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_asset_generation_jobs as admission
from asset_library_operations import OperationAdapter
from rig_errors import decode_json


class NativeGenerationJobsTests(unittest.TestCase):
    setUp=admission.GenerationAdmissionTests.setUp

    def test_box_and_sphere_actual_generation_and_exact_recovery(self):
        for primitive in ('box','sphere'):
            p=deepcopy(self.request);p['identity']=dict(p['identity'],id=primitive,slug=primitive,name='Generated '+primitive)
            p['parameters']['primitive']=primitive;p['parameters']['dimensions']=[1,1,1]
            before=self.fixture.license.read_bytes()
            job=self.owner.submit('generate',p)
            if primitive=='box':
                code='''import os,sys
from asset_library_operations import AssetOperations,OperationAdapter
owner=AssetOperations(sys.argv[1]);record=owner.job_status(sys.argv[2])
class ExitAfterPublish(OperationAdapter):
 def publish(self,*args):
  result=super().publish(*args);os._exit(78)
owner.jobs.run(record['id'],ExitAfterPublish(owner,'generate',record['parameters']))
'''
                result=subprocess.run([sys.executable,'-c',code,str(self.fixture.library.root),job['id']],env=os.environ.copy(),check=False)
                self.assertEqual(result.returncode,78)
                self.owner.resume_job(job['id'])
            result=self.owner.run_job(job['id']);self.assertEqual(result['status'],'published')
            reference=result['receipt']['reference'];manifest=self.fixture.library.resolve(reference)
            report=decode_json(self.fixture.library.read_file(reference,'evidence/import.json'))
            self.assertEqual(report['generator']['parameters'],p['parameters'])
            self.assertGreater(report['native_observation']['vertices'],0)
            self.assertTrue(report['independent_output']['ok'])
            self.assertEqual(manifest['provenance']['fixture'],True)
            self.assertEqual(manifest['metadata']['review_status'],'unreviewed')
            self.assertEqual(self.fixture.library.read_file(reference,'source/license.txt'),before)
            self.assertEqual(self.owner.run_job(job['id'])['receipt'],result['receipt'])
            from asset_library_import import _gltf_json
            document,_=_gltf_json(self.fixture.library.read_file(reference,'runtime/asset.glb'),'glb')
            self.assertTrue(document.get('animations'))
        self.assertEqual(len(self.fixture.library.list_references()),2)
