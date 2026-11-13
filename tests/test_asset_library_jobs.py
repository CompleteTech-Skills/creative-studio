"""Real local publication and process-exit recovery tests for asset jobs."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from asset_library_jobs import JobManager
from rig_errors import RigError, atomic_json


class FileAdapter:
    def __init__(self, destination, *, crash=None):
        self.destination = Path(destination)
        self.calls = []
        self.crash = crash

    def stage(self, context):
        self.calls.append('stage')
        context.progress(.1, 'copy-exact-original')
        data = context.parameters['text'].encode()
        target = context.stage_dir / 'exact.txt'
        target.write_bytes(data)
        return {'path': 'exact.txt', 'sha256': __import__('hashlib').sha256(data).hexdigest()}

    def verify(self, context, staged):
        self.calls.append('verify')
        data = (context.stage_dir / staged['path']).read_bytes()
        assert __import__('hashlib').sha256(data).hexdigest() == staged['sha256']
        return {'sha256': staged['sha256'], 'bytes': len(data)}

    def recover(self, context, staged, verified):
        self.calls.append('recover')
        marker = self.destination / (context.token + '.json')
        if marker.exists():
            receipt = json.loads(marker.read_text())
            assert receipt['sha256'] == verified['sha256']
            assert __import__('hashlib').sha256((self.destination / receipt['file']).read_bytes()).hexdigest() == receipt['sha256']
            return {'status': 'published', 'receipt': receipt}
        return {'status': 'unpublished'}

    def publish(self, context, staged, verified):
        self.calls.append('publish')
        self.destination.mkdir(exist_ok=True)
        filename = context.token + '.txt'
        data = (context.stage_dir / staged['path']).read_bytes()
        with (self.destination / filename).open('xb') as stream:
            stream.write(data)
        receipt = {'token': context.token, 'file': filename, **verified}
        atomic_json(self.destination / (context.token + '.json'), receipt)
        if self.crash == 'published':
            os._exit(72)
        return receipt


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = JobManager(self.root / 'jobs', create=True)
        self.adapter = FileAdapter(self.root / 'published')

    def error(self, code, function):
        with self.assertRaises(RigError) as caught:
            function()
        self.assertEqual(caught.exception.report['code'], code)

    def test_real_exact_publication_idempotency_and_fifo(self):
        first = self.manager.submit('import', {'text': 'source bytes'}, key='one')
        self.assertEqual(first, self.manager.submit('import', {'text': 'source bytes'}, key='one'))
        self.error('ASSET_JOB_CONFLICT', lambda: self.manager.submit('import', {'text': 'other'}, key='one'))
        self.manager.submit('preview', {'text': 'next'}, key='two')
        self.error('ASSET_JOB_QUEUED', lambda: self.manager.run('two', self.adapter))
        self.assertEqual([r['queue_position'] for r in self.manager.list()], [1, 2])
        result = self.manager.run('one', self.adapter)
        self.assertEqual(result['status'], 'published')
        self.assertEqual((self.adapter.destination / result['receipt']['file']).read_bytes(), b'source bytes')
        self.assertEqual(self.manager.run('one', self.adapter), result)
        self.assertEqual(self.adapter.calls, ['stage', 'verify', 'recover', 'publish'])
        self.assertEqual(self.manager.run('two', self.adapter)['status'], 'published')
        self.error('ASSET_JOB_PUBLISHED', lambda: self.manager.cancel('one'))

    def test_cancel_before_start_then_explicit_resume(self):
        self.manager.submit('preview', {'text': 'preserved'}, key='cancel')
        self.manager.cancel('cancel')
        self.error('ASSET_JOB_CANCELLED', lambda: self.manager.run('cancel', self.adapter))
        self.assertEqual(self.adapter.calls, [])
        self.manager.resume('cancel')
        self.assertEqual(self.manager.run('cancel', self.adapter)['status'], 'published')

    def test_cooperative_cancel_after_staging_never_publishes(self):
        manager = self.manager
        class Cancel(FileAdapter):
            def verify(self, context, staged):
                manager.cancel(context.id)
                return super().verify(context, staged)
        self.manager.submit('import', {'text': 'original'}, key='cancel')
        adapter = Cancel(self.root / 'published')
        self.error('ASSET_JOB_CANCELLED', lambda: self.manager.run('cancel', adapter))
        self.assertFalse(adapter.destination.exists())
        self.assertEqual(self.manager.status('cancel')['status'], 'cancelled')
        self.assertEqual((self.root / 'jobs/staging/cancel/exact.txt').read_bytes(), b'original')
        self.manager.resume('cancel')
        self.assertEqual(self.manager.run('cancel', self.adapter)['status'], 'published')
        self.assertNotIn('stage', self.adapter.calls)

    def test_real_process_exit_after_publication_recovers_without_duplicate(self):
        self.manager.submit('archive', {'text': 'immutable'}, key='crash')
        code = """
