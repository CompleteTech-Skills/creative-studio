"""Cinematic authoring contracts and deterministic admission, never pixel execution.

Existing camera, scene, rig, look-development and rendering owners retain their
semantics. The registry declares requirements; only a separately probed backend
can report execution support. No renderer or numerical package is imported.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from lookdev_contracts import canonical_bytes, portable_path
from camera3d_contracts import ID, VERSION, SHA, PATH, obj, arr, enum, integer
from rig_errors import RigError, require, finite_json, decode_json

KINDS = ('effect', 'stack', 'shot', 'scene', 'preset', 'render', 'graph')
STAGES = ('scene', 'lens', 'linear-composite', 'grade', 'display', 'delivery')
MAX_BYTES = 2 * 1024 * 1024
TEXT = dict(type='string', minLength=1, maxLength=4096)
BOOL = dict(type='boolean')
SCOPES = ('scene', 'shot', 'object')


def number(low, high):
    return dict(type='number', minimum=low, maximum=high)


def parameter(schema, default, unit):
    return dict(schema=schema, default=default, unit=unit)


# Registry values describe v1 contracts, not backend capabilities. A new meaning
# or parameter bound requires a new registry version; old documents retain v1.
def _registry_v1():
    f = lambda lo, hi, value, unit='dimensionless': parameter(number(lo, hi), value, unit)
    b = lambda value: parameter(BOOL, value, 'boolean')
    effect = lambda stage, params, passes=(), scopes=('scene', 'shot'): dict(
        stage=stage, scopes=list(scopes), parameters=params, required_passes=list(passes),
        backend_feature='cinematics.', default_status='unavailable',
        fallback='reject-unless-explicit-approximation', executed=False)
    out = {
        'depth-of-field': effect('lens', {'enabled': b(True)}, ('depth',)),
        'motion-blur': effect('scene', {'shutter_frames': f(0, 2, .5, 'frames'),
                                      'samples': parameter(integer(1, 256), 16, 'count')}, ('vector',)),
        'exposure': effect('grade', {'stops': f(-24, 24, 0, 'stops')}),
        'tone-map': effect('display', {'view': parameter(enum('standard', 'agx', 'filmic'), 'agx', 'view-transform')}),
        'color-grade': effect('grade', {'contrast': f(0, 4, 1), 'saturation': f(0, 4, 1),
                                      'lift': f(-1, 1, 0), 'gamma': f(.01, 10, 1), 'gain': f(0, 10, 1)}),
        'lut': effect('grade', {'strength': f(0, 1, 1)}),
        'bloom': effect('linear-composite', {'threshold': f(0, 100000, 1, 'scene-linear'), 'strength': f(0, 10, .1)}),
        'glow': effect('linear-composite', {'threshold': f(0, 100000, 1, 'scene-linear'), 'radius_pixels': f(0, 4096, 16, 'pixels')}),
        'glare': effect('linear-composite', {'threshold': f(0, 100000, 1, 'scene-linear'), 'streaks': parameter(integer(2, 16), 4, 'count')}),
        'halation': effect('linear-composite', {'strength': f(0, 1, .1)}),
        'vignette': effect('display', {'strength': f(0, 1, .2), 'radius': f(.01, 2, 1, 'normalized-image')}),
        'lens-distortion': effect('lens', {'distortion': f(-1, 1, 0), 'dispersion': f(0, 1, 0)}),
        'film-grain': effect('display', {'strength': f(0, 1, .05), 'seed': parameter(integer(0, 2**32-1), 0, 'seed')}),
        'volumetric-fog': effect('scene', {'density_per_meter': f(0, 100, .01, 'inverse-meters'),
                                         'anisotropy': f(-.99, .99, 0)}, (), SCOPES),
        'depth-atmosphere': effect('linear-composite', {'density_per_meter': f(0, 100, .01, 'inverse-meters')}, ('depth',)),
        'ambient-occlusion': effect('scene', {'distance_m': f(.000001, 10000, 1, 'meters'), 'strength': f(0, 1, 1)}, (), SCOPES),
        'contact-shadows': effect('scene', {'distance_m': f(.000001, 10000, 1, 'meters')}, (), SCOPES),
        'reflection': effect('scene', {'bounces': parameter(integer(0, 128), 4, 'count')}, (), SCOPES),
        'refraction': effect('scene', {'bounces': parameter(integer(0, 128), 8, 'count')}, (), SCOPES),
        'alpha-composite': effect('linear-composite', {'opacity': f(0, 1, 1)}, ('rgba', 'matte')),
        'chroma-key': effect('linear-composite', {'tolerance': f(0, 1, .1), 'spill': f(0, 1, .5)}, ('rgba',)),
        'cryptomatte-select': effect('linear-composite', {'invert': b(False)}, ('cryptomatte',)),
    }
    for key, entry in out.items():
        entry['backend_feature'] += key
    return out


def effect_registry(version='1.0.0'):
    require(version == '1.0.0', 'CINEMATICS_REGISTRY_VERSION', 'Unsupported effect registry version.')
    return deepcopy(_registry_v1())


@lru_cache(maxsize=1)
def _definitions():
    pointer = obj(dict(id=dict(type="string", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}(?![\s\S])"), version=VERSION, path=PATH, sha256=SHA,
                       kind=dict(type='string', pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,95}(?![\s\S])')))
    def typed(*kinds):
        return {'allOf': [{'$ref': '#/$defs/reference'}, {'properties': {'kind': enum(*kinds)}}]}
    rate = obj(dict(numerator=integer(1, 1000000), denominator=integer(1, 1000000)))
    time = obj(dict(frame=integer(0, 10000000), ticks=integer(0, 999999), ticks_per_frame={'const': 1000000}))
    policy = obj(dict(allow_approximation=BOOL, on_unavailable={'const': 'reject'}))
    color = obj(dict(working_space={'const': 'scene-linear-srgb'},
                     alpha=enum('straight', 'premultiplied'),
                     display_space=enum('srgb', 'rec709', 'rec2020-pq')))
    meta = obj(dict(fixture=BOOL, description=TEXT))
    def base(kind, fields):
        return obj(dict(schema_version={'type': 'integer', 'const': 1},
                        kind={'const': 'cinematics3d-' + kind}, id=ID, version=VERSION,
                        registry_version={'const': '1.0.0'}, metadata=meta, **fields))
    variants = []
    for key, entry in effect_registry().items():
        variants.append(obj(dict(type={'const': key}, parameters=obj({k: v['schema'] for k, v in entry['parameters'].items()}))))
    effect = base('effect', dict(scope=enum(*SCOPES), enabled=BOOL, order=integer(0, 65535),
                                operation={'oneOf': variants}, inputs=arr(pointer, maximum=16), policy=policy))
    effect['allOf'] = [
        {'if': {'properties': {'operation': {'properties': {'type': {'const': key}}}}},
         'then': {'properties': {'scope': enum(*entry['scopes'])}}}
        for key, entry in effect_registry().items()]
    stack_ref = typed('cinematics3d-stack')
    stack = base('stack', dict(scope=enum(*SCOPES), effects=arr(typed('cinematics3d-effect'), maximum=128),
                              parent={'anyOf': [stack_ref, {'type': 'null'}]}, bypass=arr(ID, maximum=128)))
    shot = base('shot', dict(source=typed('scene3d-shot', 'camera3d-shot'), stack=stack_ref,
                            fps=rate, start=time, end=time, color=color))
    scene = base('scene', dict(source=typed('scene3d-scene'), stack=stack_ref,
                              shots=arr(typed('cinematics3d-shot'), 1, 4096),
                              objects=arr(obj(dict(source=pointer, stack=stack_ref)), maximum=4096)))
    preset = base('preset', dict(stack=stack_ref, previous={'anyOf': [typed('cinematics3d-preset'), {'type': 'null'}]}))
    render = base('render', dict(scene=typed('cinematics3d-scene'), source_snapshot=typed('render3d-snapshot'),
        graph={'anyOf': [typed('cinematics3d-graph'), {'type': 'null'}]}, color=color,
        width=integer(1, 16384), height=integer(1, 16384), fps=rate, start=time, end=time,
        max_pixels=integer(1, 268435456), output=enum('png', 'exr', 'mp4', 'webm'), audio_streams={'const': 0},
        policy=policy))
    # V1 describes a typed routing DAG. Pixel node execution is owned by #252.
    port = obj(dict(name=ID, type=enum('rgba', 'scalar', 'depth', 'vector', 'cryptomatte')))
    node = obj(dict(id=ID, operation=enum('input', 'effect', 'output'),
                    effect={'anyOf': [typed('cinematics3d-effect'), {'type': 'null'}]},
                    inputs=arr(port, maximum=16), outputs=arr(port, maximum=16)))
    edge = obj(dict(source=ID, source_port=ID, target=ID, target_port=ID))
    graph = base('graph', dict(nodes=arr(node, 1, 256), edges=arr(edge, maximum=2048), color=color))
    return dict(reference=pointer, effect=effect, stack=stack, shot=shot, scene=scene,
                preset=preset, render=render, graph=graph)


def definitions():
    return deepcopy(_definitions())


def schemas():
    result = {}
    for kind in KINDS:
        included = {}; pending = [kind]
        while pending:
            name = pending.pop()
            if name in included: continue
            included[name] = deepcopy(_definitions()[name])
            pending.extend(x['$ref'].split('/')[-1] for x in _walk(included[name])
                           if isinstance(x, dict) and '$ref' in x)
        result['cinematics3d-' + kind + '.schema.json'] = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'https://gethatch.pet/schemas/cinematics3d-' + kind + '-v1',
            '$defs': included, '$ref': '#/$defs/' + kind,
            'title': 'Cinematic ' + kind + ' authoring contract v1',
            'description': 'Structural contract only. Runtime verifies references, cycles, units and policies; no effect execution is claimed.'}
    return result


@lru_cache(maxsize=7)
def _validator(kind):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/' + kind})


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def references(value):
    return [x for x in _walk(value) if isinstance(x, dict) and set(x) == {'id', 'version', 'path', 'sha256', 'kind'}]


def _unique(items, label):
    require(len(items) == len(set(items)), 'CINEMATICS_DUPLICATE', 'Duplicate ' + label + '.')


def _acyclic(edges):
    # Iterative Kahn traversal bounds both stack and work by admitted vertices/edges.
    indegree = {k: 0 for k in edges}
    for targets in edges.values():
        for target in targets:
            require(target in edges, 'CINEMATICS_REFERENCE', 'Dangling graph dependency.')
            indegree[target] += 1
    queue = [key for key, degree in indegree.items() if not degree]
    visited = 0
    while queue:
        key = queue.pop(); visited += 1
        for target in edges[key]:
            indegree[target] -= 1
            if not indegree[target]: queue.append(target)
    require(visited == len(edges), 'CINEMATICS_CYCLE', 'Cyclic cinematic dependency.')


def _time(value):
    return value['frame'] * 1000000 + value['ticks']


def validate_document(value):
    finite_json(value)
    require(isinstance(value, dict), 'CINEMATICS_SCHEMA', 'Expected an object.')
    kind = value.get('kind')
    require(isinstance(kind, str) and kind.startswith('cinematics3d-') and kind[13:] in KINDS,
            'CINEMATICS_KIND', 'Unknown cinematic document kind.')
    short = kind[13:]
    error = next(_validator(short).iter_errors(value), None)
    require(error is None, 'CINEMATICS_SCHEMA', str(error.message) if error else '')
    require(len(canonical_bytes(value)) <= MAX_BYTES, 'CINEMATICS_LIMIT', 'Document exceeds 2 MiB.')
    for reference in references(value): portable_path(reference['path'])
    if short in ('shot', 'render'):
        require(_time(value['start']) < _time(value['end']), 'CINEMATICS_TIME', 'Rendered interval must be nonempty and half-open.')
    if short == 'render':
        require(value['width'] * value['height'] <= value['max_pixels'], 'CINEMATICS_LIMIT', 'Image exceeds pixel budget.')
        require(value['output'] != 'exr' or value['color']['display_space'] != 'rec2020-pq',
                'CINEMATICS_COLOR', 'EXR stores scene-linear data, not PQ display encoding.')
    if short == 'effect':
        operation = value['operation']
        input_kinds = {'lut': 'lut-file', 'cryptomatte-select': 'cryptomatte-manifest', 'depth-of-field': 'camera3d-optics'}
        expected = input_kinds.get(operation['type'])
        require((len(value['inputs']) == 1 and value['inputs'][0]['kind'] == expected) if expected else not value['inputs'],
                'CINEMATICS_INPUT', 'Optics, LUT and Cryptomatte effects require one correctly typed immutable input; others use native passes.')
    if short == 'stack':
        _unique([r['id'] for r in value['effects']], 'effect IDs')
        _unique(value['bypass'], 'bypass IDs')
    if short == 'scene':
        _unique([r['id'] for r in value['shots']], 'shot IDs')
        _unique([r['source']['id'] for r in value['objects']], 'object IDs')
    if short == 'graph':
        nodes = {n['id']: n for n in value['nodes']}
        _unique([n['id'] for n in value['nodes']], 'node IDs')
        edges = {key: [] for key in nodes}
        linked = []
        for node in nodes.values():
            _unique([p['name'] for p in node['inputs']], 'input ports')
            _unique([p['name'] for p in node['outputs']], 'output ports')
            require((node['operation'] == 'effect') == (node['effect'] is not None), 'CINEMATICS_GRAPH', 'Only effect nodes reference an effect.')
            require(node['operation'] != 'input' or not node['inputs'], 'CINEMATICS_GRAPH', 'Input nodes have no inputs.')
            require(node['operation'] != 'output' or not node['outputs'], 'CINEMATICS_GRAPH', 'Output nodes have no outputs.')
        for edge in value['edges']:
            require(edge['source'] in nodes and edge['target'] in nodes, 'CINEMATICS_REFERENCE', 'Dangling node.')
            source = {p['name']: p['type'] for p in nodes[edge['source']]['outputs']}
            target = {p['name']: p['type'] for p in nodes[edge['target']]['inputs']}
            require(edge['source_port'] in source and edge['target_port'] in target and
                    source[edge['source_port']] == target[edge['target_port']], 'CINEMATICS_GRAPH_TYPE', 'Unknown or incompatible graph ports.')
            linked.append((edge['target'], edge['target_port']))
            edges[edge['source']].append(edge['target'])
        _unique(linked, 'input connections')
        require(set(linked) == {(n['id'], p['name']) for n in nodes.values() for p in n['inputs']},
                'CINEMATICS_GRAPH', 'Every declared input requires one connection.')
        _acyclic(edges)
    return deepcopy(value)


class Registry:
    """Immutable exact-byte portable config closure; external bytes stay opaque.

    Call each native owner before backend execution. This registry checks external
    byte identity, not native mesh/texture semantics or backend capability reports.
    """
    def __init__(self, documents, *, external=None):
        require(len(documents) <= 1024, 'CINEMATICS_LIMIT', 'Too many documents.')
        self._documents = {}; self._raw = {}; self._keys = {}; aliases = set()
        total = 0
        for path, payload in documents:
            portable_path(path)
            require(path.casefold() not in aliases, 'CINEMATICS_DUPLICATE', 'Conflicting portable paths.')
            aliases.add(path.casefold())
            require(isinstance(payload, bytes) and len(payload) <= MAX_BYTES, 'CINEMATICS_LIMIT', 'Expected bounded document bytes.')
            total += len(payload)
            require(total <= 32 * 1024 * 1024, 'CINEMATICS_LIMIT', 'Registry exceeds 32 MiB.')
            value = validate_document(decode_json(payload))
            key = (value['id'], value['version'])
            require(key not in self._keys, 'CINEMATICS_DUPLICATE', 'Duplicate document identity.')
            self._keys[key] = path; self._documents[path] = value; self._raw[path] = payload
        external = dict(external or {})
        require(len(external) <= 4096, 'CINEMATICS_LIMIT', 'Too many external resources.')
        external_total = 0
        for path, payload in external.items():
            portable_path(path)
            require(path.casefold() not in aliases, 'CINEMATICS_DUPLICATE', 'Conflicting external path.')
            aliases.add(path.casefold())
            require(isinstance(payload, bytes), 'CINEMATICS_INPUT', 'Expected external bytes.')
            external_total += len(payload)
            require(external_total <= 64 * 1024 * 1024, 'CINEMATICS_LIMIT', 'External input exceeds 64 MiB.')
        graph = {path: [] for path in self._documents}
        identities = {}
        for path, value in self._documents.items():
            for ref in references(value):
                identity = (ref['id'], ref['version'])
                previous = identities.setdefault(identity, ref)
                require(previous == ref, 'CINEMATICS_REFERENCE', 'Conflicting immutable reference.')
                source = self._raw if ref['kind'].startswith('cinematics3d-') else external
                require(ref['path'] in source, 'CINEMATICS_MISSING', 'Missing exact referenced bytes: ' + ref['path'])
                require(hashlib.sha256(source[ref['path']]).hexdigest() == ref['sha256'], 'CINEMATICS_HASH', 'Changed reference bytes.')
                if source is self._raw:
                    target = self._documents[ref['path']]
                    require((target['id'], target['version'], target['kind']) == (ref['id'], ref['version'], ref['kind']),
                            'CINEMATICS_REFERENCE', 'Reference identity mismatch.')
                    graph[path].append(ref['path'])
        _acyclic(graph)
        self._external = external
        for path, value in self._documents.items():
            if value['kind'] in ('cinematics3d-scene', 'cinematics3d-shot'):
                scope = value['kind'].split('-')[-1]
                require(self.get(value['stack'])['scope'] == scope, 'CINEMATICS_SCOPE', 'Bound stack has the wrong scope.')
                if scope == 'scene':
                    for binding in value['objects']:
                        require(self.get(binding['stack'])['scope'] == 'object', 'CINEMATICS_SCOPE', 'Object binding requires object stack.')
            if value['kind'] == 'cinematics3d-stack': self.effective_stack(self.reference(value['id'], value['version']))
            if value['kind'] == 'cinematics3d-preset' and value['previous'] is not None:
                previous = self.get(value['previous'])
                require(previous['id'] == value['id'] and tuple(map(int, previous['version'].split('.'))) < tuple(map(int, value['version'].split('.'))),
                        'CINEMATICS_HISTORY', 'Preset history must advance the same immutable identity.')

    def reference(self, identifier, version):
        require((identifier, version) in self._keys, 'CINEMATICS_MISSING', 'Unknown document identity.')
        path = self._keys[(identifier, version)]; value = self._documents[path]
        return dict(id=identifier, version=version, path=path, kind=value['kind'], sha256=hashlib.sha256(self._raw[path]).hexdigest())

    def get(self, reference):
        require(reference == self.reference(reference['id'], reference['version']), 'CINEMATICS_REFERENCE', 'Reference differs from exact stored identity.')
        return deepcopy(self._documents[reference['path']])

    def effective_stack(self, reference):
        chain = []; current = reference
        while current is not None:
            value = self.get(current)
            require(value['kind'] == 'cinematics3d-stack', 'CINEMATICS_KIND', 'Expected a stack.')
            chain.append(value); current = value['parent']
            require(len(chain) <= 64, 'CINEMATICS_LIMIT', 'Stack inheritance exceeds 64 levels.')
        effective = {}; previous_scope = -1
        for stack in reversed(chain):
            index = SCOPES.index(stack['scope'])
            require(index >= previous_scope, 'CINEMATICS_SCOPE', 'Inheritance must flow scene to shot to object.')
            previous_scope = index
            for identifier in stack['bypass']:
                require(identifier in effective, 'CINEMATICS_BYPASS', 'Cannot bypass an unknown inherited effect.')
                del effective[identifier]
            for ref in stack['effects']:
                effect = self.get(ref)
                require(effect['scope'] == stack['scope'], 'CINEMATICS_SCOPE', 'Effect scope differs from its stack.')
                effective[effect['id']] = effect
        enabled = [v for v in effective.values() if v['enabled']]
        order = lambda e: (STAGES.index(effect_registry()[e['operation']['type']]['stage']), e['order'])
        _unique([order(e) for e in enabled], 'stage/order slots')
        return sorted(enabled, key=lambda e: (*order(e), e['id']))

    def bytes(self):
        return dict(self._raw)


def default_effect(effect_type, identifier, *, scope='shot'):
    registry = effect_registry()
    require(effect_type in registry, 'CINEMATICS_EFFECT', 'Unknown effect type.')
    value = dict(schema_version=1, kind='cinematics3d-effect', id=identifier, version='1.0.0', registry_version='1.0.0',
        metadata=dict(fixture=False, description='Explicit defaults; execution is not implied.'), scope=scope,
        enabled=True, order=0, operation=dict(type=effect_type, parameters={k: deepcopy(p['default']) for k, p in registry[effect_type]['parameters'].items()}),
        inputs=[], policy=dict(allow_approximation=False, on_unavailable='reject'))
    return value


def admission(effects, capabilities=None):
    """Interpret caller-supplied backend evidence; no capabilities are invented.

    A valid report remains a claim until the backend verifier checks its bytes.
    Returning admission never marks a render executed or production approved.
    """
    registry = effect_registry(); capabilities = capabilities or {}
    require(isinstance(capabilities, dict) and len(capabilities) <= len(registry), 'CINEMATICS_CAPABILITY', 'Invalid capability report.')
    require(set(capabilities).issubset(registry), 'CINEMATICS_CAPABILITY', 'Unknown capability key.')
    rows = []
    for effect in effects:
        effect = validate_document(effect)
        require(effect['kind'] == 'cinematics3d-effect', 'CINEMATICS_KIND', 'Expected effects.')
        key = effect['operation']['type']
        status = capabilities.get(key, 'unavailable')
        require(status in ('supported', 'approximate', 'unavailable'), 'CINEMATICS_CAPABILITY', 'Unknown capability status.')
        accepted = not effect['enabled'] or status == 'supported' or (status == 'approximate' and effect['policy']['allow_approximation'])
        rows.append(dict(id=effect['id'], effect=key, status=status, admitted=accepted,
                         required_passes=registry[key]['required_passes'], executed=False))
    return dict(admitted=all(r['admitted'] for r in rows), effects=rows, executed=False,
                capability_source='caller-supplied, not independently verified')


def migrate(value, target_schema_version=1):
    require(target_schema_version == 1 and value.get('schema_version') == 1, 'CINEMATICS_VERSION', 'No implicit migration from unknown cinematic schemas.')
    return validate_document(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--registry', action='store_true')
    group.add_argument('--validate', type=Path)
    group.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        if args.registry: output = dict(version='1.0.0', effects=effect_registry(), executed=False)
        elif args.validate:
            with args.validate.open('rb') as stream: data = stream.read(MAX_BYTES + 1)
            require(len(data) <= MAX_BYTES, 'CINEMATICS_LIMIT', 'Input exceeds 2 MiB.')
            output = validate_document(decode_json(data))
        else:
            for name, schema in schemas().items():
                actual = (Path(__file__).resolve().parents[1] / 'profiles' / name).read_text(encoding='utf-8')
                require(actual == json.dumps(schema, indent=2) + '\n', 'CINEMATICS_SCHEMA_DRIFT', name)
            output = dict(schemas=len(KINDS), status='current', executed=False)
        print(json.dumps(output, indent=2, allow_nan=False))
    except (RigError, OSError) as exc:
        print(json.dumps(exc.report if isinstance(exc, RigError) else {'code': 'CINEMATICS_IO', 'message': str(exc)}))
        return 2
    return 0


if __name__ == '__main__': raise SystemExit(main())
