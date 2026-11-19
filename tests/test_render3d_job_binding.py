"""Resolve required job context through the one native SQLite authority."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
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

    def test_claim_validates_native_scheduler_inputs_before_leasing(self):
        case=self.case;job=case.submit()
        # Exercise both direct and global admission independently of status().
        changes=[('jobs','priority',8),('jobs','request',json.dumps(dict(case.request,priority=8))),
                 ('jobs','backend',json.dumps(dict(case.backend,worker_sha256='f'*64))),
                 ('frames','spec','{}'),('frames','cache_key','0'*64),('frames','attempts',3)]
        for table,field,value in changes:
            where='id=?' if table=='jobs' else 'job=? AND frame=5'
            with self.subTest(table=table,field=field):
                with case.store._transaction() as db:
                    old=db.execute(f'SELECT {field} FROM {table} WHERE {where}',(job,)).fetchone()[0]
                    db.execute(f'UPDATE {table} SET {field}=? WHERE {where}',(value,job))
                for selection in (None,job):
                    with self.assertRaises(Render3DError):case.store.claim(selection)
                with case.store._transaction() as db:
                    db.execute(f'UPDATE {table} SET {field}=? WHERE {where}',(old,job))
                    self.assertEqual(db.execute('SELECT count(*) FROM events WHERE event="claimed"').fetchone()[0],0)
        self.assertEqual(case.store.claim(job)['frame'],5)

    def test_active_job_resources_are_checked_before_another_claim(self):
        case=self.case;case.store=JobStore(case.root/'parallel',workers=2);job=case.submit()
        case.store.claim(job)
        with case.store._transaction() as db:
            db.execute('UPDATE jobs SET request=? WHERE id=?',(json.dumps(dict(case.request,priority=8)),job))
        with self.assertRaises(Render3DError):case.store.claim()
        with case.store._transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM frames WHERE frame=6').fetchone()[0],'queued')

    def test_scheduler_skips_resource_blocked_job_without_checking_each_frame(self):
        case=self.case;case.store=JobStore(case.root/'parallel',workers=2);job=case.submit()
        blocked=deepcopy(case.request);blocked['priority']=10;blocked['destination']['path']='blocked'
        blocked['settings']['resources']['workers']=2
        blocked['frame_range']['end']=9
        case.submit(blocked)
        case.store.claim(job)
        with patch.object(case.store,'_checked_request',wraps=case.store._checked_request) as checked:
            self.assertEqual(case.store.claim()['job'],job)
            self.assertEqual(checked.call_count,2)  # one live frame and the selected job

    def test_stale_resume_does_not_recover_or_release_a_dead_claim(self):
        case=self.case;job=case.submit();case.store.claim(job)
        before=case.store.status(job)
        with patch('render3d_jobs._alive',return_value=False):
            with self.assertRaisesRegex(Render3DError,'Stale job revision'):
                case.store.resume(job,expected_revision=before['revision']-1)
            self.assertEqual(case.store.status(job),before)
            case.store.resume(job,expected_revision=before['revision'])
        after=case.store.status(job)
        self.assertEqual((after['frames'][0]['state'],after['frames'][0]['attempts']),('queued',1))
        self.assertEqual(after['revision'],before['revision']+2)

    def test_final_attempt_cancellation_settles_on_resume(self):
        case=self.case;case.request['frame_range']['end']=6;case.request['max_attempts']=1
        job=case.submit();lease=case.store.claim(job);case.store.cancel(job);case.store.fail(lease,'cancelled')
        self.assertEqual(case.store.status(job)['state'],'cancelled')
        case.store.resume(job,retry_failed=True)
        current=case.store.status(job)
        self.assertEqual((current['state'],current['frames'][0]['state'],current['frames'][0]['attempts']),
                         ('failed','failed',1))
        self.assertIsNone(case.store.claim(job))

    def test_materialization_uses_latest_native_completion_per_frame(self):
        case=self.case;job=case.submit();first=case.store.claim(job)
        output,result=case.output(first);case.store.publish(first,output,result,case.verify)
        other=deepcopy(case.request);other['destination']['path']='relocated'
        job=case.submit(other);first=case.store.claim(job)
        self.assertTrue(case.store.reuse(first,case.verify))
        second=case.store.claim(job);output,result=case.output(second)
        case.store.publish(second,output,result,case.verify)
        # Legacy job events can outlive the status history window.
        with case.store._transaction() as db:
            db.executemany('INSERT INTO events(job,at,event,detail) VALUES(?,0,"diagnostic","{}")',[(job,)]*1001)
        result=JobStore(case.root/'jobs').materialize(job,case.root/'delivery',case.verify)
        self.assertEqual((result['successful'],result['reused']),([6],[5]))

    @unittest.skipIf(os.name=='nt','POSIX descriptor-relative exclusive rename injection')
    def test_pre_rename_interruption_can_retry_in_its_original_parent(self):
        case=self.case;case.request['frame_range']['end']=6
        job=case.submit();lease=case.store.claim(job);output,result=case.output(lease)
        case.store.publish(lease,output,result,case.verify)
        destination=case.root/'new'/'nested'/'delivery'
        with patch('render3d_jobs._rename_no_replace',side_effect=OSError('simulated interruption')):
            with self.assertRaisesRegex(OSError,'simulated interruption'):
                case.store.materialize(job,destination,case.verify)
        self.assertFalse(destination.exists())
        original=destination.parent.with_name('original')
        destination.parent.rename(original);destination.parent.mkdir()
        with self.assertRaisesRegex(Render3DError,'pending delivery'):
            case.store.materialize(job,destination,case.verify)
        self.assertFalse(destination.exists())
        destination.parent.rename(original.with_name('replacement'));original.rename(destination.parent)
        recovered=JobStore(case.store.root).materialize(job,destination,case.verify)
        self.assertEqual(recovered['successful'],[5])
        self.assertEqual(recovered['reused'],[])

    def test_process_exit_after_delivery_rename_reconciles_only_exact_bytes_and_inode(self):
        import shutil
        case=self.case;job=case.submit()
        for _ in range(2):
            lease=case.store.claim(job);output,result=case.output(lease)
            case.store.publish(lease,output,result,case.verify)
        destination=case.root/'delivery'
        code="""import os,sys
