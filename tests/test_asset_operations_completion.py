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

    def test_private_stage_commit_exit_recovers_create_and_duplicate_without_new_versions(self):
        from asset_library_contracts import seal
        reference=self.fixture.register()
        for action in ('create','duplicate'):
            identity=dict(self.fixture.manifest['identity'],id='staged-'+action,slug='staged-'+action,name='Staged '+action)
            if action=='create':
                manifest=deepcopy(self.fixture.manifest);manifest['identity']=identity
                payload=dict(action=action,manifest=seal(manifest),sources={k:str(v) for k,v in self.fixture.sources.items()})
            else:payload=dict(action=action,reference=reference,identity=identity)
            job=self.owner.submit('version',payload)
            code="""import os,sys
from asset_library_operations import AssetOperations
from asset_library_management import StageLibrary
owner=AssetOperations(sys.argv[1]);original=StageLibrary.register
last_id=sys.argv[3]
def exit_after_private_commit(self,manifest,sources,**options):
 result=original(self,manifest,sources,**options)
 if manifest['identity']['id']==last_id:os._exit(75)
 return result
StageLibrary.register=exit_after_private_commit
owner.run_job(sys.argv[2])
"""
            process=subprocess.run([sys.executable,'-c',code,str(self.library.root),job['id'],identity['id']],env=os.environ.copy(),check=False)
            self.assertEqual(process.returncode,75)
            self.assertFalse(any(r['id']==identity['id'] for r in self.library.list_references()))
            self.owner.resume_job(job['id']);result=self.owner.run_job(job['id'])
            self.assertEqual(result['status'],'published')
            selected=[r for r in self.library.list_references() if r['id']==identity['id']]
            self.assertEqual(len(selected),1)
            self.assertEqual(self.library.read_file(selected[0],'mesh/triangle.obj'),self.fixture.mesh.read_bytes())
            self.assertEqual(self.owner.run_job(job['id'])['receipt'],result['receipt'])
        self.assertEqual(len(self.library.list_references()),3)
        self.assertEqual(self.library.resolve(reference),self.fixture.manifest)

    def test_managed_index_preserves_native_collections_and_authorized_project_usage(self):
        from asset_library_versions import AssetVersions,ScopedResolver
        from asset_library_search import SearchIndex
        reference=self.fixture.register();versions=AssetVersions(self.library)
        versions.set_collections(reference,expected_revision=0,collections=['favorites'])
        resolver=ScopedResolver([('local',self.library)],project_library='local')
        resolver.bind('film','hero',dict(library='local',reference=reference),expected_revision=0)
        owner=AssetOperations(self.library,project_ids=['film'])
        target=self.root/'native-index';request=dict(index=str(target))
        first=owner.submit('index',request);owner.run_job(first['id'])
        reopened=SearchIndex(target,{'local':self.library},versions={'local':versions},usage_resolver=resolver,project_ids=['film'])
        self.assertEqual(reopened.search('',filters={'collections':['favorites'],'used_by':['film']})['total'],1)
        versions.set_collections(reference,expected_revision=1,collections=['updated'])
        second=owner.submit('index',request);self.assertNotEqual(second['id'],first['id']);owner.cancel_job(second['id'])
        resolver.unbind('film','hero',expected_revision=1)
        third=owner.submit('index',request);self.assertNotEqual(third['id'],second['id'])
        owner.cancel_job(third['id'])
        from asset_library_operations import ScopedAssetOperations
        intake=self.root/'intake';intake.mkdir();output=self.root/'output';output.mkdir()
        scoped=ScopedAssetOperations(AssetOperations(self.library,jobs_root=output/'jobs'),intake_root=intake,output_root=output)
        with self.assertRaises(RigError):scoped.submit('index',dict(index=str(output/'index'),project_ids=['film']))
        self.assertEqual(len(self.library.list_references()),1)

    def test_index_metadata_race_after_admission_does_not_publish_new_unpinned_revision(self):
        from unittest.mock import patch
        from asset_library_versions import AssetVersions
        from asset_library_search import SearchIndex
        reference=self.fixture.register();versions=AssetVersions(self.library)
        target=self.root/'raced-index';job=self.owner.submit('index',dict(index=str(target)))
        native_rebuild=SearchIndex.rebuild
        def race(index,*args,**kwargs):
            versions.set_collections(reference,expected_revision=0,collections=['raced'])
            return native_rebuild(index,*args,**kwargs)
        with patch('asset_library_search.SearchIndex.rebuild',new=race):
            with self.assertRaises(RigError) as caught:self.owner.run_job(job['id'])
        self.assertEqual(caught.exception.report['code'],'LIBRARY_OPERATIONS_STALE')
        self.assertFalse(target.exists())
        self.assertEqual(versions.collections(reference)['collections'],['raced'])
        self.assertEqual(self.library.resolve(reference),self.fixture.manifest)

    def test_duplicate_cannot_change_anatomy_attribution_or_overwrite_identity(self):
        ref=self.fixture.register()
        for patch in [dict(kind='character'),dict(attribution='new attribution'),dict(id='triangle')]:
            identity=dict(self.fixture.manifest['identity'],id='new-copy',slug='new-copy',name='Copy');identity.update(patch)
            job=self.owner.submit('version',dict(action='duplicate',reference=ref,identity=identity))
            with self.assertRaises(RigError):self.owner.run_job(job['id'])
            self.owner.cancel_job(job['id'])
        self.assertEqual(self.library.list_references(),[ref])
