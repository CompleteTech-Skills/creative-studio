"""Editor v1 bindings to native resource owners, never a second scene format.

These are admission contracts, not implementations of persistence, authoring or
rendering. Importing this module loads no optional numerical dependency.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import argparse
import json
import math
import os
import re
import stat
from pathlib import Path

from rig_contracts import COORDINATES, obj, arr, ref
from rig_errors import RigError, decode_json, finite_json, object_hash, portable_name, require

VERSION = '1.0.0'
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
KINDS = ('project', 'scene', 'node', 'asset', 'animation', 'command', 'revision',
         'session', 'derived', 'request', 'response')
# The server implementing these contracts remains a separate work package.
READ_OPERATIONS = ('projects.list', 'projects.open', 'projects.get', 'projects.validate',
    'revisions.list', 'scenes.list', 'scenes.get', 'scenes.validate', 'assets.list',
    'assets.get', 'assets.content', 'assets.validate', 'assets.dependencies',
    'render.capabilities', 'render.status', 'render.progress', 'render.manifest',
    'render.output', 'recovery.inspect', 'recovery.list', 'recovery.compare',
    'recovery.dependencies')
WRITE_OPERATIONS = ('projects.create', 'projects.save', 'projects.close', 'revisions.create',
    'revisions.restore', 'scenes.create', 'scenes.command', 'scenes.delete',
    'assets.import', 'assets.thumbnail', 'assets.relink', 'render.create',
    'render.cancel', 'render.retry', 'recovery.recover')
OPERATIONS = READ_OPERATIONS + WRITE_OPERATIONS

TIMESTAMP_PATTERN = (r'^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt](?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]'
                     r'(?:\.[0-9]{1,6})?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])(?![\s\S])')


def _timestamp(value, path):
    """Validate the supported offset-aware RFC3339 subset without optional extras."""
    if not isinstance(value, str) or re.fullmatch(TIMESTAMP_PATTERN, value) is None:
        _error('TIMESTAMP', 'Use a complete RFC3339 timestamp with an explicit UTC offset.', path)
    try:
        parsed = datetime.fromisoformat(value.replace('t', 'T').replace('z', 'Z').replace('Z', '+00:00'))
    except ValueError as exc:
        raise RigError('EDITOR3D_TIMESTAMP', 'Invalid timestamp: ' + str(exc),
                       stage='editor3d', asset=path) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _error('TIMESTAMP', 'Timestamp must include a UTC offset.', path)
    return parsed


def _doc(kind, properties):
    return obj(dict(schema_version={'const': 1}, kind={'const': 'editor3d-' + kind}, **properties))


@lru_cache(maxsize=1)
def _definitions():
    from model3d_contracts import _definitions as model_definitions
    text = dict(type='string', minLength=1, maxLength=4096)
    ident = dict(type='string', pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?![\s\S])')
    integer = dict(type='integer', minimum=0, maximum=2_000_000_000)
    digest = dict(type='string', pattern=r'^[0-9a-f]{64}(?![\s\S])')
    version = dict(type='string', pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?![\s\S])')
    nullable = lambda value: {'anyOf': [value, {'type': 'null'}]}
    ids = dict(arr(ident, 0, 4096), uniqueItems=True)
    stamp = dict(type='string', format='date-time', maxLength=64, pattern=TIMESTAMP_PATTERN)
    file = deepcopy(model_definitions()['file'])
    native = obj(dict(owner={'enum': ['rig', 'model3d', 'scene3d', 'production2d',
        'camera3d', 'lookdev', 'animation3d']}, id=ident, version=version,
        native_kind=ident, file=file))
    pointer = obj(dict(id=ident, sha256=digest))
    fps = obj(dict(numerator=dict(type='integer', minimum=1, maximum=240000),
                   denominator=dict(type='integer', minimum=1, maximum=10000)))
    interval = obj(dict(start=integer, end=integer))
    metadata = obj(dict(label=text, notes=dict(type='string', maxLength=4096)))
    diagnostic = obj(dict(code=ident, path=dict(type='string', maxLength=1024),
        message=text, correction=text, retryable={'type': 'boolean'}))
    provenance = obj(dict(origin={'enum': ['authored', 'imported', 'procedural-fixture']},
        fixture={'type': 'boolean'}, description=text, source_refs=arr(file, 0, 128)))
    project = _doc('project', dict(id=ident, name=text, revision=integer,
        created_at=stamp, updated_at=stamp,
        application=obj(dict(name={'const': 'hatch-editor3d'}, version=version)),
        coordinates={'const': COORDINATES}, color_management=nullable(native),
        frame_rate=fps, scenes=arr(pointer, 0, 4096), shots=arr(native, 0, 4096),
        shared_assets=arr(pointer, 0, 4096), characters=arr(native, 0, 4096),
        animation_clips=arr(pointer, 0, 4096), render_presets=arr(native, 0, 128),
        editor_metadata=metadata, provenance=provenance,
        dependency_manifest=arr(native, 0, 4096), source_projects=arr(native, 0, 128),
        validation_status={'enum': ['unvalidated', 'valid', 'invalid']},
        review_status={'const': 'unreviewed'}))
    scene = _doc('scene', dict(id=ident, project_id=ident, native=native,
        nodes=arr(pointer, 0, 4096), collections=ids, default_camera=nullable(ident),
        animations=ids, timeline=interval, editor_metadata=metadata))
    node = _doc('node', dict(id=ident, scene_id=ident, native=native, native_node_id=ident,
        node_type={'enum': ['mesh', 'group', 'character', 'armature', 'bone', 'camera',
            'point-light', 'directional-light', 'spot-light', 'area-light',
            'world', 'primitive', 'instance']}, parent=nullable(ident), sibling_order=integer,
        selectable={'type': 'boolean'}, locked={'type': 'boolean'}, collections=ids,
        asset_id=nullable(ident), rig_id=nullable(ident), editor_metadata=metadata))
    asset = _doc('asset', dict(id=ident, native=native, asset_type={'enum': [
        'model3d', 'rig', 'image2d', 'reference2d', 'material', 'texture', 'camera', 'animation']},
        original_filename=dict(type='string', minLength=1, maxLength=255),
        original_format=ident, normalized_format=ident, provenance=provenance,
        license=obj(dict(identifier=nullable(text), attribution=nullable(text),
            permission={'enum': ['unknown', 'restricted', 'permitted']}, evidence=nullable(file))),
        dependencies=arr(native, 0, 4096), thumbnails=arr(file, 0, 32),
        validation_status={'enum': ['unvalidated', 'valid', 'invalid']},
        previous=nullable(native)))
    animation = _doc('animation', dict(id=ident, native=native,
        targets=ids, frame_rate=fps, interval=interval))
    from rig_contracts import definitions as rig_definitions
    transform = deepcopy(rig_definitions()['Bone']['properties']['rest'])
    command = _doc('command', dict(id=ident, project_id=ident, scene_id=ident,
        expected_revision=integer, expected_project_sha256=digest,
        transaction_id=ident, operation={'enum': ['set-transform', 'reparent', 'set-metadata',
            'create-node', 'delete-node', 'set-native-property', 'replace-animation']},
        targets=ids, input=native, inverse=nullable(native), author=ident))
    revision = _doc('revision', dict(id=ident, project_id=ident, number=integer,
        parent=nullable(pointer), project_sha256=digest, command_ids=ids,
        created_at=stamp, author=ident, snapshot=file))
    session = _doc('session', dict(id=ident, project_id=ident, base_revision=integer,
        base_project_sha256=digest, selection=ids, active_scene=nullable(ident),
        playhead=integer, viewport=transform, overlays=arr({'enum': ['grid', 'bones',
            'bounds', 'selection', 'gizmo']}, 0, 5), tool={'enum': ['select', 'translate',
            'rotate', 'scale']}, unsaved_command_ids=ids))
    derived = _doc('derived', dict(project_id=ident, project_sha256=digest,
        frame=integer, frame_rate=fps, native_dependencies=arr(native, 0, 4096),
        evidence=arr(file, 0, 4096), status={'enum': ['unavailable', 'stale', 'verified']}))
    request = _doc('request', dict(id=ident, operation={'enum': list(OPERATIONS)},
        project_id=nullable(ident), target_id=nullable(ident), expected_revision=nullable(integer),
        expected_project_sha256=nullable(digest), argument=nullable(file),
        idempotency_key=nullable(ident)))
    response = _doc('response', dict(request_id=ident,
        status={'enum': ['accepted', 'complete', 'rejected', 'unavailable', 'cancelled']},
        project_revision=nullable(integer), project_sha256=nullable(digest),
        outputs=arr(file, 0, 4096), diagnostics=arr(diagnostic, 0, 128),
        executed={'type': 'boolean'}, review_status={'const': 'unreviewed'}))
    return dict(project=project, scene=scene, node=node, asset=asset, animation=animation,
        command=command, revision=revision, session=session, derived=derived, request=request,
        response=response, NativeReference=native, FileReference=file, ResourcePointer=pointer,
        FrameRate=fps, FrameInterval=interval, EditorMetadata=metadata, Diagnostic=diagnostic,
        Provenance=provenance, Transform=transform)


def schemas():
    return {'editor3d.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'Editor3D v1 resource bindings and service admission contracts',
        '$defs': deepcopy(_definitions()), 'oneOf': [ref(k) for k in KINDS]}}


def _error(code, message, path=''):
    raise RigError('EDITOR3D_' + code, message, stage='editor3d', asset=path)


def _unique(items, label):
    values = [i['id'] for i in items]
    if len(values) != len(set(values)):
        _error('DUPLICATE', 'Resource IDs must be unique.', label)
    return set(values)


def _walk(value, path=''):
    if isinstance(value, dict):
        yield value, path
        for key, child in value.items():
            yield from _walk(child, path + '/' + key)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _walk(child, path + '/' + str(i))


def validate_contract(value):
    """Validate structure and local semantics; no file is read or state mutated."""
    finite_json(value)
    if not isinstance(value, dict) or value.get('kind') not in ['editor3d-' + k for k in KINDS]:
        _error('KIND', 'Unsupported editor contract kind.')
    from jsonschema import Draft202012Validator, FormatChecker
    kind = value['kind'].removeprefix('editor3d-')
    validator = Draft202012Validator({'$defs': _definitions(), '$ref': '#/$defs/' + kind},
                                    format_checker=FormatChecker())
    error = next(validator.iter_errors(value), None)
    if error:
        _error('SCHEMA', error.message, '/' + '/'.join(map(str, error.absolute_path)))
    for child, path in _walk(value):
        for field in ('created_at', 'updated_at'):
            if field in child:
                _timestamp(child[field], path + '/' + field)
        if set(child) == {'path', 'sha256', 'bytes', 'media_type'}:
            try:
                portable_name(child['path'])
            except RigError as exc:
                _error('PATH', str(exc), path + '/path')
        if set(child) == {'numerator', 'denominator'}:
            if math.gcd(int(child['numerator']), int(child['denominator'])) != 1:
                _error('RATE', 'Use a reduced positive rational frame rate.', path)
        if set(child) == {'start', 'end'} and child['start'] >= child['end']:
            _error('INTERVAL', 'Frame intervals are nonempty and half-open [start,end).', path)
        if set(child) == {'translation', 'rotation', 'scale'}:
            if abs(math.hypot(*child['rotation']) - 1) > 1e-6:
                _error('QUATERNION', 'Use a normalized XYZW quaternion.', path + '/rotation')
    if kind == 'project':
        for key in ('scenes', 'shared_assets', 'animation_clips'):
            _unique(value[key], '/' + key)
        if _timestamp(value['updated_at'], '/updated_at') < _timestamp(value['created_at'], '/created_at'):
            _error('TIME', 'updated_at cannot precede created_at.', '/updated_at')
        if value['provenance']['origin'] == 'procedural-fixture' and not value['provenance']['fixture']:
            _error('PROVENANCE', 'Procedural fixtures must remain explicitly labeled.', '/provenance')
    if kind == 'asset':
        if '/' in value['original_filename'] or '\\' in value['original_filename']:
            _error('PATH', 'Original filename is a basename, not an import path.', '/original_filename')
        if value['provenance']['origin'] == 'procedural-fixture' and not value['provenance']['fixture']:
            _error('PROVENANCE', 'Procedural fixtures must remain labeled.', '/provenance')
        if value['asset_type'] == 'rig' and (value['native']['owner'] != 'rig'
                or value['native']['native_kind'] != 'Rig'):
            _error('OWNER', 'Rig assets require a native Rig contract.', '/native')
        if value['asset_type'] in ('image2d', 'reference2d') and value['native']['owner'] != 'production2d':
            _error('OWNER', '2D assets retain production2d authority.', '/native/owner')
    if kind == 'command' and not value['targets']:
        _error('TARGET', 'A command must name its targets.', '/targets')
    if kind == 'node' and value['node_type'] == 'bone' and value['rig_id'] is None:
        _error('RIG', 'Bone bindings require an explicit rig owner.', '/rig_id')
    if kind == 'scene':
        _unique(value['nodes'], '/nodes')
        if value['native']['owner'] != 'scene3d':
            _error('OWNER', 'Editor scenes bind the scene3d owner.', '/native/owner')
    if kind == 'request':
        operation = value['operation']
        global_operations = {'projects.list', 'projects.create', 'render.capabilities', 'recovery.list'}
        argument_operations = {'projects.create', 'projects.save', 'scenes.create', 'scenes.command',
            'assets.import', 'assets.relink', 'render.create', 'render.retry', 'recovery.recover'}
        untargeted = global_operations | {'projects.open', 'projects.get', 'projects.validate',
            'projects.save', 'projects.close', 'revisions.create', 'revisions.list', 'scenes.list',
            'scenes.create', 'assets.list', 'assets.import', 'render.create', 'recovery.inspect',
            'recovery.dependencies'}
        if (operation in global_operations) != (value['project_id'] is None):
            _error('TARGET', 'This operation has the wrong project scope.', '/project_id')
        if (operation in untargeted) != (value['target_id'] is None):
            _error('TARGET', 'This operation has the wrong resource scope.', '/target_id')
        if (operation in argument_operations) != (value['argument'] is not None):
            _error('ARGUMENT', 'This operation has the wrong argument presence.', '/argument')
        if operation in READ_OPERATIONS and (value['idempotency_key'] is not None
                or value['expected_revision'] is not None or value['expected_project_sha256'] is not None):
            _error('PRECONDITION', 'Read operations do not carry mutation preconditions.')
        if value['operation'] in WRITE_OPERATIONS:
            if value['idempotency_key'] is None:
                _error('IDEMPOTENCY', 'Mutations require an idempotency key.', '/idempotency_key')
            if value['operation'] != 'projects.create' and any(value[k] is None for k in ('project_id', 'expected_revision', 'expected_project_sha256')):
                _error('PRECONDITION', 'Mutations require an exact project revision and digest.')
        if value['operation'] == 'projects.create' and any(value[k] is not None for k in ('expected_revision', 'expected_project_sha256')):
            _error('PRECONDITION', 'Project creation cannot overwrite an existing revision.')
    if kind == 'response':
        if value['status'] in ('rejected', 'unavailable') and not value['diagnostics']:
            _error('DIAGNOSTIC', 'Failed or unavailable requests need an actionable diagnostic.')
        if value['status'] != 'complete' and value['executed']:
            _error('STATUS', 'Only complete responses may declare execution.')
        if value['status'] == 'complete' and not value['executed']:
            _error('STATUS', 'Complete means the declared operation actually executed.')
    if kind == 'derived' and value['status'] == 'verified' and not value['evidence']:
        _error('EVIDENCE', 'Verified derived state requires exact artifact evidence.')
    return value


def contract_hash(value):
    validate_contract(value)
    return object_hash(value)


def validate_project_bundle(project, scenes, nodes, assets, animations):
    """Verify editor binding closure and digests, not native scene correctness."""
    for value in [project, *scenes, *nodes, *assets, *animations]:
        validate_contract(value)
    for group, kind in [(scenes, 'scene'), (nodes, 'node'), (assets, 'asset'), (animations, 'animation')]:
        if any(x['kind'] != 'editor3d-' + kind for x in group):
            _error('KIND', 'Bundle member has the wrong contract kind.', kind)
        _unique(group, kind)
    if project['kind'] != 'editor3d-project':
        _error('KIND', 'Expected project contract.')
    index = {x['id']: x for x in [*scenes, *nodes, *assets, *animations]}
    if len(index) != len(scenes) + len(nodes) + len(assets) + len(animations):
        _error('DUPLICATE', 'IDs are global within an editor project.')
    for pointers, group in [(project['scenes'], scenes), (project['shared_assets'], assets),
                            (project['animation_clips'], animations)]:
        if {x['id'] for x in pointers} != {x['id'] for x in group}:
            _error('REFERENCE', 'Project pointers must exactly cover supplied bindings.')
        for pointer in pointers:
            if pointer['sha256'] != contract_hash(index[pointer['id']]):
                _error('HASH', 'Binding digest differs from the project snapshot.', pointer['id'])
    if project['id'] in index:
        _error('DUPLICATE', 'Project identity cannot alias a child binding.')
    scene_ids = {s['id'] for s in scenes}
    asset_ids = {a['id'] for a in assets}
    animation_ids = {a['id'] for a in animations}
    node_ids = {n['id'] for n in nodes}
    for scene in scenes:
        if scene['project_id'] != project['id']:
            _error('REFERENCE', 'Scene belongs to another project.', scene['id'])
        local = {n['id']: n for n in nodes if n['scene_id'] == scene['id']}
        if {p['id'] for p in scene['nodes']} != set(local):
            _error('REFERENCE', 'Scene pointers must cover exactly their node bindings.', scene['id'])
        for pointer in scene['nodes']:
            if pointer['sha256'] != contract_hash(local[pointer['id']]):
                _error('HASH', 'Node binding digest differs from scene snapshot.', pointer['id'])
        if not set(scene['animations']) <= animation_ids:
            _error('REFERENCE', 'Unknown scene animation.', scene['id'])
        camera = scene['default_camera']
        if camera is not None and (camera not in local or local[camera]['node_type'] != 'camera'):
            _error('REFERENCE', 'Default camera must name a camera in this scene.', scene['id'])
        sibling_keys = set()
        native_keys = set()
        for node in local.values():
            native_key = (node['native']['owner'], node['native']['id'],
                          node['native']['file']['sha256'], node['native_node_id'])
            if native_key in native_keys:
                _error('DUPLICATE', 'Native node is already bound in this scene.', node['id'])
            native_keys.add(native_key)
            if node['parent'] is not None and node['parent'] not in local:
                _error('REFERENCE', 'Parent is outside the node scene.', node['id'])
            if not set(node['collections']) <= set(scene['collections']):
                _error('REFERENCE', 'Node refers to an unknown collection.', node['id'])
            if node['asset_id'] is not None and node['asset_id'] not in asset_ids:
                _error('REFERENCE', 'Unknown asset binding.', node['id'])
            if node['rig_id'] is not None and (node['rig_id'] not in asset_ids or index[node['rig_id']]['asset_type'] != 'rig'):
                _error('RIG', 'Rig binding must identify a rig asset.', node['id'])
            key = (node['parent'], node['sibling_order'])
            if key in sibling_keys:
                _error('ORDER', 'Sibling ordering must be unambiguous.', node['id'])
            sibling_keys.add(key)
            seen = set()
            cursor = node['id']
            while cursor is not None:
                if cursor in seen:
                    _error('CYCLE', 'Parent hierarchy contains a cycle.', cursor)
                if cursor not in local:
                    _error('REFERENCE', 'Ancestor is outside the node scene.', cursor)
                seen.add(cursor)
                cursor = local[cursor]['parent']
    for node in nodes:
        if node['scene_id'] not in scene_ids:
            _error('REFERENCE', 'Node scene is absent.', node['id'])
    for animation in animations:
        if not set(animation['targets']) <= node_ids:
            _error('REFERENCE', 'Animation target binding is absent.', animation['id'])
        if animation['frame_rate'] != project['frame_rate']:
            _error('RATE', 'Animation and project editor clocks differ.', animation['id'])
        for scene in scenes:
            if animation['id'] in scene['animations']:
                if any(index[target]['scene_id'] != scene['id'] for target in animation['targets']):
                    _error('REFERENCE', 'Animation targets are outside the bound scene.', animation['id'])
                if not scene['timeline']['start'] <= animation['interval']['start'] < animation['interval']['end'] <= scene['timeline']['end']:
                    _error('INTERVAL', 'Animation interval exceeds the bound scene timeline.', animation['id'])
    return {'status': 'valid-bindings', 'project_sha256': contract_hash(project),
            'native_resources_verified': False, 'review_status': 'unreviewed'}


def validate_runtime_state(value, project, scenes, nodes):
    """Validate nonpersistent editor state against its exact saved snapshot."""
    validate_contract(value)
    validate_contract(project)
    if project['kind'] != 'editor3d-project':
        _error('KIND', 'Expected a saved editor project.')
    for values, kind in [(scenes, 'scene'), (nodes, 'node')]:
        for item in values:
            validate_contract(item)
            if item['kind'] != 'editor3d-' + kind:
                _error('KIND', 'Runtime context contains the wrong contract kind.')
    scene_map = {scene['id']: scene for scene in scenes}
    if len(scene_map) != len(scenes) or {p['id'] for p in project['scenes']} != set(scene_map):
        _error('REFERENCE', 'Runtime context does not match the saved scene set.')
    for pointer in project['scenes']:
        if pointer['sha256'] != contract_hash(scene_map[pointer['id']]):
            _error('HASH', 'Runtime scene differs from the saved project binding.')
    node_map = {node['id']: node for node in nodes}
    if len(node_map) != len(nodes):
        _error('DUPLICATE', 'Runtime node identities must be unique.')
    if set(node_map) != {p['id'] for scene in scenes for p in scene['nodes']} or any(
            node['scene_id'] not in scene_map for node in nodes):
        _error('REFERENCE', 'Runtime context contains nodes outside the saved scene closure.')
    for scene in scenes:
        if scene['project_id'] != project['id']:
            _error('REFERENCE', 'Runtime scene belongs to another project.')
        if {p['id'] for p in scene['nodes']} != {n['id'] for n in nodes if n['scene_id'] == scene['id']}:
            _error('REFERENCE', 'Runtime nodes do not match saved scene bindings.')
        for pointer in scene['nodes']:
            if pointer['sha256'] != contract_hash(node_map[pointer['id']]):
                _error('HASH', 'Runtime node differs from the saved scene binding.')
    if value['kind'] not in ('editor3d-session', 'editor3d-derived'):
        _error('KIND', 'Expected session or derived editor state.')
    if value['project_id'] != project['id']:
        _error('REFERENCE', 'Editor state belongs to another project.')
    expected_hash = value.get('base_project_sha256', value.get('project_sha256'))
    if expected_hash != contract_hash(project):
        _error('STALE', 'Editor state refers to a different saved snapshot.')
    if value['kind'] == 'editor3d-derived':
        if value['frame_rate'] != project['frame_rate']:
            _error('RATE', 'Derived state and project editor clocks differ.')
        if not any(scene['timeline']['start'] <= value['frame'] <= scene['timeline']['end']
                   for scene in scenes):
            _error('FRAME', 'Derived frame is outside the saved scene boundary ranges.')
    if value['kind'] == 'editor3d-session':
        if value['base_revision'] != project['revision']:
            _error('STALE', 'Editor session revision is stale.')
        scene_map = {scene['id']: scene for scene in scenes}
        if value['active_scene'] is not None and value['active_scene'] not in scene_map:
            _error('REFERENCE', 'Session active scene is absent.')
        permitted = {node['id'] for node in nodes if node['scene_id'] == value['active_scene']}
        if not set(value['selection']) <= permitted:
            _error('REFERENCE', 'Selection must belong to the active scene.')
        if value['active_scene'] is not None:
            interval = scene_map[value['active_scene']]['timeline']
            if not interval['start'] <= value['playhead'] <= interval['end']:
                _error('FRAME', 'Playhead is outside the scene including its terminal boundary.')
    return value


def migrate_document(value):
    """Identity migration only; unknown versions are explicitly unsupported."""
    return deepcopy(validate_contract(value))


def _ts(schema):
    if '$ref' in schema:
        return schema['$ref'].split('/')[-1]
    if 'const' in schema:
        return json.dumps(schema['const'], separators=(',', ':'))
    if 'enum' in schema:
        return ' | '.join(json.dumps(x) for x in schema['enum'])
    if 'anyOf' in schema:
        return '(' + ' | '.join(_ts(x) for x in schema['anyOf']) + ')'
    kind = schema.get('type')
    if kind == 'object':
        fields = schema.get('properties', {})
        required = schema.get('required', [])
        return '{ ' + '; '.join(json.dumps(k) + ('' if k in required else '?') + ': ' + _ts(v) for k, v in fields.items()) + ' }'
    if kind == 'array':
        item = _ts(schema['items'])
        if schema.get('minItems') == schema.get('maxItems'):
            return '[' + ', '.join(item for _ in range(schema['minItems'])) + ']'
        return 'Array<' + item + '>'
    return {'string': 'string', 'integer': 'number', 'number': 'number',
            'boolean': 'boolean', 'null': 'null'}[kind]


def typescript():
    return '// Generated by scripts/editor3d_contracts.py; runtime admission is still required.\n' + '\n'.join(
        'export type ' + name + ' = ' + _ts(schema) + ';' for name, schema in _definitions().items()) + '\n'


def generated_files():
    root = Path(__file__).resolve().parents[1]
    return {root / 'profiles/editor3d.schema.json': json.dumps(schemas()['editor3d.schema.json'], indent=2) + '\n',
            root / 'assets/editor3d/contracts.ts': typescript()}


def load_document(path):
    """Read a bounded regular descriptor; reject nonregular and symlink inputs."""
    flags = (os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    try:
        if Path(path).is_symlink():
            _error('FILE', 'Editor document input must not be a symbolic link.')
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            observed = os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode):
                _error('FILE', 'Editor documents must be regular files.')
            if not 0 < observed.st_size <= MAX_DOCUMENT_BYTES:
                _error('LIMIT', 'Editor document must contain 1 byte to 4 MiB.')
            payload = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(payload) > MAX_DOCUMENT_BYTES or len(payload) != observed.st_size:
                _error('LIMIT', 'Editor document size changed during bounded admission.')
    except OSError as exc:
        raise RigError('EDITOR3D_FILE', 'Cannot open editor document: ' + str(exc),
                       stage='editor3d', asset=str(path)) from exc
    return decode_json(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args(argv)
    if args.validate:
        value = load_document(args.validate)
        print(json.dumps(dict(status='valid-contract', sha256=contract_hash(value), native_resources_verified=False)))
        return
    for path, content in generated_files().items():
        if args.check:
            require(path.is_file() and path.read_text(encoding='utf-8') == content,
                    'EDITOR3D_DRIFT', 'Generated editor contract differs.', asset=str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')


if __name__ == '__main__':
    try:
        main()
    except RigError as exc:
        print(json.dumps(exc.report))
        raise SystemExit(1)
