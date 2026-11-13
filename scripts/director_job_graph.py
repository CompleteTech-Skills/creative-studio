"""Immutable Director render DAG bindings in the native JobStore SQLite database.

This adapter owns graph definitions and exact bindings only. Native jobs, leases,
workers, cancellation, cache verification and execution remain JobStore's work.
The limited MVP supports independent already-materialized render nodes; it does
not silently execute dataflow edges or claim to schedule arbitrary Director DAGs.
"""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path

from rig_errors import RigError, object_hash


def _require(condition, message):
    if not condition:
        raise RigError('DIRECTOR_JOB_GRAPH', message, stage='director-job-graph')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class RenderGraphBindings:
    def __init__(self, store):
        self.store = store
        # Additive tables share the native owner's transaction/durability policy.
        # No execution state is copied into these tables.
        with store._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS director_graphs(
                id TEXT NOT NULL, revision INTEGER NOT NULL, definition TEXT NOT NULL,
                graph_hash TEXT NOT NULL, bindings TEXT NOT NULL, input_root TEXT NOT NULL,
                PRIMARY KEY(id,revision))''')

    def _validate(self, graph, bindings, root):
        from jsonschema import Draft202012Validator
        import director_contracts as dc
        schema = dc.schemas()['director.schema.json']
        Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/output-execution-graph'}).validate(graph)
        _require(not graph['edges'], 'MVP graph bindings support independent render nodes; materialize dependencies before binding.')
        nodes = {node['id']:node for node in graph['nodes']}
        _require(len(nodes) == len(graph['nodes']) and isinstance(bindings,dict) and set(bindings)==set(nodes),
                 'Bind every unique render node exactly once.')
        jobs = []
        for identifier,node in nodes.items():
            _require(node['operation']=='RenderAdapter.submit', 'Only native render nodes are supported by this MVP adapter.')
            binding = bindings[identifier]
            _require(isinstance(binding,dict) and set(binding)=={'job','request_sha256','backend_sha256','snapshot_sha256'},
                     'Expected exact native job/request/backend/snapshot binding.')
            _require(all(isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)
                         for value in binding.values()), 'Binding hashes must be canonical SHA-256 values.')
            jobs.append(binding['job'])
            _require(node['inputs'], 'Render node must retain exact native inputs.')
            for artifact in dc.artifact_references(node['inputs']):
                dc._read_artifact(root,artifact)
        _require(len(jobs)==len(set(jobs)), 'A native job cannot implement multiple graph nodes.')

    def _native(self, db, bindings, graph):
        records = {}
        for identifier,binding in bindings.items():
            row = db.execute('SELECT * FROM jobs WHERE id=?',(binding['job'],)).fetchone()
            _require(row is not None, 'Bound native job is absent.')
            request,backend = json.loads(row['request']),json.loads(row['backend'])
            inputs = next(node['inputs'] for node in graph['nodes'] if node['id']==identifier)
            _require(any(item['sha256']==request['snapshot']['sha256'] and item['byte_size']==request['snapshot']['bytes'] for item in inputs),
                     'Graph inputs must include the exact native snapshot metadata.')
            _require(object_hash(request)==binding['request_sha256'] and object_hash(backend)==binding['backend_sha256'] and
                     request['snapshot']['sha256']==binding['snapshot_sha256'], 'Persisted native binding differs from the graph.')
            # Verify pinned metadata bytes before graph status or resume authorizes reuse.
            from render3d_contracts import make_frame_spec
            make_frame_spec(request,request['frame_range']['start'],Path(row['snapshot'])/'snapshot.json')
            frames = [dict(frame) for frame in db.execute('SELECT frame,state,attempts,pid,token,heartbeat,error FROM frames WHERE job=? ORDER BY frame',(binding['job'],))]
            events = [dict(event) for event in db.execute('SELECT seq,at,event,detail FROM events WHERE job=? ORDER BY seq',(binding['job'],))]
            records[identifier] = dict(job=row['id'],revision=row['revision'],state=row['state'],frames=frames,events=events)
        return records

    def register(self, identifier, graph, bindings, input_root, *, expected_revision=0):
        from rig_errors import portable_name
        portable_name(identifier)
        _require(type(expected_revision) is int and expected_revision>=0, 'Expected a nonnegative graph revision.')
        root = str(Path(input_root).absolute())
        _require(not Path(root).is_symlink(), 'Graph input root must not be linked.')
        self._validate(graph,bindings,root)
        with self.store._transaction() as db:
            row = db.execute('SELECT MAX(revision) AS revision FROM director_graphs WHERE id=?',(identifier,)).fetchone()
            revision = row['revision'] or 0
            _require(revision==expected_revision, 'Graph revision is stale.')
            self._native(db,bindings,graph)
            db.execute('INSERT INTO director_graphs VALUES(?,?,?,?,?,?)',
                       (identifier,revision+1,_json(graph),object_hash(graph),_json(bindings),root))
        return self.status(identifier,revision=revision+1)

    def status(self, identifier, *, revision=None, expected_sha256=None):
        with self.store._transaction() as db:
            if revision is None:
                row = db.execute('SELECT * FROM director_graphs WHERE id=? ORDER BY revision DESC LIMIT 1',(identifier,)).fetchone()
            else:
                _require(type(revision) is int and revision>0, 'Expected a positive graph revision.')
                row = db.execute('SELECT * FROM director_graphs WHERE id=? AND revision=?',(identifier,revision)).fetchone()
            _require(row is not None, 'Unknown graph revision.')
            graph,bindings = json.loads(row['definition']),json.loads(row['bindings'])
            _require(object_hash(graph)==row['graph_hash'] and
                     (expected_sha256 is None or expected_sha256==row['graph_hash']), 'Graph hash differs from the pinned revision.')
            self._validate(graph,bindings,row['input_root'])
            nodes = self._native(db,bindings,graph)
            states = [node['state'] for node in nodes.values()]
            # This is a derived view, never a second scheduler or stored state.
            state = 'complete' if all(value=='complete' for value in states) else 'running' if 'running' in states else 'failed' if 'failed' in states else 'cancelled' if 'cancelled' in states else 'queued'
            return dict(id=identifier,revision=row['revision'],graph_sha256=row['graph_hash'],
                        graph=graph,bindings=bindings,state=state,nodes=nodes)

    def resume_node(self, identifier, node, verifier, *, graph_revision, graph_sha256, job_revision):
        pinned = self.status(identifier,revision=graph_revision,expected_sha256=graph_sha256)
        _require(node in pinned['bindings'], 'Unknown graph node.')
        from director_reliability import resume_verified_job
        receipt = resume_verified_job(self.store,pinned['bindings'][node]['job'],verifier,expected_revision=job_revision)
        receipt['graph'] = dict(id=identifier,revision=graph_revision,sha256=graph_sha256,node=node)
        return receipt
