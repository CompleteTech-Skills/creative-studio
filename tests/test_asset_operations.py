"""Real canonical asset operations, managed publication and local CLI boundaries."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

from asset_library_operations import AssetOperations, OperationAdapter, ScopedAssetOperations
from asset_library_jobs import JobManager
from rig_errors import RigError
import test_asset_library as fixtures
import assets


class AssetOperationsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AssetLibraryTests();self.fixture.setUp()
        self.root=self.fixture.root;self.library=self.fixture.library
        self.owner=AssetOperations(self.library)
        self.addCleanup(self.fixture.doCleanups)

    def test_apply_dry_run_exact_read_and_registry_reopen(self):
        p=dict(manifest=self.fixture.manifest,sources={k:str(v) for k,v in self.fixture.sources.items()})
        with self.assertRaises(RigError) as caught:self.owner.execute('register',p)
        self.assertEqual(caught.exception.report['code'],'LIBRARY_OPERATIONS_APPLY')
        self.owner.execute('register',p,dry_run=True)
        self.assertEqual(self.library.list_references(),[])
        ref=self.owner.execute('register',p,apply=True)['result']
        reopened=AssetOperations(self.library.root)
        self.assertEqual(reopened.execute('show',dict(reference=ref))['result'],self.fixture.manifest)
        changed=dict(ref,version='2.0.0')
        with self.assertRaises(RigError):reopened.execute('show',dict(reference=changed))
        self.assertEqual(self.library.read_file(ref,'mesh/triangle.obj'),self.fixture.mesh.read_bytes())

    def test_real_index_staged_published_and_reused_without_duplicate_records(self):
        ref=self.fixture.register();target=self.root/'index'
        request=dict(index=str(target),locations=[dict(library='local',reference=ref)])
        first=self.owner.submit('index',request)
        result=self.owner.run_job(first['id'])
        self.assertEqual(result['status'],'published')
        self.assertEqual(self.owner.run_job(first['id'])['receipt'],result['receipt'])
        self.assertEqual(self.owner.submit('index',request)['id'],first['id'])
        self.assertEqual(len(self.owner.list_jobs()),1)
        search=self.owner.execute('search',dict(index=str(target),query='triangle'))['result']
        self.assertEqual(search['total'],1)
        self.assertEqual(len(self.library.list_references()),1)

    def test_real_index_crash_after_publication_recovers_without_replacing_destination(self):
        ref=self.fixture.register();target=self.root/'index'
        p=dict(index=str(target),locations=[dict(library='local',reference=ref)])
        job=self.owner.submit('index',p)
        code='''import os,sys
from asset_library_operations import AssetOperations,OperationAdapter
owner=AssetOperations(sys.argv[1]);record=owner.job_status(sys.argv[2])
class ExitAfterPublish(OperationAdapter):
 def publish(self,*args):
  result=super().publish(*args);os._exit(79)
owner.jobs.run(record['id'],ExitAfterPublish(owner,'index',record['parameters']))
'''
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'scripts'))
        result=subprocess.run([sys.executable,'-c',code,str(self.library.root),job['id']],env=env)
        self.assertEqual(result.returncode,79)
        inode=target.stat().st_ino
        self.owner.resume_job(job['id'])
        finished=self.owner.run_job(job['id'])
        self.assertEqual(finished['status'],'published')
        self.assertEqual(target.stat().st_ino,inode)
        self.assertEqual(len(self.library.list_references()),1)

    def test_scoped_http_operations_reject_paths_and_executable_overrides(self):
        intake=self.root/'intake';intake.mkdir();output=self.root/'output';output.mkdir()
        scoped=ScopedAssetOperations(AssetOperations(self.library,jobs_root=output/'jobs'),intake_root=intake,output_root=output)
        for p in [dict(index=str(self.root/'outside')),dict(index=str(output/'..'/'outside')),dict(index=str(output/'index'),blender='/usr/bin/python')]:
            with self.assertRaises(RigError):scoped.submit('index',p)
        secret=self.root/'secret.obj';secret.write_bytes(b'outside intake')
        with self.assertRaises(RigError):
            scoped.submit('import',dict(source=str(intake/'..'/'secret.obj'),license_file=str(secret),identity={},metadata={},version='1.0.0'))
        self.assertEqual(secret.read_bytes(),b'outside intake')
        self.assertFalse((self.root/'outside').exists())

    def test_cancelled_index_keeps_target_absent_then_explicit_resume_publishes(self):
        ref=self.fixture.register();target=self.root/'index'
        job=self.owner.submit('index',dict(index=str(target),locations=[dict(library='local',reference=ref)]))
        planned=assets.run(['--library',str(self.library.root),'jobs','run','--job',job['id'],'--apply','--dry-run'])
        self.assertFalse(planned['result']['executed'])
        self.assertEqual(self.owner.job_status(job['id'])['status'],'queued')
        self.owner.cancel_job(job['id'])
        with self.assertRaises(RigError):self.owner.run_job(job['id'])
        self.assertFalse(target.exists())
        self.owner.resume_job(job['id']);self.owner.run_job(job['id'])
        self.assertTrue(target.is_dir())

    def test_managed_package_restore_reopens_canonical_library(self):
        from asset_library_packages import create_package
        from asset_library_dependencies import DependencyGraph,locator
        ref=self.fixture.register();package=self.root/'package'
        create_package(DependencyGraph({'local':self.library}),[locator('local',ref)],package,package_id='operations-test')
        target=self.root/'restored'
        job=self.owner.submit('bundle.restore',dict(root=str(package),destination=str(target)))
        result=self.owner.run_job(job['id'])
        self.assertEqual(result['status'],'published')
        from asset_library import AssetLibrary
        self.assertEqual(AssetLibrary(target/'library').resolve(ref),self.fixture.manifest)

    def test_package_replacement_between_stage_admission_and_restore_cannot_publish(self):
        import os
        from unittest.mock import patch
        from asset_library_packages import create_package,restore_package
        from asset_library_dependencies import DependencyGraph,locator
        ref=self.fixture.register();graph=DependencyGraph({'local':self.library})
        package=self.root/'package';replacement=self.root/'replacement'
        create_package(graph,[locator('local',ref)],package,package_id='original-package')
        create_package(graph,[locator('local',ref)],replacement,package_id='changed-package')
        target=self.root/'restored';job=self.owner.submit('bundle.restore',dict(root=str(package),destination=str(target)))
        def replace_then_restore(root,destination):
            os.rename(package,self.root/'preserved-original-package')
            os.rename(replacement,package)
            return restore_package(root,destination)
        with patch('asset_library_packages.restore_package',side_effect=replace_then_restore):
            with self.assertRaises(RigError) as caught:self.owner.run_job(job['id'])
        self.assertEqual(caught.exception.report['code'],'LIBRARY_OPERATIONS_STALE')
        self.assertFalse(target.exists())
        self.assertTrue((self.root/'preserved-original-package').is_dir())
        self.assertEqual(len(self.library.list_references()),1)

    def test_index_new_source_revision_cannot_reuse_published_job(self):
        ref=self.fixture.register();p=dict(index=str(self.root/'index'),locations=[dict(library='local',reference=ref)])
        first=self.owner.submit('index',p);self.owner.run_job(first['id'])
        self.library.update_annotations(ref,expected_revision=0,tags=['changed'],favorite=False,note='')
        second=self.owner.submit('index',p)
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(second['status'],'queued')
        p['index']=str(self.root/'index-generation2')
        fresh=self.owner.submit('index',p);self.owner.cancel_job(second['id']);self.owner.run_job(fresh['id'])
        self.assertEqual(self.owner.execute('search',dict(index=p['index'],query='changed'))['result']['total'],1)

    def test_import_admission_reserves_shared_blender_pool_before_conversion(self):
        p=dict(source=str(self.fixture.mesh),identity=self.fixture.manifest['identity'],version='1.0.0',
            metadata=self.fixture.manifest['metadata'],license_file=str(self.fixture.license),fixture=True,blender_roundtrip=False)
        queued=self.owner.submit('import',p)
        self.assertIn('blender',queued['resources'])
        self.assertEqual(queued['status'],'queued')
        self.assertEqual(self.library.list_references(),[])
        ref=self.fixture.register()
        exported=self.owner.submit('export',dict(reference=ref,destination=str(self.root/'export'),format='blend'))
        self.assertIn('blender',exported['resources'])
        self.assertFalse((self.root/'export').exists())

    def test_unknown_operation_and_optional_capability_do_not_fake_success(self):
        for operation in ('remote-upload','generate','semantic-search'):
            with self.assertRaises(RigError):self.owner.execute(operation,{},apply=True)
        self.assertEqual(self.owner.capabilities()['production_approval'],False)

    def test_project_bind_uses_ordered_native_scope_and_preserves_unavailable_unbind(self):
        from asset_library_versions import ScopedResolver
        ref=self.fixture.register();binding=dict(library='local',reference=ref)
        p=dict(project_id='test-project',slot='hero',binding=binding,expected_revision=0)
        result=self.owner.execute('project.bind',p,apply=True)['result']
        self.assertEqual(result['revision'],1)
        resolver=ScopedResolver([('local',self.library)],project_library='local')
        before=resolver.bindings('test-project')
        self.assertEqual(before['bindings']['hero'],binding)
        with self.assertRaises(RigError) as caught:self.owner.execute('project.unbind',dict(project_id='test-project',slot='hero'),apply=True)
        self.assertEqual(caught.exception.report['code'],'LIBRARY_OPERATIONS_UNAVAILABLE')
        self.assertEqual(resolver.bindings('test-project'),before)
        with self.assertRaises(RigError):self.owner.execute('project.bind',p,apply=True)

    def test_browser_cli_delegates_explicit_loopback_server_arguments(self):
        from unittest.mock import patch
        with patch('asset_library_browser.main',return_value=0) as start:
            result=assets.run(['browser','--library',str(self.library.root),'--index',str(self.root/'index'),'--port','0'])
        self.assertTrue(result['ok'])
        self.assertEqual(start.call_args.args[0],['--library',str(self.library.root),'--index',str(self.root/'index'),'--port','0'])

    def test_cli_json_help_mutation_guard_and_exclusive_report(self):
        out=io.StringIO();err=io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):code=assets.main(['library','create','--library',str(self.root/'cli-library')])
        self.assertEqual(code,2);self.assertFalse(json.loads(err.getvalue())['ok'])
        result=assets.run(['library','create','--library',str(self.root/'cli-library'),'--apply'])
        self.assertTrue(result['ok'])
        report=self.root/'report.json'
        assets.run(['--library',str(self.library.root),'library','inspect','--output',str(report)])
        old=report.read_bytes()
        with self.assertRaises(FileExistsError):assets.run(['--library',str(self.library.root),'library','inspect','--output',str(report)])
        self.assertEqual(report.read_bytes(),old)
        for group,actions in assets.GROUPS.items():
            for action in actions:
                with redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as caught:assets.parser().parse_args([group,action,'--help'])
                self.assertEqual(caught.exception.code,0)


if __name__=='__main__':unittest.main()
