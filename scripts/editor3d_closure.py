"""Exact native closure for editor bindings, delegated to native resource owners.

A temporary, hash-checked snapshot is the execution boundary. This module never
publishes files or certifies rendered output or visual approval. Native references
are scoped by source_projects; paths remain portable across project relocation.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
import hashlib
import tempfile

from editor3d_adapters import verify_native_reference
from editor3d_contracts import validate_project_bundle, contract_hash, _error
from rig_errors import RigError, decode_json, portable_name

MAX_FILES = 4096
MAX_BYTES = 256 * 1024**2
MAX_FILE_BYTES = 64 * 1024**2
MAX_DEPTH = 32


def _references(value):
    if isinstance(value, dict):
        if set(value) == {'owner', 'id', 'version', 'native_kind', 'file'}:
            yield value
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _document_files(value):
    """Ask each native schema owner which fields are dependency references."""
    if not isinstance(value, dict):
        return []
    kind = value.get('kind', '')
    if isinstance(kind, str) and kind.startswith('scene3d-'):
        from scene3d_contracts import validate_document, all_refs
        validate_document(value)
        return list(all_refs(value))
    if isinstance(kind, str) and kind.startswith('camera3d-'):
        from camera3d_contracts import validate, _walk
        validate(value)
        return [item for item in _walk(value) if isinstance(item, dict) and
                set(item) == {'id', 'version', 'path', 'sha256', 'kind'}]
    from lookdev_contracts import KINDS as lookdev_kinds
    if kind in lookdev_kinds:
        from lookdev_contracts import validate_resource, _references
        validate_resource(value)
        result = list(_references(value))
        if kind == 'texture3d':
            result.append(value['file'])
        return result
    if isinstance(kind, str) and kind.startswith('model3d-'):
        from model3d_contracts import validate_contract, file_references
        validate_contract(value)
        return list(file_references(value))
    if kind == 'hatch-3d-rig':
        from rig_contracts import validate_contract
        validate_contract(value, 'Rig')
        return [*value['source']['dependencies'], value['geometry']['data'],
                *[item['data'] for item in value['skins']], *value['animations'],
                *value['review']['receipts'], *value['validation']]
    if kind == 'hatch-3d-rig-review':
        from rig_contracts import validate_contract
        validate_contract(value, 'RigReviewReceipt')
        return list(value['evidence'])
    if isinstance(value.get('artifacts'), list) and 'rig_sha256' in value:
        from rig_contracts import validate_contract
        validate_contract(value, 'RigValidationReport')
        return list(value['artifacts'])
    if kind in ('project', 'cast', 'clip', 'scene', 'shot'):
        from production_contracts import validate_structure, _walk_refs
        validate_structure(value, kind)
        return [record for record, _ in _walk_refs(value)]
    return []


def _manifest_references(value):
    """Identify child documents by the native contract, never filename suffix."""
    result = [item for item in _document_files(value) if isinstance(item.get('kind'), str)]
    if not isinstance(value, dict):
        return result
    kind = value.get('kind')
    if isinstance(kind, str) and kind.startswith('model3d-'):
        result.extend(item['manifest'] for item in value.get('dependencies', []) if 'manifest' in item)
        for field in ('asset', 'before', 'after'):
            item = value.get(field)
            if isinstance(item, dict) and 'manifest' in item:
                result.append(item['manifest'])
        if kind in ('model3d-job', 'model3d-provider-receipt'):
            result.append(value['request'])
    elif kind == 'hatch-3d-rig':
        result.extend(value['animations'])
        result.extend(value['review']['receipts'])
        result.extend(value['validation'])
    elif kind == 'project':
        result.append(value['cast'])
        result.extend(value['clips'])
        result.extend(value['scenes'])
    return result


def _joined(prefix, relative):
    portable_name(relative)
    result = (PurePosixPath(prefix) / relative).as_posix() if prefix else relative
    portable_name(result)
    return result


class _Closure:
    def __init__(self, root, destination):
        from model3d_validation import _Snapshot
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            _error('ROOT', 'Choose a regular native project directory.')
        self.snapshot = _Snapshot(self.root, destination, dict(files=MAX_FILES,
            total_bytes=MAX_BYTES, file_bytes=MAX_FILE_BYTES, memory_bytes=MAX_BYTES))
        self.values = {}
        self.active = set()
        self.expanded = set()

    def capture(self, record, prefix='', depth=0, *, manifest=False):
        if depth > MAX_DEPTH:
            _error('DEPTH', 'Native dependency closure exceeds 32 levels.')
        name = _joined(prefix, record['path'])
        if name in self.active:
            _error('CYCLE', 'Native dependency closure contains a cycle: ' + name)
        from model3d_validation import _open_payload
        size = record.get('bytes')
        if size is None:
            with _open_payload(self.root, name) as (_, info):
                size = info.st_size
        bound = dict(path=name, sha256=record['sha256'], bytes=size,
                     media_type='application/octet-stream')
        target = self.snapshot.capture(bound)
        if not manifest:
            return None
        if name not in self.values:
            if size > 16 * 1024**2:
                _error('LIMIT', 'Native JSON exceeds 16 MiB.')
            self.values[name] = decode_json(target.read_bytes())
        value = self.values[name]
        key = name, prefix
        if key not in self.expanded:
            self.active.add(name)
            documents = _manifest_references(value)
            for child in _document_files(value):
                self.capture(child, prefix, depth + 1, manifest=child in documents)
            self.active.remove(name)
            self.expanded.add(key)
        return value

    def recheck(self):
        # Re-read the original exact closure after all native work. A mutation
        # cannot turn a mixed snapshot into a successful current-source report.
        from model3d_validation import _open_payload
        for name, record in self.snapshot.files.items():
            with _open_payload(self.root, name) as (source, info):
                if info.st_size != record['bytes']:
                    _error('CHANGED', 'Source size changed during verification: ' + name)
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024**2), b''):
                    digest.update(chunk)
                if digest.hexdigest() != record['sha256']:
                    _error('CHANGED', 'Source bytes changed during verification: ' + name)


def _scope(reference, sources):
    owner = reference['owner']
    path = reference['file']['path']
    candidates = [source for source in sources if source['owner'] == owner and
        source['native_kind'] in {'scene3d-project', 'camera3d-registry', 'project', 'Rig'}]
    if owner in ('scene3d', 'camera3d', 'production2d') or owner == 'rig' and candidates:
        if len(candidates) != 1:
            _error('SOURCE_PROJECT', 'Require one exact native source project/registry for ' + owner)
        source = candidates[0]
        prefix = str(PurePosixPath(source['file']['path']).parent)
        prefix = '' if prefix == '.' else prefix
        if prefix and not path.startswith(prefix + '/'):
            _error('SOURCE_PROJECT', 'Native reference is outside its source project.')
        if owner == 'rig' and reference != source:
            _error('UNAVAILABLE', 'A Rig closure requires the exact bound Rig source manifest.')
        return prefix, source
    prefix = str(PurePosixPath(path).parent)
    if owner == 'model3d':
        prefix = ''  # Model3D portable paths use the explicitly supplied root.
    return ('' if prefix == '.' else prefix), reference


def _verify_editor_bundle(project, scenes, nodes, assets, animations, root):
    """Verify editor closure plus native bytes/semantics and node identities.

    Scene3D/Camera3D/Production2D references require the corresponding exact
    project/registry in project.source_projects. Model3D paths are relative to
    root; other native paths are relative to their source project directory.
    Unsupported owners or incomplete dependencies fail, never become ready.
    """
    validate_project_bundle(project, scenes, nodes, assets, animations)
    references = list(_references([project, scenes, nodes, assets, animations]))
    unique = {}
    for reference in references:
        key = reference['owner'], reference['id'], reference['version'], reference['native_kind']
        if key in unique and unique[key] != reference:
            _error('IDENTITY', 'A native immutable identity has conflicting references.')
        unique[key] = reference
    locked = project['dependency_manifest']
    for reference in _references([scenes, nodes, assets, animations, project['source_projects'],
                                  project['shots'], project['characters'], project['render_presets'],
                                  project['color_management']]):
        if reference not in locked:
            _error('LOCK', 'The editor dependency manifest must pin every native binding.')
    reports, native_documents, contexts = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix='hatch-editor-native-') as temporary:
        snapshot = _Closure(root, temporary)
        for key, reference in unique.items():
            prefix, source = _scope(reference, project['source_projects'])
            # The exact source project is captured first, making its dependency
            # graph authoritative rather than manufacturing one from bindings.
            source_record = dict(source['file'])
            if prefix:
                source_record['path'] = PurePosixPath(source_record['path']).relative_to(prefix).as_posix()
            snapshot.capture(source_record, prefix, manifest=True)
            value = snapshot.capture(reference['file'], '', manifest=True) if not prefix else snapshot.capture(
                dict(reference['file'], path=PurePosixPath(reference['file']['path']).relative_to(prefix).as_posix()), prefix, manifest=True)
            verify_native_reference(reference, temporary)
            if reference['owner'] == 'rig' and reference['native_kind'] == 'Rig':
                snapshot.capture(dict(path='identity.json', sha256=value['character']['identity_sha256']), prefix, manifest=True)
            native_documents[key] = value
            contexts[(reference['owner'], source['file']['path'])] = prefix, source
        for (owner, source_path), (prefix, source) in contexts.items():
            native_root = Path(temporary) / prefix
            value = snapshot.values[source_path]
            if owner == 'model3d':
                from model3d_validation import validate_document
                report = validate_document(value, temporary)
            elif owner == 'scene3d':
                from scene3d_contracts import load_project
                report = load_project(native_root, PurePosixPath(source_path).name)
                declared = {str(PurePosixPath(prefix) / name) if prefix else name for name in report['inventory']}
                for reference in unique.values():
                    if reference['owner'] == owner and reference['file']['path'] != source_path and reference['file']['path'] not in declared:
                        _error('REFERENCE', 'Editor reference is outside the native project closure.')
                report = dict(status=report['status'], files=len(report['inventory']), bytes=report['bytes'])
            elif owner == 'camera3d':
                from camera3d_contracts import load_registry
                registry = load_registry(native_root, value)
                for reference in unique.values():
                    if reference['owner'] != owner or reference['file']['path'] == source_path:
                        continue
                    relative = PurePosixPath(reference['file']['path']).relative_to(prefix).as_posix() if prefix else reference['file']['path']
                    expected = dict(path=relative, sha256=reference['file']['sha256'],
                        id=reference['id'], version=reference['version'], kind=reference['native_kind'])
                    if expected not in value['resources']:
                        _error('REFERENCE', 'Editor camera reference is absent from the exact native registry.')
                report = dict(status='verified-native-registry', resources=len(registry))
            elif owner == 'lookdev':
                from lookdev_contracts import Registry
                entries, payloads = [], {}
                pending, visited = [source_path], set()
                while pending:
                    name = pending.pop()
                    if name in visited:
                        continue
                    visited.add(name)
                    candidate = snapshot.values[name]
                    relative = PurePosixPath(name).relative_to(prefix).as_posix() if prefix else name
                    entries.append((relative, (Path(temporary) / name).read_bytes()))
                    for dependency in candidate['dependencies']:
                        pending.append(_joined(prefix, dependency['path']))
                    if candidate['kind'] == 'texture3d':
                        texture = candidate['file']
                        payloads[texture['path']] = (Path(temporary) / _joined(prefix, texture['path'])).read_bytes()
                registry = Registry(entries, payloads=payloads)
                report = dict(status='verified-native-registry', resources=len(entries))
            elif owner == 'rig':
                if source['native_kind'] != 'Rig':
                    _error('UNAVAILABLE', 'Full rig closure requires a native Rig reference.')
                from rig_models import load_rig
                from rig_validation import validate_rig
                rig = load_rig(native_root)
                report = validate_rig(rig)
                if not report['ok']:
                    _error('NATIVE', 'Native rig fails numerical validation.')
            elif owner == 'production2d':
                from production_contracts import load_project
                bundle = load_project(native_root / PurePosixPath(source_path).name)
                for reference in unique.values():
                    if reference['owner'] != owner or reference['file']['path'] == source_path:
                        continue
                    relative = PurePosixPath(reference['file']['path']).relative_to(prefix).as_posix() if prefix else reference['file']['path']
                    if reference['native_kind'] == 'shot':
                        key = owner, reference['id'], reference['version'], reference['native_kind']
                        if bundle['shots'].get(reference['id']) != native_documents[key]:
                            _error('REFERENCE', 'Editor shot differs from its native production project.')
                    elif relative not in bundle['inventory']:
                        _error('REFERENCE', 'Editor reference is absent from the native production project.')
                report = dict(status='verified-native-project', resources=bundle['resources'])
            else:
                _error('UNAVAILABLE', 'Native owner does not expose a verified closure: ' + owner)
            reports[owner + ':' + source_path] = report
        for node in nodes:
            reference = node['native']
            key = reference['owner'], reference['id'], reference['version'], reference['native_kind']
            document = native_documents[key]
            if reference['owner'] != 'scene3d':
                _error('UNAVAILABLE', 'This native node owner has no semantic editor binding adapter: ' + reference['owner'])
            if reference['owner'] == 'scene3d':
                scene = next(item for item in scenes if item['id'] == node['scene_id'])
                if reference != scene['native']:
                    _error('NODE', 'Node and scene must bind the same native scene document.')
                native_nodes = {item['id']: item for item in document.get('nodes', [])}
                if node['native_node_id'] not in native_nodes:
                    _error('NODE', 'Editor node is absent from the exact native scene.')
                native = native_nodes[node['native_node_id']]
                types = {'root': 'group', 'group': 'group', 'mesh_instance': 'mesh',
                         'static_prop': 'mesh', 'animated_prop': 'mesh', 'terrain': 'mesh',
                         'procedural_geometry': 'primitive', 'skinned_character': 'character',
                         'camera': 'camera', 'prefab_instance': 'instance', 'socket': 'group',
                         'constraint_target': 'group', 'locator': 'group'}
                expected = (native['light']['type'] + '-light' if native['type'] == 'light'
                            else types.get(native['type']))
                if expected is None:
                    _error('UNAVAILABLE', 'This native node kind has no editor binding: ' + native['type'])
                if node['node_type'] != expected:
                    _error('NODE', 'Editor node kind differs from native scene node kind.')
                parent = next((item['native_node_id'] for item in nodes if item['id'] == node['parent']), None)
                if parent != native['parent']:
                    _error('NODE', 'Editor hierarchy differs from the native scene.')
        snapshot.recheck()
        return dict(status='verified-native-closure', project_sha256=contract_hash(project),
            native_resources_verified=True, geometry_inspected_owners=sorted(
                {owner for owner, _ in contexts if owner in ('model3d', 'rig')}),
            files=deepcopy(snapshot.snapshot.files),
            bytes=snapshot.snapshot.total, native_reports=reports,
            review_status='unreviewed', rendered_output_verified=False)


def verify_editor_bundle(project, scenes, nodes, assets, animations, root):
    """Return current exact native closure, or a structured admission diagnostic."""
    try:
        return _verify_editor_bundle(project, scenes, nodes, assets, animations, root)
    except RigError:
        raise
    except ImportError as exc:
        raise RigError('EDITOR3D_DEPENDENCY', 'Install the native owner requirements: ' + str(exc), stage='editor3d') from exc
    except (OSError, ValueError) as exc:
        raise RigError('EDITOR3D_NATIVE', 'Native closure verification failed: ' + str(exc), stage='editor3d') from exc
