"""Compile a graph bound to a real native OBJ import probe (no production jobs)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from director_capabilities import probe
from director_graph import GraphCompiler, traceability

ROOT = Path(__file__).resolve().parents[1]


def demonstrate(destination):
    destination = Path(destination)
    fixtures = json.loads((ROOT / 'examples/director-contracts/contracts.json').read_text())
    def request(method):
        result = deepcopy(next(item for item in fixtures if item.get('method') == method and item['kind'] == 'director-request'))
        result['context']['deadline'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        return result
    def write(name, value, kind):
        data = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
        path = destination / name
        with path.open('xb') as stream:
            stream.write(data)
        return dict(artifact_id=path.stem, kind=kind, schema_version=1, semantic_version='1.0.0',
                    path=name, sha256=hashlib.sha256(data).hexdigest(), byte_size=len(data),
                    media_type='application/json', provenance_ref=None, fixture=True, dependencies=[])
    probing = request('CapabilityRegistry.probe')
    probing['input'].update(operations=['importer.obj'], local_probes=['importer.obj'], provider_configuration_refs=[])
    snapshot = probe(probing, destination)
    source = snapshot['artifacts'][0]['dependencies'][0]
    nodes = []
    for name, seconds in [('inspect', 1), ('resolve', 2), ('reuse', 3)]:
        nodes.append(dict(id=name, operation='AssetResolver.resolve', native_owner='AssetResolver',
                          inputs=[source], output_schema='resolution-plan', resource_class='cpu',
                          required_capabilities=['importer.obj'], estimated_seconds=seconds,
                          requirement_ids=['exact-native-input']))
    plan = dict(schema_version=1, kind='execution-plan', state='planned', nodes=nodes,
                edges=[dict(source='inspect', target='resolve', artifact=source),
                       dict(source='resolve', target='reuse', artifact=source)],
                requirements=[dict(id='exact-native-input', test='test_actual_compile_topology_exact_edges_and_critical_path',
                                   evidence=snapshot['artifacts'])])
    compile_request = request('GraphCompiler.compile')
    compile_request['context']['capability_snapshot_sha256'] = snapshot['value']['digest']
    compile_request['input'].update(plan=write('plan.json', plan, 'execution-plan'),
                                    capabilities=write('capabilities.json', snapshot, 'capability-snapshot'),
                                    resolution=write('resolution.json', {'requirements': []}, 'resolution-plan'))
    compile_request['input']['budget']['max_cpu_seconds'] = 60
    result = GraphCompiler().compile(compile_request, destination)
    write('request.json', compile_request, 'director-request')
    write('graph.json', result, 'director-result')
    report = dict(schema_version=1, fixture=True, production_jobs_executed=False,
                  native_obj_probe_executed=snapshot['value']['capabilities'][0]['execution_verified'],
                  compiled=result['status'] == 'succeeded',
                  traceability=traceability(plan, result['value'], request=compile_request, root=destination) if result['value'] else [],
                  graph_sha256=hashlib.sha256((destination / 'graph.json').read_bytes()).hexdigest())
    write('acceptance.json', report, 'graph-acceptance')
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    report = demonstrate(args.destination)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['compiled'] and report['native_obj_probe_executed'] else 1)
