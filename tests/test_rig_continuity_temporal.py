"""Real native Scene3D closure plus exact event replay; synthetic, nonvisual."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from continuity_contracts import canonical_bytes as continuity_bytes, digest, validate_document
from continuity_registry import IdentityRegistry
from continuity_temporal import TemporalEngine
from continuity_test_helpers import review_for, sample, write_assets
from scene3d_contracts import canonical_bytes, load_project
from scene3d_example import build_example, TRANSFORM


def fixture(root, *, dynamic=False, incoming_translation=None, node_scale=None,
            camera_override=None, camera_parent=False, configure_scene=None):
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
        if node_scale is not None:
            scene['nodes'][1]['transform']['scale'] = [node_scale] * 3
        if camera_parent:
            scene['nodes'][1]['parent'] = 'camera'
        if i == 1 and incoming_translation is not None:
            scene['nodes'][1]['transform']['translation'] = list(incoming_translation)
        if configure_scene is not None:
            configure_scene(scene, i)
        scenes.append(write(scene))
    for i in range(3):
        shot = deepcopy(old_shot)
        shot.update(id='shot-' + str(i), scene=scenes[0 if i < 2 else 1], duration_frames=4,
                    fps=dict(numerator=24000, denominator=1001))
        if dynamic and i == 0:
            shot['tracks'] = [dict(target='mesh', property='translation', interpolation='linear',
                keys=[dict(frame=0,value=[2,0,0]),dict(frame=4,value=[3,0,0])])]
        if camera_override is not None and i == 0:
            shot['camera_overrides']['transform'] = camera_override
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

    def test_timeline_visibility_matches_native_render_membership(self):
        from scene3d_graph import SceneGraph

        def memberships(scene, index):
            nodes = {node['id']: node for node in scene['nodes']}
            scene['collections'].append(dict(id='hidden', name='Hidden', visible=False))
            nodes['mesh']['collections'] = ['hidden']
            nodes['second-character']['parent'] = 'mesh'
            scene['layers'].append(dict(id='render-only', name='Render only', visible=True,
                                        editor_visible=False, render_visible=True))
            nodes['prop']['layers'] = ['render-only']

        root = Path(self.temp.name) / 'visibility'
        plan = fixture(root, configure_scene=memberships)
        scene = json.loads((root / 'location-0.json').read_bytes())
        native = SceneGraph(scene).evaluate_base()
        self.assertFalse(native['prop']['editor_visible'])
        self.assertTrue(native['prop']['render_visible'])
        with IdentityRegistry(Path(self.temp.name) / 'visibility.sqlite', asset_root=root) as registry:
            registry.create_timeline(plan)
            snapshot = registry.temporal_snapshot('main', 'shot-0', 0)['snapshot']
            visible = {entity['entity_id']: entity['visible'] for entity in snapshot['entities']}
            self.assertEqual(visible, {'aster': False, 'moss': False, 'key': True})
            for binding in plan['scenes'][0]['entities']:
                self.assertEqual(visible[binding['entity_id']], native[binding['node_id']]['render_visible'])

    def test_authored_relocation_matches_native_rotation_and_preserves_bytes(self):
        yaw = [0.0, math.sin(0.25), 0.0, math.cos(0.25)]

        def orientation(scene, index):
            if index == 1:
                scene['nodes'][1]['transform']['rotation'] = yaw.copy()

        root = Path(self.temp.name) / 'yaw'
        plan = fixture(root, configure_scene=orientation)
        source_file = root / 'location-1.json'
        source_bytes = source_file.read_bytes()
        authored = json.loads(source_bytes)['nodes'][1]['transform']
        before = dict(TRANSFORM, translation=[2, 0, 0])
        arrival = event('turn', 1001, kind='relocate', target='aster', before=before, after=authored)
        opposite = dict(authored, rotation=[-value for value in yaw])
        reset = event('reset', 2002, kind='relocate', target='aster', before=opposite, after=before)
        events = [arrival, reset, event('decimal', 3003, after=0.5)]
        event_bytes = continuity_bytes(events)
        with IdentityRegistry(Path(self.temp.name) / 'yaw.sqlite', asset_root=root) as registry:
            for field, value in (
                    ('rotation', [0.0, -yaw[1], 0.0, yaw[3]]),
                    ('rotation', [0.0, math.sin(0.25 + 1e-10), 0.0, math.cos(0.25 + 1e-10)]),
                    ('scale', [1.0 + 1e-10, 1.0, 1.0]),
                    ('translation', [2.0 + 1e-10, 0.0, 0.0])):
                wrong = deepcopy(arrival)
                wrong['operations'][0]['after'][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'SOURCE_STATE'):
                    registry.create_timeline(plan, events=[wrong])
            head = registry.create_timeline(plan, events=[arrival])
            head = registry.append_events('main', events[1:], expected_sha256=head)
            incoming = registry.temporal_snapshot('main', 'shot-2', 0)['snapshot']
            self.assertEqual(continuity_bytes(incoming['entities'][0]['transform']), continuity_bytes(authored))
            self.assertEqual(continuity_bytes(registry.temporal_history('main')['events']), event_bytes)
            self.assertEqual(continuity_bytes(events), event_bytes)
            self.assertEqual(source_file.read_bytes(), source_bytes)
            wrong_fact = event('wrong-fact', 4004, before=math.nextafter(0.5, 1))
            with self.assertRaisesRegex(ValueError, 'PRECONDITION'):
                registry.append_events('main', [wrong_fact], expected_sha256=head)
            self.assertEqual(registry.temporal_history('main')['sha256'], head)

    def test_native_cancellation_agrees_with_authored_origin(self):
        def configure(scene, index):
            nodes = {node['id']: node for node in scene['nodes']}
            nodes['camera']['parent'] = None
            nodes['camera']['transform'] = dict(TRANSFORM, translation=[0.1, 0.0, 0.0])
            nodes['mesh']['parent'] = 'camera' if index == 0 else None
            position = 0.2 if index == 0 else 0.3
            nodes['mesh']['transform'] = dict(TRANSFORM, translation=[position, 0.0, 0.0])
            scene['roots'] = [node['id'] for node in scene['nodes'] if node['parent'] is None]

        root = Path(self.temp.name) / 'cancellation'
        plan = fixture(root, configure_scene=configure)
        for scene in plan['scenes']:
            scene['scene_to_world']['translation'] = [-0.3, 0.0, 0.0]
        after = dict(TRANSFORM, translation=[3.0, 0.0, 0.0])
        move = event('cancellation-origin', 2002, kind='relocate', target='aster',
                     before=TRANSFORM, after=after)
        with IdentityRegistry(Path(self.temp.name) / 'cancellation.sqlite', asset_root=root) as registry:
            head = registry.create_timeline(plan)
            before = registry.temporal_snapshot('main', 'shot-0', 2, boundary='before')['snapshot']
            residual = before['entities'][0]['transform']['translation'][0]
            self.assertNotEqual(residual, 0.0)
            self.assertLess(abs(residual), 1e-15)
            registry.append_events('main', [move], expected_sha256=head)
            observed = registry.temporal_snapshot('main', 'shot-0', 2)['snapshot']
            self.assertEqual(observed['entities'][0]['transform'], after)

    def test_native_cancellation_bound_scales_with_cancelled_magnitudes(self):
        camera, child, total = 1000.1, 1000.2, 2000.3

        def configure(scene, index):
            nodes = {node['id']: node for node in scene['nodes']}
            nodes['camera']['parent'] = None
            nodes['camera']['transform'] = dict(TRANSFORM, translation=[camera, 0.0, 0.0])
            nodes['mesh']['parent'] = 'camera' if index == 0 else None
            nodes['mesh']['transform'] = dict(TRANSFORM, translation=[child if index == 0 else total, 0.0, 0.0])
            scene['roots'] = [node['id'] for node in scene['nodes'] if node['parent'] is None]

        def plan_for(name):
            root = Path(self.temp.name) / name
            plan = fixture(root, configure_scene=configure)
            for scene in plan['scenes']:
                scene['scene_to_world']['translation'] = [-total, 0.0, 0.0]
            return root, plan

        after = dict(TRANSFORM, translation=[3.0, 0.0, 0.0])
        root, plan = plan_for('scaled-cancellation')
        with IdentityRegistry(Path(self.temp.name) / 'scaled.sqlite', asset_root=root) as registry:
            head = registry.create_timeline(plan)
            snapshot = registry.temporal_snapshot('main', 'shot-0', 2, boundary='before')['snapshot']
            residual = snapshot['entities'][0]['transform']['translation'][0]
            # The residual exceeds the former fixed 8-ulp floor near zero.
            self.assertGreater(abs(residual), 8 * math.ulp(1.0))
            registry.append_events('main', [event('scaled-origin', 2002, kind='relocate', target='aster',
                                                  before=TRANSFORM, after=after)], expected_sha256=head)
            self.assertEqual(registry.temporal_snapshot('main', 'shot-0', 2)['snapshot']['entities'][0]['transform'],
                             after)

        root, plan = plan_for('scaled-distinct')
        with IdentityRegistry(Path(self.temp.name) / 'scaled-distinct.sqlite', asset_root=root) as registry:
            head = registry.create_timeline(plan)
            distinct = dict(TRANSFORM, translation=[1e-9, 0.0, 0.0])
            with self.assertRaisesRegex(ValueError, 'PRECONDITION'):
                registry.append_events('main', [event('scaled-distinct', 2002, kind='relocate', target='aster',
                                                      before=distinct, after=after)], expected_sha256=head)

    def test_authored_preconditions_are_exact_and_rotations_are_normalized(self):
        coordinate = 999999999999.0
        angle_component = math.sqrt(0.5)
        float32_component = struct.unpack('<f', struct.pack('<f', angle_component))[0]
        native_before = dict(TRANSFORM, translation=[2, 0, 0])
        recorded = dict(TRANSFORM, translation=[coordinate, 0, 0],
                        rotation=[0.0, float32_component, 0.0, float32_component])
        first = event('authored-first', 2002, kind='relocate', target='aster',
                      before=native_before, after=recorded)
        head = self.append([first])
        for field, value in (
                ('translation', [coordinate + 0.001, 0, 0]),
                ('scale', [math.nextafter(1.0, 2.0), 1.0, 1.0])):
            wrong = dict(recorded, **{field: value})
            later = event('authored-later', 3003, kind='relocate', target='aster',
                          before=wrong, after=native_before)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'PRECONDITION'):
                self.append([later], head=head)
            self.assertEqual(self.registry.temporal_history('main')['sha256'], head)
        equivalent = dict(recorded, rotation=[0.0, -angle_component, 0.0, -angle_component])
        later = event('authored-later', 3003, kind='relocate', target='aster',
                      before=equivalent, after=native_before)
        self.append([later], head=head)
        self.assertEqual(continuity_bytes(self.registry.temporal_history('main')['events']),
                         continuity_bytes([first, later]))
        self.assertEqual(self.snap(frame=3)['snapshot']['entities'][0]['transform'], native_before)

    def test_plan_and_events_share_one_immutable_path_closure(self):
        source = self.plan['shots'][0]['shot']
        conflicting = dict(event('conflict', 2002, target='new'), sources=[dict(source, version='2.0.0')])
        valid = dict(event('same', 1001), sources=[deepcopy(source)])

        def counts():
            return tuple(self.registry.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                         for table in ('temporal_heads', 'temporal_contents', 'history'))

        prefix = event('first', 1001)
        before = counts()
        with self.assertRaisesRegex(ValueError, 'REFERENCE: conflicting immutable path'):
            self.registry.create_timeline(dict(self.plan, timeline_id='conflict'), events=[prefix, conflicting])
        self.assertEqual(counts(), before)
        with self.assertRaisesRegex(ValueError, 'REFERENCE: conflicting immutable path'):
            self.append([prefix, conflicting])
        self.assertEqual(counts(), before)
        self.assertEqual(self.registry.temporal_history('main')['sha256'], self.initial)
        self.assertEqual(self.registry.temporal_history('main')['events'], [])
        self.append([valid])
        self.assertEqual(self.registry.temporal_history('main')['events'], [valid])

    def test_snapshot_fact_limit_is_checked_at_append_and_allows_overwrite(self):
        with patch('continuity_temporal.MAX_ITEMS', 2):
            self.append([event('first', 1001, target='first'), event('second', 1001, target='second')])
            head = self.append([event('update', 2002, target='first', before=True, after=False)])
            with self.assertRaisesRegex(ValueError, 'LIMIT: snapshot fact count'):
                self.append([event('third', 3003, target='third')])
            self.assertEqual(self.registry.temporal_history('main')['sha256'], head)
            self.assertEqual([(row['id'], row['value']) for row in self.snap(frame=3)['snapshot']['facts']],
                             [('first', False), ('second', True)])

    def test_composed_entity_seed_must_fit_snapshot_transform_contract(self):
        root = Path(self.temp.name) / 'composed-native'
        plan = fixture(root, node_scale=500_000)
        for scene in plan['scenes']:
            scene['scene_to_world']['scale'] = [2, 2, 2]
        with IdentityRegistry(Path(self.temp.name) / 'composed.sqlite', asset_root=root) as registry:
            registry.create_timeline(plan)
            entities = registry.temporal_snapshot('main', 'shot-0', 0)['snapshot']['entities']
            self.assertEqual(entities[0]['transform']['scale'], [1_000_000] * 3)
            plan = dict(plan, timeline_id='over-limit')
            for scene in plan['scenes']:
                scene['scene_to_world']['scale'] = [3, 3, 3]
            with self.assertRaisesRegex(ValueError, 'SOURCE_STATE: composed native entity'):
                registry.create_timeline(plan)
            self.assertEqual(registry.db.execute('SELECT count(*) FROM temporal_heads').fetchone()[0], 1)

    def test_static_camera_override_requires_adapter_only_for_affected_mappings(self):
        for attachment in ('unrelated', 'camera', 'descendant'):
            with self.subTest(attachment=attachment):
                root = Path(self.temp.name) / attachment
                plan = fixture(root, camera_override=dict(TRANSFORM, translation=[0, 1, 0]),
                               camera_parent=attachment == 'descendant')
                if attachment == 'camera':
                    for scene in plan['scenes']:
                        scene['entities'][0]['node_id'] = 'camera'
                with IdentityRegistry(Path(self.temp.name) / (attachment + '.sqlite'), asset_root=root) as registry:
                    if attachment == 'unrelated':
                        registry.create_timeline(plan)
                        self.assertEqual(len(registry.temporal_snapshot('main', 'shot-0', 0)['snapshot']['entities']), 3)
                    else:
                        with self.assertRaisesRegex(ValueError, 'ADAPTER_REQUIRED: shot camera transform'):
                            registry.create_timeline(plan)
                        self.assertEqual(registry.db.execute('SELECT count(*) FROM temporal_heads').fetchone()[0], 0)

    def test_equivalent_camera_override_keeps_mapped_camera_queryable(self):
        root = Path(self.temp.name) / 'equivalent-camera'
        scene = json.loads((self.root / 'example-scene.json').read_bytes())
        transform = next(node['transform'] for node in scene['nodes'] if node['id'] == 'camera')
        self.assertNotIn('pivot', transform)
        override = dict(transform, pivot=[0, 0, 0])
        plan = fixture(root, camera_override=override)
        for entry in plan['scenes']:
            entry['entities'][0]['node_id'] = 'camera'
        with IdentityRegistry(Path(self.temp.name) / 'equivalent-camera.sqlite', asset_root=root) as registry:
            registry.create_timeline(plan)
            first = registry.temporal_snapshot('main', 'shot-0', 0)['snapshot']['entities']
            later = registry.temporal_snapshot('main', 'shot-1', 0)['snapshot']['entities']
            self.assertEqual(first, later)

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

    def test_unmapped_shot_scene_with_registered_bindings_has_typed_error(self):
        self.registry.register_bindings(dict(schema_version='1.0.0',
            project_id=self.plan['project_id'], project_revision=self.plan['project_revision'], instances=[]))
        plan = deepcopy(self.plan)
        plan['timeline_id'] = 'unmapped'
        plan['shots'][0]['scene_id'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_MAPPING'):
            self.registry.create_timeline(plan)

    def test_late_registration_preserves_unpinned_and_legacy_replay(self):
        head = self.append([event('shared', 1001)])
        snapshot = self.snap(revision=head)
        event_snapshot = self.registry.temporal_event_snapshot('main', 'shared', revision=head)
        legacy = {key: value for key, value in self.registry.temporal_history('main').items()
                  if key in ('plan', 'events', 'parents')}
        legacy_hash = digest(legacy)
        self.registry.db.execute('INSERT INTO temporal_contents VALUES (?,?)',
                                 (legacy_hash, continuity_bytes(legacy)))
        legacy_snapshot = self.snap(revision=legacy_hash)
        project_ref = deepcopy(self.plan['project_revision'])
        project_ref['version'] = '2.0.0'
        self.registry.register_bindings(dict(schema_version='1.0.0',
            project_id=self.plan['project_id'], project_revision=project_ref, instances=[]))
        self.assertEqual(self.snap(revision=head), snapshot)
        self.assertEqual(self.registry.temporal_event_snapshot('main', 'shared', revision=head), event_snapshot)
        self.assertEqual(self.snap(revision=legacy_hash), legacy_snapshot)
        self.assertEqual(self.registry.temporal_history('main', revision=legacy_hash),
                         dict(sha256=legacy_hash, **legacy))
        fork = self.registry.fork_timeline('main', 'late-fork', 'parallel', expected_sha256=head)
        self.assertIsNone(self.registry.temporal_history('late-fork', revision=fork)['bindings_sha256'])
        plan = dict(self.plan, timeline_id='rejected')
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_REFERENCE:.*exact project revision'):
            self.registry.create_timeline(plan)
        self.assertEqual(self.registry.db.execute('SELECT count(*) FROM temporal_bindings').fetchone()[0], 0)

    def test_revision_selection_keeps_captured_binding_documents(self):
        write_assets(self.root)
        self.registry.create_identity(sample('identity'))
        revisions = []
        for version in (1, 2):
            revision = sample('revision')
            revision.update(revision_id=f'robot-r{version}', semantic_version=f'{version}.0.0',
                            parent_revision_id='robot-r1' if version == 2 else None)
            draft = f'draft{version}'
            sha = self.registry.create_draft(draft, revision)
            self.registry.promote(draft, expected_sha256=sha,
                                  review=review_for(self.root, revision, name=f'review{version}'))
            revisions.append(sha)
        pin = dict(identity_id='robot', revision_id='robot-r1', semantic_version='1.0.0', sha256=revisions[0])
        original = self.registry.register_bindings(dict(schema_version='1.0.0',
            project_id=self.plan['project_id'], project_revision=self.plan['project_revision'],
            instances=[dict(instance_id='mesh', entity_id='aster', shot_id='shot-0', revision=pin)]))
        plan = deepcopy(self.plan)
        for scene in plan['scenes']:
            scene['entities'][0]['identity_id'] = 'robot'
        plan['timeline_id'] = 'pinned'
        head = self.registry.create_timeline(plan, events=[event('shared', 1001)])
        snapshot = self.registry.temporal_snapshot('pinned', 'shot-0', 0)
        event_snapshot = self.registry.temporal_event_snapshot('pinned', 'shared')
        fork = self.registry.fork_timeline('pinned', 'pinned-fork', 'parallel', expected_sha256=head)
        updated = self.registry.migrate_selected(self.plan['project_id'], ['mesh'], 'robot-r2',
                                                 expected_sha256=original)
        self.assertNotEqual(original, updated)
        self.assertEqual(self.registry.temporal_history('pinned')['bindings_sha256'], original)
        self.assertEqual(self.registry.temporal_snapshot('pinned', 'shot-0', 0), snapshot)
        self.assertEqual(self.registry.temporal_event_snapshot('pinned', 'shared'), event_snapshot)
        merged = self.registry.merge_timeline('pinned', 'pinned-fork', expected_sha256=head, source_sha256=fork)
        self.assertEqual(self.registry.temporal_history('pinned', revision=merged)['bindings_sha256'], original)
        new_head = self.registry.create_timeline(dict(plan, timeline_id='new-binding'))
        self.assertEqual(self.registry.temporal_history('new-binding', revision=new_head)['bindings_sha256'], updated)
        self.assertEqual(self.registry.db.execute('SELECT count(*) FROM temporal_bindings').fetchone()[0], 2)
        with IdentityRegistry(Path(self.temp.name) / 'continuity.sqlite', asset_root=self.root) as reopened:
            self.assertEqual(reopened.temporal_snapshot('pinned', 'shot-0', 0, revision=head), snapshot)
            reopened.db.execute('UPDATE temporal_bindings SET document=? WHERE hash=?',
                                (continuity_bytes(self.registry.bindings(self.plan['project_id'])['document']), original))
            with self.assertRaisesRegex(ValueError, 'CONTINUITY_HASH: corrupted temporal bindings'):
                reopened.temporal_snapshot('pinned', 'shot-0', 0, revision=head)

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

    def test_branch_merge_deduplicates_identical_shared_events(self):
        shared_event = event('shared', 1001, after=True)
        left = self.append([shared_event])
        right = self.registry.fork_timeline('main', 'right', 'parallel', expected_sha256=left)
        right = self.append([event('extra', 2002, target='extra')], timeline='right', head=right)
        merged = self.registry.merge_timeline('main', 'right', expected_sha256=left, source_sha256=right)
        history = self.registry.temporal_history('main', revision=merged)
        self.assertEqual([item['event_id'] for item in history['events']], ['shared', 'extra'])
        self.assertIs(history['events'][0]['operations'][0]['after'], True)

    def test_branch_merge_rejects_different_canonical_shared_events(self):
        for index, (left_value, right_value) in enumerate([(True, 1), (False, 0), (1, 1.0), (1, 2)]):
            with self.subTest(left=left_value, right=right_value):
                left_id, right_id = f'left-{index}', f'right-{index}'
                left = self.registry.fork_timeline('main', left_id, left_id, expected_sha256=self.initial)
                right = self.registry.fork_timeline('main', right_id, right_id, expected_sha256=self.initial)
                left = self.append([event('shared', 1001, after=left_value)], timeline=left_id, head=left)
                right = self.append([event('shared', 1001, after=right_value)], timeline=right_id, head=right)
                with self.assertRaisesRegex(ValueError, 'CONTINUITY_CONFLICT: branch event ID'):
                    self.registry.merge_timeline(left_id, right_id, expected_sha256=left, source_sha256=right)
                self.assertEqual(self.registry.temporal_history(left_id)['sha256'], left)
                self.assertEqual(self.registry.temporal_history(right_id)['sha256'], right)

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

    def test_cli_requires_exact_wire_integers_for_temporal_coordinates(self):
        event_file = Path(self.temp.name) / 'wire-events.json'
        payload = json.dumps([event('wire-tick', 1001, after=0.125)], separators=(',', ':'))
        command = [
            sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/continuity_temporal.py'),
            '--registry', str(self.registry.path), '--asset-root', str(self.root),
            'append', 'main', str(event_file), '--expected', self.initial,
        ]
        for token in ('1001.0000000000000000001', '9007199254740991.1'):
            with self.subTest(token=token):
                event_file.write_text(payload.replace('"tick":1001', '"tick":' + token), encoding='utf-8')
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('CONTINUITY_TIME', json.loads(result.stdout)['error'])
                self.assertEqual(self.registry.temporal_history('main')['sha256'], self.initial)

        event_file.write_text(payload.replace('"tick":1001', '"tick":1001.0'), encoding='utf-8')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current, = self.registry.temporal_history('main')['events']
        self.assertIs(type(current['tick']), int)
        self.assertEqual(current['tick'], 1001)
        self.assertEqual(current['operations'][0]['after'], 0.125)

    def test_cli_requires_exact_wire_integers_for_rational_terms(self):
        plan_file = Path(self.temp.name) / 'wire-plan.json'
        plan = deepcopy(self.plan)
        plan['timeline_id'] = 'wire-rational'
        payload = json.dumps(plan, separators=(',', ':'))
        command = [
            sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/continuity_temporal.py'),
            '--registry', str(self.registry.path), '--asset-root', str(self.root), 'create', str(plan_file),
        ]
        plan_file.write_text(payload.replace('"numerator":1,', '"numerator":1.0000000000000000001,'),
                             encoding='utf-8')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('CONTINUITY_TIME', json.loads(result.stdout)['error'])
        with self.assertRaisesRegex(ValueError, 'unknown temporal timeline'):
            self.registry.temporal_history(plan['timeline_id'])

        plan_file.write_text(payload.replace('"numerator":1,', '"numerator":1.0,'), encoding='utf-8')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stored = self.registry.temporal_history(plan['timeline_id'])['plan']
        self.assertEqual(stored['shots'][0]['story_speed'], {'numerator': 1, 'denominator': 1})
        self.assertIs(type(stored['shots'][0]['story_speed']['numerator']), int)

    def test_contract_loader_checks_inferred_temporal_and_nullable_integers(self):
        from continuity_contracts import load_document

        wire = Path(self.temp.name) / 'wire-contract.json'
        payload = json.dumps(event('loaded-tick', 1001), separators=(',', ':'))
        wire.write_text(payload.replace('"tick":1001,', '"tick":1001.0000000000000000001,'),
                        encoding='utf-8')
        command = [
            sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/continuity_contracts.py'),
            '--validate', str(wire),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('CONTINUITY_TIME', json.loads(result.stdout)['error'])

        snapshot = json.dumps(self.snap(frame=1)['snapshot'], separators=(',', ':'))
        wire.write_text(snapshot.replace('"frame":1,', '"frame":1.0000000000000000001,'),
                        encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_TIME'):
            load_document(wire, 'temporal-snapshot')
        wire.write_text(snapshot.replace('"frame":1,', '"frame":1.0,'), encoding='utf-8')
        self.assertIs(type(load_document(wire, 'temporal-snapshot')['frame']), int)

    def test_native_shot_rate_uses_exact_wire_terms_after_hash_binding(self):
        shot_path = self.root / 'shot-0.json'
        project_path = self.root / 'project.json'
        authored_shot = shot_path.read_bytes()
        project = json.loads(project_path.read_bytes())
        plan = deepcopy(self.plan)
        plan['timeline_id'] = 'native-wire-rate'
        self.assertEqual(authored_shot.count(b'"numerator":24000'), 1)

        def set_rate_token(token):
            shot_bytes = authored_shot.replace(b'"numerator":24000', b'"numerator":' + token)
            shot_path.write_bytes(shot_bytes)
            shot_hash = hashlib.sha256(shot_bytes).hexdigest()
            for entry in project['shots'] + project['dependency_lock']:
                if entry['id'] == 'shot-0':
                    entry['sha256'] = shot_hash
            project_bytes = canonical_bytes(project)
            project_path.write_bytes(project_bytes)
            plan['project_revision']['sha256'] = hashlib.sha256(project_bytes).hexdigest()
            plan['shots'][0]['shot']['sha256'] = shot_hash

        set_rate_token(b'24000.000000000000001')
        with self.assertRaisesRegex(ValueError, r'fps\.numerator.*exact integer'):
            self.registry.create_timeline(plan)
        with self.assertRaisesRegex(ValueError, 'unknown temporal timeline'):
            self.registry.temporal_history(plan['timeline_id'])

        set_rate_token(b'24000.0')
        self.registry.create_timeline(plan)
        result = self.registry.temporal_snapshot(plan['timeline_id'], 'shot-0', 1)
        self.assertEqual(result['snapshot']['story_tick'], 1001)

    def test_native_closure_must_match_the_plan_project_digest(self):
        alternate_root = self.root.parent / 'alternate-native'
        alternate_plan = fixture(alternate_root, incoming_translation=(5, 0, 0))
        alternate = load_project(alternate_root)
        self.assertEqual(alternate['project']['id'], self.plan['project_revision']['id'])
        self.assertNotEqual(alternate_plan['project_revision']['sha256'],
                            self.plan['project_revision']['sha256'])

        plan = deepcopy(self.plan)
        plan['timeline_id'] = 'mismatched-native-closure'
        with patch('scene3d_contracts.load_project', return_value=alternate):
            with self.assertRaisesRegex(ValueError, 'loaded native project digest mismatch'):
                self.registry.create_timeline(plan)
        with self.assertRaisesRegex(ValueError, 'unknown temporal timeline'):
            self.registry.temporal_history(plan['timeline_id'])

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
