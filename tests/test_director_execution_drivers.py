"""Actual native SQLite leases and decoded synthetic-cache adapter controls.

Synthetic PNG cache records exercise verification; they are not Blender or
production-render evidence. No existing Director film is rendered again.
"""
from copy import deepcopy
import inspect
from pathlib import Path
import unittest

from director_execution_drivers import RenderJobDriver
from rig_errors import RigError, object_hash


class RenderExecutionDriverTests(unittest.TestCase):
    def setUp(self):
        # Reuse the native job owner's existing explicitly synthetic test setup.
        from test_render3d_jobs import JobTests
        self.fixture = JobTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.job = self.fixture.submit()
        state = self.store.status(self.job)
        self.binding = dict(job=self.job, request_sha256=object_hash(state['request']),
                            backend_sha256=object_hash(state['backend']),
                            snapshot_sha256=state['request']['snapshot']['sha256'])
        self.driver = RenderJobDriver(self.store, None, self.fixture.verify)

    def test_native_claim_heartbeat_cancel_and_resume_keep_original_job(self):
        self.assertEqual(self.driver.inspect(self.binding)['state'], 'queued')
        lease = self.store.claim(self.job)
        self.assertEqual(self.driver.inspect(self.binding)['state'], 'running')
        self.assertFalse(self.driver.heartbeat(self.binding, lease))
        self.driver.cancel(self.binding)
        self.assertTrue(self.driver.heartbeat(self.binding, lease))
        self.store.fail(lease, 'known cancelled worker')
        self.assertEqual(self.driver.inspect(self.binding)['state'], 'cancelled')
        self.driver.resume(self.binding)
        self.assertEqual(self.driver.inspect(self.binding)['state'], 'queued')
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_changed_binding_rejects_before_claiming_or_changing_native_state(self):
        before = self.store.status(self.job)
        for key in ('request_sha256', 'backend_sha256', 'snapshot_sha256'):
            wrong = deepcopy(self.binding)
            wrong[key] = '0' * 64
            with self.subTest(key=key), self.assertRaises(RigError):
                self.driver.inspect(wrong)
        self.assertEqual(before, self.store.status(self.job))

    def test_graph_cannot_substitute_unrelated_snapshot_reference(self):
        node = dict(operation='RenderAdapter.submit', inputs=[dict(sha256='0' * 64, byte_size=1)])
        with self.assertRaises(RigError):
            self.driver.validate(self.binding, node, self.fixture.root)

    def test_status_label_without_verified_cache_never_completes(self):
        # Deliberately corrupt persistent native state, rather than mock a
        # successful backend response. The adapter must still require bytes.
        with self.store._transaction() as db:
            db.execute('UPDATE frames SET state="complete" WHERE job=?', (self.job,))
            self.store._refresh(db, self.job)
        self.assertEqual(self.store.status(self.job)['state'], 'complete')
        with self.assertRaises(ValueError):
            self.driver.inspect(self.binding)

    def test_all_completed_synthetic_cache_images_are_decoded_and_hash_bound(self):
        while True:
            lease = self.store.claim(self.job)
            if lease is None:
                break
            output, result = self.fixture.output(lease)
            self.store.publish(lease, output, result, self.fixture.verify)
        report = self.driver.inspect(self.binding)
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(len(report['artifacts']), 2)
        self.assertIsNone(report['actual_usage'])
        self.assertTrue(all(ref['fixture'] for ref in report['artifacts']))
        original = report['artifacts'][0]
        image = Path(report['artifact_root']) / original['path']
        image.write_bytes(b'corrupt source image')
        with self.assertRaises(ValueError):
            self.driver.inspect(self.binding)

    def test_dispatch_requires_atomic_native_admission(self):
        before = self.store.status(self.job)
        if 'admit' not in inspect.signature(self.store.run_one).parameters:
            with self.assertRaisesRegex(RigError, 'atomic DAG admission'):
                self.driver.dispatch(self.binding, admit=lambda db, record: False)
        else:
            self.assertIsNone(self.driver.dispatch(self.binding, admit=lambda db, record: False))
        self.assertEqual(before, self.store.status(self.job))


if __name__ == '__main__':
    unittest.main()
