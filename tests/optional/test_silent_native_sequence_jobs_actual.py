"""Reuse actual retained animated mesh renders; never fabricate native output."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import silent_native_sequence_jobs as native

class ActualNativeSequenceJobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('HATCH_NATIVE_SEQUENCE_JOBS_TEST')!='1':raise RuntimeError('Explicit retained-native job gate requires HATCH_NATIVE_SEQUENCE_JOBS_TEST=1')
        cls.fixture=Path(os.environ['HATCH_NATIVE_SEQUENCE_FIXTURE'])
        cls.evidence=json.loads((cls.fixture/'evidence.json').read_text())
        if cls.evidence.get('geometry_vertices',0)<=0 or cls.evidence.get('max_world_vertex_displacement',0)<=0:
            raise RuntimeError('Fixture must retain actual animated geometry proof')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.render=self.root/'render';shutil.copytree(self.fixture/'render',self.render)
        self.request=json.loads((self.fixture/'request.json').read_text());self.result=json.loads((self.render/'result.json').read_text())
        self.job=self.root/'job';self.delivery=self.root/'delivery'

    def create(self):
        return native.create_native(self.request,self.result,self.render,self.job,self.delivery,
            snapshot_root=self.render/'snapshot',expected_backend=self.evidence['backend'])

    def test_native_geometry_closure_and_actual_frame_bytes_in_outer_delivery(self):
        self.create();result=native.run(self.job)
        self.assertEqual(result['state']['phase'],'completed',result)
        digest,report=native._delivery(result['plan'],result['state'])
        self.assertEqual(report['source']['origin'],'blender3d')
        self.assertEqual(len(report['decoded']),3)
        for frame in report['source']['frames']:
            self.assertEqual((self.delivery/'sequence'/frame['path']).read_bytes(),(self.render/frame['source_path']).read_bytes())
        self.assertEqual(digest,hashlib.sha256((self.delivery/'job.json').read_bytes()).hexdigest())
        self.assertEqual(native.status(self.job)['state']['phase'],'completed')

    def test_cancel_and_retry_preserve_verified_native_cache_bytes(self):
        self.create()
        def stop(state):
            if state['progress']['checkpoint']==3:native.cancel(self.job)
        result=native.run(self.job,progress=stop)
        self.assertEqual(result['state']['phase'],'cancelled',result);self.assertFalse(self.delivery.exists())
        cached=self.job/'.native-sequence-cache/frames/000000.png'
        before=(cached.stat().st_ino,cached.stat().st_mtime_ns,cached.read_bytes())
        result=native.run(self.job,retry=True)
        self.assertEqual(result['state']['phase'],'completed',result)
        self.assertEqual((cached.stat().st_ino,cached.stat().st_mtime_ns,cached.read_bytes()),before)

    def test_renamed_job_directory_keeps_owned_fd_and_preserves_foreign_replacement(self):
        self.create();held=self.root/'held';renamed=[False]
        def move(state):
            if not renamed[0]:
                self.job.rename(held);self.job.mkdir();(self.job/'foreign.txt').write_text('keep');renamed[0]=True
        result=native.run(self.job,progress=move)
        self.assertEqual(result['state']['phase'],'completed',result)
        self.assertEqual((self.job/'foreign.txt').read_text(),'keep')
        self.assertEqual(list(self.job.iterdir()),[self.job/'foreign.txt'])
        self.assertTrue((held/'native-sequence/sequence.json').is_file())
        native._delivery(result['plan'],result['state'])

    def test_publication_wrapper_substitution_preserves_foreign_and_verified_output(self):
        self.create();original=native._rename_no_replace;foreign=[]
        def replace(source,destination,**options):
            if destination==self.delivery.name:
                wrapper=Path(os.readlink('/proc/self/fd/'+str(options['source_dir_fd'])))
                wrapper.rename(wrapper.with_name(wrapper.name+'-held'));wrapper.mkdir();(wrapper/'foreign.txt').write_text('keep');foreign.append(wrapper)
            return original(source,destination,**options)
        with mock.patch.object(native,'_rename_no_replace',side_effect=replace):result=native.run(self.job)
        self.assertEqual(result['state']['phase'],'completed',result)
        self.assertEqual((foreign[0]/'foreign.txt').read_text(),'keep')
        self.assertEqual(set(p.name for p in self.delivery.iterdir()),{'sequence','job.json'})
        native._delivery(result['plan'],result['state'])

    def test_native_snapshot_tampering_invalidates_execution_before_publication(self):
        self.create();snapshot=self.render/'snapshot/snapshot.json';snapshot.write_bytes(snapshot.read_bytes()+b' ')
        result=native.run(self.job)
        self.assertEqual(result['state']['phase'],'failed',result)
        self.assertFalse(self.delivery.exists())

    def test_actual_publication_recovers_after_lost_checkpoint_ack(self):
        self.create();save=native.controls._save
        def interrupted(fd,state,phase,**changes):
            if phase=='completed':raise KeyboardInterrupt('injected after actual immutable publication')
            return save(fd,state,phase,**changes)
        with mock.patch.object(native.controls,'_save',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):native.run(self.job)
        before=(self.delivery/'job.json').read_bytes()
        self.assertEqual(native.status(self.job)['state']['phase'],'interrupted')
        result=native.run(self.job,resume=True)
        self.assertEqual(result['state']['phase'],'completed',result)
        self.assertEqual((self.delivery/'job.json').read_bytes(),before)

    def test_status_reads_checkpoint_after_lock_acquisition(self):
        import fcntl
        self.create();complete=native.run(self.job)['state'];original=fcntl.flock
        with native.pinned_directory(self.job) as fd:
            native.jobs._put_json(fd,'state.json',{**complete,'phase':'preparing'},replace=True)
            def complete_at_acquisition(lock,operation):
                original(lock,operation)
                native.jobs._put_json(fd,'state.json',complete,replace=True)
            with mock.patch.object(fcntl,'flock',side_effect=complete_at_acquisition):
                result=native.status(self.job)
            self.assertEqual(result['state']['phase'],'completed')
            self.assertEqual(native.jobs._json(fd,'state.json')['phase'],'completed')

if __name__=='__main__':unittest.main()
