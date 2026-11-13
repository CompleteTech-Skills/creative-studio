"""Scene-specific lifecycle inspection and exact selective dependency receipts.

Persistence is delegated to EditorRepository. Receipts bind selected immutable
scene/shot/asset documents rather than the entire project catalog, so updating an
unused asset does not invalidate unrelated renders or geometry reviews.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from rig_errors import RigError
from scene3d_contracts import all_refs, load_project, portable_path


def _require(condition, message):
    if not condition:
        raise RigError('SCENE3D_LIFECYCLE', message, stage='scene3d-lifecycle')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def dependency_receipt(source_root, targets, *, purpose):
    """Bind actual transitive dependencies for explicitly selected documents.

    Targets are exact persisted paths, never mutable ``latest`` identities.
    Geometry review targets an asset; camera/final rendering targets the shot.
    Caller-supplied purpose describes the inspection, not an approval claim.
    """
    _require(isinstance(targets, list) and 0 < len(targets) <= 1024 and
             all(isinstance(p, str) for p in targets), 'Select 1–1024 exact document paths.')
    _require(len(targets) == len(set(targets)), 'Duplicate dependency targets.')
    _require(isinstance(purpose, str) and 1 <= len(purpose) <= 128, 'Expected bounded purpose label.')
    bundle = load_project(source_root)
    pending = sorted(targets)
    selected = {}
    while pending:
        path = pending.pop()
        portable_path(path)
        _require(path in bundle['inventory'], 'Target or dependency is absent from verified closure: '+path)
        if path in selected:
            continue
        selected[path] = deepcopy(bundle['inventory'][path])
        document = bundle['documents'].get(path)
        if path in targets:
            _require(document is not None, 'Select a document, not a raw payload, as dependency root.')
        if document is not None:
            for reference in all_refs(document):
                child = reference['path']
                _require(child in bundle['inventory'] and
                         bundle['inventory'][child]['sha256'] == reference['sha256'],
                         'Dependency hash differs from the verified closure.')
                pending.append(child)
    value = dict(schema_version=1, kind='scene3d-dependency-receipt', purpose=purpose,
                 targets=sorted(targets), files=dict(sorted(selected.items())))
    value['sha256'] = _digest(value)
    return value


def _validate_receipt(value):
    _require(isinstance(value, dict) and set(value) ==
             {'schema_version', 'kind', 'purpose', 'targets', 'files', 'sha256'}, 'Malformed dependency receipt.')
    _require(value['schema_version'] == 1 and type(value['schema_version']) is int and
             value['kind'] == 'scene3d-dependency-receipt', 'Unsupported dependency receipt.')
    _require(isinstance(value['purpose'], str) and 1 <= len(value['purpose']) <= 128,
             'Invalid dependency receipt purpose.')
    _require(isinstance(value['targets'], list) and 0 < len(value['targets']) <= 1024 and
             all(isinstance(p, str) for p in value['targets']) and
             value['targets'] == sorted(set(value['targets'])), 'Invalid receipt roots.')
    _require(isinstance(value['files'], dict) and 0 < len(value['files']) <= 10000,
             'Invalid dependency receipt file budget.')
    for path, record in value['files'].items():
        portable_path(path)
        _require(isinstance(record, dict) and set(record) == {'bytes', 'sha256'} and
                 type(record['bytes']) is int and 0 <= record['bytes'] <= 64*1024**2 and
                 isinstance(record['sha256'], str) and re.fullmatch('[0-9a-f]{64}', record['sha256']),
                 'Invalid dependency receipt record.')
    _require(all(p in value['files'] for p in value['targets']), 'Receipt target is not retained.')
    _require(value['sha256'] == _digest({k: v for k, v in value.items() if k != 'sha256'}),
             'Dependency receipt digest differs.')
    return value


def compare_dependencies(previous, current):
    """Report exact stale inputs while preserving evidence for old revisions."""
    previous, current = _validate_receipt(previous), _validate_receipt(current)
    _require(previous['purpose'] == current['purpose'], 'Compare receipts for the same inspection purpose.')
    before, after = previous['files'], current['files']
    added = sorted(set(after)-set(before))
    removed = sorted(set(before)-set(after))
    changed = sorted(p for p in set(before)&set(after) if before[p] != after[p])
    roots_changed = previous['targets'] != current['targets']
    return dict(stale=bool(added or removed or changed or roots_changed), added=added,
                removed=removed, changed=changed, targets_changed=roots_changed,
                previous_sha256=previous['sha256'], current_sha256=current['sha256'])


def open_repository(root):
    """Use the authoritative durable repository owner; never emulate its state."""
    try:
        from editor3d_repository import EditorRepository
    except ImportError as exc:
        raise RigError('SCENE3D_LIFECYCLE_DEPENDENCY',
                       'Install the Editor3D durable repository owner.', stage='scene3d-lifecycle') from exc
    return EditorRepository(root)


def inspect_revision(repository_root, revision=None):
    """Reopen verified native bytes and report actual base-pose transforms."""
    from scene3d_graph import SceneGraph
    repository = open_repository(repository_root)
    snapshot = repository.snapshot(revision)
    native = repository.native_snapshot(snapshot['revision'], snapshot['sha256'])
    bundle = load_project(native['root'])
    graph = SceneGraph.from_bundle(bundle, snapshot['scene_path'], source_root=native['root'])
    if any('prefab' in node for node in graph.document()['nodes']):
        from scene3d_prefabs import PrefabLibrary
        expanded = PrefabLibrary.from_files(native['root'], bundle).expand(graph.document())
        graph.replace_document(expanded['scene'], expected_revision=graph.revision, derived_documents=expanded['derived_documents'])
    evaluated = graph.evaluate_base()
    scene = snapshot['scene']
    return dict(schema_version=1, kind='scene3d-revision-inspection',
                revision=snapshot['revision'], snapshot_sha256=snapshot['sha256'],
                project=dict(id=bundle['project']['id'], version=bundle['project']['version']),
                scene=dict(id=scene['id'], version=scene['version'], path=snapshot['scene_path']),
                evaluation='authored-base-pose', nodes=deepcopy(evaluated),
                dependency_receipt=dependency_receipt(native['root'], [snapshot['scene_path']],
                                                     purpose='scene-base-inspection'),
                recovery=repository.recovery(), undo_available=snapshot['undo_available'],
                redo_available=snapshot['redo_available'], production_approved=False)
