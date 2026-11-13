"""Additive, bounded look-development contracts; no renderer capability is implied.

JSON schemas and runtime structural validation share one source. Registry resolves
exact source bytes; effective materials are copied and hashable, never edited in
place. This module imports no raster, numerical or rendering dependency.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
import os
import stat
from pathlib import Path
import re
import unicodedata

from rig_errors import RigError, decode_json, finite_json, require

MAX_RESOURCE_BYTES = 2 * 1024 * 1024
MAX_RESOURCES = 1024
KINDS = ('material3d', 'material-instance3d', 'material-variant3d', 'texture3d',
         'material-graph3d', 'light3d', 'lighting-rig3d', 'look3d', 'render-quality3d')


def obj(properties, required=None):
    return dict(type='object', additionalProperties=False, properties=properties,
                required=list(properties if required is None else required))


def array(items, maximum=256, minimum=0):
    return dict(type='array', items=items, minItems=minimum, maxItems=maximum)


def mapping(items, maximum=256):
    return dict(type='object', propertyNames={'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$(?![\s\S])'},
                additionalProperties=items, maxProperties=maximum)


def canonical_bytes(value):
    finite_json(value)
    try:
        return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                           allow_nan=False) + '\n').encode('utf-8')
    except (TypeError, UnicodeError) as exc:
        raise RigError('LOOKDEV_JSON', 'Expected UTF-8-encodable JSON values.') from exc


def content_hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def portable_path(value):
    """One portable spelling, without touching or resolving the host filesystem."""
    require(isinstance(value, str) and len(value) <= 4096 and value,
            'LOOKDEV_PATH', 'Expected a bounded portable relative path.')
    require(unicodedata.normalize('NFC', value) == value and '\\' not in value,
            'LOOKDEV_PATH', 'Paths use NFC and forward slashes.')
    for part in value.split('/'):
        require(part not in ('', '.', '..') and not part.endswith((' ', '.'))
                and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF or c in '<>:"|?*' for c in part)
                and not re.fullmatch(r'(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', part),
                'LOOKDEV_PATH', 'Unsafe or platform-dependent path: ' + value)
    return value


@lru_cache(maxsize=1)
def definitions():
    text = dict(type='string', minLength=1, maxLength=4096)
    # Portable lexical path rules are shared by every resource/file reference.
    # NFC normalization and cross-document aliases additionally need runtime checks.
    reserved = (r'(?:[Cc][Oo][Nn]|[Pp][Rr][Nn]|[Aa][Uu][Xx]|[Nn][Uu][Ll]|'
                r'[Cc][Oo][Nn][Ii][Nn]\$|[Cc][Oo][Nn][Oo][Uu][Tt]\$|'
                r'[Cc][Oo][Mm][1-9¹²³]|[Ll][Pp][Tt][1-9¹²³])')
    component = r'[^/\\<>:"|?*\u0000-\u001f\u007f\ud800-\udfff]+'
    path_pattern = (r'^(?!.*(?:^|/)' + reserved + r'(?:\.|/|$))'
                    r'(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*[ .](?:/|$))' +
                    component + r'(?:/' + component + r')*$(?![\s\S])')
    portable = dict(type='string', minLength=1, maxLength=4096, pattern=path_pattern)
    identifier = dict(type='string', pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$(?![\s\S])')
    version = dict(type='string', pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$(?![\s\S])')
    digest = dict(type='string', pattern=r'^[0-9a-f]{64}$(?![\s\S])')
    unit = dict(type='number', minimum=0, maximum=1)
    number = dict(type='number', minimum=-1e9, maximum=1e9)
    positive = dict(type='number', exclusiveMinimum=0, maximum=1e9)
    nonnegative = dict(type='number', minimum=0, maximum=1e9)
    boolean = dict(type='boolean')
    rgb = array(unit, 3, 3)
    vec = array(number, 3, 3)
    color = obj(dict(value=rgb, encoding={'enum': ['linear-srgb', 'srgb']}))
    reference = obj(dict(id=identifier, version=version, path=portable, sha256=digest, kind={'enum': list(KINDS)}))
    def ref():
        return {'$ref': '#/$defs/ResourceRef'}
    policy = obj(dict(strict=boolean, allow_approximations=boolean,
                      required_features=array(identifier, 64),
                      fallback={'enum': ['reject', 'explicit-approximation', 'bake']}))
    policy['properties']['required_features']['uniqueItems'] = True
    provenance = obj(dict(origin={'enum': ['import', 'authored', 'derived', 'procedural-fixture']},
                          tool=text, created_at=dict(type='string', pattern=r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$(?![\s\S])'),
                          source_hashes=array(digest, 1024), attribution=text,
                          license=obj(dict(identifier=text, source=text))))
    common = dict(schema_version={'const': 1}, id=identifier, version=version, name=text,
                  description=text, dependencies=array(ref(), 256), provenance=provenance,
                  validation_status={'enum': ['unvalidated', 'structurally-valid', 'rejected']},
                  tags=array(identifier, 64), capabilities=array(identifier, 64))
    def resource(kind, props, optional=()):
        fields = {**common, 'kind': {'const': kind}, **props}
        return obj(fields, [k for k in fields if k not in ('name', 'description', 'tags', *optional)])
    texture_slot = obj(dict(texture=ref(), texcoord=dict(type='integer', minimum=0, maximum=7),
                            channels={'enum': ['r', 'g', 'b', 'a', 'rgb', 'rgba']},
                            scale=vec, offset=vec, rotation_radians=number,
                            strength=nonnegative))
    normal = obj(dict(texture=ref(), strength=nonnegative, orientation={'const': 'opengl-positive-y'}))
    surface_fields = dict(
        base_color=array(unit, 4, 4), base_color_space={'const': 'linear-srgb'},
        metallic=unit, roughness=unit,
        specular=obj(dict(strength=unit, color=rgb)),
        ior=dict(type='number', minimum=1, maximum=4), normal=normal,
        height=obj(dict(texture=ref(), scale_meters=number, midlevel=unit)),
        displacement=obj(dict(enabled=boolean, texture=ref(), scale_meters=number, midlevel=unit,
                              max_subdivision=dict(type='integer', minimum=0, maximum=8))),
        opacity=unit, alpha_mode={'enum': ['OPAQUE', 'MASK', 'BLEND']}, alpha_cutoff=unit,
        transmission=unit,
        volume=obj(dict(thickness_meters=nonnegative, attenuation_distance_meters=positive,
                        attenuation_color=rgb)),
        subsurface=obj(dict(weight=unit, radius_meters=array(nonnegative, 3, 3), color=rgb,
                            method={'enum': ['random-walk', 'burley']})),
        emission=obj(dict(color=rgb, strength_watts_per_square_meter=nonnegative)),
        occlusion=obj(dict(texture=ref(), strength=unit)),
        clearcoat=obj(dict(weight=unit, roughness=unit, normal=normal), ['weight', 'roughness']),
        sheen=obj(dict(weight=unit, color=rgb, roughness=unit)),
        anisotropy=obj(dict(strength=unit, direction=array(number, 2, 2), rotation_radians=number)),
        iridescence=obj(dict(weight=unit, ior=dict(type='number', minimum=1, maximum=4),
                             thickness_min_nanometers=nonnegative, thickness_max_nanometers=nonnegative)),
        double_sided=boolean, normal_orientation={'const': 'opengl-positive-y'})
    surface_required = ('base_color', 'base_color_space', 'metallic', 'roughness', 'ior',
                        'opacity', 'alpha_mode', 'alpha_cutoff', 'double_sided', 'normal_orientation')
    surface = obj(surface_fields, surface_required)
    overrides = obj(deepcopy(surface_fields), [])
    material = resource('material3d', dict(workflow={'const': 'metallic-roughness'}, surface=surface,
                         texture_slots=mapping(texture_slot, 32), graph=ref(),
                         variants=mapping(ref(), 64), render_policy=policy), ['graph'])
    scalar_slots = ('metallic', 'roughness', 'height', 'displacement', 'occlusion', 'opacity',
                    'transmission', 'thickness', 'clearcoat', 'clearcoat_roughness',
                    'sheen_roughness', 'anisotropy', 'iridescence')
    color_slots = ('base_color', 'normal', 'emission', 'sheen_color')
    typed_slots = {}
    for key in scalar_slots + color_slots:
        slot = deepcopy(texture_slot)
        slot['properties']['channels'] = {'enum': ['r', 'g', 'b', 'a'] if key in scalar_slots else
                                         (['rgb', 'rgba'] if key == 'base_color' else ['rgb'])}
        typed_slots[key] = slot
    material['properties']['texture_slots'] = obj(typed_slots, [])
    instance = resource('material-instance3d', dict(base_material=ref(), overrides=overrides,
                         render_policy=policy))
    variant = resource('material-variant3d', dict(base_material=ref(), overrides=overrides,
                        graph=ref(), render_policy=policy), ['graph'])
    texture = resource('texture3d', dict(file=obj(dict(path=portable, sha256=digest,
                         bytes=dict(type='integer', minimum=1, maximum=1024**3))),
                         role={'enum': ['base-color', 'emission', 'normal', 'metallic', 'roughness',
                                        'occlusion', 'height', 'displacement', 'opacity', 'environment',
                                        'transmission', 'thickness', 'clearcoat', 'clearcoat-roughness',
                                        'sheen-color', 'sheen-roughness', 'anisotropy', 'iridescence']},
                         encoding={'enum': ['srgb', 'linear-srgb', 'data']},
                         width=dict(type='integer', minimum=1, maximum=16384),
                         height=dict(type='integer', minimum=1, maximum=16384),
                         channels=dict(type='integer', minimum=1, maximum=4),
                         bit_depth={'enum': [8, 16, 32]},
                         wrap={'enum': ['repeat', 'clamp', 'mirror']},
                         filter={'enum': ['nearest', 'linear', 'mipmap']},
                         normal_orientation={'enum': ['opengl-positive-y', 'not-applicable']}))
    edge = obj(dict(node=identifier, socket=identifier))
    node = obj(dict(id=identifier,
                   type={'enum': ['constant-float', 'constant-color', 'texture', 'multiply-float',
                                  'mix-color', 'clamp-float', 'principled-output']},
                   inputs=mapping(edge, 16), value={'oneOf': [number, color]}, texture=ref(),
                   channels={'enum': ['r', 'g', 'b', 'a', 'rgb', 'rgba']}),
               ['id', 'type', 'inputs'])
    graph = resource('material-graph3d', dict(nodes=array(node, 256, 1),
                        output=edge, render_policy=policy))
    transform = obj(dict(translation_meters=vec, rotation_xyzw=array(number, 4, 4)))
    intensity = obj(dict(value=nonnegative, unit={'enum': ['candela', 'lux', 'watt', 'watt-per-square-meter']}))
    light = resource('light3d', dict(type={'enum': ['point', 'directional', 'spot', 'area', 'environment']},
                         transform=transform, color=color, intensity=intensity,
                         temperature_kelvin=dict(type='number', minimum=1000, maximum=40000),
                         radius_meters=nonnegative, spot_inner_radians=dict(type='number', minimum=0, maximum=3.141592653589793),
                         spot_outer_radians=dict(type='number', exclusiveMinimum=0, maximum=3.141592653589793),
                         area_size_meters=array(positive, 2, 2), environment=ref(),
                         casts_shadow=boolean, render_policy=policy),
                     ['temperature_kelvin', 'radius_meters', 'spot_inner_radians', 'spot_outer_radians',
                      'area_size_meters', 'environment'])
    lighting = resource('lighting-rig3d', dict(lights=array(ref(), 256, 1), render_policy=policy))
    look = resource('look3d', dict(materials=mapping(ref()), lighting=ref(), quality=ref(),
                        identity= obj(dict(id=identifier, version=version, sha256=digest)),
                        style=obj(dict(id=identifier, version=version, sha256=digest)),
                        color_management=obj(dict(working_space={'const': 'linear-srgb'},
                            output_encoding={'enum': ['srgb', 'linear-srgb']},
                            exposure_stops=dict(type='number', minimum=-32, maximum=32),
                            white_balance_kelvin=dict(type='number', minimum=1000, maximum=40000),
                            tone_mapping={'enum': ['none', 'agx', 'aces', 'reinhard']}))))
    quality = resource('render-quality3d', dict(samples=dict(type='integer', minimum=1, maximum=65536),
                         max_bounces=dict(type='integer', minimum=0, maximum=64),
                         resolution=array(dict(type='integer', minimum=1, maximum=16384), 2, 2),
                         seed=dict(type='integer', minimum=0, maximum=2**32-1), denoise=boolean,
                         budgets=obj(dict(memory_bytes=dict(type='integer', minimum=1024**2, maximum=64*1024**3),
                              texture_bytes=dict(type='integer', minimum=1, maximum=16*1024**3),
                              timeout_seconds=dict(type='number', exclusiveMinimum=0, maximum=86400))),
                         render_policy=policy))
    return dict(ResourceRef=reference, Surface=surface, SurfaceOverrides=overrides,
                **dict(zip(KINDS, (material, instance, variant, texture, graph, light, lighting, look, quality))))


def default_surface():
    """Explicit dielectric defaults; serialization never injects hidden values."""
    return dict(base_color=[0.8, 0.8, 0.8, 1.0], base_color_space='linear-srgb',
                metallic=0.0, roughness=0.5, ior=1.5, opacity=1.0,
                alpha_mode='OPAQUE', alpha_cutoff=0.5, double_sided=False,
                normal_orientation='opengl-positive-y',
                specular=dict(strength=1.0, color=[1.0, 1.0, 1.0]), transmission=0.0,
                emission=dict(color=[0.0, 0.0, 0.0], strength_watts_per_square_meter=0.0),
                clearcoat=dict(weight=0.0, roughness=0.0),
                sheen=dict(weight=0.0, color=[0.0, 0.0, 0.0], roughness=0.5),
                anisotropy=dict(strength=0.0, direction=[1.0, 0.0], rotation_radians=0.0))


def schemas():
    # Each standalone schema carries only its reachable definitions.
    result = {}
    def referenced(value):
        if isinstance(value, dict):
            if '$ref' in value:
                yield value['$ref'].split('/')[-1]
            for child in value.values():
                yield from referenced(child)
        elif isinstance(value, list):
            for child in value:
                yield from referenced(child)
    for kind in KINDS:
        selected, pending = {}, [kind]
        while pending:
            key = pending.pop()
            if key not in selected:
                selected[key] = deepcopy(definitions()[key])
                pending.extend(referenced(selected[key]))
        result['lookdev-' + kind + '.schema.json'] = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'https://gethatch.pet/schemas/lookdev-' + kind + '-v1',
            'title': 'Hatch look development ' + kind + ' v1',
            'description': 'Contract validity alone proves no executed shader or renderer capability.',
            '$defs': selected, '$ref': '#/$defs/' + kind}
    return result


@lru_cache(maxsize=16)
def _validator(kind):
    require(kind in KINDS, 'LOOKDEV_KIND', 'Unknown look-development resource kind.')
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RigError('LOOKDEV_DEPENDENCY', 'Install optional requirements-rig.txt for contract validation.') from exc
    return Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/' + kind})


def _references(value):
    if isinstance(value, dict):
        if set(value) == {'id', 'version', 'path', 'sha256', 'kind'}:
            yield value
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _walk_paths(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'path':
                portable_path(child)
            else:
                _walk_paths(child)
    elif isinstance(value, list):
        for child in value:
            _walk_paths(child)


def _surface_checks(surface):
    if 'anisotropy' in surface:
        direction = surface['anisotropy']['direction']
        require(abs(sum(x*x for x in direction) - 1) <= 1e-6,
                'LOOKDEV_DIRECTION', 'Anisotropy tangent direction must have unit length.')
    if 'iridescence' in surface:
        ir = surface['iridescence']
        require(ir['thickness_min_nanometers'] <= ir['thickness_max_nanometers'],
                'LOOKDEV_RANGE', 'Iridescent film thickness interval is reversed.')
    if surface.get('alpha_mode') == 'OPAQUE':
        require(surface.get('opacity', 1) == 1 and surface.get('base_color', [1]*4)[3] == 1,
                'LOOKDEV_ALPHA', 'OPAQUE must have full surface coverage; use MASK or BLEND.')


NODE_TYPES = {
    'constant-float': ({}, {'value': 'float'}),
    'constant-color': ({}, {'color': 'color'}),
    'texture': ({}, {'color': 'color', 'value': 'float'}),
    'clamp-float': ({'value': 'float'}, {'value': 'float'}),
    'multiply-float': ({'a': 'float', 'b': 'float'}, {'value': 'float'}),
    'mix-color': ({'a': 'color', 'b': 'color', 'factor': 'float'}, {'color': 'color'}),
    'principled-output': ({'base_color': 'color', 'roughness': 'float', 'metallic': 'float'}, {'surface': 'surface'}),
}


def _node_types(node):
    inputs, outputs = NODE_TYPES[node['type']]
    if node['type'] == 'texture':
        outputs = {'color': 'color'} if node.get('channels') in ('rgb', 'rgba') else {'value': 'float'}
    return inputs, outputs


def _acyclic(edges, code):
    visiting, done = set(), set()
    def visit(key):
        require(key not in visiting, code, 'Cyclic dependency: ' + str(key))
        if key in done:
            return
        visiting.add(key)
        for dependency in edges[key]:
            visit(dependency)
        visiting.remove(key)
        done.add(key)
    # Resource/node limits keep recursion under the interpreter recursion limit.
    try:
        for key in edges:
            visit(key)
    except RecursionError as exc:
        raise RigError('LOOKDEV_DEPTH', 'Dependency chain exceeds supported depth.') from exc


def _graph_checks(value):
    nodes = {node['id']: node for node in value['nodes']}
    require(len(nodes) == len(value['nodes']), 'LOOKDEV_DUPLICATE', 'Duplicate graph node ID.')
    edges = {}
    for key, node in nodes.items():
        expected_inputs, _ = _node_types(node)
        require(set(node['inputs']) == set(expected_inputs), 'LOOKDEV_SOCKET', 'Missing or unknown node input sockets.')
        require(('value' in node) == node['type'].startswith('constant-'), 'LOOKDEV_NODE', 'Only constant nodes carry values.')
        require(('texture' in node) == (node['type'] == 'texture'), 'LOOKDEV_NODE', 'Only texture nodes carry texture references.')
        require(('channels' in node) == (node['type'] == 'texture'),
                'LOOKDEV_NODE', 'Texture nodes require explicit channels; other nodes cannot carry channels.')
        if node['type'] == 'constant-float':
            require(type(node['value']) in (int, float), 'LOOKDEV_TYPE', 'Expected a scalar constant.')
        if node['type'] == 'constant-color':
            require(isinstance(node['value'], dict), 'LOOKDEV_TYPE', 'Expected an explicitly encoded color constant.')
        edges[key] = []
        for socket, link in node['inputs'].items():
            require(link['node'] in nodes, 'LOOKDEV_REFERENCE', 'Dangling graph node.')
            outputs = _node_types(nodes[link['node']])[1]
            require(outputs.get(link['socket']) == expected_inputs[socket],
                    'LOOKDEV_TYPE', 'Graph socket types do not match.')
            edges[key].append(link['node'])
    _acyclic(edges, 'LOOKDEV_CYCLE')
    ranges = {}
    def interval(key):
        if key in ranges:
            return ranges[key]
        node = nodes[key]
        if node['type'] == 'constant-float':
            result = (node['value'], node['value'])
        elif node['type'] == 'texture':
            # Texture scalar sampling is normalized/clamped to [0,1]. HDR color
            # sampling remains a color output, never an implicit scalar coercion.
            result = (0, 1)
        elif node['type'] == 'clamp-float':
            low, high = interval(node['inputs']['value']['node'])
            result = (max(0, min(1, low)), max(0, min(1, high)))
        elif node['type'] == 'multiply-float':
            left = interval(node['inputs']['a']['node'])
            right = interval(node['inputs']['b']['node'])
            products = [a*b for a in left for b in right]
            result = (min(products), max(products))
        else:
            raise RigError('LOOKDEV_TYPE', 'Requested a scalar interval from a non-scalar node.')
        require(all(math.isfinite(x) and abs(x) <= 1e9 for x in result),
                'LOOKDEV_GRAPH_RANGE', 'Graph arithmetic exceeds the finite +/-1e9 bound.')
        ranges[key] = result
        return result
    for node in nodes.values():
        if 'value' in _node_types(node)[1] and _node_types(node)[1]['value'] == 'float':
            interval(node['id'])
        bounded = ('roughness', 'metallic') if node['type'] == 'principled-output' else \
                  (('factor',) if node['type'] == 'mix-color' else ())
        for socket in bounded:
            low, high = interval(node['inputs'][socket]['node'])
            require(0 <= low <= high <= 1, 'LOOKDEV_GRAPH_RANGE',
                    'Physical graph input is outside [0,1]; add an explicit clamp-float node.')
    out = value['output']
    require(out['node'] in nodes and _node_types(nodes[out['node']])[1].get(out['socket']) == 'surface',
            'LOOKDEV_OUTPUT', 'Graph output must reference a surface socket.')


def validate_resource(value):
    """Validate one document; use Registry to check dependency closure and bytes."""
    finite_json(value)
    require(isinstance(value, dict), 'LOOKDEV_SCHEMA', 'Resource must be an object.')
    require(isinstance(value.get('kind'), str), 'LOOKDEV_KIND', 'Resource kind must be a string.')
    errors = _validator(value['kind']).iter_errors(value)
    error = next(errors, None)
    if error:
        raise RigError('LOOKDEV_SCHEMA', error.message, asset='.'.join(map(str, error.absolute_path)))
    require(len(canonical_bytes(value)) <= MAX_RESOURCE_BYTES, 'LOOKDEV_LIMIT', 'Resource exceeds 2 MiB.')
    _walk_paths(value)
    try:
        datetime.strptime(value['provenance']['created_at'], '%Y-%m-%dT%H:%M:%SZ')
    except ValueError as exc:
        raise RigError('LOOKDEV_TIMESTAMP', 'Provenance timestamp must be a valid UTC date/time.') from exc
    for field in ('tags', 'capabilities'):
        require(len(value.get(field, [])) == len(set(value.get(field, []))), 'LOOKDEV_DUPLICATE', 'Duplicate ' + field)
    policy = value.get('render_policy')
    if policy:
        require(not (policy['strict'] and (policy['allow_approximations'] or policy['fallback'] != 'reject')),
                'LOOKDEV_POLICY', 'Strict rendering forbids approximations and fallback.')
    dependencies = value['dependencies']
    keys = [(r['id'], r['version']) for r in dependencies]
    require(len(keys) == len(set(keys)), 'LOOKDEV_DUPLICATE', 'Duplicate or conflicting dependencies.')
    declared = {canonical_bytes(r) for r in dependencies}
    for field, child in value.items():
        if field != 'dependencies':
            for reference in _references(child):
                require(canonical_bytes(reference) in declared, 'LOOKDEV_DEPENDENCY', 'Resource reference is not declared in dependencies.')
    if value['kind'] == 'material3d':
        _surface_checks(value['surface'])
        require(not set(value['texture_slots']).intersection(
                    key for key in ('normal', 'height', 'displacement', 'occlusion') if key in value['surface']),
                'LOOKDEV_TEXTURE_CONFLICT', 'Use either an embedded map or its texture slot, not both.')
    elif value['kind'] in ('material-instance3d', 'material-variant3d'):
        _surface_checks(value['overrides'])
        require(value['base_material']['kind'] in ('material3d', 'material-instance3d', 'material-variant3d'),
                'LOOKDEV_TYPE', 'Base material must be a material, instance or variant.')
    elif value['kind'] == 'texture3d':
        data_role = value['role'] not in ('base-color', 'emission', 'environment', 'sheen-color')
        require((value['encoding'] == 'data') == data_role, 'LOOKDEV_COLOR', 'Color maps and data maps require distinct encodings.')
        require((value['normal_orientation'] == 'opengl-positive-y') == (value['role'] == 'normal'),
                'LOOKDEV_NORMAL', 'Normal convention applies only to normal maps.')
        require(value['role'] != 'normal' or value['channels'] >= 3,
                'LOOKDEV_CHANNEL', 'A tangent normal map requires at least RGB channels.')
        require(value['width'] * value['height'] * value['channels'] * (value['bit_depth']//8) <= 1024**3,
                'LOOKDEV_LIMIT', 'Decoded texture exceeds the 1 GiB bound.')
    elif value['kind'] == 'material-graph3d':
        _graph_checks(value)
    elif value['kind'] == 'light3d':
        light_type = value['type']
        expected_unit = {'point': 'candela', 'spot': 'candela', 'directional': 'lux',
                         'area': 'watt', 'environment': 'watt-per-square-meter'}[light_type]
        require(value['intensity']['unit'] == expected_unit, 'LOOKDEV_UNIT', 'Intensity unit conflicts with light type.')
        required = {'point': {'radius_meters'}, 'directional': set(),
                    'spot': {'radius_meters', 'spot_inner_radians', 'spot_outer_radians'},
                    'area': {'area_size_meters'}, 'environment': {'environment'}}[light_type]
        conditional = {'radius_meters', 'spot_inner_radians', 'spot_outer_radians', 'area_size_meters', 'environment'}
        require(set(value).intersection(conditional) == required, 'LOOKDEV_LIGHT', 'Missing or inapplicable light-type settings.')
        if light_type == 'spot':
            require(value['spot_inner_radians'] <= value['spot_outer_radians'], 'LOOKDEV_RANGE', 'Spot inner angle exceeds outer angle.')
        rotation = value['transform']['rotation_xyzw']
        require(abs(sum(x*x for x in rotation) - 1) <= 1e-6, 'LOOKDEV_ROTATION', 'Light rotation must be a unit XYZW quaternion.')
    Registry._typed_references(value)
    return deepcopy(value)


class Registry:
    """Immutable, in-memory exact-byte resolver. No disk publication or renderer.

    Entries are (portable path, source bytes), with a bounded aggregate of 32 MiB.
    Referenced texture payloads are mandatory exact bytes (64 MiB aggregate).
    Consumers receive defensive copies. All declared dependencies must be present.
    """
    def __init__(self, entries, *, payloads=None):
        self._values, self._refs = {}, {}
        require(payloads is None or isinstance(payloads, dict), 'LOOKDEV_PAYLOAD', 'Payloads must map portable paths to bytes.')
        payloads = {} if payloads is None else payloads
        payload_total, payload_aliases = 0, set()
        for path, payload in payloads.items():
            portable_path(path)
            require(path.casefold() not in payload_aliases and isinstance(payload, bytes),
                    'LOOKDEV_PAYLOAD', 'Payload paths must be unique and contents must be bytes.')
            payload_aliases.add(path.casefold())
            payload_total += len(payload)
            require(payload_total <= 64*1024**2, 'LOOKDEV_LIMIT', 'Registry payloads exceed 64 MiB.')
        require(isinstance(entries, (list, tuple)) and len(entries) <= MAX_RESOURCES,
                'LOOKDEV_LIMIT', 'Expected at most 1024 resources.')
        total, paths = 0, set()
        for entry in entries:
            require(isinstance(entry, (tuple, list)) and len(entry) == 2,
                    'LOOKDEV_INPUT', 'Each resource entry must be a path/bytes pair.')
            path, payload = entry
            portable_path(path)
            alias = path.casefold()
            require(alias not in paths, 'LOOKDEV_DUPLICATE', 'Aliased or duplicate resource path.')
            paths.add(alias)
            require(isinstance(payload, bytes) and len(payload) <= MAX_RESOURCE_BYTES,
                    'LOOKDEV_LIMIT', 'Resource source must be at most 2 MiB of bytes.')
            total += len(payload)
            require(total <= 32*1024**2, 'LOOKDEV_LIMIT', 'Registry source bytes exceed 32 MiB.')
            value = validate_resource(decode_json(payload))
            key = (value['id'], value['version'])
            require(key not in self._values, 'LOOKDEV_DUPLICATE', 'Conflicting resource identity/version.')
            self._values[key] = value
            self._refs[key] = dict(id=value['id'], version=value['version'], kind=value['kind'],
                                   path=path, sha256=hashlib.sha256(payload).hexdigest())
        require(not paths.intersection(payload_aliases), 'LOOKDEV_DUPLICATE', 'Resource and payload paths overlap.')
        edges = {}
        for key, value in self._values.items():
            if value['kind'] == 'texture3d':
                source = value['file']
                payload = payloads.get(source['path'])
                require(payload is not None and len(payload) == source['bytes'] and
                        hashlib.sha256(payload).hexdigest() == source['sha256'],
                        'LOOKDEV_PAYLOAD', 'Missing or corrupt exact texture source bytes.')
            edges[key] = []
            for reference in value['dependencies']:
                target = (reference['id'], reference['version'])
                require(self._refs.get(target) == reference, 'LOOKDEV_REFERENCE', 'Missing dependency or exact reference mismatch.')
                edges[key].append(target)
            self._typed_references(value)
            self._resolved_checks(value)
        _acyclic(edges, 'LOOKDEV_CYCLE')
        for key, value in self._values.items():
            if value['kind'] in ('material-instance3d', 'material-variant3d'):
                self.effective_material(self._refs[key])

    @staticmethod
    def _typed_references(value):
        def kind(reference, allowed):
            require(reference['kind'] in allowed, 'LOOKDEV_TYPE', 'Reference has the wrong resource kind.')
        resource_kind = value['kind']
        if resource_kind in ('material3d', 'material-instance3d', 'material-variant3d'):
            for field in ('surface', 'overrides', 'texture_slots'):
                for reference in _references(value.get(field, {})):
                    kind(reference, ('texture3d',))
            if 'graph' in value:
                kind(value['graph'], ('material-graph3d',))
            for reference in value.get('variants', {}).values():
                kind(reference, ('material-variant3d',))
        elif resource_kind == 'material-graph3d':
            for node in value['nodes']:
                if 'texture' in node:
                    kind(node['texture'], ('texture3d',))
        elif resource_kind == 'light3d' and 'environment' in value:
            kind(value['environment'], ('texture3d',))
        elif resource_kind == 'lighting-rig3d':
            for reference in value['lights']:
                kind(reference, ('light3d',))
        elif resource_kind == 'look3d':
            for reference in value['materials'].values():
                kind(reference, ('material3d', 'material-instance3d', 'material-variant3d'))
            kind(value['lighting'], ('lighting-rig3d',))
            kind(value['quality'], ('render-quality3d',))

    def _resolved_checks(self, value):
        def role(reference, expected):
            actual = self.get(reference)
            require(actual['kind'] == 'texture3d' and actual['role'] == expected,
                    'LOOKDEV_TEXTURE_ROLE', 'Texture role conflicts with its material/light slot.')
        if value['kind'] in ('material3d', 'material-instance3d', 'material-variant3d'):
            for field in ('surface', 'overrides'):
                surface = value.get(field, {})
                for key in ('normal', 'height', 'displacement', 'occlusion'):
                    if key in surface:
                        role(surface[key]['texture'], key)
                if 'normal' in surface.get('clearcoat', {}):
                    role(surface['clearcoat']['normal']['texture'], 'normal')
            direct_roles = {'base_color': 'base-color', 'metallic': 'metallic', 'roughness': 'roughness',
                            'normal': 'normal', 'height': 'height', 'displacement': 'displacement',
                            'occlusion': 'occlusion', 'emission': 'emission', 'opacity': 'opacity'}
            for key, slot in value.get('texture_slots', {}).items():
                role(slot['texture'], direct_roles.get(key, key.replace('_', '-')))
                _check_channels(slot['channels'], self.get(slot['texture'])['channels'])
        elif value['kind'] == 'material-graph3d':
            for node in value['nodes']:
                if node['type'] == 'texture':
                    texture = self.get(node['texture'])
                    channels = node['channels']
                    _check_channels(channels, texture['channels'])
                    if channels in ('rgb', 'rgba'):
                        require(texture['role'] in ('base-color', 'emission', 'environment', 'sheen-color'),
                                'LOOKDEV_COLOR', 'Data textures cannot implicitly produce graph color; use a declared color texture.')
        elif value['kind'] == 'light3d' and value['type'] == 'environment':
            role(value['environment'], 'environment')

    def get(self, reference):
        require(isinstance(reference, dict) and isinstance(reference.get('id'), str) and
                isinstance(reference.get('version'), str), 'LOOKDEV_REFERENCE', 'Expected an exact resource reference.')
        key = (reference['id'], reference['version'])
        require(self._refs.get(key) == reference, 'LOOKDEV_REFERENCE', 'Exact resource reference mismatch.')
        return deepcopy(self._values[key])

    def reference(self, identifier, version):
        require(isinstance(identifier, str) and isinstance(version, str),
                'LOOKDEV_REFERENCE', 'Resource identity and version must be strings.')
        key = (identifier, version)
        require(key in self._refs, 'LOOKDEV_REFERENCE', 'Unknown resource.')
        return deepcopy(self._refs[key])

    def effective_material(self, reference):
        value = self.get(reference)
        chain = []
        while value['kind'] in ('material-instance3d', 'material-variant3d'):
            chain.append(value)
            require(len(chain) <= 64, 'LOOKDEV_DEPTH', 'Material inheritance exceeds 64 levels.')
            value = self.get(value['base_material'])
        require(value['kind'] == 'material3d', 'LOOKDEV_TYPE', 'Expected a material base.')
        for instance in reversed(chain):
            # Surface fields are typed complete properties, not arbitrary JSON patches.
            value['surface'].update(deepcopy(instance['overrides']))
            value['render_policy'] = _inherit_policy(value['render_policy'], instance['render_policy'])
            if 'graph' in instance:
                value['graph'] = deepcopy(instance['graph'])
        _surface_checks(value['surface'])
        require(not set(value['texture_slots']).intersection(
                    key for key in ('normal', 'height', 'displacement', 'occlusion') if key in value['surface']),
                'LOOKDEV_TEXTURE_CONFLICT', 'An inherited embedded map conflicts with the base texture slot.')
        return dict(surface=value['surface'], texture_slots=value['texture_slots'],
                    graph=value.get('graph'), render_policy=value['render_policy'],
                    sha256=content_hash(dict(surface=value['surface'], texture_slots=value['texture_slots'],
                                            graph=value.get('graph'), render_policy=value['render_policy'])))

    def check_material_compatibility(self, reference, supported_features):
        effective = self.effective_material(reference)
        base = self.get(reference)
        # Rebuild a standalone effective definition for the same strict checker.
        definition = dict(base)
        definition.update(kind='material3d', workflow='metallic-roughness',
                          surface=effective['surface'], texture_slots=effective['texture_slots'],
                          variants={}, render_policy=effective['render_policy'])
        for key in ('base_material', 'overrides', 'graph'):
            definition.pop(key, None)
        if effective['graph']:
            definition['graph'] = effective['graph']
        references = list(_references(definition['surface'])) + list(_references(definition['texture_slots']))
        if 'graph' in definition:
            references.append(definition['graph'])
        definition['dependencies'] = list({canonical_bytes(r): r for r in references}.values())
        result = _check_compatibility(definition, supported_features, resolved_material=True)
        if effective['graph']:
            graph_result = check_compatibility(self.get(effective['graph']), supported_features)
            missing = set(result['missing']) | set(graph_result['missing'])
            result = _check_features(definition['render_policy'], supported_features, missing)
        return result


def _surface_features(surface):
    features = {'metallic-roughness'}
    for key in ('normal', 'height', 'occlusion'):
        if key in surface:
            features.add(key)
    for key, field in (('transmission', None), ('subsurface', 'weight'),
                       ('clearcoat', 'weight'), ('sheen', 'weight'),
                       ('anisotropy', 'strength'), ('iridescence', 'weight')):
        item = surface.get(key, 0)
        if (item.get(field, 0) if field and isinstance(item, dict) else item) > 0:
            features.add(key)
    if surface.get('displacement', {}).get('enabled'):
        features.add('true-displacement')
    if surface.get('emission', {}).get('strength_watts_per_square_meter', 0) > 0:
        features.add('emission')
    if surface.get('alpha_mode', 'OPAQUE') != 'OPAQUE':
        features.add('alpha-' + surface['alpha_mode'].lower())
    if surface.get('specular', {'strength': 1, 'color': [1, 1, 1]}) != {'strength': 1, 'color': [1, 1, 1]}:
        features.add('specular')
    if surface.get('ior', 1.5) != 1.5:
        features.add('ior')
    if surface.get('volume', {}).get('thickness_meters', 0) > 0:
        features.add('volume')
    if surface.get('double_sided'):
        features.add('double-sided')
    return features


def _slot_features(slots):
    features = set()
    role_features = dict(normal='normal', height='height', displacement='true-displacement',
                         occlusion='occlusion', emission='emission', opacity='opacity',
                         transmission='transmission', thickness='volume', clearcoat='clearcoat',
                         clearcoat_roughness='clearcoat', sheen_color='sheen', sheen_roughness='sheen',
                         anisotropy='anisotropy', iridescence='iridescence')
    for slot, setting in slots.items():
        if setting['strength'] > 0 and slot in role_features:
            features.add(role_features[slot])
    return features


def _inherit_policy(base, derived):
    strict = base['strict'] or derived['strict']
    allowed = base['allow_approximations'] and derived['allow_approximations'] and not strict
    fallbacks = {base['fallback'], derived['fallback']}
    fallback = next(iter(fallbacks)) if len(fallbacks) == 1 and allowed else 'reject'
    return dict(strict=strict, allow_approximations=allowed,
                fallback=fallback, required_features=sorted(set(base['required_features']) | set(derived['required_features'])))


def _check_channels(selection, count):
    required = {'r': 1, 'g': 2, 'b': 3, 'a': 4, 'rgb': 3, 'rgba': 4}[selection]
    require(count >= required, 'LOOKDEV_CHANNEL', 'Selected texture channels are unavailable.')


def _check_features(policy, supported_features, needed):
    missing = sorted(set(needed) - supported_features)
    require(not missing or (not policy['strict'] and policy['fallback'] != 'reject' and policy['allow_approximations']),
            'LOOKDEV_UNSUPPORTED', 'Adapter does not support required features: ' + ', '.join(missing))
    return dict(status='requires-explicit-fallback' if missing else 'compatible', missing=missing, executed=False)


def check_compatibility(resource, supported_features):
    return _check_compatibility(resource, supported_features)


def _check_compatibility(resource, supported_features, *, resolved_material=False):
    """Validate against capabilities supplied by an actually probed adapter.

    This contract module declares no Blender/browser support. Approximation is
    returned as a diagnostic, never silently treated as feature execution.
    """
    value = validate_resource(resource)
    require(isinstance(supported_features, (set, frozenset)) and all(isinstance(x, str) for x in supported_features),
            'LOOKDEV_CAPABILITY', 'Adapter must supply a set of feature identifiers.')
    policy = value.get('render_policy')
    require(policy is not None, 'LOOKDEV_POLICY', 'Resource has no renderer policy.')
    require(value['kind'] not in ('material-instance3d', 'material-variant3d', 'lighting-rig3d'),
            'LOOKDEV_CAPABILITY', 'A single-document checker cannot verify inherited materials or lighting rigs; resolve their dependency scope first.')
    require(not (value['kind'] == 'material3d' and value.get('graph') and not resolved_material),
            'LOOKDEV_CAPABILITY', 'Referenced graph policies require Registry.check_material_compatibility().')
    needed = set(policy['required_features'])
    if value['kind'] == 'material3d':
        needed.update(_surface_features(value['surface']))
        if value.get('graph'):
            needed.add('material-graph')
        if value['texture_slots']:
            needed.add('texture-slots')
            needed.update(_slot_features(value['texture_slots']))
    elif value['kind'] == 'light3d':
        needed.add('light-' + value['type'])
        if value['casts_shadow']:
            needed.add('shadows')
    elif value['kind'] == 'render-quality3d':
        needed.add('render-quality')
        if value['denoise']:
            needed.add('denoise')
        if value['max_bounces'] > 0:
            needed.add('indirect-lighting')
    elif value['kind'] == 'material-graph3d':
        needed.update('graph-' + node['type'] for node in value['nodes'])
    return _check_features(policy, supported_features, needed)


def read_document(path):
    """Read a bounded regular JSON file without blocking on a named pipe."""
    path = Path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_RESOURCE_BYTES,
            'LOOKDEV_INPUT', 'Expected a bounded regular resource file, not a link/device/pipe.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode) and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                'LOOKDEV_CHANGED', 'Resource file changed while opening.')
        payload = stream.read(MAX_RESOURCE_BYTES + 1)
        after = os.fstat(stream.fileno())
        require(len(payload) <= MAX_RESOURCE_BYTES and (opened.st_size, opened.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                'LOOKDEV_CHANGED', 'Resource file changed or exceeds the byte bound.')
    return decode_json(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check generated schemas for drift.')
    parser.add_argument('--write-schemas', action='store_true')
    parser.add_argument('--validate', type=Path, help='Validate one resource document (not dependency closure).')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.check or args.write_schemas:
        for name, schema in schemas().items():
            path = root / 'profiles' / name
            payload = json.dumps(schema, indent=2) + '\n'
            if args.check:
                require(path.is_file() and path.read_text(encoding='utf-8') == payload,
                        'LOOKDEV_SCHEMA_DRIFT', 'Schema drift: ' + name)
            else:
                path.write_text(payload, encoding='utf-8')
    if args.validate:
        result = validate_resource(read_document(args.validate))
        print(json.dumps(dict(kind=result['kind'], id=result['id'], status='structurally-valid', executed=False)))


if __name__ == '__main__':
    main()
