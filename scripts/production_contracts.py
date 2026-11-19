"""Portable, bounded production contracts and fail-closed semantic validation.

This implementation was reconstructed after loss of the unpublished workspace.
Its public data model is versioned independently of legacy hatch-pet manifests.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import copy
import hashlib
import json
import math
import re
from pathlib import Path

from PIL import Image

from asset_contracts import MAX_SOURCE_PIXELS, MAX_WORK_BYTES
from rig_errors import open_payload_at, owned_directory_fd

SCHEMA_VERSION = 1
CONTRACT_VERSION = '1.0.0'
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_FRAMES = 18000
MAX_ASSETS = 2048
MAX_SHOTS = 256
MAX_COMPONENTS = 128
MAX_PROJECT_BYTES = 1024 * 1024 * 1024
DIMENSIONS = ('action', 'pose', 'viewpoint', 'facing', 'expression', 'intensity',
              'gaze', 'mouth', 'hand', 'costume', 'prop_state', 'contact')
CHANNELS = ('body', 'face', 'gaze', 'mouth')
VIEWPOINTS = ('front', 'side-left', 'side-right', 'back', 'three-quarter-left',
             'three-quarter-right', 'three-quarter-back-left', 'three-quarter-back-right')
FACINGS = ('front', 'left', 'right', 'back')
ID_PATTERN = r'^[a-z][a-z0-9_-]{0,63}$'
VERSION_PATTERN = r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
KEY_PATTERN = r'^[a-z][a-z0-9_-]{0,63}@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'


def obj(properties, required=(), **kwargs):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(required), **kwargs}


def arr(items, minimum=0, maximum=4096, **kwargs):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum, **kwargs}


def ref(name):
    return {'$ref': '#/$defs/' + name}


def number(low=-100000, high=100000):
    return {'type': 'number', 'minimum': low, 'maximum': high}


def integer(low=0, high=MAX_FRAMES):
    return {'type': 'integer', 'minimum': low, 'maximum': high}


def enum(*values):
    return {'enum': list(values)}


def nullable(value):
    return {'anyOf': [{'type': 'null'}, value]}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 16384}
ID = {'type': 'string', 'pattern': ID_PATTERN}
VERSION = {'type': 'string', 'pattern': VERSION_PATTERN}
KEY = {'type': 'string', 'pattern': KEY_PATTERN}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'}
POINT = arr(number(), 2, 2)
SIZE = arr(integer(1, 8192), 2, 2)
BOOL = {'type': 'boolean'}
STRING_LIST = arr(TEXT, maximum=256, uniqueItems=True)
ID_LIST = arr(ID, maximum=128, uniqueItems=True)
REQUIREMENTS = obj({dimension: STRING_LIST for dimension in DIMENSIONS})
TRACK = obj({'interpolation': enum('step', 'linear'),
             'keys': arr(obj({'frame': integer(), 'value': {'anyOf': [number(), POINT, TEXT, BOOL]}},
                             ('frame', 'value')), 1, 4096)}, ('interpolation', 'keys'))
PROPERTIES = obj({name: ref('track') for name in ('offset', 'rotation', 'drawing', 'visible')})
TRACKS = obj({**{channel: {'type': 'object', 'propertyNames': ID,
                          'additionalProperties': ref('component_tracks'), 'maxProperties': MAX_COMPONENTS}
                 for channel in CHANNELS},
              'root': obj({name: ref('track') for name in ('offset', 'facing', 'visible')})})


def schema_definitions():
    definitions = {'track': TRACK, 'component_tracks': PROPERTIES, 'tracks': TRACKS,
                   'requirements': REQUIREMENTS}
    definitions['file'] = obj({'path': {'type': 'string', 'minLength': 1, 'maxLength': 1024}, 'sha256': SHA},
                              ('path', 'sha256'))
    definitions['model_call'] = obj({'id': nullable(TEXT), 'model': nullable(TEXT),
        'parameters': nullable({'type': 'object', 'maxProperties': 64}), 'unavailable_reason': TEXT},
        ('id', 'model', 'parameters'))
    definitions['provenance'] = obj({'kind': enum('fixture', 'import', 'imagegen', 'derived'),
        'origin': TEXT, 'fixture': BOOL, 'attribution': TEXT, 'permissions': TEXT, 'source': ref('file'),
        'references': arr(ref('file'), maximum=256, uniqueItems=True),
        'source_sha256': SHA, 'note': TEXT,
        'operation': obj({'kind': TEXT, 'rect': arr(integer(0, 8192), 4, 4), 'note': TEXT}, ('kind',)),
        'model_call': ref('model_call'),
        'acquisition': obj({'run_id': TEXT, 'job': TEXT, 'receipt_path': TEXT, 'receipt_sha256': SHA,
                            'model_call': ref('model_call')}, ('run_id', 'job', 'receipt_path', 'receipt_sha256'))},
        ('kind', 'origin', 'fixture'))
    definitions['image'] = obj({'path': definitions['file']['properties']['path'], 'sha256': SHA,
                                'size': SIZE, 'provenance': ref('provenance')}, ('path', 'sha256', 'size', 'provenance'))
    definitions['view_review'] = obj({'status': enum('pending', 'approved', 'rejected'),
        'kind': enum('human', 'assistant_visual', 'synthetic', 'unreviewed'), 'reviewer': nullable(TEXT),
        'evidence': arr(ref('file'), maximum=256), 'note': TEXT}, ('status', 'kind', 'reviewer', 'evidence', 'note'))
    definitions['drawing'] = obj({'viewpoint': enum(*VIEWPOINTS), 'costume': ID,
                                 'image': ref('image'), 'alpha': {'const': 'straight'}},
                                ('viewpoint', 'costume', 'image', 'alpha'))
    definitions['component'] = obj({'id': ID,
        'role': enum('body', 'torso', 'head', 'limb', 'eyes', 'brows', 'mouth', 'hands', 'hair', 'clothing', 'accessory', 'other'),
        'parent': nullable(ID), 'parent_anchor': nullable(ID), 'pivot': POINT, 'offset': POINT,
        'anchors': {'type': 'object', 'propertyNames': ID, 'additionalProperties': POINT, 'maxProperties': 128},
        'z': integer(-10000, 10000),
        'drawings': {'type': 'object', 'propertyNames': ID, 'additionalProperties': ref('drawing'), 'minProperties': 1, 'maxProperties': 256},
        'default_drawing': ID, 'mask': nullable(ref('image')), 'visible': BOOL},
        ('id', 'role', 'parent', 'parent_anchor', 'pivot', 'offset', 'anchors', 'z', 'drawings', 'default_drawing', 'visible'))
    definitions['identity'] = obj({
        'proportions': {'type': 'object', 'additionalProperties': number(.0001, 10000), 'maxProperties': 128},
        'palette': {'type': 'object', 'additionalProperties': {'type': 'string', 'pattern': r'^#[0-9a-fA-F]{6}$'}, 'maxProperties': 128},
        'materials': STRING_LIST, 'markings': STRING_LIST, 'asymmetries': STRING_LIST,
        'allowed_facings': arr(enum(*FACINGS), 1, 4, uniqueItems=True), 'forbid_mirror': BOOL},
        ('proportions', 'palette', 'materials', 'markings', 'asymmetries', 'allowed_facings', 'forbid_mirror'))
    definitions['costume'] = obj({'id': ID, 'differences': STRING_LIST,
        'drawings': {'type': 'object', 'propertyNames': ID, 'additionalProperties': ID, 'maxProperties': 128}},
        ('id', 'differences', 'drawings'))
    definitions['character'] = obj({'id': ID, 'version': VERSION, 'name': TEXT,
        'dimensions': obj({'width': integer(1, 8192), 'height': integer(1, 8192)}, ('width', 'height')),
        'relative_scale': number(.01, 10), 'identity': ref('identity'),
        'views': arr(enum(*VIEWPOINTS), 1, 8, uniqueItems=True),
        'view_reviews': {'type': 'object', 'propertyNames': enum(*VIEWPOINTS), 'additionalProperties': ref('view_review'), 'maxProperties': 8},
        'components': arr(ref('component'), 1, MAX_COMPONENTS), 'costumes': arr(ref('costume'), 1, 64),
        'capabilities': ref('requirements'),
        'restrictions': arr(obj({'when': ref('requirements'), 'reason': TEXT}, ('when', 'reason')), maximum=128),
        'references': arr(ref('file'), maximum=256), 'provenance': ref('provenance')},
        ('id', 'version', 'name', 'dimensions', 'relative_scale', 'identity', 'views', 'components', 'costumes', 'capabilities', 'provenance'))
    definitions['prop_asset'] = obj({'id': ID, 'version': VERSION, 'name': TEXT, 'image': ref('image'),
        'pivot': POINT, 'anchors': {'type': 'object', 'propertyNames': ID, 'additionalProperties': POINT, 'maxProperties': 128}},
        ('id', 'version', 'name', 'image', 'pivot', 'anchors'))
    definitions['cast'] = obj({'schema_version': {'const': 1}, 'kind': {'const': 'cast'}, 'id': ID, 'version': VERSION,
        'fixture': BOOL, 'characters': arr(ref('character'), 1, 128), 'props': arr(ref('prop_asset'), maximum=256)},
        ('schema_version', 'kind', 'id', 'version', 'fixture', 'characters', 'props'))
    definitions['clip'] = obj({'schema_version': {'const': 1}, 'kind': {'const': 'clip'}, 'id': ID, 'version': VERSION,
        'character': KEY, 'viewpoint': enum(*VIEWPOINTS), 'facing': enum(*FACINGS),
        'start_pose': ID, 'end_pose': ID, 'duration_frames': integer(1), 'mode': enum('loop', 'once', 'hold'),
        'completion': enum('hold', 'loop', 'return-to-start', 'exit'),
        'root': obj({'offset': ref('track'), 'facing': ref('track')}), 'tracks': ref('tracks'),
        'contacts': arr(obj({'start_frame': integer(), 'end_frame': integer(1), 'component': ID, 'anchor': ID,
                            'kind': enum('foot', 'hand'), 'target': ID},
                           ('start_frame', 'end_frame', 'component', 'anchor', 'kind', 'target')), maximum=2048),
        'events': arr(obj({'frame': integer(), 'id': ID, 'type': enum('marker', 'contact', 'attach', 'release'),
                          'component': ID, 'anchor': ID, 'target': ID}, ('frame', 'id', 'type')), maximum=2048),
        'entry_points': arr(obj({'frame': integer(), 'pose': ID}, ('frame', 'pose')), 1, 256),
        'exit_points': arr(obj({'frame': integer(), 'pose': ID}, ('frame', 'pose')), 1, 256),
        'interpolation': obj({'max_frames': integer(0, 120), 'max_offset_per_frame': number(0, 256),
                              'max_rotation_per_frame': number(0, 180)},
                             ('max_frames', 'max_offset_per_frame', 'max_rotation_per_frame')),
        'interruptions': obj({'policy': enum('never', 'at-markers', 'any'), 'frames': arr(integer(), maximum=256, uniqueItems=True),
                              'cancel_pose': ID, 'max_transition_frames': integer(0, 120)},
                             ('policy', 'frames', 'cancel_pose', 'max_transition_frames')),
        'displacement': POINT, 'requirements': ref('requirements'),
        'full_frames': arr(obj({'start_frame': integer(), 'end_frame': integer(1), 'image': ref('image'),
                                'pivot': POINT, 'viewpoint': enum(*VIEWPOINTS)},
                               ('start_frame', 'end_frame', 'image', 'pivot', 'viewpoint')), maximum=2048)},
        ('schema_version', 'kind', 'id', 'version', 'character', 'viewpoint', 'facing', 'start_pose', 'end_pose',
         'duration_frames', 'mode', 'completion', 'root', 'tracks', 'contacts', 'events', 'entry_points',
         'exit_points', 'interpolation', 'interruptions', 'displacement', 'requirements', 'full_frames'))
    definitions['attachment'] = obj({'instance': ID, 'component': ID, 'anchor': ID}, ('instance', 'component', 'anchor'))
    definitions['placement'] = obj({'clip': KEY, 'start_frame': integer(), 'end_frame': integer(1),
        'transition': obj({'kind': enum('cut', 'interpolate'), 'frames': integer(0, 120)}, ('kind', 'frames'))},
        ('clip', 'start_frame', 'end_frame', 'transition'))
    definitions['instance'] = obj({'id': ID, 'character': KEY, 'viewpoint': enum(*VIEWPOINTS), 'facing': enum(*FACINGS),
        'costume': ID, 'position': POINT, 'z': integer(-10000, 10000), 'clips': arr(ref('placement'), maximum=2048),
        'tracks': ref('tracks'), 'requirements': ref('requirements'),
        'bindings': {'type': 'object', 'propertyNames': ID, 'additionalProperties': ID, 'maxProperties': 256}},
        ('id', 'character', 'viewpoint', 'facing', 'costume', 'position', 'z', 'clips', 'tracks', 'requirements', 'bindings'))
    definitions['ownership'] = obj({'frame': integer(), 'from': nullable(ID), 'to': nullable(ID),
        'attachment': nullable(ref('attachment')), 'event': ID}, ('frame', 'from', 'to', 'attachment', 'event'))
    definitions['prop_instance'] = obj({'id': ID, 'prop': KEY, 'position': POINT, 'z': integer(-10000, 10000),
        'owner': nullable(ID), 'attachment': nullable(ref('attachment')),
        'tracks': obj({key: ref('track') for key in ('position', 'rotation', 'visible')}),
        'ownership': arr(ref('ownership'), maximum=2048)},
        ('id', 'prop', 'position', 'z', 'owner', 'attachment', 'tracks', 'ownership'))
    definitions['interaction'] = obj({'id': ID, 'frame': integer(), 'prop': ID,
        'participants': arr(obj({'instance': ID, 'component': ID, 'anchor': ID, 'marker': ID},
                                ('instance', 'component', 'anchor', 'marker')), 1, 16),
        'position': POINT, 'tolerance': number(0, 32)}, ('id', 'frame', 'prop', 'participants', 'position', 'tolerance'))
    definitions['layer'] = obj({'id': ID, 'image': ref('image'), 'position': POINT, 'z': integer(-10000, 10000),
                               'opacity': number(0, 1), 'parallax': number(0, 2)},
                              ('id', 'image', 'position', 'z', 'opacity', 'parallax'))
    definitions['camera'] = obj({'position': ref('track'), 'zoom': ref('track'),
        'bounds': obj({'max_pan_per_frame': number(0, 256), 'min_zoom': number(.1, 10), 'max_zoom': number(.1, 10)},
                      ('max_pan_per_frame', 'min_zoom', 'max_zoom'))}, ('position', 'zoom', 'bounds'))
    definitions['audio'] = obj({'id': ID, 'file': ref('file'), 'start_frame': integer(), 'duration_frames': integer(1),
        'speaker': nullable(ID), 'text': TEXT, 'provenance': ref('provenance'), 'gain': number(0, 1),
        'mouth_cues': arr(obj({'start_frame': integer(), 'end_frame': integer(1), 'shape': ID},
                              ('start_frame', 'end_frame', 'shape')), maximum=4096)},
        ('id', 'file', 'start_frame', 'duration_frames', 'speaker', 'text', 'provenance', 'gain', 'mouth_cues'))
    definitions['shot'] = obj({'id': ID, 'duration_frames': integer(1), 'cast_scale': number(.01, 10),
        'instances': arr(ref('instance'), maximum=64), 'props': arr(ref('prop_instance'), maximum=128),
        'interactions': arr(ref('interaction'), maximum=2048), 'layers': arr(ref('layer'), maximum=64),
        'camera': ref('camera'), 'audio': arr(ref('audio'), maximum=256),
        'lighting': obj({'direction': enum('ambient', 'left', 'right', 'overhead'), 'color_treatment': TEXT},
                        ('direction', 'color_treatment')),
        'explicit_requirements': STRING_LIST, 'inferred_choices': STRING_LIST},
        ('id', 'duration_frames', 'cast_scale', 'instances', 'props', 'interactions', 'layers', 'camera', 'audio',
         'lighting', 'explicit_requirements', 'inferred_choices'))
    definitions['scene'] = obj({'schema_version': {'const': 1}, 'kind': {'const': 'scene'}, 'id': ID, 'version': VERSION,
        'shots': arr(ref('shot'), 1, MAX_SHOTS),
        'continuity': arr(obj({'from_shot': ID, 'to_shot': ID, 'characters': arr(KEY, maximum=64, uniqueItems=True),
                               'requirements': arr(enum('costume', 'scale', 'prop_ownership', 'position'), 1, 4, uniqueItems=True),
                               'note': TEXT}, ('from_shot', 'to_shot', 'characters', 'requirements', 'note')), maximum=256)},
        ('schema_version', 'kind', 'id', 'version', 'shots', 'continuity'))
    definitions['render'] = obj({'width': integer(1, 4096), 'height': integer(1, 4096),
        'fps': obj({'numerator': integer(1, 240000), 'denominator': integer(1, 10000)}, ('numerator', 'denominator')),
        'color_space': {'const': 'srgb'}, 'alpha': {'const': 'straight'},
        'resampling': enum('nearest', 'bilinear', 'bicubic'),
        'background': {'type': 'string', 'pattern': r'^#[0-9a-fA-F]{8}$'}},
        ('width', 'height', 'fps', 'color_space', 'alpha', 'resampling', 'background'))
    definitions['limits'] = obj({'max_frames': integer(1), 'max_assets': integer(1, MAX_ASSETS),
        'max_source_pixels': integer(1, MAX_SOURCE_PIXELS), 'max_work_bytes': integer(1, MAX_WORK_BYTES),
        'max_generation_calls': integer(0, 256)},
        ('max_frames', 'max_assets', 'max_source_pixels', 'max_work_bytes', 'max_generation_calls'))
    definitions['project'] = obj({'schema_version': {'const': 1}, 'kind': {'const': 'project'}, 'id': ID,
        'version': VERSION, 'fixture': BOOL, 'cast': ref('file'), 'clips': arr(ref('file'), maximum=2048),
        'scenes': arr(ref('file'), 1, 128), 'render': ref('render'), 'limits': ref('limits')},
        ('schema_version', 'kind', 'id', 'version', 'fixture', 'cast', 'clips', 'scenes', 'render', 'limits'))
    return definitions


DEFINITIONS = schema_definitions()


def schemas():
    return {kind: {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                   'title': 'Hatch production ' + kind + ' v1', '$defs': copy.deepcopy(DEFINITIONS),
                   '$ref': '#/$defs/' + kind} for kind in ('project', 'cast', 'clip', 'scene', 'shot')}


def _fail(label, message):
    raise ValueError(str(label) + ': ' + message)


def _structural(value, schema, label, depth=0):
    if depth > 64:
        _fail(label, 'JSON nesting exceeds 64 levels')
    if '$ref' in schema:
        return _structural(value, DEFINITIONS[schema['$ref'].rsplit('/', 1)[-1]], label, depth + 1)
    if 'anyOf' in schema:
        for choice in schema['anyOf']:
            try:
                _structural(value, choice, label, depth + 1)
                return
            except ValueError:
                continue
        _fail(label, 'value does not match any supported type')
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        _fail(label, 'expected ' + repr(schema['const']))
    if 'enum' in schema and not any(type(value) is type(item) and value == item for item in schema['enum']):
        _fail(label, 'expected one of ' + repr(schema['enum']))
    kind = schema.get('type')
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
               'integer': type(value) is int, 'number': type(value) in (int, float),
               'string': isinstance(value, str), 'boolean': type(value) is bool, 'null': value is None}
    if kind and not matches[kind]:
        _fail(label, 'expected ' + kind)
    if type(value) in (int, float):
        if not math.isfinite(value):
            _fail(label, 'number must be finite')
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            _fail(label, 'number exceeds supported bounds')
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', MAX_MANIFEST_BYTES):
            _fail(label, 'text length exceeds supported bounds')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            _fail(label, 'text does not match ' + schema['pattern'])
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', MAX_MANIFEST_BYTES):
            _fail(label, 'array length exceeds supported bounds')
        if schema.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            _fail(label, 'duplicate entries are not supported')
        for index, item in enumerate(value):
            _structural(item, schema.get('items', {}), f'{label}[{index}]', depth + 1)
    if isinstance(value, dict):
        if not schema.get('minProperties', 0) <= len(value) <= schema.get('maxProperties', MAX_MANIFEST_BYTES):
            _fail(label, 'object size exceeds supported bounds')
        missing = set(schema.get('required', ())) - set(value)
        if missing:
            _fail(label, 'missing required fields: ' + ', '.join(sorted(missing)))
        properties = schema.get('properties', {})
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(label, 'object keys must be strings')
            if 'propertyNames' in schema:
                _structural(key, schema['propertyNames'], label + '.<key>', depth + 1)
            if key in properties:
                child = properties[key]
            else:
                child = schema.get('additionalProperties', {})
                if child is False:
                    _fail(label, 'unsupported field: ' + key)
                if child is True:
                    child = {}
            _structural(item, child, label + '.' + key, depth + 1)


def validate_structure(value, kind):
    if kind not in DEFINITIONS:
        raise ValueError('unsupported contract kind: ' + str(kind))
    _structural(value, DEFINITIONS[kind], kind)
    return value


def asset_key(value):
    return value['id'] + '@' + value['version']


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def safe_path(root, relative, *, must_exist=True):
    if (not isinstance(relative, str) or not 0 < len(relative) <= 1024 or
            '\\' in relative or ':' in relative or relative.startswith('/') or
            any(part in ('', '.', '..') for part in relative.split('/'))):
        raise ValueError('asset path must be a portable relative path without traversal: ' + repr(relative))
    root = Path(root).resolve()
    path = root
    for part in relative.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('asset path must not traverse a symlink: ' + relative)
    if must_exist and not path.is_file():
        raise ValueError('missing dependency: ' + relative + '; restore or import the exact referenced file')
    return path


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('manifest exceeds 16 MiB: ' + str(path))
    try:
        return _decode_manifest(path.read_text(encoding='utf-8'), str(path))
    except UnicodeError as exc:
        raise ValueError('invalid UTF-8 JSON manifest: ' + str(path)) from exc


def _decode_manifest(text, label):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('non-finite JSON number: ' + value)
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
        _structural(value, {}, label)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid UTF-8 JSON manifest: ' + label) from exc


def _unique(values, label, versioned=False):
    result = {}
    for value in values:
        key = asset_key(value) if versioned else value['id']
        if key in result:
            _fail(label, 'duplicate stable identity: ' + key)
        result[key] = value
    return result


def _point_in_image(point, size, label):
    if not all(0 <= point[index] <= size[index] for index in range(2)):
        _fail(label, 'pivot or anchor must lie inside its declared image viewport')


def _validate_provenance(value, fixture, label):
    _structural(value, DEFINITIONS['provenance'], label)
    if value['fixture'] is not fixture:
        _fail(label, 'fixture provenance must match the project; fixtures cannot become production artwork')
    if value['kind'] == 'fixture' and not fixture:
        _fail(label, 'fixture sources cannot be production artwork')
    if value['kind'] == 'derived' and not all(key in value for key in ('source', 'operation')):
        _fail(label, 'derived artwork requires the exact source and explicit operation')
    if value['kind'] == 'imagegen' and 'acquisition' not in value:
        _fail(label, 'imagegen artwork requires an acquisition receipt')
    for record in (value.get('model_call'), value.get('acquisition', {}).get('model_call')):
        if record and any(record[key] is None for key in ('id', 'model', 'parameters')) and not record.get('unavailable_reason'):
            _fail(label, 'unavailable provider metadata requires an explicit unavailable_reason')


def validate_cast(value, fixture=None):
    validate_structure(value, 'cast')
    fixture = value['fixture'] if fixture is None else fixture
    if value['fixture'] is not fixture:
        _fail('cast', 'fixture flag differs from project')
    characters = _unique(value['characters'], 'cast.characters', True)
    _unique(value['props'], 'cast.props', True)
    for key, character in characters.items():
        label = 'character ' + key
        _validate_provenance(character['provenance'], fixture, label + '.provenance')
        components = _unique(character['components'], label + '.components')
        costumes = _unique(character['costumes'], label + '.costumes')
        if sum(component['parent'] is None for component in components.values()) != 1:
            _fail(label, 'component hierarchy requires exactly one root')
        # Validate every parent before walking ancestors so a missing grandparent is actionable.
        for cid, component in components.items():
            if component['parent'] is None:
                if component['parent_anchor'] is not None:
                    _fail(label + '.' + cid, 'root component cannot have a parent anchor')
            elif (component['parent'] not in components or
                  component['parent_anchor'] not in components[component['parent']]['anchors']):
                _fail(label + '.' + cid, 'missing parent component or parent anchor')
        for cid, component in components.items():
            seen, cursor = set(), cid
            while cursor is not None:
                if cursor in seen:
                    _fail(label + '.' + cid, 'component hierarchy contains a cycle')
                seen.add(cursor)
                cursor = components[cursor]['parent']
            if component['default_drawing'] not in component['drawings']:
                _fail(label + '.' + cid, 'missing default drawing')
            sizes = {tuple(drawing['image']['size']) for drawing in component['drawings'].values()}
            if len(sizes) != 1:
                _fail(label + '.' + cid, 'replacement drawings must use the same component viewport')
            size = next(iter(sizes))
            _point_in_image(component['pivot'], size, label + '.' + cid + '.pivot')
            for anchor, point in component['anchors'].items():
                _point_in_image(point, size, label + '.' + cid + '.anchors.' + anchor)
            if component.get('mask'):
                if tuple(component['mask']['size']) != size:
                    _fail(label + '.' + cid, 'mask must match the component viewport')
                _validate_provenance(component['mask']['provenance'], fixture, label + '.' + cid + '.mask')
            for name, drawing in component['drawings'].items():
                if drawing['viewpoint'] not in character['views'] or drawing['costume'] not in costumes:
                    _fail(label + '.' + cid + '.' + name, 'drawing viewpoint or costume is undeclared')
                _validate_provenance(drawing['image']['provenance'], fixture, label + '.' + cid + '.' + name)
        for costume in costumes.values():
            for cid, name in costume['drawings'].items():
                if cid not in components or name not in components[cid]['drawings']:
                    _fail(label, 'costume references a missing component drawing')
                if components[cid]['drawings'][name]['costume'] != costume['id']:
                    _fail(label, 'costume map references a drawing for another costume')
        for view, review in character.get('view_reviews', {}).items():
            if view not in character['views']:
                _fail(label, 'review names an undeclared viewpoint')
            if review['status'] == 'approved' and (not review['reviewer'] or not review['evidence'] or review['kind'] == 'unreviewed'):
                _fail(label, 'approved viewpoint requires a reviewer and exact evidence')
            if not fixture and review['kind'] == 'synthetic':
                _fail(label, 'synthetic review cannot approve production artwork')
        for restriction in character.get('restrictions', []):
            if not restriction['when'] or any(not values for values in restriction['when'].values()):
                _fail(label, 'restriction requires nonempty independent conditions')
    for prop in value['props']:
        _point_in_image(prop['pivot'], prop['image']['size'], 'prop ' + asset_key(prop))
        for point in prop['anchors'].values():
            _point_in_image(point, prop['image']['size'], 'prop ' + asset_key(prop))
        _validate_provenance(prop['image']['provenance'], fixture, 'prop ' + asset_key(prop))
    return value


def _interval(start, end, duration, label):
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration:
        _fail(label, 'expected integer frames 0 <= start_frame < end_frame <= duration_frames')


def validate_track(track, duration, label='track', value_kind='number', max_delta=None):
    _structural(track, TRACK, label)
    previous = None
    value_schema = {'number': number(), 'point': POINT, 'drawing': ID, 'bool': BOOL, 'facing': enum(*FACINGS)}[value_kind]
    for key in track['keys']:
        frame, value = key['frame'], key['value']
        if frame > duration or (previous and frame <= previous['frame']):
            _fail(label, 'track frames must be strictly increasing and within duration')
        _structural(value, value_schema, label + '.value')
        if value_kind in ('drawing', 'bool', 'facing') and track['interpolation'] != 'step':
            _fail(label, 'discrete drawing, visibility and facing tracks require step interpolation')
        if previous and max_delta is not None:
            a = previous['value']
            distance = math.dist(a, value) if value_kind == 'point' else abs(value - a)
            frames = frame - previous['frame'] if track['interpolation'] == 'linear' else 1
            if distance / frames > max_delta + 1e-9:
                _fail(label, 'track exceeds the per-frame interpolation limit')
        previous = key
    if track['keys'][0]['frame'] != 0:
        _fail(label, 'track must declare its initial value at frame 0')
    return track


def _validate_tracks(tracks, character, duration, label, limits=None):
    components = {component['id']: component for component in character['components']}
    written = set()
    for channel in CHANNELS:
        for cid, properties in tracks.get(channel, {}).items():
            if cid not in components:
                _fail(label, 'track references a missing component: ' + cid)
            for prop, track in properties.items():
                if (cid, prop) in written:
                    _fail(label, 'independent channels cannot write the same component property: ' + cid + '.' + prop)
                written.add((cid, prop))
                kind = {'offset': 'point', 'rotation': 'number', 'drawing': 'drawing', 'visible': 'bool'}[prop]
                limit = limits.get('max_' + prop + '_per_frame') if limits else None
                validate_track(track, duration, label + '.' + channel + '.' + cid + '.' + prop, kind, limit)
                if prop == 'drawing' and any(key['value'] not in components[cid]['drawings'] for key in track['keys']):
                    _fail(label, 'track references a missing component drawing: ' + cid)
    for prop, track in tracks.get('root', {}).items():
        validate_track(track, duration, label + '.root.' + prop,
                       {'offset': 'point', 'facing': 'facing', 'visible': 'bool'}[prop])
        if prop == 'facing' and any(key['value'] not in character['identity']['allowed_facings'] for key in track['keys']):
            _fail(label, 'root facing is forbidden by character identity')


def _validate_component_anchor(record, components, label):
    if record['component'] not in components or record['anchor'] not in components[record['component']]['anchors']:
        _fail(label, 'missing component or contact anchor')


def validate_clip(value, characters):
    validate_structure(value, 'clip')
    label = 'clip ' + asset_key(value)
    if value['character'] not in characters:
        _fail(label, 'missing character version: ' + value['character'])
    character = characters[value['character']]
    components = {component['id']: component for component in character['components']}
    n = value['duration_frames']
    if value['viewpoint'] not in character['views'] or value['facing'] not in character['identity']['allowed_facings']:
        _fail(label, 'unsupported viewpoint or identity facing; import and review that view explicitly')
    if 'root' in value['tracks']:
        _fail(label, 'clip root tracks belong in clip.root')
    _validate_tracks(value['tracks'], character, n, label + '.tracks', value['interpolation'])
    from production_performance import evaluate_track
    if 'offset' in value['root']:
        validate_track(value['root']['offset'], n, label + '.root.offset', 'point', value['interpolation']['max_offset_per_frame'])
    origin = evaluate_track(value['root'].get('offset'), 0, [0, 0])
    if origin != [0, 0]:
        _fail(label, 'relative clip root must begin at offset [0, 0]')
    displacement = evaluate_track(value['root'].get('offset'), n, [0, 0])
    if any(abs(displacement[i] - value['displacement'][i]) > 1e-9 for i in range(2)):
        _fail(label, 'declared displacement must equal the root endpoint at duration_frames')
    if 'facing' in value['root']:
        validate_track(value['root']['facing'], n, label + '.root.facing', 'facing')
        if any(key['value'] not in character['identity']['allowed_facings'] or key['value'] != value['facing']
               for key in value['root']['facing']['keys']):
            _fail(label, 'root facing requires a compatible authored viewpoint clip; implicit mirroring is unsupported')
    if value['mode'] == 'hold':
        for root_or_channel in [value['root'], *[properties for channel in CHANNELS
            for properties in value['tracks'].get(channel, {}).values()]]:
            for track in root_or_channel.values():
                if any(key['value'] != track['keys'][0]['value'] for key in track['keys']):
                    _fail(label, 'hold clip cannot contain changing tracks')
        if value['displacement'] != [0, 0] or value['start_pose'] != value['end_pose']:
            _fail(label, 'hold clip requires a fixed pose and zero displacement')
        if any(event['frame'] != 0 for event in value['events']):
            _fail(label, 'hold events are entry events at frame 0 only')
    if value['mode'] == 'loop' and (value['start_pose'] != value['end_pose'] or value['completion'] != 'loop'):
        _fail(label, 'loop requires matching start/end poses and loop completion')
    if value['mode'] != 'loop' and value['completion'] == 'loop':
        _fail(label, 'loop completion requires loop mode')
    for name, endpoint, pose in (('entry_points', 0, value['start_pose']), ('exit_points', n - 1, value['end_pose'])):
        points = value[name]
        if any(point['frame'] >= n for point in points) or len({point['frame'] for point in points}) != len(points):
            _fail(label, name + ' must contain distinct local frames before the duration endpoint')
        if not any(point['frame'] == endpoint and point['pose'] == pose for point in points):
            _fail(label, name + ' must include the declared endpoint pose')
    rules = value['interruptions']
    if any(frame >= n for frame in rules['frames']) or (rules['policy'] == 'at-markers' and not rules['frames']):
        _fail(label, 'interruption markers must be declared local frames before clip completion')
    if rules['max_transition_frames'] > value['interpolation']['max_frames']:
        _fail(label, 'cancellation exceeds the clip interpolation bound')
    for contact in value['contacts']:
        _interval(contact['start_frame'], contact['end_frame'], n, label + '.contact')
        _validate_component_anchor(contact, components, label + '.contact')
    event_keys = set()
    for event in value['events']:
        if event['frame'] >= n or (event['id'], event['frame']) in event_keys:
            _fail(label, 'events require distinct id/frame pairs before completion')
        event_keys.add((event['id'], event['frame']))
        if ('component' in event) != ('anchor' in event):
            _fail(label, 'event component and anchor must be supplied together')
        if 'component' in event:
            _validate_component_anchor(event, components, label + '.event')
    end = 0
    for override in value['full_frames']:
        _interval(override['start_frame'], override['end_frame'], n, label + '.full_frame')
        if override['start_frame'] < end:
            _fail(label, 'full-frame overrides must be ordered and nonoverlapping')
        end = override['end_frame']
        if override['viewpoint'] != value['viewpoint']:
            _fail(label, 'full-frame override must preserve the clip viewpoint')
        if override['image']['size'] != [character['dimensions']['width'], character['dimensions']['height']]:
            _fail(label, 'full-frame override must use the canonical character viewport')
        _point_in_image(override['pivot'], override['image']['size'], label + '.full_frame.pivot')
        _validate_provenance(override['image']['provenance'], character['provenance']['fixture'], label + '.full_frame')
    return value


def natural_exit_pose(clip, elapsed):
    if clip['mode'] == 'hold':
        return clip['start_pose']
    if clip['mode'] == 'once' and elapsed >= clip['duration_frames']:
        return clip['start_pose'] if clip['completion'] == 'return-to-start' else clip['end_pose']
    if elapsed <= 0:
        return None
    local = (elapsed - 1) % clip['duration_frames'] if clip['mode'] == 'loop' else elapsed - 1
    return next((point['pose'] for point in clip['exit_points'] if point['frame'] == local), None)


def _attachment(value, owner, instances, characters, label):
    if owner is None:
        if value is not None:
            _fail(label, 'unowned prop cannot retain a character attachment')
        return
    if owner not in instances or value is None or value['instance'] != owner:
        _fail(label, 'prop owner and attachment must identify the same existing instance')
    components = {component['id']: component for component in characters[instances[owner]['character']]['components']}
    _validate_component_anchor(value, components, label)


def _drawing_compatibility(tracks, character, instance, label):
    components = {component['id']: component for component in character['components']}
    for channel in CHANNELS:
        for cid, properties in tracks.get(channel, {}).items():
            for key in properties.get('drawing', {}).get('keys', []):
                drawing = components[cid]['drawings'][key['value']]
                if drawing['viewpoint'] != instance['viewpoint'] or drawing['costume'] != instance['costume']:
                    _fail(label, 'replacement drawing does not match the selected viewpoint and costume')


def validate_shot(value, characters, clips, props, fixture=False):
    validate_structure(value, 'shot')
    label, duration = 'shot ' + value['id'], value['duration_frames']
    instances = _unique(value['instances'], label + '.instances')
    prop_instances = _unique(value['props'], label + '.props')
    _unique([*value['instances'], *value['props'], *value['layers']], label + '.layer identities')
    _unique(value['audio'], label + '.audio')
    interactions = _unique(value['interactions'], label + '.interactions')
    from production_performance import evaluate_clip, interruption_plan, selected_drawing
    for iid, instance in instances.items():
        ilabel = label + '.' + iid
        if instance['character'] not in characters:
            _fail(ilabel, 'missing character version: ' + instance['character'])
        character = characters[instance['character']]
        if instance['viewpoint'] not in character['views'] or instance['facing'] not in character['identity']['allowed_facings']:
            _fail(ilabel, 'unsupported viewpoint or facing; author and review an explicit compatible view')
        if instance['costume'] not in {costume['id'] for costume in character['costumes']}:
            _fail(ilabel, 'missing costume variant')
        for component in character['components']:
            selected_drawing(component, instance['viewpoint'], instance['costume'], character)
        _validate_tracks(instance['tracks'], character, duration, ilabel + '.tracks')
        _drawing_compatibility(instance['tracks'], character, instance, ilabel)
        if any(key['value'] != instance['facing'] for key in instance['tracks'].get('root', {}).get('facing', {}).get('keys', [])):
            _fail(ilabel, 'shot root facing must match the selected authored viewpoint clip')
        if any(target not in instances and target not in prop_instances for target in instance['bindings'].values()):
            _fail(ilabel, 'binding references a missing prop or character instance')
        previous, last_end = None, 0
        for placement in instance['clips']:
            _interval(placement['start_frame'], placement['end_frame'], duration, ilabel + '.placement')
            if placement['start_frame'] < last_end:
                _fail(ilabel, 'clip placements must be ordered and nonoverlapping')
            last_end = placement['end_frame']
            if placement['clip'] not in clips:
                _fail(ilabel, 'missing clip version: ' + placement['clip'])
            clip = clips[placement['clip']]
            if any(clip[key] != instance[key] for key in ('character', 'viewpoint', 'facing')):
                _fail(ilabel, 'clip character, viewpoint and facing must match its instance')
            _drawing_compatibility(clip['tracks'], character, instance, ilabel)
            for record in [*clip['contacts'], *clip['events']]:
                if record.get('target') not in (None, 'ground') and record['target'] not in instance['bindings']:
                    _fail(ilabel, 'clip contact/event target requires an explicit shot binding: ' + record['target'])
            transition = placement['transition']
            blend = transition['frames']
            if transition['kind'] == 'cut' and blend != 0:
                _fail(ilabel, 'cut transition must have zero frames')
            if transition['kind'] == 'interpolate' and not 1 <= blend <= clip['interpolation']['max_frames']:
                _fail(ilabel, 'interpolation must have a positive bounded number of frames')
            if blend > placement['end_frame'] - placement['start_frame']:
                _fail(ilabel, 'transition cannot exceed the incoming placement duration')
            if previous:
                prior = clips[previous['clip']]
                elapsed = previous['end_frame'] - previous['start_frame']
                exit_pose = natural_exit_pose(prior, elapsed)
                if exit_pose is None:
                    interruption_plan(prior, elapsed, clip['start_pose'], blend)
                elif exit_pose != clip['start_pose'] and blend == 0:
                    _fail(ilabel, 'incompatible pose cut requires an explicit bounded transition')
                if blend > prior['interpolation']['max_frames']:
                    _fail(ilabel, 'transition exceeds outgoing clip interpolation bound')
            previous = placement
    for pid, prop in prop_instances.items():
        if prop['prop'] not in props:
            _fail(label + '.' + pid, 'missing prop version')
        _attachment(prop['attachment'], prop['owner'], instances, characters, label + '.' + pid)
        for name, track in prop['tracks'].items():
            validate_track(track, duration, label + '.' + pid + '.' + name,
                           {'position': 'point', 'rotation': 'number', 'visible': 'bool'}[name])
        owner, previous_frame = prop['owner'], -1
        for change in prop['ownership']:
            if not previous_frame < change['frame'] < duration:
                _fail(label, 'ownership events must use distinct ordered frames within the shot')
            previous_frame = change['frame']
            if change['from'] != owner or change['to'] == owner:
                _fail(label, 'ownership history must match the current owner and change ownership')
            _attachment(change['attachment'], change['to'], instances, characters, label + '.' + pid)
            interaction = interactions.get(change['event'])
            if interaction is None or interaction['frame'] != change['frame'] or interaction['prop'] != pid:
                _fail(label, 'ownership change requires an exact synchronized shared interaction')
            participants = {item['instance'] for item in interaction['participants']}
            if any(actor is not None and actor not in participants for actor in (owner, change['to'])):
                _fail(label, 'ownership handoff must include both the releasing and receiving participants')
            owner = change['to']
    for interaction in interactions.values():
        if interaction['frame'] >= duration or interaction['prop'] not in prop_instances:
            _fail(label, 'interaction requires an existing prop and frame within the shot')
        seen = set()
        for participant in interaction['participants']:
            iid = participant['instance']
            if iid not in instances:
                _fail(label, 'interaction references a missing character instance')
            character = characters[instances[iid]['character']]
            _validate_component_anchor(participant, {c['id']: c for c in character['components']}, label + '.interaction')
            signature = (iid, participant['component'], participant['anchor'])
            if signature in seen:
                _fail(label, 'duplicate interaction participant anchor')
            seen.add(signature)
            matches = []
            for placement in instances[iid]['clips']:
                if placement['start_frame'] <= interaction['frame'] < placement['end_frame']:
                    state = evaluate_clip(clips[placement['clip']], interaction['frame'] - placement['start_frame'])
                    matches += [event for event in state['events'] if event['id'] == participant['marker']]
            if len(matches) != 1:
                _fail(label, 'interaction participant requires exactly one clip marker at the shared frame')
            event = matches[0]
            if 'component' in event and (event['component'] != participant['component'] or event['anchor'] != participant['anchor']):
                _fail(label, 'interaction marker and participant contact anchor disagree')
            if 'target' in event and instances[iid]['bindings'].get(event['target']) != interaction['prop']:
                _fail(label, 'interaction marker target is not bound to the same prop')
    camera = value['camera']
    if camera['bounds']['min_zoom'] > camera['bounds']['max_zoom']:
        _fail(label, 'camera zoom bounds are reversed')
    validate_track(camera['position'], duration, label + '.camera.position', 'point', camera['bounds']['max_pan_per_frame'])
    validate_track(camera['zoom'], duration, label + '.camera.zoom')
    if any(not camera['bounds']['min_zoom'] <= key['value'] <= camera['bounds']['max_zoom'] for key in camera['zoom']['keys']):
        _fail(label, 'camera zoom exceeds the declared bounds')
    cues_by_speaker = {}
    for audio in value['audio']:
        _interval(audio['start_frame'], audio['start_frame'] + audio['duration_frames'], duration, label + '.audio')
        _validate_provenance(audio['provenance'], fixture, label + '.audio.provenance')
        speaker = audio['speaker']
        if speaker is not None and speaker not in instances:
            _fail(label, 'audio speaker must identify an existing instance')
        last_end = audio['start_frame']
        for cue in audio['mouth_cues']:
            if speaker is None:
                _fail(label, 'mouth cue requires an explicit speaker')
            _interval(cue['start_frame'], cue['end_frame'], audio['start_frame'] + audio['duration_frames'], label + '.mouth_cue')
            if cue['start_frame'] < last_end:
                _fail(label, 'mouth cues must be ordered, nonoverlapping and within their audio interval')
            last_end = cue['end_frame']
            instance = instances[speaker]
            character = characters[instance['character']]
            if not any(component['role'] == 'mouth' and cue['shape'] in component['drawings'] and
                       component['drawings'][cue['shape']]['viewpoint'] == instance['viewpoint'] and
                       component['drawings'][cue['shape']]['costume'] == instance['costume']
                       for component in character['components']):
                _fail(label, 'mouth cue requires a compatible mouth drawing for the speaker')
            cues_by_speaker.setdefault(speaker, []).append((cue['start_frame'], cue['end_frame']))
    for speaker, cues in cues_by_speaker.items():
        ordered = sorted(cues)
        if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
            _fail(label, 'mouth cues overlap for speaker ' + speaker)
    return value


def validate_scene(value, characters, clips, props, fixture=False):
    validate_structure(value, 'scene')
    _unique(value['shots'], 'scene.shots')
    for shot in value['shots']:
        validate_shot(shot, characters, clips, props, fixture)
    for continuity in value['continuity']:
        if continuity['from_shot'] == continuity['to_shot'] or any(key not in characters for key in continuity['characters']):
            _fail('scene ' + value['id'], 'continuity requires distinct shots and exact known character versions')
    return value


def _walk_refs(value, label='source'):
    if isinstance(value, dict):
        if 'path' in value and 'sha256' in value:
            yield value, label
        if 'receipt_path' in value and 'receipt_sha256' in value:
            yield {'path': value['receipt_path'], 'sha256': value['receipt_sha256']}, label + '.receipt'
        for key, item in value.items():
            yield from _walk_refs(item, label + '.' + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_refs(item, f'{label}[{index}]')


def load_project(path, verify_hashes=True, *, allow_missing_assets=False):
    """Load portable manifests; diagnostic mode records stale/missing leaf assets.

    Structural errors, unsafe paths and conflicting hash declarations always fail.
    Referenced cast, clip and scene manifests are required even in diagnostic mode.
    """
    if verify_hashes and allow_missing_assets:
        raise ValueError('allow_missing_assets requires diagnostic verify_hashes=False')
    path = Path(path).resolve()
    if path.is_dir():
        path /= 'project.json'
    return _load_project(path.parent, path.name, verify_hashes, allow_missing_assets)


def load_project_at(root_fd, *, expected_project_sha256=None):
    """Strictly verify a native 2D closure in a held Scene3D snapshot directory.

    The staging caller already admits every archived filename under the Scene3D
    profile. The ordinary legacy loader keeps its own path spelling policy.
    """
    with owned_directory_fd(root_fd) as descriptor:
        return _load_project(None, 'project.json', True, False, expected_project_sha256=expected_project_sha256,
                             open_file=lambda name: open_payload_at(descriptor, name, profile='scene3d'))


def _load_project(root, name, verify_hashes, allow_missing_assets, *, open_file=None, expected_project_sha256=None):
    def read_manifest(target, expected_sha256=None):
        if open_file is None:
            return read_json(target)
        with open_file(target) as (stream, info):
            if info.st_size > MAX_MANIFEST_BYTES:
                raise ValueError('manifest exceeds 16 MiB: ' + target)
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(payload) != info.st_size:
                raise ValueError('manifest size changed while reading: ' + target)
        if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
            _fail(target, 'manifest hash differs from the verified snapshot bytes')
        try:
            return _decode_manifest(payload.decode('utf-8'), target)
        except UnicodeError as exc:
            raise ValueError('invalid UTF-8 JSON manifest: ' + target) from exc

    def bounded_hash(stream, size, label):
        if size > MAX_SOURCE_BYTES:
            _fail(label, 'source exceeds 64 MiB encoded-file bound')
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                _fail(label, 'source size changed while reading')
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            _fail(label, 'source size changed while reading')
        return digest.hexdigest()

    def inspect_bytes(target, label, relative):
        if open_file is None:
            size = target.stat().st_size
            if size > MAX_SOURCE_BYTES:
                _fail(label, 'source exceeds 64 MiB encoded-file bound: ' + relative)
            return size, file_sha(target)
        with open_file(target) as (stream, info):
            return info.st_size, bounded_hash(stream, info.st_size, relative)

    def image_input(target, relative):
        if open_file is None:
            return nullcontext(target)
        return checked_image_stream(target, relative)

    @contextmanager
    def checked_image_stream(target, relative):
        with open_file(target) as (stream, info):
            record = inventory[relative]
            if info.st_size != record['bytes'] or bounded_hash(stream, info.st_size, relative) != record['sha256']:
                _fail(relative, 'encoded image bytes changed before validation')
            stream.seek(0)
            yield stream

    project = validate_structure(read_manifest(root / name if open_file is None else name, expected_project_sha256), 'project')
    inventory, stale, missing, expected_hashes = {}, [], [], {}
    limits = project['limits']

    def inspect(record, label, manifest=False):
        relative = record['path']
        target = safe_path(root, relative, must_exist=False) if open_file is None else relative
        expected = record['sha256']
        if relative in expected_hashes and expected_hashes[relative] != expected:
            _fail(label, 'conflicting hashes for one portable path: ' + relative)
        expected_hashes[relative] = expected
        if open_file is None and not target.is_file():
            if not allow_missing_assets or manifest:
                _fail(label, 'missing dependency: ' + relative + '; restore or import this exact asset')
            if relative not in inventory:
                detail = {'path': relative, 'expected': expected, 'actual': None,
                          'reason': label + ': missing dependency; restore or import this exact asset'}
                missing.append(detail)
                stale.append(detail)
                inventory[relative] = {'path': relative, 'sha256': None, 'expected_sha256': expected, 'bytes': 0, 'missing': True}
            if len(inventory) > limits['max_assets']:
                _fail('project', 'asset count exceeds configured resource bound')
            return target
        if relative not in inventory:
            size, actual = inspect_bytes(target, label, relative)
            if actual != expected:
                if verify_hashes:
                    _fail(label, 'hash mismatch: ' + relative + '; recover the exact file or explicitly revise its version/reference')
                stale.append({'path': relative, 'expected': expected, 'actual': actual, 'reason': label + ': hash mismatch'})
            inventory[relative] = {'path': relative, 'sha256': actual, 'expected_sha256': expected, 'bytes': size}
            if len(inventory) > limits['max_assets'] or sum(item['bytes'] for item in inventory.values()) > MAX_PROJECT_BYTES:
                _fail('project', 'source inventory exceeds configured assets or 1 GiB total source resource bound')
        if 'size' in record:
            try:
                with image_input(target, relative) as source, Image.open(source) as image:
                    if image.format not in ('PNG', 'WEBP', 'JPEG') or getattr(image, 'n_frames', 1) != 1:
                        _fail(label, 'only single-frame PNG, WebP or JPEG sources are supported')
                    if image.width * image.height > limits['max_source_pixels']:
                        _fail(label, 'image exceeds configured source pixel resource bound')
                    if list(image.size) != record['size']:
                        _fail(label, 'image header dimensions differ from its declared viewport')
                    requires_alpha = '.components[' in label or '.full_frames[' in label or (label.startswith('cast.props[') and label.endswith('.image'))
                    if requires_alpha and not (image.mode in ('RGBA', 'LA') or image.mode == 'P' and 'transparency' in image.info):
                        _fail(label, 'character and prop drawings require straight alpha sources')
                    image.verify()
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError(label + ': invalid or excessive encoded image') from exc
        return target

    def load_reference(record, label):
        return read_manifest(inspect(record, label, True), record['sha256'] if open_file is not None else None)

    cast = validate_cast(load_reference(project['cast'], 'project.cast'), project['fixture'])
    characters = _unique(cast['characters'], 'cast.characters', True)
    props = _unique(cast['props'], 'cast.props', True)
    clips, clip_paths = {}, {}
    for record in project['clips']:
        clip = validate_clip(load_reference(record, 'project.clips'), characters)
        key = asset_key(clip)
        if key in clips:
            _fail('project', 'duplicate clip version: ' + key)
        clips[key], clip_paths[key] = clip, record['path']
    scenes, scene_paths, shots = [], {}, {}
    for record in project['scenes']:
        scene = validate_scene(load_reference(record, 'project.scenes'), characters, clips, props, project['fixture'])
        if scene['id'] in scene_paths:
            _fail('project', 'scene IDs must be unique across the project')
        scene_paths[scene['id']] = record['path']
        scenes.append(scene)
        for shot in scene['shots']:
            if shot['id'] in shots:
                _fail('project', 'shot IDs must be unique across all scenes')
            shots[shot['id']] = shot
    for value, label in [(cast, 'cast'), *[(clip, 'clip ' + key) for key, clip in clips.items()],
                         *[(scene, 'scene ' + scene['id']) for scene in scenes]]:
        for record, source_label in _walk_refs(value, label):
            inspect(record, source_label)
            if 'provenance' in record:
                _validate_provenance(record['provenance'], project['fixture'], source_label + '.provenance')
    total_frames = sum(shot['duration_frames'] for shot in shots.values())
    if total_frames > limits['max_frames'] or len(shots) > MAX_SHOTS:
        _fail('project', 'total frames or shot count exceeds configured resource bounds')
    settings = project['render']
    pixels = settings['width'] * settings['height']
    if pixels > MAX_SOURCE_PIXELS:
        _fail('project', 'render dimensions exceed the supported 2,000,000-pixel raster bound')
    fps = settings['fps']['numerator'] / settings['fps']['denominator']
    if not 1 <= fps <= 60:
        _fail('project', 'render frame rate must be between 1 and 60 fps')
    work_bytes = pixels * 32 + min(limits['max_source_pixels'] * 4 * 8, 128 * 1024 * 1024) + 32 * 1024 * 1024
    if work_bytes > limits['max_work_bytes']:
        _fail('project', 'estimated raster working set exceeds max_work_bytes')
    for scene in scenes:
        for link in scene['continuity']:
            if link['from_shot'] not in shots or link['to_shot'] not in shots:
                _fail('project', 'continuity references a missing shot')
    bundle = {'root': root, 'project': project, 'cast': cast, 'characters': characters, 'props': props,
        'clips': clips, 'scenes': scenes, 'shots': shots,
              'paths': {'project': name, 'cast': project['cast']['path'], 'clips': clip_paths, 'scenes': scene_paths},
        'inventory': inventory, 'stale': stale, 'missing': missing,
        'resources': {'total_frames': total_frames, 'assets': len(inventory),
                      'source_bytes': sum(item['bytes'] for item in inventory.values()), 'estimated_work_bytes': work_bytes},
        'contract_version': CONTRACT_VERSION}
    from production_performance import validate_continuity, validate_transition_limits
    validate_continuity(bundle)
    validate_transition_limits(bundle)
    return bundle


validate_project = load_project


def planned_requirements(bundle, shot_id):
    from production_performance import planned_requirements as plan
    return plan(bundle, shot_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', nargs='?')
    parser.add_argument('--write-schemas', action='store_true')
    args = parser.parse_args(argv)
    if args.write_schemas:
        destination = Path(__file__).resolve().parents[1] / 'profiles'
        destination.mkdir(parents=True, exist_ok=True)
        for kind, schema in schemas().items():
            (destination / ('production-' + kind + '.schema.json')).write_text(json.dumps(schema, indent=2) + '\n', encoding='utf-8')
    if args.project:
        bundle = load_project(args.project)
        print(json.dumps({'valid': True, 'project': bundle['project']['id'], 'resources': bundle['resources']}, indent=2))
    if not args.write_schemas and not args.project:
        parser.error('provide a project path or --write-schemas')


if __name__ == '__main__':
    main()
