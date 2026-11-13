"""Compile typed inspector edits to the authoritative native Scene3D commands.

This module does not save documents. The editor repository executes returned
commands inside its revision-checked transaction and reopens the resulting
native snapshot. Unsupported native features are reported, never simulated.
"""
from __future__ import annotations
from copy import deepcopy

from rig_errors import RigError, finite_json
from scene3d_contracts import validate_document


_NODE_FIELDS = {'camera': frozenset({'projection', 'near', 'far', 'vertical_fov', 'orthographic_height'}),
                'light': frozenset({'type', 'color', 'intensity'})}
_WORLD_FIELDS = frozenset({'background', 'exposure', 'environment'})


def _fail(message):
    raise RigError('EDITOR3D_INSPECTOR', message, stage='editor3d')


def _scene(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('scene'), dict):
        _fail('Inspectors require an acknowledged native scene snapshot.')
    return validate_document(snapshot['scene'], 'scene')


def describe_inspector(snapshot, identifier=None):
    """Return copy-only controls for actual native data in the selected snapshot."""
    scene = _scene(snapshot)
    world = dict(values=deepcopy(scene['world']), editable=sorted(_WORLD_FIELDS),
                 unavailable=['environment-import', 'HDRI-preview', 'world-animation'])
    if identifier is None:
        return dict(kind='world', id=None, world=world, editable=True)
    node = next((item for item in scene['nodes'] if item['id'] == identifier), None)
    if node is None:
        _fail('The selected node does not exist in this saved scene.')
    result = dict(id=identifier, kind=node['type'], name=node['name'], world=world,
                  editable=not node['locked'], node=deepcopy(node), controls={}, unavailable=[])
    if node['type'] == 'camera':
        result['controls']['camera'] = dict(values=deepcopy(node['camera']), editable=sorted(_NODE_FIELDS['camera']))
        result['unavailable'].extend(['focal-length-sensor-conversion', 'depth-of-field',
            'camera-target-and-path-authoring', 'camera-keyframes'])
    elif node['type'] == 'light':
        result['controls']['light'] = dict(values=deepcopy(node['light']), editable=sorted(_NODE_FIELDS['light']))
        result['unavailable'].extend(['light-shape-and-size', 'light-range', 'shadow-resolution',
            'light-animation'])
    else:
        result['unavailable'].append('native-material-version-publication')
        result['material_overrides'] = deepcopy(node['material_overrides'])
        if node['type'] == 'skinned_character':
            result['unavailable'].extend(['persistent-bone-pose', 'persistent-morph-weights',
                'rig-constraints', 'pose-mirroring'])
    return result


def compile_inspector_commands(snapshot, identifier, changes):
    """Validate bounded changes and return canonical atomic native commands.

    All values are validated as a complete candidate native scene before any
    command is returned. Lock state and node type are enforced on this boundary;
    the native transaction checks them again and owns persistence and revisions.
    """
    finite_json(changes)
    if not isinstance(changes, dict) or not changes or set(changes) - {'camera', 'light', 'world'}:
        _fail('Choose supported camera, light or world inspector fields.')
    scene = _scene(snapshot)
    candidate = deepcopy(scene)
    commands = []
    if 'world' in changes:
        values = changes['world']
        if not isinstance(values, dict) or not values or set(values) - _WORLD_FIELDS:
            _fail('Unsupported world inspector property.')
        candidate['world'].update(deepcopy(values))
        commands.append(dict(operation='configure-world', parameters=dict(properties=deepcopy(values))))
    for kind in ('camera', 'light'):
        if kind not in changes:
            continue
        values = changes[kind]
        if not isinstance(values, dict) or not values or set(values) - _NODE_FIELDS[kind]:
            _fail('Unsupported ' + kind + ' inspector property.')
        node = next((item for item in candidate['nodes'] if item['id'] == identifier), None)
        if node is None or node['type'] != kind:
            _fail('Select a native ' + kind + ' node before editing these properties.')
        if node['locked']:
            _fail('Unlock the selected node before editing its properties.')
        node[kind].update(deepcopy(values))
        commands.append(dict(operation='configure-node', parameters=dict(identifier=identifier,
            properties={kind: deepcopy(node[kind])})))
    validate_document(candidate, 'scene')
    return commands


