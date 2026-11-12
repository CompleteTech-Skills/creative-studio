"""Additive scene3d v1 contracts and hash-verified document loading.

Schema validation is structural; validate_document also checks semantic invariants.
load_project verifies the actual bounded dependency closure before returning it.
No function in this module implies rendering or visual/production approval.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

from rig_errors import RigError, decode_json, finite_json

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_FILES = 4096
MAX_TOTAL_BYTES = 1024 ** 3
MAX_NODES = 10000
MAX_DEPTH = 128
COORDINATES = dict(handedness='right', right='+X', up='+Y', forward='+Z', unit='meter',
                   angles='radian', quaternion='XYZW', matrices='column-vectors', time='second')
KINDS = ('project', 'scene', 'shot', 'node', 'asset', 'prefab', 'animation', 'render')
MANIFEST_KINDS = tuple('scene3d-' + x for x in KINDS if x != 'node')
END = r'(?![\s\S])'
ID = {'type': 'string', 'pattern': r'^[a-z][a-z0-9_-]{0,63}' + END}
VERSION = {'type': 'string', 'pattern': r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)' + END}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}' + END}
_RESERVED = r'(?:[cC][oO][nN](?:[iI][nN][$]|[oO][uU][tT][$])?|[pP][rR][nN]|[aA][uU][xX]|[nN][uU][lL]|[cC][oO][mM][0-9¹²³]|[lL][pP][tT][0-9¹²³])'
_COMPONENT = (r'(?! )(?!' + _RESERVED + r'(?:[.]|/|' + END + r'))' +
              r'[^/\\:\x00-\x1f<>"|?*]*[^/\\:\x00-\x1f<>"|?* .]')
PATH = dict(type='string', minLength=1, maxLength=1024,
            pattern=r'^(?:' + _COMPONENT + r'/)*' + _COMPONENT + END)
BOOL = {'type': 'boolean'}
NUM = {'type': 'number', 'minimum': -1e9, 'maximum': 1e9}
POS = {'type': 'number', 'exclusiveMinimum': 1e-8, 'maximum': 1e6}


def obj(properties, required=None):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(properties if required is None else required)}


def arr(items, minimum=0, maximum=4096, unique=False):
    result = dict(type='array', items=items, minItems=minimum, maxItems=maximum)
    if unique:
        result['uniqueItems'] = True
    return result


def integer(low=0, high=2_000_000_000):
    return dict(type='integer', minimum=low, maximum=high)


def enum(*values):
    return {'enum': list(values)}


def ref(name):
    return {'$ref': '#/$defs/' + name}


def nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


def mapping(schema, limit=256):
    return dict(type='object', propertyNames=ID, additionalProperties=schema, maxProperties=limit)


def base(kind, properties):
    return obj(dict(schema_version={'type': 'integer', 'const': 1}, kind={'const': 'scene3d-' + kind},
                    id=ID, version=VERSION, **properties))


@lru_cache(maxsize=1)
def definitions():
    vec = arr(NUM, 3, 3)
    quat = arr({'type': 'number', 'minimum': -1, 'maximum': 1}, 4, 4)
    ids = arr(ID, maximum=MAX_NODES, unique=True)
    file = obj(dict(path=PATH, sha256=SHA))
    reference = obj(dict(id={'type': 'string', 'pattern': r'^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}' + END}, version=VERSION, path=file['properties']['path'], sha256=SHA,
                         kind=enum(*(k for k in MANIFEST_KINDS if k != 'scene3d-project'), 'hatch-3d-rig', 'hatch-3d-animation', 'project',
                                   'mesh', 'material', 'texture', 'catalog', 'evidence', 'collision')))
    def typed_ref(*kinds):
        return {'allOf': [ref('reference'), {'properties': {'kind': {'enum': list(kinds)}}}]}
    material_ref = typed_ref('material')
    clip_ref = typed_ref('scene3d-animation', 'hatch-3d-animation')
    meta = mapping({'type': ['string', 'number', 'boolean', 'null'], 'maxLength': 4096}, 64)
    transform = obj(dict(translation=vec, rotation=quat, scale=arr(POS, 3, 3), pivot=vec),
                    ['translation', 'rotation', 'scale'])
    bounds = obj(dict(minimum=vec, maximum=vec))
    rate = obj(dict(numerator=integer(1, 240000), denominator=integer(1, 10000)))
    render_settings = obj(dict(backend=enum('blender-cycles', 'blender-eevee', 'unavailable'),
                               width=integer(1, 8192), height=integer(1, 8192),
                               samples=integer(1, 4096), transparent=BOOL,
                               color_space=enum('linear-srgb', 'srgb'),
                               output=enum('png', 'exr'), device=enum('cpu', 'gpu', 'auto')))
    provenance = obj(dict(origin=enum('import', 'procedural-fixture', 'authored', 'derived'),
                          fixture=BOOL, tool=TEXT, inputs=arr(ref('file'), maximum=256),
                          attribution=TEXT, license=TEXT))
    review = obj(dict(status=enum('unreviewed', 'reviewed', 'approved', 'rejected'),
                      receipts=arr(ref('file'), maximum=256)))
    key = obj(dict(frame=integer(0, 1_000_000),
                   value={'anyOf': [NUM, BOOL, vec, quat, TEXT]}))
    track = obj(dict(target=ID, property=enum('translation', 'rotation', 'scale', 'visible',
                  'material', 'light_intensity', 'world_exposure'),
                  interpolation=enum('step', 'linear', 'slerp'), keys=arr(key, 1, 10000)))
    binding = obj(dict(target=ID, clip=clip_ref, start_frame=integer(0, 1_000_000),
                       end_frame=integer(1, 1_000_000), speed=POS, loop=BOOL))
    constraint = obj(dict(id=ID, type=enum('look-at', 'copy-transform', 'socket'), target=ID,
                          influence={'type': 'number', 'minimum': 0, 'maximum': 1}))
    attachment = obj(dict(owner=ID, socket=ID, offset=ref('transform')))
    override = obj(dict(slot=ID, material=material_ref))
    node_override = obj(dict(node=ID, transform=ref('transform'), visible=BOOL), ['node'])
    instance_overrides = obj(dict(parameters=mapping({'type': ['number', 'string', 'boolean'], 'maxLength': 4096}),
                                 materials=mapping(material_ref),
                                 nodes=arr(node_override, maximum=256)))
    node = obj(dict(id=ID, name=TEXT,
        type=enum('root', 'group', 'mesh_instance', 'skinned_character', 'static_prop',
                  'animated_prop', 'terrain', 'procedural_geometry', 'camera', 'light',
                  'environment_volume', 'socket', 'constraint_target', 'locator',
                  'prefab_instance', 'billboard_2d'), parent=nullable(ID), transform=ref('transform'),
        visible=BOOL, collections=ids, layers=ids, asset=typed_ref('scene3d-asset'),
        prefab=typed_ref('scene3d-prefab'), prefab_overrides=instance_overrides, material_overrides=arr(override, maximum=128),
        animation_bindings=arr(ref('binding'), maximum=256), constraints=arr(constraint, maximum=256),
        attachment=attachment, bounds=nullable(ref('bounds')), locked=BOOL, selectable=BOOL,
        render=obj(dict(visible=BOOL, cast_shadow=BOOL, receive_shadow=BOOL)),
        source=nullable(ref('file')), metadata=meta, billboard=BOOL,
        camera=obj(dict(projection=enum('perspective', 'orthographic'), near=POS, far=POS,
                        vertical_fov=POS, orthographic_height=POS)),
        light=obj(dict(type=enum('point', 'spot', 'directional', 'area'), color=vec,
                       intensity={'type': 'number', 'minimum': 0, 'maximum': 1e9}))),
        ['id', 'name', 'type', 'parent', 'transform', 'visible', 'collections', 'layers',
         'material_overrides', 'animation_bindings', 'constraints', 'bounds', 'locked',
         'selectable', 'render', 'source', 'metadata'])
    # Type-owned fields cannot be smuggled onto unrelated node kinds.
    for types, field in [(['mesh_instance', 'skinned_character', 'static_prop', 'animated_prop',
                          'terrain', 'billboard_2d'], 'asset'),
                         (['prefab_instance'], 'prefab'), (['camera'], 'camera'),
                         (['light'], 'light'), (['billboard_2d'], 'billboard')]:
        node.setdefault('allOf', []).append({
            'if': {'properties': {'type': {'enum': types}}}, 'then': {'required': [field]}})
    budget = obj(dict(max_nodes=integer(1, MAX_NODES), max_files=integer(1, MAX_FILES),
                       max_bytes=integer(1, MAX_TOTAL_BYTES), max_frames=integer(1, 1_000_000)))
    world = obj(dict(coordinate_system={'const': COORDINATES},
                     background=vec, exposure=NUM, environment=nullable(typed_ref('texture', 'scene3d-asset')),
                     metadata=meta))
    collection = obj(dict(id=ID, name=TEXT, visible=BOOL))
    scene = base('scene', dict(name=TEXT, description=TEXT, revision=integer(),
        coordinate_system={'const': COORDINATES}, roots=ids, nodes=arr(ref('node'), 1, MAX_NODES),
        world=world, collections=arr(collection, maximum=256), layers=arr(collection, maximum=64),
        cameras=ids, lights=ids, recipes=arr(ref('file'), maximum=256),
        dependencies=arr(ref('reference')), bounds=nullable(ref('bounds')), shots=ids,
        continuity=arr(ref('file'), maximum=256), metadata=meta, provenance=ref('provenance')))
    prefab = base('prefab', dict(name=TEXT, roots=ids, nodes=arr(ref('node'), 1, MAX_NODES),
        parameters=mapping({'type': ['number', 'string', 'boolean'], 'maxLength': 4096}),
        material_slots=ids, sockets=ids,
        overrides=arr(node_override, maximum=256), dependencies=arr(ref('reference')),
        provenance=ref('provenance')))
    asset = base('asset', dict(name=TEXT,
        asset_type=enum('mesh', 'character', 'prop', 'terrain', 'material', 'texture', 'environment'),
        content=ref('file'), source=ref('file'), provenance=ref('provenance'),
        coordinate_system={'const': COORDINATES}, original_coordinate_system=TEXT,
        pivot=vec, meshes=arr(ref('file')), materials=arr(material_ref),
        bounds=nullable(ref('bounds')), collision=nullable(ref('file')),
        sockets=arr(obj(dict(id=ID, transform=ref('transform'))), maximum=256),
        components=mapping(ids), skeleton=nullable(typed_ref('hatch-3d-rig')),
        animations=arr(clip_ref), lods=arr(typed_ref('scene3d-asset'), maximum=16),
        dependencies=arr(ref('reference')), resources=obj(dict(vertices=integer(0, 1000000),
        triangles=integer(0, 2000000), bytes=integer(1, MAX_TOTAL_BYTES))),
        validation=arr(ref('file')), review=ref('review')))
    animation = base('animation', dict(duration_frames=integer(1, 1_000_000), fps=ref('rate'),
        tracks=arr(ref('track'), maximum=4096), bindings=arr(ref('binding'), maximum=256),
        dependencies=arr(ref('reference')), provenance=ref('provenance')))
    shot = base('shot', dict(name=TEXT, scene=typed_ref('scene3d-scene'),
        duration_frames=integer(1, 1_000_000), fps=ref('rate'), camera=ID,
        camera_overrides=obj(dict(transform=ref('transform'), camera=node['properties']['camera']), []),
        tracks=arr(ref('track'), maximum=4096), bindings=arr(ref('binding'), maximum=256),
        material_overrides=arr(obj(dict(target=ID, slot=ID, material=material_ref)), maximum=256),
        temporal_constraints=arr(obj(dict(id=ID, target=ID, start_frame=integer(0, 1_000_000),
            end_frame=integer(1, 1_000_000), constraint=constraint)), maximum=256),
        render=ref('render_settings'), continuity=arr(ref('file'), maximum=256),
        dependencies=arr(ref('reference')), metadata=meta))
    render = base('render', dict(shot=typed_ref('scene3d-shot'),
        start_frame=integer(0, 1_000_000), end_frame=integer(1, 1_000_000),
        settings=ref('render_settings'), output_directory=file['properties']['path'],
        dependencies=arr(ref('reference')), provenance=ref('provenance')))
    project = base('project', dict(name=TEXT, status=enum('fixture', 'development', 'production'),
        coordinate_system={'const': COORDINATES}, asset_catalog=nullable(typed_ref('catalog')),
        assets=arr(typed_ref('scene3d-asset')), characters=arr(typed_ref('scene3d-asset')),
        scenes=arr(typed_ref('scene3d-scene'), 1, 256), shots=arr(typed_ref('scene3d-shot'), maximum=4096),
        prefabs=arr(typed_ref('scene3d-prefab')), animations=arr(typed_ref('scene3d-animation')),
        render_defaults=ref('render_settings'), budgets=ref('budget'),
        created_at=TEXT, modified_at=TEXT, linked_2d_project=nullable(typed_ref('project')),
        dependency_lock=arr(ref('reference')), validation=arr(ref('file')),
        provenance=ref('provenance'), metadata=meta))
    return dict(file=file, reference=reference, transform=transform, bounds=bounds,
                rate=rate, render_settings=render_settings, provenance=provenance,
                review=review, track=track, binding=binding, budget=budget,
                node=node, scene=scene, prefab=prefab, asset=asset, animation=animation,
                shot=shot, render=render, project=project)


def schemas():
    return {'scene3d-' + kind + '.schema.json': {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'https://gethatch.pet/schemas/scene3d-' + kind + '-v1',
        'title': 'Scene3D ' + kind + ' v1',
        'description': 'Structural schema. Runtime checks paths, quaternions, graphs, timing and actual dependency hashes.',
        '$defs': definitions(), '$ref': '#/$defs/' + kind} for kind in KINDS}


def fail(message, code='SCENE3D_CONTRACT'):
    raise RigError(code, message, stage='scene3d')


def require(condition, message, code='SCENE3D_CONTRACT'):
    if not condition:
        fail(message, code)


@lru_cache(maxsize=32)
def validator(kind):
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RigError('SCENE3D_DEPENDENCY', 'Install requirements-delivery.txt for Scene3D contract validation.', stage='scene3d') from exc
    require(kind in definitions(), 'Unknown scene3d kind: ' + str(kind))
    return Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/' + kind})


def portable_path(value):
    """Canonical portable spelling; no aliases, traversal, drive or device paths."""
    require(isinstance(value, str) and 0 < len(value) <= 1024, 'Invalid path.')
    _unicode_scalars(value)
    require(unicodedata.normalize('NFC', value) == value, 'Paths must use NFC Unicode.')
    require(not re.search(r'[\\:\x00-\x1f<>"|?*]', value), 'Path contains a nonportable character.')
    parts = value.split('/')
    for part in parts:
        require(part not in ('', '.', '..') and not part.startswith(' ') and not part.endswith((' ', '.')), 'Noncanonical path component.')
        require(not re.match(r'^(CON|CONIN[$]|CONOUT[$]|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)', part, re.I),
                'Windows device path is unsupported.')
    return value


def _typed_values(value, schema):
    """Walk declared schema positions, never infer resources from metadata keys."""
    if value is None:
        return
    if '$ref' in schema:
        name = schema['$ref'].rsplit('/', 1)[-1]
        yield name, value
        schema = definitions()[name]
    for branch in schema.get('allOf', []) + schema.get('anyOf', []):
        yield from _typed_values(value, branch)
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        additional = schema.get('additionalProperties')
        for key, child in value.items():
            child_schema = properties.get(key, additional)
            if isinstance(child_schema, dict):
                yield from _typed_values(child, child_schema)
    elif isinstance(value, list) and isinstance(schema.get('items'), dict):
        for child in value:
            yield from _typed_values(child, schema['items'])


def all_refs(value, kind=None):
    """Yield only declared file/manifest refs from a document or explicit ref."""
    if kind is None:
        if isinstance(value, list):
            for child in value:
                yield from all_refs(child)
            return
        if not isinstance(value, dict):
            return
        if value.get('schema_version') == 1 and value.get('kind') in MANIFEST_KINDS:
            kind = value['kind'][8:]
        elif set(value) == {'path', 'sha256'}:
            kind = 'file'
        elif set(value) == {'id', 'version', 'path', 'sha256', 'kind'}:
            kind = 'reference'
        elif 'type' in value and 'transform' in value and 'parent' in value:
            kind = 'node'
        else:
            return
    for name, child in _typed_values(value, ref(kind)):
        if name in ('file', 'reference'):
            yield child


def _unique(items, label, key=lambda x: x):
    values = [key(item) for item in items]
    require(len(values) == len(set(values)), 'Duplicate ' + label + '.')
    return set(values)


def _unicode_scalars(value):
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeError:
            fail('Scene3D strings must contain UTF-8 encodable Unicode scalar values.')
    elif isinstance(value, dict):
        for key, child in value.items():
            _unicode_scalars(key)
            _unicode_scalars(child)
    elif isinstance(value, list):
        for child in value:
            _unicode_scalars(child)


def _transforms(value, kind):
    for name, transform in _typed_values(value, ref(kind)):
        if name == 'transform':
            q = transform['rotation']
            require(abs(sum(x*x for x in q) - 1) <= 1e-6, 'Quaternion must already be normalized.')
            require(min(transform['scale']) / max(transform['scale']) >= 1e-8,
                    'Transform scale is numerically unstable.')
        elif name == 'bounds':
            require(all(a <= b for a, b in zip(transform['minimum'], transform['maximum'])), 'Inverted bounds.')


def _node_semantics(node):
    require(node['parent'] != node['id'], 'A node cannot parent itself.')
    own_type = node['type']
    require('camera' not in node or own_type == 'camera', 'Camera configuration on a non-camera node.')
    require('light' not in node or own_type == 'light', 'Light configuration on a non-light node.')
    require('prefab' not in node or own_type == 'prefab_instance', 'Prefab on a non-prefab node.')
    require('billboard' not in node or (own_type == 'billboard_2d' and node['billboard'] is True),
            '2D billboard must be explicitly marked.')
    if own_type == 'camera':
        _camera_semantics(node['camera'])
    if own_type == 'light':
        require(all(0 <= x <= 1 for x in node['light']['color']), 'Light color must be linear RGB [0,1].')

    require('prefab_overrides' not in node or own_type == 'prefab_instance',
            'Prefab instance overrides on a non-prefab node.')
    if 'prefab_overrides' in node:
        _unique(node['prefab_overrides']['nodes'], 'instance node override', lambda o: o['node'])
    _unique(node['constraints'], 'constraint ID', lambda item: item['id'])
    require(all(c['target'] != node['id'] for c in node['constraints']), 'Self-referential constraint.')
    _unique(node['material_overrides'], 'material slot', lambda item: item['slot'])
    for binding in node['animation_bindings']:
        require(binding['target'] == node['id'], 'A node animation binding must target its owning node.')
        _binding_interval(binding)


def _camera_semantics(camera):
    require(camera['near'] < camera['far'], 'Camera near plane must precede far plane.')
    require(camera['vertical_fov'] < math.pi, 'Camera field of view must be below pi radians.')


def _binding_interval(binding, duration=None):
    require(type(binding['start_frame']) is int and type(binding['end_frame']) is int,
            'Exact clip boundaries require JSON integers.')
    require(binding['start_frame'] < binding['end_frame'], 'Invalid clip interval.')
    if duration is not None:
        require(binding['end_frame'] <= duration, 'Invalid clip interval.')


def _graph(nodes, roots):
    ids = _unique(nodes, 'node ID', lambda node: node['id'])
    by_id = {node['id']: node for node in nodes}
    require(set(roots) == {node['id'] for node in nodes if node['parent'] is None},
            'Root inventory must exactly match parentless nodes.')
    require(bool(roots), 'A graph must have a root.')
    _unique(roots, 'root ID')
    for node in nodes:
        require(node['parent'] is None or node['parent'] in ids, 'Dangling transform parent.')
        visited = set()
        cursor = node
        while cursor is not None:
            require(cursor['id'] not in visited, 'Cyclic transform graph.')
            visited.add(cursor['id'])
            require(len(visited) <= MAX_DEPTH, 'Transform hierarchy exceeds 128 levels.')
            cursor = by_id.get(cursor['parent'])
        _node_semantics(node)
        for constraint in node['constraints']:
            require(constraint['target'] in ids and constraint['target'] != node['id'],
                    'Constraint target is dangling or self-referential.')
        if 'attachment' in node:
            a = node['attachment']
            require(a['owner'] in ids and a['owner'] != node['id'], 'Dangling attachment owner.')
            require(a['socket'] in ids and by_id[a['socket']]['type'] == 'socket', 'Dangling attachment socket.')
            require(by_id[a['socket']]['parent'] == a['owner'], 'Socket must belong to attachment owner.')
    # A constraint/attachment cannot introduce a second evaluation cycle.
    edges = _node_edges(nodes)
    _acyclic(edges, 'node dependency')
    return by_id


def _node_edges(nodes):
    return {node['id']: ([node['parent']] if node['parent'] else []) +
            [c['target'] for c in node['constraints']] +
            ([node['attachment']['socket']] if 'attachment' in node else []) for node in nodes}


def _acyclic(edges, label):
    states, longest = {}, {}
    def visit(key, call_depth):
        require(call_depth <= MAX_DEPTH, label + ' nesting exceeds 128 levels.')
        require(states.get(key) != 1, 'Cyclic ' + label + ' graph.')
        if states.get(key) == 2:
            return longest[key]
        states[key] = 1
        length = 1 + max((visit(child, call_depth + 1) for child in edges.get(key, ())), default=0)
        require(length <= MAX_DEPTH, label + ' nesting exceeds 128 levels.')
        states[key] = 2
        longest[key] = length
        return length
    for key in edges:
        visit(key, 0)


def _timing(value):
    rate = value['fps']
    require(type(rate['numerator']) is int and type(rate['denominator']) is int
            and type(value['duration_frames']) is int, 'Exact timing requires JSON integer components.')
    require(math.gcd(rate['numerator'], rate['denominator']) == 1, 'Frame rate must be reduced.')
    require(Fraction(rate['numerator'], rate['denominator']) <= 240, 'Frame rate exceeds 240 fps.')
    duration = value['duration_frames']
    for track in value['tracks']:
        frames = [key['frame'] for key in track['keys']]
        require(all(type(frame) is int for frame in frames), 'Exact key frames require JSON integers.')
        require(frames == sorted(set(frames)) and frames[-1] <= duration,
                'Track keys must be unique, sorted and within the terminal boundary.')
        prop = track['property']
        interpolation = track['interpolation']
        require(prop != 'world_exposure' or track['target'] == 'world',
                'World exposure tracks must use the explicit world target namespace.')
        require((prop == 'rotation' and interpolation in ('step', 'slerp')) or
                (prop in ('visible', 'material') and interpolation == 'step') or
                (prop not in ('rotation', 'visible', 'material') and interpolation in ('step', 'linear')),
                'Interpolation is inconsistent with track property.')
        for key in track['keys']:
            v = key['value']
            if prop in ('translation', 'scale', 'rotation'):
                n = 4 if prop == 'rotation' else 3
                require(isinstance(v, list) and len(v) == n and
                        all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v),
                        'Track value dimension/type mismatch.')
                if prop == 'rotation':
                    require(abs(sum(x*x for x in v) - 1) <= 1e-6, 'Track quaternion must be normalized.')
                if prop == 'scale':
                    require(all(1e-8 < x <= 1e6 for x in v), 'Track scale must be positive and bounded.')
            elif prop == 'visible':
                require(type(v) is bool, 'Visibility requires boolean keys.')
            elif prop == 'material':
                require(isinstance(v, str), 'Material requires string keys.')
                declared = {r['id'] + '@' + r['version'] for r in value['dependencies'] if r['kind'] == 'material'}
                require(v in declared, 'Material track key must resolve to a hash-bound material dependency.')
            else:
                require(type(v) in (int, float), 'Numeric property requires scalar keys.')
                require(prop != 'light_intensity' or v >= 0, 'Light intensity cannot be negative.')
    _unique(value['tracks'], 'target/property track', lambda t: (t['target'], t['property']))
    for binding in value['bindings']:
        _binding_interval(binding, duration)


def _integer_value(value, schema, path):
    """Admit JSON's numeric integer values without lossy float roundtrips."""
    require(type(value) in (int, float), path + ': expected an integer-valued JSON number.')
    if type(value) is float:
        require(math.isfinite(value) and value.is_integer(),
                path + ': expected a finite integer-valued JSON number.')
    # Compare native integers directly, including arbitrarily large Python ints.
    # Never convert an integer to float, or a huge out-of-budget float to int.
    for key, comparison in (('minimum', lambda a, b: a >= b),
                            ('maximum', lambda a, b: a <= b),
                            ('exclusiveMinimum', lambda a, b: a > b),
                            ('exclusiveMaximum', lambda a, b: a < b)):
        if key in schema:
            require(comparison(value, schema[key]), path + ': integer is outside the declared bounds.')
    if 'const' in schema:
        require(value == schema['const'], path + ': integer does not match the declared constant.')
    if 'enum' in schema:
        require(value in schema['enum'], path + ': integer is not a declared value.')
    return int(value)


