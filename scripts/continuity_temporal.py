"""Exact, deterministic temporal replay in the existing continuity sidecar.

Native Scene3D files remain authoritative for identities and initial transforms.
This engine persists immutable authored events, not a competing scene database.
Snapshots are always derived and caches are checked against a fresh replay.
"""
from __future__ import annotations

from sys import _getframe as _loaded_module_frame

_EVALUATOR_MODULE_CODE = _loaded_module_frame().f_code
del _loaded_module_frame

from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
from importlib import import_module, metadata
from pathlib import Path
from types import CodeType
import hashlib
import marshal
import math
import platform
import sqlite3

from continuity_contracts import (MAX_BYTES, MAX_ITEMS, canonical_bytes, decode_json, definitions, digest,
    validate_document, verify_references)

MAX_EVENTS = 4096
ENGINE_VERSION = 'continuity-temporal-v1'
EVALUATOR_SOURCES = (
    'continuity_temporal.py', 'continuity_contracts.py', 'scene3d_contracts.py',
    'scene3d_graph.py', 'scene3d_math.py', 'rig_math.py', 'rig_errors.py',
    'portable_paths.py', 'production_contracts.py', 'asset_contracts.py',
)


def _portable_constant(value):
    if isinstance(value, CodeType):
        # Source filenames may differ between workers with the same code.
        return value.replace(co_filename='<loaded evaluator>',
                             co_consts=tuple(map(_portable_constant, value.co_consts)))
    if isinstance(value, slice):
        # Compiled subscripts can contain slice literals on newer Python
        # versions. Marshal format 2 does not support slice objects.
        return ('slice', tuple(map(_portable_constant, (value.start, value.stop, value.step))))
    if isinstance(value, tuple):
        # Frame real tuple literals so they cannot collide with slice encoding.
        return ('tuple', tuple(map(_portable_constant, value)))
    if isinstance(value, frozenset):
        return ('frozenset', frozenset(map(_portable_constant, value)))
    return value


@lru_cache(maxsize=1)
def _evaluator_fingerprint():
    import numpy
    modules = {}
    for filename in EVALUATOR_SOURCES:
        loaded = import_module(filename.removesuffix('.py'))._EVALUATOR_MODULE_CODE
        # Marshal without reference or interning tags so process import order
        # cannot change the digest of equivalent compiled code.
        modules[filename] = hashlib.sha256(marshal.dumps(_portable_constant(loaded), 2)).hexdigest()
    return digest(dict(format='loaded-evaluator-v2', modules=modules,
                       python=platform.python_version(), numpy=numpy.__version__,
                       jsonschema=metadata.version('jsonschema')))


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


def _same_transform(a, b, *, native, floor=None, relative=None, conditioned=None):
    """Compare validated TRS, allowing roundoff only at a native matrix boundary.

    ``floor`` is the per-axis absolute translation bound recorded when the
    native value was composed; cancellation leaves a residual proportional to
    the magnitudes that cancelled, not to the result. ``relative`` bounds the
    composition depth; ``conditioned`` additionally includes the world-scale
    conditioning, which affects only the decomposed scale and rotation.
    """
    unit = 8 * math.ulp(1.0)
    base = max(unit, relative or 0.) if native else unit
    roundoff = max(base, conditioned or 0.) if native else unit
    floor = floor or (0., 0., 0.)
    if native:
        if not all(math.isclose(x, y, rel_tol=base, abs_tol=max(unit, bound))
                   for x, y, bound in zip(a['translation'], b['translation'], floor)):
            return False
        if not all(math.isclose(x, y, rel_tol=roundoff, abs_tol=0.)
                   for x, y in zip(a['scale'], b['scale'])):
            return False
    elif not (_equal(a['translation'], b['translation']) and _equal(a['scale'], b['scale'])):
        return False
    if not native:
        # Compare exact ratios of the authored components before normalization.
        # A common nonzero scale, including a sign flip, preserves rotation.
        rotation_a = [Fraction(x) for x in a['rotation']]
        rotation_b = [Fraction(x) for x in b['rotation']]
        pivot = next(i for i, x in enumerate(rotation_a) if x)
        return all(x * rotation_b[pivot] == y * rotation_a[pivot]
                   for x, y in zip(rotation_a, rotation_b))

    # Native decompositions may differ by roundoff or reverse all components.
    length_a, length_b = math.hypot(*a['rotation']), math.hypot(*b['rotation'])
    rotation_a = [x / length_a for x in a['rotation']]
    rotation_b = [x / length_b for x in b['rotation']]
    return any(all(abs(x - sign * y) <= roundoff for x, y in zip(rotation_a, rotation_b))
               for sign in (1, -1))


