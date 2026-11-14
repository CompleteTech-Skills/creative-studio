"""Read-only editor projection of canonical prefab expansion.

The captured source bundle and authored instance IDs remain immutable. This
adapter delegates all expansion to PrefabLibrary and exposes a separate runtime
scene; it never detaches, publishes, or edits prefab definitions.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

from asset_library import read_source_bytes
from rig_errors import decode_json, require, safe_path
from scene3d_contracts import (MAX_DEPTH, MAX_FILES, MAX_TOTAL_BYTES,
                               canonical_bytes, portable_path)
from scene3d_prefabs import PrefabLibrary


def _bound(value, name, maximum):
    require(type(value) is int and 1 <= value <= maximum,
            'EDITOR3D_PREFAB_LIMIT', 'Use a bounded positive integer for ' + name + '.')
    return value


def prepare_prefab_projection(captured_root, snapshot, *, max_nodes=1000,
                              max_files=MAX_FILES, max_bytes=64 * 1024 * 1024,
                              max_depth=MAX_DEPTH):
    """Expand within the caller's verified captured-source lifetime.

    Return None for an ordinary scene. Otherwise ``runtime_snapshot`` is private
    evaluation context, never a saved repository snapshot. ``projection`` is the
    explicit read-only browser metadata. Source and virtual derived inventories
    stay distinct and all original byte references are retained.
    """
    max_nodes = _bound(max_nodes, 'max_nodes', 10000)
    max_files = _bound(max_files, 'max_files', MAX_FILES)
    max_bytes = _bound(max_bytes, 'max_bytes', MAX_TOTAL_BYTES)
    max_depth = _bound(max_depth, 'max_depth', MAX_DEPTH)
    scene = snapshot['scene']
    if not any(node['type'] == 'prefab_instance' for node in scene['nodes']):
        return None
    root = Path(captured_root)
    require(root.is_dir() and not root.is_symlink(), 'EDITOR3D_PREFAB_SOURCE',
            'Use the owned captured source directory.')
    scene_path = snapshot['scene_path']
    portable_path(scene_path)
    require(scene_path in snapshot['documents'] and scene_path in snapshot['inventory']
            and snapshot['documents'][scene_path] == scene,
            'EDITOR3D_PREFAB_SOURCE', 'Authored scene differs from its verified bundle.')
    inventory = snapshot['inventory']
    project_limits = snapshot['project']['budgets']
    def project_bound(name, ceiling):
        value = project_limits[name]
        require(type(value) in (int, float) and 1 <= value <= ceiling
                and (type(value) is int or value.is_integer()),
                'EDITOR3D_PREFAB_LIMIT', 'Invalid native project budget: ' + name + '.')
        return int(value)
    max_nodes = min(max_nodes, project_bound('max_nodes', 10000))
    max_files = min(max_files, project_bound('max_files', MAX_FILES))
    max_bytes = min(max_bytes, project_bound('max_bytes', MAX_TOTAL_BYTES))
    require(len(scene['nodes']) <= max_nodes and len(inventory) <= max_files,
            'EDITOR3D_PREFAB_LIMIT', 'Captured scene exceeds the interactive node/file budget.')
    total = 0
    for name, record in inventory.items():
        portable_path(name)
        require(type(record['bytes']) is int and 0 <= record['bytes'] <= max_bytes,
                'EDITOR3D_PREFAB_LIMIT', 'Invalid captured resource byte budget.')
        total += record['bytes']
        require(total <= max_bytes, 'EDITOR3D_PREFAB_LIMIT',
                'Captured resources exceed the interactive byte budget.')
    source = read_source_bytes(safe_path(root, scene_path, exists=True), min(max_bytes, 16 * 1024 * 1024))
    require(len(source) == inventory[scene_path]['bytes'] and
            hashlib.sha256(source).hexdigest() == inventory[scene_path]['sha256'] and
            decode_json(source) == scene, 'EDITOR3D_PREFAB_SOURCE',
            'Authored scene bytes changed after snapshot admission.')
    # from_files preserves noncanonical original bytes and checks exact references.
    expanded = PrefabLibrary.from_files(root, snapshot).expand(scene)
    runtime_scene = expanded['scene']
    require(len(runtime_scene['nodes']) <= max_nodes, 'EDITOR3D_PREFAB_LIMIT',
            'Expanded prefab nodes exceed the interactive budget.')
    authored = {node['id']: node for node in scene['nodes']}
    runtime = {node['id']: node for node in runtime_scene['nodes']}
    require(set(authored) <= set(runtime), 'EDITOR3D_PREFAB_IDENTITY',
            'Canonical expansion lost an authored anchor.')
    edit_targets = {}
    for identifier in runtime:
        cursor, seen = identifier, set()
        while cursor not in authored:
            require(cursor in runtime and cursor not in seen and len(seen) < max_depth,
                    'EDITOR3D_PREFAB_LIMIT', 'Invalid or excessive derived prefab ancestry.')
            seen.add(cursor)
            cursor = runtime[cursor]['parent']
        if identifier not in authored:
            require(authored[cursor]['type'] == 'prefab_instance',
                    'EDITOR3D_PREFAB_IDENTITY', 'Derived content has no authored instance anchor.')
            edit_targets[identifier] = cursor
    # Virtual documents are explicit derived context. No source inventory entry,
    # source bytes, authored scene or portable project record is replaced.
    virtual = 'editor-runtime-' + snapshot['sha256'] + '/scene.json'
    derived = deepcopy(expanded['derived_documents'])
    require(virtual not in inventory and virtual not in derived,
            'EDITOR3D_PREFAB_IDENTITY', 'Runtime document namespace conflicts with source.')
    derived[virtual] = runtime_scene
    require(len(inventory) + len(derived) <= max_files, 'EDITOR3D_PREFAB_LIMIT',
            'Derived prefab documents exceed the interactive file budget.')
    derived_inventory = {}
    for name, document in derived.items():
        portable_path(name)
        require(name not in inventory and name not in snapshot['documents'],
                'EDITOR3D_PREFAB_IDENTITY', 'Derived prefab document would replace a source.')
        payload = canonical_bytes(document)
        total += len(payload)
        require(len(payload) <= 16 * 1024 * 1024 and total <= max_bytes,
                'EDITOR3D_PREFAB_LIMIT', 'Derived prefab documents exceed the interactive byte budget.')
        derived_inventory[name] = dict(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
    context = dict(snapshot, scene=runtime_scene, scene_path=virtual,
                   documents={**snapshot['documents'], **derived},
                   inventory={**inventory, **derived_inventory})
    projection = dict(schema_version=1, revision=snapshot['revision'], sha256=snapshot['sha256'],
        authored_scene_path=scene_path, authored_scene_sha256=inventory[scene_path]['sha256'],
        scene=runtime_scene, mappings=deepcopy(expanded['mappings']),
        original_prefab_references=deepcopy(expanded['original_prefab_references']),
        derived_documents=derived_inventory, readonly_nodes=sorted(edit_targets),
        edit_targets=edit_targets, base_pose_only=True, animation_evaluated=False)
    return dict(runtime_snapshot=context, projection=projection)


def project_prefab_state(prepared):
    """Reuse the canonical static graph evaluator for the derived runtime scene."""
    from editor3d_state import project_editor_state
    # Expansion has already verified exact source clips. Static base-pose
    # evaluation needs no second filesystem lookup for virtual derived clips.
    state = project_editor_state(prepared['runtime_snapshot'], None)
    for identifier, anchor in prepared['projection']['edit_targets'].items():
        state['transform_readonly'][identifier] = 'Prefab content is read-only; edit authored instance ' + anchor + '.'
    return state
