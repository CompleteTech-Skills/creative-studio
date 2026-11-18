"""Persistent world-state projections over the existing temporal event owner.

This module owns neither a database nor native scene/physics state. Strict v1.1
records extend the same immutable temporal plan/events and deterministic replay.
"""
from copy import deepcopy

RICH_VERSION = '1.1.0'
WORLD_OPERATIONS = ('world-entity', 'world-fact', 'world-relocate')


def fail(message):
    raise ValueError('CONTINUITY_WORLD_STATE: ' + message)


def extend_definitions(d):
    """Extend the canonical schema graph, preserving v1.0 wire shapes."""
    from continuity_contracts import obj, arr, ref, nullable, enum, integer, ID, BOOL, TEXT
    pin = ref('revision-pin')
    d['world-costume'] = obj(dict(costume=ref('ref'), compatible_revisions=arr(pin, minimum=1, maximum=256),
        clothing=arr(ref('ref'), maximum=256), materials=arr(ref('ref'), maximum=256),
        textures=arr(ref('ref'), maximum=256), attachments=arr(ref('ref'), maximum=256),
        layers=arr(obj(dict(id=ID, visible=BOOL)), maximum=128),
        approved_combination=ref('ref'), appearance=enum('clean', 'worn', 'dirty', 'damaged'),
        physics_state=nullable(ref('ref')), reference_renders=arr(ref('ref'), maximum=64)))
    d['world-accessory'] = obj(dict(id=ID, asset=ref('ref'), status=enum('equipped', 'removed', 'damaged'),
        attachment=nullable(ref('ref')), material=nullable(ref('ref'))))
    d['world-environment'] = obj(dict(door=enum('open', 'closed', 'ajar', 'broken'), light_on=BOOL,
        device=enum('active', 'inactive', 'broken'), condition=enum('intact', 'damaged', 'destroyed', 'repaired'),
        weather=enum('clear', 'cloudy', 'rain', 'snow', 'storm', 'unknown'),
        time_of_day=enum('dawn', 'day', 'dusk', 'night', 'unknown'),
        atmosphere=nullable(ref('ref')), lighting=nullable(ref('ref'))), [])
    d['world-instance'] = obj(dict(id=ID, shot_id=ID, node_id=ID, render_ids=arr(ID, maximum=256, unique=True)))
    d['world-entity'] = obj(dict(entity_id=ID, entity_type=enum('character', 'prop', 'environment'),
        asset=ref('ref'), revision=nullable(pin), instances=arr(ref('world-instance'), minimum=1, maximum=4096),
        location_id=ID, physical_presence=BOOL, render_visible=BOOL, visibility_reason=enum('visible', 'out-of-frame', 'occluded', 'hidden', 'absent'),
        enabled=BOOL, parent_entity_id=nullable(ID), costume=nullable(ref('world-costume')),
        accessories=arr(ref('world-accessory'), maximum=256), attachments=arr(ref('ref'), maximum=256),
        environment=ref('world-environment')))
    d['world-location'] = obj(dict(id=ID, name=TEXT, scene_ids=arr(ID, minimum=1, unique=True)))
    d['world-fact-scope'] = obj(dict(kind=enum('project', 'world', 'scene', 'character', 'shot', 'trait', 'time-bounded'),
        subject=nullable(ID), start_tick=nullable(integer()), end_tick=nullable(integer())))
    d['world-fact'] = obj(dict(id=ID, scope=ref('world-fact-scope'),
        category=enum('knowledge', 'objective', 'relationship', 'intention', 'emotion', 'discovery', 'action', 'consequence', 'milestone', 'condition', 'other'),
        certainty=enum('definitive', 'inferred', 'unknown', 'unresolved'),
        value=dict(type=['string', 'number', 'boolean', 'null'], maxLength=4096),
        causes=arr(ID, maximum=128, unique=True), evidence=arr(ref('ref'), maximum=64)))
    d['world-state'] = obj(dict(locations=arr(ref('world-location'), minimum=1, maximum=4096),
        entities=arr(ref('world-entity'), minimum=1, maximum=4096), facts=arr(ref('world-fact'), maximum=4096)))
    # Entity transitions retain immutable identity/instance inventories. A reason
    # names the authored change; arbitrary path mutation is never accepted.
    entity_op = obj(dict(event_type=dict(const='world-entity'), entity_id=ID,
        reason=enum('costume-change', 'accessory-change', 'enter', 'exit', 'occlusion', 'reveal', 'environment-change', 'attachment-change', 'enable-change', 'render-visibility'),
        before=ref('world-entity'), after=ref('world-entity')))
    fact_op = obj(dict(event_type=dict(const='world-fact'), fact_id=ID,
        before=nullable(ref('world-fact')), after=ref('world-fact')))
    relocation = obj(dict(event_type=dict(const='world-relocate'), entity_id=ID,
        reason=enum('continuous-travel', 'authored-teleport', 'time-jump'),
        before=ref('transform'), after=ref('transform'),
        previous_location_id=ID, next_location_id=ID, cause=TEXT))
    d['world-operation'] = dict(oneOf=[entity_op, fact_op, relocation])
    for name in ('temporal-timeline', 'temporal-event', 'temporal-snapshot'):
        original = deepcopy(d[name]); rich = deepcopy(original)
        rich['properties']['schema_version'] = dict(const=RICH_VERSION)
        if name == 'temporal-timeline':
            rich['properties']['world_state'] = ref('world-state'); rich['required'].append('world_state')
            rich['properties']['scenes'] = arr(ref('world-temporal-scene'), minimum=1)
        elif name == 'temporal-event':
            rich['properties']['operations'] = arr(ref('world-operation'), minimum=1)
            rich['properties']['reason'] = TEXT; rich['required'].append('reason')
        else:
            rich['properties']['world_state'] = ref('world-state'); rich['required'].append('world_state')
            rich['properties']['active_fact_ids'] = arr(ID, unique=True); rich['required'].append('active_fact_ids')
        d[name] = dict(oneOf=[original, rich])
    d['world-temporal-scene'] = obj(dict(**deepcopy(d['temporal-scene']['properties']),
        location_id=ID, incoming_world_state=ref('world-state')))
    return d