def _schema_shape(value, schema):
    if '$ref' in schema:
        schema = definitions()[schema['$ref'].rsplit('/', 1)[-1]]
    types = schema.get('type')
    if types is None:
        return True
    types = types if isinstance(types, list) else [types]
    checks = {'object': isinstance(value, dict), 'array': isinstance(value, list),
              'string': isinstance(value, str), 'boolean': type(value) is bool,
              'null': value is None, 'number': type(value) in (int, float),
              'integer': type(value) is int or (type(value) is float and math.isfinite(value) and value.is_integer())}
    return any(checks.get(name, False) for name in types)


def _normalize_integers(value, schema, path='$'):
    """Normalize declared local integer positions in an already copied tree."""
    if '$ref' in schema:
        schema = definitions()[schema['$ref'].rsplit('/', 1)[-1]]
    if schema.get('type') == 'integer':
        return _integer_value(value, schema, path)
    for branch in schema.get('allOf', []):
        value = _normalize_integers(value, branch, path)
    for branch in schema.get('anyOf', []):
        if _schema_shape(value, branch):
            value = _normalize_integers(value, branch, path)
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        additional = schema.get('additionalProperties')
        for key, child in list(value.items()):
            child_schema = properties.get(key, additional)
            if isinstance(child_schema, dict):
                value[key] = _normalize_integers(child, child_schema, path + '.' + key)
    elif isinstance(value, list) and isinstance(schema.get('items'), dict):
        for index, child in enumerate(value):
            value[index] = _normalize_integers(child, schema['items'], path + '[' + str(index) + ']')
    return value


