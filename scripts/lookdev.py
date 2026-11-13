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
import secrets

from atomic_publish import rename_no_replace_at
from lookdev_contracts import Registry, canonical_bytes, content_hash, portable_path
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
        parent_fd = _directory_fd(destination.parent)
        stage_fd = None
        stage_name = '.lookdev-export-' + secrets.token_hex(16)
        try:
            os.mkdir(stage_name, mode=0o700, dir_fd=parent_fd)
            stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
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
            rename_no_replace_at(parent_fd, stage_name, parent_fd, destination.name)
            return manifest
        finally:
            # Failed staging remains anchored under the owned output parent for
            # diagnosis; no unverified directory is published as the destination.
            if stage_fd is not None:
                os.close(stage_fd)
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
