"""Real native scene persistence, failure recovery and archive regressions."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import io
import struct
import zlib
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

    def test_create_accounts_for_repository_metadata_before_publication(self):
        from scene3d_contracts import load_project

        bundle = load_project(self.source)
        for limits in ({'max_files': len(bundle['inventory'])}, {'max_bytes': bundle['bytes']}):
            with self.subTest(limits=limits):
                destination = self.root / 'over-budget'
                with self.assertRaises(RigError) as caught:
                    EditorRepository.create(self.source, destination, limits=limits)
                self.assertEqual(caught.exception.report['code'], 'EDITOR3D_LIMIT')
                self.assertFalse(destination.exists())
        self.assertEqual(load_project(self.source)['inventory'], bundle['inventory'])

    def test_metadata_read_limit_is_checked_before_create_or_edit_publication(self):
        import editor3d_repository as module

        revision_record = self.repo.root / 'revisions' / '00000000' / 'record.json'
        before = self.repo.snapshot()
        limit = revision_record.stat().st_size
        destination = self.root / 'oversized-record'
        with patch.object(module, 'MAX_METADATA_BYTES', limit - 1):
            with self.assertRaises(RigError) as created:
                EditorRepository.create(self.source, destination)
        self.assertEqual(created.exception.report['code'], 'EDITOR3D_LIMIT')
        self.assertFalse(destination.exists())
        with patch.object(module, 'MAX_METADATA_BYTES', limit):
            with self.assertRaises(RigError) as edited:
                self.repo.apply(0, self.move(8), 'oversized-record')
        self.assertEqual(edited.exception.report['code'], 'EDITOR3D_LIMIT')
        self.assertEqual(self.repo.snapshot(), before)
        self.assertFalse(self.repo.recovery())

    @unittest.skipUnless(os.name == 'posix', 'Native directory synchronization is POSIX-specific.')
    def test_native_directories_are_synced_before_revision_is_verified(self):
        import editor3d_repository as module

        events = []
        sync_directory = module._sync_directory
        write_metadata = module._write_metadata

        def sync(path):
            events.append(('sync', Path(path)))
            return sync_directory(path)

        def write(path, value):
            if path.parent.name == 'journal' and value.get('status') == 'verified':
                events.append(('verified', path))
            if path == self.repo.root / 'HEAD.json':
                events.append(('head', path))
            return write_metadata(path, value)

        with patch.object(module, '_sync_directory', side_effect=sync), \
             patch.object(module, '_write_metadata', side_effect=write):
            saved = self.repo.apply(0, self.move(8), 'sync-native')
        verified = next(index for index, (event, _) in enumerate(events) if event == 'verified')
        stages = [path for _, path in events[:verified] if path.name == 'native']
        self.assertEqual(len(stages), 1)
        nested = stages[0] / 'versions' / 'example-scene'
        self.assertIn(('sync', nested), events[:verified])
        self.assertLess(events.index(('sync', nested)), events.index(('sync', stages[0])))
        self.assertLess(events.index(('sync', stages[0])),
                        events.index(('sync', stages[0].parent.parent)))
        self.assertLess(events.index(('sync', stages[0].parent.parent)), verified)
        head = events.index(('head', self.repo.root / 'HEAD.json'))
        self.assertIn(('sync', self.repo.root / 'journal'), events[head + 1:])
        self.assertEqual(EditorRepository(self.repo.root).snapshot()['sha256'], saved['sha256'])

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

    def test_auto_checkpoint_schedules_new_commits_and_history_only(self):
        first = self.repo.apply(0, self.move(), 'move', schedule_autosave=True)
        undo = self.repo.undo(1, 'undo', schedule_autosave=True)
        self.assertEqual(self.repo.autosave(force=True)['sha256'], undo['sha256'])
        redo = self.repo.redo(2, 'redo', schedule_autosave=True)
        saved = self.repo.autosave(force=True)
        self.assertEqual((saved['revision'], saved['sha256']), (3, redo['sha256']))

        self.assertEqual(self.repo.apply(0, self.move(), 'move', schedule_autosave=True)['sha256'], first['sha256'])
        with self.assertRaises(RigError):
            self.repo.apply(2, self.move(5), 'stale', schedule_autosave=True)
        self.assertEqual(self.repo.autosave(force=True)['reason'], 'no-pending-change')

    def test_failed_checkpoint_retries_and_superseded_checkpoint_is_skipped(self):
        scheduled = self.repo.apply(0, self.move(), 'move', schedule_autosave=True)
        with patch('editor3d_repository._write_metadata', side_effect=OSError('checkpoint unavailable')):
            with self.assertRaises(OSError):
                self.repo.autosave(force=True)
        checkpoint = self.repo.root / 'autosave.json'
        self.assertFalse(checkpoint.exists())
        self.assertEqual(self.repo.autosave(force=True)['sha256'], scheduled['sha256'])
        checkpoint_bytes = checkpoint.read_bytes()

        self.repo.apply(1, self.move(4), 'next', schedule_autosave=True)
        EditorRepository(self.repo.root).apply(2, self.move(5), 'separate-writer')
        self.assertEqual(self.repo.autosave(force=True)['reason'], 'superseded')
        self.assertEqual(checkpoint.read_bytes(), checkpoint_bytes)

    def test_manual_autosave_request_and_non_due_poll_do_not_take_writer_lock(self):
        with job_lock(self.repo.root):
            self.repo.request_autosave(now=10)
            self.assertEqual(self.repo.autosave(now=10.5)['reason'], 'debounced')
        self.assertTrue(self.repo.autosave(now=20)['saved'])

    def test_autosave_refuses_portable_slot_alias_and_edits_remain_writable(self):
        self.repo.apply(0, self.move(), 'first', schedule_autosave=True)
        alias = self.repo.root / 'AUTOSAVE.JSON'
        alias.write_bytes(b'separate file')
        with self.assertRaises(RigError) as caught:
            self.repo.autosave(force=True)
        self.assertEqual(caught.exception.report['code'], 'EDITOR3D_PATH')
        self.assertEqual(alias.read_bytes(), b'separate file')
        self.assertNotIn('autosave.json', {entry.name for entry in self.repo.root.iterdir()})
        second = self.repo.apply(1, self.move(4), 'second', schedule_autosave=True)
        self.assertEqual(self.mesh_x(EditorRepository(self.repo.root).snapshot()), 4)
        alias.unlink()
        self.assertEqual(self.repo.autosave(force=True)['sha256'], second['sha256'])

    def test_autosave_preserves_nonregular_exact_slot(self):
        checkpoint = self.repo.root / 'autosave.json'
        checkpoint.mkdir()
        self.repo.request_autosave()
        with self.assertRaises(RigError) as caught:
            self.repo.autosave(force=True)
        self.assertEqual(caught.exception.report['code'], 'EDITOR3D_PATH')
        self.assertTrue(checkpoint.is_dir())
        checkpoint.rmdir()
        self.assertTrue(self.repo.autosave(force=True)['saved'])

    def test_autosave_rechecks_root_identity_after_verification(self):
        from editor3d_repository import _directory_identity

        verified = False
        bundle = self.repo._bundle

        def mark_verified(record):
            nonlocal verified
            result = bundle(record)
            verified = True
            return result

        def identity(path):
            device, inode = _directory_identity(path)
            return device, inode + int(verified)

        self.repo.request_autosave()
        with patch.object(self.repo, '_bundle', side_effect=mark_verified), patch(
                'editor3d_repository._directory_identity', side_effect=identity):
            with self.assertRaises(RigError) as caught:
                self.repo.autosave(force=True)
        self.assertTrue(verified)
        self.assertEqual(caught.exception.report['code'], 'EDITOR3D_PATH')
        self.assertFalse((self.repo.root / 'autosave.json').exists())
        self.assertTrue(self.repo.autosave(force=True)['saved'])

    def test_autosave_descriptor_publication_can_replace_its_own_checkpoint(self):
        from atomic_publish import private_stage_fd_supported

        if not (private_stage_fd_supported() and os.scandir in os.supports_fd and os.rename in os.supports_dir_fd):
            self.skipTest('Descriptor-relative publication is unavailable.')
        with patch('editor3d_repository.private_stage_supported', return_value=False):
            self.repo.request_autosave()
            self.assertEqual(self.repo.autosave(force=True)['revision'], 0)
            expected = self.repo.apply(0, self.move(4), 'descriptor-checkpoint', schedule_autosave=True)
            self.assertTrue(self.repo.autosave(force=True)['saved'])
        stored = json.loads((self.repo.root / 'autosave.json').read_bytes())
        self.assertEqual((stored['revision'], stored['sha256']), (expected['revision'], expected['sha256']))
        self.assertFalse(list(self.repo.root.glob('.autosave-*.tmp')))

    def test_checkpoint_and_restore_are_conflict_checked(self):
        self.repo.apply(0,self.move(),'move');checkpoint=self.repo.checkpoint(1)
        self.assertEqual(checkpoint['sha256'],self.repo.snapshot()['sha256'])
        with self.assertRaises(RigError):self.repo.checkpoint(0)
        restored=self.repo.restore_revision(0,1,'restore')
        self.assertEqual(restored['revision'],2);self.assertEqual(self.mesh_x(restored),2)

    def test_checkpoint_admission_preserves_archive_and_allows_exact_replacement(self):
        def save(repository, name):
            if name == 'autosave':
                repository.request_autosave()
                return repository.autosave(force=True)
            return repository.checkpoint(0)

        baseline = len(self.repo._all_files())
        for name in ('checkpoint', 'autosave'):
            for budget in ('files', 'bytes'):
                with self.subTest(name=name, budget=budget):
                    label = name + '-' + budget
                    limits = {'max_files': baseline} if budget == 'files' else {'max_bytes': 100_000}
                    repository = EditorRepository.create(self.source, self.root / label, limits=limits)
                    if budget == 'bytes':
                        used = sum(item['bytes'] for item in repository._all_files().values())
                        (repository.root / 'padding.bin').write_bytes(bytes(limits['max_bytes'] - used))
                    original = repository._all_files()
                    with self.assertRaises(RigError) as refused:
                        save(repository, name)
                    self.assertEqual(refused.exception.report['code'], 'EDITOR3D_LIMIT')
                    self.assertEqual(repository._all_files(), original)
                    archive = self.root / (label + '.zip')
                    repository.archive(archive)
                    self.assertEqual(EditorRepository.verify_archive(archive)['files'], original)
            repository = EditorRepository.create(self.source, self.root / (name + '-replace'),
                                                 limits={'max_files': baseline + 2, 'max_bytes': 100_000})
            save(repository, name)
            used = sum(item['bytes'] for item in repository._all_files().values())
            (repository.root / 'padding.bin').write_bytes(bytes(repository.limits['max_bytes'] - used))
            self.assertEqual(save(repository, name)['revision'], 0)
            stored = (repository.root / (name + '.json')).read_bytes()
            # A growing replacement must leave its predecessor intact.
            (repository.root / (name + '.json')).write_bytes(stored[:-1])
            with (repository.root / 'padding.bin').open('ab') as padding:
                padding.write(b'x')
            with self.assertRaises(RigError) as refused:
                save(repository, name)
            self.assertEqual(refused.exception.report['code'], 'EDITOR3D_LIMIT')
            self.assertEqual((repository.root / (name + '.json')).read_bytes(), stored[:-1])

    def test_history_rejects_changed_staged_inventory_and_preserves_head(self):
        import editor3d_repository as module
        from scene3d_contracts import canonical_bytes

        def changed_copy(source, destination, limits):
            result = original_copy(source, destination, limits)
            project = dict(result['project'], name='Different valid native project')
            (destination / 'project.json').write_bytes(canonical_bytes(project))
            return result

        original_copy = module._copy_closure
        moved = self.repo.apply(0, self.move(4), 'move')
        for action in ('restore', 'undo', 'redo'):
            with self.subTest(action=action):
                if action == 'redo':
                    self.repo.undo(moved['revision'], 'prepare-redo')
                before = self.repo.snapshot()
                revision = before['revision']
                with patch.object(module, '_copy_closure', side_effect=changed_copy):
                    with self.assertRaises(RigError) as changed:
                        if action == 'restore':
                            self.repo.restore_revision(0, revision, 'restore')
                        else:
                            getattr(self.repo, action)(revision, 'changed-' + action)
                self.assertEqual(changed.exception.report['code'], 'EDITOR3D_HASH')
                self.assertEqual(self.repo.snapshot(), before)
                self.assertFalse(self.repo.recovery())
                self.assertFalse((self.repo.root / 'revisions' / f'{revision + 1:08d}').exists())

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

    def test_recovery_requires_exact_bounded_history_before_or_after_head_commit(self):
        import editor3d_repository as module

        for committed in (False, True):
            with self.subTest(committed=committed):
                repo = EditorRepository.create(self.source, self.root / f'bounded-{committed}', limits={'max_history': 2})
                for i in range(3):
                    repo.apply(i, self.move(4 + i), f'edit-{i}')
                with self.fail_head_once(write_then_fail=committed), self.assertRaises(OSError):
                    repo.undo(3, 'recover-undo')
                journal, = (repo.root / 'journal').glob('*.json')
                original = json.loads(journal.read_bytes())
                self.assertEqual(original['new_head']['undo'], [1])
                self.assertEqual(original['new_head']['redo'], [3])
                for key, invalid in [('undo', [0]), ('redo', [2])]:
                    changed = deepcopy(original)
                    changed['new_head'][key] = invalid
                    module._write_metadata(journal, changed)
                    if committed:
                        module._write_metadata(repo.root / 'HEAD.json', changed['new_head'])
                    head = (repo.root / 'HEAD.json').read_bytes()
                    self.assertFalse(repo.recovery()[0]['can_recover'])
                    with self.assertRaises(RigError) as refused:
                        repo.recover('recover-undo', 4 if committed else 3)
                    self.assertEqual(refused.exception.report['code'], 'EDITOR3D_HISTORY')
                    self.assertEqual((repo.root / 'HEAD.json').read_bytes(), head)
                    self.assertTrue(journal.is_file())
                module._write_metadata(journal, original)
                if committed:
                    module._write_metadata(repo.root / 'HEAD.json', original['new_head'])
                recovered = repo.recover('recover-undo', 4 if committed else 3)
                self.assertEqual(self.mesh_x(recovered), 5)
                self.assertEqual(self.mesh_x(repo.redo(4, 'redo')), 6)

    def test_public_snapshots_only_admit_committed_revisions(self):
        with self.fail_head_once(), self.assertRaises(OSError):
            self.repo.apply(0, self.move(8), 'pending')
        reopened = EditorRepository(self.repo.root)
        item, = reopened.recovery()
        revision, digest = item['revision'], item['new_head']['sha256']
        for select, args in ((reopened.snapshot, (revision,)), (reopened.native_snapshot, (revision, digest))):
            with self.assertRaises(RigError) as refused:
                select(*args)
            self.assertEqual(refused.exception.report['code'], 'EDITOR3D_REVISION')
        self.assertEqual(reopened.history()[-1]['revision'], 0)
        recovered = reopened.recover('pending', 0)
        self.assertEqual(reopened.snapshot(revision), recovered)
        self.assertEqual(reopened.native_snapshot(revision, digest)['inventory'], recovered['inventory'])

    def test_recovery_acknowledges_exact_revision_after_next_writer_commits(self):
        import editor3d_repository as module

        with self.fail_head_once(), self.assertRaises(OSError):
            self.repo.apply(0, self.move(8), 'pending')
        next_writer = EditorRepository(self.repo.root)
        waiting = [True]

        @contextmanager
        def advance_on_unlock(root):
            with job_lock(root):
                yield
            if waiting:
                waiting.pop()
                next_writer.undo(1, 'downstream')

        with patch.object(module, 'job_lock', advance_on_unlock):
            recovered = self.repo.recover('pending', 0)
        self.assertEqual(self.repo.snapshot()['revision'], 2)
        self.assertEqual(recovered, self.repo.snapshot(1))
        self.assertEqual(self.mesh_x(recovered), 8)
        self.assertTrue(recovered['undo_available'])
        self.assertFalse(recovered['redo_available'])

    def test_verified_private_stage_can_recover_before_publication(self):
        import editor3d_repository as module
        with patch.object(module,'_rename_no_replace',side_effect=OSError('injected publication stop')):
            with self.assertRaises(OSError):self.repo.apply(0,self.move(8),'before-publish')
        self.assertEqual(self.repo.snapshot()['revision'],0)
        self.assertTrue(self.repo.recovery()[0]['can_recover'])
        self.assertEqual(self.mesh_x(self.repo.recover('before-publish',0)),8)

    def test_verified_journal_recovers_an_already_installed_revision(self):
        import editor3d_repository as module
        write_metadata = module._write_metadata

        def stop_after_install(path, value):
            if path.parent.name == 'journal' and value['status'] == 'published':
                raise OSError('injected interruption after install')
            write_metadata(path, value)

        before = self.repo.snapshot()
        source_bytes = (self.source / 'triangle.obj').read_bytes()
        with patch.object(module, '_write_metadata', side_effect=stop_after_install):
            with self.assertRaises(OSError):
                self.repo.apply(0, self.move(8), 'after-install')

        reopened = EditorRepository(self.repo.root)
        item, = reopened.recovery()
        self.assertEqual(item['status'], 'verified')
        self.assertTrue(item['can_recover'])
        self.assertEqual(reopened.snapshot(), before)
        recovered = reopened.recover('after-install', 0, expected_sha256=before['sha256'])
        self.assertEqual(self.mesh_x(recovered), 8)
        self.assertEqual(EditorRepository(self.repo.root).snapshot(), recovered)
        self.assertFalse(reopened.recovery())
        self.assertEqual(reopened.read_file('triangle.obj', before['sha256']), source_bytes)
        self.assertEqual((self.source / 'triangle.obj').read_bytes(), source_bytes)

    def test_mutation_rejects_live_head_stacks_that_disagree_with_committed_records(self):
        self.repo.apply(0, self.move(4), 'first-move')
        current = self.repo.apply(1, self.move(9), 'second-move')
        head_path = self.repo.root / 'HEAD.json'
        original = head_path.read_bytes()
        head = json.loads(original)
        self.assertEqual(head['undo'], [0, 1])
        head['undo'] = [0]
        changed = json.dumps(head).encode('utf-8')
        head_path.write_bytes(changed)

        reopened = EditorRepository(self.repo.root)
        for action in (
                lambda: reopened.undo(2, 'wrong-undo', expected_sha256=current['sha256']),
                lambda: reopened.apply(2, self.move(12), 'wrong-apply', expected_sha256=current['sha256']),
                reopened.snapshot):
            with self.assertRaises(RigError) as refused:
                action()
            self.assertEqual(refused.exception.report['code'], 'EDITOR3D_HISTORY')
        self.assertEqual(head_path.read_bytes(), changed)
        self.assertFalse((self.repo.root / 'revisions/00000003').exists())
        self.assertFalse(reopened.recovery())
        head_path.write_bytes(original)
        self.assertEqual(self.mesh_x(reopened.undo(2, 'correct-undo')), 4)

    def test_discard_releases_stage_after_actual_oversized_partial_import_exit(self):
        small = EditorRepository.create(self.source, self.root / 'bounded-editor',
                                        limits={'max_file_bytes': 32768})
        before = small.snapshot()
        native = (self.source / 'triangle.obj').read_bytes()
        child = """import os