def _unique(items, key, label):
    index = {item[key]: item for item in items}
    if len(index) != len(items): fail('duplicate ' + label)
    return index


def validate_world(state, plan, *, applied=()):
    """Check persistent identity and scope relationships, never infer native art."""
    locations = _unique(state['locations'], 'id', 'location ID')
    entities = _unique(state['entities'], 'entity_id', 'persistent entity ID')
    facts = _unique(state['facts'], 'id', 'scoped fact ID')
    scenes = {entry['scene']['id']: entry for entry in plan['scenes']}
    shots = {entry['shot']['id']: entry for entry in plan['shots']}
    mappings = {binding['entity_id']: binding['identity_id'] for entry in plan['scenes'] for binding in entry['entities']}
    if set(entities) != set(mappings): fail('persistent entities must exactly match native entity mappings')
    for location in locations.values():
        if not set(location['scene_ids']) <= set(scenes): fail('location references an unmapped scene')
    for scene_id, entry in scenes.items():
        location = locations.get(entry['location_id'])
        if location is None or scene_id not in location['scene_ids']: fail('scene has an unmapped location')
        if sum(scene_id in loc['scene_ids'] for loc in locations.values()) != 1:
            fail('scene is ambiguously mapped to multiple physical locations')
    instance_ids, render_ids = set(), set()
    for identifier, entity in entities.items():
        if entity['location_id'] not in locations: fail('entity has an unmapped location')
        revision = entity['revision']
        if (entity['entity_type'] == 'character') != (revision is not None): fail('character needs its exact identity revision pin')
        if revision is not None and revision['identity_id'] != mappings[identifier]: fail('character revision disagrees with native entity identity mapping')
        if entity['physical_presence'] and entity['visibility_reason'] == 'absent': fail('physical presence contradicts absence')
        if not entity['physical_presence'] and (entity['visibility_reason'] != 'absent' or entity['render_visible']): fail('absent entity needs explicit absent visibility reason')
        if entity['parent_entity_id'] is not None and entity['parent_entity_id'] not in entities: fail('unknown persistent parent')
        for instance in entity['instances']:
            if instance['id'] in instance_ids: fail('duplicate shot instance ID')
            instance_ids.add(instance['id'])
            if instance['shot_id'] not in shots: fail('instance references unmapped shot')
            scene = scenes[shots[instance['shot_id']]['scene_id']]
            native = next((b for b in scene['entities'] if b['entity_id'] == identifier), None)
            if native is None or native['node_id'] != instance['node_id']: fail('shot instance disagrees with native node mapping')
            for render_id in instance['render_ids']:
                if render_id in render_ids: fail('duplicate render object ID')
                render_ids.add(render_id)
        _unique(entity['accessories'], 'id', 'accessory ID')
        costume = entity['costume']
        if costume is not None:
            if revision is None or revision not in costume['compatible_revisions']: fail('costume incompatible with exact character revision')
            _unique(costume['layers'], 'id', 'costume layer ID')
    for identifier in entities:
        seen = set(); current = identifier
        while current is not None:
            if current in seen: fail('persistent parent cycle')
            seen.add(current); current = entities[current]['parent_entity_id']
    for fact in facts.values():
        scope = fact['scope']; kind, subject = scope['kind'], scope['subject']
        valid = ((kind in ('project', 'world', 'time-bounded') and subject is None) or
                 (kind == 'scene' and subject in scenes) or (kind == 'shot' and subject in shots) or
                 (kind in ('character', 'trait') and subject in entities and entities[subject]['entity_type'] == 'character'))
        if not valid: fail('fact has unresolved scope')
        start, end = scope['start_tick'], scope['end_tick']
        if (start is None) != (end is None) or start is not None and start >= end: fail('fact has invalid half-open lifetime')
        if kind in ('shot', 'time-bounded') and start is None: fail('temporary fact requires explicit lifetime')
        if fact['category'] == 'knowledge' and kind != 'character': fail('knowledge requires explicit character scope')
        if fact['certainty'] in ('unknown', 'unresolved') and fact['value'] is not None: fail('unknown fact cannot assert a definitive value')
        if fact['certainty'] == 'inferred' and not fact['evidence']: fail('inferred fact requires retained evidence')
        if not set(fact['causes']) <= set(applied): fail('fact cause is missing or not earlier')
    return state


