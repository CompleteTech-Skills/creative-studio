"""Actual authenticated HTTP intake delegates immutable management to native jobs."""
from copy import deepcopy
import unittest
import shutil

import test_asset_library_browser as browser_fixture
from asset_library_contracts import seal
from asset_library_operations import AssetOperations,ScopedAssetOperations


class ManagedHTTPTests(unittest.TestCase):
    setUp=browser_fixture.BrowserTests.setUp
    request=browser_fixture.BrowserTests.request
    stop=browser_fixture.BrowserTests.stop

    def test_http_version_submission_local_worker_and_exact_status(self):
        intake=self.root/'intake';intake.mkdir();output=self.root/'output';output.mkdir()
        owner=AssetOperations(self.library,jobs_root=output/'jobs')
        self.server.operations=ScopedAssetOperations(owner,intake_root=intake,output_root=output)
        candidate=deepcopy(self.fixture.manifest);candidate['version']='2.0.0'
        candidate['provenance']['parents']=[self.reference];candidate=seal(candidate)
        sources={}
        for name,source in self.fixture.sources.items():
            target=intake/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target);sources[name]=str(target)
        payload=dict(action='create',manifest=candidate,sources=sources,previous=self.reference)
        status,job,_=self.request('/api/actions/version',payload)
        self.assertEqual(status,200,job);self.assertEqual(job['status'],'queued')
        self.assertEqual(len(self.library.list_references()),1)
        completed=owner.run_job(job['id']);self.assertEqual(completed['status'],'published')
        status,state,_=self.request('/api/jobs/status',dict(id=job['id']))
        self.assertEqual(status,200,state);self.assertEqual(state['status'],'published')
        reference=dict(id=candidate['identity']['id'],version='2.0.0',sha256=candidate['manifest_sha256'])
        self.assertEqual(self.library.resolve(reference),candidate)
        self.assertEqual(self.library.read_file(self.reference,'mesh/triangle.obj'),self.fixture.mesh.read_bytes())

    def test_http_management_cannot_forge_journal_baseline_or_escape_intake(self):
        intake=self.root/'intake';intake.mkdir();output=self.root/'output';output.mkdir()
        owner=AssetOperations(self.library,jobs_root=output/'jobs')
        self.server.operations=ScopedAssetOperations(owner,intake_root=intake,output_root=output)
        forged=dict(action='promote',reference=self.reference,expected_revision=0,baseline=[])
        self.assertEqual(self.request('/api/actions/version',forged)[0],400)
        payload=dict(source=str(intake/'..'/'outside'),digest='f'*64,expected_bytes=1)
        self.assertEqual(self.request('/api/actions/repair',payload)[0],400)
        self.assertEqual(self.library.list_references(),[self.reference])