def _material_source(snapshot, node_id, slot):
    scene = _scene(snapshot)
    node = next((item for item in scene['nodes'] if item['id'] == node_id), None)
    if node is None:
        _fail('Select a native mesh before inspecting its material.')
    if node['type'] not in {'mesh_instance', 'skinned_character', 'static_prop', 'animated_prop', 'terrain'}:
        _fail('The selected node does not support material slots.')
    for item in node['material_overrides']:
        if item['slot'] == slot:
            return item['material']
    asset = next((item for item in snapshot.get('documents', {}).values()
                  if item.get('kind') == 'scene3d-asset' and item['id'] == (node.get('asset') or {}).get('id')
                  and item['version'] == (node.get('asset') or {}).get('version')), None)
    if asset is not None:
        for item in asset['materials']:
            if item['id'] == slot:
                return item
    _fail('Choose an existing native material slot; new arbitrary slots are not admitted.')


def _load_material_registry(repository, snapshot, reference):
    """Read native material closure through the repository's exact-byte API."""
    import hashlib
    from pathlib import PurePosixPath
    from lookdev_contracts import Registry, _references, portable_path, validate_resource
    from rig_errors import decode_json
    entries, payloads, seen = [], {}, set()
    # Native LookDev dependencies are scoped to the selected resource directory.
    prefix = PurePosixPath(reference['path']).parent
    def read(path, expected):
        portable_path(path)
        full = str(prefix / path) if str(prefix) != '.' else path
        raw = repository.read_file(full, snapshot['sha256'])
        if hashlib.sha256(raw).hexdigest() != expected:
            _fail('Material dependency bytes differ from the exact reference.')
        return raw
    def visit(path, expected, depth=0):
        if depth > 16 or len(seen) >= 256:
            _fail('Material dependency budget exceeded.')
        key = (path, expected)
        if key in seen: return
        seen.add(key)
        raw = read(path, expected); value = validate_resource(decode_json(raw))
        if depth == 0 and (value.get('id') != reference['id'] or
                value.get('version') != reference['version'] or
                value.get('kind') not in {'material3d', 'material-instance3d', 'material-variant3d'}):
            _fail('Bound material bytes do not match the exact material identity.')
        entries.append((path, raw))
        for child in _references(value): visit(child['path'], child['sha256'], depth+1)
        if value.get('kind') == 'texture3d':
            file = value['file'];payloads[file['path']] = read(file['path'], file['sha256'])
    path = PurePosixPath(reference['path']).name
    visit(path, reference['sha256'])
    registry = Registry(entries, payloads=payloads)
    return registry, path, prefix, entries, payloads


def describe_materials(repository, snapshot, node_id):
    """Return only existing exact native material slots, including resolved PBR."""
    node = next((item for item in _scene(snapshot)['nodes'] if item['id'] == node_id), None)
    if node is None: _fail('Unknown node.')
    slots = {item['slot'] for item in node['material_overrides']}
    for asset in snapshot.get('documents', {}).values():
        if asset.get('kind') == 'scene3d-asset' and asset['id'] == (node.get('asset') or {}).get('id') and asset['version'] == (node.get('asset') or {}).get('version'):
            slots.update(item['id'] for item in asset['materials'])
    results = []
    for slot in sorted(slots):
        reference = _material_source(snapshot, node_id, slot)
        registry, _, _, _, _ = _load_material_registry(repository, snapshot, reference)
        native_ref = registry.reference(reference['id'], reference['version'])
        effective = registry.effective_material(native_ref)
        results.append(dict(slot=slot, reference=deepcopy(reference), effective=effective))
    return results