import sys
from pathlib import Path
from asset_library_jobs import JobManager
from test_asset_library_jobs import FileAdapter
root=Path(sys.argv[1])
JobManager(root/'jobs').run('crash',FileAdapter(root/'published',crash='published'))
"""
        completed = subprocess.run([sys.executable, '-c', code, str(self.root)], check=False,
                                   env={**os.environ, 'PYTHONPATH': 'scripts:tests'})
        self.assertEqual(completed.returncode, 72)
        self.assertEqual(self.manager.status('crash')['status'], 'interrupted')
        self.error('ASSET_JOB_RECOVERY',lambda:self.manager.cancel('crash'))
        self.assertEqual(self.manager.status('crash')['status'],'interrupted')
        self.manager.resume('crash')
        result = self.manager.run('crash', self.adapter)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(self.adapter.calls, ['recover'])
        self.assertEqual(len(list(self.adapter.destination.glob('*.txt'))), 1)
        self.assertEqual(result['attempts'], 2)

    def test_process_exit_at_staged_and_verified_checkpoints(self):
        for checkpoint in ('staged', 'verified'):
            with self.subTest(checkpoint=checkpoint):
                name = 'exit-' + checkpoint
                self.manager.submit('preview', {'text': checkpoint}, key=name)
                code = """
import os,sys
from pathlib import Path
from asset_library_jobs import JobManager
from test_asset_library_jobs import FileAdapter
class CrashManager(JobManager):
 def _transition(self, identifier, state, **changes):
  result=super()._transition(identifier,state,**changes)
  if state==sys.argv[2]:os._exit(73)
  return result
root=Path(sys.argv[1]);CrashManager(root/'jobs').run(sys.argv[3],FileAdapter(root/'published'))
"""
                result = subprocess.run([sys.executable, '-c', code, str(self.root), checkpoint, name],
                                        env={**os.environ, 'PYTHONPATH': 'scripts:tests'})
                self.assertEqual(result.returncode, 73)
                self.assertEqual(self.manager.status(name)['status'], 'interrupted')
                self.manager.resume(name)
                adapter = FileAdapter(self.root / 'published')
                result = self.manager.run(name, adapter)
                self.assertEqual(result['status'], 'published')
                self.assertNotIn('stage', adapter.calls)
                self.assertEqual('verify' in adapter.calls, checkpoint == 'staged')
                self.assertEqual((adapter.destination / result['receipt']['file']).read_bytes(), checkpoint.encode())

    def test_actual_staged_library_verify_and_final_publication_keep_inventory(self):
        from asset_library_operations import AssetOperations, OperationAdapter
        from test_asset_library import AssetLibraryTests
        source = AssetLibraryTests('test_actual_geometry_registration_restart_relocation_and_exact_bytes')
        source.setUp()
        self.addCleanup(source.doCleanups)
        original = source.mesh.read_bytes()
        owner = AssetOperations(source.library, jobs_root=self.root / 'jobs')
        class LibraryAdapter(OperationAdapter):
            def stage(self, context):
                library = self._stage_library(context)
                reference = library.register(source.manifest, source.sources)
                self._prepare_import_payloads(context, library, reference)
                return {'reference': reference}
            def verify(self, context, staged):
                manifest=self._stage_library(context).resolve(staged['reference'])
                return dict(reference=staged['reference'],manifest_sha256=manifest['manifest_sha256'])
        self.manager.submit('import', {}, key='library')
        result = self.manager.run('library', LibraryAdapter(owner, 'import', {}))
        self.assertEqual(result['status'], 'published')
        reference = result['receipt']['reference']
        self.assertEqual(source.library.resolve(reference), source.manifest)
        self.assertEqual(source.library.read_file(reference, source.files[0]['path']), original)
        self.assertEqual(source.mesh.read_bytes(), original)
        self.assertEqual(self.manager._inventory('library'), result['staged']['inventory'])

    def test_runtime_failure_and_durable_pool_configuration(self):
        manager = JobManager(self.root / 'short', limits={'runtime_seconds': 1, 'io': 1.0}, create=True)
        self.assertIs(type(manager.limits['io']),int)
        manager.submit('import', {'text': 'slow'}, key='slow')
        self.assertEqual(JobManager(self.root / 'short').limits['io'], 1)
        self.error('ASSET_JOB_LIMIT', lambda: JobManager(self.root / 'short', limits={'io': 2}))
        class Slow(FileAdapter):
            def stage(self, context):
                time.sleep(1.05)
                context.check_cancelled()
                return super().stage(context)
        adapter = Slow(self.root / 'published')
        self.error('ASSET_JOB_TIMEOUT', lambda: manager.run('slow', adapter))
        self.assertFalse(adapter.destination.exists())
        self.assertEqual(manager.status('slow')['status'], 'failed')
        self.assertEqual(manager.status('slow')['error']['code'], 'ASSET_JOB_TIMEOUT')

    def test_retained_staging_tamper_and_ambiguous_publisher_fail_closed(self):
        class Ambiguous(FileAdapter):
            def recover(self, context, staged, verified):
                return {'status': 'unknown'}
        self.manager.submit('index', {'text': 'input'}, key='ambiguous')
        adapter = Ambiguous(self.root / 'published')
        self.error('ASSET_JOB_RECOVERY', lambda: self.manager.run('ambiguous', adapter))
        self.assertFalse(adapter.destination.exists())
        (self.root / 'jobs/staging/ambiguous/exact.txt').write_bytes(b'mutated')
        self.manager.resume('ambiguous')
        self.error('ASSET_JOB_CHANGED', lambda: self.manager.run('ambiguous', self.adapter))
        self.assertEqual(self.adapter.calls, [])

    def test_resource_slots_release_on_worker_exit_and_bound_concurrency(self):
        self.manager.submit('import', {'text': 'one'}, resources=['blender'], key='first')
        self.manager.submit('import', {'text': 'two'}, resources=['blender'], key='second')
        code = """
