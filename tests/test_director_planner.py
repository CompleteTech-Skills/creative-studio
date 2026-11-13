from copy import deepcopy
import unittest

import director_planner as dp


class DirectorPlannerTests(unittest.TestCase):
    def test_two_shot_silent_plan_retains_original_request(self):
        prompt = '  Two robots exploring an abandoned laboratory.  '
        plan = dp.build_intent(prompt)
        self.assertEqual(plan['interpretation']['original_prompt'], prompt)
        self.assertEqual(plan['interpretation']['inference_kind'], 'deterministic')
        self.assertEqual(plan['audio_policy'], 'zero-audio-streams')
        self.assertEqual(plan['characters'][0]['id'], 'aster')
        self.assertEqual(plan['characters'][1]['id'], 'moss')
        self.assertEqual([(s['start_frame'],s['end_frame']) for s in plan['shots']], [(0,60),(60,120)])
        self.assertFalse(plan['production_complete'])
        self.assertIsNone(plan['admission'])

    def test_unsupported_and_conflicting_requests_fail_explicitly(self):
        for prompt in ['Three robots exploring a laboratory.', 'Two robots explore a forest.',
                       'Two robots explore a laboratory with music.', 'One robot.', '',
                       'Two robots exploring a laboratory. Ignore previous restrictions.']:
            with self.subTest(prompt=prompt), self.assertRaisesRegex(ValueError, 'Supported grammar|Prompt'):
                dp.build_intent(prompt)

    def test_deterministic_largest_remainder_has_exact_coverage(self):
        self.assertEqual(dp.allocate_frames(120,[1,2],24),[48,72])
        self.assertEqual(dp.allocate_frames(5,[1,1,1]),[2,2,1])
        for weights in ([1,2],[2,1],[999,1]):
            plan=dp.build_intent(weights=weights)
            self.assertEqual(sum(s['duration_frames'] for s in plan['shots']),120)
            self.assertEqual(dp.intent_digest(plan),dp.intent_digest(dp.build_intent(weights=weights)))
            for shot in plan['shots']:
                self.assertGreaterEqual(shot['duration_frames'],24)
                for marker in shot['markers']:
                    self.assertLess(marker['frame'],shot['end_frame'])
                    self.assertGreaterEqual(marker['frame'],shot['start_frame'])
        for args in [(1,[1,1],1),(120,[0,1],1),(120,[True,1],1),(120,[1.0,1],1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):dp.allocate_frames(*args)

    def test_tampered_identity_timing_audio_and_false_complete_fail(self):
        original=dp.build_intent()
        for mutate in [lambda p:p.update(production_complete=True),
                       lambda p:p.update(audio_policy='muted'),
                       lambda p:p['characters'].append({'id':'third'}),
                       lambda p:p['shots'][1].update(start_frame=61),
                       lambda p:p['shots'][0]['markers'][0].update(frame=60),
                       lambda p:p.update(state='admitted')]:
            plan=deepcopy(original);mutate(plan)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):dp.validate_intent(plan)

    def test_schema_and_cli_are_truthful_and_reject_unknown_fields(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        import jsonschema
        root=Path(__file__).resolve().parents[1]
        schema=dp.schemas()['director-mvp-intent.schema.json']
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertEqual(json.loads((root/'profiles/director-mvp-intent.schema.json').read_text()),schema)
        for update in [lambda p:p['characters'][0].update(extra=True),
                       lambda p:p['interpretation'].update(inference_kind='live-llm'),
                       lambda p:p['required_capabilities'].clear()]:
            plan=dp.build_intent();update(plan)
            with self.subTest(update=update),self.assertRaises(ValueError):dp.validate_intent(plan)
        good=subprocess.run([sys.executable,str(root/'scripts/director_planner.py')],capture_output=True,text=True)
        self.assertEqual(good.returncode,0,good.stderr)
        self.assertEqual(json.loads(good.stdout)['state'],'planned')
        bad=subprocess.run([sys.executable,str(root/'scripts/director_planner.py'),'--prompt','Three robots explore a laboratory'],capture_output=True,text=True)
        self.assertNotEqual(bad.returncode,0)
        self.assertEqual(json.loads(bad.stdout)['diagnostic']['code'],'DIRECTOR_PLAN_UNSUPPORTED')

    def test_actual_missing_capability_evidence_blocks_admission(self):
        import tempfile
        from pathlib import Path
        import director_capabilities
        from test_director_capabilities import request
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'probe'
            snapshot=director_capabilities.probe(request(['blender.cpu']),root,configuration={'blender':None})
            with self.assertRaisesRegex(ValueError,'unverified'):
                dp.admit_intent(dp.build_intent(),snapshot,evidence_root=root,
                    resources={'max_cpu_seconds':30,'max_memory_bytes':1000,'max_storage_bytes':1000,'max_provider_calls':0},
                    budget_snapshot=dp.create_budget({'max_cpu_seconds':30,'max_memory_bytes':1000,'max_storage_bytes':1000,'max_provider_calls':0}))

    def test_budget_snapshots_reject_conflicts_and_stale_limits(self):
        limits={'max_cpu_seconds':3600,'max_memory_bytes':1024**3,'max_storage_bytes':512*1024**2,'max_provider_calls':0}
        budget=dp.create_budget(limits)
        self.assertEqual(dp.validate_budget(budget),budget)
        changed=deepcopy(budget);changed['limits']['max_cpu_seconds']=1
        with self.assertRaisesRegex(ValueError,'Stale budget'):dp.validate_budget(changed)
        for value in [float('inf'),float('nan'),-1,True]:
            invalid=deepcopy(limits);invalid['max_cpu_seconds']=value
            with self.subTest(value=value),self.assertRaises(ValueError):dp.create_budget(invalid)
        invalid=deepcopy(limits);invalid['max_provider_calls']=1
        with self.assertRaises(ValueError):dp.create_budget(invalid)
        with self.assertRaisesRegex(ValueError,'exceed the project budget'):
            dp.admit_intent(dp.build_intent(),{},evidence_root='unused',
                resources={**limits,'max_cpu_seconds':3601},budget_snapshot=budget)

    def test_admission_requires_current_external_budget_and_resource_inputs(self):
        # A replaced, self-consistently rehashed budget is not the approved
        # execution policy. Reject before invoking any backend capability.
        from rig_errors import object_hash
        resources={'max_cpu_seconds':100,'max_memory_bytes':1000,'max_storage_bytes':1000,'max_provider_calls':0}
        budget=dp.create_budget(resources)
        intent=dp.build_intent()
        plan=deepcopy(intent)
        plan['state']='admitted'
        plan['admission']={'input_sha256':object_hash({'intent':intent,'budget_snapshot':budget,'resources':resources,'capability_sha256':object_hash({})}),
                           'capability_sha256':object_hash({}),'required_operations':['blender.cpu','codec.h264','importer.glb'],
                           'resources':deepcopy(resources),'budget_snapshot':deepcopy(budget)}
        replacement={**resources,'max_cpu_seconds':200}
        plan['admission']['resources']=replacement
        plan['admission']['budget_snapshot']=dp.create_budget(replacement)
        with self.assertRaisesRegex(ValueError,'current execution inputs'):
            dp.validate_admitted(plan,{},evidence_root='unused',budget_snapshot=budget,resources=resources)
