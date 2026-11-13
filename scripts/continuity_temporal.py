"""Exact, deterministic temporal replay in the existing continuity sidecar.

Native Scene3D files remain authoritative for identities and initial transforms.
This engine persists immutable authored events, not a competing scene database.
Snapshots are always derived and caches are checked against a fresh replay.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import hashlib

from continuity_contracts import (canonical_bytes, decode_json, digest,
    read_reference_bytes, validate_document, verify_references)

MAX_EVENTS = 4096
ENGINE_VERSION = 'continuity-temporal-v1'


def fail(code, message):
    raise ValueError('CONTINUITY_' + code + ': ' + message)


def fraction(value):
    return Fraction(value['numerator'], value['denominator'])


def _integer(value, label):
    if type(value) is not int or not 0 <= value <= 2**53-1:
        fail('TIME', label + ' must be a nonnegative exact integer')
    return value


def _exact(value):
    if value.denominator != 1 or not 0 <= value <= 2**53-1:
        fail('MAPPING', 'coordinate is not representable in the declared integer timebase')
    return int(value)


def _key(event):
    return event['tick'], event['priority'], event['event_id']


def _target(operation):
    if operation['event_type'] == 'set-fact':
        return 'fact:' + operation['fact_id']
    return 'entity:' + operation['entity_id'] + ':' + operation['event_type']


def _equal(a, b):
    # JSON true and 1 differ; exact numeric 2 and 2.0 are equivalent.
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _operations(events, initial, until=None):
    """Replay from source; no mutable evaluator state survives a query."""
    state = deepcopy(initial)
    applied, previous = [], None
    for event in events:
        key = _key(event)
        if previous is not None and key <= previous:
            fail('ORDER', 'events must be strictly ordered by tick, priority, event ID')
        previous = key
        if event['event_id'] in applied:
            fail('DUPLICATE', 'event ID applied twice')
        if not set(event['prerequisites']) <= set(applied):
            fail('PRECONDITION', 'event prerequisite is absent or not earlier')
        if until is not None and not until(key):
            break
        targets = set()
        for operation in event['operations']:
            target = _target(operation)
            if target in targets:
                fail('DUPLICATE', 'event writes the same state field twice')
            targets.add(target)
            kind = operation['event_type']
            if kind == 'set-fact':
                container, field = state['facts'], operation['fact_id']
                current = container.get(field)
            else:
                entity = operation['entity_id']
                if entity not in state['entities']:
                    fail('REFERENCE', 'event targets an unmapped native entity')
                container = state['entities'][entity]
                field = 'transform' if kind == 'relocate' else 'visible'
                current = container[field]
            if not _equal(current, operation['before']):
                fail('PRECONDITION', event['event_id'] + ' before-state mismatch for ' + target)
            container[field] = deepcopy(operation['after'])
        applied.append(event['event_id'])
    return state, applied


class TemporalEngine:
    """Bounded native-file adapter and stateless event evaluator.

    The supported source is a static Scene3D hierarchy. Native dynamic binding
    declarations fail closed in evaluate_transforms; no pose/physics inference.
    """
    def __init__(self, root, manifest):
        self.root = Path(root)
        self.plan = validate_document(manifest['plan'], 'temporal-timeline')
        self.events = [validate_document(e, 'temporal-event') for e in manifest['events']]
        if len(self.events) > MAX_EVENTS:
            fail('LIMIT', 'too many temporal events')
        if len({e['event_id'] for e in self.events}) != len(self.events):
            fail('DUPLICATE', 'event ID reused')
        verify_references(self.root, self.plan)
        verify_references(self.root, dict(events=self.events))
        from scene3d_contracts import load_project, validate_document as native_validate, frame_time
        from scene3d_math import evaluate_transforms, local_matrix
        from rig_math import decompose
        # The native closure loader verifies actual project, scene and asset
        # identity/version/hash relationships, including transitive payloads.
        project = self.plan['project_revision']
        if project['kind'] != 'scene3d-project':
            fail('REFERENCE', 'temporal project must reference native Scene3D')
        self.native = load_project((self.root / project['path']).parent, Path(project['path']).name)
        # Decode from verified references so the serialized native IDs are checked
        # even when a plan selects a subset of a larger project.
        project_doc = native_validate(decode_json(read_reference_bytes(self.root, project)), 'project')
        if (project_doc['id'], project_doc['version'], project_doc['kind']) != (project['id'], project['version'], project['kind']):
            fail('REFERENCE', 'project reference identity mismatch')
        known_scenes = {digest(r) for r in project_doc['scenes']}
        known_shots = {digest(r) for r in project_doc['shots']}
        scenes, seeds = {}, {}
        for entry in self.plan['scenes']:
            reference = entry['scene']
            if digest(reference) not in known_scenes:
                fail('REFERENCE', 'scene is not in native project')
            scene = native_validate(decode_json(read_reference_bytes(self.root, reference)), 'scene')
            if scene['id'] in scenes:
                fail('DUPLICATE', 'scene mapping repeated')
            evaluated = evaluate_transforms(scene)
            matrix = local_matrix(entry['scene_to_world'])
            entities, nodes = {}, set()
            for binding in entry['entities']:
                entity, node = binding['entity_id'], binding['node_id']
                if entity in entities or node in nodes:
                    fail('DUPLICATE', 'entity/node mapping must be one-to-one within a scene')
                nodes.add(node)
                if node not in evaluated:
                    fail('MAPPING', 'node has no native evaluated state')
                result = evaluated[node]
                transform = decompose(matrix @ __import__('numpy').array(result['world_matrix']))
                entities[entity] = dict(entity_id=entity, identity_id=binding['identity_id'],
                    transform={k: transform[k] for k in ('translation', 'rotation', 'scale')},
                    visible=result['visible'])
            scenes[scene['id']] = scene
            seeds[scene['id']] = entities
        self.shots = {}
        last_end = -1
        scene_intervals = {}
        for entry in self.plan['shots']:
            reference = entry['shot']
            if digest(reference) not in known_shots:
                fail('REFERENCE', 'shot is not in native project')
            shot = native_validate(decode_json(read_reference_bytes(self.root, reference)), 'shot')
            if shot['id'] in self.shots:
                fail('DUPLICATE', 'shot mapping repeated')
            scene_id = entry['scene_id']
            if scene_id not in scenes or shot['scene']['id'] != scene_id:
                fail('MAPPING', 'shot scene mapping unresolved')
            if shot['tracks'] or shot['bindings'] or shot['temporal_constraints']:
                fail('ADAPTER_REQUIRED', 'dynamic shot needs an admitted native evaluator')
            speed = fraction(entry['story_speed'])
            if speed < 0:
                fail('MAPPING', 'story speed cannot be negative; use an explicit offset for flashbacks')
            frame_ticks = frame_time(1, shot['fps']) * self.plan['ticks_per_second']
            step = _exact(frame_ticks)
            story_step = _exact(frame_ticks * speed)
            end = entry['presentation_start_tick'] + step * shot['duration_frames']
            _integer(end, 'presentation end')
            if entry['presentation_start_tick'] < last_end:
                fail('MAPPING', 'presentation mappings overlap or are out of order')
            last_end = end
            interval = (entry['scene_start_tick'], entry['scene_start_tick'] + step * shot['duration_frames'])
            if any(interval[0] < b and a < interval[1] for a,b in scene_intervals.get(scene_id, [])):
                fail('MAPPING', 'scene shot intervals overlap')
            scene_intervals.setdefault(scene_id, []).append(interval)
            self.shots[shot['id']] = dict(entry=entry, native=shot, step=step, story_step=story_step)
        first = min(self.shots.values(), key=lambda s: s['entry']['story_start_tick'])
        if first['entry']['story_start_tick'] != 0:
            fail('MAPPING', 'initial native state must be anchored at story tick zero')
        self.initial = dict(entities=seeds[first['entry']['scene_id']], facts={})
        for scene_id, entities in seeds.items():
            if set(entities) != set(self.initial['entities']):
                fail('MAPPING', 'every scene must explicitly map the persistent entity set')
            if any(entities[k]['identity_id'] != self.initial['entities'][k]['identity_id'] for k in entities):
                fail('IDENTITY', 'scene changes persistent logical identity')
        self.max_tick = max(s['entry']['story_start_tick'] + s['story_step'] * s['native']['duration_frames'] for s in self.shots.values())
        _integer(self.max_tick, 'story end')
        if any(e['tick'] > self.max_tick for e in self.events):
            fail('MAPPING', 'event falls outside mapped story time')
        _operations(self.events, self.initial)
        # An additional scene's native starting state must agree with the
        # authored chronology. Never silently discard a conflicting native
        # incoming transform/visibility under an inherited sidecar value.
        seed_scene = first['entry']['scene_id']
        for scene_id, entities in seeds.items():
            if scene_id == seed_scene:
                continue
            incoming = [s['entry']['story_start_tick'] for s in self.shots.values()
                        if s['entry']['scene_id'] == scene_id]
            if not incoming:
                fail('MAPPING', 'scene mapping has no consuming shot')
            initial_tick = min(incoming)
            state, _ = _operations(self.events, self.initial, lambda key: key[0] <= initial_tick)
            if not _equal(entities, state['entities']):
                fail('SOURCE_STATE', 'incoming native scene disagrees with replay; author an explicit transition and matching source')
        verify_references(self.root, self.plan)
        verify_references(self.root, dict(events=self.events))
        self.dependencies_sha256 = digest(dict(plan=self.plan, events=self.events,
            native_inventory=self.native['inventory']))
        # Changes to the implementation invalidate persisted snapshot caches.
        import importlib.metadata
        import platform
        self.evaluator_sha256 = digest(dict(
            sources={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                     for name in ('continuity_temporal.py', 'continuity_contracts.py',
                                  'scene3d_contracts.py', 'scene3d_math.py', 'rig_math.py')},
            python=platform.python_version(), numpy=importlib.metadata.version('numpy'),
            jsonschema=importlib.metadata.version('jsonschema')))

    def snapshot(self, shot_id, frame, *, boundary='after', event_id=None, head_sha256):
        _integer(frame, 'frame')
        if shot_id not in self.shots:
            fail('MAPPING', 'shot has no timeline mapping')
        shot = self.shots[shot_id]
        duration = shot['native']['duration_frames']
        if frame > duration or boundary not in ('before', 'after', 'terminal'):
            fail('TIME', 'invalid frame/boundary')
        if (frame == duration) != (boundary == 'terminal'):
            fail('TIME', 'frame N requires terminal; displayed frames stop at N-1')
        entry = shot['entry']
        tick = entry['story_start_tick'] + frame * shot['story_step']
        cutoff = lambda key: key[0] < tick if boundary == 'before' else key[0] <= tick
        if event_id is not None:
            matches = [e for e in self.events if e['event_id'] == event_id]
            if len(matches) != 1 or matches[0]['tick'] != tick or boundary == 'terminal':
                fail('TIME', 'event boundary must name an event at this exact frame')
            event_key = _key(matches[0])
            cutoff = lambda key: key < event_key if boundary == 'before' else key <= event_key
        return self._snapshot(cutoff, head_sha256=head_sha256, shot_id=shot_id,
            scene_id=entry['scene_id'], frame=frame, boundary=boundary, event_id=event_id,
            tick=tick, scene_tick=entry['scene_start_tick'] + frame * shot['step'],
            presentation_tick=entry['presentation_start_tick'] + frame * shot['step'])

    def event_snapshot(self, event_id, *, boundary, head_sha256):
        if boundary not in ('before', 'after'):
            fail('TIME', 'event boundary must be before or after')
        matches = [e for e in self.events if e['event_id'] == event_id]
        if len(matches) != 1:
            fail('REFERENCE', 'unknown event')
        event_key = _key(matches[0])
        cutoff = lambda key: key < event_key if boundary == 'before' else key <= event_key
        # Chronological events may occur offscreen during a time skip or appear
        # in several shots. Do not invent one presentation/frame coordinate.
        return self._snapshot(cutoff, head_sha256=head_sha256, shot_id=None,
            scene_id=None, frame=None, boundary=boundary, event_id=event_id,
            tick=matches[0]['tick'], scene_tick=None, presentation_tick=None)

    def _snapshot(self, cutoff, *, head_sha256, shot_id, scene_id, frame, boundary,
                  event_id, tick, scene_tick, presentation_tick):
        state, applied = _operations(self.events, self.initial, cutoff)
        result = dict(schema_version='1.0.0', kind='continuity-temporal-snapshot',
            timeline_id=self.plan['timeline_id'], branch_id=self.plan['branch_id'],
            project_revision=self.plan['project_revision'], world_id=self.plan['world_id'],
            scene_id=scene_id, shot_id=shot_id, frame=frame, boundary=boundary, event_id=event_id,
            story_tick=tick, scene_tick=scene_tick, presentation_tick=presentation_tick,
            ticks_per_second=self.plan['ticks_per_second'], head_sha256=head_sha256,
            dependencies_sha256=self.dependencies_sha256, evaluator_sha256=self.evaluator_sha256,
            entities=[state['entities'][k] for k in sorted(state['entities'])],
            facts=[dict(id=k, value=state['facts'][k], evidence=[]) for k in sorted(state['facts'])],
            applied_events=applied,
            unavailable_fields=['animation', 'physics', 'pose', 'costume', 'prop-ownership', 'visual-approval'])
        return validate_document(result, 'temporal-snapshot')


class TemporalRegistryMixin:
    """Uses IdentityRegistry's connection, transaction and audit history."""
    def _init_temporal(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS temporal_contents (hash TEXT PRIMARY KEY, document BLOB NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS temporal_heads (id TEXT PRIMARY KEY, hash TEXT NOT NULL REFERENCES temporal_contents(hash))')
        self.db.execute('CREATE TABLE IF NOT EXISTS temporal_cache (key TEXT PRIMARY KEY, document BLOB NOT NULL, hash TEXT NOT NULL)')

    def _temporal_record(self, head):
        from continuity_registry import _sha
        _sha(head)
        row = self.db.execute('SELECT document FROM temporal_contents WHERE hash=?', (head,)).fetchone()
        if not row:
            fail('REFERENCE', 'unknown temporal revision')
        record = decode_json(row[0])
        if digest(record) != head or set(record) != {'plan', 'events', 'parents'}:
            fail('HASH', 'corrupted immutable temporal history')
        for parent in record['parents']:
            _sha(parent)
            if not self.db.execute('SELECT 1 FROM temporal_contents WHERE hash=?', (parent,)).fetchone():
                fail('REFERENCE', 'temporal parent history missing')
        return record

    def temporal_history(self, timeline_id, *, revision=None):
        from continuity_registry import _id
        _id(timeline_id)
        row = self.db.execute('SELECT hash FROM temporal_heads WHERE id=?', (timeline_id,)).fetchone()
        if not row:
            fail('REFERENCE', 'unknown temporal timeline')
        head = row[0] if revision is None else revision
        record = self._temporal_record(head)
        pending, visited = list(record['parents']), {head}
        while pending:
            ancestor = pending.pop()
            if ancestor in visited:
                continue
            if len(visited) >= 10000:
                fail('LIMIT', 'temporal history exceeds limit')
            visited.add(ancestor)
            pending.extend(self._temporal_record(ancestor)['parents'])
        if record['plan']['timeline_id'] != timeline_id:
            fail('REFERENCE', 'revision belongs to another timeline')
        return dict(sha256=head, **record)

    def _validate_temporal_bindings(self, plan):
        # A project with canonical sidecar bindings already has an authority for
        # shot-instance/entity identity. Temporal mappings must agree with it.
        if not self.db.execute('SELECT 1 FROM projects WHERE id=?', (plan['project_id'],)).fetchone():
            return
        bindings = self.bindings(plan['project_id'])['document']
        scenes = {s['scene']['id']: s for s in plan['scenes']}
        for shot in plan['shots']:
            mapped = {e['node_id']: e for e in scenes[shot['scene_id']]['entities']}
            for instance in bindings['instances']:
                if instance['shot_id'] != shot['shot']['id']:
                    continue
                entity = mapped.get(instance['instance_id'])
                if (entity is None or entity['entity_id'] != instance['entity_id'] or
                        entity['identity_id'] != instance['revision']['identity_id']):
                    fail('IDENTITY', 'temporal mapping disagrees with authoritative project bindings')

    def _temporal_commit(self, record, expected):
        plan = record['plan']
        identifier = plan['timeline_id']
        current = self.db.execute('SELECT hash FROM temporal_heads WHERE id=?', (identifier,)).fetchone()
        if (current[0] if current else None) != expected:
            fail('CONFLICT', 'stale temporal revision')
        # Verify persistent identity binding where one is declared. The native
        # scene/node mapping remains explicit; no new identity is minted here.
        for scene in plan['scenes']:
            for binding in scene['entities']:
                if binding['identity_id'] is not None:
                    self.identity(binding['identity_id'])
        self._validate_temporal_bindings(plan)
        TemporalEngine(self.asset_root, record)
        checksum = digest(record)
        self._limit('temporal_contents')
        self.db.execute('INSERT OR IGNORE INTO temporal_contents VALUES (?,?)', (checksum, canonical_bytes(record)))
        self.db.execute('INSERT INTO temporal_heads VALUES (?,?) ON CONFLICT(id) DO UPDATE SET hash=excluded.hash', (identifier, checksum))
        self._history('temporal-commit', identifier, dict(before_sha256=expected, after_sha256=checksum, parents=record['parents']))
        return checksum

    def create_timeline(self, plan):
        plan = validate_document(plan, 'temporal-timeline')
        with self._transaction():
            return self._temporal_commit(dict(plan=plan, events=[], parents=[]), None)

    def append_events(self, timeline_id, events, *, expected_sha256):
        if not isinstance(events, list) or not events:
            fail('EVENT', 'append requires a nonempty event batch')
        events = [validate_document(e, 'temporal-event') for e in events]
        with self._transaction():
            previous = self.temporal_history(timeline_id)
            record = dict(plan=previous['plan'], events=previous['events'] + events, parents=[previous['sha256']])
            return self._temporal_commit(record, expected_sha256)

    def fork_timeline(self, timeline_id, new_id, branch_id, *, expected_sha256):
        from continuity_registry import _id
        _id(new_id); _id(branch_id)
        with self._transaction():
            previous = self.temporal_history(timeline_id)
            if previous['sha256'] != expected_sha256:
                fail('CONFLICT', 'stale fork source')
            plan = deepcopy(previous['plan'])
            plan.update(timeline_id=new_id, branch_id=branch_id)
            return self._temporal_commit(dict(plan=plan, events=previous['events'], parents=[previous['sha256']]), None)

    def merge_timeline(self, target_id, source_id, *, expected_sha256, source_sha256):
        with self._transaction():
            target, source = self.temporal_history(target_id), self.temporal_history(source_id)
            if target['sha256'] != expected_sha256 or source['sha256'] != source_sha256:
                fail('CONFLICT', 'stale branch merge')
            tp, sp = deepcopy(target['plan']), deepcopy(source['plan'])
            for plan in (tp, sp):
                plan.pop('timeline_id'); plan.pop('branch_id')
            if tp != sp:
                fail('CONFLICT', 'branches use incompatible worlds or mappings')
            # Shared immutable IDs must have identical payloads. Unique suffixes
            # can be interleaved only when they do not compete for a state field.
            te = {e['event_id']: e for e in target['events']}
            se = {e['event_id']: e for e in source['events']}
            shared = te.keys() & se.keys()
            if any(te[k] != se[k] for k in shared):
                fail('CONFLICT', 'branch event ID has incompatible content')
            left = [e for e in target['events'] if e['event_id'] not in shared]
            right = [e for e in source['events'] if e['event_id'] not in shared]
            if {_target(o) for e in left for o in e['operations']} & {_target(o) for e in right for o in e['operations']}:
                fail('CONFLICT', 'branches write the same field; explicit authored reconciliation required')
            # Require real shared ancestry, not merely lookalike plans/events.
            def ancestry(head):
                pending, seen = [head], set()
                while pending:
                    current = pending.pop()
                    if current in seen:
                        continue
                    if len(seen) >= 10000:
                        fail('LIMIT', 'temporal ancestry exceeds limit')
                    seen.add(current)
                    pending.extend(self._temporal_record(current)['parents'])
                return seen
            if not ancestry(target['sha256']) & ancestry(source['sha256']):
                fail('CONFLICT', 'branches have no common immutable ancestor')
            events = sorted(list(te.values()) + right, key=_key)
            return self._temporal_commit(dict(plan=target['plan'], events=events,
                parents=[target['sha256'], source['sha256']]), expected_sha256)

    def temporal_snapshot(self, timeline_id, shot_id, frame, *, boundary='after', event_id=None,
                          revision=None, use_cache=True):
        record = self.temporal_history(timeline_id, revision=revision)
        self._validate_temporal_bindings(record['plan'])
        engine = TemporalEngine(self.asset_root, record)
        result = engine.snapshot(shot_id, frame, boundary=boundary, event_id=event_id, head_sha256=record['sha256'])
        checksum = digest(result)
        key = digest(dict(head=record['sha256'], shot=shot_id, frame=frame, boundary=boundary, event=event_id))
        if use_cache:
            with self._transaction():
                cached = self.db.execute('SELECT document,hash FROM temporal_cache WHERE key=?', (key,)).fetchone()
                if cached:
                    if cached[1] != checksum or cached[0] != canonical_bytes(result):
                        fail('STALE_CACHE', 'cached snapshot disagrees with fresh native replay')
                else:
                    self._limit('temporal_cache')
                    self.db.execute('INSERT INTO temporal_cache VALUES (?,?,?)', (key, canonical_bytes(result), checksum))
        return dict(snapshot=result, sha256=checksum)

    def temporal_event_snapshot(self, timeline_id, event_id, *, boundary='after', revision=None):
        record = self.temporal_history(timeline_id, revision=revision)
        self._validate_temporal_bindings(record['plan'])
        engine = TemporalEngine(self.asset_root, record)
        result = engine.event_snapshot(event_id, boundary=boundary, head_sha256=record['sha256'])
        return dict(snapshot=result, sha256=digest(result))

    def temporal_scene_boundary(self, timeline_id, scene_id, boundary, *, revision=None):
        if boundary not in ('start', 'end'):
            fail('TIME', 'scene boundary must be start or end')
        record = self.temporal_history(timeline_id, revision=revision)
        engine = TemporalEngine(self.asset_root, record)
        shots = [s for s in engine.shots.values() if s['entry']['scene_id'] == scene_id]
        if not shots:
            fail('MAPPING', 'scene has no shot mappings')
        if boundary == 'start':
            shot = min(shots, key=lambda s: s['entry']['scene_start_tick'])
            frame, mode = 0, 'before'
        else:
            shot = max(shots, key=lambda s: s['entry']['scene_start_tick'] + s['step'] * s['native']['duration_frames'])
            frame, mode = shot['native']['duration_frames'], 'terminal'
        return self.temporal_snapshot(timeline_id, shot['native']['id'], frame,
                                      boundary=mode, revision=record['sha256'])

    def discard_temporal_cache(self):
        """Recovery removes only derived rows; immutable events remain intact."""
        with self._transaction():
            self.db.execute('DELETE FROM temporal_cache')


def main(argv=None):
    """Small local CLI over the same registry APIs; JSON in/out, no server."""
    import argparse
    import json
    from continuity_registry import IdentityRegistry
    from continuity_contracts import MAX_BYTES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--asset-root', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create'); create.add_argument('plan', type=Path)
    append = commands.add_parser('append'); append.add_argument('timeline'); append.add_argument('events', type=Path); append.add_argument('--expected', required=True)
    fork = commands.add_parser('fork'); fork.add_argument('timeline'); fork.add_argument('new_id'); fork.add_argument('branch'); fork.add_argument('--expected', required=True)
    merge = commands.add_parser('merge'); merge.add_argument('target'); merge.add_argument('source'); merge.add_argument('--expected', required=True); merge.add_argument('--source-head', required=True)
    history = commands.add_parser('history'); history.add_argument('timeline'); history.add_argument('--revision')
    snapshot = commands.add_parser('snapshot'); snapshot.add_argument('timeline'); snapshot.add_argument('shot'); snapshot.add_argument('frame', type=int); snapshot.add_argument('--boundary', choices=['before','after','terminal'], default='after'); snapshot.add_argument('--event'); snapshot.add_argument('--revision'); snapshot.add_argument('--no-cache', action='store_true')
    event_parser = commands.add_parser('event-snapshot'); event_parser.add_argument('timeline'); event_parser.add_argument('event'); event_parser.add_argument('--boundary', choices=['before','after'], default='after'); event_parser.add_argument('--revision')
    scene = commands.add_parser('scene-boundary'); scene.add_argument('timeline'); scene.add_argument('scene'); scene.add_argument('boundary', choices=['start','end']); scene.add_argument('--revision')
    commands.add_parser('discard-cache')
    args = parser.parse_args(argv)
    def read(path):
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            fail('LIMIT', 'CLI input exceeds 8 MiB')
        return decode_json(data)
    try:
        with IdentityRegistry(args.registry, asset_root=args.asset_root) as registry:
            if args.command == 'create': result = dict(sha256=registry.create_timeline(read(args.plan)))
            elif args.command == 'append': result = dict(sha256=registry.append_events(args.timeline, read(args.events), expected_sha256=args.expected))
            elif args.command == 'fork': result = dict(sha256=registry.fork_timeline(args.timeline,args.new_id,args.branch,expected_sha256=args.expected))
            elif args.command == 'merge': result = dict(sha256=registry.merge_timeline(args.target,args.source,expected_sha256=args.expected,source_sha256=args.source_head))
            elif args.command == 'history': result = registry.temporal_history(args.timeline,revision=args.revision)
            elif args.command == 'snapshot': result = registry.temporal_snapshot(args.timeline,args.shot,args.frame,boundary=args.boundary,event_id=args.event,revision=args.revision,use_cache=not args.no_cache)
            elif args.command == 'event-snapshot': result = registry.temporal_event_snapshot(args.timeline,args.event,boundary=args.boundary,revision=args.revision)
            elif args.command == 'scene-boundary': result = registry.temporal_scene_boundary(args.timeline,args.scene,args.boundary,revision=args.revision)
            else: registry.discard_temporal_cache(); result = dict(discarded='derived temporal cache')
        print(json.dumps(result,sort_keys=True,allow_nan=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps(dict(error=str(exc)),sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
