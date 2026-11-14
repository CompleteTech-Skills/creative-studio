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
from rig_errors import RigError, object_hash

ROOT = Path(__file__).resolve().parents[1]


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

    def require_secure_reads(self):
        """Exercise the real fail-closed boundary when secure reads are unavailable."""
        if hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd:
            return True
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with self.assertRaises(RigError) as failure:
            self.compile()
        self.assertEqual(failure.exception.report['code'], 'DIRECTOR_PLATFORM')
        result = dg.GraphCompiler().compile(self.request, self.root, now=self.now)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['value'])
        self.assertEqual(result['diagnostics'][0]['code'], 'DIRECTOR_PLATFORM')
        dc.validate_method_result('GraphCompiler.compile', result, self.request)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})
        return False

    def compile(self):
        return dg.compile_graph(self.request, self.root, now=self.now)

    def planned_metadata(self, node_id):
        return dict(schema_version=2, estimated_usage=dg._usage(), actual_usage=dg._usage(),
            retry_classification='unclassified',idempotency_key='node-'+node_id,
            execution_status='planned',checkpoint_refs=[],output_artifacts=[],
            validation_results=[],provenance=[deepcopy(self.input)],failure_reason=None,native_job_ref=None)

    def test_v2_metadata_migration_compiles_without_fabricating_native_execution(self):
        original=deepcopy(self.plan)
        metadata={node['id']:self.planned_metadata(node['id']) for node in self.plan['nodes']}
        self.plan=dg.migrate_plan_v2(self.plan,metadata)
        self.assertEqual(original['schema_version'],1)
        self.assertTrue(all('execution_metadata' not in node for node in original['nodes']))
        self.refresh()
        if not self.require_secure_reads():return
        graph=self.compile()['value']
        self.assertEqual(graph['schema_version'],2)
        for node in graph['nodes']:
            self.assertEqual(node['execution_metadata'],metadata[node['id']])
            self.assertIsNone(node['execution_metadata']['native_job_ref'])
        false_result=self.compile()
        false_result['value']['nodes'][0]['execution_metadata'].update(
            execution_status='running',native_job_ref=deepcopy(self.input))
        with self.assertRaises(RigError):dc.validate_method_result('GraphCompiler.compile',false_result,self.request)
        self.assertEqual(dg.traceability(self.plan,graph,request=self.request,root=self.root,now=self.now)
                         [0]['requirement'],'produce')
        # Ownership/idempotency metadata is part of the dependency fingerprint.
        self.plan['nodes'][0]['execution_metadata']['idempotency_key']='replacement-operation'
        self.refresh()
        changed=self.compile()['value']
        self.assertNotEqual(graph['nodes'][0]['cache_key'],changed['nodes'][0]['cache_key'])
        self.assertNotEqual(graph['nodes'][2]['cache_key'],changed['nodes'][2]['cache_key'])
        self.assertEqual(dg.impact(graph,changed)['stale'],['a','b','c'])

    def test_v2_plan_rejects_incomplete_or_false_executed_metadata(self):
        metadata={node['id']:self.planned_metadata(node['id']) for node in self.plan['nodes']}
        migrated=dg.migrate_plan_v2(self.plan,metadata)
        for key in metadata['a']:
            changed=deepcopy(migrated)
            del changed['nodes'][0]['execution_metadata'][key]
            with self.subTest(missing=key),self.assertRaises(RigError):dg.validate_plan(changed)
        for updates in ({'execution_status':'running','native_job_ref':self.input},
                        {'checkpoint_refs':[self.input]}, {'output_artifacts':[self.input]},
                        {'actual_usage':dict(dg._usage(),classification='actual')}):
            changed=deepcopy(migrated)
            changed['nodes'][0]['execution_metadata'].update(updates)
            with self.subTest(updates=updates),self.assertRaises(RigError):dg.validate_plan(changed)
        with self.assertRaises(RigError):dg.migrate_plan_v2(self.plan,{})
        duplicate=deepcopy(migrated)
        duplicate['nodes'][1]['execution_metadata']['idempotency_key']=metadata['a']['idempotency_key']
        with self.assertRaises(RigError):dg.validate_plan(duplicate)
        legacy=deepcopy(self.plan)
        legacy['nodes'][0]['execution_metadata']=metadata['a']
        with self.assertRaises(RigError):dg.validate_plan(legacy)

    def test_partial_compiler_value_cannot_claim_dispatch_or_actual_outputs(self):
        metadata={node['id']:self.planned_metadata(node['id']) for node in self.plan['nodes']}
        self.plan=dg.migrate_plan_v2(self.plan,metadata)
        self.refresh()
        if not self.require_secure_reads():return
        result=self.compile()
        result.update(status='partial',diagnostics=[dict(code='PARTIAL_FIXTURE',severity='warning',
            stage='graph',entity_id=None,field_path='value',expected=None,observed=None,
            retryable=False,recommended_action='Inspect partial planning diagnostics.',evidence=[])])
        dc.validate_method_result('GraphCompiler.compile',result,self.request)
        execution=result['value']['nodes'][0]['execution_metadata']
        execution.update(execution_status='running',native_job_ref=deepcopy(self.input),
            checkpoint_refs=[deepcopy(self.input)],output_artifacts=[deepcopy(self.input)])
        execution['actual_usage']['classification']='actual'
        execution['actual_usage']['cpu_seconds']['value']=10
        for status in ('partial','succeeded'):
            result['status']=status
            with self.subTest(status=status),self.assertRaises(RigError):
                dc.validate_method_result('GraphCompiler.compile',result,self.request)

    def test_execution_metadata_requires_native_ownership_and_output_validation(self):
        metadata=self.planned_metadata('a')
        metadata['execution_status']='running'
        with self.assertRaises(RigError):dc.validate_execution_metadata(metadata)
        metadata['native_job_ref']=deepcopy(self.input)
        dc.validate_execution_metadata(metadata)
        metadata['execution_status']='validated'
        with self.assertRaises(RigError):dc.validate_execution_metadata(metadata)
        metadata.update(output_artifacts=[deepcopy(self.input)],validation_results=[
            dict(check='synthetic-contract-check',status='passed',evidence=[deepcopy(self.input)],diagnostics=[])])
        dc.validate_execution_metadata(metadata)
        metadata['execution_status']='failed'
        with self.assertRaises(RigError):dc.validate_execution_metadata(metadata)
        metadata['failure_reason']='Synthetic contract failure; no native job ran.'
        dc.validate_execution_metadata(metadata)

    def test_v2_declared_usage_cannot_hide_work_from_budget_admission(self):
        metadata={node['id']:self.planned_metadata(node['id']) for node in self.plan['nodes']}
        self.plan=dg.migrate_plan_v2(self.plan,metadata)
        usage=self.plan['nodes'][0]['execution_metadata']['estimated_usage']
        usage['classification']='estimated'
        usage['cpu_seconds']['value']=101
        self.refresh()
        if not self.require_secure_reads():return
        with self.assertRaisesRegex(RigError,'Declared node usage exceeds'):
            self.compile()

    def test_v2_reconciles_each_node_before_summing(self):
        metadata={node['id']:self.planned_metadata(node['id']) for node in self.plan['nodes']}
        self.plan=dg.migrate_plan_v2(self.plan,metadata)
        self.plan['nodes'][0]['estimated_seconds']=6
        self.plan['nodes'][1]['estimated_seconds']=None
        self.plan['nodes'][2]['estimated_seconds']=0
        usage=self.plan['nodes'][1]['execution_metadata']['estimated_usage']
        usage['classification']='estimated'
        usage['cpu_seconds']['value']=5
        self.request['input']['budget']['max_cpu_seconds']=10
        self.refresh()
        if not self.require_secure_reads():return
        with self.assertRaisesRegex(RigError,'Declared node usage exceeds'):self.compile()
        self.request['input']['budget']['max_cpu_seconds']=11
        self.compile()
        # Two declarations on the same node overlap; they are not added together.
        self.plan['nodes'][0]['execution_metadata']['estimated_usage']['classification']='estimated'
        self.plan['nodes'][0]['execution_metadata']['estimated_usage']['cpu_seconds']['value']=6
        self.refresh()
        self.compile()
        # Different resource dimensions remain independently accountable.
        usage['gpu_seconds']['value']=4
        self.request['input']['budget']['max_gpu_seconds']=3
        self.refresh()
        with self.assertRaisesRegex(RigError,'Declared node usage exceeds'):self.compile()

    def test_v2_resource_totals_use_exact_decimals(self):
        self.plan=dg.migrate_plan_v2(self.plan,{n['id']:self.planned_metadata(n['id']) for n in self.plan['nodes']})
        for node in self.plan['nodes']:
            node['estimated_seconds']=None
            node['execution_metadata']['estimated_usage']['classification']='estimated'
        if not self.require_secure_reads():return
        for metric in ('cpu_seconds','gpu_seconds'):
            for values,limit,accepted in [((.1,.2,0),.3,True),((1,1e-16,0),1,False)]:
                with self.subTest(metric=metric,values=values):
                    for node,value in zip(self.plan['nodes'],values):
                        node['execution_metadata']['estimated_usage'][metric]['value']=value
                    self.request['input']['budget']['max_'+metric]=limit
                    self.refresh()
                    if accepted:
                        self.assertEqual(self.compile()['status'],'succeeded')
                    else:
                        with self.assertRaisesRegex(RigError,'project budget: '+metric):self.compile()
            for node in self.plan['nodes']:
                node['execution_metadata']['estimated_usage'][metric]['value']=0
            self.request['input']['budget']['max_'+metric]=100

    def test_v1_legacy_float_budget_semantics_are_preserved(self):
        for node,value in zip(self.plan['nodes'],(.1,.2,0)):
            node['estimated_seconds']=value
        self.request['input']['budget']['max_cpu_seconds']=.3
        self.refresh()
        if not self.require_secure_reads():return
        with self.assertRaisesRegex(RigError,'Estimated CPU work exceeds budget'):
            self.compile()

    def test_v2_legacy_and_mixed_resource_decimal_boundaries(self):
        self.plan=dg.migrate_plan_v2(self.plan,{n['id']:self.planned_metadata(n['id']) for n in self.plan['nodes']})
        for resource in ('cpu', 'io', 'render', 'gpu'):
            metric='gpu_seconds' if resource == 'gpu' else 'cpu_seconds'
            operation='blender.gpu' if resource == 'gpu' else 'blender.cpu' if resource == 'render' else 'native-cpu'
            limits=deepcopy(self.snapshot['capabilities'][0]['limits'])
            receipt=self.write('boundary-'+operation,dict(operation=operation,status='available',
                execution_verified=True,fixture=True,version='test',policy_limits=limits,payloads=[]),kind='evidence')
            self.snapshot['capabilities']=[dict(operation=operation,version='test',status='available',
                execution_verified=True,limits=limits,probe_evidence=[receipt])]
            self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
            for node in self.plan['nodes']:
                node.update(resource_class=resource,required_capabilities=[operation])
                if resource in ('gpu','render'):
                    node.update(operation='RenderAdapter.submit',native_owner='RenderAdapter',output_schema='job-snapshot')
                else:
                    node.update(operation='FrameEvaluator.evaluate',native_owner='FrameEvaluator',output_schema='evaluated-frame')
            for mixed in (False, True):
                for second,accepted in ((.2,True),(.2000000000000001,False)):
                    with self.subTest(resource=resource,mixed=mixed,second=second):
                        for index,(node,value) in enumerate(zip(self.plan['nodes'],(.1,second,0))):
                            node['estimated_seconds']=None if mixed and index == 1 else value
                            usage=node['execution_metadata']['estimated_usage']
                            usage['classification']='estimated'
                            usage[metric]['value']=value if mixed and index == 1 else None
                        self.request['input']['budget']['max_'+metric]=.3
                        self.refresh()
                        if accepted:
                            self.assertEqual(self.compile()['status'],'succeeded')
                        else:
                            with self.assertRaisesRegex(RigError,'project budget: '+metric):self.compile()
            self.request['input']['budget']['max_'+metric]=100

    def test_v2_integral_float_provider_calls(self):
        self.plan=dg.migrate_plan_v2(self.plan,{n['id']:self.planned_metadata(n['id']) for n in self.plan['nodes']})
        limits=deepcopy(self.request['input']['budget'])
        limits.update(currency='USD',max_amount=10,max_provider_calls=10)
        caps=[]
        for operation,extra in [('native-cpu',{}),('planning.provider',dict(
                price_availability='known',price_per_call=1,currency='USD',provider_id='fixture-provider'))]:
            receipt=self.write('count-'+operation,dict(operation=operation,status='available',
                execution_verified=True,fixture=True,version='test',policy_limits=limits,payloads=[],**extra),kind='evidence')
            caps.append(dict(operation=operation,version='test',status='available',execution_verified=True,
                             limits=deepcopy(limits),probe_evidence=[receipt]))
        self.snapshot['capabilities']=caps
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        node=self.plan['nodes'][0]
        node.update(operation='PlanningProvider.generate_structured',native_owner='PlanningProvider',
                    output_schema='planning-artifact',resource_class='provider',required_capabilities=['planning.provider'])
        self.request['input']['budget']=deepcopy(limits)
        self.request['context']['policy'].update(network='allow-configured',paid='bounded',providers=['fixture-provider'],
            policy_ref=self.write('count-policy',{'fixture':True}))
        self.request['context']['budget_reservation_id']='fixture-reservation'
        usage=node['execution_metadata']['estimated_usage']
        usage['classification']='estimated'
        for count in (2,2.0,1.5):
            with self.subTest(count=count):
                usage['provider_calls']['value']=count
                self.refresh()
                if count == 1.5:
                    with self.assertRaises(RigError):self.compile()
                elif self.require_secure_reads():
                    self.assertEqual(self.compile()['status'],'succeeded')

    def test_v2_typed_usage_obeys_exact_capability_limit(self):
        metadata={node['id']:self.planned_metadata(node['id']) for node in self.plan['nodes']}
        self.plan=dg.migrate_plan_v2(self.plan,metadata)
        usage=self.plan['nodes'][0]['execution_metadata']['estimated_usage']
        usage['classification']='estimated'
        usage['gpu_seconds']['value']=100
        self.refresh()
        if not self.require_secure_reads():return
        self.compile()
        usage['gpu_seconds']['value']=101
        self.request['input']['budget']['max_gpu_seconds']=101
        self.refresh()
        with self.assertRaisesRegex(RigError,'capability bound'):self.compile()

    def test_passing_validation_requires_evidence_in_every_state(self):
        metadata=self.planned_metadata('a')
        metadata.update(execution_status='running',native_job_ref=deepcopy(self.input))
        check=dict(check='native-check',status='passed',evidence=[],diagnostics=[])
        metadata['validation_results']=[check]
        with self.assertRaises(RigError):dc.validate_execution_metadata(metadata)
        check['evidence']=[deepcopy(self.input)]
        dc.validate_execution_metadata(metadata)
        check['diagnostics']=[dict(code='INVALID',severity='error',stage='validation',entity_id=None,
            field_path='validation',expected=None,observed=None,retryable=False,recommended_action='Fix',evidence=[])]
        with self.assertRaises(RigError):dc.validate_execution_metadata(metadata)
        check['status']='failed'
        check['evidence']=[]
        dc.validate_execution_metadata(metadata)
        with self.assertRaises(RigError):dc.validate_execution_metadata(metadata,planned=True)

    def test_actual_compile_topology_exact_edges_and_critical_path(self):
        if not self.require_secure_reads():
            return
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = self.compile()
        self.assertEqual(result['status'], 'succeeded')
        dc.validate_method_result('GraphCompiler.compile', result, self.request)
        self.assertEqual([n['id'] for n in result['value']['nodes']], ['a', 'b', 'c'])
        self.assertEqual(result['value']['critical_path_seconds'], 9)
        self.assertEqual(result['value']['edges'], self.plan['edges'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_equivalent_reordering_preserves_graph_and_cache_keys(self):
        if not self.require_secure_reads():
            return
        original = self.compile()['value']
        self.plan['nodes'].reverse()
        self.plan['edges'].reverse()
        self.refresh()
        self.assertEqual(original, self.compile()['value'])

    def test_branch_critical_path_and_unknown_estimate(self):
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
        self.plan['edges'][0]['artifact'] = deepcopy(self.input)
        self.plan['edges'][0]['artifact']['semantic_version'] = '2.0.0'
        self.refresh()
        with self.assertRaises(ValueError):
            self.compile()

    def test_changed_input_invalidates_node_and_descendants_only(self):
        if not self.require_secure_reads():
            return
        before = self.compile()['value']
        new_input = self.write('replacement', {'new': 'native bytes'})
        self.plan['nodes'][1]['inputs'].append(new_input)
        self.refresh()
        after = self.compile()['value']
        impact = dg.impact(before, after)
        self.assertEqual(impact['preserved'], ['a'])
        self.assertEqual(impact['stale'], ['b', 'c'])

    def test_uncertain_dependency_invalidates_conservatively(self):
        if not self.require_secure_reads():
            return
        graph = self.compile()['value']
        self.assertEqual(dg.impact(graph, graph, uncertain=['a'])['stale'], ['a', 'b', 'c'])
        with self.assertRaises(ValueError):
            dg.impact(graph, graph, uncertain=['absent'])

    def test_tampered_missing_and_symlink_input_fail(self):
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
        self.request['input']['budget']['max_cpu_seconds'] = 8
        with self.assertRaises(ValueError):
            self.compile()

    def test_class_returns_typed_capability_block(self):
        if not self.require_secure_reads():
            return
        self.snapshot['capabilities'] = []
        self.snapshot['digest'] = object_hash({k: v for k, v in self.snapshot.items() if k != 'digest'})
        self.refresh()
        result = dg.GraphCompiler().compile(self.request, self.root, now=self.now)
        self.assertEqual(result['status'], 'blocked_capability')
        self.assertIsNone(result['value'])
        self.assertTrue(result['diagnostics'])
        dc.validate_method_result('GraphCompiler.compile', result, self.request)

    def test_traceability_and_edge_changes_are_in_cache_keys(self):
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
        for node in self.plan['nodes']:
            node['resource_class'] = 'io'
        self.refresh()
        self.request['input']['budget']['max_cpu_seconds'] = 0
        with self.assertRaises(ValueError):
            self.compile()

    def test_provider_calls_are_aggregated_before_admission(self):
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
        # Engineering contract fixture only: no provider execution or payment.
        limits={**deepcopy(self.request['input']['budget']),'max_provider_calls':3,
                'currency':'USD','max_amount':1}
        receipt=self.write('provider-pricing', dict(operation='planning.provider',
                   status='available',execution_verified=True,fixture=True,version='fixture',
                   policy_limits=limits,payloads=[],price_availability='known',
                   price_per_call=.25,currency='USD',provider_id='fixture-provider'),kind='evidence')
        self.snapshot['capabilities']=[dict(operation='planning.provider',version='fixture',
                   status='available',execution_verified=True,limits=limits,probe_evidence=[receipt])]
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        for node in self.plan['nodes']:
            node.update(operation='PlanningProvider.generate_structured',native_owner='PlanningProvider',
                        output_schema='planning-artifact',resource_class='provider',
                        required_capabilities=['planning.provider'])
        self.request['input']['budget']=deepcopy(limits)
        self.request['context']['policy'].update(network='allow-configured',paid='bounded',providers=['fixture-provider'],
                    policy_ref=self.write('paid-policy',{'fixture':True},kind='policy'))
        self.request['context']['budget_reservation_id']='fixture-reservation'
        self.refresh()
        self.assertEqual(self.compile()['status'],'succeeded')
        for providers in ([],['other-provider']):
            self.request['context']['policy']['providers']=providers
            with self.assertRaisesRegex(ValueError,'not permitted'):
                self.compile()
        self.request['context']['policy']['providers']=['fixture-provider']
        payload=json.loads((self.root/receipt['path']).read_text())
        payload.pop('provider_id')
        self.snapshot['capabilities'][0]['probe_evidence']=[self.write('missing-provider',payload,kind='evidence')]
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        self.refresh()
        with self.assertRaisesRegex(ValueError,'provider identity'):
            self.compile()
        self.snapshot['capabilities'][0]['probe_evidence']=[receipt]
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        self.refresh()
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

    def test_v2_monetary_totals_use_exact_decimals(self):
        if not self.require_secure_reads():
            return
        self.plan=dg.migrate_plan_v2(self.plan,{n['id']:self.planned_metadata(n['id']) for n in self.plan['nodes']})
        limits=self.snapshot['capabilities'][0]['limits']
        limits.update(currency='USD',max_amount=1)
        self.snapshot['capabilities'][0]['probe_evidence']=[self.write('decimal-receipt',dict(
            operation='native-cpu',status='available',execution_verified=True,fixture=True,
            policy_limits=limits,version='test',payloads=[]),kind='evidence')]
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        for node, amount in zip(self.plan['nodes'],(.1,.2,0)):
            usage=node['execution_metadata']['estimated_usage']
            usage.update(classification='estimated',currency='USD')
            usage['amount']['value']=amount
        self.request['input']['budget'].update(currency='USD',max_amount=.3)
        self.refresh()
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.compile()['status'],'succeeded')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
        self.request['input']['budget']['max_amount']=.299999999
        with self.assertRaisesRegex(RigError,'project budget: amount'):
            self.compile()

    def test_v2_reconciles_receipt_and_declared_amounts_per_node(self):
        if not self.require_secure_reads():
            return
        self.plan=dg.migrate_plan_v2(self.plan,{n['id']:self.planned_metadata(n['id']) for n in self.plan['nodes']})
        limits=deepcopy(self.request['input']['budget'])
        limits.update(currency='USD',max_amount=10,max_provider_calls=10)
        caps=[]
        for operation,extra in [('native-cpu',{}),('planning.provider',dict(
                price_availability='known',price_per_call=6,currency='USD',provider_id='fixture-provider'))]:
            receipt=self.write('cost-'+operation,dict(operation=operation,status='available',
                execution_verified=True,fixture=True,version='fixture',policy_limits=limits,payloads=[],**extra),kind='evidence')
            caps.append(dict(operation=operation,version='fixture',status='available',execution_verified=True,
                             limits=deepcopy(limits),probe_evidence=[receipt]))
        self.snapshot['capabilities']=caps
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        self.plan['nodes'][0].update(operation='PlanningProvider.generate_structured',native_owner='PlanningProvider',
            output_schema='planning-artifact',resource_class='provider',required_capabilities=['planning.provider'])
        self.request['input']['budget']=deepcopy(limits)
        self.request['context']['policy'].update(network='allow-configured',paid='bounded',providers=['fixture-provider'],
            policy_ref=self.write('cost-policy',{'fixture':True}))
        self.request['context']['budget_reservation_id']='fixture-reservation'
        provider=self.plan['nodes'][0]['execution_metadata']['estimated_usage']
        local=self.plan['nodes'][1]['execution_metadata']['estimated_usage']
        for usage in (provider,local):
            usage.update(classification='estimated',currency='USD')
        provider['amount']['value']=6
        local['amount']['value']=4
        self.refresh()
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.compile()['status'],'succeeded')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
        local['amount']['value']=6
        self.refresh()
        with self.assertRaisesRegex(RigError,'project budget: amount'):
            self.compile()
        local['amount']['value']=4
        provider['amount']['value']=7
        self.refresh()
        with self.assertRaisesRegex(RigError,'project budget: amount'):
            self.compile()
        provider['currency']='EUR'
        self.refresh()
        with self.assertRaisesRegex(RigError,'currency differs'):
            self.compile()
        provider['currency']='USD'
        # Unknown pricing still needs explicit authority; known typed amounts remain bounded.
        receipt_path=self.root/caps[1]['probe_evidence'][0]['path']
        unknown=json.loads(receipt_path.read_text())
        unknown.update(price_availability='unknown',price_per_call=None,currency=None)
        caps[1]['probe_evidence']=[self.write('unknown-cost',unknown,kind='evidence')]
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        self.refresh()
        with self.assertRaisesRegex(RigError,'Unknown provider price'):
            self.compile()
        self.request['context']['policy']['paid']='allow-unknown-price'
        with self.assertRaisesRegex(RigError,'project budget: amount'):
            self.compile()
        provider['amount']['value']=6
        self.refresh()
        self.assertEqual(self.compile()['status'],'succeeded')
        caps[1]['probe_evidence']=[self.write('restored-cost',
            {**unknown,'price_availability':'known','price_per_call':6,'currency':'USD'},kind='evidence')]
        self.snapshot['digest']=object_hash({k:v for k,v in self.snapshot.items() if k!='digest'})
        receipt_path=self.root/caps[1]['probe_evidence'][0]['path']
        receipt_path.write_text(receipt_path.read_text()+' ')
        self.refresh()
        with self.assertRaises(RigError):
            self.compile()

    def test_traceability_rejects_unbound_plan_and_graph(self):
        if not self.require_secure_reads():
            return
        graph = self.compile()['value']
        altered = deepcopy(self.plan)
        altered['requirements'][0]['test'] = 'not-the-compiled-test'
        with self.assertRaises(ValueError):
            dg.traceability(altered, graph, request=self.request, root=self.root, now=self.now)
        graph['nodes'][0]['cache_key'] = 'a' * 64
        with self.assertRaises(ValueError):
            dg.traceability(self.plan, graph, request=self.request, root=self.root, now=self.now)

    def test_concurrent_source_change_fails_before_result(self):
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
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
        if not self.require_secure_reads():
            return
        self.request['input']['budget']['max_cpu_seconds'] = 100
        self.request['context']['capability_snapshot_sha256'] = 'a' * 64
        with self.assertRaises(ValueError):
            self.compile()


if __name__ == '__main__':
    unittest.main()
