"""Canonical Director envelopes over the native-backed ExecutionService.

The host supplies its existing ProjectService context, native binding resolver
and optional immutable artifact recorder. This adapter creates no claim,
authorization database, renderer, artifact registry or publication mechanism.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import director_contracts as dc
from director_graph import _verify_refs
from rig_errors import RigError, decode_json, object_hash


def _need(condition, code, message):
    if not condition:
        raise RigError('DIRECTOR_EXECUTION_' + code, message, stage='execution-facade')


def _usage():
    return dict(classification='unavailable',
        **{name: dict(value=None, explanation='Envelope adaptation does not measure native resource usage.')
           for name in ('provider_calls', 'input_tokens', 'output_tokens', 'cpu_seconds', 'gpu_seconds',
                        'storage_bytes', 'amount')}, currency=None, measurement_source='director-execution-facade-v1')


def _diagnostic(code, message, *, expected=None, observed=None):
    return dict(code=code, severity='error', stage='execution', entity_id=None, field_path='input',
                expected=expected, observed=observed, retryable=False,
                recommended_action=message[:4096], evidence=[])


class ImmutableExecutionRecorder:
    """Thin consumer of the shared atomic publisher; no parallel registry.

    A release contains exact copies of native outputs, native owner receipts and
    separately validated execution observations. Large frame sets use a pinned
    inventory ArtifactRef, preserving the canonical 256-reference envelope bound.
    All native decoding/geometry verification remains with the native drivers.
    """
    def __init__(self, core, *, publisher=None, max_bytes=1024**3, max_files=99000):
        if publisher is None:
            import atomic_publish as publisher
        _need(type(max_bytes) is int and 0 < max_bytes <= 16 * 1024**3 and
              type(max_files) is int and 0 < max_files <= 99000, 'CONFIG', 'Invalid recorder storage bounds.')
        self.core, self.publisher = core, publisher
        self.max_bytes, self.max_files = max_bytes, max_files

    @staticmethod
    def _reference(identifier, path, payload, *, kind, fixture):
        return dict(artifact_id=identifier, kind=kind, schema_version=1, semantic_version='1.0.0',
            path=path, sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload),
            media_type='application/json', provenance_ref=None, fixture=fixture, dependencies=[])

    @staticmethod
    def _json(value):
        payload = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
        _need(len(payload) <= dc.MAX_BYTES, 'LIMIT', 'Execution observation exceeds its JSON byte bound.')
        return payload

    def __call__(self, snapshot, root):
        root = Path(root).absolute()
        _need(root.is_dir() and root.resolve() == root, 'ROOT', 'Recorder requires an existing canonical owned output root.')
        with self.core.store._transaction() as db:
            run = self.core._read(db, snapshot['id'])
        _need(run['graph_hash'] == snapshot['graph_sha256'], 'RECORDER', 'Recording graph identity changed.')
        plan = {node['id']: node for node in run['graph']['nodes']}
        _need(set(plan) == set(snapshot['nodes']), 'RECORDER', 'Recording has an incomplete node observation set.')
        release = 'execution-' + object_hash(dict(snapshot=snapshot, observation_schema=2))
        recipes = {}
        published = {}
        all_outputs = []
        total = 0
        for node_id, node in snapshot['nodes'].items():
            native = node['native']
            for source in native.get('artifacts', []):
                key = object_hash({name: value for name, value in source.items() if name != 'path'})
                if key not in published:
                    suffix = Path(source['path']).suffix
                    suffix = suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,10}', suffix) else '.bin'
                    name = 'output-' + key + suffix
                    public = dict(deepcopy(source), path=release + '/' + name)
                    recipes[name] = dict(source=source, root=native['artifact_root'],
                                         sha256=source['sha256'], byte_size=source['byte_size'])
                    published[key] = public
                    total += source['byte_size']
                all_outputs.append(published[key])
        _need(total <= self.max_bytes and len(recipes) <= self.max_files, 'LIMIT', 'Native outputs exceed recorder storage bounds.')
        metadata_bytes = 0

        def document(name, value, kind, fixture):
            nonlocal total, metadata_bytes
            payload = self._json(value)
            total += len(payload); metadata_bytes += len(payload)
            _need(total <= self.max_bytes and metadata_bytes <= 64 * 1024**2,
                  'LIMIT', 'Recorded metadata exceeds its bounded working/storage set.')
            reference = self._reference('evidence-' + hashlib.sha256(payload).hexdigest(), release + '/' + name,
                                        payload, kind=kind, fixture=fixture)
            recipes[name] = dict(payload=payload, sha256=reference['sha256'], byte_size=len(payload))
            return reference

        fixture = any(reference['fixture'] for node in plan.values() for reference in node['inputs'])
        if len(all_outputs) > 256:
            outputs = [document('outputs.json', dict(schema_version=1, kind='native-output-inventory',
                                artifacts=all_outputs), 'native-output-inventory', fixture)]
        else:
            outputs = all_outputs
        worker_refs, node_refs = [], []
        for index, (node_id, node) in enumerate(snapshot['nodes'].items()):
            native = node['native']
            worker = document(f'worker-{index:04d}.json', dict(node=node_id, native_receipt=native['receipt']),
                              'native-worker-receipt', fixture)
            worker_refs.append(worker)
            node_outputs = [published[object_hash({key: value for key, value in ref.items() if key != 'path'})]
                            for ref in native.get('artifacts', [])]
            if len(node_outputs) > 256:
                node_outputs = [document(f'outputs-{index:04d}.json', dict(schema_version=1,
                    kind='native-output-inventory', artifacts=node_outputs), 'native-output-inventory', fixture)]
            original = plan[node_id].get('execution_metadata', {})
            status = {'queued': 'planned', 'running': 'running', 'uncertain': 'awaiting_external_result',
                      'complete': 'validated', 'failed': 'failed', 'cancelled': 'canceled'}[native['state']]
            # A queued declaration is not promoted to a claimed/running job.
            native_job = None if status == 'planned' else worker
            metadata = dict(schema_version=2, estimated_usage=deepcopy(original.get('estimated_usage', _usage())),
                actual_usage=_usage(), retry_classification='unclassified',
                idempotency_key=original.get('idempotency_key', 'observation-' + object_hash([snapshot['id'], node_id])[:40]),
                execution_status=status, checkpoint_refs=[], output_artifacts=node_outputs,
                validation_results=[], provenance=[worker], failure_reason=None, native_job_ref=native_job)
            measured = native.get('actual_usage')
            if isinstance(measured, dict) and any(value is not None for value in measured.values()):
                # A currency-bearing amount needs the native owner's explicit
                # currency. Without that information monetary usage stays unknown.
                actual = _usage(); actual['classification'] = 'actual'
                for key in ('provider_calls', 'input_tokens', 'output_tokens', 'cpu_seconds', 'gpu_seconds', 'storage_bytes'):
                    if key in measured and measured[key] is not None:
                        actual[key] = dict(value=measured[key], explanation=None)
                actual['measurement_source'] = 'authoritative-native-owner'
                metadata['actual_usage'] = actual
            if status == 'validated':
                _need(node_outputs, 'RECORDER', 'A validated node needs actual native output artifacts.')
                metadata['validation_results'] = [dict(check='native-owner-output-verification', status='passed',
                    evidence=[worker] + node_outputs, diagnostics=[])]
            elif status == 'failed':
                metadata['failure_reason'] = node.get('error') or 'The authoritative native owner reports failure; detailed reason is unavailable.'
            dc.validate_execution_metadata(metadata, planned=False)
            node_refs.append(document(f'node-{index:04d}.json', dict(node=node_id,
                graph_sha256=snapshot['graph_sha256'], execution_metadata=metadata), 'native-execution-observation', fixture))
        if len(worker_refs) > 256:
            worker_refs = [document('workers.json', dict(schema_version=1, kind='native-worker-inventory',
                receipts=[dict(node=node_id, native_receipt=node['native']['receipt'])
                          for node_id, node in snapshot['nodes'].items()]), 'native-worker-inventory', fixture)]
        if len(node_refs) > 256:
            node_refs = [document('nodes.json', dict(schema_version=1, kind='native-observation-inventory',
                observations=node_refs), 'native-observation-inventory', fixture)]
        _need(len(recipes) <= self.max_files, 'LIMIT', 'Recorded files exceed the publication/cleanup bound.')
        self._publish(root, release, recipes)
        return dict(nodes=node_refs, native_workers=worker_refs, claims=[], artifacts=outputs)

    def _payload(self, recipe):
        return recipe['payload'] if 'payload' in recipe else dc._read_artifact(recipe['root'], recipe['source'])

    @staticmethod
    def _verify_fd(directory, recipes):
        # A capability descriptor may carry a shared directory-stream offset.
        # Open a separate stream rather than consuming/relying on that offset.
        stream = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            names = set(os.listdir(stream))
        finally:
            os.close(stream)
        expected = set(recipes)
        _need(names == expected, 'RECORDER', 'Published inventory differs: missing=' + repr(sorted(expected - names)[:8])
              + ', unexpected=' + repr(sorted(names - expected)[:8]))
        for name, recipe in recipes.items():
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == recipe['byte_size'],
                      'RECORDER', 'Recorded file type/size/ownership differs from its verified bytes.')
                digest = hashlib.sha256(); count = 0
                while True:
                    chunk = os.read(descriptor, min(1024**2, recipe['byte_size'] - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    _need(count <= recipe['byte_size'], 'RECORDER', 'Recorded file grew during verification.')
                    digest.update(chunk)
                _need(count == recipe['byte_size'] and digest.hexdigest() == recipe['sha256'],
                      'RECORDER', 'Recorded bytes differ from the actual native evidence.')
            finally:
                os.close(descriptor)

    def _publish(self, root, release, recipes):
        publisher = self.publisher
        if os.name == 'posix':
            _need(callable(getattr(publisher, 'private_stage_fd_supported', None)) and publisher.private_stage_fd_supported(),
                  'UNAVAILABLE', 'Canonical descriptor-relative publication is unavailable; no pathname fallback is used.')
            with publisher.pinned_directory_fd(root) as parent:
                with publisher.private_stage_fd(parent, prefix='.execution-', cleanup_on_error=False) as stage:
                    os.mkdir('payload', mode=0o700, dir_fd=stage)
                    payload = os.open('payload', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage)
                    try:
                        for name, recipe in recipes.items():
                            data = self._payload(recipe)
                            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=payload)
                            try:
                                view = memoryview(data)
                                while view:
                                    written = os.write(descriptor, view)
                                    _need(written > 0, 'RECORDER', 'Short write while recording native evidence.')
                                    view = view[written:]
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                        self._verify_fd(payload, recipes)
                        os.fsync(payload)
                        try:
                            publisher.rename_no_replace_at(stage, 'payload', parent, release)
                        except FileExistsError:
                            # Idempotency reuses only an independently verified
                            # identical immutable release, never a foreign path.
                            pass
                        destination = os.open(release, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            self._verify_fd(destination, recipes)
                        finally:
                            os.close(destination)
                        os.fsync(parent)
                    finally:
                        os.close(payload)
        elif os.name == 'nt':
            _need(callable(getattr(publisher, 'windows_private_stage_supported', None)) and publisher.windows_private_stage_supported(),
                  'UNAVAILABLE', 'Canonical native Windows publication is unavailable.')
            with publisher.windows_private_stage(root, prefix='.execution-', cleanup_on_error=False) as stage:
                for name, recipe in recipes.items():
                    with (stage.path / name).open('xb') as stream:
                        stream.write(self._payload(recipe)); stream.flush(); os.fsync(stream.fileno())
                self._verify_windows(stage.path, recipes)
                try:
                    stage.publish_to(root / release)
                except FileExistsError:
                    pass
                self._verify_windows(root / release, recipes)
        else:
            _need(False, 'UNAVAILABLE', 'Canonical immutable publication is unsupported on this platform.')

    @staticmethod
    def _verify_windows(root, recipes):
        from asset_library import read_source_bytes
        _need({path.name for path in root.iterdir()} == set(recipes), 'RECORDER', 'Published inventory differs from verified native files.')
        for name, recipe in recipes.items():
            data = read_source_bytes(root / name, recipe['byte_size'])
            _need(len(data) == recipe['byte_size'] and hashlib.sha256(data).hexdigest() == recipe['sha256'],
                  'RECORDER', 'Published native bytes differ from their exact reference.')


class ExecutionServiceFacade:
    """Adapt four strict methods without changing execution ownership.

    ``project_context()`` must read the host's authorized native project/version;
    never construct it by echoing the incoming request. The cancellation token
    and original submit request ID are also bound durably by the core service.

    ``artifact_recorder(snapshot, output_root)`` delegates to an existing owned
    immutable publication authority and returns exactly four ArtifactRef lists:
    nodes, native_workers, claims and artifacts. Native worker receipt files
    contain ``{"node": node_id, "native_receipt": actual_driver_receipt}``.
    Completed output copies must preserve every actual native artifact identity
    and byte hash. Without this recorder a native completed job is reported as
    blocked evidence export, never as a canonical completed delivery.
    """

    def __init__(self, core, *, binding_resolver, capabilities, input_root,
                 project_context, authorized_policy=None, artifact_recorder=None,
                 artifact_root=None, clock=time.time):
        _need(callable(binding_resolver) and callable(project_context), 'CONFIG',
              'Trusted native binding and ProjectService context resolvers are required.')
        _need(artifact_recorder is None or callable(artifact_recorder), 'CONFIG', 'Invalid native artifact recorder.')
        self.core, self.binding_resolver, self.project_context = core, binding_resolver, project_context
        self.capabilities, self.clock = deepcopy(capabilities), clock
        self.input_root = Path(input_root).absolute()
        self.artifact_root = Path(artifact_root if artifact_root is not None else input_root).absolute()
        self.artifact_recorder = artifact_recorder
        self.authorized_policy = deepcopy(authorized_policy or dict(network='deny', providers=[], paid='deny', policy_ref=None))

    def _root(self, root):
        actual = Path(root).absolute()
        _need(actual == self.input_root and actual.is_dir() and actual.resolve() == actual,
              'ROOT', 'The request must use the configured canonical native project root.')
        return actual

    def _context(self, request, root):
        self._root(root)
        context = request['context']
        trusted = self.project_context()
        _need(isinstance(trusted, dict) and set(trusted) == {'project_id', 'project_revision_sha256'},
              'CONFIG', 'ProjectService must return its exact authorized project revision.')
        _need(all(context[name] == trusted[name] for name in trusted), 'OWNERSHIP',
              'Request project identity or revision differs from the authorized native project.')
        _need(context['policy'] == self.authorized_policy, 'POLICY', 'Request policy differs from explicit host authorization.')
        _need(context['capability_snapshot_sha256'] == self.capabilities.get('digest'), 'CAPABILITY',
              'Request pins a different capability snapshot.')
        deadline = datetime.fromisoformat(context['deadline'].replace('Z', '+00:00').replace('z', '+00:00'))
        _need(deadline.tzinfo is not None and self.clock() < deadline.timestamp(), 'DEADLINE',
              'Method deadline expired. A previous native effect may still exist; inspect its original job.')
        _verify_refs(root, list(dc.artifact_references(request)))
        return context

    def _job_context(self, request):
        binding = self.core.get_context_binding(request['input']['job_id'])
        _need(bool(binding), 'OWNERSHIP', 'This job has no canonical project ownership binding.')
        context = request['context']
        _need(all(binding[name] == context[name] for name in
                  ('project_id', 'project_revision_sha256', 'cancellation_token')), 'OWNERSHIP',
              'Project, revision or cancellation ownership differs from the original job.')
        _need(binding['request_id'] == request['input']['request_id'], 'OWNERSHIP',
              'Job operation identity differs from its original submit request.')

    @staticmethod
    def _graph(payload):
        graph = decode_json(payload)
        if isinstance(graph, dict) and graph.get('kind') == 'director-result':
            dc.validate_method_result('GraphCompiler.compile', graph)
            _need(graph['status'] == 'succeeded' and graph['value'] is not None, 'GRAPH', 'Graph compilation did not succeed.')
            graph = graph['value']
        _need(isinstance(graph, dict), 'GRAPH', 'Expected an exact compiled execution graph.')
        return graph

    def _recorded(self, snapshot):
        if self.artifact_recorder is None:
            return dict(nodes=[], native_workers=[], claims=[], artifacts=[])
        _need(self.artifact_root.is_dir() and self.artifact_root.resolve() == self.artifact_root, 'ROOT',
              'Artifact recording requires its configured canonical output root.')
        result = self.artifact_recorder(deepcopy(snapshot), self.artifact_root)
        _need(isinstance(result, dict) and set(result) == {'nodes', 'native_workers', 'claims', 'artifacts'} and
              all(isinstance(value, list) and len(value) <= 256 for value in result.values()),
              'RECORDER', 'Native recorder must return four bounded exact ArtifactRef lists.')
        # The canonical schema validates each complete ArtifactRef before reads.
        from jsonschema import Draft202012Validator
        definitions = dc.schemas()['director.schema.json']['$defs']
        validator = Draft202012Validator({'$defs': definitions, '$ref': '#/$defs/artifact-ref'})
        for values in result.values():
            for reference in values:
                _need(validator.is_valid(reference), 'RECORDER', 'Recorder returned a malformed native ArtifactRef.')
        _verify_refs(self.artifact_root, [ref for values in result.values() for ref in values])
        def read_document(reference):
            return decode_json(dc._read_artifact(self.artifact_root, reference))

        def output_members(references):
            members = []
            for reference in references:
                if reference['kind'] == 'native-output-inventory':
                    document = read_document(reference)
                    _need(isinstance(document, dict) and set(document) == {'schema_version', 'kind', 'artifacts'}
                          and document['schema_version'] == 1 and document['kind'] == 'native-output-inventory'
                          and isinstance(document['artifacts'], list) and len(document['artifacts']) <= 99000,
                          'RECORDER', 'Invalid native output inventory.')
                    for member in document['artifacts']:
                        _need(validator.is_valid(member) and member['kind'] != 'native-output-inventory',
                              'RECORDER', 'Native inventory contains an invalid or recursive member.')
                        # Bound peak read memory to one actual output at a time.
                        dc._read_artifact(self.artifact_root, member)
                        members.append(member)
                else:
                    members.append(reference)
            return members

        receipt_nodes = set()
        for reference in result['native_workers']:
            document = read_document(reference)
            if reference['kind'] == 'native-worker-inventory':
                _need(isinstance(document, dict) and set(document) == {'schema_version', 'kind', 'receipts'}
                      and document['schema_version'] == 1 and document['kind'] == 'native-worker-inventory'
                      and isinstance(document['receipts'], list) and len(document['receipts']) <= 1024,
                      'RECORDER', 'Invalid native worker receipt inventory.')
                receipts = document['receipts']
            else:
                receipts = [document]
            for document in receipts:
                _need(isinstance(document, dict) and set(document) == {'node', 'native_receipt'},
                      'RECORDER', 'Native worker evidence needs its exact node and native receipt.')
                node = document['node']
                _need(node in snapshot['nodes'] and node not in receipt_nodes and
                      document['native_receipt'] == snapshot['nodes'][node]['native']['receipt'],
                      'RECORDER', 'Native worker evidence differs from the actual owner observation.')
                receipt_nodes.add(node)
        expected = [reference for node in snapshot['nodes'].values() for reference in node['native'].get('artifacts', [])]
        # Publication may namespace a copied path; immutable identity, source
        # status and exact bytes must remain unchanged.
        identity = lambda reference: object_hash({key: value for key, value in reference.items() if key != 'path'})
        _need(sorted(map(identity, output_members(result['artifacts']))) == sorted(map(identity, expected)), 'RECORDER',
              'Recorded outputs differ from the actual verified native artifacts.')
        node_documents = []
        for reference in result['nodes']:
            document = read_document(reference)
            if reference['kind'] == 'native-observation-inventory':
                _need(isinstance(document, dict) and set(document) == {'schema_version', 'kind', 'observations'}
                      and document['schema_version'] == 1 and document['kind'] == 'native-observation-inventory'
                      and isinstance(document['observations'], list) and len(document['observations']) <= 1024,
                      'RECORDER', 'Invalid native runtime observation inventory.')
                for member in document['observations']:
                    _need(validator.is_valid(member) and member['kind'] == 'native-execution-observation',
                          'RECORDER', 'Invalid runtime observation reference.')
                    node_documents.append(read_document(member))
            else:
                node_documents.append(document)
        observed_nodes = set()
        for document in node_documents:
            _need(isinstance(document, dict) and set(document) == {'node', 'graph_sha256', 'execution_metadata'}
                  and document['node'] in snapshot['nodes'] and document['node'] not in observed_nodes
                  and document['graph_sha256'] == snapshot['graph_sha256'], 'RECORDER', 'Runtime observation identity differs.')
            node_id = document['node']; observed_nodes.add(node_id)
            native = snapshot['nodes'][node_id]['native']
            metadata = dc.validate_execution_metadata(document['execution_metadata'], planned=False)
            expected_status = {'queued': 'planned', 'running': 'running', 'uncertain': 'awaiting_external_result',
                'complete': 'validated', 'failed': 'failed', 'cancelled': 'canceled'}[native['state']]
            _need(metadata['execution_status'] == expected_status, 'RECORDER', 'Runtime metadata promotes an unobserved native state.')
            if native.get('actual_usage') is None:
                _need(metadata['actual_usage']['classification'] == 'unavailable', 'RECORDER', 'Recorder invented native resource usage.')
            _need(sorted(map(identity, output_members(metadata['output_artifacts']))) ==
                  sorted(map(identity, native.get('artifacts', []))), 'RECORDER', 'Runtime node outputs differ from native verification.')
            if metadata['native_job_ref'] is not None:
                worker = read_document(metadata['native_job_ref'])
                _need(worker == dict(node=node_id, native_receipt=native['receipt']), 'RECORDER', 'Runtime job reference differs from its native owner.')
        if snapshot['state'] == 'complete':
            _need(receipt_nodes == set(snapshot['nodes']) and observed_nodes == set(snapshot['nodes']) and bool(result['artifacts']), 'RECORDER',
                  'Every completed node needs its native worker evidence and actual output artifacts.')
        if result['claims']:
            # A status label or an arbitrary metadata file is not an ownership
            # token. Validate claim documents against an actual native receipt.
            for reference in result['claims']:
                document = decode_json(dc._read_artifact(self.artifact_root, reference))
                _need(isinstance(document, dict) and set(document) == {'node', 'native_receipt'} and
                      document['node'] in snapshot['nodes'], 'RECORDER', 'Invalid native claim evidence.')
                actual = snapshot['nodes'][document['node']]['native']['receipt']
                _need(document['native_receipt'] == actual and any(
                    isinstance(value, dict) and any(value.get(key) for key in ('token', 'claim_id', 'ownership_token'))
                    for value in dc._walk(actual)), 'RECORDER', 'Claim evidence has no actual native ownership token.')
        return deepcopy(result)

    def _value(self, snapshot, *, require_completion=True):
        recorded = self._recorded(snapshot)
        diagnostics = []
        state = {'queued': 'queued', 'running': 'running', 'complete': 'completed',
                 'failed': 'failed', 'cancelled': 'canceled', 'uncertain': 'blocked'}.get(snapshot['state'], 'blocked')
        if require_completion and state == 'completed' and (not recorded['native_workers'] or not recorded['artifacts']):
            state = 'blocked'
            diagnostics.append(_diagnostic('DIRECTOR_EXECUTION_RECORDER',
                'Native work completed, but its canonical immutable worker/output recorder is unavailable.'))
        if snapshot['state'] == 'uncertain':
            diagnostics.append(_diagnostic('DIRECTOR_EXECUTION_UNCERTAIN',
                'Reconcile the original native claim; uncertainty does not authorize another external call.'))
        completed = sum(node['native']['state'] == 'complete' for node in snapshot['nodes'].values())
        reservations = []
        # The core owns these exact persisted bindings. Read them using the
        # same authority, without creating another ledger or job registry.
        with self.core.store._transaction() as db:
            run = self.core._read(db, snapshot['id'])
            reservations = sorted({binding['reservation_id'] for binding in run['bindings'].values()})
        return dict(job_id=snapshot['id'], revision=snapshot['revision'], state=state,
                    nodes=recorded['nodes'], attempts=sum(node['attempt'] for node in snapshot['nodes'].values()),
                    native_workers=recorded['native_workers'], claims=recorded['claims'],
                    progress=completed / len(snapshot['nodes']) if snapshot['nodes'] else None,
                    reservations=reservations, diagnostics=diagnostics, artifacts=recorded['artifacts'])

    def _invoke(self, operation, request, root):
        method = 'ExecutionService.' + operation
        dc.validate_method_request(method, request)
        try:
            context = self._context(request, root)
            data = request['input']
            if operation == 'submit':
                graph = self._graph(dc._read_artifact(root, data['graph']))
                bindings = self.binding_resolver(deepcopy(graph), Path(root))
                identifier = 'execution-' + object_hash(dict(project=context['project_id'],
                    idempotency_key=context['idempotency_key']))[:40]
                self._context(request, root)  # Recheck the host revision/deadline after native resolution.
                context_binding = {name: context[name] for name in ('project_id', 'project_revision_sha256',
                    'cancellation_token', 'request_id', 'idempotency_key')}
                snapshot = self.core.submit_graph(identifier, graph, bindings, root,
                    capabilities=self.capabilities, context_binding=context_binding,
                    policy=dict(network_policy=context['policy']['network'], paid_policy=context['policy']['paid'],
                                allowed_providers=context['policy']['providers']))
            else:
                self._job_context(request)
                snapshot = self.core.status_graph(data['job_id'])
                _need(snapshot['revision'] == data['expected_revision'], 'STALE',
                      'Execution revision changed; read its native status before retrying the mutation.')
                if operation == 'resume':
                    graph = self._graph(dc._read_artifact(root, data['dependency_snapshot']))
                    _need(object_hash(graph) == snapshot['graph_sha256'], 'DEPENDENCY', 'Resume dependency graph identity changed.')
                    _need(bool(data['ownership_evidence']), 'OWNERSHIP', 'Resume requires exact original native ownership evidence.')
                    expected_receipts = {node_id: node['native']['receipt'] for node_id, node in snapshot['nodes'].items()}
                    observed = {}
                    for reference in data['ownership_evidence']:
                        document = decode_json(dc._read_artifact(root, reference))
                        _need(isinstance(document, dict) and set(document) == {'node', 'native_receipt'} and
                              document['node'] not in observed, 'OWNERSHIP', 'Invalid or repeated ownership evidence.')
                        observed[document['node']] = document['native_receipt']
                    _need(observed == expected_receipts, 'OWNERSHIP', 'Ownership evidence differs from the actual native jobs.')
                    snapshot = self.core.resume_graph(data['job_id'], expected_revision=data['expected_revision'])
                elif operation == 'cancel':
                    snapshot = self.core.cancel_graph(data['job_id'], expected_revision=data['expected_revision'])
            self._context(request, root)
            value = self._value(snapshot, require_completion=operation != 'submit')
            self._context(request, root)  # Native decoding/recording also consumes the method deadline.
            if operation == 'submit' and snapshot['state'] == 'complete':
                # Submission acknowledges immutable graph admission, never a
                # new execution or a render-completion milestone.
                value['state'] = 'queued'
                value['diagnostics'].append(dict(_diagnostic('DIRECTOR_EXECUTION_REUSE',
                    'Submission acknowledged. Query status for independently verified native cache reuse.'), severity='info'))
            status = ('blocked_capability' if any(d['code'] == 'DIRECTOR_EXECUTION_RECORDER' for d in value['diagnostics'])
                      else 'awaiting_external_result' if snapshot['state'] == 'uncertain' else 'succeeded')
            result = dict(schema_version=1, kind='director-result', method=method, status=status,
                operation_id=context['request_id'], input_digest=object_hash(request),
                value=value if status in ('succeeded', 'partial') else None,
                artifacts=deepcopy(value['artifacts']), diagnostics=deepcopy(value['diagnostics']),
                usage=_usage(), provenance_receipts=deepcopy(value['native_workers']))
        except (RigError, ValueError, OSError) as exc:
            code = getattr(exc, 'report', {}).get('code', 'DIRECTOR_EXECUTION_FAILED')
            status = 'blocked_budget' if 'BUDGET' in code else 'blocked_capability' if any(
                part in code for part in ('CAPABILITY', 'RECORDER', 'PLATFORM', 'UNAVAILABLE')) else 'failed'
            result = dict(schema_version=1, kind='director-result', method=method, status=status,
                operation_id=request['context']['request_id'], input_digest=object_hash(request), value=None,
                artifacts=[], diagnostics=[_diagnostic(code, str(exc))], usage=_usage(), provenance_receipts=[])
        dc.validate_method_result(method, result, request)
        return result

    def submit(self, request, root):
        return self._invoke('submit', request, root)

    def status(self, request, root):
        return self._invoke('status', request, root)

    def resume(self, request, root):
        return self._invoke('resume', request, root)

    def cancel(self, request, root):
        return self._invoke('cancel', request, root)
