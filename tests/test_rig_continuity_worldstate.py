"""Real Scene3D closure and shared temporal persistence; synthetic, nonvisual."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from continuity_contracts import digest
from continuity_registry import IdentityRegistry
from continuity_worldstate import RICH_VERSION
from continuity_test_helpers import sample, write_assets, review_for, pointer
from test_rig_continuity_temporal import fixture


def world_fixture(root, registry, plan):
    write_assets(root)
    mesh = pointer('triangle.obj', (root/'triangle.obj').read_bytes(), identity='triangle')
    pins = {}
    for name in ('aster', 'moss'):
        identity = sample('identity'); identity['identity_id'] = name
        registry.create_identity(identity)
        revision = sample('revision')
        revision.update(identity_id=name, revision_id=name+'-r1', asset=deepcopy(mesh), geometry=deepcopy(mesh))
        revision['components'][0]['mesh']=deepcopy(mesh)
        registry.create_draft(name+'-draft', revision)
        registry.promote(name+'-draft', expected_sha256=digest(revision),
            review=review_for(root, revision, name=name+'-receipt'))
        pins[name] = dict(identity_id=name, revision_id=revision['revision_id'],
                          semantic_version='1.0.0', sha256=digest(revision))
    for scene in plan['scenes']:
        for binding in scene['entities']:
            binding['identity_id'] = binding['entity_id'] if binding['entity_id'] in pins else None
    entities = []
    for identifier, node_id in [('aster', 'mesh'), ('moss', 'second-character'), ('key', 'prop')]:
        entities.append(dict(entity_id=identifier, entity_type='character' if identifier in pins else 'prop',
            asset=deepcopy(mesh), revision=pins.get(identifier),
            instances=[dict(id=identifier+'-'+shot['shot']['id'], shot_id=shot['shot']['id'],
                            node_id=node_id, render_ids=[identifier+'-render-'+shot['shot']['id']]) for shot in plan['shots']],
            location_id='stage', physical_presence=True, render_visible=True, visibility_reason='visible', enabled=True,
            parent_entity_id=None, costume=None, accessories=[], attachments=[], environment={}))
    # Interactive environment state uses explicit persistent entities rather than
    # introducing an unrelated property database. Existing key stand-in doubles
    # as the labeled engineering device fixture, without artwork claims.
    entities[-1]['environment'] = dict(door='closed', light_on=False)
    world = dict(locations=[dict(id='stage', name='same physical stage in two native files',
                  scene_ids=[s['scene']['id'] for s in plan['scenes']])], entities=entities, facts=[])
    plan.update(schema_version=RICH_VERSION, world_state=deepcopy(world))
    for scene in plan['scenes']:
        scene.update(location_id='stage', incoming_world_state=deepcopy(world))
    return plan, world


def rich_event(identifier, tick, operation, *, prerequisites=()):
    return dict(schema_version=RICH_VERSION, kind='continuity-temporal-event', event_id=identifier,
        tick=tick, priority=0, prerequisites=list(prerequisites), operations=[operation], sources=[],
        author='synthetic worldstate regression', revision=1, reason='explicit authored structural change')


def entity_event(world, identifier, tick, *, entity_id='key', reason='environment-change', **changes):
    before = deepcopy(next(e for e in world['entities'] if e['entity_id']==entity_id))
    after = deepcopy(before); after.update(changes)
    return rich_event(identifier, tick, dict(event_type='world-entity', entity_id=entity_id,
                                            reason=reason, before=before, after=after))


class WorldStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'native'
        plan = fixture(self.root)
        self.registry = IdentityRegistry(Path(self.temp.name)/'continuity.sqlite', asset_root=self.root)
        self.addCleanup(self.registry.close)
        self.plan, self.world = world_fixture(self.root, self.registry, plan)

    def create(self, events=None):
        return self.registry.create_timeline(self.plan, events=events)

    def snapshot(self, shot='shot-0', frame=0, **options):
        return self.registry.temporal_snapshot('main', shot, frame, **options)['snapshot']

    def incoming(self, world):
        self.plan['scenes'][1]['incoming_world_state'] = deepcopy(world)

    def test_door_light_and_character_knowledge_survive_different_scene_files(self):
        before = self.world['entities'][-1]
        after = deepcopy(before); after['environment'] = dict(door='open', light_on=True)
        event = entity_event(self.world, 'open-door', 1001, environment=after['environment'])
        fact = dict(id='aster-knows-door', scope=dict(kind='character', subject='aster', start_tick=None,end_tick=None),
            category='knowledge', certainty='definitive', value=True, causes=['open-door'], evidence=[])
        knowledge = rich_event('learn',1001,dict(event_type='world-fact',fact_id=fact['id'],before=None,after=fact),prerequisites=['open-door'])
        knowledge['priority']=1
        incoming=deepcopy(self.world); incoming['entities'][-1]=after;incoming['facts']=[fact]
        self.incoming(incoming);self.create([event,knowledge])
        expected=self.snapshot('shot-2',0)['world_state']
        self.assertEqual(expected, incoming)
        self.assertEqual(self.snapshot('shot-0',0)['world_state'],self.world)
        self.assertEqual(self.snapshot('shot-2',0)['world_state'],expected)
        self.assertIn(fact['id'],self.snapshot('shot-2',0)['active_fact_ids'])

    def test_camera_occlusion_keeps_physical_presence_and_identity(self):
        after=deepcopy(self.world['entities'][0]);after['visibility_reason']='occluded'
        event=entity_event(self.world,'occlude',2002,entity_id='aster',reason='occlusion',visibility_reason='occluded')
        self.create();head=self.registry.temporal_history('main')['sha256']
        self.registry.append_events('main',[event],expected_sha256=head)
        snapshot=self.snapshot(frame=2)
        actual=snapshot['world_state']['entities'][0]
        self.assertTrue(actual['physical_presence']);self.assertEqual(actual['revision'],after['revision'])
        self.assertTrue(next(e for e in snapshot['entities'] if e['entity_id']=='aster')['visible'])
        self.assertEqual(actual['visibility_reason'],'occluded')

    def test_duplicate_entities_instances_and_unmapped_locations_fail_atomically(self):
        for mutation in (
            lambda w:w['entities'].append(deepcopy(w['entities'][0])),
            lambda w:w['entities'][1]['instances'][0].update(id=w['entities'][0]['instances'][0]['id']),
            lambda w:w['entities'][0].update(location_id='unmapped')):
            plan=deepcopy(self.plan);mutation(plan['world_state'])
            with self.assertRaises(ValueError):self.registry.create_timeline(plan)
            self.assertEqual(self.registry.db.execute('SELECT count(*) FROM temporal_heads').fetchone()[0],0)

    def test_incoming_scene_reset_and_missing_prerequisite_reject(self):
        event=entity_event(self.world,'open',1001,environment=dict(door='open',light_on=True))
        with self.assertRaisesRegex(ValueError,'SOURCE_STATE|WORLD_STATE'):self.create([event])
        event['prerequisites']=['missing']
        with self.assertRaisesRegex(ValueError,'PRECONDITION'):self.create([event])

    def test_v1_shape_rejects_rich_fields_and_v11_rejects_legacy_operations(self):
        plan=deepcopy(self.plan);plan['schema_version']='1.0.0'
        with self.assertRaises(ValueError):self.registry.create_timeline(plan)
        self.create()
        from test_rig_continuity_temporal import event
        head=self.registry.temporal_history('main')['sha256']
        with self.assertRaises(ValueError):self.registry.append_events('main',[event('legacy',2002)],expected_sha256=head)
        self.assertEqual(self.registry.temporal_history('main')['sha256'],head)

    def test_costume_accessory_changes_persist_and_incompatible_revision_rejects(self):
        from continuity_contracts import canonical_bytes
        from continuity_test_helpers import pointer
        costume=sample('costume'); costume.update(id='coat', identity_id='aster')
        costume['variant']=pointer();costume['accessories']=[];costume['material_overrides']=[]
        data=canonical_bytes(costume);(self.root/'coat.json').write_bytes(data)
        selected=dict(costume=pointer('coat.json',data,kind='continuity-costume',identity='coat'),
            compatible_revisions=[self.world['entities'][0]['revision']],clothing=[pointer()],materials=[],textures=[],
            attachments=[],layers=[dict(id='coat',visible=True)],approved_combination=pointer('reference.ppm',b'P6\n1 1\n255\n\xff\x00\x00',kind='evidence',identity='reference'),
            appearance='clean',physics_state=None,reference_renders=[])
        event=entity_event(self.world,'wear',1001,entity_id='aster',reason='costume-change',costume=selected)
        incoming=deepcopy(self.world);incoming['entities'][0]['costume']=selected
        self.incoming(incoming);head=self.create([event])
        self.assertEqual(self.snapshot('shot-2',0)['world_state']['entities'][0]['costume'],selected)
        bad=deepcopy(selected);bad['compatible_revisions']=[self.world['entities'][1]['revision']]
        previous=incoming['entities'][0];after=deepcopy(previous);after['costume']=bad
        invalid=rich_event('bad-coat',2002,dict(event_type='world-entity',entity_id='aster',reason='costume-change',before=previous,after=after))
        with self.assertRaisesRegex(ValueError,'incompatible'):
            self.registry.append_events('main',[invalid],expected_sha256=head)
        self.assertEqual(self.registry.temporal_history('main')['sha256'],head)

    def test_fact_uncertainty_and_temporary_scope_are_explicit(self):
        fact=dict(id='temporary',scope=dict(kind='shot',subject='shot-0',start_tick=0,end_tick=2002),
                  category='objective',certainty='unresolved',value=None,causes=[],evidence=[])
        world=deepcopy(self.world);world['facts']=[fact]
        self.plan['world_state']=world
        for scene in self.plan['scenes']:scene['incoming_world_state']=deepcopy(world)
        self.create()
        self.assertEqual(self.snapshot(frame=1)['active_fact_ids'],['temporary'])
        self.assertEqual(self.snapshot(frame=2)['active_fact_ids'],[])
        self.assertEqual(self.snapshot('shot-2',0)['active_fact_ids'],[])
        before=deepcopy(fact);after=deepcopy(fact);after.update(value=True)
        event=rich_event('unsupported-certainty',2002,dict(event_type='world-fact',fact_id='temporary',before=before,after=after))
        head=self.registry.temporal_history('main')['sha256']
        with self.assertRaisesRegex(ValueError,'unknown fact'):
            self.registry.append_events('main',[event],expected_sha256=head)

    def test_exit_entry_require_actual_presence_changes(self):
        self.create();head=self.registry.temporal_history('main')['sha256']
        event=entity_event(self.world,'exit',2002,entity_id='aster',reason='exit',physical_presence=False,render_visible=False,visibility_reason='absent')
        head=self.registry.append_events('main',[event],expected_sha256=head)
        actual=self.snapshot(frame=2)['world_state']['entities'][0]
        self.assertFalse(actual['physical_presence'])
        after=deepcopy(actual);after.update(physical_presence=True,render_visible=True,visibility_reason='visible')
        enter=rich_event('entry',3003,dict(event_type='world-entity',entity_id='aster',reason='enter',before=actual,after=after))
        self.registry.append_events('main',[enter],expected_sha256=head)
        self.assertTrue(self.snapshot(frame=3)['world_state']['entities'][0]['physical_presence'])

    def test_false_fact_causes_and_illegal_identity_replacement_reject(self):
        self.create();head=self.registry.temporal_history('main')['sha256']
        fact=dict(id='caused',scope=dict(kind='world',subject=None,start_tick=None,end_tick=None),
            category='consequence',certainty='definitive',value=True,causes=['missing'],evidence=[])
        event=rich_event('fact',2002,dict(event_type='world-fact',fact_id='caused',before=None,after=fact))
        with self.assertRaisesRegex(ValueError,'cause'):
            self.registry.append_events('main',[event],expected_sha256=head)
        after=deepcopy(self.world['entities'][0]);after['asset']={**after['asset'],'id':'different'}
        invalid=rich_event('replacement',2002,dict(event_type='world-entity',entity_id='aster',reason='costume-change',before=self.world['entities'][0],after=after))
        with self.assertRaisesRegex(ValueError,'identity'):
            self.registry.append_events('main',[invalid],expected_sha256=head)