from pathlib import Path
from render3d_jobs import JobStore
from test_render3d_jobs import JobTests
store=JobStore(sys.argv[1])
if os.name=='nt':
    store._finish_delivery=lambda *args:os._exit(77)
else:
    import render3d_jobs
    rename=render3d_jobs._rename_no_replace
    def rename_then_exit(*args,**kwargs):
        rename(*args,**kwargs)
        os._exit(77)
    render3d_jobs._rename_no_replace=rename_then_exit
store.materialize(sys.argv[2],Path(sys.argv[3]),JobTests().verify)
"""
        run=subprocess.run([sys.executable,'-c',code,str(case.store.root),job,str(destination)],
                           env={**os.environ,'PYTHONPATH':os.pathsep.join(map(str,(ROOT/'scripts',ROOT/'tests')))},
                           capture_output=True,text=True,timeout=30)
        self.assertEqual(run.returncode,77,run.stderr)
        staged_result=(destination/'result.json').read_bytes()
        store=JobStore(case.store.root)
        self.assertFalse(any(e['event']=='delivery-published' for e in store.status(job)['events']))
        original=destination.with_name('original')
        destination.rename(original);shutil.copytree(original,destination)
        with self.assertRaisesRegex(Render3DError,'directory differs'):store.materialize(job,destination,case.verify)
        destination.rename(destination.with_name('copy'));original.rename(destination)
        image=destination/'frames/000005.png';data=image.read_bytes()
        image.write_bytes(b'0'*len(data))
        with self.assertRaisesRegex(Render3DError,'artifact differs'):store.materialize(job,destination,case.verify)
        image.write_bytes(data)
        unexpected=destination/'unexpected'
        unexpected.mkdir()
        with self.assertRaisesRegex(Render3DError,'inventory differs'):store.materialize(job,destination,case.verify)
        unexpected.rename(case.root/'unlisted')
        recovered=store.materialize(job,destination,case.verify)
        self.assertEqual(recovered,json.loads(staged_result))
        events=[json.loads(e['detail']) for e in store.status(job)['events'] if e['event']=='delivery-published']
        import hashlib
        self.assertEqual(events,[dict(destination=str(destination),result_sha256=hashlib.sha256(staged_result).hexdigest())])
        with self.assertRaisesRegex(Render3DError,'already exists'):store.materialize(job,destination,case.verify)



@unittest.skipUnless(os.name=='nt','Native Windows synchronization handle query')
class NativeLivenessTests(unittest.TestCase):
    def test_windows_liveness_query_preserves_child_and_observes_exit(self):
        from render3d_jobs import _alive
        code='import sys; print("ready",flush=True); print(sys.stdin.readline().strip(),flush=True)'
        with subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True) as child:
            self.assertEqual(child.stdout.readline().strip(),'ready')
            with patch('render3d_jobs.os.kill',side_effect=AssertionError('observer sent a signal')):
                self.assertTrue(_alive(child.pid))
                stdout,_=child.communicate('observed\n',timeout=10)
                self.assertEqual(stdout.strip(),'observed')
                self.assertEqual(child.returncode,0)
                self.assertFalse(_alive(child.pid))


if __name__=='__main__':unittest.main()
