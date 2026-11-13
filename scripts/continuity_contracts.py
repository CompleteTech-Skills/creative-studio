"""Additive continuity data contracts; no geometry/visual acceptance is inferred.

JSON Schema and runtime structural validation share one authority. Files remain
owned by their native asset/rig/scene/review subsystems. This module imports no
optional geometry/render package and never changes production-v1 documents.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import unicodedata

from rig_errors import decode_json, finite_json

VERSION = '1.0.0'
MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 4096
MAX_DEPENDENCY_BYTES = 128 * 1024 * 1024
END = r'(?![\s\S])'
ID = dict(type='string', pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}' + END)
SEMVER = dict(type='string', pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)' + END)
TEXT = dict(type='string', minLength=1, maxLength=4096)
SHA = dict(type='string', pattern=r'^[0-9a-f]{64}' + END)
_BOUNDARY = r'(?:/|' + END + ')'
_RESERVED = r'(?:[Cc][Oo][Nn]|[Pp][Rr][Nn]|[Aa][Uu][Xx]|[Nn][Uu][Ll]|[Cc][Oo][Mm][1-9¹²³]|[Ll][Pp][Tt][1-9¹²³]|[Cc][Oo][Nn][Ii][Nn]\$|[Cc][Oo][Nn][Oo][Uu][Tt]\$)'
_SEGMENT = (r'(?! )(?![.]{1,2}' + _BOUNDARY + r')(?!' + _RESERVED + r'(?:[.]|/|' + END + r'))'
            + r'(?![^/]*[. ]' + _BOUNDARY + r')[^/]+')
PATH = dict(type='string', minLength=1, maxLength=1024,
            pattern=r'^(?!.*[\\:<>\"|?*\x00-\x1f])(?:' + _SEGMENT + r'/)*' + _SEGMENT + END)
BOOL = dict(type='boolean')
NUMBER = dict(type='number', minimum=-1e12, maximum=1e12)
STAMP = dict(type='string', pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' + END)
KINDS = ('identity', 'revision', 'asset', 'rig-compatibility', 'costume', 'entity',
         'character-state', 'prop-state', 'environment-state', 'camera-state',
         'animation-state', 'narrative-state', 'event', 'snapshot', 'link', 'rule',
         'exception', 'issue', 'comparison', 'review', 'dependency', 'impact', 'repair')


def obj(properties, required=None):
    return dict(type='object', additionalProperties=False, properties=properties,
                required=list(properties) if required is None else required)


def arr(items, minimum=0, maximum=MAX_ITEMS, unique=False):
    result = dict(type='array', items=items, minItems=minimum, maxItems=maximum)
    if unique:
        result['uniqueItems'] = True
    return result


def enum(*values):
    return dict(enum=list(values))


def nullable(value):
    return dict(anyOf=[value, dict(type='null')])


def ref(name):
    return {'$ref': '#/$defs/' + name}


def integer(low=0, high=2**53 - 1):
    return dict(type='integer', minimum=low, maximum=high)


def mapping(value, maximum=256):
    return dict(type='object', propertyNames=ID, additionalProperties=value, maxProperties=maximum)


def document(kind, fields):
    return obj(dict(schema_version=dict(const=VERSION), kind=dict(const='continuity-' + kind), **fields))


@lru_cache(maxsize=1)
def definitions():
    d = {}
    d['ref'] = obj(dict(id=ID, version=SEMVER, path=PATH, sha256=SHA, kind=ID))
    d['rational'] = obj(dict(numerator=integer(-2**53 + 1), denominator=integer(1)))
    d['vec3'] = arr(NUMBER, 3, 3)
    d['quaternion'] = arr(dict(type='number', minimum=-1, maximum=1), 4, 4)
    d['transform'] = obj(dict(translation=ref('vec3'), rotation=ref('quaternion'),
                              scale=arr(dict(type='number', exclusiveMinimum=0, maximum=1e6), 3, 3)))
    d['coordinates'] = obj(dict(handedness=dict(const='right'), up=dict(const='+Y'),
                                forward=dict(const='+Z'), unit=dict(const='meter'),
                                quaternion=dict(const='XYZW'), angles=dict(const='radian')))
    d['bounds'] = obj(dict(minimum=ref('vec3'), maximum=ref('vec3')))
    d['provenance'] = obj(dict(origin=enum('import', 'authored', 'derived', 'procedural-fixture'),
                              fixture=BOOL, attribution=TEXT, license=TEXT,
                              created_at=STAMP, native_receipts=arr(ref('ref'), maximum=256)))
    d['constraint'] = obj(dict(id=ID, category=enum('proportion', 'silhouette', 'anatomy', 'face',
                              'color', 'material', 'marking', 'asymmetry', 'surface', 'accessory', 'geometry'),
                              description=TEXT, measurement=nullable(NUMBER), unit=nullable(ID),
                              tolerance=nullable(dict(type='number', minimum=0, maximum=1e9)),
                              confidence=nullable(dict(type='number', minimum=0, maximum=1)),
                              evidence=arr(ref('ref'), maximum=64), uncertainty=nullable(TEXT)))
    d['visual-spec'] = obj(dict(description=TEXT, constraints=arr(ref('constraint'), maximum=256),
                                approved_variations=arr(TEXT, maximum=128),
                                default_pose=nullable(ref('ref')), default_expressions=arr(ID, maximum=128)))
    d['scope'] = obj(dict(project_id=ID, world_id=ID, timeline_id=ID, branch_id=ID,
                          entity_ids=arr(ID, unique=True), shot_ids=arr(ID, unique=True),
                          start=ref('rational'), end=ref('rational')))
    d['query'] = obj(dict(project_revision=ref('ref'), world_id=ID, timeline_id=ID,
                          branch_id=ID, story_time=ref('rational'),
                          presentation_time=ref('rational'), boundary=enum('before', 'after', 'terminal')))
    d['revision-pin'] = obj(dict(identity_id=ID, revision_id=ID, semantic_version=SEMVER, sha256=SHA))
    d['attachment'] = obj(dict(id=ID, parent_component_id=ID, local_transform=ref('transform')))
    d['component'] = obj(dict(id=ID, parent_id=nullable(ID), local_transform=ref('transform'),
                              mesh=ref('ref'), materials=arr(ref('ref'), maximum=256)))
    d['morph'] = obj(dict(id=ID, minimum=NUMBER, maximum=NUMBER, default=NUMBER))
    d['metrics'] = obj(dict(status=enum('unverified', 'measured'),
                            vertices=nullable(integer(1, 2_000_000_000)),
                            triangles=nullable(integer(1, 2_000_000_000)),
                            components=nullable(integer(1, 65536)), measured_by=nullable(ref('ref'))))
    d['state-base'] = obj(dict(entity_id=ID, query=ref('query'), transform=ref('transform'),
                              source_evaluation=ref('ref'), unavailable_fields=arr(ID, unique=True)))
    d['fact'] = obj(dict(id=ID, value=dict(type=['string', 'number', 'boolean', 'null']),
                         evidence=arr(ref('ref'), maximum=64)))
    d['change'] = obj(dict(path=PATH, before_sha256=nullable(SHA), after_sha256=nullable(SHA)))
    d['repair-op'] = obj(dict(operation=enum('select-revision', 'set-costume', 'set-fact', 'set-state',
                                           'recompute', 'rerender', 'invalidate-review'),
                              target=ref('ref'), expected_sha256=SHA, replacement=nullable(ref('ref'))))
    d['identity'] = document('identity', dict(identity_id=ID, namespace=ID, display_name=TEXT,
        description=TEXT, archetype=TEXT, canonical_revision=nullable(ref('revision-pin')),
        visual_spec=ref('visual-spec'), permitted_variations=arr(TEXT, maximum=128),
        approved_costumes=arr(ref('ref'), maximum=256), rig_requirements=nullable(ref('ref')),
        scale_m=dict(type='number', exclusiveMinimum=0, maximum=1e6),
        proportions=mapping(dict(type='number', exclusiveMinimum=0, maximum=1e6)),
        reference_images=arr(ref('ref'), maximum=256), provenance=ref('provenance'),
        approval_status=enum('unreviewed', 'reviewed', 'approved'), created_at=STAMP))
    d['revision'] = document('revision', dict(identity_id=ID, revision_id=ID, semantic_version=SEMVER,
        parent_revision_id=nullable(ID), asset=ref('ref'), geometry=ref('ref'),
        components=arr(ref('component'), minimum=1, maximum=65536),
        materials=arr(ref('ref')), textures=arr(ref('ref')), rig=nullable(ref('ref')),
        skin_weights=arr(ref('ref')), morphs=arr(ref('morph'), maximum=1024),
        attachments=arr(ref('attachment'), maximum=1024), coordinates=ref('coordinates'),
        rest_pose=nullable(ref('ref')), bounds=nullable(ref('bounds')), geometry_statistics=ref('metrics'),
        source_provenance=ref('provenance'), dependencies=arr(ref('ref')),
        reference_renders=arr(ref('ref'), maximum=256), review_records=arr(ref('ref'), maximum=256),
        visual_spec=ref('visual-spec'), created_at=STAMP))
    d['asset'] = document('asset', dict(asset_id=ID, semantic_version=SEMVER, native=ref('ref'),
        geometry=ref('ref'), media_type=enum('model/gltf-binary', 'model/gltf+json', 'application/x-blender',
                                         'application/x-hatch-rig'),
        dependencies=arr(ref('ref')), fixture=BOOL, provenance=ref('provenance')))
    d['rig-compatibility'] = document('rig-compatibility', dict(id=ID, semantic_version=SEMVER,
        rig=ref('ref'), required_bones=arr(ID, unique=True), required_sockets=arr(ID, unique=True),
        required_morphs=arr(ID, unique=True), rest_pose=nullable(ref('ref')),
        retarget_profile=nullable(ref('ref')), inspection=nullable(ref('ref')),
        status=enum('compatible', 'incompatible', 'partial', 'unverified')))
    d['costume'] = document('costume', dict(id=ID, semantic_version=SEMVER, identity_id=ID,
        variant=ref('ref'), accessories=arr(ref('ref')), material_overrides=arr(ref('ref')),
        scope=ref('scope'), evidence=arr(ref('ref')), approval=enum('pending', 'approved', 'rejected')))
    d['entity'] = document('entity', dict(entity_id=ID, world_id=ID, entity_type=enum('character', 'prop', 'environment'),
        identity_id=nullable(ID), revision=nullable(ref('revision-pin')), asset=ref('ref'),
        shot_instance_ids=arr(ID, unique=True), render_ids=arr(ID, unique=True)))
    d['character-state'] = document('character-state', dict(state=ref('state-base'),
        revision=ref('revision-pin'), costume=nullable(ref('ref')), animation=nullable(ref('ref')),
        pose=nullable(ref('ref')), velocity=ref('vec3'), visibility=BOOL,
        contacts=arr(ref('ref')), expressions=arr(ID, maximum=128)))
    d['prop-state'] = document('prop-state', dict(state=ref('state-base'), asset=ref('ref'),
        owner_entity_id=nullable(ID), owner_socket_id=nullable(ID), physical_state=nullable(ref('ref'))))
    d['environment-state'] = document('environment-state', dict(world_id=ID, query=ref('query'),
        scene=ref('ref'), lights=arr(ref('ref')), atmosphere=nullable(ref('ref')), facts=arr(ref('fact'))))
    d['camera-state'] = document('camera-state', dict(camera_id=ID, query=ref('query'),
        evaluated=ref('ref'), world_transform=ref('transform'), projection=enum('perspective', 'orthographic'),
        source_camera=ref('ref')))
    d['animation-state'] = document('animation-state', dict(entity_id=ID, query=ref('query'),
        animation=ref('ref'), pose=ref('ref'), phase=ref('rational'),
        root_transform=ref('transform'), velocity=ref('vec3'), contacts=arr(ref('ref'))))
    d['narrative-state'] = document('narrative-state', dict(query=ref('query'), facts=arr(ref('fact')),
        completed_events=arr(ID, unique=True), source_events=arr(ref('ref'))))
    d['event'] = document('event', dict(event_id=ID, world_id=ID, timeline_id=ID, branch_id=ID,
        story_time=ref('rational'), presentation_time=ref('rational'), priority=integer(0, 65535),
        event_type=enum('spawn', 'despawn', 'set-costume', 'set-fact', 'handoff', 'relocate', 'environment', 'exception'),
        entity_ids=arr(ID, minimum=1, unique=True), prerequisites=arr(ID, unique=True),
        payload=ref('ref'), provenance=ref('provenance')))
    d['snapshot'] = document('snapshot', dict(snapshot_id=ID, query=ref('query'),
        entities=arr(ref('ref')), environment=nullable(ref('ref')), narrative=nullable(ref('ref')),
        dependencies=arr(ref('ref')), evaluator=ref('ref'), unavailable_fields=arr(ID, unique=True)))
    d['link'] = document('link', dict(link_id=ID, outgoing=ref('query'), incoming=ref('query'),
        outgoing_shot_id=ID, incoming_shot_id=ID, scene_mapping=ref('transform'),
        entity_mapping=mapping(ID, MAX_ITEMS), aspects=arr(enum('identity', 'costume', 'position', 'pose',
        'velocity', 'contact', 'owner', 'camera', 'lighting', 'narrative'), minimum=1, unique=True), exceptions=arr(ref('ref'))))
    d['rule'] = document('rule', dict(rule_id=ID, semantic_version=SEMVER, scope=ref('scope'),
        aspect=enum('identity', 'costume', 'position', 'pose', 'velocity', 'contact', 'owner', 'camera', 'lighting', 'narrative'),
        severity=enum('error', 'warning', 'review'), comparison=enum('equal', 'within-tolerance', 'requires', 'compatible'),
        target=ref('ref'), expected=ref('ref'), tolerance=dict(type='number', minimum=0, maximum=1e9),
        unit=ID, dependencies=arr(ref('ref'))))
    d['exception'] = document('exception', dict(exception_id=ID, rule=ref('ref'), scope=ref('scope'),
        reason=TEXT, authorized_by=TEXT, authorization=ref('ref'), evidence=arr(ref('ref'), minimum=1),
        expires_at=nullable(STAMP)))
    d['issue'] = document('issue', dict(issue_id=ID, rule=ref('ref'), query=ref('query'),
        severity=enum('error', 'warning', 'review'), expected=ref('ref'), observed=ref('ref'),
        measurement=nullable(NUMBER), tolerance=nullable(dict(type='number', minimum=0, maximum=1e9)),
        unit=ID, exceptions=arr(ref('ref')), evidence=arr(ref('ref')), message=TEXT))
    d['comparison'] = document('comparison', dict(comparison_id=ID, baseline=ref('ref'), current=ref('ref'),
        metric=ID, metric_version=SEMVER, measured=nullable(NUMBER), threshold=nullable(NUMBER),
        unit=ID, settings=ref('ref'), evidence=arr(ref('ref'), minimum=1),
        status=enum('consistent', 'inconsistent', 'needs_visual_review', 'unavailable'), limitations=arr(TEXT, maximum=128)))
    d['review'] = document('review', dict(review_id=ID, reviewer=TEXT,
        reviewer_kind=enum('human', 'assistant', 'synthetic-test'), verdict=enum('approved', 'rejected', 'pending'),
        subject_sha256=SHA, inspected=arr(ref('ref'), minimum=1), scope=ref('scope'),
        fixture=BOOL, native_receipt=ref('ref'), created_at=STAMP, notes=TEXT))
    d['dependency'] = document('dependency', dict(source=ref('ref'), target=ref('ref'),
        scope=ref('scope'), aspects=arr(ID, minimum=1, unique=True)))
    d['impact'] = document('impact', dict(impact_id=ID, before=ref('ref'), after=ref('ref'),
        affected=arr(ref('ref')), unaffected=arr(ref('ref')), dependencies=arr(ref('ref')),
        recompute_state=arr(ID, unique=True), reinspect_assets=arr(ID, unique=True),
        evaluate_animation=arr(ID, unique=True), rerender=arr(ID, unique=True),
        assemble_video=arr(ID, unique=True), invalidate_reviews=arr(ID, unique=True),
        unavailable=arr(ID, unique=True)))
    d['repair'] = document('repair', dict(plan_id=ID, expected_revision=ref('ref'), impact=ref('ref'),
        operations=arr(ref('repair-op'), minimum=1, maximum=1024), authorization=ref('ref'),
        scope=ref('scope'), rollback=ref('ref')))
    d['bindings'] = obj(dict(schema_version=dict(const=VERSION), project_id=ID, project_revision=ref('ref'),
        instances=arr(obj(dict(instance_id=ID, entity_id=ID, shot_id=ID, revision=ref('revision-pin'))))))
    return d


def schemas():
    def closure(kind):
        found = {}
        def visit(name):
            if name in found:
                return
            found[name] = deepcopy(definitions()[name])
            for item in _walk(found[name]):
                if '$ref' in item:
                    visit(item['$ref'].split('/')[-1])
        visit(kind)
        return found
    return {kind: {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                   '$id': 'https://gethatch.pet/schemas/continuity-' + kind + '-v1',
                   'title': 'Continuity ' + kind + ' v' + VERSION,
                   '$defs': closure(kind), '$ref': '#/$defs/' + kind}
            for kind in KINDS}


def canonical_bytes(value):
    finite_json(value)
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError('CONTINUITY_JSON: invalid JSON value') from exc
    if len(data) > MAX_BYTES:
        raise ValueError('CONTINUITY_LIMIT: manifest exceeds 8 MiB')
    return data


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def portable_path(value):
    if (not isinstance(value, str) or len(value) > 1024 or not value or '\\' in value or ':' in value
            or value.startswith('/') or unicodedata.normalize('NFC', value) != value):
        raise ValueError('CONTINUITY_PATH: expected canonical portable relative path')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', *(f'COM{i}' for i in '123456789¹²³'), *(f'LPT{i}' for i in '123456789¹²³')}
    for part in value.split('/'):
        if (part in ('', '.', '..') or part.startswith(' ') or part.endswith(('.', ' ')) or any(ord(c) < 32 for c in part)
                or any(c in part for c in '<>"|?*') or part.split('.')[0].upper() in reserved):
            raise ValueError('CONTINUITY_PATH: unsafe portable path')
    return value


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def validate_document(value, kind=None):
    # Enforce resource bounds before recursive copying of programmatic inputs.
    canonical_bytes(value)
    value = deepcopy(value)
    if kind is None:
        kind = str(value.get('kind', '')).removeprefix('continuity-') if isinstance(value, dict) else ''
    if kind not in KINDS and kind not in ('bindings',):
        raise ValueError('CONTINUITY_KIND: unsupported contract kind')
    # Optional jsonschema is needed only for explicit contract operations, never import/discovery.
    from jsonschema import Draft202012Validator
    schema = {'$defs': definitions(), '$ref': '#/$defs/' + kind}
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        raise ValueError('CONTINUITY_SCHEMA: ' + str(list(errors[0].path)) + ': ' + errors[0].message)
    for item in _walk(value):
        if 'path' in item:
            portable_path(item['path'])
        if 'confidence' in item and item['confidence'] is not None and not item.get('evidence'):
            raise ValueError('CONTINUITY_CONFIDENCE: confidence requires documented evidence')
        if set(item) == {'numerator', 'denominator'}:
            # JSON Schema integer means a number with zero fractional part;
            # normalize exact validated values before Python integer arithmetic.
            item['numerator'], item['denominator'] = int(item['numerator']), int(item['denominator'])
            if math.gcd(item['numerator'], item['denominator']) != 1:
                raise ValueError('CONTINUITY_TIME: rational times must be reduced')
        if set(item) == {'translation', 'rotation', 'scale'}:
            if abs(sum(x*x for x in item['rotation']) - 1) > 1e-8:
                raise ValueError('CONTINUITY_TRANSFORM: quaternion must be normalized')
        if set(item) == {'minimum', 'maximum'}:
            if any(a > b for a, b in zip(item['minimum'], item['maximum'])):
                raise ValueError('CONTINUITY_BOUNDS: inverted bounds')
        if set(item) == {'start', 'end', 'project_id', 'world_id', 'timeline_id', 'branch_id', 'entity_ids', 'shot_ids'}:
            from fractions import Fraction
            if Fraction(int(item['start']['numerator']), int(item['start']['denominator'])) >= Fraction(int(item['end']['numerator']), int(item['end']['denominator'])):
                raise ValueError('CONTINUITY_SCOPE: interval must be nonempty and half-open')
        if item.get('origin') == 'procedural-fixture' and not item['fixture']:
            raise ValueError('CONTINUITY_PROVENANCE: procedural fixtures cannot become original artwork')
        if 'created_at' in item:
            datetime.strptime(item['created_at'], '%Y-%m-%dT%H:%M:%SZ')
        if item.get('expires_at') is not None:
            datetime.strptime(item['expires_at'], '%Y-%m-%dT%H:%M:%SZ')
    for key in ('components', 'attachments', 'morphs', 'facts', 'instances', 'constraints'):
        if key in value:
            ids = [x['instance_id'] if key == 'instances' else x['id'] for x in value[key]]
            if len(ids) != len(set(ids)):
                raise ValueError('CONTINUITY_REFERENCE: duplicate ' + key + ' IDs')
    if kind == 'revision':
        metrics = value['geometry_statistics']
        measured = metrics['status'] == 'measured'
        values = [metrics[key] for key in ('vertices', 'triangles', 'components', 'measured_by')]
        if measured and (any(item is None for item in values) or value['bounds'] is None):
            raise ValueError('CONTINUITY_INSPECTION: measured geometry requires actual metrics, bounds and receipt')
        if not measured and any(item is not None for item in values):
            raise ValueError('CONTINUITY_INSPECTION: unverified metrics must remain explicitly unavailable')
        if measured and metrics['measured_by']['kind'] != 'evidence':
            raise ValueError('CONTINUITY_INSPECTION: measurement source must be an inspection receipt, not mesh metadata')
        components = {x['id']: x for x in value['components']}
        state = {}
        for component in components.values():
            current, path = component['id'], []
            while current is not None and state.get(current) != 2:
                if current not in components or state.get(current) == 1:
                    raise ValueError('CONTINUITY_REFERENCE: missing or cyclic component parent')
                state[current] = 1
                path.append(current)
                current = components[current]['parent_id']
            for name in path:
                state[name] = 2
        for attachment in value['attachments']:
            if attachment['parent_component_id'] not in components:
                raise ValueError('CONTINUITY_REFERENCE: attachment references unknown component')
        for morph in value['morphs']:
            if not morph['minimum'] <= morph['default'] <= morph['maximum']:
                raise ValueError('CONTINUITY_MORPH: invalid morph range')
        if value['parent_revision_id'] == value['revision_id']:
            raise ValueError('CONTINUITY_REFERENCE: revision cannot parent itself')
        if value['asset']['kind'] not in ('model3d-asset', 'asset-library-version', 'hatch-3d-rig', 'mesh'):
            raise ValueError('CONTINUITY_ASSET: canonical asset must reference native 3D authority')
        if value['geometry']['kind'] not in ('mesh', 'model3d-asset', 'hatch-3d-rig'):
            raise ValueError('CONTINUITY_ASSET: geometry reference must be 3D, not an image')
    if kind == 'identity' and value['canonical_revision'] is not None and value['canonical_revision']['identity_id'] != value['identity_id']:
        raise ValueError('CONTINUITY_IDENTITY: canonical revision belongs to another logical identity')
    if kind == 'identity' and value['approval_status'] == 'approved' and value['canonical_revision'] is None:
        raise ValueError('CONTINUITY_APPROVAL: approved identity must select an exact revision')
    if kind == 'asset' and value['fixture'] != value['provenance']['fixture']:
        raise ValueError('CONTINUITY_PROVENANCE: fixture classification must be preserved')
    if kind == 'rig-compatibility' and value['status'] == 'compatible' and value['inspection'] is None:
        raise ValueError('CONTINUITY_INSPECTION: compatible status requires retained inspection')
    if kind == 'comparison' and value['status'] in ('consistent', 'inconsistent'):
        if value['measured'] is None or value['threshold'] is None:
            raise ValueError('CONTINUITY_COMPARISON: completed metrics require measurements and thresholds')
    if kind == 'entity':
        character = value['entity_type'] == 'character'
        if ((character and (value['identity_id'] is None or value['revision'] is None))
                or (not character and (value['identity_id'] is not None or value['revision'] is not None))):
            raise ValueError('CONTINUITY_IDENTITY: character requires separate identity/revision')
        if character and value['identity_id'] != value['revision']['identity_id']:
            raise ValueError('CONTINUITY_IDENTITY: entity identity and revision disagree')
    if kind == 'prop-state' and value['owner_socket_id'] is not None and value['owner_entity_id'] is None:
        raise ValueError('CONTINUITY_OWNER: socket without owner')
    if kind == 'event' and value['event_type'] == 'handoff' and len(value['entity_ids']) < 2:
        raise ValueError('CONTINUITY_EVENT: handoff requires distinct participant entities')
    if kind == 'event' and value['event_id'] in value['prerequisites']:
        raise ValueError('CONTINUITY_EVENT: event cannot depend on itself')
    if kind == 'review' and value['reviewer_kind'] == 'synthetic-test' and value['verdict'] == 'approved':
        raise ValueError('CONTINUITY_REVIEW: synthetic tests cannot grant visual approval')
    if kind == 'link':
        if value['outgoing']['boundary'] != 'terminal':
            raise ValueError('CONTINUITY_BOUNDARY: outgoing state must be terminal, not last displayed frame')
        if len(set(value['entity_mapping'].values())) != len(value['entity_mapping']):
            raise ValueError('CONTINUITY_REFERENCE: entity mapping must be one-to-one')
    return value


def load_document(path, kind=None):
    # Do not reopen a path after a file-type check: a FIFO/link can race it.
    return validate_document(decode_json(_read_regular_bytes(Path(path), limit=MAX_BYTES)), kind)


def references(value):
    """All typed exact pointers, deduplicated; contradictory path bindings fail."""
    found = {}
    for item in _walk(value):
        if set(item) == {'id', 'version', 'path', 'sha256', 'kind'}:
            key = unicodedata.normalize('NFC', item['path']).casefold()
            previous = found.get(key)
            if previous is not None and previous != item:
                raise ValueError('CONTINUITY_REFERENCE: conflicting immutable path reference')
            found[key] = item
    return [found[key] for key in sorted(found)]


def verify_references(root, value, *, maximum_bytes=MAX_DEPENDENCY_BYTES):
    """Verify regular native files by bytes. This is not geometry/visual inspection.

    Symlinks at every dependency component are rejected; stable open-handle and
    final path identities detect observed replacement. Arbitrary hostile ancestor
    renames are not a filesystem sandbox and are outside this local API guarantee.
    """
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_DEPENDENCY_BYTES:
        raise ValueError('CONTINUITY_LIMIT: invalid dependency byte limit')
    canonical_bytes(value)
    root = Path(root).resolve(strict=True)
    total = 0
    result = []
    for item in references(value):
        relative = portable_path(item['path'])
        path = root
        for part in relative.split('/'):
            path /= part
            if path.is_symlink():
                raise ValueError('CONTINUITY_PATH: dependency symlink is forbidden')
        flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes - total:
                raise ValueError('CONTINUITY_LIMIT: nonregular or oversized dependency')
            digestor = hashlib.sha256()
            count = 0
            while True:
                chunk = stream.read(min(1024 * 1024, maximum_bytes - total - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if total + count > maximum_bytes:
                    raise ValueError('CONTINUITY_LIMIT: dependency closure exceeds byte limit')
                digestor.update(chunk)
            after = os.fstat(stream.fileno())
            current = path.stat(follow_symlinks=False)
        fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
            raise ValueError('CONTINUITY_CHANGED: dependency changed while verifying')
        if digestor.hexdigest() != item['sha256']:
            raise ValueError('CONTINUITY_HASH: dependency bytes disagree with exact reference')
        total += count
        result.append(dict(path=relative, sha256=item['sha256'], byte_size=count))
    return dict(files=result, total_bytes=total, geometry_inspected=False, visual_approval=False)


def _read_regular_bytes(path, *, limit, expected_sha256=None):
    """Read one regular file through one bounded nonblocking/nofollow handle."""
    path = Path(path)
    leaf = path.lstat()
    if not stat.S_ISREG(leaf.st_mode):
        raise ValueError('CONTINUITY_PATH: nonregular or symlink document/receipt rejected')
    if leaf.st_size > limit:
        raise ValueError('CONTINUITY_LIMIT: oversized document or receipt')
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('CONTINUITY_LIMIT: nonregular or oversized document or receipt')
        payload = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
    fingerprint = lambda status: (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
    if fingerprint(leaf) != fingerprint(before) or fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
        raise ValueError('CONTINUITY_CHANGED: document or receipt changed while reading')
    if len(payload) > limit:
        raise ValueError('CONTINUITY_LIMIT: document or receipt exceeds byte bound')
    if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError('CONTINUITY_HASH: receipt bytes disagree with pinned decision')
    return payload


def read_reference_bytes(root, item, *, limit=MAX_BYTES):
    """One bounded nofollow/nonblocking descriptor binds inspected receipt bytes."""
    if type(limit) is not int or not 1 <= limit <= MAX_BYTES:
        raise ValueError('CONTINUITY_LIMIT: invalid receipt byte bound')
    relative = portable_path(item['path'])
    path = Path(root).resolve(strict=True)
    for part in relative.split('/'):
        path /= part
        if path.is_symlink():
            raise ValueError('CONTINUITY_PATH: receipt symlink is forbidden')
    return _read_regular_bytes(path, limit=limit, expected_sha256=item['sha256'])


def generate(destination, check=False):
    destination = Path(destination)
    for name, schema in schemas().items():
        path = destination / ('continuity-' + name + '.schema.json')
        payload = (json.dumps(schema, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()
        if check:
            if not path.is_file() or path.read_bytes() != payload:
                raise ValueError('CONTINUITY_SCHEMA_STALE: ' + str(path))
        else:
            destination.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate-schemas', type=Path)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args()
    if not args.generate_schemas and not args.validate:
        parser.error('select --generate-schemas or --validate')
    try:
        if args.generate_schemas:
            generate(args.generate_schemas, args.check)
        if args.validate:
            value = load_document(args.validate)
            print(json.dumps(dict(valid=True, kind=value['kind'], sha256=digest(value),
                                  geometry_inspected=False, visual_approval=False)))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps(dict(valid=False, error=str(exc), geometry_inspected=False, visual_approval=False)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
