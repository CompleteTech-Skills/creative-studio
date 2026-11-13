"""Compile exact Director job specifications without executing native jobs.

Edges bind materialized specification/dependency artifacts, never invented
future output hashes. Native execution must validate produced outputs before
any consumer runs. Compilation alone confers no production milestone.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path

import director_contracts as dc
from director_schema import ID, TEXT, INTEGER, NUMBER, METHODS, definitions
from rig_contracts import arr, obj, ref
from rig_errors import RigError, decode_json, finite_json, object_hash, portable_name, require

RESOURCE_CLASSES = ('none', 'cpu', 'gpu', 'io', 'provider', 'render')


def _require(condition, code, message):
    require(condition, 'DIRECTOR_GRAPH_' + code, message, stage='director-graph')


def plan_schema():
    """Strict compiler input. Output intent is a schema, never a fake file."""
    defs = {'artifact-ref': definitions()['artifact-ref']}
    defs['graph-plan-node'] = obj(dict(
        id=ID, operation={'enum': list(METHODS)}, inputs=arr(ref('artifact-ref'), 1, 256),
        output_schema=ID, resource_class={'enum': list(RESOURCE_CLASSES)}, native_owner=ID,
        required_capabilities=arr(ID, 1, 32), estimated_seconds={'anyOf': [NUMBER, {'type': 'null'}]},
        requirement_ids=arr(ID, 1, 256)))
    return dict(**{'$schema': 'https://json-schema.org/draft/2020-12/schema'},
                **obj(dict(schema_version={'const': 1}, kind={'const': 'execution-plan'},
                           state={'const': 'planned'},
                           requirements=arr(obj(dict(id=ID, test=TEXT, evidence=arr(ref('artifact-ref'), 0, 128))), 1, 1024),
                           nodes=arr(ref('graph-plan-node'), 1, 1024),
                           edges=arr(obj(dict(source=ID, target=ID, artifact=ref('artifact-ref'))), 0, 4096))),
                **{'$defs': defs})


def schemas():
    return {'director-execution-plan.schema.json': plan_schema()}


def _sort_refs(refs):
    return sorted(deepcopy(refs), key=lambda item: (item['artifact_id'], item['semantic_version'], item['path']))


def canonical_plan(plan):
    """Order-insensitive declarative collections produce identical cache keys."""
    result = deepcopy(plan)
    for node in result['nodes']:
        node['inputs'] = _sort_refs(node['inputs'])
        node['required_capabilities'] = sorted(node['required_capabilities'])
        node['requirement_ids'] = sorted(node['requirement_ids'])
    for item in result['requirements']:
        item['evidence'] = _sort_refs(item['evidence'])
    result['nodes'].sort(key=lambda item: item['id'])
    result['requirements'].sort(key=lambda item: item['id'])
    result['edges'].sort(key=lambda item: (item['source'], item['target'], object_hash(item['artifact'])))
    return result


def validate_plan(plan):
    from jsonschema import Draft202012Validator
    finite_json(plan)
    error = next(Draft202012Validator(plan_schema()).iter_errors(plan), None)
    _require(error is None, 'SCHEMA', error.message[:2048] if error else '')
    ids = [item['id'] for item in plan['nodes']]
    requirements = [item['id'] for item in plan['requirements']]
    _require(len(ids) == len(set(ids)), 'DUPLICATE', 'Node IDs must be unique.')
    _require(len(requirements) == len(set(requirements)), 'DUPLICATE', 'Requirement IDs must be unique.')
    by_id = {item['id']: item for item in plan['nodes']}
    edge_ids = set()
    used = set()
    for node in plan['nodes']:
        _require(node['native_owner'] == node['operation'].split('.')[0], 'OWNER',
                 'Nodes must delegate to the named native interface owner.')
        _require(node['output_schema'] == METHODS[node['operation']][1], 'OUTPUT',
                 'Output schema must match the native method contract.')
        if node['operation'] == 'RenderAdapter.submit':
            _require(bool({'blender.cpu', 'blender.gpu'} & set(node['required_capabilities'])),
                     'CAPABILITY', 'Render submission requires an executed Blender backend capability.')
        if node['operation'] == 'PlanningProvider.generate_structured':
            _require('planning.provider' in node['required_capabilities'], 'CAPABILITY',
                     'Model planning requires its actual provider capability.')
        if node['operation'].startswith('AcquisitionGateway.'):
            _require('native.acquisition' in node['required_capabilities'], 'CAPABILITY',
                     'Acquisition must use the native acquisition capability.')
        for key in ('required_capabilities', 'requirement_ids'):
            _require(len(node[key]) == len(set(node[key])), 'DUPLICATE', 'Duplicate ' + key + '.')
        _require(set(node['requirement_ids']) <= set(requirements), 'REFERENCE', 'Unknown requirement.')
        used.update(node['requirement_ids'])
        hashes = [object_hash(item) for item in node['inputs']]
        _require(len(hashes) == len(set(hashes)), 'DUPLICATE', 'Duplicate node input.')
    _require(used == set(requirements), 'TRACEABILITY', 'Every requirement needs a compiled node.')
    for edge in plan['edges']:
        _require(edge['source'] in by_id and edge['target'] in by_id, 'REFERENCE', 'Dangling graph edge.')
        key = (edge['source'], edge['target'], object_hash(edge['artifact']))
        _require(key not in edge_ids, 'DUPLICATE', 'Duplicate dependency edge.')
        edge_ids.add(key)
        _require(all(edge['artifact'] in by_id[edge[item]]['inputs'] for item in ('source', 'target')),
                 'REFERENCE', 'An edge must pin an exact shared native specification or dependency input.')
    topological_order(plan)
    return plan


def topological_order(plan):
    nodes = {item['id'] for item in plan['nodes']}
    successors = {key: set() for key in nodes}
    counts = {key: 0 for key in nodes}
    for edge in plan['edges']:
        source, target = edge['source'], edge['target']
        _require(source in nodes and target in nodes, 'REFERENCE', 'Dangling graph edge.')
        if target not in successors[source]:
            successors[source].add(target)
            counts[target] += 1
    ready = [key for key in nodes if counts[key] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for child in sorted(successors[node]):
            counts[child] -= 1
            if counts[child] == 0:
                heapq.heappush(ready, child)
    _require(len(order) == len(nodes), 'CYCLE', 'Graph contains a dependency cycle.')
    return order


def _read(root, artifact):
    # Reuse the owning bounded, descriptor-relative no-follow reader.
    return dc._read_artifact(root, artifact)


def _all_refs(value):
    return list(dc.artifact_references(value))


def _verify_refs(root, refs):
    paths, aliases, identities, payloads = {}, {}, {}, {}
    import unicodedata
    for artifact in refs:
        key = (artifact['artifact_id'], artifact['semantic_version'])
        path = artifact['path']
        _require(portable_name(path) == unicodedata.normalize('NFC', path), 'PATH', 'Noncanonical artifact path.')
        alias = unicodedata.normalize('NFC', path).casefold()
        _require(path not in paths or paths[path] == artifact, 'REFERENCE', 'Conflicting file reference.')
        _require(key not in identities or identities[key] == artifact, 'REFERENCE', 'Conflicting artifact identity.')
        _require(alias not in aliases or aliases[alias] == path, 'PATH', 'Conflicting path alias.')
        paths[path], identities[key], aliases[alias] = artifact, artifact, path
    _require(len(paths) <= dc.MAX_FILES and sum(item['byte_size'] for item in paths.values()) <= dc.MAX_TOTAL_BYTES,
             'LIMIT', 'Input closure exceeds Director bounds.')
    for path, artifact in sorted(paths.items()):
        payloads[path] = _read(root, artifact)
    return payloads


def _snapshot(payload, root, now):
    value = decode_json(payload)
    _require(isinstance(value, dict), 'SCHEMA', 'Capability snapshot must be an object.')
    if value.get('kind') == 'director-result':
        dc.validate_document(value)
        _require(value['method'] == 'CapabilityRegistry.probe' and value['status'] == 'succeeded',
                 'CAPABILITY', 'Expected a successful capability probe, not execution success.')
        value = value['value']
    from jsonschema import Draft202012Validator
    schema = dc.schemas()['director.schema.json']
    errors = list(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/output-capability-snapshot'},
                         format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(value))
    _require(not errors, 'SCHEMA', 'Invalid capability snapshot structure.')
    expected = object_hash({key: child for key, child in value.items() if key != 'digest'})
    _require(value['digest'] == expected, 'CAPABILITY', 'Capability snapshot digest is stale.')
    expiry = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00'))
    _require(expiry > now, 'CAPABILITY', 'Capability snapshot expired; probe again.')
    _verify_refs(root, _all_refs(value))
    return value


def _usage():
    return dict(classification='unavailable',
                **{key: dict(value=None, explanation='Compiler does not meter this resource.') for key in
                   ('provider_calls', 'input_tokens', 'output_tokens', 'cpu_seconds', 'gpu_seconds', 'storage_bytes', 'amount')},
                currency=None, measurement_source='director-graph-v1')


def compile_graph(request, root, *, now=None):
    """Return a typed GraphCompiler result; no native jobs are submitted.

    All external inputs are byte-verified twice around compilation. Execution
    controllers must reverify the resulting pinned inputs when starting work.
    """
    dc.validate_method_request('GraphCompiler.compile', request)
    now = now or datetime.now(timezone.utc)
    _require(now.tzinfo is not None, 'TIME', 'Compiler clock must have timezone information.')
    payloads = _verify_refs(root, _all_refs(request))
    data = request['input']
    plan = decode_json(payloads[data['plan']['path']])
    validate_plan(plan)
    plan = canonical_plan(plan)
    snapshot = _snapshot(payloads[data['capabilities']['path']], root, now)
    _require(snapshot['digest'] == request['context']['capability_snapshot_sha256'],
             'CAPABILITY', 'Operation context pins a different capability snapshot.')
    resolution = decode_json(payloads[data['resolution']['path']])
    _require(isinstance(resolution, dict), 'SCHEMA', 'Resolution must be an object.')
    if resolution.get('kind') == 'director-result':
        dc.validate_document(resolution)
        _require(resolution['method'] == 'AssetResolver.resolve' and resolution['status'] == 'succeeded',
                 'RESOLUTION', 'Asset resolution is incomplete.')
        resolution = resolution['value']
    from jsonschema import Draft202012Validator
    schema = dc.schemas()['director.schema.json']
    errors = list(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/output-resolution-plan'}).iter_errors(resolution))
    _require(not errors, 'SCHEMA', 'Invalid asset resolution structure.')
    _require(all(item['disposition'] in ('reusable', 'adaptable') for item in resolution['requirements']),
             'RESOLUTION', 'A mandatory asset is missing, incompatible, unreviewed or unavailable.')
    _require(all(item['candidates'] for item in resolution['requirements']),
             'RESOLUTION', 'Reusable/adaptable requirements need exact existing candidate artifacts.')
    closure = _all_refs(plan) + _all_refs(resolution)
    _verify_refs(root, closure)
    available = {item['operation']: item for item in snapshot['capabilities']}
    _require(len(available) == len(snapshot['capabilities']), 'CAPABILITY', 'Duplicate capability operation.')
    estimates = {name: 0 for name in RESOURCE_CLASSES}
    for node in plan['nodes']:
        if node['estimated_seconds'] is not None:
            estimates[node['resource_class']] += node['estimated_seconds']
        if node['resource_class'] == 'provider':
            _require(request['context']['policy']['network'] == 'allow-configured', 'POLICY',
                     'Provider nodes require explicit configured-network policy.')
    _require(estimates['cpu'] + estimates['render'] <= data['budget']['max_cpu_seconds'],
             'BUDGET', 'Estimated CPU work exceeds budget.')
    _require(estimates['gpu'] <= data['budget']['max_gpu_seconds'], 'BUDGET', 'Estimated GPU work exceeds budget.')
    predecessors = {item['id']: [] for item in plan['nodes']}
    for edge in plan['edges']:
        predecessors[edge['target']].append(edge['source'])
    by_id = {item['id']: item for item in plan['nodes']}
    keys, finish, nodes = {}, {}, []
    for node_id in topological_order(plan):
        node = by_id[node_id]
        _require(all(name in available and available[name]['probe_evidence']
                     for name in node['required_capabilities']), 'CAPABILITY',
                 'Every mandatory capability needs retained execution evidence.')
        from director_capabilities import assert_admission
        resource_estimate = {}
        if node['estimated_seconds'] is not None:
            if node['resource_class'] in ('cpu', 'render'):
                resource_estimate['max_cpu_seconds'] = node['estimated_seconds']
            elif node['resource_class'] == 'gpu':
                resource_estimate['max_gpu_seconds'] = node['estimated_seconds']
        if node['resource_class'] == 'provider':
            resource_estimate['max_provider_calls'] = 1
        assert_admission(snapshot, node['required_capabilities'], evidence_root=root, now=now,
                         resources=resource_estimate,
                         network_policy=request['context']['policy']['network'],
                         budget_reservation_id=request['context']['budget_reservation_id'],
                         allow_unknown_price=request['context']['policy']['paid'] == 'allow-unknown-price')
        parent_ids = sorted(set(predecessors[node_id]))
        keys[node_id] = object_hash(dict(node=node, parents={key: keys[key] for key in parent_ids},
                                         incoming=[edge for edge in plan['edges'] if edge['target'] == node_id],
                                         capabilities=snapshot['digest'], resolution=data['resolution']))
        duration = node['estimated_seconds']
        prior = [finish[key] for key in parent_ids]
        finish[node_id] = None if duration is None or any(value is None for value in prior) else max(prior, default=0) + duration
        nodes.append({key: deepcopy(node[key]) for key in
                      ('id', 'operation', 'inputs', 'output_schema', 'resource_class', 'native_owner')})
        nodes[-1].update(cache_key=keys[node_id], gates=sorted(node['required_capabilities']))
    graph = dict(nodes=nodes, edges=plan['edges'],
                 critical_path_seconds=None if any(value is None for value in finish.values()) else max(finish.values()))
    # Reject input replacement/corruption during compilation, before returning.
    _verify_refs(root, _all_refs(request) + closure + _all_refs(snapshot))
    result = dict(schema_version=1, kind='director-result', method='GraphCompiler.compile', status='succeeded',
                  operation_id=request['context']['request_id'], input_digest=object_hash(request),
                  value=graph, artifacts=[], diagnostics=[], usage=_usage(), provenance_receipts=[])
    dc.validate_method_result('GraphCompiler.compile', result, request)
    return result


class GraphCompiler:
    """Read-only adapter; scheduling and production remain native owners' work."""
    def compile(self, request, root, **kwargs):
        try:
            return compile_graph(request, root, **kwargs)
        except RigError as exc:
            # Malformed envelope cannot be represented as a valid bound result.
            dc.validate_method_request('GraphCompiler.compile', request)
            code = exc.report['code']
            status = 'blocked_capability' if 'CAPABILITY' in code else 'blocked_budget' if 'BUDGET' in code else 'failed'
            diagnostic = dict(code=code, severity='error', stage='graph', entity_id=None,
                              field_path='input', expected='verified compilable native plan',
                              observed=str(exc)[:4096], retryable=False,
                              recommended_action='Correct the pinned input or obtain current native capability evidence.',
                              evidence=[])
            result = dict(schema_version=1, kind='director-result', method='GraphCompiler.compile', status=status,
                          operation_id=request['context']['request_id'], input_digest=object_hash(request), value=None,
                          artifacts=[], diagnostics=[diagnostic], usage=_usage(), provenance_receipts=[])
            dc.validate_method_result('GraphCompiler.compile', result, request)
            return result


