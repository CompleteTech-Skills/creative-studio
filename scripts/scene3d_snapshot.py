"""Materialize complete Scene3D dependency snapshots to a new owned destination.

This is a portable immutable snapshot, not a replacement for editor autosave or
project revision storage. Existing scene/shot versions are retained exactly;
edited versions receive new identities before any project is published.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import zipfile

from asset_library import read_source_bytes
from atomic_publish import _rename_no_replace
from rig_errors import timestamp, decode_json
from scene3d_contracts import (all_refs, canonical_bytes, load_project,
                               portable_path, validate_document)
from scene3d_graph import require


def version_patch(version):
    major, minor, patch = map(int, version.split('.'))
    return f'{major}.{minor}.{patch+1}'


def _file_reference(document, payload, path):
    return dict(kind=document['kind'], id=document['id'], version=document['version'],
                path=path, sha256=hashlib.sha256(payload).hexdigest())


def _new_destination(destination):
    destination = Path(destination).absolute()
    require(not destination.is_symlink() and not destination.exists(), 'Choose a new snapshot destination.',
            'SCENE3D_DESTINATION')
    require(destination.parent.is_dir(), 'Snapshot parent must already be a trusted directory.', 'SCENE3D_DESTINATION')
    # Canonicalize parents only. Never follow the requested destination leaf.
    return destination.parent.resolve(strict=True) / destination.name


def snapshot_project(source_root, destination, graph=None, *, scene_path=None,
                     scene_version=None, project_version=None, derived_documents=None,
                     extra_payloads=None, max_bytes=1024**3):
    """Copy and revalidate actual source bytes, then exclusively publish a closure.

    New scene versions coexist with original scene/shot versions. The return value
    selects the new scene explicitly; no hidden mutable ``latest`` binding exists.
    Additional derived documents must already reference complete input payloads.
    """
    require(type(max_bytes) is int and 0 < max_bytes <= 1024**3, 'Invalid snapshot byte budget.')
    destination = _new_destination(destination)
    bundle = load_project(source_root)
    source_root = Path(source_root).resolve(strict=True)
    files, total = {}, 0
    for path, record in bundle['inventory'].items():
        require(0 < record['bytes'] <= 64*1024*1024, 'Snapshot file exceeds the safe-reader profile.')
        payload = read_source_bytes(source_root/portable_path(path), record['bytes'])
        require(hashlib.sha256(payload).hexdigest() == record['sha256'] and len(payload) == record['bytes'],
                'Source changed after closure validation.', 'SCENE3D_SOURCE_CHANGED')
        total += len(payload)
        require(total <= max_bytes, 'Snapshot byte budget exceeded.', 'SCENE3D_SNAPSHOT_LIMIT')
        files[path] = payload
    legacy = bundle['project']['linked_2d_project']
    legacy_prefix = None
    if legacy is not None:
        relative = Path(portable_path(legacy['path']))
        require(relative.name == 'project.json' and relative.parent != Path('.'),
                'Hybrid snapshots require a separate canonical legacy project directory.', 'SCENE3D_LEGACY_LAYOUT')
        legacy_prefix = relative.parent.as_posix()
        # Delegate full acquisition/review/history and file-inventory ownership to
        # the existing archive implementation, not a new partial 2D path walker.
        from production_workflow import archive_project, verify_project_archive
        try:
            with tempfile.TemporaryDirectory(prefix='scene3d-legacy-') as temporary:
                archive_path = Path(temporary)/'project.zip'
                archive_project(source_root/relative.parent, archive_path)
                receipt = verify_project_archive(archive_path)
                with zipfile.ZipFile(archive_path) as archive:
                    for native_path, expected in receipt['files'].items():
                        path = legacy_prefix+'/'+portable_path(native_path)
                        info = archive.getinfo(native_path)
                        require(info.file_size <= 64*1024*1024, 'Legacy file exceeds snapshot profile.')
                        payload = archive.read(native_path)
                        require(hashlib.sha256(payload).hexdigest() == expected,
                                'Legacy archive content failed exact-byte verification.', 'SCENE3D_LEGACY_SOURCE')
                        require(path not in files or files[path] == payload, 'Legacy/project path collision.')
                        files[path] = payload
                        require(sum(map(len, files.values())) <= max_bytes, 'Legacy closure exceeds snapshot byte budget.')
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            from rig_errors import RigError
            if isinstance(exc, RigError):
                raise
            raise RigError('SCENE3D_LEGACY_SOURCE', 'Native legacy archival validation failed: '+str(exc),
                           stage='scene3d-snapshot') from exc
    documents = deepcopy(bundle['documents'])
    project = deepcopy(bundle['project'])
    active_scene = None
    additions = []
    def add_document(document, path=None):
        if isinstance(document, bytes):
            require(0 < len(document) <= 16*1024*1024, 'Manifest bytes exceed the document bound.')
            payload = document
            document = validate_document(decode_json(payload))
        else:
            payload = canonical_bytes(document)
        path = path or 'versions/'+document['id']+'/'+document['version']+'.json'
        portable_path(path)
        require(path not in files, 'Immutable snapshot path already exists.', 'SCENE3D_IMMUTABLE')
        identity = (document['kind'], document['id'], document['version'])
        require(all((d['kind'], d['id'], d['version']) != identity for d in documents.values()),
                'Immutable identity/version already exists.', 'SCENE3D_IMMUTABLE')
        files[path], documents[path] = payload, deepcopy(document)
        r = _file_reference(document, payload, path)
        additions.append(r)
        field = {'scene3d-asset': 'assets', 'scene3d-prefab': 'prefabs',
                 'scene3d-animation': 'animations'}.get(document['kind'])
        if field is not None:
            project[field].append(deepcopy(r))
        if document['kind'] == 'scene3d-asset' and document['asset_type'] == 'character':
            project['characters'].append(deepcopy(r))
        return r
    for path, payload in (extra_payloads or {}).items():
        portable_path(path)
        require(isinstance(payload, bytes) and 0 < len(payload) <= 64*1024*1024,
                'Additional payload must be nonempty bounded immutable bytes.')
        require(path not in files or files[path] == payload, 'Additional payload conflicts with original bytes.',
                'SCENE3D_IMMUTABLE')
        files[path] = payload
    combined_derived = graph.derived_documents() if graph is not None else {}
    for path, document in (derived_documents or {}).items():
        if path in combined_derived:
            prior = combined_derived[path]
            prior_payload = prior if isinstance(prior, bytes) else canonical_bytes(prior)
            payload = document if isinstance(document, bytes) else canonical_bytes(document)
            require(prior_payload == payload, 'Conflicting derived document.')
        combined_derived[path] = document
    for path, document in combined_derived.items():
        add_document(document, path)
    if graph is not None:
        require(scene_path in bundle['documents'] and bundle['documents'][scene_path]['kind'] == 'scene3d-scene',
                'Select the exact original scene for this edit.')
        original = bundle['documents'][scene_path]
        replacement = graph.document()
        require(replacement['id'] == original['id'] and replacement['version'] == original['version'],
                'Edited working document must retain its original identity/version until snapshot promotion.')
        replacement['version'] = scene_version or version_patch(original['version'])
        require(replacement['version'] != original['version'], 'Edited scene needs a new immutable version.',
                'SCENE3D_IMMUTABLE')
        replacement['provenance']['origin'] = 'derived'
        replacement['provenance']['tool'] = 'scene3d-edit-snapshot-v1'
        seen, pending = set(), [replacement]
        while pending:
            current = pending.pop()
            if current.get('provenance', {}).get('fixture'):
                replacement['provenance']['fixture'] = True
            for reference in all_refs(current):
                path = reference['path']
                if path in documents and path not in seen:
                    seen.add(path)
                    pending.append(documents[path])
        if replacement['provenance']['fixture']:
            project['provenance']['fixture'] = True
            project['status'] = 'fixture'
        replacement['provenance']['inputs'].append(dict(path=scene_path,
                                                     sha256=bundle['inventory'][scene_path]['sha256']))
        active_scene = add_document(replacement)
        project['scenes'].append(active_scene)
        # Existing shots remain valid against the retained old scene; new exact
        # shot versions bind the edited scene without overwriting their originals.
        for reference in list(project['shots']):
            old_shot = bundle['documents'][reference['path']]
            if old_shot['scene']['path'] != scene_path:
                continue
            shot = deepcopy(old_shot)
            shot['version'] = version_patch(shot['version'])
            shot['scene'] = deepcopy(active_scene)
            project['shots'].append(add_document(shot))
    original_project = files['project.json']
    history = 'history/project-'+hashlib.sha256(original_project).hexdigest()+'.json'
    files[history] = original_project
    project['version'] = project_version or version_patch(project['version'])
    project['modified_at'] = timestamp()
    require(project['version'] != bundle['project']['version'], 'Snapshot needs a new project version.', 'SCENE3D_IMMUTABLE')
    project['provenance']['origin'] = 'derived'
    project['provenance']['tool'] = 'scene3d-complete-dependency-snapshot-v1'
    project['provenance']['inputs'].append(dict(path=history, sha256=hashlib.sha256(original_project).hexdigest()))
    project['dependency_lock'].extend(additions)
    # Lock any exact references added by expanded prefabs or imported assets.
    locked = {(r['kind'], r['id'], r['version']): r for r in project['dependency_lock']}
    for document in [project, *documents.values()]:
        for r in all_refs(document):
            if 'kind' not in r:
                continue
            key = (r['kind'], r['id'], r['version'])
            require(key not in locked or locked[key] == r, 'Conflicting dependency version in snapshot.',
                    'SCENE3D_IMMUTABLE')
            locked[key] = deepcopy(r)
    project['dependency_lock'] = sorted(locked.values(), key=lambda r: (r['kind'], r['id'], r['version']))
    validate_document(project, 'project')
    files['project.json'] = canonical_bytes(project)
    total = sum(map(len, files.values()))
    require(total <= min(max_bytes, project['budgets']['max_bytes']) and len(files) <= project['budgets']['max_files'],
            'Snapshot exceeds project file/byte budgets.', 'SCENE3D_SNAPSHOT_LIMIT')
    # All writes are inside an attempt-owned private directory. The destination
    # leaf is never reserved or replaced and is rechecked by kernel publication.
    with tempfile.TemporaryDirectory(prefix='.scene3d-snapshot-', dir=destination.parent) as private:
        stage = Path(private)/'project'
        stage.mkdir(mode=0o700)
        for path, payload in sorted(files.items()):
            target = stage / portable_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        verified = load_project(stage)
        if legacy_prefix is not None:
            from production_contracts import load_project as load_legacy_project
            try:
                load_legacy_project(stage/legacy_prefix)
            except ValueError as exc:
                from rig_errors import RigError
                raise RigError('SCENE3D_LEGACY_SOURCE', 'Restored native legacy closure is invalid: '+str(exc)) from exc
        require(set(verified['inventory']) <= set(files), 'Verification opened an uninventoried payload.')
        _rename_no_replace(stage, destination)
    return dict(destination=str(destination), project_sha256=hashlib.sha256(files['project.json']).hexdigest(),
                project_version=project['version'], active_scene=active_scene,
                inventory={path: dict(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                           for path, payload in sorted(files.items())},
                status='complete-byte-and-contract-verified', production_approved=False)
