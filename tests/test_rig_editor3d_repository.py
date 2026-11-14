"""Real native scene persistence, failure recovery and archive regressions."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from editor3d_repository import EditorRepository
from rig_errors import RigError, job_lock
from scene3d_example import build_example


class EditorRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source'
        build_example(self.source)
        self.repo=EditorRepository.create(self.source,self.root/'editor')

    def move(self, x=3):
        return {'schema_version':1,'commands':[{'operation':'transform','parameters':{
            'identifier':'mesh','transform':{'translation':[x,0,0],'rotation':[0,0,0,1],'scale':[1,1,1]}}}]}

    def mesh_x(self, snapshot):
        return next(n for n in snapshot['scene']['nodes'] if n['id']=='mesh')['transform']['translation'][0]

    def test_actual_native_edit_reopens_and_original_payload_survives(self):
        original=(self.source/'triangle.obj').read_bytes();before=self.repo.snapshot()
        result=self.repo.apply(0,self.move(8),'move',expected_sha256=before['sha256'])
        reopened=EditorRepository(self.root/'editor').snapshot()
        self.assertEqual(result,reopened);self.assertEqual(self.mesh_x(reopened),8)
        self.assertEqual(self.repo.read_file('triangle.obj',reopened['sha256']),original)
        self.assertEqual((self.source/'triangle.obj').read_bytes(),original)
        self.assertEqual(self.mesh_x(self.repo.snapshot(0)),2)
        self.assertEqual(result['revision'],1)

    def test_apply_rejects_source_project_replaced_after_revision_validation(self):
        import editor3d_repository as module
        import scene3d_snapshot
        from scene3d_contracts import canonical_bytes, load_project
        for phase in ('before-working-load', 'before-snapshot-load'):
            with self.subTest(phase=phase):
                self.repo = EditorRepository.create(self.source, self.root/('editor-'+phase))
                before = self.repo.snapshot()
                head_bytes = (self.repo.root/'HEAD.json').read_bytes()
                project_path = self.repo.root/'revisions/00000000/native/project.json'
                original = project_path.read_bytes()
                foreign = json.loads(original)
                foreign['name'] = 'A valid but unadmitted project revision'
                replacement = canonical_bytes(foreign)
                observed = []
                def replace_source():
                    project_path.write_bytes(replacement)
                    # This remains a valid native project; rejection must bind
                    # the originally admitted revision, not merely its schema.
                    load_project(project_path.parent)
                    observed.append(phase)
                real_load = module.load_project
                real_snapshot = scene3d_snapshot.snapshot_project
                calls = []
                def load(source):
                    calls.append(source)
                    if phase == 'before-working-load' and len(calls) == 2:
                        replace_source()
                    return real_load(source)
                def snapshot(*args, **kwargs):
                    if phase == 'before-snapshot-load':
                        replace_source()
                    return real_snapshot(*args, **kwargs)
                try:
                    with patch.object(module, 'load_project', side_effect=load), \
                         patch.object(scene3d_snapshot, 'snapshot_project', side_effect=snapshot):
                        with self.assertRaises(RigError) as caught:
                            self.repo.apply(0, self.move(8), 'changed-project-'+phase)
                    self.assertEqual(caught.exception.report['code'], 'SCENE3D_SOURCE_CHANGED')
                    self.assertEqual(observed, [phase])
                    self.assertEqual(project_path.read_bytes(), replacement)
                    self.assertEqual((self.repo.root/'HEAD.json').read_bytes(), head_bytes)
                    self.assertFalse(self.repo.recovery())
                    self.assertEqual(sorted(p.name for p in (self.repo.root/'revisions').iterdir()), ['00000000'])
                finally:
                    project_path.write_bytes(original)
                self.assertEqual(EditorRepository(self.repo.root).snapshot(), before)

    def test_compound_failure_keeps_head_and_native_source_unchanged(self):
        before=self.repo.snapshot();batch=self.move(9)
        batch['commands'].append({'operation':'reparent','parameters':{'identifier':'root','parent':'mesh'}})
        with self.assertRaises(RigError):self.repo.apply(0,batch,'cycle')
        self.assertEqual(self.repo.snapshot(),before)
        self.assertFalse(self.repo.recovery())
        self.assertEqual(len(self.repo.history()),1)

    def test_expected_revision_and_hash_reject_stale_writer(self):
        stale=EditorRepository(self.root/'editor');before=stale.snapshot()
        self.repo.apply(0,self.move(),'first')
        for revision,digest in ((0,before['sha256']),(1,'0'*64)):
            with self.subTest(revision=revision),self.assertRaises(RigError) as caught:
                stale.apply(revision,self.move(7),'stale-'+str(revision),expected_sha256=digest)
            self.assertEqual(caught.exception.report['code'],'EDITOR3D_STALE')
        self.assertEqual(self.repo.snapshot()['revision'],1)

    def test_invalid_installed_native_data_does_not_advance_head(self):
        import editor3d_repository as module
        before = self.repo.snapshot()
        head_bytes = (self.repo.root/'HEAD.json').read_bytes()
        real_write = module._write_metadata
        observed = []
        def write(path, value):
            result = real_write(path, value)
            if path.parent.name == 'journal' and value.get('status') == 'published':
                observed.append(value['revision'])
                native = self.repo.root/f"revisions/{value['revision']:08d}/native/triangle.obj"
                native.write_bytes(b'not the verified native resource')
            return result
        with patch.object(module, '_write_metadata', side_effect=write):
            with self.assertRaises(RigError) as caught:
                self.repo.apply(0, self.move(8), 'invalid-installed-native')
        self.assertEqual(observed, [1])
        self.assertEqual(caught.exception.report['code'], 'EDITOR3D_HASH')
        self.assertEqual((self.repo.root/'HEAD.json').read_bytes(), head_bytes)
        self.assertEqual(EditorRepository(self.repo.root).snapshot(), before)
        journals = list((self.repo.root/'journal').glob('*.json'))
        self.assertEqual(len(journals), 1)
        self.assertEqual(json.loads(journals[0].read_bytes())['status'], 'published')

    def test_installed_record_must_match_the_verified_transaction(self):
        import editor3d_repository as module
        before = self.repo.snapshot()
        head_bytes = (self.repo.root/'HEAD.json').read_bytes()
        real_write = module._write_metadata
        observed = []
        def write(path, value):
            result = real_write(path, value)
            if path.parent.name == 'journal' and value.get('status') == 'published':
                observed.append(value['revision'])
                record_path = self.repo.root/f"revisions/{value['revision']:08d}/record.json"
                record = json.loads(record_path.read_bytes())
                record['transaction_id'] = 'different-valid-transaction'
                real_write(record_path, record)
            return result
        with patch.object(module, '_write_metadata', side_effect=write):
            with self.assertRaises(RigError) as caught:
                self.repo.apply(0, self.move(8), 'installed-record-binding')
        self.assertEqual(observed, [1])
        self.assertEqual(caught.exception.report['code'], 'EDITOR3D_HASH')
        self.assertEqual((self.repo.root/'HEAD.json').read_bytes(), head_bytes)
        self.assertEqual(EditorRepository(self.repo.root).snapshot(), before)
        self.assertEqual(len(list((self.repo.root/'journal').glob('*.json'))), 1)

    def test_idempotent_delivery_does_not_duplicate_undo_history(self):
        first=self.repo.apply(0,self.move(),'same');self.repo.apply(1,self.move(4),'second')
        self.assertEqual(self.repo.apply(0,self.move(),'same'),first)
        self.assertEqual(len(self.repo.history()),3)
        with self.assertRaises(RigError):self.repo.apply(2,self.move(7),'same')

    def test_os_lock_serializes_process_writers(self):
        script='''from editor3d_repository import EditorRepository
from rig_errors import RigError
import sys
r=EditorRepository(sys.argv[1])
try:r.restore_revision(0,0,'concurrent')
except RigError as e:
 print(e.report['code']);sys.exit(0)
raise AssertionError('writer was not excluded')
'''
        with job_lock(self.repo.root):
            result=subprocess.run([sys.executable,'-c',script,str(self.repo.root)],
                env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'scripts')},capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('RIG_BUSY',result.stdout)
        self.assertEqual(self.repo.snapshot()['revision'],0)

    def test_undo_redo_and_divergent_edit_have_durable_separate_revisions(self):
        self.repo.apply(0,self.move(9),'a')
        self.assertEqual(self.mesh_x(self.repo.undo(1,'undo-a')),2)
        self.assertEqual(self.mesh_x(self.repo.redo(2,'redo-a')),9)
        self.repo.undo(3,'undo-b')
        self.repo.apply(4,self.move(6),'divergent')
        with self.assertRaises(RigError):self.repo.redo(5,'redo-invalid')
        self.assertEqual(self.mesh_x(EditorRepository(self.repo.root).snapshot()),6)
        self.assertEqual(len(self.repo.history()),6)

    def test_drag_preview_cancel_and_single_commit(self):
        before=(self.repo.root/'HEAD.json').read_bytes()
        self.repo.begin_drag(0)
        for x in range(3,13):self.assertEqual(self.mesh_x(self.repo.preview_drag(self.move(x))),x)
        self.assertEqual((self.repo.root/'HEAD.json').read_bytes(),before)
        self.repo.cancel_drag();self.assertEqual(self.mesh_x(self.repo.snapshot()),2)
        self.repo.begin_drag(0);self.repo.preview_drag(self.move(20))
        committed=self.repo.commit_drag('drag')
        self.assertEqual(committed['revision'],1);self.assertEqual(len(self.repo.history()),2)

    def test_stale_drag_commit_preserves_preview_for_retry_or_cancel(self):
        self.repo.begin_drag(0);self.repo.preview_drag(self.move(20))
        other=EditorRepository(self.repo.root);other.apply(0,self.move(5),'other')
        with self.assertRaises(RigError):self.repo.commit_drag('stale-drag')
        self.assertEqual(self.mesh_x(self.repo.cancel_drag()),5)

    def test_debounced_autosave_does_not_commit_incomplete_drag_or_erase_undo(self):
        self.repo.apply(0,self.move(),'change');self.repo.request_autosave(now=10)
        self.assertFalse(self.repo.autosave(now=10.5)['saved'])
        self.repo.begin_drag(1);self.repo.preview_drag(self.move(50))
        self.assertFalse(self.repo.autosave(now=20,force=True)['saved'])
        self.repo.cancel_drag();saved=self.repo.autosave(now=20)
        self.assertTrue(saved['saved']);self.assertEqual(saved['revision'],1)
        self.assertEqual(self.mesh_x(self.repo.undo(1,'undo')),2)
        self.assertEqual(json.loads((self.repo.root/'autosave.json').read_text())['revision'],1)

    def test_checkpoint_and_restore_are_conflict_checked(self):
        self.repo.apply(0,self.move(),'move');checkpoint=self.repo.checkpoint(1)
        self.assertEqual(checkpoint['sha256'],self.repo.snapshot()['sha256'])
        with self.assertRaises(RigError):self.repo.checkpoint(0)
        restored=self.repo.restore_revision(0,1,'restore')
        self.assertEqual(restored['revision'],2);self.assertEqual(self.mesh_x(restored),2)

    def fail_head_once(self, write_then_fail=False):
        import editor3d_repository as module
        real=module._write_metadata
        def fail(path,value):
            if path.name=='HEAD.json':
                if write_then_fail:real(path,value)
                raise OSError('injected interruption before acknowledgement')
            return real(path,value)
        return patch.object(module,'_write_metadata',side_effect=fail)

    def test_published_but_unacknowledged_revision_recovers_after_reopen(self):
        with self.fail_head_once(),self.assertRaises(OSError):self.repo.apply(0,self.move(8),'interrupted')
        reopened=EditorRepository(self.repo.root)
        self.assertEqual(reopened.snapshot()['revision'],0)
        self.assertTrue(reopened.recovery()[0]['can_recover'])
        self.assertEqual(self.mesh_x(reopened.recover('interrupted',0)),8)
        self.assertFalse(reopened.recovery())

    def test_interruption_after_head_commit_is_idempotently_reconciled(self):
        with self.fail_head_once(write_then_fail=True),self.assertRaises(OSError):self.repo.apply(0,self.move(8),'after-head')
        reopened=EditorRepository(self.repo.root);self.assertEqual(reopened.snapshot()['revision'],1)
        self.assertTrue(reopened.recovery()[0]['committed'])
        reopened.recover('after-head',1)
        self.assertFalse(reopened.recovery());self.assertEqual(len(reopened.history()),2)

    def test_corrupt_recovery_head_never_replaces_last_good_head(self):
        with self.fail_head_once(),self.assertRaises(OSError):self.repo.apply(0,self.move(),'corrupt-journal')
        path=next((self.repo.root/'journal').glob('*.json'));value=json.loads(path.read_text())
        value['new_head']['undo']=['bad'];path.write_text(json.dumps(value))
        head=(self.repo.root/'HEAD.json').read_bytes()
        with self.assertRaises(RigError):self.repo.recover('corrupt-journal',0)
        self.assertEqual((self.repo.root/'HEAD.json').read_bytes(),head)

    def test_verified_private_stage_can_recover_before_publication(self):
        import editor3d_repository as module
        with patch.object(module,'_rename_no_replace',side_effect=OSError('injected publication stop')):
            with self.assertRaises(OSError):self.repo.apply(0,self.move(8),'before-publish')
        self.assertEqual(self.repo.snapshot()['revision'],0)
        self.assertTrue(self.repo.recovery()[0]['can_recover'])
        self.assertEqual(self.mesh_x(self.repo.recover('before-publish',0)),8)

    def test_read_file_is_bound_to_snapshot_inventory(self):
        view=self.repo.snapshot()
        for path in ('../secret','HEAD.json','unknown.obj'):
            with self.subTest(path=path),self.assertRaises(RigError):self.repo.read_file(path,view['sha256'])
        with self.assertRaises(RigError):self.repo.read_file('triangle.obj','0'*64)
        native=self.repo.root/'revisions/00000000/native/triangle.obj';native.write_bytes(b'corrupt')
        with self.assertRaises(RigError):self.repo.read_file('triangle.obj',view['sha256'])
        with self.assertRaises(RigError):self.repo.snapshot()

    def test_save_as_and_archive_restore_preserve_all_revisions_and_bytes(self):
        self.repo.apply(0,self.move(4),'edit');self.repo.undo(1,'undo')
        clone=self.repo.save_as(self.root/'copy')
        self.assertEqual(clone.snapshot(),self.repo.snapshot())
        self.repo.archive(self.root/'archive.zip')
        restored=EditorRepository.restore_archive(self.root/'archive.zip',self.root/'restored')
        self.assertEqual(restored.history(),self.repo.history())
        self.assertEqual(restored.snapshot(),self.repo.snapshot())
        self.assertEqual(self.mesh_x(restored.redo(2,'redo-restored')),4)

    def test_existing_restore_destination_is_preserved(self):
        self.repo.archive(self.root/'archive.zip');destination=self.root/'existing';destination.mkdir()
        with self.assertRaises(RigError):EditorRepository.restore_archive(self.root/'archive.zip',destination)
        self.assertTrue(destination.is_dir());self.assertEqual(list(destination.iterdir()),[])

    def test_archive_rejects_bytes_changed_after_verification(self):
        original = EditorRepository.verify_archive
        before = self.repo.snapshot()
        destination = self.root/'changed-after-verification.zip'
        def change_verified_input(source):
            receipt = original(source)
            # Exercise the actual admitted file, including the historical
            # path-based implementation used as this regression's control.
            if hasattr(source, 'seek'):
                source.seek(0); source.write(b'UNVERIFIED ARCHIVE REPLACEMENT'); source.truncate(); source.flush()
            else:
                Path(source).write_bytes(b'UNVERIFIED ARCHIVE REPLACEMENT')
            return receipt
        with patch.object(EditorRepository, 'verify_archive', side_effect=change_verified_input):
            with self.assertRaises((ValueError, RigError)):
                self.repo.archive(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.repo.snapshot(), before)

    def test_archive_racing_destination_preserves_foreign_bytes(self):
        import editor3d_repository as module
        original = module.publish_verified_file
        destination = self.root/'racing-archive.zip'
        def race(source_fd, target, **kwargs):
            Path(target).write_bytes(b'foreign archive destination')
            return original(source_fd, target, **kwargs)
        with patch.object(module, 'publish_verified_file', side_effect=race):
            with self.assertRaises(FileExistsError):
                self.repo.archive(destination)
        self.assertEqual(destination.read_bytes(), b'foreign archive destination')

    def test_archive_cleanup_failure_reports_the_committed_verified_archive(self):
        import editor3d_repository as module
        original = module.publish_verified_file
        destination = self.root/'committed-archive.zip'
        def cleanup_failure(source_fd, target, **kwargs):
            receipt = original(source_fd, target, **kwargs)
            raise module.VerifiedFileCleanupError(receipt)
        with patch.object(module, 'publish_verified_file', side_effect=cleanup_failure):
            result = self.repo.archive(destination)
        self.assertTrue(result['cleanup_pending'])
        self.assertEqual(result['sha256'], hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertEqual(EditorRepository.verify_archive(destination)['head']['revision'], result['revision'])

    def test_archive_rejects_path_traversal_duplicates_and_link_members(self):
        for name,mode in (('../escape',0),('Foo',0),('link',0o120777<<16)):
            archive_path=self.root/(name.replace('/','_')+'.zip')
            with zipfile.ZipFile(archive_path,'w') as archive:
                info=zipfile.ZipInfo(name);info.external_attr=mode;archive.writestr(info,b'x')
                if name=='Foo':archive.writestr('foo',b'x')
                archive.writestr('archive-receipt.json',b'{}')
            with self.subTest(name=name),self.assertRaises(RigError):EditorRepository.verify_archive(archive_path)
        self.assertFalse((self.root/'escape').exists())

    def test_revision_budget_rejects_new_edit_preserving_history(self):
        bounded=EditorRepository.create(self.source,self.root/'bounded',limits={'max_revisions':2,'max_history':2})
        bounded.apply(0,self.move(),'first');before=bounded.snapshot()
        with self.assertRaises(RigError):bounded.apply(1,self.move(8),'too-many')
        self.assertEqual(bounded.snapshot(),before)

    def test_repository_symlink_metadata_cannot_overwrite_external_file(self):
        target=self.root/'external';target.write_text('untouched')
        head=self.repo.root/'HEAD.json';head.unlink()
        try:head.symlink_to(target)
        except OSError:self.skipTest('symlink creation unavailable')
        with self.assertRaises(RigError):EditorRepository(self.repo.root)
        self.assertEqual(target.read_text(),'untouched')

    def test_native_snapshot_remains_exact_after_another_edit(self):
        initial=self.repo.snapshot();self.repo.apply(0,self.move(8),'later')
        frozen=self.repo.native_snapshot(0,initial['sha256'])
        self.assertEqual((frozen['root']/'triangle.obj').read_bytes(),(self.source/'triangle.obj').read_bytes())
        with self.assertRaises(RigError):self.repo.native_snapshot(0,'0'*64)

    def test_actual_new_revision_size_is_checked_before_publication(self):
        before=self.repo.snapshot()
        used=sum(p.stat().st_size for p in self.repo.root.rglob('*') if p.is_file())
        base_bytes=sum(r['bytes'] for r in before['inventory'].values())
        self.repo.limits['max_bytes']=used+base_bytes+500
        with self.assertRaises(RigError):self.repo.apply(0,self.move(10),'over-budget')
        self.assertEqual(self.repo.snapshot(),before)
        self.assertFalse((self.repo.root/'revisions/00000001').exists())

    def test_directory_traversal_itself_is_bounded(self):
        from editor3d_repository import _scan
        directory=self.root/'many';directory.mkdir()
        for n in range(17):(directory/str(n)).mkdir()
        limits=dict(self.repo.limits,max_files=4)
        with self.assertRaises(RigError):_scan(directory,limits)

    def test_trusted_adapter_must_not_publish_uninventoried_output(self):
        from editor3d_repository import _copy_closure
        before=self.repo.snapshot()
        def build(source,destination,scene):
            _copy_closure(source,destination,self.repo.limits)
            (destination/'not-a-native-dependency.txt').write_text('unexpected')
            return scene
        with self.assertRaises(RigError):self.repo.transact_native(0,'extra-file',{},build)
        self.assertEqual(self.repo.snapshot(),before)

    def test_racing_restore_destination_is_not_overwritten(self):
        import editor3d_repository as module
        self.repo.archive(self.root/'race.zip');destination=self.root/'racer'
        real=module._rename_no_replace
        def race(stage,target):
            target.mkdir();(target/'mine').write_bytes(b'preserve')
            return real(stage,target)
        with patch.object(module,'_rename_no_replace',side_effect=race),self.assertRaises(FileExistsError):
            EditorRepository.restore_archive(self.root/'race.zip',destination)
        self.assertEqual((destination/'mine').read_bytes(),b'preserve')

    def test_actual_process_exit_leaves_inspectable_incomplete_attempt(self):
        script="""import os,sys
from editor3d_repository import EditorRepository,_copy_closure
r=EditorRepository(sys.argv[1])
def crash(source,destination,scene):
 _copy_closure(source,destination,r.limits)
 os._exit(17)
r.transact_native(0,'crashed',{},crash)
"""
        result=subprocess.run([sys.executable,'-c',script,str(self.repo.root)],
            env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'scripts')},timeout=15)
        self.assertEqual(result.returncode,17)
        reopened=EditorRepository(self.repo.root)
        report=reopened.recovery();self.assertEqual(report[0]['status'],'building')
        self.assertEqual(reopened.snapshot()['revision'],0)
        with self.assertRaises(RigError):reopened.apply(0,self.move(),'while-interrupted')
        result=reopened.discard_incomplete('crashed',0)
        self.assertTrue(result['discarded']);self.assertFalse(reopened.recovery())
        self.assertEqual(len(list((reopened.root/'failed').glob('*.json'))),1)
        self.assertEqual(reopened.apply(0,self.move(),'after-recovery')['revision'],1)


if __name__=='__main__':unittest.main()