def validate_document(value, kind=None):
    """Return a validated, integer-normalized copy without mutating source data."""
    finite_json(value)
    if kind is None:
        require(isinstance(value, dict) and value.get('kind') in MANIFEST_KINDS, 'Unknown scene3d document kind.')
        kind = value['kind'][8:]
    checker = validator(kind)
    value = _normalize_integers(deepcopy(value), ref(kind))
    error = next(checker.iter_errors(value), None)
    require(error is None, '' if error is None else '.'.join(map(str, error.absolute_path)) + ': ' + error.message)
    _unicode_scalars(value)
    for item in all_refs(value, kind):
        portable_path(item['path'])
    if kind == 'render':
        portable_path(value['output_directory'])
        require(type(value['start_frame']) is int and type(value['end_frame']) is int,
                'Exact render boundaries require JSON integers.')
        require(value['start_frame'] < value['end_frame'], 'Render interval must be nonempty.')
    _transforms(value, kind)
    if 'provenance' in value:
        require(value['provenance']['fixture'] or value['provenance']['origin'] != 'procedural-fixture',
                'Procedural fixtures cannot lose fixture provenance.')
    if kind == 'node':
        _node_semantics(value)
    elif kind in ('scene', 'prefab'):
        nodes = _graph(value['nodes'], value['roots'])
        if kind == 'scene':
            collections = _unique(value['collections'], 'collection ID', lambda x: x['id'])
            layers = _unique(value['layers'], 'layer ID', lambda x: x['id'])
            for node in nodes.values():
                require(set(node['collections']) <= collections and set(node['layers']) <= layers,
                        'Node references an undeclared collection or layer.')
            require(set(value['cameras']) == {n['id'] for n in nodes.values() if n['type'] == 'camera'},
                    'Camera inventory differs from camera nodes.')
            require(set(value['lights']) == {n['id'] for n in nodes.values() if n['type'] == 'light'},
                    'Light inventory differs from light nodes.')
        else:
            require(set(value['sockets']) <= {n['id'] for n in nodes.values() if n['type'] == 'socket'},
                    'Prefab sockets must reference socket nodes.')
            _unique(value['overrides'], 'prefab override', lambda x: x['node'])
            require(all(o['node'] in nodes for o in value['overrides']), 'Dangling prefab override.')
    elif kind in ('shot', 'animation'):
        _timing(value)
        if kind == 'shot' and 'camera' in value['camera_overrides']:
            _camera_semantics(value['camera_overrides']['camera'])
        _unique(value.get('temporal_constraints', []), 'temporal constraint ID', lambda c: c['id'])
        for c in value.get('temporal_constraints', []):
            require(type(c['start_frame']) is int and type(c['end_frame']) is int,
                    'Exact temporal boundaries require JSON integers.')
            require(c['start_frame'] < c['end_frame'] <= value['duration_frames'], 'Invalid temporal constraint interval.')
            require(c['target'] != c['constraint']['target'], 'Self-referential temporal constraint.')
    elif kind == 'asset':
        _unique(value['sockets'], 'asset socket', lambda x: x['id'])
        require(value['review']['status'] == 'unreviewed' or bool(value['review']['receipts']),
                'A review status needs retained evidence receipts.')
        require(value['provenance']['fixture'] or value['provenance']['origin'] != 'procedural-fixture',
                'Procedural fixtures cannot lose fixture provenance.')
    elif kind == 'project':
        require(all(type(x) is int for x in value['budgets'].values()), 'Resource budgets require JSON integers.')
        try:
            created = datetime.fromisoformat(value['created_at'].replace('Z', '+00:00'))
            modified = datetime.fromisoformat(value['modified_at'].replace('Z', '+00:00'))
            require(created.tzinfo is not None and modified.tzinfo is not None, 'Project timestamps need explicit timezones.')
            require(created <= modified, 'Modified time cannot precede creation.')
        except ValueError as exc:
            fail('Invalid project timestamp: ' + str(exc))
        for name in ('assets', 'characters', 'scenes', 'shots', 'prefabs', 'animations', 'dependency_lock'):
            _unique(value[name], name + ' reference', lambda r: (r['kind'], r['id'], r['version']))
        require(value['status'] != 'production' or not value['provenance']['fixture'],
                'A fixture project cannot claim production status.')
    return value


