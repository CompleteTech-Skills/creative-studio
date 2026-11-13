"""Additive camera3d v1 contracts; authoring admission is distinct from execution.

Definitions generate JSON Schema. No NumPy, Blender or browser import is needed
for discovery or serialization. Camera-local -Z views forward, +Y points up.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
import os
import stat
from pathlib import Path, PurePosixPath
import re
import unicodedata

from rig_errors import decode_json, finite_json

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 8 * 1024 * 1024
KINDS = ('camera', 'optics', 'transform', 'animation', 'constraint', 'target', 'path',
         'rig', 'preset', 'binding', 'shot', 'transition', 'storyboard', 'evaluated', 'render', 'registry')
ID = {'type': 'string', 'pattern': r'^[a-z][a-z0-9_-]{0,63}(?![\s\S])'}
VERSION = {'type': 'string', 'pattern': r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?![\s\S])'}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
PATH = {'type': 'string', 'minLength': 1, 'maxLength': 1024,
        'pattern': r'^(?!/)(?!.*(?:^|/) )(?!.*[\\:\x00-\x1f*?"<>|])(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|(?![\s\S])))(?!.*[. ](?:/|(?![\s\S])))(?!(?:[^/]+/)*(?:[cC][oO][nN]|[pP][rR][nN]|[aA][uU][xX]|[nN][uU][lL]|[cC][oO][nN][iI][nN]\$|[cC][oO][nN][oO][uU][tT]\$|[cC][oO][mM][1-9¹²³]|[lL][pP][tT][1-9¹²³])(?:[.]|/|(?![\s\S])))[^/]+(?:/[^/]+)*(?![\s\S])'}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}(?![\s\S])'}
NUM = {'type': 'number', 'minimum': -1e9, 'maximum': 1e9}
POS = {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1e9}


def obj(properties, required=None):
    return dict(type='object', additionalProperties=False, properties=properties,
                required=list(properties if required is None else required))


def arr(items, minimum=0, maximum=4096):
    return dict(type='array', items=items, minItems=minimum, maxItems=maximum)


def nullable(value):
    return {'anyOf': [value, {'type': 'null'}]}


def ref(name):
    return {'$ref': '#/$defs/' + name}


def enum(*values):
    return {'enum': list(values)}


def integer(low=0, high=10_000_000):
    return dict(type='integer', minimum=low, maximum=high)


def base(kind, fields):
    return obj(dict(schema_version={'type': 'integer', 'const': 1},
                    kind={'const': 'camera3d-' + kind}, id=ID, version=VERSION, **fields))


@lru_cache(maxsize=1)
def definitions():
    vec = arr(NUM, 3, 3)
    quat = arr({'type': 'number', 'minimum': -1, 'maximum': 1}, 4, 4)
    matrix = arr(arr(NUM, 4, 4), 4, 4)
    pointer_id = {'type': 'string', 'pattern': r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}(?![\s\S])'}
    pointer = obj(dict(id=pointer_id, version=VERSION, path=PATH,
                       sha256=SHA, kind={'type': 'string', 'pattern': r'^[A-Za-z][A-Za-z0-9_-]{0,95}(?![\s\S])'}))
    def typed(kind):
        return {'allOf': [ref('reference'), {'properties': {'kind': {'const': kind}}}]}
    rate = obj(dict(numerator=integer(1, 1_000_000), denominator=integer(1, 1_000_000)))
    time = obj(dict(frame=integer(), subframe_ticks=integer(0, 999999)))
    trs = obj(dict(translation=vec, rotation=quat, scale=arr({'const': 1}, 3, 3)))
    provenance = obj(dict(origin=enum('authored', 'import', 'derived', 'procedural-fixture'),
                          fixture={'type': 'boolean'}, attribution=TEXT, license=TEXT,
                          inputs=arr(ref('reference'), maximum=256)))
    meta = obj(dict(name=TEXT, description=TEXT, provenance=ref('provenance')))
    scene_ref = typed('scene3d-scene')
    camera_ref = typed('camera3d-camera')
    target_ref = typed('camera3d-target')
    constraint_ref = typed('camera3d-constraint')
    render_ref = typed('camera3d-render')
    optics_ref = typed('camera3d-optics')
    dof = obj(dict(enabled={'type': 'boolean'}, focus_distance_m=POS, f_stop=POS,
                   aperture_blades=integer(0, 32), aperture_rotation_rad=NUM, anamorphic_ratio=POS))
    perspective = obj(dict(type={'const': 'perspective'}, focal_length_mm=POS,
                           sensor_width_mm=POS, sensor_height_mm=POS,
                           sensor_fit=enum('horizontal', 'vertical', 'auto'),
                           shift_x=NUM, shift_y=NUM, near_clip_m=POS, far_clip_m=POS))
    ortho = obj(dict(type={'const': 'orthographic'}, orthographic_height_m=POS,
                     shift_x=NUM, shift_y=NUM, near_clip_m=POS, far_clip_m=POS))
    projection = {'oneOf': [perspective, ortho]}
    sample = obj(dict(time=ref('time'), translation=vec, rotation=quat))
    d = dict(reference=pointer, rate=rate, time=time, trs=trs, matrix=matrix,
             provenance=provenance, metadata=meta, projection=projection, dof=dof)
    d['parent'] = obj(dict(scene=scene_ref, node=ID))
    d['transform'] = base('transform', dict(local=ref('trs'), parent=nullable(ref('parent'))))
    d['optics'] = base('optics', dict(projection=ref('projection'), depth_of_field=ref('dof')))
    d['camera'] = base('camera', dict(transform=ref('trs'), parent=nullable(ref('parent')),
                                     optics=optics_ref, constraints=arr(constraint_ref, maximum=128),
                                     animation=nullable(typed('camera3d-animation')), metadata=ref('metadata')))
    d['target'] = base('target', dict(scene=scene_ref, node=ID, attachment=nullable(pointer_id),
                                     offset_m=vec, space=enum('world', 'node', 'bone', 'socket')))
    d['constraint'] = base('constraint', dict(type=enum('look-at', 'frame', 'focus', 'collision', 'horizon'),
        targets=arr(target_ref, maximum=128), priority=integer(0, 1000), enabled={'type': 'boolean'},
        weight={'type': 'number', 'minimum': 0, 'maximum': 1}, up=vec,
        safe_rect=arr({'type': 'number', 'minimum': 0, 'maximum': 1}, 4, 4),
        distance_range_m=arr(POS, 2, 2), collision_radius_m=POS,
        failure=enum('reject', 'warn'), metadata=ref('metadata')))
    d['path'] = base('path', dict(space=enum('world', 'parent'),
        interpolation=enum('linear', 'bezier', 'catmull-rom'), closed={'type': 'boolean'},
        control_points_m=arr(vec, 2, 4096), duration_frames=integer(1), fps=ref('rate'),
        orientation=enum('authored', 'tangent', 'target'), target=nullable(target_ref),
        speed=enum('parameter', 'arc-length'), metadata=ref('metadata')))
    d['animation'] = base('animation', dict(duration_frames=integer(1), fps=ref('rate'),
        timeline=nullable({'allOf': [ref('reference'), {'properties': {'kind': enum('animation_project_3d', 'animation_sequence_3d', 'animation_clip_3d')}}]}), path=nullable(typed('camera3d-path')),
        terminal_pose=nullable(sample), metadata=ref('metadata')))
    rig_node = obj(dict(id=ID, parent=nullable(ID), transform=ref('trs'), camera=nullable(camera_ref)))
    d['rig'] = base('rig', dict(nodes=arr(rig_node, 1, 4096), metadata=ref('metadata')))
    overrides = obj(dict(transform=ref('trs'), optics=optics_ref), [])
    d['preset'] = base('preset', dict(camera=camera_ref, overrides=overrides, metadata=ref('metadata')))
    d['render'] = base('render', dict(width=integer(1, 16384), height=integer(1, 16384),
        pixel_aspect=ref('rate'), overscan={'type': 'number', 'minimum': 1, 'maximum': 4},
        gate_fit=enum('crop', 'expand'), output=enum('png', 'exr'),
        color_space=enum('linear-srgb', 'srgb'), transparent={'type': 'boolean'},
        max_pixels=integer(1, 268435456)))
    d['binding'] = base('binding', dict(scene=scene_ref, camera=camera_ref,
        rig=nullable(typed('camera3d-rig')), animation=nullable(typed('camera3d-animation')),
        overrides=overrides, targets=arr(target_ref, maximum=128), constraints=arr(constraint_ref, maximum=128),
        render=render_ref, metadata=ref('metadata')))
    d['transition'] = base('transition', dict(type=enum('cut', 'dissolve', 'fade', 'wipe'),
        duration_frames=integer(), overlap=enum('none', 'tail-head'),
        color_rgba=arr({'type': 'number', 'minimum': 0, 'maximum': 1}, 4, 4),
        direction=enum('left', 'right', 'up', 'down'), curve=enum('linear', 'smoothstep')))
    native_shot = obj(dict(render_mode={'const': '3d'}, scene=scene_ref,
        binding=typed('camera3d-binding'), duration_frames=integer(1), fps=ref('rate'),
        transition=typed('camera3d-transition'), start_frame=integer()))
    legacy_shot = obj(dict(render_mode={'const': '2d'}, legacy_shot=typed('hatch-production-shot'),
        duration_frames=integer(1), fps=ref('rate'), transition=typed('camera3d-transition'), start_frame=integer()))
    d['shot'] = base('shot', dict(shot={'oneOf': [native_shot, legacy_shot]}, metadata=ref('metadata')))
    d['storyboard'] = base('storyboard', dict(shots=arr(typed('camera3d-shot'), 1, 4096),
        total_frames=integer(1), fps=ref('rate'), source=nullable(ref('reference')), metadata=ref('metadata')))
    d['evaluated'] = base('evaluated', dict(camera=camera_ref, scene=scene_ref,
        shot=typed('camera3d-shot'), time=ref('time'), fps=ref('rate'), terminal={'type': 'boolean'},
        duration_frames=integer(1), world_matrix=ref('matrix'), view_matrix=ref('matrix'),
        projection_matrix=ref('matrix'), projection=ref('projection'), depth_of_field=ref('dof'),
        render=render_ref, dependencies=arr(ref('reference'), maximum=1024)))
    d['registry'] = base('registry', dict(resources=arr(ref('reference'), 1, 4096), metadata=ref('metadata')))
    return d


def schemas():
    """Generate only transitively referenced definitions for reviewable artifacts."""
    definitions_by_name = definitions()
    result = {}
    for kind in KINDS:
        pending, included = [kind], {}
        while pending:
            name = pending.pop()
            if name in included:
                continue
            included[name] = deepcopy(definitions_by_name[name])
            for value in _walk(included[name]):
                if isinstance(value, dict) and '$ref' in value:
                    pending.append(value['$ref'].split('/')[-1])
        result[f'camera3d-{kind}.schema.json'] = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': f'Hatch camera3d {kind} v1', '$defs': included, '$ref': '#/$defs/' + kind}
    return result


def canonical_bytes(value):
    finite_json(value)
    try:
        return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    except (TypeError,UnicodeError) as exc:
        raise ValueError('camera3d requires serializable JSON data') from exc


def portable_path(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value:
        raise ValueError('camera3d reference requires a portable relative path')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or str(path) != value or any(p in ('', '.', '..') for p in path.parts):
        raise ValueError('camera3d reference path is not canonical relative POSIX')
    for part in path.parts:
        stem = part.split('.')[0].upper()
        if (unicodedata.normalize('NFC', part) != part or part.startswith(' ') or part.endswith((' ', '.'))
                or any(ord(c) < 32 or c in ':*?"<>|' for c in part)
                or stem in {'CON','PRN','AUX','NUL','CONIN$','CONOUT$',*(f'COM{i}' for i in '123456789¹²³'),*(f'LPT{i}' for i in '123456789¹²³')}):
            raise ValueError('camera3d reference path has a nonportable component')
    return path


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


@lru_cache(maxsize=len(KINDS))
def _validator(kind):
    import jsonschema
    return jsonschema.Draft202012Validator(schemas()['camera3d-' + kind + '.schema.json'])


def validate(value, kind=None):
    """Structural plus semantic admission; not proof a renderer supports a resource."""
    import jsonschema
    finite_json(value)
    if len(canonical_bytes(value)) > MAX_BYTES:
        raise ValueError('camera3d document exceeds 8 MiB')
    declared = value.get('kind') if isinstance(value,dict) else None
    kind = kind or (declared[9:] if isinstance(declared,str) and declared.startswith('camera3d-') else '')
    if kind not in KINDS:
        raise ValueError('unsupported camera3d kind')
    try:
        _validator(kind).validate(value)
    except jsonschema.ValidationError as exc:
        raise ValueError('camera3d schema: ' + exc.message) from exc
    for item in _walk(value):
        if isinstance(item, dict):
            if 'fps' in item and isinstance(item['fps'],dict):
                fps = Fraction(int(item['fps']['numerator']),int(item['fps']['denominator']))
                if not Fraction(1,1000) <= fps <= 1000:
                    raise ValueError('camera3d fps outside shared 0.001..1000 range')
            if set(item) == {'id','version','path','sha256','kind'}:
                portable_path(item['path'])
            if set(item) == {'numerator','denominator'}:
                n, d = int(item['numerator']), int(item['denominator'])
                if math.gcd(n, d) != 1:
                    raise ValueError('camera3d rational must be reduced')
            if set(item) == {'translation','rotation','scale'}:
                if abs(sum(x*x for x in item['rotation']) - 1) > 1e-6:
                    raise ValueError('camera3d quaternion must be normalized')
            if 'near_clip_m' in item and item['near_clip_m'] >= item['far_clip_m']:
                raise ValueError('camera3d near clipping must precede far clipping')
    if kind == 'constraint':
        if math.sqrt(sum(x*x for x in value['up'])) < 1e-8:
            raise ValueError('camera3d constraint up vector is zero')
        x0,y0,x1,y1 = value['safe_rect']
        if x0 >= x1 or y0 >= y1 or value['distance_range_m'][0] > value['distance_range_m'][1]:
            raise ValueError('camera3d constraint bounds are inverted')
        if value['type'] in ('look-at', 'frame', 'focus') and not value['targets']:
            raise ValueError('camera3d tracking requires targets')
        if len({r['id'] for r in value['targets']}) != len(value['targets']):
            raise ValueError('camera3d constraint duplicates target')
    if kind == 'target' and ((value['space'] in ('bone','socket')) != (value['attachment'] is not None)):
        raise ValueError('camera3d attachment required only for bone/socket target')
    if kind == 'path':
        if value['interpolation'] == 'bezier' and (len(value['control_points_m']) - 1) % 3:
            raise ValueError('camera3d Bezier path needs 3n+1 control points')
        if value['orientation'] == 'target' and value['target'] is None:
            raise ValueError('camera3d target-oriented path requires target')
    if kind == 'animation':
        if value['timeline'] is None and value['path'] is None:
            raise ValueError('camera3d animation requires timeline or path')
        terminal = value['terminal_pose']
        if terminal is not None and (terminal['time']['frame'] != value['duration_frames'] or terminal['time']['subframe_ticks'] != 0):
            raise ValueError('camera3d terminal pose must be at exact duration boundary')
        if terminal is not None and abs(sum(x*x for x in terminal['rotation'])-1) > 1e-6:
            raise ValueError('camera3d terminal quaternion must be normalized')
    if kind == 'rig':
        nodes = {n['id']: n for n in value['nodes']}
        if len(nodes) != len(value['nodes']):
            raise ValueError('camera3d rig duplicates node ID')
        for node in nodes.values():
            seen = set()
            while node is not None:
                if node['id'] in seen:
                    raise ValueError('camera3d rig parent cycle')
                seen.add(node['id'])
                parent = node['parent']
                if parent is not None and parent not in nodes:
                    raise ValueError('camera3d rig dangling parent')
                node = nodes.get(parent)
    if kind == 'render' and value['width']*value['height']*value['overscan']**2 > value['max_pixels']:
        raise ValueError('camera3d render pixel budget exceeded')
    if kind == 'transition':
        if value['type'] == 'cut' and (value['duration_frames'] != 0 or value['overlap'] != 'none'):
            raise ValueError('camera3d cut has zero duration and no overlap')
        if value['type'] != 'cut' and (value['duration_frames'] == 0 or value['overlap'] != 'tail-head'):
            raise ValueError('camera3d transition requires positive overlap')
    if kind == 'evaluated':
        frame = Fraction(int(value['time']['frame'])) + Fraction(int(value['time']['subframe_ticks']), 1_000_000)
        if frame > value['duration_frames'] or (frame == value['duration_frames']) != value['terminal']:
            raise ValueError('camera3d terminal state is distinct from rendered frames')
        from camera3d_math import validate_evaluated_matrices
        validate_evaluated_matrices(value)
    return value


def _read_payload(path):
    """Read at most 8 MiB from an admitted regular binary file descriptor."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('camera3d rejects symlink document files')
    flags = os.O_RDONLY | getattr(os,'O_BINARY',0) | getattr(os,'O_NONBLOCK',0) | getattr(os,'O_NOFOLLOW',0)
    descriptor = os.open(path,flags)
    with os.fdopen(descriptor,'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('camera3d document requires a regular file')
        if metadata.st_size > MAX_BYTES:
            raise ValueError('camera3d document exceeds 8 MiB')
        payload = stream.read(MAX_BYTES+1)
    if len(payload) > MAX_BYTES:
        raise ValueError('camera3d document exceeds bounded read')
    return payload


def load(path):
    return validate(decode_json(_read_payload(path)))


def load_registry(root, registry):
    """Verify bounded exact-byte files and resolve complete immutable references.

    This is a read-only admission snapshot. Concurrent writers must publish their
    own immutable resources; it is not a filesystem locking or asset-store API.
    """
    root = Path(root).resolve(strict=True)
    validate(registry, 'registry')
    resources, documents, external_shots, total = {}, {}, {}, 0
    aliases = set()
    for pointer in registry['resources']:
        key = (pointer['kind'],pointer['id'],pointer['version'])
        alias = unicodedata.normalize('NFC',pointer['path']).casefold()
        if key in resources or alias in aliases:
            raise ValueError('camera3d registry duplicates resource or portable path')
        aliases.add(alias)
        parts = portable_path(pointer['path']).parts
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError('camera3d registry rejects symlink resources')
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('camera3d registry resource missing or outside root')
        payload = _read_payload(path)
        total += len(payload)
        if total > 256*1024*1024:
            raise ValueError('camera3d registry byte budget exceeded')
        if hashlib.sha256(payload).hexdigest() != pointer['sha256']:
            raise ValueError('camera3d registry resource hash mismatch')
        resources[key] = pointer
        if pointer['kind'] == 'hatch-production-shot':
            from production_contracts import validate_structure
            legacy = decode_json(payload)
            validate_structure(legacy, 'shot')
            if legacy['id'] != pointer['id']:
                raise ValueError('camera3d legacy reference ID differs from v1 shot')
            external_shots[key] = legacy
        if pointer['kind'].startswith('camera3d-'):
            document = validate(decode_json(payload))
            if (document['kind'],document['id'],document['version']) != key:
                raise ValueError('camera3d resource identity differs from reference')
            documents[key] = document
    for doc in documents.values():
        for pointer in _walk(doc):
            if isinstance(pointer,dict) and set(pointer) == {'id','version','path','sha256','kind'}:
                key = (pointer['kind'],pointer['id'],pointer['version'])
                if resources.get(key) != pointer:
                    raise ValueError('camera3d registry has unresolved exact reference')
    _validate_bindings(documents,external_shots)
    return documents


def _validate_bindings(documents,external_shots=None):
    external_shots = external_shots or {}
    def get(pointer):
        return documents.get((pointer['kind'],pointer['id'],pointer['version']))
    for doc in documents.values():
        if doc['kind'] == 'camera3d-animation' and doc['path'] is not None:
            path = get(doc['path'])
            if path['fps'] != doc['fps'] or path['duration_frames'] != doc['duration_frames']:
                raise ValueError('camera3d animation/path timing mismatch; no implicit retiming')
        if doc['kind'] == 'camera3d-camera':
            constraints = [get(p) for p in doc['constraints']]
            priorities = [c['priority'] for c in constraints if c is not None and c['enabled']]
            if len(priorities) != len(set(priorities)):
                raise ValueError('camera3d enabled constraints require distinct priorities')
        if doc['kind'] == 'camera3d-binding':
            camera = get(doc['camera'])
            if camera['parent'] is not None and camera['parent']['scene'] != doc['scene']:
                raise ValueError('camera3d camera parent belongs to another bound scene')
            all_targets = list(doc['targets'])
            animation_pointer = doc['animation'] or camera['animation']
            if animation_pointer is not None:
                animation = get(animation_pointer)
                if animation['path'] is not None:
                    path = get(animation['path'])
                    if path['target'] is not None:
                        all_targets.append(path['target'])
            for constraint in camera['constraints'] + doc['constraints']:
                all_targets.extend(get(constraint)['targets'])
            if any(get(target)['scene'] != doc['scene'] for target in all_targets):
                raise ValueError('camera3d target belongs to another bound scene')
            if doc['rig'] is not None:
                if [node['camera'] for node in get(doc['rig'])['nodes']].count(doc['camera']) != 1:
                    raise ValueError('camera3d selected camera must occur exactly once in bound rig')
            combined = [get(p) for p in camera['constraints'] + doc['constraints']]
            priorities = [c['priority'] for c in combined if c is not None and c['enabled']]
            if len(priorities) != len(set(priorities)):
                raise ValueError('camera3d bound constraints require distinct priorities')
        if doc['kind'] == 'camera3d-shot' and doc['shot']['render_mode'] == '2d':
            pointer = doc['shot']['legacy_shot']
            legacy = external_shots.get((pointer['kind'],pointer['id'],pointer['version']))
            if legacy is None or legacy['duration_frames'] != doc['shot']['duration_frames']:
                raise ValueError('camera3d legacy wrapper duration differs from unchanged v1 shot')
        if doc['kind'] == 'camera3d-shot' and doc['shot']['render_mode'] == '3d':
            if get(doc['shot']['binding'])['scene'] != doc['shot']['scene']:
                raise ValueError('camera3d shot/binding scene mismatch')
            binding = get(doc['shot']['binding'])
            camera = get(binding['camera'])
            animation_pointer = binding['animation'] or camera['animation']
            if animation_pointer is not None:
                animation = get(animation_pointer)
                if animation['fps'] != doc['shot']['fps'] or animation['duration_frames'] != doc['shot']['duration_frames']:
                    raise ValueError('camera3d shot/animation timing mismatch; no implicit retiming')

        if doc['kind'] == 'camera3d-evaluated':
            from camera3d_math import projection_matrix
            import numpy as np
            optics = dict(schema_version=1,kind='camera3d-optics',id='evaluated',version='1.0.0',
                          projection=doc['projection'],depth_of_field=doc['depth_of_field'])
            shot = get(doc['shot'])['shot']
            if shot['render_mode'] != '3d':
                raise ValueError('camera3d evaluated state cannot bind a legacy 2D shot')
            binding = get(shot['binding'])
            camera = get(binding['camera'])
            effective_optics = get(binding['overrides'].get('optics',camera['optics']))
            if (doc['camera'] != binding['camera'] or doc['scene'] != shot['scene']
                    or doc['render'] != binding['render'] or doc['fps'] != shot['fps']
                    or doc['duration_frames'] != shot['duration_frames']):
                raise ValueError('camera3d evaluated identity differs from bound shot')
            if (doc['projection'] != effective_optics['projection']
                    or doc['depth_of_field'] != effective_optics['depth_of_field']):
                raise ValueError('camera3d evaluated optical state differs from bound optics')
            expected = projection_matrix(optics,get(doc['render']))
            if not np.allclose(doc['projection_matrix'],expected,rtol=1e-9,atol=1e-9):
                raise ValueError('camera3d evaluated projection disagrees with bound render/optics')
        if doc['kind'] == 'camera3d-storyboard':
            end, previous_duration, seen = 0, 0, set()
            for pointer in doc['shots']:
                key = (pointer['id'],pointer['version'])
                if key in seen:
                    raise ValueError('camera3d storyboard duplicates shot')
                seen.add(key)
                shot = get(pointer)['shot']
                transition = get(shot['transition'])
                overlap = transition['duration_frames']
                if overlap > min(previous_duration,shot['duration_frames']):
                    raise ValueError('camera3d transition exceeds adjacent shot duration')
                if shot['fps'] != doc['fps'] or shot['start_frame'] != end-overlap:
                    raise ValueError('camera3d storyboard requires exact-rate overlap accounting')
                end = shot['start_frame']+shot['duration_frames']
                previous_duration = shot['duration_frames']
            if end != doc['total_frames']:
                raise ValueError('camera3d storyboard total frame mismatch')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check generated camera schemas')
    parser.add_argument('--generate', action='store_true', help='write only generated camera schemas')
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args()
    if args.validate is not None:
        payload = _read_payload(args.validate)
        value = validate(decode_json(payload))
        report = dict(ok=True,kind=value['kind'],id=value['id'],sha256=hashlib.sha256(payload).hexdigest(),
                      validation='document-admission',references_verified=False)
        if value['kind'] == 'camera3d-registry':
            documents = load_registry(args.validate.parent,value)
            report.update(validation='registry-byte-closure',references_verified=True,
                          native_camera_documents=len(documents),external_semantics='owner-required')
        print(json.dumps(report))
    for name, schema in schemas().items():
        path = ROOT/'profiles'/name
        content = json.dumps(schema, indent=2)+'\n'
        if args.check and (not path.is_file() or path.read_text(encoding='utf-8') != content):
            raise SystemExit('schema drift: '+str(path))
        if args.generate:
            path.write_text(content, encoding='utf-8')


if __name__ == '__main__':
    try:
        main()
    except (ValueError,OSError) as exc:
        print(json.dumps(dict(ok=False,error=str(exc))))
        raise SystemExit(2)