def target(operation):
    if operation['event_type'] == 'world-fact': return 'world-fact:' + operation['fact_id']
    if operation['event_type'] == 'world-relocate': return 'entity:' + operation['entity_id'] + ':relocate'
    return 'world-entity:' + operation['entity_id']


def apply(operation, state, plan, applied, equal):
    """Apply one typed before/after projection to the shared replay state."""
    world = state.get('world_state')
    if world is None: fail('rich operation requires explicit v1.1 world state')
    entities = {e['entity_id']: e for e in world['entities']}
    kind = operation['event_type']
    if kind == 'world-fact':
        facts = {f['id']: f for f in world['facts']}; identifier = operation['fact_id']
        if operation['after']['id'] != identifier or not equal(facts.get(identifier), operation['before']): fail('scoped fact precondition mismatch')
        facts[identifier] = deepcopy(operation['after']); world['facts'] = [facts[k] for k in sorted(facts)]
    else:
        identifier = operation['entity_id']
        if identifier not in entities: fail('operation targets unmapped persistent entity')
        entity = entities[identifier]
        if kind == 'world-relocate':
            native = state['entities'][identifier]
            if not equal(native['transform'], operation['before']) or entity['location_id'] != operation['previous_location_id']: fail('relocation precondition mismatch')
            if operation['next_location_id'] not in {x['id'] for x in world['locations']}: fail('relocation destination is unmapped')
            native['transform'] = deepcopy(operation['after']); entity['location_id'] = operation['next_location_id']
        else:
            after = operation['after']
            if not equal(entity, operation['before']) or after['entity_id'] != identifier: fail('persistent entity precondition mismatch')
            immutable = ('entity_id', 'entity_type', 'asset', 'revision', 'instances')
            if any(not equal(entity[key], after[key]) for key in immutable): fail('transition cannot replace persistent native identity or instance mappings')
            allowed = {
                'costume-change': {'costume'}, 'accessory-change': {'accessories'},
                'enter': {'physical_presence', 'visibility_reason', 'render_visible'}, 'exit': {'physical_presence', 'visibility_reason', 'render_visible'},
                'occlusion': {'visibility_reason'}, 'reveal': {'visibility_reason'},
                'environment-change': {'environment'}, 'attachment-change': {'attachments', 'parent_entity_id'},
                'enable-change': {'enabled'}, 'render-visibility': {'render_visible'}}[operation['reason']]
            changed = {key for key in entity if not equal(entity[key], after[key])}
            if not changed or not changed <= allowed: fail('transition fields do not match declared reason')
            reason = operation['reason']
            if reason == 'enter' and (entity['physical_presence'] or not after['physical_presence']): fail('entry must change physical absence to presence')
            if reason == 'exit' and (not entity['physical_presence'] or after['physical_presence']): fail('exit must change physical presence to absence')
            if reason == 'occlusion' and after['visibility_reason'] not in ('out-of-frame', 'occluded', 'hidden'): fail('occlusion needs explicit camera invisibility')
            if reason == 'reveal' and after['visibility_reason'] != 'visible': fail('reveal must make entity visible')
            entity.clear(); entity.update(deepcopy(after))
            state['entities'][identifier]['visible'] = entity['render_visible']
    validate_world(world, plan, applied=applied)


