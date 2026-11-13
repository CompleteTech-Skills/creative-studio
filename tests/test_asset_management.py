"""Actual managed canonical mutations and process-exit recovery."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_asset_library as fixtures
from asset_library_contracts import seal
from asset_library_operations import AssetOperations
from asset_library_versions import AssetVersions
from asset_library_dependencies import DependencyGraph,locator,replacement_plan
from rig_errors import RigError


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AssetLibraryTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.library=self.fixture.library
        self.old=self.fixture.register();self.owner=AssetOperations(self.library)

    def candidate(self,version='1.0.1'):
        manifest=deepcopy(self.fixture.manifest);manifest['version']=version
        manifest['provenance']['parents']=[self.old];manifest['metadata']['description']='Revised description'
        return seal(manifest)

    def create_request(self):
        return dict(action='create',manifest=self.candidate(),sources={k:str(v) for k,v in self.fixture.sources.items()},previous=self.old)

    def test_create_uses_native_semver_and_preserves_originals(self):
        before=self.fixture.mesh.read_bytes();p=self.create_request()
        job=self.owner.submit('version',p);self.assertEqual(len(self.library.list_references()),1)
        result=self.owner.run_job(job['id']);self.assertEqual(result['status'],'published')
        self.assertEqual(len(self.library.list_references()),2)
        self.assertEqual(self.owner.run_job(job['id'])['receipt'],result['receipt'])
        self.assertEqual(self.fixture.mesh.read_bytes(),before)
        self.assertEqual(AssetVersions(self.library).history(self.old['id'])[-1]['version'],'1.0.1')

    def test_request_cannot_supply_internal_baseline_or_source_admission_evidence(self):
        for field,value in [('baseline',[]),('input_hashes',{}),('lifecycle',{})]:
            p=self.create_request();p[field]=value
            with self.assertRaises(RigError) as caught:self.owner.submit('version',p)
            self.assertEqual(caught.exception.report['code'],'LIBRARY_MANAGEMENT_REQUEST')
        self.assertEqual(len(self.library.list_references()),1)

    def test_source_changes_after_submission_cannot_publish(self):
        job=self.owner.submit('version',self.create_request())
        self.fixture.mesh.write_bytes(b'changed input')
        with self.assertRaises(RigError):self.owner.run_job(job['id'])
        self.assertEqual(len(self.library.list_references()),1)

    def test_other_writer_identity_change_fails_cas(self):
        job=self.owner.submit('version',self.create_request())
        newer=self.candidate('1.0.2');AssetVersions(self.library).create(newer,self.fixture.sources,previous=self.old)
        with self.assertRaises(RigError):self.owner.run_job(job['id'])
        self.assertEqual({r['version'] for r in self.library.list_references()},{'1.0.0','1.0.2'})

    def crash_after_publish(self,operation,parameters):
        job=self.owner.submit(operation,parameters)
        code='''import os,sys
from asset_library_operations import AssetOperations
from asset_library_management import ManagementAdapter
owner=AssetOperations(sys.argv[1]);record=owner.job_status(sys.argv[2])
class CrashAdapter(ManagementAdapter):
 def publish(self,*args):
  super().publish(*args);os._exit(81)
owner.jobs.run(record['id'],CrashAdapter(owner,record['kind'],record['parameters']))
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.library.root),job['id']],env=dict(os.environ,PYTHONPATH='scripts:tests'))
        self.assertEqual(result.returncode,81)
        self.assertEqual(self.owner.job_status(job['id'])['status'],'interrupted')
        self.owner.resume_job(job['id'])
        return self.owner.run_job(job['id'])

    def test_create_process_exit_after_commit_recovers_without_duplicate_version(self):
        result=self.crash_after_publish('version',self.create_request())
        self.assertEqual(result['status'],'published');self.assertEqual(len(self.library.list_references()),2)

    def test_promote_event_process_exit_recovers_without_duplicate_event_then_rollback(self):
        first=self.owner.submit('version',dict(action='promote',reference=self.old,expected_revision=0))
        self.owner.run_job(first['id'])
        creation=self.owner.submit('version',self.create_request());created=self.owner.run_job(creation['id'])['receipt']['references'][0]
        promoted=self.crash_after_publish('version',dict(action='promote',reference=created,expected_revision=1))
        self.assertEqual(promoted['status'],'published')
        versions=AssetVersions(self.library);self.assertEqual(versions.state(self.old['id'])['revision'],2)
        rollback=self.owner.submit('version',dict(action='rollback',asset_id=self.old['id'],expected_revision=2))
        self.owner.run_job(rollback['id'])
        self.assertEqual(versions.state(self.old['id'])['promoted'],self.old)
        self.assertEqual(versions.state(self.old['id'])['revision'],3)

    def test_lifecycle_conflict_preserves_other_event(self):
        job=self.owner.submit('version',dict(action='promote',reference=self.old,expected_revision=0))
        AssetVersions(self.library).promote(self.old,expected_revision=0,reason='Other explicit writer')
        with self.assertRaises(RigError):self.owner.run_job(job['id'])
        self.assertEqual(AssetVersions(self.library).state(self.old['id'])['revision'],1)

    def test_exact_index_blob_repair_recovers_process_exit(self):
        record=self.fixture.files[0];before=self.fixture.mesh.read_bytes()
        with self.library._connect() as db:db.execute('UPDATE blobs SET payload=? WHERE sha256=?',(b'broken',record['sha256']))
        result=self.crash_after_publish('repair',dict(source=str(self.fixture.mesh),digest=record['sha256'],expected_bytes=record['bytes']))
        self.assertEqual(result['status'],'published')
        self.assertEqual(self.library.read_file(self.old,record['path']),before)
        self.assertEqual(self.fixture.mesh.read_bytes(),before)

    def test_replacement_runs_native_plan_and_preserves_previous_consumer(self):
        changed=self.candidate();new=AssetVersions(self.library).create(changed,self.fixture.sources,previous=self.old)
        manifest=deepcopy(self.fixture.manifest)
        manifest['identity']=dict(manifest['identity'],id='consumer',slug='consumer',name='Consumer')
        manifest['dependencies']=[dict(target=self.old,purpose='mesh',required=True,origin='authored',relationship='direct',capabilities=[])]
        consumer=self.library.register(seal(manifest),self.fixture.sources)
        graph=DependencyGraph({'local':self.library})
        plan=replacement_plan(graph,locator('local',self.old),locator('local',new),consumers=[dict(asset=locator('local',consumer),purposes=['mesh'])])
        result=self.crash_after_publish('replace',dict(plan=plan,versions={'consumer':'1.0.1'}))
        self.assertEqual(result['status'],'published')
        refs=[r for r in result['receipt']['references'] if r['id']=='consumer']
        self.assertEqual(self.library.resolve(refs[0])['dependencies'][0]['target'],new)
        self.assertEqual(self.library.resolve(consumer)['dependencies'][0]['target'],self.old)


if __name__=='__main__':unittest.main()