import sys
from editor3d_repository import EditorRepository
repo = EditorRepository(sys.argv[1])
def unverified(source, destination, scene):
    destination.mkdir()
    (destination / 'partial.obj').write_bytes(b'x' * 32769)
    os._exit(47)
repo.transact_native(0, 'oversized-interruption', {}, unverified)
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts'))
        exited = subprocess.run([sys.executable, '-B', '-c', child, str(small.root)],
                                env=env, capture_output=True, timeout=20)
        self.assertEqual(exited.returncode, 47)
        reopened = EditorRepository(small.root)
        item, = reopened.recovery()
        self.assertEqual((item['status'], item['can_recover']), ('building', False))
        self.assertEqual(reopened.snapshot(), before)
        reopened.discard_incomplete('oversized-interruption', 0, expected_sha256=before['sha256'])
        self.assertFalse((small.root / 'revisions' / item['stage']).exists())
        self.assertFalse(reopened.recovery())
        self.assertEqual(len(list((small.root / 'failed').glob('*.json'))), 1)
        self.assertEqual(self.mesh_x(reopened.apply(0, self.move(8), 'after-discard')), 8)
        self.assertEqual((self.source / 'triangle.obj').read_bytes(), native)

    def test_archive_refuses_captured_payload_that_differs_from_immutable_revision(self):
        original = self.repo.root / 'revisions/00000000/native/triangle.obj'
        saved_bytes = original.read_bytes()
        capture_files = self.repo._all_files
        archive = self.root / 'drift.zip'

        def drift_during_capture():
            original.write_bytes(saved_bytes + b'\n')
            return capture_files()

        try:
            with patch.object(self.repo, '_all_files', side_effect=drift_during_capture):
                with self.assertRaises(RigError) as refused:
                    self.repo.archive(archive)
            self.assertEqual(refused.exception.report['code'], 'EDITOR3D_ARCHIVE')
            self.assertIn('Archived revision file differs from its immutable inventory.',
                          refused.exception.report['message'])
            self.assertFalse(archive.exists())
        finally:
            original.write_bytes(saved_bytes)
        self.repo.archive(archive)
        restored = EditorRepository.restore_archive(archive, self.root / 'drift-control')
        self.assertEqual(restored.snapshot(), self.repo.snapshot())

    def test_read_file_is_bound_to_snapshot_inventory(self):
        view=self.repo.snapshot()
        for path in ('../secret','HEAD.json','unknown.obj'):
            with self.subTest(path=path),self.assertRaises(RigError):self.repo.read_file(path,view['sha256'])
        with self.assertRaises(RigError):self.repo.read_file('triangle.obj','0'*64)
        native=self.repo.root/'revisions/00000000/native/triangle.obj';native.write_bytes(b'corrupt')
        with self.assertRaises(RigError):self.repo.read_file('triangle.obj',view['sha256'])
        with self.assertRaises(RigError):self.repo.snapshot()

    def test_save_as_rejects_resolved_descendant_before_creating_parents(self):
        untouched = self.repo._all_files()
        alias = self.root / 'source-alias'
        if os.name == 'nt':
            subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(alias), str(self.repo.root)],
                           check=True, capture_output=True)
        else:
            alias.symlink_to(self.repo.root, target_is_directory=True)
        for parent in (self.repo.root, alias, self.root / 'absent' / '..' / self.repo.root.name):
            with self.assertRaises(RigError) as refused:
                self.repo.save_as(parent / 'new-parent' / 'export')
            self.assertEqual(refused.exception.report['code'], 'EDITOR3D_DESTINATION')
            with self.assertRaises(RigError) as refused:
                self.repo.archive(parent / 'new-parent' / 'export.zip')
            self.assertEqual(refused.exception.report['code'], 'EDITOR3D_DESTINATION')
        self.assertFalse((self.repo.root / 'new-parent').exists())
        self.assertFalse((self.root / 'absent').exists())
        self.assertEqual(self.repo._all_files(), untouched)

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

    def test_archive_requires_head_stacks_derived_from_committed_actions(self):
        self.repo.apply(0, self.move(), 'move')
        self.repo.restore_revision(0, 1, 'restore')
        self.repo.undo(2, 'undo-restore')
        archive = self.root / 'valid.zip'
        self.repo.archive(archive)
        with zipfile.ZipFile(archive) as source:
            members = {name: source.read(name) for name in source.namelist()}
        head = json.loads(members['HEAD.json'])
        self.assertEqual((head['undo'], head['redo']), ([0], [2]))
        head['redo'] = [1]
        members['HEAD.json'] = json.dumps(head).encode()
        receipt = json.loads(members['archive-receipt.json'])
        receipt['head'] = head
        receipt['files']['HEAD.json'] = dict(bytes=len(members['HEAD.json']),
                                             sha256=hashlib.sha256(members['HEAD.json']).hexdigest())
        members['archive-receipt.json'] = json.dumps(receipt).encode()
        invalid = self.root / 'invalid-history.zip'
        with zipfile.ZipFile(invalid, 'w') as output:
            for name, data in members.items():
                output.writestr(name, data)
        with self.assertRaises(RigError) as refused:
            EditorRepository.verify_archive(invalid)
        self.assertEqual(refused.exception.report['code'], 'EDITOR3D_ARCHIVE')
        self.assertEqual(refused.exception.__cause__.report['code'], 'EDITOR3D_HISTORY')
        destination = self.root / 'restored'
        with self.assertRaises(RigError):
            EditorRepository.restore_archive(invalid, destination)
        self.assertFalse(destination.exists())
        restored = EditorRepository.restore_archive(archive, destination)
        self.assertEqual(self.mesh_x(restored.redo(3, 'redo-restore')), 2)

    def test_archive_restores_repetitive_native_provenance_bytes(self):
        from scene3d_contracts import canonical_bytes

        name = 'source-audit.txt'
        payload = b'Native capture log\n' + b' ' * (4 * 1024 * 1024)
        (self.source / name).write_bytes(payload)
        project_path = self.source / 'project.json'
        project = json.loads(project_path.read_bytes())
        project['budgets']['max_bytes'] = 8 * 1024 * 1024
        project['provenance']['inputs'].append(dict(path=name, sha256=hashlib.sha256(payload).hexdigest()))
        project_path.write_bytes(canonical_bytes(project))
        source = EditorRepository.create(self.source, self.root / 'editor-with-audit')

        archive = self.root / 'audit.zip'
        source.archive(archive)
        with zipfile.ZipFile(archive) as zipped:
            entry = zipped.getinfo('revisions/00000000/native/' + name)
            self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
        restored = EditorRepository.restore_archive(archive, self.root / 'restored-audit')
        snapshot = restored.snapshot()
        self.assertEqual(snapshot['sha256'], source.snapshot()['sha256'])
        self.assertEqual(restored.read_file(name, snapshot['sha256']), payload)

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

    def test_archive_publishes_only_in_admitted_destination_parent(self):
        import editor3d_repository as module

        parent = self.root / 'exports'
        parent.mkdir()
        admitted = self.root / 'original-exports'
        publish = module.publish_verified_file

        def change_parent(source_fd, target, **kwargs):
            parent.rename(admitted)
            parent.mkdir()
            return publish(source_fd, target, **kwargs)

        with patch.object(module, 'publish_verified_file', side_effect=change_parent), self.assertRaises(OSError):
            self.repo.archive(parent / 'archive.zip')
        self.assertEqual(list(admitted.iterdir()), [])
        self.assertEqual(list(parent.iterdir()), [])

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

    def test_archive_rejects_forged_deflate_lengths_and_truncation(self):
        receipt = json.dumps({'schema_version': 1, 'kind': 'editor3d-archive', 'files': {}, 'head': {}}).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('archive-receipt.json', receipt + b' ' * (8 * 1024 * 1024))
        forged = bytearray(buffer.getvalue())
        central = forged.index(b'PK\x01\x02')
        for offset in (14, central + 16): struct.pack_into('<I', forged, offset, zlib.crc32(receipt))
        for offset in (22, central + 24): struct.pack_into('<I', forged, offset, len(receipt))
        with self.assertRaises(RigError): EditorRepository.verify_archive(io.BytesIO(forged))
        path = self.root/'forged.zip'; path.write_bytes(forged)
        with self.assertRaises(RigError): EditorRepository.restore_archive(path, self.root/'forged-restored')
        self.assertFalse((self.root/'forged-restored').exists())
        # Preserve ZIP framing while cutting the actual deflate stream short.
        broken = bytearray(buffer.getvalue())
        name_len, extra_len = struct.unpack_from('<HH', broken, 26)
        compressed_start = 30 + name_len + extra_len
        compressed_size = struct.unpack_from('<I', broken, 18)[0]
        broken[compressed_start + compressed_size - 1] ^= 0x80
        with self.assertRaises(RigError): EditorRepository.verify_archive(io.BytesIO(broken))
        for data in (buffer.getvalue()[:-1], buffer.getvalue()[:40]):
            with self.assertRaises(RigError): EditorRepository.verify_archive(io.BytesIO(data))

    def test_archive_source_symlink_is_rejected_and_restore_uses_snapshot(self):
        path = self.root/'valid.zip'; self.repo.archive(path)
        link = self.root/'link.zip'
        try: link.symlink_to(path)
        except OSError: self.skipTest('symlink creation unavailable')
        with self.assertRaises(RigError): EditorRepository.verify_archive(link)
        with self.assertRaises(RigError): EditorRepository.restore_archive(link, self.root/'symlink-restore')
        original = EditorRepository.verify_archive
        def replace_path(source):
            receipt = original(source)
            path.write_bytes(b'replaced after verification')
            return receipt
        with patch.object(EditorRepository, 'verify_archive', side_effect=replace_path):
            restored = EditorRepository.restore_archive(path, self.root/'snapshot-restore')
        self.assertEqual(restored.snapshot(), self.repo.snapshot())

    def test_archive_directory_count_is_checked_before_zipfile(self):
        self.repo.archive(self.root/'valid.zip')
        data = bytearray((self.root/'valid.zip').read_bytes())
        end = data.rfind(b'PK\x05\x06')
        struct.pack_into('<HH', data, end + 8, 1, 1)
        with patch('editor3d_repository.zipfile.ZipFile', side_effect=AssertionError('directory parsed early')):
            with self.assertRaises(RigError): EditorRepository.verify_archive(io.BytesIO(data))

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

    def test_commit_reads_parent_inventory_once_for_space_estimate(self):
        counts = []
        original_recovery = self.repo.recovery
        original_space = self.repo._space
        with patch.object(self.repo, '_record', wraps=self.repo._record) as read:
            def recovery():
                result = original_recovery()
                read.reset_mock()
                return result

            def space(revision, estimate):
                counts.append(read.call_count)
                return original_space(revision, estimate)

            with patch.object(self.repo, 'recovery', side_effect=recovery), \
                 patch.object(self.repo, '_space', side_effect=space):
                self.repo.apply(0, self.move(), 'estimate')
        self.assertEqual(counts, [1])

    def test_failed_cleanup_keeps_the_attempt_recoverable(self):
        import editor3d_repository as module

        def fail(source, destination, scene):
            destination.mkdir()
            raise ValueError('interrupted build')

        with patch.object(module.shutil, 'rmtree', side_effect=PermissionError('busy stage')):
            with self.assertRaises(PermissionError):
                self.repo.transact_native(0, 'busy', {}, fail)
        reopened = EditorRepository(self.repo.root)
        attempt, = reopened.recovery()
        self.assertEqual(attempt['status'], 'building')
        reopened.discard_incomplete('busy', 0)
        archive = self.root / 'clean.zip'
        reopened.archive(archive)
        self.assertEqual(EditorRepository.restore_archive(archive, self.root / 'clean').snapshot(),
                         self.repo.snapshot())

    def test_interrupted_build_journals_physical_directory_identity(self):
        child = """import os, sys
from editor3d_repository import EditorRepository
def stop(source, destination, scene):
    os._exit(43)
EditorRepository(sys.argv[1]).transact_native(0, 'identity', {}, stop)
"""
        exited = subprocess.run([sys.executable, '-B', '-c', child, str(self.repo.root)],
                                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts')),
                                capture_output=True, timeout=20)
        self.assertEqual(exited.returncode, 43)
        reopened = EditorRepository(self.repo.root)
        item, = reopened.recovery()
        stage = self.repo.root / 'revisions' / item['stage']
        info = stage.stat(follow_symlinks=False)
        self.assertGreater(info.st_ino, 0)
        self.assertEqual(item['stage_identity'], [info.st_dev, info.st_ino])
        journal, = (self.repo.root / 'journal').glob('*.json')
        original = journal.read_bytes()
        with patch('editor3d_repository._directory_identity', return_value=(info.st_dev, info.st_ino + 1)):
            with self.assertRaises(RigError) as refused:
                reopened.discard_incomplete('identity', 0)
        self.assertEqual(refused.exception.report['code'], 'EDITOR3D_RECOVERY')
        self.assertEqual(journal.read_bytes(), original)
        self.assertTrue(stage.is_dir())
        # Legacy building journals carry no proof that the named directory is theirs.
        legacy = json.loads(original)
        del legacy['stage_identity']
        journal.write_text(json.dumps(legacy), encoding='utf-8')
        with self.assertRaises(RigError):
            reopened.discard_incomplete('identity', 0)
        self.assertTrue(stage.exists())
        self.assertTrue(journal.exists())
        journal.write_bytes(original)
        reopened.discard_incomplete('identity', 0)
        self.assertFalse(stage.exists())

    def test_discard_retains_unadmittable_stages_outside_the_repository(self):
        outside = self.root / 'outside-target'
        outside.mkdir()
        (outside / 'keep.txt').write_bytes(b'keep')
        builders = {
            'alias': "(destination / 'Part.obj').write_bytes(b'a'); (destination / 'part.obj').write_bytes(b'b')",
            'reserved': "(destination / 'aux.tmp').write_bytes(b'x')",
            'link': "os.symlink(sys.argv[2], destination / 'escape', target_is_directory=True)",
        }
        # Detect case-insensitive volumes (including the normal Windows and
        # macOS filesystems). Full Unicode case folding also aliases two names
        # that such volumes can store independently.
        probe = self.root / 'CaseProbe'
        probe.touch()
        if (self.root / 'caseprobe').exists():
            builders['alias'] = "(destination / 'mass.obj').touch(); (destination / 'ma\\u00df.obj').touch()"
        if os.name == 'nt':
            # AUX is a real Windows device. DEL is legal for a local filename
            # and exercises the same portable-name rejection without opening it.
            del builders['reserved']
            builders['nonportable'] = "(destination / ('invalid' + chr(127) + '.obj')).touch()"
        if os.name == 'posix' and os.geteuid() != 0:
            builders['unreadable'] = "(destination / 'unreadable').mkdir(mode=0)"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts'))
        for name, body in builders.items():
            with self.subTest(stage=name):
                repo = EditorRepository.create(self.source, self.root / ('unadmittable-' + name))
                before = repo.snapshot()
                child = f"""import os
import sys
from editor3d_repository import EditorRepository
repo = EditorRepository(sys.argv[1])
def unverified(source, destination, scene):
    destination.mkdir()
    {body}
    os._exit(47)
repo.transact_native(0, 'interrupted-{name}', {{}}, unverified)
"""
                exited = subprocess.run([sys.executable, '-B', '-c', child, str(repo.root), str(outside)],
                                        env=env, capture_output=True, timeout=20)
                if name == 'link' and os.name == 'nt' and b'WinError 1314' in exited.stderr:
                    self.skipTest('directory symlinks require a Windows privilege')
                self.assertEqual(exited.returncode, 47, exited.stderr)
                reopened = EditorRepository(repo.root)
                item, = reopened.recovery()
                with self.assertRaises(RigError) as blocked:
                    reopened.apply(0, self.move(8), 'blocked-by-recovery')
                self.assertEqual(blocked.exception.report['code'], 'EDITOR3D_RECOVERY')
                result = reopened.discard_incomplete('interrupted-' + name, 0, expected_sha256=before['sha256'])
                retained = Path(result['retained_stage'])
                self.assertFalse(retained.is_relative_to(repo.root))
                self.assertTrue(retained.is_dir() and not retained.is_symlink())
                if name == 'link':
                    self.assertTrue((retained / 'native' / 'escape').is_symlink())
                if name == 'unreadable':
                    locked = retained / 'native' / 'unreadable'
                    self.assertEqual(locked.stat().st_mode & 0o777, 0)
                    locked.chmod(0o700)
                self.assertFalse(os.path.lexists(repo.root / 'revisions' / item['stage']))
                self.assertFalse(reopened.recovery())
                self.assertEqual(self.mesh_x(reopened.apply(0, self.move(8), 'after-discard')), 8)
                reopened.archive(self.root / (name + '.zip'))
        self.assertEqual((outside / 'keep.txt').read_bytes(), b'keep')

    def test_discard_reports_retained_over_entry_budget_stage(self):
        child = """import os, sys
from editor3d_repository import EditorRepository
def stop(source, destination, scene):
    destination.mkdir()
    for index in range(128):
        (destination / str(index)).touch()
    os._exit(41)
EditorRepository(sys.argv[1]).transact_native(0, 'many', {}, stop)
"""
        repo = EditorRepository.create(self.source, self.root / 'bounded', limits={'max_files': 32})
        exited = subprocess.run([sys.executable, '-B', '-c', child, str(repo.root)],
                                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts')),
                                capture_output=True, timeout=20)
        self.assertEqual(exited.returncode, 41)
        reopened = EditorRepository(repo.root)
        result = reopened.discard_incomplete('many', 0)
        retained = Path(result['retained_stage'])
        self.assertFalse(retained.is_relative_to(repo.root))
        self.assertEqual(len(list((retained / 'native').iterdir())), 128)
        self.assertFalse(reopened.recovery())
        reopened.archive(self.root / 'bounded.zip')

    def test_archive_receipt_has_a_separate_byte_allowance(self):
        import editor3d_repository as module

        payload = sum(record['bytes'] for record in self.repo._all_files().values())
        # Store a matching local ceiling in the fixture's metadata before measuring it.
        metadata = self.repo.root / 'repository.json'
        value = json.loads(metadata.read_bytes())
        while value['limits']['max_bytes'] != payload:
            value['limits']['max_bytes'] = payload
            module._write_metadata(metadata, value)
            payload = sum(record['bytes'] for record in self.repo._all_files().values())
        with patch.dict(module.DEFAULT_LIMITS, max_bytes=payload):
            repo = EditorRepository(self.repo.root)
            archive = self.root / 'exact.zip'
            repo.archive(archive)
            self.assertEqual(EditorRepository.restore_archive(archive, self.root / 'exact').snapshot(),
                             repo.snapshot())

    def test_restored_repository_obeys_configured_retention_limits(self):
        import editor3d_repository as module

        archive = self.root / 'saved.zip'
        self.repo.archive(archive)
        files = self.repo._all_files()
        validated = module._validated_limits
        for key, maximum in (('max_files', len(files) - 1),
                             ('max_bytes', sum(record['bytes'] for record in files.values()) - 1)):
            with self.subTest(key=key):
                destination = self.root / key
                with patch.object(module, '_validated_limits',
                                  side_effect=lambda values: dict(validated(values), **{key: maximum})):
                    with self.assertRaises(RigError):
                        EditorRepository.restore_archive(archive, destination)
                self.assertFalse(destination.exists())

    def test_export_checks_selected_scene_of_every_native_revision(self):
        import editor3d_repository as module

        self.repo.apply(0, self.move(), 'move')
        load = module.load_project
        initial = self.repo.snapshot(0)
        portable = self.root / 'portable.zip'
        self.repo.archive(portable)

        def load_native(path):
            bundle = load(path)
            if path.parent.name == '00000000':
                bundle['documents'][initial['scene_path']]['kind'] = 'scene3d-project'
            return bundle

        with patch.object(module, 'load_project', side_effect=load_native):
            for operation, destination in ((self.repo.archive, self.root / 'selected.zip'),
                                           (self.repo.save_as, self.root / 'selected'),
                                           (lambda dest: EditorRepository.restore_archive(portable, dest),
                                            self.root / 'selected-restore')):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(RigError) as refused:
                        operation(destination)
                    self.assertEqual(refused.exception.report['code'], 'EDITOR3D_SCENE')
                    self.assertFalse(destination.exists())

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
        failure_dir = reopened.root / 'failed'
        original, = failure_dir.glob('*.json')
        original_bytes = original.read_bytes()
        first_stage = reopened.root / 'revisions' / report[0]['stage']
        self.assertFalse(first_stage.exists())
        retry = subprocess.run([sys.executable, '-c', script, str(reopened.root)],
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'scripts')}, timeout=15)
        self.assertEqual(retry.returncode, 17)
        item, = reopened.recovery()
        second_stage = reopened.root / 'revisions' / item['stage']
        self.assertTrue(second_stage.is_dir())
        self.assertNotEqual(first_stage, second_stage)
        journal, = (reopened.root / 'journal').glob('*.json')
        retry_bytes = journal.read_bytes()
        reopened.discard_incomplete('crashed', 0)
        self.assertFalse(reopened.recovery())
        self.assertFalse(second_stage.exists())
        self.assertEqual(original.read_bytes(), original_bytes)
        remaining, = set(failure_dir.glob('*.json')) - {original}
        self.assertEqual(remaining.read_bytes(), retry_bytes)
        self.assertEqual(reopened.apply(0,self.move(),'after-recovery')['revision'],1)


if __name__=='__main__':unittest.main()