def active_facts(world, *, tick, scene_id, shot_id):
    result = []
    for fact in world['facts']:
        scope = fact['scope']; start, end = scope['start_tick'], scope['end_tick']
        if start is not None and not start <= tick < end: continue
        if scope['kind'] == 'scene' and scope['subject'] != scene_id: continue
        if scope['kind'] == 'shot' and scope['subject'] != shot_id: continue
        result.append(fact['id'])
    return sorted(result)


def validate_native_bindings(root, world):
    """Bind selected costume declarations to retained canonical costume records.

    This checks the supplied structural record and exact bytes; it grants no new
    visual approval, compatibility solver result or cloth/physics execution.
    """
    from continuity_contracts import read_reference_bytes, decode_json, validate_document
    for entity in world['entities']:
        costume = entity['costume']
        if costume is None: continue
        reference = costume['costume']
        record = validate_document(decode_json(read_reference_bytes(root, reference)), 'costume')
        if (reference['kind'] != 'continuity-costume' or record['id'] != reference['id']
                or record['semantic_version'] != reference['version']
                or record['identity_id'] != entity['revision']['identity_id']):
            fail('costume record identity/version differs from selected native reference')
        if record['variant'] not in costume['clothing']:
            fail('costume selection omits its canonical clothing variant')
        if not set((r['path'], r['sha256']) for r in record['accessories']) <= set(
                (r['asset']['path'], r['asset']['sha256']) for r in entity['accessories'] if r['status'] != 'removed'):
            fail('canonical costume accessory is missing')
        if not set((r['path'], r['sha256']) for r in record['material_overrides']) <= set(
                (r['path'], r['sha256']) for r in costume['materials']):
            fail('canonical costume material override is missing')


def validate_scene_assets(world, entry, native_scene, documents, project_directory):
    """Prove persistent canonical assets agree with actual mapped native nodes."""
    from pathlib import Path
    entities = {e['entity_id']:e for e in world['entities']}
    for entity in entry['incoming_world_state']['entities']:
        if entity['render_visible'] and entity['location_id'] != entry['location_id']:
            fail('visible incoming entity belongs to another physical location')
    nodes = {n['id']:n for n in native_scene['nodes']}
    for binding in entry['entities']:
        entity = entities[binding['entity_id']]
        node = nodes[binding['node_id']]
        native_ref = node.get('asset')
        if native_ref is None: fail('persistent entity needs an explicit native asset binding')
        native_asset = documents[native_ref['path']]
        source = entity['asset']
        manifest_path = (Path(project_directory)/native_ref['path']).as_posix()
        matches_manifest = (source['path']==manifest_path and source['sha256']==native_ref['sha256']
                            and source['id']==native_ref['id'] and source['version']==native_ref['version'])
        content = native_asset.get('content', {})
        matches_content = (source['kind']=='mesh' and source['path']==(Path(project_directory)/content.get('path','')).as_posix()
                           and source['sha256']==content.get('sha256') and source['id']==native_asset['id']
                           and source['version']==native_asset['version'])
        if not (matches_manifest or matches_content): fail('canonical persistent asset differs from actual native scene asset')