def _native_bounds(scene, scene_to_world):
    """Per-node roundoff bounds for values composed from native matrices.

    Mirrors evaluate_transforms with norm bounds rather than entrywise values:
    a rotation's computed zero entries carry ulp-sized error, and entrywise
    products of rotations grow exponentially with depth. For each level the
    linear part has spectral norm at most max|scale|; the effective local
    translation t + p - R S p is bounded by |t| + |p| + max|scale| |p|.
    Returns {node: (translation floor per axis, base relative tolerance,
    composed max-scale product)}; ``_seed_bound`` applies the conditioning of
    the decomposed world scale. Non-finite bounds fail closed.
    """
    nodes = {node['id']: node for node in scene['nodes']}
    def local(transform):
        largest = max(abs(float(v)) for v in transform['scale'])
        magnitude = math.hypot(*map(float, transform['translation']))
        if 'pivot' in transform:
            magnitude += (1 + largest) * math.hypot(*map(float, transform['pivot']))
        return largest, magnitude
    chains = {}
    def visit(identifier):
        if identifier not in chains:
            node = nodes[identifier]
            largest, magnitude = local(node['transform'])
            if node['parent'] is None:
                chains[identifier] = (largest, magnitude, 1)
            else:
                scale, total, depth = visit(node['parent'])
                chains[identifier] = (scale * largest, total + scale * magnitude, depth + 1)
        return chains[identifier]
    outer_scale, outer_magnitude = local(scene_to_world)
    bounds = {}
    for identifier in nodes:
        scale, total, depth = visit(identifier)
        levels = depth + 1
        floor = 8 * math.sqrt(3) * (levels + 1) * math.ulp(1.0) * (outer_magnitude + outer_scale * total)
        product = outer_scale * scale
        bounds[identifier] = ([floor] * 3, 8 * (levels + 1) * math.ulp(1.0), product)
    return bounds


MAX_CONDITIONED_ROUNDOFF = 1e-6


def _seed_bound(bound, transform):
    """Scale and rotation error grow with the conditioning of the world scale."""
    floor, relative, product = bound
    smallest = min(abs(float(value)) for value in transform['scale'])
    conditioned = relative * max(1.0, product / smallest)
    # Only mapped nodes are checked; non-finite bounds fail closed. Beyond the
    # orthonormality tolerance decompose already enforces, the conditioned
    # bound would no longer distinguish rotations or scales, so refuse it.
    if not all(math.isfinite(value) for value in (*floor, product, conditioned)):
        fail('LIMIT', 'native transform magnitudes exceed representable roundoff bounds')
    if conditioned > MAX_CONDITIONED_ROUNDOFF:
        fail('LIMIT', 'native world scale is too ill-conditioned to compare decomposed rotation and scale')
    return floor, relative, conditioned


def _bound_arguments(bound):
    return {} if bound is None else dict(floor=bound[0], relative=bound[1], conditioned=bound[2])


def _combined_bound(incoming, initial):
    """Both sides may be native: add their floors and relative bounds."""
    if initial is None:
        return _bound_arguments(incoming)
    return dict(floor=[a + b for a, b in zip(incoming[0], initial[0])], relative=incoming[1] + initial[1],
                conditioned=incoming[2] + initial[2])