import sys,time
from pathlib import Path
from asset_library_jobs import JobManager
from test_asset_library_jobs import FileAdapter
class Blocking(FileAdapter):
 def stage(self,c):
  (Path(sys.argv[1])/'started').write_text('yes')
  time.sleep(30)
root=Path(sys.argv[1]);JobManager(root/'jobs').run('first',Blocking(root/'published'))
"""
        worker = subprocess.Popen([sys.executable, '-c', code, str(self.root)],
                                  env={**os.environ, 'PYTHONPATH': 'scripts:tests'})
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / 'started').exists())
            self.assertEqual(self.manager.status('first')['status'], 'running')
            self.error('ASSET_JOB_BUSY', lambda: self.manager.run('second', self.adapter))
        finally:
            worker.kill(); worker.wait(timeout=5)
        self.assertEqual(self.manager.status('first')['status'], 'interrupted')
        self.assertEqual(self.manager.run('second', self.adapter)['status'], 'published')

    def test_limits_paths_bad_data_and_runtime(self):
        self.error('ASSET_JOB_ID', lambda: self.manager.submit('../bad', {}))
        self.error('ASSET_JOB_JSON', lambda: self.manager.submit('import', {'x': float('nan')}))
        self.error('ASSET_JOB_JSON', lambda: self.manager.submit('import', {1: 'non-string'}))
        self.error('ASSET_JOB_RESOURCE', lambda: self.manager.submit('import', {}, resources=[{}]))
        self.error('ASSET_JOB_LIMIT', lambda: JobManager(self.root / 'jobs', limits={'blender': 9}))
        self.error('ASSET_JOB_LIMIT', lambda: JobManager(self.root / 'jobs', limits={'runtime_seconds': True}))
        limited = JobManager(self.root / 'limited', limits={'stage_bytes': 2}, create=True)
        limited.submit('import', {'text': 'long'}, key='large')
        self.error('ASSET_JOB_LIMIT', lambda: limited.run('large', self.adapter))
        self.assertFalse(self.adapter.destination.exists())
        if os.name == 'posix':
            (self.root / 'alias').symlink_to(self.root / 'jobs', target_is_directory=True)
            self.error('ASSET_JOB_PATH', lambda: JobManager(self.root / 'alias'))

    def test_corrupt_record_and_stage_links_reject(self):
        self.manager.submit('import', {'text': 'bytes'}, key='bad')
        record = self.root / 'jobs/records/bad.json'
        value = json.loads(record.read_text());value['parameters']['text'] = 'changed'
        atomic_json(record, value)
        self.error('ASSET_JOB_CORRUPT', lambda: self.manager.status('bad'))
        if os.name == 'posix':
            self.manager = JobManager(self.root / 'links', create=True)
            self.manager.submit('import', {'text': 'bytes'}, key='linked')
            class Linked(FileAdapter):
                def stage(self, context):
                    outside = context.manager.root.parent / 'outside';outside.write_bytes(b'outside')
                    (context.stage_dir / 'link').symlink_to(outside)
                    return {}
            self.error('ASSET_JOB_PATH', lambda: self.manager.run('linked', Linked(self.root / 'published')))


if __name__ == '__main__':
    unittest.main()
