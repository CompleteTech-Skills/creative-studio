"""Executable graph compilation and hostile input regressions (synthetic plans)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import director_contracts as dc
import director_graph as dg
from rig_errors import object_hash

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd,
                     'Secure descriptor-relative file access unavailable.')
class DirectorGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fixtures = json.loads((ROOT / 'examples/director-contracts/contracts.json').read_text())
        self.request = deepcopy(next(x for x in fixtures if x.get('method') == 'GraphCompiler.compile'
                                     and x['kind'] == 'director-request'))
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.input = self.write('native-request', {'type': 'synthetic native job specification'}, kind='native-request')
        limits = deepcopy(self.request['input']['budget'])
        limits.update(max_cpu_seconds=100, max_gpu_seconds=100)
        self.evidence = self.write('probe', {'operation': 'native-cpu', 'status': 'available',
                                           'execution_verified': True, 'fixture': True,
                                           'policy_limits': limits, 'version': 'test', 'payloads': []}, kind='evidence')
        self.snapshot = dict(capabilities=[dict(operation='native-cpu', version='test', status='available',
                            limits=limits, probe_evidence=[self.evidence], execution_verified=True)],
                             expires_at=(self.now + timedelta(hours=1)).isoformat())
        self.snapshot['digest'] = object_hash(self.snapshot)
        self.plan = dict(schema_version=1, kind='execution-plan', state='planned',
                         requirements=[dict(id='produce', test='test_actual_compile', evidence=[])],
                         nodes=[self.node('a', 2), self.node('b', 3), self.node('c', 4)],
                         edges=[dict(source='a', target='b', artifact=self.input),
                                dict(source='b', target='c', artifact=self.input)])
        self.resolution = dict(requirements=[])
        self.request['input']['budget'] = deepcopy(limits)
        self.refresh()

    def write(self, name, data, *, kind='test-json'):
        payload = (json.dumps(data, sort_keys=True) + '\n').encode()
        path = name + '.json'
        (self.root / path).write_bytes(payload)
        return dict(artifact_id=name, kind=kind, schema_version=1, semantic_version='1.0.0',
                    path=path, sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload),
                    media_type='application/json', provenance_ref=None, fixture=True, dependencies=[])

    def node(self, name, seconds):
        return dict(id=name, operation='FrameEvaluator.evaluate', inputs=[deepcopy(self.input)],
                    output_schema='evaluated-frame', resource_class='cpu', native_owner='FrameEvaluator',
                    required_capabilities=['native-cpu'], estimated_seconds=seconds, requirement_ids=['produce'])

    def refresh(self):
        self.request['input'].update(plan=self.write('plan', self.plan, kind='execution-plan'),
                                     capabilities=self.write('caps', self.snapshot, kind='capability-snapshot'),
                                     resolution=self.write('resolution', self.resolution, kind='resolution-plan'))
        self.request['context']['capability_snapshot_sha256'] = self.snapshot['digest']

    def compile(self):
        return dg.compile_graph(self.request, self.root, now=self.now)

    def test_actual_compile_topology_exact_edges_and_critical_path(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = self.compile()
        self.assertEqual(result['status'], 'succeeded')
        dc.validate_method_result('GraphCompiler.compile', result, self.request)
        self.assertEqual([n['id'] for n in result['value']['nodes']], ['a', 'b', 'c'])
        self.assertEqual(result['value']['critical_path_seconds'], 9)
        self.assertEqual(result['value']['edges'], self.plan['edges'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_equivalent_reordering_preserves_graph_and_cache_keys(self):
        original = self.compile()['value']
        self.plan['nodes'].reverse()
        self.plan['edges'].reverse()
        self.refresh()
        self.assertEqual(original, self.compile()['value'])

    def test_branch_critical_path_and_unknown_estimate(self):
        self.plan['edges'] = [dict(source='a', target='c', artifact=self.input),
                              dict(source='b', target='c', artifact=self.input)]
        self.refresh()
        self.assertEqual(self.compile()['value']['critical_path_seconds'], 7)
        self.plan['nodes'][0]['estimated_seconds'] = None
        self.refresh()
        self.assertIsNone(self.compile()['value']['critical_path_seconds'])

    def test_cycles_dangling_and_duplicate_edges_rejected(self):
        for extra in (dict(source='c', target='a', artifact=self.input),
                      dict(source='missing', target='a', artifact=self.input), self.plan['edges'][0]):
            plan = deepcopy(self.plan)
            plan['edges'].append(extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                dg.validate_plan(plan)

    def test_edges_pin_complete_exact_input(self):
        self.plan['edges'][0]['artifact'] = deepcopy(self.input)
        self.plan['edges'][0]['artifact']['semantic_version'] = '2.0.0'
        self.refresh()
        with self.assertRaises(ValueError):
            self.compile()

    def test_changed_input_invalidates_node_and_descendants_only(self):
        before = self.compile()['value']
        new_input = self.write('replacement', {'new': 'native bytes'})
        self.plan['nodes'][1]['inputs'].append(new_input)
        self.refresh()
        after = self.compile()['value']
        impact = dg.impact(before, after)
        self.assertEqual(impact['preserved'], ['a'])
        self.assertEqual(impact['stale'], ['b', 'c'])

    def test_uncertain_dependency_invalidates_conservatively(self):
        graph = self.compile()['value']
        self.assertEqual(dg.impact(graph, graph, uncertain=['a'])['stale'], ['a', 'b', 'c'])
        with self.assertRaises(ValueError):
            dg.impact(graph, graph, uncertain=['absent'])

    def test_tampered_missing_and_symlink_input_fail(self):
        source = self.root / self.input['path']
        original = source.read_bytes()
        for mode in ('tamper', 'missing', 'symlink'):
            if source.exists() or source.is_symlink():
                source.unlink()
            if mode == 'tamper':
                source.write_bytes(original + b'!')
            elif mode == 'symlink':
                source.symlink_to(self.root / self.evidence['path'])
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.compile()

    def test_missing_unavailable_and_unverified_capability_fail(self):
        for change in ('missing', 'unavailable', 'untested', 'unverified', 'no-evidence'):
            original = deepcopy(self.snapshot)
            if change == 'missing':
                self.snapshot['capabilities'] = []
            elif change == 'unverified':
                self.snapshot['capabilities'][0]['execution_verified'] = False
            elif change == 'no-evidence':
                self.snapshot['capabilities'][0]['probe_evidence'] = []
            else:
                self.snapshot['capabilities'][0]['status'] = change
            self.snapshot['digest'] = object_hash({k: v for k, v in self.snapshot.items() if k != 'digest'})
            self.refresh()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.compile()
            self.snapshot = original

    def test_stale_snapshot_and_missing_evidence_fail(self):
        self.snapshot['expires_at'] = self.now.isoformat()
        self.snapshot['digest'] = object_hash({k: v for k, v in self.snapshot.items() if k != 'digest'})
        self.refresh()
        with self.assertRaises(ValueError):
            self.compile()
        self.snapshot['expires_at'] = (self.now + timedelta(hours=1)).isoformat()
        self.snapshot['digest'] = object_hash({k: v for k, v in self.snapshot.items() if k != 'digest'})
        self.refresh()
        (self.root / self.evidence['path']).unlink()
        with self.assertRaises(ValueError):
            self.compile()

    def test_false_complete_unknown_fields_and_bad_owner_fail(self):
        for key, value in [('state', 'complete'), ('production_complete', True)]:
            plan = deepcopy(self.plan)
            plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                dg.validate_plan(plan)
        for key, value in [('native_owner', 'DirectorPrivateRenderer'), ('output_schema', 'pretend-success')]:
            plan = deepcopy(self.plan)
            plan['nodes'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                dg.validate_plan(plan)

    def test_requirements_need_nodes_and_duplicate_nodes_fail(self):
        self.plan['requirements'].append(dict(id='uncovered', test='test_missing', evidence=[]))
        with self.assertRaises(ValueError):
            dg.validate_plan(self.plan)
        self.plan['requirements'].pop()
        self.plan['nodes'].append(deepcopy(self.plan['nodes'][0]))
        with self.assertRaises(ValueError):
            dg.validate_plan(self.plan)

    def test_budget_and_context_digest_fail(self):
        self.request['input']['budget']['max_cpu_seconds'] = 8
        with self.assertRaises(ValueError):
            self.compile()

    def test_class_returns_typed_capability_block(self):
        self.snapshot['capabilities'] = []
        self.snapshot['digest'] = object_hash({k: v for k, v in self.snapshot.items() if k != 'digest'})
        self.refresh()
        result = dg.GraphCompiler().compile(self.request, self.root, now=self.now)
        self.assertEqual(result['status'], 'blocked_capability')
        self.assertIsNone(result['value'])
        self.assertTrue(result['diagnostics'])
        dc.validate_method_result('GraphCompiler.compile', result, self.request)

    def test_traceability_and_edge_changes_are_in_cache_keys(self):
        before = self.compile()['value']
        mapping = dg.traceability(self.plan, before, request=self.request, root=self.root, now=self.now)
        self.assertEqual(mapping, [dict(requirement='produce', test='test_actual_compile', evidence=[], nodes=['a', 'b', 'c'])])
        second = self.write('second', {'spec': 2})
        for node in self.plan['nodes']:
            node['inputs'].append(second)
        self.refresh()
        shared = self.compile()['value']
        self.plan['edges'].append(dict(source='a', target='b', artifact=second))
        self.refresh()
        after = self.compile()['value']
        self.assertEqual(shared['nodes'][0]['cache_key'], after['nodes'][0]['cache_key'])
        self.assertNotEqual(shared['nodes'][1]['cache_key'], after['nodes'][1]['cache_key'])

    def test_expired_deadline_fails_before_reading_inputs(self):
        self.request['context']['deadline'] = (self.now - timedelta(seconds=1)).isoformat()
        with patch.object(dg, '_verify_refs', side_effect=AssertionError('read input')):
            with self.assertRaisesRegex(ValueError, 'deadline has expired'):
                self.compile()

    def test_deadline_expiring_during_compilation_fails(self):
        self.request['context']['deadline'] = (self.now + timedelta(seconds=1)).isoformat()
        with patch.object(dg.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(ValueError, 'deadline expired during compilation'):
                self.compile()

    def test_resource_classes_cannot_hide_estimated_work(self):
        for resource in ('none', 'provider', 'gpu', 'render'):
            plan = deepcopy(self.plan)
            plan['nodes'][0]['resource_class'] = resource
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                dg.validate_plan(plan)
        for node in self.plan['nodes']:
            node['resource_class'] = 'io'
        self.refresh()
        self.request['input']['budget']['max_cpu_seconds'] = 0
        with self.assertRaises(ValueError):
            self.compile()

    def test_provider_calls_are_aggregated_before_admission(self):
        for node in self.plan['nodes']:
            node.update(operation='PlanningProvider.generate_structured',
                        native_owner='PlanningProvider', output_schema='planning-artifact',
                        resource_class='provider', required_capabilities=['planning.provider'])
        self.request['context']['policy']['network'] = 'allow-configured'
        self.request['context']['policy']['policy_ref'] = self.write('network-policy', {'allow': 'configured-provider'}, kind='policy')
        self.request['input']['budget']['max_provider_calls'] = 2
        self.refresh()
        with self.assertRaisesRegex(ValueError, 'provider invocations exceed budget'):
            self.compile()

    def test_known_provider_receipt_pricing_fits_bounded_aggregate_budget(self):
        # Engineering contract fixture only: no provider execution or payment.
        limits={**deepcopy(self.request['input']['budget']),'max_provider_calls':3,
                'currency':'USD','max_amount':1}
        receipt=self.write('provider-pricing', dict(operation='planning.provider',
                   status='available',execution_verified=True,fixture=True,version='fixture',
                   policy_limits=limits,payloads=[],price_availability='known',
                   price_per_call=.25,currency='USD'),kind='evidence')
        self.snapshot['capabilities']=[dict(operation='planning.provider',version='fixture',
                   status='available',execution_verified=True,limits=limits,probe_evidence=[receipt])]
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        for node in self.plan['nodes']:
            node.update(operation='PlanningProvider.generate_structured',native_owner='PlanningProvider',
                        output_schema='planning-artifact',resource_class='provider',
                        required_capabilities=['planning.provider'])
        self.request['input']['budget']=deepcopy(limits)
        self.request['context']['policy'].update(network='allow-configured',paid='bounded',
                    policy_ref=self.write('paid-policy',{'fixture':True},kind='policy'))
        self.request['context']['budget_reservation_id']='fixture-reservation'
        self.refresh()
        self.assertEqual(self.compile()['status'],'succeeded')
        self.request['context']['policy']['paid']='deny'
        with self.assertRaisesRegex(ValueError,'paid authority'):
            self.compile()
        self.request['context']['policy']['paid']='bounded'
        self.request['input']['budget']['max_amount']=.5
        with self.assertRaisesRegex(ValueError,'provider amount exceeds budget'):
            self.compile()
        self.request['input']['budget']['max_amount']=1
        self.request['input']['budget']['currency']='EUR'
        with self.assertRaisesRegex(ValueError,'matching currency'):
            self.compile()

    def test_traceability_rejects_unbound_plan_and_graph(self):
        graph = self.compile()['value']
        altered = deepcopy(self.plan)
        altered['requirements'][0]['test'] = 'not-the-compiled-test'
        with self.assertRaises(ValueError):
            dg.traceability(altered, graph, request=self.request, root=self.root, now=self.now)
        graph['nodes'][0]['cache_key'] = 'a' * 64
        with self.assertRaises(ValueError):
            dg.traceability(self.plan, graph, request=self.request, root=self.root, now=self.now)

    def test_concurrent_source_change_fails_before_result(self):
        original = dg._read
        replaced = False
        def read(root, artifact):
            nonlocal replaced
            data = original(root, artifact)
            if artifact['path'] == self.input['path'] and not replaced:
                (self.root / artifact['path']).write_bytes(b'changed during compile')
                replaced = True
            return data
        with patch.object(dg, '_read', side_effect=read), self.assertRaises(ValueError):
            self.compile()

    def test_malformed_snapshot_is_typed_failure(self):
        self.request['input']['capabilities'] = self.write('bad-caps', [], kind='capability-snapshot')
        result = dg.GraphCompiler().compile(self.request, self.root, now=self.now)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('SCHEMA', result['diagnostics'][0]['code'])

    def test_generated_plan_schema_matches_authority(self):
        self.assertEqual(json.loads((ROOT / 'profiles/director-execution-plan.schema.json').read_text()), dg.plan_schema())

    def test_native_render_and_provider_capabilities_cannot_be_substituted(self):
        for operation, schema, owner in [('RenderAdapter.submit', 'job-snapshot', 'RenderAdapter'),
                                         ('PlanningProvider.generate_structured', 'planning-artifact', 'PlanningProvider'),
                                         ('AcquisitionGateway.claim', 'acquisition-state', 'AcquisitionGateway')]:
            plan = deepcopy(self.plan)
            plan['nodes'][0].update(operation=operation, output_schema=schema, native_owner=owner)
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                dg.validate_plan(plan)
        self.request['input']['budget']['max_cpu_seconds'] = 100
        self.request['context']['capability_snapshot_sha256'] = 'a' * 64
        with self.assertRaises(ValueError):
            self.compile()


if __name__ == '__main__':
    unittest.main()
