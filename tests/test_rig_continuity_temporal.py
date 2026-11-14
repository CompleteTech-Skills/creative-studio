"""Real native Scene3D closure plus exact event replay; synthetic, nonvisual."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from continuity_contracts import digest, validate_document
from continuity_registry import IdentityRegistry
from continuity_temporal import TemporalEngine
from scene3d_contracts import canonical_bytes, load_project
from scene3d_example import build_example, TRANSFORM


def fixture(root, *, dynamic=False, incoming_translation=None):
    build_example(root)
    old_scene = json.loads((root / 'example-scene.json').read_bytes())
    old_shot = json.loads((root / 'example-shot.json').read_bytes())
    project = json.loads((root / 'project.json').read_bytes())
    def write(doc):
        data = canonical_bytes(doc)
        path = doc['id'] + '.json'
        (root / path).write_bytes(data)
        return dict(id=doc['id'], version=doc['version'], kind=doc['kind'], path=path,
                    sha256=hashlib.sha256(data).hexdigest())
    scenes, shots = [], []
    for i in range(2):
        scene = deepcopy(old_scene)
        scene.update(id='location-' + str(i), shots=['shot-0', 'shot-1'] if i == 0 else ['shot-2'])
        # Two character stand-ins and a separate persistent prop in a real native
        # hierarchy; temporal IDs are explicitly mapped, never guessed by name.
        for identifier in ('second-character', 'prop'):
            node = deepcopy(scene['nodes'][1]); node['id'] = identifier
            scene['nodes'].append(node)
        if i == 1 and incoming_translation is not None:
            scene['nodes'][1]['transform']['translation'] = list(incoming_translation)
        scenes.append(write(scene))
    for i in range(3):
        shot = deepcopy(old_shot)
        shot.update(id='shot-' + str(i), scene=scenes[0 if i < 2 else 1], duration_frames=4,
                    fps=dict(numerator=24000, denominator=1001))
        if dynamic and i == 0:
            shot['tracks'] = [dict(target='mesh', property='translation', interpolation='linear',
                keys=[dict(frame=0,value=[2,0,0]),dict(frame=4,value=[3,0,0])])]
        shots.append(write(shot))
    project.update(scenes=scenes, shots=shots, prefabs=[], animations=[])
    project['dependency_lock'] = [project['assets'][0], *scenes, *shots]
    project_ref = write(project)
    load_project(root)
    plan = dict(schema_version='1.0.0', kind='continuity-temporal-timeline', timeline_id='main',
        branch_id='main', project_id='production', project_revision=project_ref, world_id='world',
        ticks_per_second=24000,
        scenes=[dict(scene=s, scene_to_world=deepcopy(TRANSFORM),
            entities=[dict(entity_id=entity, node_id=node, identity_id=None)
                for entity,node in [('aster','mesh'),('moss','second-character'),('key','prop')]]) for s in scenes],
        shots=[dict(shot=s, scene_id=scenes[0 if i < 2 else 1]['id'],
                    scene_start_tick=4004 if i == 1 else 0, presentation_start_tick=i*4004,
                    story_start_tick=[0,4004,1001][i], story_speed=dict(numerator=1, denominator=1)) for i,s in enumerate(shots)],
        dependencies=[])
    return plan


def event(name, tick, *, kind='set-fact', target='discovered', before=None, after=True, priority=0, prerequisites=None):
    operation = dict(event_type=kind, before=before, after=after)
    operation['fact_id' if kind == 'set-fact' else 'entity_id'] = target
    return dict(schema_version='1.0.0', kind='continuity-temporal-event', event_id=name,
        tick=tick, priority=priority, prerequisites=prerequisites or [], operations=[operation],
        sources=[], author='synthetic temporal test', revision=1)


class TemporalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'native'
        self.plan = fixture(self.root)
        self.registry = IdentityRegistry(Path(self.temp.name) / 'continuity.sqlite', asset_root=self.root)
        self.addCleanup(self.registry.close)
        self.initial = self.registry.create_timeline(self.plan)

    def append(self, events, timeline='main', head=None):
        return self.registry.append_events(timeline, events,
            expected_sha256=head or self.registry.temporal_history(timeline)['sha256'])

    def snap(self, shot='shot-0', frame=0, **kw):
        return self.registry.temporal_snapshot('main', shot, frame, **kw)

    def test_shot_binds_exact_selected_scene_version(self):
        def write(document, name=None):
            data = canonical_bytes(document)
            name = name or document['id'] + '.json'
            (self.root/name).write_bytes(data)
            return dict(id=document['id'], version=document['version'], kind=document['kind'],
                        path=name, sha256=hashlib.sha256(data).hexdigest())
        project = json.loads((self.root/'project.json').read_bytes())
        original = json.loads((self.root/'location-0.json').read_bytes())
        newer = deepcopy(original)
        newer.update(version='2.0.0', shots=['shot-0'])
        newer['nodes'][1]['transform']['translation'] = [99,0,0]
        newer_ref = write(newer, 'location-0-v2.json')
        original['shots'] = ['shot-1']
        original_ref = write(original)
        shot0 = json.loads((self.root/'shot-0.json').read_bytes())
        shot0['scene'] = newer_ref
        shot0_ref = write(shot0)
        shot1 = json.loads((self.root/'shot-1.json').read_bytes())
        shot1['scene'] = original_ref
        shot1_ref = write(shot1)
        project.update(scenes=[original_ref, project['scenes'][1], newer_ref],
                       shots=[shot0_ref, shot1_ref, project['shots'][2]])
        project['dependency_lock'] = [*project['assets'], *project['scenes'], *project['shots']]
        self.plan['project_revision'] = write(project)
        self.plan['scenes'][0]['scene'] = original_ref
        self.plan['shots'][0]['shot'] = shot0_ref
        self.plan['shots'][1]['shot'] = shot1_ref
        load_project(self.root)  # Both exact versions are legitimate native inputs.
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_MAPPING'):
            TemporalEngine(self.root, dict(plan=self.plan, events=[]))
        self.plan['scenes'][0]['scene'] = newer_ref
        self.plan['scenes'] = [self.plan['scenes'][0]]
        self.plan['shots'] = [self.plan['shots'][0]]
        engine = TemporalEngine(self.root, dict(plan=self.plan, events=[]))
        snapshot = engine.snapshot('shot-0', 0, head_sha256='0'*64)
        entity = next(e for e in snapshot['entities'] if e['entity_id'] == 'aster')
        self.assertEqual(entity['transform']['translation'], [99,0,0])

    def test_out_of_order_exact_before_after_terminal_and_flashback(self):
        first = event('discover',1001)
        transform = deepcopy(TRANSFORM); transform['translation'] = [7,0,0]
        before = deepcopy(TRANSFORM); before['translation'] = [2,0,0]
        second = event('move',4004,kind='relocate',target='aster',before=before,after=transform,prerequisites=['discover'])
        head = self.append([first,second])
        queries = [('shot-0',4,'terminal'),('shot-0',1,'after'),('shot-2',0,'before'),
                   ('shot-0',0,'after'),('shot-1',0,'after'),('shot-0',3,'after')]
        cold = [self.snap(s,f,boundary=b,use_cache=False) for s,f,b in queries]
        for i in [3,1,5,0,2,4,0,1]:
            self.assertEqual(cold[i],self.snap(queries[i][0],queries[i][1],boundary=queries[i][2]))
        self.assertEqual(cold[0]['snapshot']['entities'][0]['transform']['translation'],[7,0,0])
        self.assertEqual(cold[-1]['snapshot']['entities'][0]['transform']['translation'],[2.,0.,0.])
        self.assertEqual(cold[1]['snapshot']['applied_events'],['discover'])
        self.assertEqual(cold[2]['snapshot']['applied_events'],[])
        self.assertEqual(cold[2]['snapshot']['presentation_tick'],8008)
        self.assertEqual(cold[2]['snapshot']['story_tick'],1001)
        self.assertEqual(cold[0]['snapshot']['entities'],cold[4]['snapshot']['entities'])
        self.assertEqual(self.registry.temporal_history('main',revision=self.initial)['events'],[])
        self.assertEqual(self.registry.temporal_history('main')['sha256'],head)

    def test_native_project_nested_under_asset_root(self):
        # Native internal pointers stay project-local; only plan pointers gain
        # the path from the continuity asset root to that project directory.
        asset_root = Path(self.temp.name)
        plan = deepcopy(self.plan)
        plan['project_revision']['path'] = 'native/' + plan['project_revision']['path']
        for section, field in (('scenes','scene'),('shots','shot')):
            for entry in plan[section]:
                entry[field]['path'] = 'native/' + entry[field]['path']
        engine = TemporalEngine(asset_root, dict(plan=plan, events=[]))
        snapshot = engine.snapshot('shot-0',0,head_sha256='0'*64)
        self.assertEqual(len(snapshot['entities']),3)
        # An equal-ID source at a different asset-root path is not membership.
        plan['scenes'][0]['scene']['path'] = 'other/' + Path(plan['scenes'][0]['scene']['path']).name
        other = asset_root/'other'; other.mkdir()
        (other/'location-0.json').write_bytes((self.root/'location-0.json').read_bytes())
        with self.assertRaisesRegex(ValueError,'scene is not in native project'):
            TemporalEngine(asset_root, dict(plan=plan, events=[]))

    def test_scene_interval_derived_end_stays_exact_at_safe_integer_boundary(self):
        plan = deepcopy(self.plan)
        plan['timeline_id'] = 'scene-boundary'
        plan['shots'][2]['scene_start_tick'] = 2**53-1
        with self.assertRaisesRegex(ValueError, 'TIME.*scene end'):
            self.registry.create_timeline(plan)
        # Reject before persistence, then allow a mapping whose terminal tick
        # is exactly the largest supported JSON integer.
        plan['shots'][2]['scene_start_tick'] -= 4004
        self.registry.create_timeline(plan)
        terminal = self.registry.temporal_snapshot('scene-boundary','shot-2',4,boundary='terminal')
        self.assertEqual(terminal['snapshot']['scene_tick'],2**53-1)

    def test_event_specific_order_at_same_tick(self):
        self.append([event('a',1001),event('b',1001,before=True,after=False,priority=1,prerequisites=['a'])])
        self.assertEqual(self.snap(frame=1,boundary='before')['snapshot']['applied_events'],[])
        self.assertEqual(self.snap(frame=1,boundary='after')['snapshot']['applied_events'],['a','b'])
        self.assertEqual(self.snap(frame=1,boundary='before',event_id='b')['snapshot']['applied_events'],['a'])
        self.assertEqual(self.snap(frame=1,boundary='after',event_id='a')['snapshot']['applied_events'],['a'])

    def test_invalid_order_duplicates_preconditions_and_atomic_batch(self):
        for events, code in [([event('b',2002),event('a',1001)],'ORDER'),
            ([event('a',1001),event('a',2002)],'DUPLICATE'),
            ([event('a',1001,prerequisites=['missing'])],'PRECONDITION'),
            ([event('a',1001,before=True)],'PRECONDITION'),
            ([event('a',1001,prerequisites=['b']),event('b',2002)],'PRECONDITION'),
            ([event('a',99999)],'MAPPING')]:
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,code):self.append(events)
            self.assertEqual(self.registry.temporal_history('main')['sha256'],self.initial)
        a=event('a',1001);a['operations']*=2
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.append([a])
        self.append([event('a',1001)])
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.append([event('a',1001)])
        with self.assertRaisesRegex(ValueError,'CONFLICT'):
            self.append([event('b',2002,target='other')],head=self.initial)

    def test_invalid_mapping_and_terminal_queries(self):
        mutations = [lambda p:p['shots'][0].update(story_start_tick=1),
            lambda p:p['shots'][0].update(scene_id='missing'),
            lambda p:p.update(ticks_per_second=24),
            lambda p:p['shots'][1].update(presentation_start_tick=1),
            lambda p:p['shots'][1].update(scene_start_tick=1),
            lambda p:p['scenes'][1]['entities'].pop()]
        for modify in mutations:
            p=deepcopy(self.plan);p['timeline_id']='invalid';modify(p)
            with self.assertRaisesRegex(ValueError,'MAPPING'):self.registry.create_timeline(p)
        for frame,boundary in [(4,'after'),(3,'terminal'),(5,'terminal'),(-1,'after'),(True,'after')]:
            with self.subTest(frame=frame,boundary=boundary),self.assertRaises(ValueError):self.snap(frame=frame,boundary=boundary)
        with self.assertRaisesRegex(ValueError,'MAPPING'):self.snap(shot='unknown')

    def test_branch_disjoint_merge_conflict_and_immutable_source(self):
        left = self.registry.fork_timeline('main','left','alternative',expected_sha256=self.initial)
        right = self.registry.fork_timeline('main','right','parallel',expected_sha256=self.initial)
        left = self.append([event('a',1001,target='left')],timeline='left',head=left)
        right = self.append([event('b',2002,target='right')],timeline='right',head=right)
        merged = self.registry.merge_timeline('left','right',expected_sha256=left,source_sha256=right)
        self.assertNotEqual(merged,left)
        self.assertEqual(len(self.registry.temporal_history('left')['events']),2)
        self.assertEqual(len(self.registry.temporal_history('right')['events']),1)
        self.assertEqual(self.registry.temporal_history('left',revision=left)['events'][0]['event_id'],'a')
        fork = self.registry.fork_timeline('main','conflict','conflict',expected_sha256=self.initial)
        fork = self.append([event('c',3003,target='left')],timeline='conflict',head=fork)
        with self.assertRaisesRegex(ValueError,'CONFLICT'):
            self.registry.merge_timeline('left','conflict',expected_sha256=merged,source_sha256=fork)
        self.assertEqual(self.registry.temporal_history('left')['sha256'],merged)
        with self.assertRaisesRegex(ValueError,'CONFLICT'):
            self.registry.merge_timeline('left','right',expected_sha256=left,source_sha256=right)

    def test_stale_cache_native_mutation_recovery_and_restart(self):
        expected=self.snap()
        self.registry.db.execute("UPDATE temporal_cache SET document=?",(b'{}',))
        with self.assertRaisesRegex(ValueError,'STALE_CACHE'):self.snap()
        self.registry.discard_temporal_cache()
        self.assertEqual(expected,self.snap())
        # This new handle proves all state/event history survives process restart.
        with IdentityRegistry(self.registry.path,asset_root=self.root) as reopened:
            self.assertEqual(expected,reopened.temporal_snapshot('main','shot-0',0))
        path=self.root/'triangle.obj'; original=path.read_bytes();path.write_bytes(original+b'# changed\n')
        with self.assertRaises(ValueError):self.snap()
        path.write_bytes(original)
        self.assertEqual(expected,self.snap())
        self.registry.db.execute('UPDATE temporal_contents SET document=? WHERE hash=?',(b'{}',self.initial))
        with self.assertRaisesRegex(ValueError,'HASH'):self.snap()

    def test_strict_typed_operations_and_normalized_transform(self):
        invalid=event('bad',1001);invalid['operations'][0]['path']='arbitrary/path'
        with self.assertRaises(ValueError):self.append([invalid])
        invalid=event('bad',1001,kind='relocate',target='aster',before=deepcopy(TRANSFORM),after=deepcopy(TRANSFORM))
        invalid['operations'][0]['after']['rotation']=[0,0,0,0]
        with self.assertRaisesRegex(ValueError,'TRANSFORM'):self.append([invalid])
        self.append([event('bool',1001,after=1)])
        with self.assertRaisesRegex(ValueError,'PRECONDITION'):
            self.append([event('wrong',2002,before=True)])

    def test_rates_and_explicit_time_skip(self):
        # Shared native helper gives exact representability for all required rates.
        from scene3d_contracts import frame_time
        from fractions import Fraction
        for n,d in [(24,1),(25,1),(30,1),(60,1),(24000,1001),(30000,1001)]:
            self.assertEqual(frame_time(1000000,dict(numerator=n,denominator=d)),Fraction(1000000*d,n))
        p=deepcopy(self.plan);p['timeline_id']='skip';p['shots'][1]['story_start_tick']=240000
        self.registry.create_timeline(p)
        self.registry.append_events('skip',[event('offscreen',100000)],expected_sha256=self.registry.temporal_history('skip')['sha256'])
        self.assertEqual(self.registry.temporal_snapshot('skip','shot-1',0)['snapshot']['applied_events'],['offscreen'])
        before=self.registry.temporal_event_snapshot('skip','offscreen',boundary='before')['snapshot']
        after=self.registry.temporal_event_snapshot('skip','offscreen',boundary='after')['snapshot']
        self.assertEqual(before['applied_events'],[])
        self.assertEqual(after['applied_events'],['offscreen'])
        self.assertIsNone(after['frame'])
        self.assertIsNone(after['presentation_tick'])
        self.assertEqual(after['story_tick'],100000)

    def test_incoming_native_scene_cannot_silently_override_replay(self):
        p=deepcopy(self.plan);p['timeline_id']='contradiction'
        p['scenes'][1]['scene_to_world']['translation']=[10,0,0]
        with self.assertRaisesRegex(ValueError,'SOURCE_STATE'):
            self.registry.create_timeline(p)

    def test_atomic_initial_events_author_changed_native_scene_and_cli(self):
        root = Path(self.temp.name)/'arrival-native'
        plan = fixture(root,incoming_translation=[7,0,0])
        plan['shots'][2]['story_start_tick'] = 4004
        before = deepcopy(TRANSFORM); before['translation'] = [2,0,0]
        after = deepcopy(TRANSFORM); after['translation'] = [7,0,0]
        arrival = event('arrive',4004,kind='relocate',target='aster',before=before,after=after)
        database = Path(self.temp.name)/'arrival.sqlite'
        with IdentityRegistry(database,asset_root=root) as registry:
            with self.assertRaisesRegex(ValueError,'SOURCE_STATE'):
                registry.create_timeline(plan)
            invalid = deepcopy(arrival); invalid['operations'][0]['before']['translation'] = [99,0,0]
            with self.assertRaisesRegex(ValueError,'PRECONDITION'):
                registry.create_timeline(plan,events=[invalid])
            head = registry.create_timeline(plan,events=[arrival])
            self.assertEqual(registry.temporal_history('main')['events'],[arrival])
            self.assertEqual(registry.temporal_history('main')['sha256'],head)
            previous = registry.temporal_snapshot('main','shot-0',3)['snapshot']
            terminal = registry.temporal_snapshot('main','shot-0',4,boundary='terminal')['snapshot']
            incoming = registry.temporal_snapshot('main','shot-2',0)['snapshot']
            self.assertEqual(previous['entities'][0]['transform']['translation'],[2.,0.,0.])
            self.assertEqual(incoming['entities'][0]['transform']['translation'],[7.,0.,0.])
            self.assertEqual(terminal['entities'],incoming['entities'])
            self.assertEqual(incoming['applied_events'],['arrive'])
        plan['timeline_id'] = 'cli-arrival'
        from continuity_contracts import canonical_bytes as continuity_bytes
        plan_file = Path(self.temp.name)/'arrival-plan.json'; plan_file.write_bytes(continuity_bytes(plan))
        events_file = Path(self.temp.name)/'arrival-events.json'; events_file.write_bytes(continuity_bytes([arrival]))
        base = [sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/continuity_temporal.py'),
            '--registry',str(database),'--asset-root',str(root)]
        process = subprocess.run(base+['create',str(plan_file),'--events',str(events_file)],capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        with IdentityRegistry(database,asset_root=root) as registry:
            self.assertEqual(registry.temporal_history('cli-arrival')['events'],[arrival])

    def test_native_dynamic_source_fails_closed(self):
        plan = fixture(Path(self.temp.name) / 'dynamic', dynamic=True)
        with IdentityRegistry(Path(self.temp.name) / 'dynamic.sqlite', asset_root=Path(self.temp.name) / 'dynamic') as registry:
            with self.assertRaisesRegex(ValueError, 'ADAPTER_REQUIRED'):
                registry.create_timeline(plan)

    def test_scene_boundary_native_world_mapping_and_cli(self):
        start = self.registry.temporal_scene_boundary('main','location-0','start')
        end = self.registry.temporal_scene_boundary('main','location-0','end')
        self.assertEqual(start['snapshot']['scene_tick'],0)
        self.assertEqual(end['snapshot']['scene_tick'],8008)
        self.assertEqual(end['snapshot']['frame'],4)
        p=deepcopy(self.plan);p['timeline_id']='mapped'
        for scene in p['scenes']:
            scene['scene_to_world']['translation']=[10,0,0]
        self.registry.create_timeline(p)
        self.assertEqual(self.registry.temporal_snapshot('mapped','shot-0',0)['snapshot']['entities'][0]['transform']['translation'],[12.,0.,0.])
        base=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/continuity_temporal.py'),
            '--registry',str(self.registry.path),'--asset-root',str(self.root)]
        command=base+['snapshot','main','shot-0','4','--boundary','terminal']
        process=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(json.loads(process.stdout)['snapshot']['story_tick'],4004)
        process=subprocess.run(base+['snapshot','main','shot-0','4'],capture_output=True,text=True)
        self.assertEqual(process.returncode,1)
        self.assertIn('TIME',json.loads(process.stdout)['error'])

    def test_schema_roundtrip(self):
        from jsonschema import Draft202012Validator
        from continuity_contracts import temporal_schemas
        for kind,value in [('temporal-timeline',self.plan),('temporal-event',event('a',1001)),
                           ('temporal-snapshot',self.snap()['snapshot'])]:
            Draft202012Validator(temporal_schemas()[kind]).validate(value)
            self.assertEqual(value,validate_document(value,kind))

if __name__=='__main__': unittest.main()
