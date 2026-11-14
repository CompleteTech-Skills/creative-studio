"""Durable DAG orchestration over authoritative native owners.

Only graph intent and observations live here. JobStore and native acquisition
retain execution claims, worker leases, capacity accounting and final artifacts.
An uncertain handoff is reconciled against its original native identity.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time

from director_graph import _verify_refs, topological_order, _snapshot
from director_capabilities import assert_admission
from rig_errors import RigError, finite_json, object_hash, portable_name


def _need(condition, message):
    if not condition:
        raise RigError('DIRECTOR_EXECUTION', message, stage='director-execution')


def _dump(value):
    stack = [(value, 0)]; count = 0
    while stack:
        current, depth = stack.pop(); count += 1
        _need(depth <= 64 and count <= 262144, 'Execution value exceeds structural bounds.')
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    finite_json(value)
    result = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    _need(len(result.encode()) <= 4 * 1024**2, 'Director execution document exceeds four MiB.')
    return result


def retry_delay(attempt, *, base_seconds=1, maximum_seconds=300, retry_after=None, now=None):
    """Bound exponential delay and HTTP Retry-After without shortening it."""
    from email.utils import parsedate_to_datetime
    _need(type(attempt) is int and 1 <= attempt <= 17, 'Invalid bounded attempt index.')
    for value in (base_seconds, maximum_seconds):
        _need(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 86400,
              'Invalid retry delay bound.')
    _need(base_seconds <= maximum_seconds, 'Retry base exceeds maximum.')
    delay = min(maximum_seconds, base_seconds * 2**(attempt - 1))
    if retry_after is not None:
        _need(isinstance(retry_after, str) and len(retry_after) <= 128, 'Invalid Retry-After.')
        if retry_after.isascii() and retry_after.isdecimal():
            _need(len(retry_after) <= 10, 'Retry-After exceeds its bound.')
            requested = int(retry_after)
        else:
            try:
                value = parsedate_to_datetime(retry_after)
                _need(value.tzinfo is not None, 'Retry-After date needs a timezone.')
                requested = max(0, value.timestamp() - (time.time() if now is None else now))
            except (ValueError, TypeError, OverflowError) as exc:
                raise RigError('DIRECTOR_EXECUTION', 'Invalid Retry-After date.', stage='director-execution') from exc
        # An excessive server delay blocks admission rather than being silently
        # capped to an earlier, unauthorized retry.
        _need(requested <= 86400, 'Retry-After requires an explicit later resumption.')
        delay = max(delay, requested)
    return delay


class ExecutionService:
    """Persistent readiness and reconciliation in the existing JobStore DB.

    ``drivers`` contains trusted host-created native adapters, keyed by exact
    Director method. It is not an executable registry supplied by graph JSON.
    ``ledger`` is the existing BudgetLedger, never copied into SQLite.
    """

    def __init__(self, store, *, drivers, ledger, clock=time.time):
        self.store, self.drivers, self.ledger, self.clock = store, dict(drivers), ledger, clock
        with store._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS director_runs(
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, graph TEXT NOT NULL,
                graph_hash TEXT NOT NULL, bindings TEXT NOT NULL, input_root TEXT NOT NULL,
                capabilities TEXT NOT NULL, policy TEXT NOT NULL, expires REAL NOT NULL,
                definition_hash TEXT NOT NULL, ledger_root TEXT NOT NULL,
                context_binding TEXT NOT NULL,
                cancel INTEGER NOT NULL DEFAULT 0)''')
            db.execute('''CREATE TABLE IF NOT EXISTS director_nodes(
                run TEXT NOT NULL, node TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                phase TEXT NOT NULL DEFAULT 'queued', not_before REAL NOT NULL DEFAULT 0,
                dispatch TEXT, error TEXT, receipt TEXT, resource_lease TEXT,
                PRIMARY KEY(run,node), FOREIGN KEY(run) REFERENCES director_runs(id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS director_run_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL,
                at REAL NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)''')

    def _event(self, db, identifier, kind, detail):
        count = db.execute('SELECT COUNT(*) FROM director_run_events WHERE run=?', (identifier,)).fetchone()[0]
        _need(count < 32768, 'Director event history requires archival before further work.')
        db.execute('INSERT INTO director_run_events(run,at,event,detail) VALUES(?,?,?,?)',
                   (identifier, self.clock(), kind, _dump(detail)))
        db.execute('UPDATE director_runs SET revision=revision+1 WHERE id=?', (identifier,))

    def _read(self, db, identifier):
        row = db.execute('SELECT * FROM director_runs WHERE id=?', (identifier,)).fetchone()
        _need(row is not None, 'Unknown execution graph.')
        result = dict(row)
        for name in ('graph', 'bindings', 'capabilities', 'policy', 'context_binding'):
            result[name] = json.loads(result[name])
        _need(result['graph_hash'] == object_hash(result['graph']), 'Persisted graph identity changed.')
        definition = {name: result[name] for name in ('graph', 'bindings', 'input_root', 'capabilities', 'policy', 'ledger_root', 'context_binding')}
        _need(result['definition_hash'] == object_hash(definition), 'Persisted execution binding changed.')
        _need(result['ledger_root'] == str(self.ledger.root), 'Execution must reopen its original budget ledger.')
        return result

    @staticmethod
    def _revision(row):
        return object_hash(dict(id=row['id'], revision=row['revision'], graph=row['definition_hash'],
                                bindings=row['bindings'], cancel=row['cancel']))

    def _validate_graph(self, graph):
        from jsonschema import Draft202012Validator
        from director_contracts import schemas, artifact_references, validate_execution_metadata
        _dump(graph)
        definitions = schemas()['director.schema.json']['$defs']
        alias = 'output-execution-graph-v2' if graph.get('schema_version') == 2 else 'output-execution-graph'
        error = next(Draft202012Validator({'$defs': definitions, '$ref': '#/$defs/' + alias}).iter_errors(graph), None)
        _need(error is None, str(error.message)[:2048] if error else '')
        ids = [node['id'] for node in graph['nodes']]
        _need(len(ids) == len(set(ids)), 'Graph repeats a node identity.')
        topological_order(graph)
        edges = set()
        by_id = {node['id']: node for node in graph['nodes']}
        from director_schema import METHODS
        from director_graph import RESOURCE_CLASSES
        for node in graph['nodes']:
            if graph.get('schema_version') == 2:
                # A compiled declaration never supplies native execution proof.
                # Current owner state is derived separately from exact bindings.
                validate_execution_metadata(node['execution_metadata'], planned=True)
            _need(node['native_owner'] == node['operation'].split('.')[0], 'Graph names a different native owner.')
            _need(node['output_schema'] == METHODS[node['operation']][1], 'Graph output schema differs from its native method.')
            _need(node['resource_class'] in RESOURCE_CLASSES, 'Unknown native resource class.')
            _need(len(node['gates']) == len(set(node['gates'])), 'Graph repeats capability requirements.')
        for edge in graph['edges']:
            key = (edge['source'], edge['target'], object_hash(edge['artifact']))
            _need(key not in edges, 'Graph repeats a dependency edge.')
            edges.add(key)
            _need(all(edge['artifact'] in by_id[edge[end]]['inputs'] for end in ('source', 'target')),
                  'Dependency must bind exact materialized native input bytes.')
        return list(artifact_references(graph))

    def _admission(self, run, node, binding):
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        _verify_refs(run['input_root'], self._validate_graph(run['graph']))
        driver = self.drivers[node['operation']]
        required = driver.required_capabilities(binding['native'])
        _need(set(required) <= set(node['gates']), 'Graph omits a mandatory native capability.')
        estimates = driver.admission_resources(binding['native'])
        for native, capacity in (('max_memory_bytes', 'memory_bytes'), ('max_storage_bytes', 'storage_bytes')):
            if native in estimates:
                _need(binding['resources'][capacity] >= estimates[native], 'Binding understates native resource requirements.')
        assert_admission(run['capabilities'], node['gates'], evidence_root=run['input_root'], now=now,
                         resources=estimates, budget_reservation_id=binding['reservation_id'], **run['policy'])
        state = self.ledger.snapshot()
        row = state['state']['reservations'].get(binding['reservation_id'])
        _need(row is not None and row['state'] == 'reserved', 'Exact active budget reservation is required.')
        _need(row['operation_id'] == binding['operation_id'], 'Budget operation identity differs from the node binding.')
        return state, row

    def submit_graph(self, identifier, graph, bindings, input_root, *, capabilities, policy=None, context_binding=None):
        portable_name(identifier)
        _dump(bindings)
        context_binding = deepcopy(context_binding or {})
        _dump(context_binding)
        _need(not context_binding or (set(context_binding) == {'project_id', 'project_revision_sha256', 'cancellation_token', 'request_id', 'idempotency_key'}
              and all(isinstance(value, str) and 0 < len(value) <= 128 for value in context_binding.values())),
              'Invalid immutable execution context binding.')
        refs = self._validate_graph(graph)
        root = Path(input_root).absolute()
        _need(root.is_dir() and root.resolve() == root, 'Input root must be a canonical existing directory.')
        _verify_refs(root, refs)
        _need(isinstance(bindings, dict) and set(bindings) == {node['id'] for node in graph['nodes']},
              'Every graph node needs one exact native binding.')
        policy = deepcopy(policy or {})
        _need(not set(policy) - {'network_policy', 'paid_policy', 'allowed_providers', 'budget_currency',
                                'budget_amount', 'allow_unknown_price'}, 'Unknown admission policy.')
        supplied_snapshot = capabilities.get('value') if isinstance(capabilities, dict) and capabilities.get('kind') == 'director-result' else capabilities
        _dump(supplied_snapshot)
        # Idempotent submission reopens the original run. It does not re-admit
        # execution using a subsequently expired capability or settled budget.
        with self.store._transaction() as db:
            existing = db.execute('SELECT id FROM director_runs WHERE id=?', (identifier,)).fetchone()
            old_run = self._read(db, identifier) if existing else None
        if old_run is not None:
            _need(old_run['graph'] == graph and old_run['bindings'] == bindings
                  and old_run['input_root'] == str(root) and old_run['capabilities'] == supplied_snapshot
                  and old_run['policy'] == policy and old_run['context_binding'] == context_binding,
                  'Existing graph identity cannot be repurposed.')
            return self.status_graph(identifier)
        snapshot = _snapshot(_dump(capabilities).encode(), root, datetime.fromtimestamp(self.clock(), timezone.utc))
        prepared = dict(graph=deepcopy(graph), bindings=deepcopy(bindings), input_root=str(root),
                        capabilities=snapshot, policy=policy, ledger_root=str(self.ledger.root), context_binding=context_binding)
        native_identities, reservations = set(), set()
        for node in graph['nodes']:
            binding = bindings[node['id']]
            _need(isinstance(binding, dict) and set(binding) == {'native', 'reservation_id', 'operation_id', 'resources', 'retry'},
                  'Expected exact native/reservation/resource/retry binding.')
            for key in ('reservation_id', 'operation_id'):
                portable_name(binding[key])
            _need(binding['reservation_id'] not in reservations, 'Each node needs its own exact budget reservation.')
            reservations.add(binding['reservation_id'])
            native_identity = (node['operation'], object_hash(binding['native']))
            _need(native_identity not in native_identities, 'One native operation cannot implement multiple graph nodes.')
            native_identities.add(native_identity)
            resources = binding['resources']
            _need(isinstance(resources, dict) and set(resources) == {'cpu_workers', 'gpu_workers', 'provider_workers', 'memory_bytes', 'storage_bytes'},
                  'All resource capacities must be explicit.')
            _need(all(type(value) is int and 0 <= value <= 2**53 for value in resources.values()), 'Invalid resource count.')
            if node['operation'].startswith('AcquisitionGateway.'):
                _need(resources['provider_workers'] >= 1,
                      'Native acquisition must retain provider capacity until its external outcome is known.')
            retry = binding['retry']
            _need(isinstance(retry, dict) and set(retry) == {'max_attempts', 'base_seconds', 'maximum_seconds'}, 'Invalid retry policy.')
            _need(type(retry['max_attempts']) is int and 1 <= retry['max_attempts'] <= 17, 'Invalid bounded attempts.')
            retry_delay(1, base_seconds=retry['base_seconds'], maximum_seconds=retry['maximum_seconds'])
            driver = self.drivers.get(node['operation'])
            _need(driver is not None, 'No authoritative native driver for operation: ' + node['operation'])
            driver.validate(binding['native'], node, root)
            self._admission(prepared, node, binding)
        expires = datetime.fromisoformat(snapshot['expires_at'].replace('Z', '+00:00').replace('z', '+00:00')).timestamp()
        with self.store._transaction() as db:
            old = db.execute('SELECT * FROM director_runs WHERE id=?', (identifier,)).fetchone()
            if old is not None:
                _need(old['graph_hash'] == object_hash(graph) and old['bindings'] == _dump(bindings)
                      and old['input_root'] == str(root) and old['capabilities'] == _dump(snapshot)
                      and old['policy'] == _dump(policy), 'Existing graph identity cannot be repurposed.')
            else:
                db.execute('INSERT INTO director_runs(id,revision,graph,graph_hash,bindings,input_root,capabilities,policy,expires,definition_hash,ledger_root,context_binding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier, 0, _dump(graph), object_hash(graph), _dump(bindings), str(root), _dump(snapshot), _dump(policy), expires,
                            object_hash(prepared), str(self.ledger.root), _dump(context_binding)))
                db.executemany('INSERT INTO director_nodes(run,node) VALUES(?,?)', [(identifier, node['id']) for node in graph['nodes']])
                self._event(db, identifier, 'submitted', {'graph_sha256': object_hash(graph)})
        return self.status_graph(identifier)

    def get_context_binding(self, identifier):
        with self.store._transaction() as db:
            return deepcopy(self._read(db, identifier)['context_binding'])

    def status_graph(self, identifier):
        with self.store._transaction() as db:
            run = self._read(db, identifier)
            nodes = {row['node']: dict(row) for row in db.execute('SELECT * FROM director_nodes WHERE run=?', (identifier,))}
            events = [dict(row) for row in db.execute('SELECT * FROM director_run_events WHERE run=? ORDER BY seq', (identifier,))]
        observed = {}
        for node in run['graph']['nodes']:
            native = self.drivers[node['operation']].inspect(run['bindings'][node['id']]['native'])
            _need(native['state'] in ('queued', 'running', 'complete', 'failed', 'cancelled', 'uncertain'), 'Invalid native state.')
            observed[node['id']] = dict(nodes[node['id']], native=native)
        states = [value['native']['state'] for value in observed.values()]
        phase = ('complete' if all(value == 'complete' for value in states) else
                 'running' if 'running' in states else 'uncertain' if 'uncertain' in states else
                 'uncertain' if any(value['phase'] == 'uncertain' for value in observed.values()) else
                 'cancelled' if run['cancel'] else 'failed' if 'failed' in states else 'queued')
        return dict(id=identifier, revision=self._revision(run), version=run['revision'], graph_sha256=run['graph_hash'],
                    state=phase, nodes=observed, events=events, cancel_requested=bool(run['cancel']))

    def _ready(self, db, identifier, node_id):
        run = self._read(db, identifier)
        row = db.execute('SELECT * FROM director_nodes WHERE run=? AND node=?', (identifier, node_id)).fetchone()
        if row is None or run['cancel'] or self.clock() >= run['expires'] or self.clock() < row['not_before']:
            return False
        if row['phase'] not in ('queued', 'dispatching', 'admitted'):
            return False
        parents = {edge['source'] for edge in run['graph']['edges'] if edge['target'] == node_id}
        return all(db.execute('SELECT phase FROM director_nodes WHERE run=? AND node=?', (identifier, parent)).fetchone()[0] == 'complete'
                   for parent in parents)

    def _callback(self, identifier, node_id, dispatch_id, *, resource_only=False):
        # Executed by the native owner's transaction. Bounded SQL only: no
        # provider, filesystem, ledger lock or nested transaction is entered.
        def admit(db, record):
            row = db.execute('SELECT dispatch,phase FROM director_nodes WHERE run=? AND node=?', (identifier, node_id)).fetchone()
            if row is None or row['dispatch'] != dispatch_id or row['phase'] != 'admitted':
                return False
            if resource_only:
                return bool(self._ready(db, identifier, node_id))
            run = self._read(db, identifier)
            binding = run['bindings'][node_id]['native']
            if 'job' in binding and record.get('job') != binding['job']:
                return False
            if 'request_sha256' in binding and object_hash(record.get('request')) != binding['request_sha256']:
                return False
            if 'backend_sha256' in binding and object_hash(record.get('backend')) != binding['backend_sha256']:
                return False
            if 'input_sha256' in binding and record.get('input_sha256') != binding['input_sha256']:
                return False
            return bool(self._ready(db, identifier, node_id))
        return admit

    def _proof(self, value):
        """Use the existing ledger's immutable evidence storage, outside SQL."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix='.director-proof-', dir=self.ledger.root.parent) as temporary:
            source = Path(temporary) / 'proof.json'
            source.write_text(_dump(value), encoding='utf-8')
            return [self.ledger.retain_evidence(source)]

    def _known_outcome(self, identifier, node_id, observed):
        """Persist owner-verified observations; never turn uncertainty into success."""
        from director_graph import _verify_refs
        artifacts = observed.get('artifacts', [])
        if observed['state'] == 'complete':
            _need(bool(artifacts), 'A native completed operation needs verified retained artifacts.')
            _verify_refs(observed['artifact_root'], artifacts)
        with self.store._transaction() as db:
            run = self._read(db, identifier)
            row = db.execute('SELECT * FROM director_nodes WHERE run=? AND node=?', (identifier, node_id)).fetchone()
            phase = observed['state']
            if phase == 'queued' and row['dispatch']:
                phase = 'uncertain' if row['phase'] == 'uncertain' else 'admitted'
            receipt = _dump(observed)
            if row['phase'] != phase or row['receipt'] != receipt:
                db.execute('UPDATE director_nodes SET phase=?,receipt=? WHERE run=? AND node=?',
                           (phase, receipt, identifier, node_id))
                self._event(db, identifier, 'native-observed', dict(node=node_id, state=observed['state'], receipt_sha256=object_hash(observed)))
        if row['dispatch']:
            binding = run['bindings'][node_id]
            budget = self.ledger.snapshot()
            reservation = budget['state']['reservations'][binding['reservation_id']]
            records = [value for value in reservation.get('dispatches', []) if value['id'] == row['dispatch']]
            _need(len(records) == 1, 'Original native dispatch budget fence is absent.')
            native_state = observed['state']
            outcome = {'running': 'claimed', 'uncertain': 'uncertain', 'complete': 'complete',
                       'failed': 'failed', 'cancelled': 'failed'}.get(native_state)
            if outcome is not None and records[0]['state'] != outcome:
                proof = self._proof(observed)
                self.ledger.record_dispatch_outcome(binding['reservation_id'], row['dispatch'], outcome=outcome,
                    expected_revision=budget['sha256'],
                    idempotency_key='outcome-'+object_hash([identifier, node_id, row['dispatch'], outcome])[:40], evidence=proof)
            usage = observed.get('actual_usage')
            if outcome in ('complete', 'failed') and isinstance(usage, dict) and all(value is not None for value in usage.values()):
                budget = self.ledger.snapshot()
                reservation = budget['state']['reservations'][binding['reservation_id']]
                record = next(value for value in reservation['dispatches'] if value['id'] == row['dispatch'])
                if not record['usage_accounted']:
                    proof = self._proof(observed)
                    settlement = ('complete' if outcome == 'complete' else 'retry'
                                  if row['attempt'] < binding['retry']['max_attempts'] else 'failed')
                    self.ledger.settle(binding['reservation_id'], usage=usage, outcome=settlement,
                        dispatch_id=row['dispatch'], expected_revision=budget['sha256'],
                        idempotency_key='usage-'+object_hash([identifier, node_id, row['dispatch']])[:40], evidence=proof)
            if (observed['state'] in ('complete', 'failed', 'cancelled') and row['resource_lease']
                    and json.loads(row['resource_lease']).get('pid') == os.getpid()):
                # Native terminal outcome, not an elapsed heartbeat, releases
                # concurrency capacity. Unknown usage still holds budget.
                self.store.release_resources(json.loads(row['resource_lease']))
                with self.store._transaction() as db:
                    db.execute('UPDATE director_nodes SET resource_lease=NULL WHERE run=? AND node=?', (identifier, node_id))
        return run, dict(row)

    def reconcile_graph(self, identifier):
        """Reopen original native identities and validate actual immutable outputs."""
        with self.store._transaction() as db:
            run = self._read(db, identifier)
        for node in run['graph']['nodes']:
            binding = run['bindings'][node['id']]
            driver = self.drivers[node['operation']]
            driver.validate(binding['native'], node, run['input_root'])
            observed = driver.inspect(binding['native'])
            self._known_outcome(identifier, node['id'], observed)
        return self.status_graph(identifier)

    def dispatch_ready(self, identifier):
        """Execute one native-owner step; native owners alone claim execution."""
        self.reconcile_graph(identifier)
        with self.store._transaction() as db:
            run = self._read(db, identifier)
        by_id = {node['id']: node for node in run['graph']['nodes']}
        for node_id in topological_order(run['graph']):
            with self.store._transaction() as db:
                if not self._ready(db, identifier, node_id):
                    continue
                row = dict(db.execute('SELECT * FROM director_nodes WHERE run=? AND node=?', (identifier, node_id)).fetchone())
            node, binding = by_id[node_id], run['bindings'][node_id]
            driver = self.drivers[node['operation']]
            budget, reservation = self._admission(run, node, binding)
            driver.validate(binding['native'], node, run['input_root'])
            if row['dispatch'] is None:
                _need(row['attempt'] < binding['retry']['max_attempts'], 'Bounded attempt allowance is exhausted.')
                proof = self._proof(dict(graph_sha256=run['graph_hash'], node=node, native=binding['native']))
                fence = self.ledger.begin_dispatch(binding['reservation_id'], native_owner=node['native_owner'],
                    operation_sha256=object_hash(dict(node=node, binding=binding['native'])),
                    expected_revision=budget['sha256'], idempotency_key='dispatch-'+object_hash([identifier, node_id, row['attempt']])[:40], evidence=proof)
                with self.store._transaction() as db:
                    _need(self._ready(db, identifier, node_id), 'Graph changed before native admission.')
                    current = db.execute('SELECT dispatch FROM director_nodes WHERE run=? AND node=?', (identifier, node_id)).fetchone()
                    _need(current['dispatch'] is None or current['dispatch'] == fence['dispatch_id'], 'Native dispatch identity changed.')
                    phase = 'uncertain' if fence['replayed'] else 'admitted'
                    db.execute('UPDATE director_nodes SET dispatch=?,attempt=?,phase=? WHERE run=? AND node=?',
                               (fence['dispatch_id'], row['attempt'] + 1, phase, identifier, node_id))
                    self._event(db, identifier, 'budget-fenced', dict(node=node_id, dispatch=fence['dispatch_id'], replayed=fence['replayed']))
                if fence['replayed']:
                    # An interrupted handoff is not a second provider-call
                    # authorization. Its original native owner must reconcile.
                    return self.status_graph(identifier)
                row['dispatch'] = fence['dispatch_id']
            else:
                records = [item for item in reservation.get('dispatches', []) if item['id'] == row['dispatch']]
                _need(len(records) == 1 and records[0]['state'] in ('pending', 'claimed'), 'Original budget dispatch is not active.')
            admit = self._callback(identifier, node_id, row['dispatch'])
            resource = None
            if node['operation'] != 'RenderAdapter.submit':
                resource = self.store.reserve_resources('director-'+object_hash([identifier, node_id, row['dispatch']])[:40],
                    binding['resources'], admit=self._callback(identifier, node_id, row['dispatch'], resource_only=True))
                if resource is None:
                    return self.status_graph(identifier)
                with self.store._transaction() as db:
                    db.execute('UPDATE director_nodes SET resource_lease=? WHERE run=? AND node=?',
                               (_dump(resource), identifier, node_id))
            try:
                result = driver.dispatch(binding['native'], admit=admit)
            except Exception as exc:
                with self.store._transaction() as db:
                    db.execute('UPDATE director_nodes SET error=? WHERE run=? AND node=?', (str(exc)[:2048], identifier, node_id))
                    self._event(db, identifier, 'native-dispatch-error', dict(node=node_id, error=str(exc)[:2048]))
                # Reconcile the real native state after exceptions; an error
                # message never proves that an external operation did not run.
                self.reconcile_graph(identifier)
                raise
            self.reconcile_graph(identifier)
            return self.status_graph(identifier)
        return self.status_graph(identifier)

    def heartbeat(self, identifier, node_id):
        with self.store._transaction() as db:
            run = self._read(db, identifier)
            row = db.execute('SELECT resource_lease FROM director_nodes WHERE run=? AND node=?', (identifier, node_id)).fetchone()
            _need(row is not None, 'Unknown graph node.')
        if row['resource_lease']:
            self.store.heartbeat_resources(json.loads(row['resource_lease']))
        return not bool(run['cancel'])

    def recover_graph(self, identifier):
        """Recover original native owners; provider uncertainty retains capacity."""
        self.store.recover()
        self.reconcile_graph(identifier)
        def terminal(db, record):
            for row in db.execute('SELECT phase,resource_lease FROM director_nodes WHERE run=?', (identifier,)):
                if row['phase'] not in ('complete', 'failed', 'cancelled') or not row['resource_lease']:
                    continue
                lease = json.loads(row['resource_lease'])
                if lease.get('operation_id') == record.get('operation_id'):
                    return True
            return False
        self.store.recover_resources(admit=terminal)
        return self.status_graph(identifier)

    def cancel_graph(self, identifier, *, expected_revision):
        with self.store._transaction() as db:
            run = self._read(db, identifier)
            _need(self._revision(run) == expected_revision, 'Execution revision is stale.')
            if not run['cancel']:
                db.execute('UPDATE director_runs SET cancel=1 WHERE id=?', (identifier,))
                self._event(db, identifier, 'cancel-requested', {})
        # Persist cancellation intent first; each native owner then applies its
        # own bounded cancellation. Outputs and uncertain claims are retained.
        failures = []
        for node in run['graph']['nodes']:
            try:
                self.drivers[node['operation']].cancel(run['bindings'][node['id']]['native'])
            except Exception as exc:
                failures.append(dict(node=node['id'], error=str(exc)[:2048]))
        if failures:
            with self.store._transaction() as db:
                self._event(db, identifier, 'cancellation-incomplete', dict(failures=failures))
        return self.reconcile_graph(identifier)

    def resume_graph(self, identifier, *, expected_revision, retry_after=None):
        status = self.reconcile_graph(identifier)
        _need(status['revision'] == expected_revision, 'Execution revision is stale; reconcile and read current state.')
        with self.store._transaction() as db:
            run = self._read(db, identifier)
        for node in run['graph']['nodes']:
            current = status['nodes'][node['id']]
            if current['native']['state'] not in ('failed', 'cancelled'):
                continue
            binding = run['bindings'][node['id']]
            _need(current['attempt'] < binding['retry']['max_attempts'], 'Bounded node attempt allowance is exhausted.')
            snapshot = self.ledger.snapshot()
            reservation = snapshot['state']['reservations'].get(binding['reservation_id'])
            _need(reservation is not None and not any(item['state'] in ('pending', 'claimed', 'uncertain') or not item['usage_accounted']
                for item in reservation.get('dispatches', [])), 'Reconcile exact known native outcome and actual attempt usage before retry.')
            delay = retry_delay(max(1, current['attempt']), base_seconds=binding['retry']['base_seconds'],
                                maximum_seconds=binding['retry']['maximum_seconds'], retry_after=retry_after, now=self.clock())
            self.drivers[node['operation']].resume(binding['native'])
            with self.store._transaction() as db:
                _need(self._revision(self._read(db, identifier)) == expected_revision, 'Execution changed during resume.')
                db.execute('UPDATE director_nodes SET phase="queued",dispatch=NULL,error=NULL,not_before=? WHERE run=? AND node=?',
                           (self.clock() + delay, identifier, node['id']))
                db.execute('UPDATE director_runs SET cancel=0 WHERE id=?', (identifier,))
                self._event(db, identifier, 'resumed', dict(node=node['id'], not_before=self.clock() + delay))
                expected_revision = self._revision(self._read(db, identifier))
        return self.status_graph(identifier)

