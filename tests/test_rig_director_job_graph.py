"""Durable graph bindings over actual native SQLite job state; no mock scheduler."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from director_job_graph import RenderGraphBindings
from render3d_jobs import JobStore
from render3d_contracts import Render3DError
from render3d_snapshot import Scene3DAdapter, create_snapshot
from rig_errors import RigError, object_hash
from scene3d_example import build_example
from test_render3d_contracts import request,backend,file


def graph_for_job(store,job,artifact_path='snapshot/snapshot.json'):
    state = store.status(job)
    ref = dict(artifact_id='render-snapshot',kind='render3d-snapshot',schema_version=1,
        semantic_version='1.0.0',path=artifact_path,sha256=state['request']['snapshot']['sha256'],
        byte_size=state['request']['snapshot']['bytes'],media_type='application/json',
        provenance_ref=None,fixture=True,dependencies=[])
    graph = dict(nodes=[dict(id='render',operation='RenderAdapter.submit',inputs=[ref],output_schema='render3d-result',
        resource_class='render',cache_key=object_hash(state['request']),gates=['cycles-cpu'],native_owner='render3d')],
        edges=[],critical_path_seconds=None)
    binding = dict(render=dict(job=job,request_sha256=object_hash(state['request']),
        backend_sha256=object_hash(state['backend']),snapshot_sha256=state['request']['snapshot']['sha256']))
    return graph,binding


class GraphBindingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        build_example(self.root/'source')
        create_snapshot(self.root/'source',self.root/'snapshot',Scene3DAdapter())
        self.store=JobStore(self.root/'jobs')
        req=request();req['scene']['id']='example-scene';req['shot']['id']='example-shot'
        req['snapshot']=file(payload=(self.root/'snapshot/snapshot.json').read_bytes())
        self.job=self.store.submit(req,self.root/'snapshot',backend())
        self.graph,self.binding=graph_for_job(self.store,self.job)
        self.adapter=RenderGraphBindings(self.store)

    def test_reopened_graph_derives_state_and_native_worker_lease(self):
        registered=self.adapter.register('mvp',self.graph,self.binding,self.root)
        self.assertEqual(registered['revision'],1)
        lease=self.store.claim(self.job)
        reopened=RenderGraphBindings(JobStore(self.root/'jobs')).status('mvp')
        self.assertEqual(reopened['state'],'running')
        self.assertEqual(reopened['nodes']['render']['frames'][0]['token'],lease['token'])
        self.assertIsInstance(reopened['nodes']['render']['frames'][0]['pid'],int)
        self.store.cancel(self.job)
        self.store.fail(lease,RuntimeError('cancelled'))
        cancelled=self.adapter.status('mvp')
        self.assertEqual(cancelled['state'],'cancelled')
        receipt=self.adapter.resume_node('mvp','render',None,graph_revision=1,
            graph_sha256=registered['graph_sha256'],job_revision=cancelled['nodes']['render']['revision'])
        self.assertEqual(receipt['job'],self.job)
        self.assertEqual(self.adapter.status('mvp')['state'],'queued')

    def test_stale_graph_hash_revision_and_wrong_native_binding_preserve_job(self):
        registered=self.adapter.register('mvp',self.graph,self.binding,self.root)
        before=self.store.status(self.job)
        for kwargs in (dict(expected_revision=0),dict(expected_revision=2)):
            with self.assertRaises(RigError):self.adapter.register('mvp',self.graph,self.binding,self.root,**kwargs)
        wrong=deepcopy(self.binding);wrong['render']['request_sha256']='0'*64
        with self.assertRaises(RigError):self.adapter.register('wrong',self.graph,wrong,self.root)
        with self.assertRaises(RigError):self.adapter.resume_node('mvp','render',None,graph_revision=1,graph_sha256='0'*64,job_revision=before['revision'])
        self.assertEqual(self.store.status(self.job),before)
        self.assertEqual(registered['graph'],self.adapter.status('mvp')['graph'])

    def test_tampered_inputs_reject_resume_without_native_state_change(self):
        registered=self.adapter.register('mvp',self.graph,self.binding,self.root)
        before=self.store.status(self.job)
        (self.root/'snapshot/snapshot.json').write_bytes(b'corrupted snapshot')
        with self.assertRaises(RigError):self.adapter.resume_node('mvp','render',None,graph_revision=1,
            graph_sha256=registered['graph_sha256'],job_revision=before['revision'])
        self.assertEqual(self.store.status(self.job),before)

    def test_event_view_uses_native_bounded_history(self):
        self.adapter.register('mvp',self.graph,self.binding,self.root)
        with self.store._transaction() as db:
            for index in range(1005):self.store._event(db,self.job,'history-regression',{'index':index})
        native=self.store.status(self.job)
        derived=self.adapter.status('mvp')['nodes']['render']
        self.assertEqual(derived['events'],native['events'])
        self.assertEqual(len(derived['events']),1000)

    def test_aggregate_input_budget_rejected_before_any_file_read(self):
        # A small cap exercises admission without allocating oversized payloads.
        from unittest.mock import patch
        import director_contracts as dc
        graph=deepcopy(self.graph)
        extra=deepcopy(graph['nodes'][0]['inputs'][0])
        extra.update(artifact_id='other',path='other.json')
        graph['nodes'][0]['inputs'].append(extra)
        with patch.object(dc,'MAX_TOTAL_BYTES',extra['byte_size']),patch.object(dc,'_read_artifact') as read:
            with self.assertRaises(RigError):self.adapter.register('too-large',graph,self.binding,self.root)
            read.assert_not_called()

    def test_tampered_native_payload_rejects_resume_before_native_state_change(self):
        registered=self.adapter.register('mvp',self.graph,self.binding,self.root)
        before=self.store.status(self.job)
        (self.root/'snapshot/triangle.obj').write_bytes(b'changed payload')
        with self.assertRaises(Render3DError):self.adapter.resume_node('mvp','render',None,graph_revision=1,
            graph_sha256=registered['graph_sha256'],job_revision=before['revision'])
        self.assertEqual(self.store.status(self.job),before)

    def test_case_colliding_graph_inputs_rejected_before_any_file_read(self):
        from unittest.mock import patch
        import director_contracts as dc
        graph=deepcopy(self.graph)
        alias=deepcopy(graph['nodes'][0]['inputs'][0])
        alias.update(artifact_id='case-alias',path='snapshot/SNAPSHOT.json')
        graph['nodes'][0]['inputs'].append(alias)
        with patch.object(dc,'_read_artifact') as read:
            with self.assertRaises(RigError):self.adapter.register('case-alias',graph,self.binding,self.root)
            read.assert_not_called()

    def test_graph_cannot_substitute_unrelated_valid_input_for_native_snapshot(self):
        graph=deepcopy(self.graph)
        original=graph['nodes'][0]['inputs'][0]
        payload=(self.root/'source/triangle.obj').read_bytes()
        from hashlib import sha256
        original.update(path='source/triangle.obj',sha256=sha256(payload).hexdigest(),byte_size=len(payload))
        with self.assertRaises(RigError):self.adapter.register('unrelated',graph,self.binding,self.root)