def apply_material_edit(repository, request):
    """Persist a validated new native material version and one slot override.

    The repository supplies revision checks, idempotency, journaling and undo.
    This fixed adapter supplies immutable LookDev bytes and delegates the scene
    replacement/publication to SceneGraph and snapshot_project.
    """
    from pathlib import PurePosixPath
    import hashlib
    from lookdev_contracts import canonical_bytes, validate_resource, Registry, _references
    from rig_errors import timestamp
    allowed = {'node_id', 'slot', 'surface', 'texture_slots', 'expected_revision',
               'expected_sha256', 'transaction_id'}
    if not isinstance(request, dict) or set(request) - allowed or not {'node_id', 'slot', 'surface',
            'expected_revision', 'expected_sha256', 'transaction_id'} <= set(request):
        _fail('Malformed material edit request.')
    snapshot = repository.snapshot()
    if not describe_inspector(snapshot, request['node_id'])['editable']:
        _fail('Unlock the selected node before editing its material.')
    reference = _material_source(snapshot, request['node_id'], request['slot'])
    registry, _, prefix, entries, payloads = _load_material_registry(repository, snapshot, reference)
    native_ref = registry.reference(reference['id'], reference['version'])
    original = registry.get(native_ref)
    effective = registry.effective_material(native_ref)
    if not isinstance(request['surface'], dict) or not request['surface']:
        _fail('A material edit must change an explicit surface property.')
    candidate = deepcopy(original)
    for key in ('base_material', 'overrides', 'graph'):
        candidate.pop(key, None)
    candidate.update(kind='material3d', workflow='metallic-roughness', surface=deepcopy(effective['surface']),
                     texture_slots=deepcopy(effective['texture_slots']), variants={},
                     render_policy=deepcopy(effective['render_policy']))
    if effective['graph'] is not None:
        _fail('Graph materials require the native graph editor; PBR inspector changes are unavailable.')
    candidate['surface'].update(deepcopy(request['surface']))
    if 'texture_slots' in request:
        if not isinstance(request['texture_slots'], dict): _fail('Texture slots must be a map.')
        candidate['texture_slots'] = deepcopy(request['texture_slots'])
    references = list(_references(candidate['surface'])) + list(_references(candidate['texture_slots']))
    candidate['dependencies'] = list({canonical_bytes(item): item for item in references}.values())
    major, minor, patch = map(int, candidate['version'].split('.'))
    candidate['version'] = f'{major}.{minor}.{patch+1}'
    candidate['provenance'] = dict(candidate['provenance'], origin='derived',
        tool='editor3d-native-material-inspector-v1', created_at=timestamp(),
        source_hashes=list(dict.fromkeys([*candidate['provenance']['source_hashes'], reference['sha256']])))
    candidate['validation_status'] = 'unvalidated'
    validate_resource(candidate)
    raw = canonical_bytes(candidate)
    digest = hashlib.sha256(raw).hexdigest()
    relative = f'{candidate["id"]}-{candidate["version"]}-{digest}.json'
    # Dependencies retain their established native directory scope. A material
    # is placed beside its original resource, in a unique content-bound path.
    full = str(prefix / relative) if str(prefix) != '.' else relative
    Registry([*entries, (relative, raw)], payloads=payloads)
    generic = dict(id=candidate['id'], version=candidate['version'], path=full, sha256=digest, kind='material')
    def builder(source_root, destination, scene_path):
        from scene3d_contracts import load_project
        from scene3d_graph import SceneGraph
        from scene3d_snapshot import snapshot_project
        bundle = load_project(source_root)
        graph = SceneGraph.from_bundle(bundle, scene_path, source_root=source_root)
        scene = graph.document()
        target = next(item for item in scene['nodes'] if item['id'] == request['node_id'])
        overrides = [item for item in target['material_overrides'] if item['slot'] != request['slot']]
        overrides.append(dict(slot=request['slot'], material=generic))
        graph.configure_node(request['node_id'], {'material_overrides': overrides})
        result = snapshot_project(source_root, destination, graph, scene_path=scene_path,
                                  extra_payloads={full: raw})
        return result['active_scene']['path']
    return repository.transact_native(request['expected_revision'], request['transaction_id'], request,
        builder, expected_sha256=request['expected_sha256'], label='Edit native material')


def inspector_routes():
    """Fixed trusted handlers for EditorServer(routes=...); no Python from HTTP."""
    def describe(server, body):
        if not isinstance(body, dict) or set(body) != {'node_id'}:
            _fail('Expected only the selected node identity.')
        snapshot = server.repository.snapshot()
        result = describe_inspector(snapshot, body['node_id'])
        if body['node_id'] is not None:
            result['materials'] = describe_materials(server.repository, snapshot, body['node_id'])
        return result
    def material(server, body):
        apply_material_edit(server.repository, body)
        return server.snapshot()
    return {'inspectors/describe': describe, 'inspectors/material': material}