def canonical_bytes(value):
    value = validate_document(value)
    # Bound encoded output while walking tokens instead of materializing a
    # potentially oversized whole JSON string before checking its byte length.
    encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False)
    chunks, size = [], 1  # Include the terminating newline in the byte budget.
    for token in encoder.iterencode(value):
        chunk = token.encode('utf-8')
        size += len(chunk)
        require(size <= MAX_DOCUMENT_BYTES, 'Canonical manifest exceeds 16 MiB.')
        chunks.append(chunk)
    return b''.join(chunks) + b'\n'


def document_hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def frame_time(frame, rate):
    frame = _integer_value(frame, {'type': 'integer', 'minimum': 0}, 'frame')
    require(isinstance(rate, dict), 'Invalid rational frame rate.')
    rate = {name: _integer_value(rate.get(name), schema, 'fps.' + name)
            for name, schema in definitions()['rate']['properties'].items()}
    require(math.gcd(rate['numerator'], rate['denominator']) == 1 and
            Fraction(rate['numerator'], rate['denominator']) <= 240, 'Invalid reduced frame rate.')
    return Fraction(frame * rate['denominator'], rate['numerator'])


def read_document(path):
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    require(len(data) <= MAX_DOCUMENT_BYTES, 'Document exceeds 16 MiB.')
    return validate_document(decode_json(data))


