"""Versioned, dependency-free structural and relational decomposition contracts.

The exported JSON schemas and runtime checks share these definitions. Extensions
are explicitly namespaced data; no extension is executed or silently interpreted.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path, PurePosixPath
import re

from asset_contracts import MAX_SOURCE_PIXELS, hash_object
from decomposition_masks import MAX_COMPONENTS, MAX_MASK_PIXELS, MAX_TOTAL_RUNS, point

SCHEMA_VERSION = 1
ALGORITHM = 'source-ownership-v1'


def obj(properties, required=None):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(properties if required is None else required)}


def array(items, maximum=10000, minimum=0):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 8192}
NOTE = {'type': 'string', 'maxLength': 16384}
SLUG = {'type': 'string', 'pattern': r'^[a-z0-9][a-z0-9-]{0,95}$'}
HASH = {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'}
NUMBER = {'type': 'number'}
INTEGER = {'type': 'integer'}
BOOLEAN = {'type': 'boolean'}
NULL = {'type': 'null'}
CONFIDENCE = {'type': 'number', 'minimum': 0, 'maximum': 1}
POINT = array(NUMBER, 2, 2)
BOX = array({'type': 'integer', 'minimum': 0}, 4, 4)
EXTENSIONS = {'type': 'object', 'maxProperties': 128}
ASSET = obj({'path': TEXT, 'sha256': HASH})
NULL_ID = {'anyOf': [SLUG, NULL]}
ORIENTATION = {'enum': ['front', 'back', 'left', 'right', 'unknown']}
OCCLUSION = obj({'status': {'enum': ['visible', 'partial', 'unknown']},
                 'occluded_by': array(SLUG, MAX_COMPONENTS), 'note': NOTE})
ATTACHMENT = obj({'id': SLUG, 'point': POINT, 'target_id': NULL_ID, 'confidence': CONFIDENCE})
REVIEW = obj({'status': {'enum': ['pending', 'approved', 'rejected']},
              'author': NOTE, 'note': NOTE, 'signature': {'anyOf': [HASH, NULL]}})
MASK_SPEC = {'oneOf': [
    obj({'path': TEXT}),
    obj({'runs': array(array({'type': 'integer', 'minimum': 0}, 3, 3), MAX_TOTAL_RUNS)}),
    obj({'polygons': array(array(POINT, 10000, 3), 4096, 1)}),
    obj({'colors': obj({'values': array({'type': 'string', 'pattern': '^#[0-9a-fA-F]{6}$'}, 32, 1),
                       'tolerance': {'type': 'number', 'minimum': 0, 'maximum': 64},
                       'seeds': array(POINT, 1024, 1),
                       'within': array(array(POINT, 10000, 3), 4096, 1)}, ['values'])})]}
ANNOTATED_COMPONENT = obj({'id': SLUG, 'name': TEXT, 'category': TEXT, 'parent_id': NULL_ID,
    'z_index': {'type': 'integer', 'minimum': -100000, 'maximum': 100000},
    'side': {'enum': ['left', 'right', 'center', 'unknown']}, 'mask': MASK_SPEC,
    'pivot': {'anyOf': [POINT, NULL]}, 'attachments': array(ATTACHMENT, 128),
    'confidence': CONFIDENCE, 'occlusion': OCCLUSION, 'notes': array(TEXT, 128)},
    ['id', 'name', 'category', 'mask'])
ANNOTATION_SCHEMA = obj({'schema_version': {'type': 'integer', 'const': 1}, 'source_sha256': HASH,
    'author': TEXT, 'note': TEXT, 'orientation': ORIENTATION, 'foreground_mask': MASK_SPEC,
    'components': array(ANNOTATED_COMPONENT, MAX_COMPONENTS), 'extensions': EXTENSIONS},
    ['schema_version', 'source_sha256', 'author', 'note', 'components'])

MODEL_CALL = obj({'id': {'anyOf': [TEXT, NULL]}, 'model': {'anyOf': [TEXT, NULL]},
    'parameters': {'anyOf': [{'type': 'object'}, NULL]}, 'unavailable_reason': NOTE}, [])
RECONSTRUCTION = obj({'kind': {'const': 'inferred'}, 'status': {'enum': ['pending-review', 'approved', 'rejected']},
    'asset': ASSET, 'bbox': BOX, 'inferred_mask': ASSET, 'allowed_mask': ASSET,
    'original_output': ASSET, 'receipt': ASSET, 'observed_asset': ASSET, 'source_sha256': HASH,
    'token': TEXT, 'input_hashes': {'type': 'object', 'additionalProperties': HASH},
    'fixture': BOOLEAN, 'model_call': MODEL_CALL, 'note': TEXT})
PROVENANCE = obj({'kind': {'enum': ['directly-observed', 'segmented-observed', 'manually-corrected', 'inferred']},
    'source_sha256': HASH, 'annotation_sha256': HASH, 'orientation': ORIENTATION, 'method': TEXT,
    'author': TEXT, 'note': NOTE, 'original_asset': ASSET,
    'reconstruction': RECONSTRUCTION},
    ['kind', 'source_sha256', 'annotation_sha256', 'orientation', 'method', 'author', 'note'])
COMPONENT = obj({'id': SLUG, 'name': TEXT, 'category': TEXT, 'parent_id': NULL_ID,
    'z_index': {'type': 'integer', 'minimum': -100000, 'maximum': 100000},
    'side': {'enum': ['left', 'right', 'center', 'unknown']}, 'bbox': BOX,
    'asset': ASSET, 'mask': ASSET, 'pivot': {'anyOf': [POINT, NULL]},
    'attachments': array(ATTACHMENT, 128), 'confidence': CONFIDENCE,
    'transform': obj({'position': POINT, 'rotation': NUMBER, 'scale': POINT, 'local_origin': POINT}),
    'uncertainty': array(TEXT, 256), 'occlusion': OCCLUSION, 'provenance': PROVENANCE,
    'review': REVIEW})

ERROR = obj({'max_channel_error': {'type': 'integer', 'minimum': 0, 'maximum': 255},
             'rmse': {'type': 'number', 'minimum': 0}, 'different_pixels': {'type': 'integer', 'minimum': 0}})
QUALITY = obj({'width': {'type': 'integer', 'minimum': 1}, 'height': {'type': 'integer', 'minimum': 1},
    'foreground_pixels': {'type': 'integer', 'minimum': 1},
    'partial_alpha_pixels': {'type': 'integer', 'minimum': 0}, 'foreground_bbox': BOX,
    'unique_colors_up_to_256': {'anyOf': [{'type': 'integer', 'minimum': 1, 'maximum': 256}, {'const': '>256'}]},
    'luminance_stddev': {'type': 'number', 'minimum': 0}, 'edge_contrast_mean': {'type': 'number', 'minimum': 0},
    'pixel_art': BOOLEAN, 'resampling': {'enum': ['nearest', 'none']},
    'source_resized': {'const': False}, 'warnings': array(TEXT, 256)})
VALIDATION = obj({'schema_version': {'type': 'integer', 'const': 1}, 'algorithm': {'const': ALGORITHM},
    'ok': BOOLEAN, 'faithful_fidelity_passed': BOOLEAN, 'semantic_status': {'enum': ['pending', 'approved', 'rejected']},
    'input_signature': HASH, 'reconstruction_error': ERROR, 'source_pixel_coverage': CONFIDENCE,
    'foreground_pixels': {'type': 'integer', 'minimum': 0},
    'unassigned_foreground_pixels': {'type': 'integer', 'minimum': 0},
    'unexpected_overlap_pixels': {'type': 'integer', 'minimum': 0},
    'gap_pixels': {'type': 'integer', 'minimum': 0}, 'alpha_preserved': BOOLEAN,
    'component_clipping': array(SLUG, MAX_COMPONENTS), 'edge_artifacts': {'type': 'integer', 'minimum': 0},
    'generated_content_pixels': {'type': 'integer', 'minimum': 0}, 'errors': array(TEXT, 1024),
    'warnings': array(TEXT, 1024), 'tolerances': obj({'max_channel_error': {'const': 0},
        'minimum_coverage': {'const': 1}, 'maximum_overlap_pixels': {'const': 0}})})

MANIFEST_SCHEMA = obj({'schema_version': {'type': 'integer', 'const': 1}, 'kind': {'const': 'character-decomposition'},
    'revision': {'type': 'integer', 'minimum': 1}, 'mode': {'enum': ['faithful', 'assisted']},
    'character': obj({'id': SLUG, 'name': TEXT, 'orientation': ORIENTATION}),
    'source': obj({'sha256': HASH, 'path': TEXT, 'normalized_path': TEXT, 'normalized_sha256': HASH,
        'format': {'enum': ['PNG', 'WEBP', 'JPEG']}, 'origin': TEXT, 'fixture': BOOLEAN,
        'original_name': TEXT, 'original_mode': TEXT, 'exif_orientation': {'anyOf': [INTEGER, NULL]}}),
    'canvas': obj({'width': {'type': 'integer', 'minimum': 1}, 'height': {'type': 'integer', 'minimum': 1},
        'pixel_art': BOOLEAN}),
    'foreground': obj({'path': TEXT, 'sha256': HASH, 'mask': ASSET, 'method': TEXT,
        'confidence': CONFIDENCE, 'review_required': BOOLEAN, 'warnings': array(TEXT, 256)}),
    'components': array(COMPONENT, MAX_COMPONENTS + 1, 1), 'annotation': ASSET, 'identity': ASSET,
    'analysis': obj({'mode': {'enum': ['manual-pending', 'annotation-assisted', 'model-assisted']},
        'semantic_model_called': BOOLEAN, 'candidates': array(obj({'id': SLUG, 'bbox': BOX,
            'pixels': {'type': 'integer', 'minimum': 0}, 'semantic_character': {'const': False},
            'confidence': CONFIDENCE}), 128), 'multiple_characters_suspected': BOOLEAN,
        'selection': {'anyOf': [BOX, NULL]}, 'selection_resolved': BOOLEAN, 'warnings': array(TEXT, 256)}),
    'quality': QUALITY,
    'rig': obj({'animation_ready': {'const': False}, 'status': {'enum': ['unrigged', 'partial']},
        'missing_pivots': array(SLUG, MAX_COMPONENTS + 1),
        'suggested_pivots': {'type': 'object', 'additionalProperties': obj({'point': POINT, 'confidence': CONFIDENCE, 'method': TEXT})},
        'note': TEXT}),
    'validation': VALIDATION,
    'review': obj({'status': {'enum': ['pending', 'approved', 'rejected']},
        'pending': {'type': 'integer', 'minimum': 0}, 'approved': {'type': 'integer', 'minimum': 0},
        'rejected': {'type': 'integer', 'minimum': 0}}),
    'artifacts': {'type': 'object', 'additionalProperties': HASH}, 'extensions': EXTENSIONS})


def schemas():
    from decomposition_analysis import schemas as analysis_schemas
    return {**analysis_schemas(), 'decomposition.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'Hatch Pet character decomposition v1',
        'description': 'Runtime also checks paths, hashes, exact pixels, hierarchy, review signatures and bounded allocations.',
        **copy.deepcopy(MANIFEST_SCHEMA)},
        'decomposition-annotations.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'Hatch Pet source-bound semantic component annotations v1', **copy.deepcopy(ANNOTATION_SCHEMA)}}


def validate_structure(value, spec, label='decomposition'):
    """Check our published, deliberately small JSON Schema subset without a new dependency."""
    for join in ('anyOf', 'oneOf'):
        if join in spec:
            matches = 0
            for child in spec[join]:
                try:
                    validate_structure(value, child, label)
                    matches += 1
                except ValueError:
                    pass
            if not matches or (join == 'oneOf' and matches != 1):
                raise ValueError(label + ': value does not match the supported alternatives')
    kind = spec.get('type')
    tests = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'number': type(value) in (int, float) and math.isfinite(value),
             'boolean': type(value) is bool, 'null': value is None}
    if kind and not tests[kind]:
        raise ValueError(label + ': expected ' + kind)
    if 'const' in spec and (value != spec['const'] or isinstance(value, bool) != isinstance(spec['const'], bool)):
        raise ValueError(label + ': unsupported constant/version')
    if 'enum' in spec and value not in spec['enum']:
        raise ValueError(label + ': unsupported value')
    if kind in ('integer', 'number'):
        if value < spec.get('minimum', -math.inf) or value > spec.get('maximum', math.inf):
            raise ValueError(label + ': number outside supported bounds')
    if kind == 'string':
        if not spec.get('minLength', 0) <= len(value) <= spec.get('maxLength', math.inf):
            raise ValueError(label + ': invalid text length')
        if spec.get('minLength', 0) and not value.strip():
            raise ValueError(label + ': nonempty text required')
        if 'pattern' in spec and not re.search(spec['pattern'], value):
            raise ValueError(label + ': invalid format')
    if kind == 'array':
        if not spec.get('minItems', 0) <= len(value) <= spec.get('maxItems', math.inf):
            raise ValueError(label + ': array resource bound or required count')
        for index, child in enumerate(value):
            validate_structure(child, spec.get('items', {}), label + '[' + str(index) + ']')
    if kind == 'object':
        if len(value) > spec.get('maxProperties', 100000):
            raise ValueError(label + ': object resource bound exceeded')
        if set(spec.get('required', [])) - value.keys():
            raise ValueError(label + ': missing required fields ' + ', '.join(sorted(set(spec['required']) - value.keys())))
        properties = spec.get('properties', {})
        extra = set(value) - properties.keys()
        if spec.get('additionalProperties') is False and extra:
            raise ValueError(label + ': unsupported fields ' + ', '.join(sorted(extra)))
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(label + ': string object keys required')
            child_spec = properties.get(key, spec.get('additionalProperties', {}))
            if isinstance(child_spec, dict):
                validate_structure(child, child_spec, label + '.' + key)


def safe_path(root, value):
    # Geometry, review and transport use the same portable filename contract.
    from decomposition_package import _relative
    _relative(value)
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or '\0' in value:
        raise ValueError('unsafe decomposition artifact path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('.', '..', '') for p in value.split('/')):
        raise ValueError('decomposition artifact path escapes package')
    target = Path(root) / path
    from rig_errors import normalize_root
    root = normalize_root(root)
    target = root / path
    if not normalize_root(target.parent).is_relative_to(root):
        raise ValueError('decomposition artifact path escapes package')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symbolic links are not decomposition assets')
    return target


def check_slug(value):
    validate_structure(value, SLUG, 'component identifier')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', value):
        raise ValueError('invalid component identifier')
    if value in {'con', 'prn', 'aux', 'nul', *('com' + str(i) for i in range(1, 10)), *('lpt' + str(i) for i in range(1, 10))}:
        raise ValueError('reserved filename is not a portable component identifier')


def validate_relations(components, size):
    if size[0] * size[1] > MAX_SOURCE_PIXELS or size[0] * size[1] * len(components) > MAX_MASK_PIXELS:
        raise ValueError('decomposition exceeds source/mask working-set resource bound')
    ids = [c['id'] for c in components]
    if len(ids) != len(set(ids)):
        raise ValueError('component identifiers must be unique')
    by_id = {c['id']: c for c in components}
    for component in components:
        check_slug(component['id'])
        parent = component.get('parent_id')
        if parent is not None and (parent not in by_id or parent == component['id']):
            raise ValueError('unknown or self parent for component ' + component['id'])
        visited = {component['id']}
        while parent is not None:
            if parent in visited:
                raise ValueError('component hierarchy contains a cycle')
            visited.add(parent)
            parent = by_id[parent].get('parent_id')
            if parent is not None and parent not in by_id:
                raise ValueError('unknown component parent')
        if component.get('pivot') is not None:
            point(component['pivot'], size, 'pivot')
        attachment_ids = []
        for attachment in component.get('attachments', []):
            check_slug(attachment['id'])
            point(attachment['point'], size, 'attachment point')
            target = attachment['target_id']
            if target is not None and (target not in by_id or target == component['id']):
                raise ValueError('attachment must name a different existing component')
            attachment_ids.append(attachment['id'])
        if len(attachment_ids) != len(set(attachment_ids)):
            raise ValueError('attachment identifiers must be unique per component')
        for target in component.get('occlusion', {}).get('occluded_by', []):
            if target not in by_id or target == component['id']:
                raise ValueError('occlusion must name a different existing component')


def component_signature(component):
    """Bind approval to every material field, excluding only the approval itself."""
    return hash_object({key: value for key, value in component.items() if key != 'review'})