def traceability(plan, graph):
    """Map every declared requirement to compiled nodes and exact test/evidence."""
    validate_plan(plan)
    nodes = {item['id']: item for item in graph['nodes']}
    _require(set(nodes) == {item['id'] for item in plan['nodes']}, 'TRACEABILITY', 'Graph differs from plan.')
    return [dict(requirement=item['id'], test=item['test'], evidence=_sort_refs(item['evidence']),
                 nodes=sorted(node['id'] for node in plan['nodes'] if item['id'] in node['requirement_ids']))
            for item in sorted(plan['requirements'], key=lambda item: item['id'])]


def impact(previous, current, *, uncertain=()):
    """Conservatively invalidate changed nodes and every downstream consumer."""
    for graph in (previous, current):
        from jsonschema import Draft202012Validator
        schema = dc.schemas()['director.schema.json']
        Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/output-execution-graph'}).validate(graph)
    before = {item['id']: item for item in previous['nodes']}
    after = {item['id']: item for item in current['nodes']}
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    old_edges = {object_hash(edge): edge for edge in previous['edges']}
    new_edges = {object_hash(edge): edge for edge in current['edges']}
    changed.update((old_edges.get(key) or new_edges[key])['target']
                   for key in set(old_edges) ^ set(new_edges))
    _require(set(uncertain) <= set(before) | set(after), 'REFERENCE', 'Unknown uncertain node.')
    changed.update(uncertain)
    children = {key: set() for key in set(before) | set(after)}
    for edge in previous['edges'] + current['edges']:
        _require(edge['source'] in children and edge['target'] in children, 'REFERENCE', 'Dangling impact edge.')
        children[edge['source']].add(edge['target'])
    stale = set(changed)
    pending = list(changed)
    while pending:
        for child in children[pending.pop()]:
            if child not in stale:
                stale.add(child)
                pending.append(child)
    return dict(changed=sorted(changed), stale=sorted(stale), preserved=sorted(set(after) - stale),
                removed=sorted(set(before) - set(after)))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = GraphCompiler().compile(decode_json(dc._read_json_file(args.request)), args.root)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'succeeded' else 1
    except (RigError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'diagnostic': exc.report if isinstance(exc, RigError) else str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