def _operations(events, initial, until=None, floors=None):
    """Replay from source; no mutable evaluator state survives a query."""
    state = deepcopy(initial)
    applied, previous = [], None
    relocated = set()
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
                if field not in container and len(container) >= MAX_ITEMS:
                    fail('LIMIT', 'snapshot fact count exceeds the continuity contract')
                current = container.get(field)
            else:
                entity = operation['entity_id']
                if entity not in state['entities']:
                    fail('REFERENCE', 'event targets an unmapped native entity')
                container = state['entities'][entity]
                field = 'transform' if kind == 'relocate' else 'visible'
                current = container[field]
            if kind == 'relocate':
                matches = _same_transform(current, operation['before'], native=entity not in relocated,
                                          **_bound_arguments((floors or {}).get(entity)))
                relocated.add(entity)
            else:
                matches = _equal(current, operation['before'])
            if not matches:
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
        sources = dict(plan=self.plan, events=self.events)
        verify_references(self.root, sources)
        from scene3d_contracts import load_project, frame_time
        from scene3d_graph import SceneGraph
        from scene3d_math import evaluate_transforms, local_matrix
        from rig_math import decompose
        from jsonschema import Draft202012Validator
        entity_validator = Draft202012Validator({
            '$defs': definitions(), '$ref': '#/$defs/temporal-entity',
        })
        # The native closure loader verifies actual project, scene and asset
        # identity/version/hash relationships, including transitive payloads.
        project = self.plan['project_revision']
        if project['kind'] != 'scene3d-project':
            fail('REFERENCE', 'temporal project must reference native Scene3D')
        project_path = Path(project['path'])
        self.native = load_project(self.root / project_path.parent, project_path.name)
        # Resolve timeline selections from the same admitted bytes as the inventory.
        if self.native['inventory'][project_path.name]['sha256'] != project['sha256']:
            fail('REFERENCE', 'loaded native project digest mismatch')
        project_doc = self.native['project']
        if (project_doc['id'], project_doc['version'], project_doc['kind']) != (project['id'], project['version'], project['kind']):
            fail('REFERENCE', 'project reference identity mismatch')
        # Native child paths are relative to the native project directory;
        # timeline pointers are relative to the continuity asset root. Preserve
        # exact identity/hash binding while comparing in the same path space.
        project_directory = project_path.parent
        def rooted(reference):
            return dict(reference, path=(project_directory / reference['path']).as_posix())
        known_scenes = {digest(rooted(r)): r['path'] for r in project_doc['scenes']}
        known_shots = {digest(rooted(r)): r['path'] for r in project_doc['shots']}
        scenes, scene_references, seeds, mapped_ancestors = {}, {}, {}, {}
        mapped_parents = set()
        floors = {}
        import numpy
        for entry in self.plan['scenes']:
            reference = entry['scene']
            native_path = known_scenes.get(digest(reference))
            if native_path is None:
                fail('REFERENCE', 'scene is not in native project')
            scene = self.native['documents'][native_path]
            if scene['id'] in scenes:
                fail('DUPLICATE', 'scene mapping repeated')
            evaluated = evaluate_transforms(scene)
            # Shot evaluation exposes render visibility, including inherited
            # collection and layer flags, through its single visible field.
            graph_state = SceneGraph(scene).evaluate_base()
            matrix = local_matrix(entry['scene_to_world'])
            node_bounds = _native_bounds(scene, entry['scene_to_world'])
            scene_floors = {}
            entities, nodes = {}, set()
            for binding in entry['entities']:
                entity, node = binding['entity_id'], binding['node_id']
                if entity in entities or node in nodes:
                    fail('DUPLICATE', 'entity/node mapping must be one-to-one within a scene')
                nodes.add(node)
                if node not in evaluated:
                    fail('MAPPING', 'node has no native evaluated state')
                result = evaluated[node]
                world = numpy.array(result['world_matrix'])
                transform = decompose(matrix @ world)
                scene_floors[entity] = _seed_bound(node_bounds[node], transform)
                state = dict(entity_id=entity, identity_id=binding['identity_id'],
                    transform={k: transform[k] for k in ('translation', 'rotation', 'scale')},
                    visible=graph_state[node]['render_visible'])
                if not entity_validator.is_valid(state):
                    fail('SOURCE_STATE', 'composed native entity state exceeds the snapshot contract')
                entities[entity] = state
            scene_nodes = {node['id']: node for node in scene['nodes']}
            ancestors = {}
            for node in nodes:
                while node is not None and node not in ancestors:
                    ancestors[node] = scene_nodes[node]
                    node = scene_nodes[node]['parent']
            mapped_ancestors[scene['id']] = ancestors
            parent_nodes = {node['parent'] for node in ancestors.values()}
            mapped_parents.update(binding['entity_id'] for binding in entry['entities']
                                  if binding['node_id'] in parent_nodes)
            scenes[scene['id']] = scene
            scene_references[scene['id']] = reference
            seeds[scene['id']] = entities
            floors[scene['id']] = scene_floors
        self.shots = {}
        last_end = -1
        scene_intervals = {}
        for entry in self.plan['shots']:
            reference = entry['shot']
            native_path = known_shots.get(digest(reference))
            if native_path is None:
                fail('REFERENCE', 'shot is not in native project')
            shot = self.native['documents'][native_path]
            if shot['id'] in self.shots:
                fail('DUPLICATE', 'shot mapping repeated')
            scene_id = entry['scene_id']
            if scene_id not in scenes or rooted(shot['scene']) != scene_references[scene_id]:
                fail('MAPPING', 'shot scene mapping unresolved')
            if shot['tracks'] or shot['bindings'] or shot['temporal_constraints']:
                fail('ADAPTER_REQUIRED', 'dynamic shot needs an admitted native evaluator')
            camera = mapped_ancestors[scene_id].get(shot['camera'])
            override = shot['camera_overrides'].get('transform')
            if (camera is not None and override is not None and
                    not (local_matrix(override) == local_matrix(camera['transform'])).all()):
                fail('ADAPTER_REQUIRED', 'shot camera transform affects a mapped entity')
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
            _integer(interval[1], 'scene end')
            if any(interval[0] < b and a < interval[1] for a,b in scene_intervals.get(scene_id, [])):
                fail('MAPPING', 'scene shot intervals overlap')
            scene_intervals.setdefault(scene_id, []).append(interval)
            self.shots[shot['id']] = dict(entry=entry, native=shot, step=step, story_step=story_step)
        first = min(self.shots.values(), key=lambda s: s['entry']['story_start_tick'])
        if first['entry']['story_start_tick'] != 0:
            fail('MAPPING', 'initial native state must be anchored at story tick zero')
        self.initial = dict(entities=seeds[first['entry']['scene_id']], facts={})
        self.initial_floors = floors[first['entry']['scene_id']]
        for scene_id, entities in seeds.items():
            if set(entities) != set(self.initial['entities']):
                fail('MAPPING', 'every scene must explicitly map the persistent entity set')
            if any(entities[k]['identity_id'] != self.initial['entities'][k]['identity_id'] for k in entities):
                fail('IDENTITY', 'scene changes persistent logical identity')
        self.max_tick = max(s['entry']['story_start_tick'] + s['story_step'] * s['native']['duration_frames'] for s in self.shots.values())
        _integer(self.max_tick, 'story end')
        if any(e['tick'] > self.max_tick for e in self.events):
            fail('MAPPING', 'event falls outside mapped story time')
        for event in self.events:
            for operation in event['operations']:
                if operation.get('entity_id') not in mapped_parents:
                    continue
                before, after = operation['before'], operation['after']
                unchanged = (_same_transform(before, after, native=False)
                             if operation['event_type'] == 'relocate' else _equal(before, after))
                if not unchanged:
                    fail('ADAPTER_REQUIRED', 'event changes a native ancestor of another mapped entity')
        _operations(self.events, self.initial, floors=self.initial_floors)
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
            state, applied = _operations(self.events, self.initial, lambda key: key[0] <= initial_tick,
                                         floors=self.initial_floors)
            applied = set(applied)
            # After an authored relocation the replayed transform is exact data.
            moved = {operation['entity_id'] for event in self.events if event['event_id'] in applied
                     for operation in event['operations'] if operation['event_type'] == 'relocate'}
            if any(native['visible'] != state['entities'][entity]['visible'] or
                   not _same_transform(native['transform'], state['entities'][entity]['transform'], native=True,
                                       **_combined_bound(floors[scene_id][entity],
                                                         None if entity in moved else self.initial_floors.get(entity)))
                   for entity, native in entities.items()):
                fail('SOURCE_STATE', 'incoming native scene disagrees with replay; author an explicit transition and matching source')
        verify_references(self.root, sources)
        dependencies = dict(sources, native_inventory=self.native['inventory'])
        if manifest.get('bindings_sha256') is not None:
            dependencies['bindings_sha256'] = manifest['bindings_sha256']
        self.dependencies_sha256 = digest(dependencies)
        self.evaluator_sha256 = _evaluator_fingerprint()

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
        state, applied = _operations(self.events, self.initial, cutoff, floors=self.initial_floors)
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
        self.db.execute('CREATE TABLE IF NOT EXISTS temporal_bindings (hash TEXT PRIMARY KEY, document BLOB NOT NULL)')

    def _temporal_record(self, head):
        from continuity_registry import _sha
        _sha(head)
        row = self.db.execute('SELECT document FROM temporal_contents WHERE hash=?', (head,)).fetchone()
        if not row:
            fail('REFERENCE', 'unknown temporal revision')
        record = decode_json(row[0])
        fields = {'plan', 'events', 'parents'}
        if digest(record) != head or set(record) not in (fields, fields | {'bindings_sha256'}):
            fail('HASH', 'corrupted immutable temporal history')
        if record.get('bindings_sha256') is not None:
            _sha(record['bindings_sha256'])
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

    def _capture_temporal_bindings(self, plan):
        if not self.db.execute('SELECT 1 FROM projects WHERE id=?', (plan['project_id'],)).fetchone():
            return
        bindings = self.bindings(plan['project_id'])
        checksum = bindings['sha256']
        payload = canonical_bytes(bindings['document'])
        if len(payload) > MAX_BYTES:
            fail('LIMIT', 'temporal bindings exceed byte limit')
        if not self.db.execute('SELECT 1 FROM temporal_bindings WHERE hash=?', (checksum,)).fetchone():
            self._limit('temporal_bindings')
            self.db.execute('INSERT INTO temporal_bindings VALUES (?,?)', (checksum, payload))
        return checksum

    def _validate_temporal_bindings(self, record):
        plan = record['plan']
        declared = {binding['identity_id'] for scene in plan['scenes']
                    for binding in scene['entities'] if binding['identity_id'] is not None}
        for identity_id in sorted(declared):
            self.identity(identity_id)
        checksum = record.get('bindings_sha256')
        if checksum is None:
            return
        row = self.db.execute('SELECT document FROM temporal_bindings WHERE hash=?', (checksum,)).fetchone()
        if not row:
            fail('REFERENCE', 'temporal bindings missing')
        if len(row[0]) > MAX_BYTES or hashlib.sha256(row[0]).hexdigest() != checksum:
            fail('HASH', 'corrupted temporal bindings')
        bindings = validate_document(decode_json(row[0]), 'bindings')
        if canonical_bytes(bindings) != row[0]:
            fail('HASH', 'corrupted temporal bindings')
        if (bindings['project_id'] != plan['project_id'] or
                bindings['project_revision'] != plan['project_revision']):
            fail('REFERENCE', 'temporal bindings disagree with exact project revision')
        self._validate_pins(bindings, allow_deprecated=True)
        verify_references(self.asset_root, bindings)
        scenes = {s['scene']['id']: s for s in plan['scenes']}
        for shot in plan['shots']:
            scene = scenes.get(shot['scene_id'])
            if scene is None:
                fail('MAPPING', 'shot scene mapping unresolved')
            mapped = {e['node_id']: e for e in scene['entities']}
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
        self._validate_temporal_bindings(record)
        TemporalEngine(self.asset_root, record)
        checksum = digest(record)
        self._limit('temporal_contents')
        self.db.execute('INSERT OR IGNORE INTO temporal_contents VALUES (?,?)', (checksum, canonical_bytes(record)))
        self.db.execute('INSERT INTO temporal_heads VALUES (?,?) ON CONFLICT(id) DO UPDATE SET hash=excluded.hash', (identifier, checksum))
        self._history('temporal-commit', identifier, dict(before_sha256=expected, after_sha256=checksum, parents=record['parents']))
        return checksum

    def create_timeline(self, plan, *, events=None):
        plan = validate_document(plan, 'temporal-timeline')
        if events is None:
            events = []
        if not isinstance(events, list):
            fail('EVENT', 'initial events require a list')
        if len(events) > MAX_EVENTS:
            fail('LIMIT', 'too many initial temporal events')
        events = [validate_document(e, 'temporal-event') for e in events]
        # Incoming native scene state and its authored transitions must be
        # admitted together; no invalid empty intermediate timeline is stored.
        with self._transaction():
            return self._temporal_commit(dict(plan=plan, events=events, parents=[],
                bindings_sha256=self._capture_temporal_bindings(plan)), None)

    def append_events(self, timeline_id, events, *, expected_sha256):
        if not isinstance(events, list) or not events:
            fail('EVENT', 'append requires a nonempty event batch')
        events = [validate_document(e, 'temporal-event') for e in events]
        with self._transaction():
            previous = self.temporal_history(timeline_id)
            record = dict(plan=previous['plan'], events=previous['events'] + events,
                parents=[previous['sha256']], bindings_sha256=previous.get('bindings_sha256'))
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
            return self._temporal_commit(dict(plan=plan, events=previous['events'],
                parents=[previous['sha256']], bindings_sha256=previous.get('bindings_sha256')), None)

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
            if target.get('bindings_sha256') != source.get('bindings_sha256'):
                fail('CONFLICT', 'branches use incompatible project bindings')
            # Shared immutable IDs must have identical payloads. Unique suffixes
            # can be interleaved only when they do not compete for a state field.
            te = {e['event_id']: e for e in target['events']}
            se = {e['event_id']: e for e in source['events']}
            shared = te.keys() & se.keys()
            if any(canonical_bytes(te[k]) != canonical_bytes(se[k]) for k in shared):
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
                parents=[target['sha256'], source['sha256']],
                bindings_sha256=target.get('bindings_sha256')), expected_sha256)

    def temporal_snapshot(self, timeline_id, shot_id, frame, *, boundary='after', event_id=None,
                          revision=None, use_cache=True):
        record = self.temporal_history(timeline_id, revision=revision)
        self._validate_temporal_bindings(record)
        engine = TemporalEngine(self.asset_root, record)
        result = engine.snapshot(shot_id, frame, boundary=boundary, event_id=event_id, head_sha256=record['sha256'])
        checksum = digest(result)
        key = digest(dict(head=record['sha256'], shot=shot_id, frame=frame, boundary=boundary, event=event_id))
        if use_cache:
            encoded = canonical_bytes(result)
            inserting = False
            try:
                with self._transaction():
                    cached = self.db.execute('SELECT document,hash FROM temporal_cache WHERE key=?', (key,)).fetchone()
                    if cached:
                        if cached != (encoded, checksum):
                            fail('STALE_CACHE', 'cached snapshot disagrees with fresh native replay')
                    elif self._has_table_room('temporal_cache'):
                        inserting = True
                        self.db.execute('INSERT INTO temporal_cache VALUES (?,?,?)', (key, encoded, checksum))
            except sqlite3.OperationalError as exc:
                # A budget failure while storing a cache miss leaves the fresh
                # result usable. The transaction rolls back any partial insert.
                if not inserting or getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                    raise
        return dict(snapshot=result, sha256=checksum)

    def temporal_event_snapshot(self, timeline_id, event_id, *, boundary='after', revision=None):
        record = self.temporal_history(timeline_id, revision=revision)
        self._validate_temporal_bindings(record)
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
    from continuity_contracts import MAX_BYTES, decode_temporal_input
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--asset-root', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create'); create.add_argument('plan', type=Path); create.add_argument('--events', type=Path)
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
        return decode_temporal_input(data)
    try:
        with IdentityRegistry(args.registry, asset_root=args.asset_root) as registry:
            if args.command == 'create': result = dict(sha256=registry.create_timeline(read(args.plan),events=read(args.events) if args.events is not None else None))
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
