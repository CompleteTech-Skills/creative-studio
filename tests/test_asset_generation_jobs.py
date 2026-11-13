"""Admission and source immutability for the canonical bounded native generator."""
from copy import deepcopy
import unittest
import test_asset_library as foundation
from asset_library_operations import AssetOperations
from rig_errors import RigError


class GenerationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=foundation.AssetLibraryTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.owner=AssetOperations(self.fixture.library)
        self.request=dict(identity=self.fixture.manifest['identity'],version='1.0.0',metadata=self.fixture.manifest['metadata'],
            license_file=str(self.fixture.license),fixture=True,parameters=dict(primitive='box',dimensions=[1,2,3],color=[.2,.4,.6,1],animated=True))

    def test_bounded_generation_queues_without_running_or_publishing(self):
        job=self.owner.submit('generate',self.request)
        self.assertEqual(job['status'],'queued');self.assertIn('blender',job['resources'])
        self.assertEqual(self.fixture.library.list_references(),[])
        self.assertEqual(self.owner.submit('generate',self.request)['id'],job['id'])
        self.assertIn('generate',self.owner.capabilities()['managed_operations'])

    def test_invalid_native_profiles_and_provider_substitution_do_not_create_jobs(self):
        for patch in [dict(primitive='text-to-model'),dict(dimensions=[0,1,1]),dict(color=[1,1,1]),dict(primitive='sphere',dimensions=[1,2,3]),dict(animated='yes'),dict(dimensions=[10**400]*3),dict(color=[10**400]*4)]:
            p=deepcopy(self.request);p['parameters'].update(patch)
            with self.assertRaises(RigError):self.owner.submit('generate',p)
        p=deepcopy(self.request);p['provider']='remote-model'
        with self.assertRaises(RigError):self.owner.submit('generate',p)
        self.assertFalse(self.owner.jobs_root.exists())
        self.assertEqual(self.fixture.library.list_references(),[])

    def test_license_changes_after_queue_cannot_run_backend_or_publish(self):
        job=self.owner.submit('generate',self.request)
        self.fixture.license.write_bytes(b'changed license')
        with self.assertRaises(RigError) as caught:self.owner.run_job(job['id'])
        self.assertEqual(caught.exception.report['code'],'LIBRARY_OPERATIONS_STALE')
        self.assertEqual(self.fixture.library.list_references(),[])
