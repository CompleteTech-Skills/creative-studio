#!/usr/bin/env python3
"""Inspect and export exact LookDev projects; adjacent to production.py.

Project indices enumerate portable resource and payload paths explicitly. They
never recursively discover files or execute imported code. Native export retains
source bytes and is not a claim of GLB, rendering or aesthetic acceptance.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat

from atomic_publish import rename_no_replace_at, private_stage_fd, private_stage_fd_supported
from lookdev_contracts import Registry, canonical_bytes, content_hash, portable_path, validate_resource
from rig_errors import RigError, decode_json, require

INDEX = 'lookdev-project.json'
MATERIALS = frozenset(('material3d', 'material-instance3d', 'material-variant3d'))
GROUPS = {'materials': MATERIALS, 'textures': frozenset(('texture3d',)),
          'lighting': frozenset(('light3d', 'lighting-rig3d'))}


def _directory_fd(path):
    """Open an absolute directory without following any path component links."""
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_at(root_fd, name, payload):
    portable_path(name)
    descriptor = os.dup(root_fd)
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=descriptor)
        with os.fdopen(file_fd, 'wb') as stream:
            stream.write(payload)
    finally:
        os.close(descriptor)


def _read_at(root_fd, name, maximum):
    """Capture one bounded regular file using anchored no-follow traversal."""
    portable_path(name)
    descriptor = os.dup(root_fd)
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file_fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum,
                    'LOOKDEV_PROJECT_LIMIT', 'Expected bounded regular input: ' + name)
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            require(len(payload) <= maximum and (before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                    'LOOKDEV_PROJECT_CHANGED', 'Input changed during capture: ' + name)
            return payload
    finally:
        os.close(descriptor)


class CapturedClosure:
    """Verified native resource closure; no unrelated project files included."""
    def __init__(self, resources, payloads, root_references, aliases):
        self.sources = dict(resources, **payloads)
        self.registry = Registry(list(resources.items()), payloads=payloads)
        self.all_references = sorted((self.registry.reference(value['id'], value['version'])
                for value in (decode_json(raw) for raw in resources.values())),
                key=lambda r: (r['kind'], r['id'], r['version']))
        self.references = deepcopy(root_references)
        self._aliases = deepcopy(aliases)
        self.inventory = {p: dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                          for p, raw in sorted(self.sources.items())}
        self.sha256 = content_hash(self.inventory)

    def canonical_reference(self, source_reference):
        key = canonical_bytes(source_reference)
        if key in self._aliases:
            return deepcopy(self._aliases[key])
        self.registry.get(source_reference)
        return deepcopy(source_reference)


def _capture_closure(reader, references):
    require(isinstance(references, (list, tuple)) and 0 < len(references) <= 1024,
            'LOOKDEV_CLOSURE_REFERENCE', 'Supply one to 1024 exact root references.')
    resources, payloads, aliases, roots = {}, {}, {}, []
    total_resources = total_payloads = 0
    path_aliases = {}

    def claim(path):
        portable_path(path)
        folded = path.casefold()
        require(folded not in path_aliases or path_aliases[folded] == path,
                'LOOKDEV_CLOSURE_ALIAS', 'Case-aliased source paths are rejected.')
        path_aliases[folded] = path

    def visit(reference, depth=0, root=False):
        nonlocal total_resources, total_payloads
        require(depth <= 64, 'LOOKDEV_CLOSURE_DEPTH', 'Dependency closure exceeds 64 levels.')
        require(isinstance(reference, dict) and set(reference) == {'id','version','kind','path','sha256'},
                'LOOKDEV_CLOSURE_REFERENCE', 'Supply a complete exact resource reference.')
        path = reference['path'];claim(path)
        require(path not in payloads, 'LOOKDEV_CLOSURE_ALIAS', 'Resource and payload paths overlap.')
        if path not in resources:
            require(len(resources) < 1024, 'LOOKDEV_CLOSURE_LIMIT', 'More than 1024 source documents.')
            raw = reader(path, min(2 * 1024**2, 32 * 1024**2 - total_resources))
            total_resources += len(raw)
            resources[path] = raw
        raw = resources[path]
        value = validate_resource(decode_json(raw))
        canonical = dict(id=value['id'], version=value['version'], kind=value['kind'], path=path,
                         sha256=hashlib.sha256(raw).hexdigest())
        expected = dict(reference)
        # Scene3D's existing material alias is admitted only at an explicitly
        # supplied root; transitive LookDev dependencies remain canonical.
        if root and expected['kind'] == 'material':
            require(value['kind'] in MATERIALS, 'LOOKDEV_CLOSURE_TYPE', 'Scene material must resolve to a native material.')
            expected['kind'] = value['kind']
        require(expected == canonical, 'LOOKDEV_CLOSURE_REFERENCE', 'Exact source reference mismatch: ' + path)
        key = canonical_bytes(reference)
        if key in aliases:
            return canonical
        aliases[key] = canonical
        for dependency in value['dependencies']:
            visit(dependency, depth+1)
        if value['kind'] == 'texture3d':
            source = value['file'];name=source['path'];claim(name)
            require(name not in resources, 'LOOKDEV_CLOSURE_ALIAS', 'Payload and resource paths overlap.')
            if name not in payloads:
                require(len(payloads) < 1024, 'LOOKDEV_CLOSURE_LIMIT', 'More than 1024 payloads.')
                raw_payload = reader(name, min(64 * 1024**2 - total_payloads, source['bytes']))
                total_payloads += len(raw_payload)
                payloads[name] = raw_payload
            require(len(payloads[name]) == source['bytes'] and hashlib.sha256(payloads[name]).hexdigest() == source['sha256'],
                    'LOOKDEV_CLOSURE_PAYLOAD', 'Exact texture payload mismatch: ' + name)
        return canonical

    for reference in references:
        roots.append(visit(reference, root=True))
    return CapturedClosure(resources, payloads, roots, aliases)


def capture_closure(reader, references):
    """Portable closure capture through an owner's already validated reader.

    reader(path, maximum_bytes) must return original immutable bytes, enforcing
    the owner's filesystem boundary. Size/type bounds are independently checked.
    """
    require(callable(reader), 'LOOKDEV_CLOSURE_READER', 'Expected a validated bounded source reader.')
    def bounded(path, maximum):
        payload = reader(path, maximum)
        require(isinstance(payload, bytes) and len(payload) <= maximum,
                'LOOKDEV_CLOSURE_LIMIT', 'Reader returned nonbytes or oversized source: ' + path)
        return payload
    return _capture_closure(bounded, references)


def load_closure(root, references):
    """Capture transitive exact references without requiring a project index.

    Scene callers may pass a root with kind=material. The returned canonical
    reference uses the actual material3d/instance/variant kind. Every dependency
    and texture payload is retained as original bytes for snapshot publication.
    """
    descriptor = _directory_fd(root)
    try:
        return capture_closure(lambda path, maximum: _read_at(descriptor, path, maximum), references)
    finally:
        os.close(descriptor)


class Project:
    """A bounded captured source closure; later operations use captured bytes."""
    def __init__(self, root):
        self.root = Path(root).absolute()
        descriptor = _directory_fd(self.root)
        try:
            raw_index = _read_at(descriptor, INDEX, 128 * 1024)
            index = decode_json(raw_index)
            require(isinstance(index, dict) and set(index) == {'schema_version', 'resources', 'payloads'} and
                    type(index['schema_version']) is int and index['schema_version'] == 1,
                    'LOOKDEV_PROJECT_INDEX', 'Expected version 1 resource/payload project index.')
            resources, payloads = index['resources'], index['payloads']
            for paths, maximum in ((resources, 1024), (payloads, 1024)):
                require(isinstance(paths, list) and len(paths) <= maximum and all(isinstance(p, str) for p in paths),
                        'LOOKDEV_PROJECT_LIMIT', 'Project paths must be bounded string arrays.')
            paths = resources + payloads
            require(len({p.casefold() for p in paths + [INDEX, 'export-manifest.json']}) == len(paths) + 2,
                    'LOOKDEV_PROJECT_DUPLICATE', 'Project paths overlap or alias.')
            self.sources = {INDEX: raw_index}
            for names, maximum, total_limit in ((resources, 2 * 1024**2, 32 * 1024**2),
                                                (payloads, 64 * 1024**2, 64 * 1024**2)):
                total = 0
                for name in names:
                    payload = _read_at(descriptor, name, min(maximum, total_limit-total))
                    total += len(payload)
                    self.sources[name] = payload
            self.registry = Registry([(p, self.sources[p]) for p in resources],
                                     payloads={p: self.sources[p] for p in payloads})
            self.references = [self.registry.reference(v['id'], v['version'])
                               for v in (decode_json(self.sources[p]) for p in resources)]
            self.references.sort(key=lambda r: (r['kind'], r['id'], r['version']))
        finally:
            os.close(descriptor)
        self.inventory = {p: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                          for p, data in sorted(self.sources.items())}
        self.sha256 = content_hash(self.inventory)

    def capture_closure(self, reference):
        """Select one transitive source subset from already captured bytes."""
        def reader(path, maximum):
            require(path in self.sources and len(self.sources[path]) <= maximum,
                    'LOOKDEV_CLOSURE_SOURCE', 'Missing or oversized captured dependency: ' + path)
            return self.sources[path]
        return capture_closure(reader, [reference])

    def list(self, group):
        return [deepcopy(r) for r in self.references if r['kind'] in GROUPS[group]]

    def select(self, group, identifier, version):
        matches = [r for r in self.list(group) if r['id'] == identifier and (version is None or r['version'] == version)]
        require(len(matches) == 1, 'LOOKDEV_PROJECT_SELECTION',
                'Select one exact ID/version; missing and ambiguous selections are rejected.')
        return matches[0]

    def inspect(self, group, identifier, version=None):
        reference = self.select(group, identifier, version)
        result = dict(reference=reference, resource=self.registry.get(reference), project_sha256=self.sha256)
        if reference['kind'] in MATERIALS:
            result['effective'] = self.registry.effective_material(reference)
        return result

    def validate(self):
        return dict(status='source-closure-valid', project_sha256=self.sha256,
                    resources=len(self.references), inventory=deepcopy(self.inventory),
                    rendered=False, aesthetic_review='not-run')

    def export_native(self, destination):
        """Publish captured original bytes exclusively; never replace a destination."""
        destination = Path(destination).absolute()
        require(not destination.exists() and not destination.is_symlink(),
                'LOOKDEV_PROJECT_EXISTS', 'Export destination must be new.')
        require(not destination.resolve().is_relative_to(self.root.resolve()),
                'LOOKDEV_PROJECT_DESTINATION', 'Export outside the source project.')
        require(private_stage_fd_supported(), 'LOOKDEV_PROJECT_EXPORT_PLATFORM',
                'Pinned native export requires a supported POSIX descriptor staging adapter.')
        parent_fd = _directory_fd(destination.parent)
        try:
            # The public wrapper can be renamed or replaced without changing
            # the private descriptor that owns the inner payload publication.
            with private_stage_fd(parent_fd, prefix='.lookdev-export-', cleanup_on_error=False) as wrapper_fd:
                os.mkdir('payload', mode=0o700, dir_fd=wrapper_fd)
                stage_fd = os.open('payload', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=wrapper_fd)
                try:
                    for name, payload in self.sources.items():
                        _write_at(stage_fd, name, payload)
                    # Independent disk reads bind the delivered bytes rather than the
                    # success of write() or existence of file names.
                    inventory = {}
                    for name, payload in self.sources.items():
                        captured = _read_at(stage_fd, name, len(payload))
                        inventory[name] = dict(bytes=len(captured), sha256=hashlib.sha256(captured).hexdigest())
                    require(inventory == self.inventory, 'LOOKDEV_PROJECT_EXPORT', 'Export failed exact-byte verification.')
                    manifest = dict(schema_version=1, kind='lookdev-native-export', project_sha256=self.sha256,
                                    inventory=deepcopy(self.inventory), loss_report=dict(format='native', lost=[], approximated=[]),
                                    rendered=False, aesthetic_review='not-run')
                    _write_at(stage_fd, 'export-manifest.json', canonical_bytes(manifest))
                    verified_stage = os.fstat(stage_fd)
                    rename_no_replace_at(wrapper_fd, 'payload', parent_fd, destination.name)
                    published = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
                    require(stat.S_ISDIR(published.st_mode) and
                            (published.st_dev, published.st_ino) == (verified_stage.st_dev, verified_stage.st_ino),
                            'LOOKDEV_PROJECT_PUBLICATION_CHANGED',
                            'Published directory differs from verified staging; destination is unverified and preserved.')
                    # Directory identity does not authenticate its children. Re-read
                    # the delivered bytes after publication under the bound descriptor.
                    # A publication-time writer must never turn stale pre-rename hashes
                    # into a successful export receipt.
                    delivered = {}
                    for name, payload in self.sources.items():
                        captured = _read_at(stage_fd, name, len(payload))
                        delivered[name] = dict(bytes=len(captured), sha256=hashlib.sha256(captured).hexdigest())
                    require(delivered == self.inventory, 'LOOKDEV_PROJECT_PUBLICATION_CHANGED',
                            'Delivered source inventory changed; unverified destination is preserved.')
                    expected_manifest = canonical_bytes(manifest)
                    require(_read_at(stage_fd, 'export-manifest.json', len(expected_manifest)) == expected_manifest,
                            'LOOKDEV_PROJECT_PUBLICATION_CHANGED',
                            'Delivered export manifest changed; unverified destination is preserved.')
                    final_identity = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
                    require((final_identity.st_dev, final_identity.st_ino) == (verified_stage.st_dev, verified_stage.st_ino),
                            'LOOKDEV_PROJECT_PUBLICATION_CHANGED', 'Destination changed during delivered verification.')
                    return manifest
                finally:
                    os.close(stage_fd)
        finally:
            os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    commands = parser.add_subparsers(dest='group', required=True)
    for group in GROUPS:
        actions = commands.add_parser(group).add_subparsers(dest='action', required=True)
        actions.add_parser('list')
        actions.add_parser('validate')
        inspect = actions.add_parser('inspect')
        inspect.add_argument('--id', required=True)
        inspect.add_argument('--version')
    export = commands.add_parser('export-native')
    export.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        project = Project(args.project)
        if args.group == 'export-native':
            result = project.export_native(args.destination)
        elif args.action == 'list':
            result = dict(project_sha256=project.sha256, resources=project.list(args.group))
        elif args.action == 'validate':
            result = project.validate()
        else:
            result = project.inspect(args.group, args.id, args.version)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (RigError, OSError, ValueError) as exc:
        print(json.dumps(dict(status='failed', code=getattr(exc, 'report', {}).get('code', 'LOOKDEV_PROJECT_INPUT'), message=str(exc))))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