def load_project(root, name='project.json'):
    """Read-only closure verification. Files must remain stable during this call.

    This is not a transaction or a sandbox against a malicious concurrent writer.
    Later workers must revalidate input hashes at their own admission boundary.
    """
    root = Path(root).resolve(strict=True)
    require(root.is_dir(), 'Project root must be a directory.')
    documents, inventory, identities, edges, aliases, payloads = {}, {}, {}, {}, {}, {}
    total = 0
    budgets = dict(max_files=MAX_FILES, max_bytes=MAX_TOTAL_BYTES, max_nodes=MAX_NODES, max_frames=1_000_000)

    def read_file(path):
        nonlocal total
        portable_path(path)
        folded = unicodedata.normalize('NFC', path).casefold()
        require(folded not in aliases or aliases[folded] == path, 'Portable path alias collision.')
        aliases[folded] = path
        target = root
        for part in path.split('/'):
            target = target / part
            require(not target.is_symlink(), 'Dependency path traverses a symlink.')
            require(not getattr(target, 'is_junction', lambda: False)(), 'Dependency path traverses a junction.')
            require(target.resolve().is_relative_to(root), 'Dependency resolves outside the project root.')
        require(target.is_file(), 'Missing regular dependency: ' + path)
        size = target.stat().st_size
        require(size <= budgets['max_bytes'] - total, 'Project byte budget exceeded.')
        require(len(inventory) < budgets['max_files'], 'Project file budget exceeded.')
        digest = hashlib.sha256()
        observed = 0
        chunks = []
        with target.open('rb') as stream:
            while True:
                chunk = stream.read(min(1024*1024, budgets['max_bytes'] - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                observed += len(chunk)
                require(total <= budgets['max_bytes'], 'Project byte budget exceeded during read.')
                digest.update(chunk)
                if path.endswith('.json'):
                    require(sum(map(len, chunks)) + len(chunk) <= MAX_DOCUMENT_BYTES, 'Document exceeds 16 MiB.')
                    chunks.append(chunk)
        require(observed == size, 'Dependency changed size while reading: ' + path)
        inventory[path] = dict(sha256=digest.hexdigest(), bytes=observed)
        payloads[path] = b''.join(chunks)
        return payloads[path]

    def visit(path, expected=None, depth=0):
        require(depth <= MAX_DEPTH, 'Dependency closure exceeds 128 levels.')
        if path in inventory:
            if expected:
                require(inventory[path]['sha256'] == expected['sha256'], 'Conflicting dependency hash: ' + path)
            if path in documents:
                if expected and (expected.get('kind') in MANIFEST_KINDS or expected.get('kind') == 'project'):
                    _reference_identity(documents[path], expected)
                return
            if not expected or expected.get('kind') not in MANIFEST_KINDS:
                if expected and expected.get('kind') == 'project':
                    _legacy_reference(payloads[path], expected)
                return
            payload = payloads[path]
        else:
            payload = read_file(path)
            if expected:
                require(inventory[path]['sha256'] == expected['sha256'], 'Dependency hash mismatch: ' + path)
            if expected and expected.get('kind') not in MANIFEST_KINDS:
                if expected.get('kind') == 'project':
                    _legacy_reference(payload, expected)
                return
        require(path.endswith('.json'), 'Scene3D manifests must use .json filenames.')
        document = validate_document(decode_json(payload))
        if expected and expected.get('kind') in MANIFEST_KINDS:
            _reference_identity(document, expected)
        require(depth == 0 or document['kind'] != 'scene3d-project', 'Nested Scene3D project manifests are unsupported.')
        documents[path] = document
        identity = (document['kind'], document['id'], document['version'])
        require(identity not in identities or identities[identity] == path, 'Duplicate versioned manifest identity.')
        identities[identity] = path
        if depth == 0:
            require(document['kind'] == 'scene3d-project', 'Root document must be a scene3d project.')
            budgets.update(document['budgets'])
            require(total <= budgets['max_bytes'] and len(inventory) <= budgets['max_files'], 'Root exceeds project budget.')
        edges[path] = []
        for item in all_refs(document):
            if item.get('kind') in MANIFEST_KINDS:
                edges[path].append(item['path'])
            visit(item['path'], item, depth+1)
    visit(name)
    _acyclic(edges, 'manifest/prefab dependency')
    project = documents[name]
    locked = {(r['kind'], r['id'], r['version'], r['path'], r['sha256']) for r in project['dependency_lock']}
    references = [r for d in documents.values() for r in all_refs(d) if 'kind' in r]
    require(all((r['kind'], r['id'], r['version'], r['path'], r['sha256']) in locked for r in references),
            'Dependency lock does not cover every typed reference.')
    binding_context = dict(seen=set(), checks=0)
    registered_scenes = {(r['id'], r['version'], r['path'], r['sha256']) for r in project['scenes']}
    registered_assets = {(r['id'], r['version'], r['path'], r['sha256']) for r in project['assets']}
    for reference in project['characters']:
        require((reference['id'], reference['version'], reference['path'], reference['sha256']) in registered_assets,
                'Project character must be registered in project assets.')
        require(documents[reference['path']]['asset_type'] == 'character', 'Project character reference must identify a character asset.')
    for d in documents.values():
        if d['kind'] in ('scene3d-scene', 'scene3d-prefab'):
            require(len(d['nodes']) <= budgets['max_nodes'], 'Scene/prefab exceeds project node budget.')
            for node in d['nodes']:
                for binding in node['animation_bindings']:
                    require(binding['end_frame'] <= budgets['max_frames'], 'Node binding exceeds project frame budget.')
                    _binding_context(binding, d, documents, binding_context)
                if 'prefab_overrides' in node:
                    _prefab_overrides(node['prefab_overrides'], documents[node['prefab']['path']])
                if 'asset' in node:
                    asset = documents[node['asset']['path']]
                    require(asset['asset_type'] not in ('material', 'texture'), 'Geometry node references a non-geometric asset.')
                    require(node['type'] != 'skinned_character' or asset['asset_type'] == 'character',
                            'Skinned character must reference a character asset.')
        elif d['kind'] in ('scene3d-shot', 'scene3d-animation'):
            require(d['duration_frames'] <= budgets['max_frames'], 'Timeline exceeds project frame budget.')
        if d['kind'] == 'scene3d-scene' and d['world']['environment'] is not None:
            environment = d['world']['environment']
            if environment['kind'] == 'scene3d-asset':
                require(documents[environment['path']]['asset_type'] == 'environment',
                        'World environment must reference an environment asset.')
        if d['kind'] == 'scene3d-shot':
            scene_ref = d['scene']
            require((scene_ref['id'], scene_ref['version'], scene_ref['path'], scene_ref['sha256']) in registered_scenes,
                    'Shot scene is not registered in project scenes.')
            scene = documents[d['scene']['path']]
            nodes = {n['id']: n for n in scene['nodes']}
            require(d['id'] in scene['shots'], 'Shot is not associated with its scene.')
            require(d['camera'] in scene['cameras'], 'Shot camera does not exist in scene.')
            targets = [t['target'] for t in d['tracks'] if t['property'] != 'world_exposure'] + [b['target'] for b in d['bindings']]
            targets += [o['target'] for o in d['material_overrides']]
            targets += [c['target'] for c in d['temporal_constraints']]
            targets += [c['constraint']['target'] for c in d['temporal_constraints']]
            require(all(t in nodes for t in targets), 'Shot references a missing node.')
            for track in d['tracks']:
                _track_context(track, nodes, allow_world=True)
            for binding in d['bindings']:
                _binding_context(binding, scene, documents, binding_context)
            for node in scene['nodes']:
                for binding in node['animation_bindings']:
                    require(binding['end_frame'] <= d['duration_frames'], 'Node binding exceeds consuming shot duration.')
            for boundary in sorted({c['start_frame'] for c in d['temporal_constraints']}):
                edges = _node_edges(scene['nodes'])
                for c in d['temporal_constraints']:
                    if c['start_frame'] <= boundary < c['end_frame']:
                        edges[c['target']].append(c['constraint']['target'])
                _acyclic(edges, 'temporal constraint')
        elif d['kind'] == 'scene3d-render':
            require(d['end_frame'] <= documents[d['shot']['path']]['duration_frames'], 'Render range exceeds shot.')
        elif d['kind'] == 'scene3d-asset':
            require(inventory[d['content']['path']]['bytes'] == d['resources']['bytes'], 'Asset resource byte count differs from actual content.')
    project_shots = [documents[r['path']] for r in project['shots']]
    for scene_ref in project['scenes']:
        scene = documents[scene_ref['path']]
        associated = {shot['id'] for shot in project_shots if shot['scene']['path'] == scene_ref['path']}
        require(set(scene['shots']) == associated, 'Scene shot inventory has dangling or missing project associations.')
    return dict(project=deepcopy(project), documents=documents, inventory=inventory,
                bytes=total, status='hash-and-contract-verified')


def _track_context(track, nodes, *, allow_world=False):
    if track['property'] == 'world_exposure':
        require(allow_world, 'World tracks must be authored directly on a shot, not a node binding.')
        return
    require(track['target'] in nodes, 'Animation clip references a missing node in the consuming graph.')
    if track['property'] == 'light_intensity':
        require(nodes[track['target']]['type'] == 'light', 'Light track must target a light node.')


def _binding_context(binding, scene, documents, context, depth=0):
    require(depth < MAX_DEPTH, 'Animation binding depth exceeds 128 levels.')
    nodes = {n['id']: n for n in scene['nodes']}
    require(binding['target'] in nodes, 'Binding target is absent from the consuming graph.')
    if binding['clip']['kind'] != 'scene3d-animation':
        return  # Native rig animation requires its owner's validator/evaluator.
    key = (scene['kind'], scene['id'], scene['version'], binding['clip']['path'], binding['target'])
    if key in context['seen']:
        return
    context['seen'].add(key)
    clip = documents[binding['clip']['path']]
    def in_subtree(target):
        cursor = target
        while cursor is not None and cursor != binding['target']:
            cursor = nodes[cursor]['parent']
        require(cursor == binding['target'], 'Animation clip target lies outside its binding subtree.')
    for track in clip['tracks']:
        context['checks'] += 1
        require(context['checks'] <= 250000, 'Animation binding context operation budget exceeded.')
        _track_context(track, nodes)
        in_subtree(track['target'])
    for nested in clip['bindings']:
        context['checks'] += 1
        require(context['checks'] <= 250000, 'Animation binding context operation budget exceeded.')
        require(nested['target'] in nodes, 'Nested binding target is absent from the consuming graph.')
        in_subtree(nested['target'])
        _binding_context(nested, scene, documents, context, depth + 1)


def _prefab_overrides(overrides, prefab):
    require(set(overrides['parameters']) <= set(prefab['parameters']), 'Unknown prefab instance parameter.')
    for key, value in overrides['parameters'].items():
        default = prefab['parameters'][key]
        numeric = type(value) in (int, float) and type(default) in (int, float)
        require(numeric or type(value) is type(default), 'Prefab parameter type mismatch.')
    require(set(overrides['materials']) <= set(prefab['material_slots']), 'Unknown prefab instance material slot.')
    ids = {n['id'] for n in prefab['nodes']}
    require(all(o['node'] in ids for o in overrides['nodes']), 'Unknown prefab instance node override.')


def _legacy_reference(payload, expected):
    from production_contracts import validate_structure
    legacy = decode_json(payload)
    try:
        validate_structure(legacy, 'project')
    except ValueError as exc:
        fail('Linked 2D project violates production v1: ' + str(exc))
    _reference_identity(legacy, expected)


def _reference_identity(document, expected):
    require(all(document[k] == expected[k] for k in ('id', 'version', 'kind')),
            'Reference kind/identity/version does not match actual manifest.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path, help='Project root directory')
    parser.add_argument('--manifest', default='project.json')
    args = parser.parse_args()
    bundle = load_project(args.project, args.manifest)
    print(json.dumps(dict(status=bundle['status'], documents=len(bundle['documents']),
                         files=len(bundle['inventory']), bytes=bundle['bytes'],
                         inventory=bundle['inventory']), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RigError, OSError) as exc:
        import sys
        report = exc.report if isinstance(exc, RigError) else dict(code='SCENE3D_IO', stage='scene3d', message=str(exc))
        print(json.dumps(dict(ok=False, error=report)), file=sys.stderr)
        raise SystemExit(1)
