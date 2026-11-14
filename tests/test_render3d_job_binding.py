"""Resolve required job context through the one native SQLite authority."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from render3d_contracts import Render3DError
from render3d_jobs import JobStore
from rig_errors import object_hash
import test_render3d_jobs as job_fixtures


class JobBindingTests(unittest.TestCase):
    def setUp(self):
        self.case=job_fixtures.JobTests();self.case.setUp();self.addCleanup(self.case.tearDown)
        self.case.request['priority']=9;self.case.request['max_attempts']=3

    def test_resolves_exact_native_request_and_persists_across_reopen(self):
        case=self.case;job=case.submit();view=case.store.status(job)
        self.assertEqual(view['request_binding']['job_id'],job)
        self.assertEqual(view['request_sha256'],object_hash(case.request))
        reopened=JobStore(case.root/'jobs')
        resolved=reopened.resolve_request(job,view['request_sha256'])
        self.assertEqual(resolved,case.request)
        resolved['priority']=999
        self.assertEqual(reopened.resolve_request(job,view['request_sha256'])['priority'],9)
        self.assertEqual((view['project'],view['scene'],view['shot']),
                         tuple(case.request[key] for key in ('project','scene','shot')))
        self.assertEqual(view['priority'],9)
        self.assertEqual(view['retry_policy']['max_attempts'],3)
        self.assertIn('worker-crash',view['retry_policy']['retryable_categories'])
        self.assertEqual(view['counts'],dict(requested=2,queued=2,active=0,completed=0,failed=0,cancelled=0))
        self.assertEqual(view['progress']['completion_ratio'],dict(numerator=0,denominator=1))

    def test_rejects_missing_stale_and_tampered_native_bindings(self):
        case=self.case;job=case.submit();digest=object_hash(case.request)
        with self.assertRaises(Render3DError):case.store.resolve_request(job,'0'*64)
        with self.assertRaises(Render3DError):case.store.resolve_request('bad-id',digest)
        with self.assertRaises(Render3DError):case.store.status([])
        with self.assertRaises(Render3DError):JobStore(case.root/'empty').resolve_request(job,digest)
        for field,value in (('request',dict(case.request,priority=8)),('backend',dict(case.backend,worker_sha256='f'*64)),('request',[])):
            with self.subTest(field=field,value=value):
                with case.store._transaction() as db:
                    old=db.execute('SELECT '+field+' FROM jobs WHERE id=?',(job,)).fetchone()[0]
                    db.execute('UPDATE jobs SET '+field+'=? WHERE id=?',(json.dumps(value),job))
                try:
                    with self.assertRaises(Render3DError):case.store.resolve_request(job,digest)
                    with self.assertRaises(Render3DError):case.store.status(job)
                finally:
                    with case.store._transaction() as db:db.execute('UPDATE jobs SET '+field+'=? WHERE id=?',(old,job))

    def test_rejects_mutated_scheduling_priority_and_frame_partition(self):
        case=self.case;job=case.submit();digest=object_hash(case.request)
        with case.store._transaction() as db:db.execute('UPDATE jobs SET priority=8 WHERE id=?',(job,))
        with self.assertRaises(Render3DError):case.store.resolve_request(job,digest)
        with self.assertRaises(Render3DError):case.store.status(job)
        with case.store._transaction() as db:
            db.execute('UPDATE jobs SET priority=9 WHERE id=?',(job,))
            db.execute('UPDATE frames SET frame=7 WHERE job=? AND frame=6',(job,))
        with self.assertRaises(Render3DError):case.store.status(job)

    def test_progress_and_worker_reservations_follow_native_lifecycle(self):
        case=self.case;job=case.submit();first=case.store.claim(job)
        active=case.store.status(job)
        self.assertEqual(active['reservation']['workers'],case.request['settings']['resources']['workers'])
        self.assertEqual(active['reservation']['memory_bytes'],case.request['settings']['resources']['memory_bytes'])
        self.assertEqual(active['frames'][0]['token'],first['token'])
        self.assertEqual(active['frames'][0]['pid'],os.getpid())
        output,result=case.output(first);case.store.publish(first,output,result,case.verify)
        halfway=case.store.status(job)
        self.assertEqual(halfway['progress']['completion_percent'],50)
        self.assertEqual(halfway['progress']['completion_ratio'],dict(numerator=1,denominator=2))
        self.assertEqual(halfway['reservation']['workers'],0)
        second=case.store.claim(job);case.store.fail(second,'permanent failure')
        failed=case.store.status(job)
        self.assertEqual(failed['state'],'failed')
        self.assertEqual(failed['progress']['completion_percent'],50)
        self.assertEqual(failed['progress']['settled_percent'],100)
        case.store.resume(job,retry_failed=True,verifier=case.verify);case.store.cancel(job)
        cancelled=case.store.status(job)
        self.assertEqual(cancelled['counts']['cancelled'],1)
        self.assertEqual(cancelled['counts']['failed'],0)
        self.assertEqual(cancelled['progress']['completion_percent'],50)
        self.assertEqual(cancelled['progress']['settled_percent'],100)

    def test_cache_stage_reservation_is_bound_to_its_native_job(self):
        import render3d_jobs as module
        case=self.case;job=case.submit();lease=case.store.claim(job);output,result=case.output(lease)
        observed=[];original=module._copy_artifact
        def observe(source,target,reference):
            if not observed:observed.append(case.store.status(job)['reservation'])
            return original(source,target,reference)
        with patch.object(module,'_copy_artifact',side_effect=observe):
            case.store.publish(lease,output,result,case.verify)
        self.assertGreater(observed[0]['cache_staging_bytes'],0)
        self.assertEqual(observed[0]['worker_storage_bytes'],case.request['settings']['resources']['storage_bytes'])
        self.assertEqual(case.store.status(job)['reservation']['cache_staging_bytes'],0)

    def test_older_stage_table_migrates_without_losing_unattributed_reservations(self):
        case=self.case;job=case.submit()
        with case.store._transaction() as db:
            db.execute('DROP TABLE storage_reservations')
            db.execute('CREATE TABLE storage_reservations(stage TEXT PRIMARY KEY,bytes INTEGER NOT NULL,pid INTEGER NOT NULL)')
            db.execute('INSERT INTO storage_reservations VALUES(?,?,?)',('cache-old',50,os.getpid()))
        reopened=JobStore(case.root/'jobs');view=reopened.status(job)
        self.assertEqual(view['reservation']['unattributed_store_staging_bytes'],50)
        self.assertEqual(view['reservation']['cache_staging_bytes'],0)
        with reopened._transaction() as db:
            row=db.execute('SELECT * FROM storage_reservations').fetchone()
            self.assertEqual(row['bytes'],50);self.assertIsNone(row['job'])


if __name__=='__main__':unittest.main()
