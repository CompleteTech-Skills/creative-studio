"""Durable graph bindings over actual native SQLite job state; no mock scheduler."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from director_job_graph import RenderGraphBindings
from render3d_jobs import JobStore
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

    def test_graph_cannot_substitute_unrelated_valid_input_for_native_snapshot(self):
        graph=deepcopy(self.graph)
        original=graph['nodes'][0]['inputs'][0]
        payload=(self.root/'source/triangle.obj').read_bytes()
        from hashlib import sha256
        original.update(path='source/triangle.obj',sha256=sha256(payload).hexdigest(),byte_size=len(payload))
        with self.assertRaises(RigError):self.adapter.register('unrelated',graph,self.binding,self.root)
