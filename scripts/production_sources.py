#!/usr/bin/env python3
"""Exact source/artifact byte transport using small, deduplicated verified parts.

This format transports an explicit file inventory. It does not replace complete
production archives, invoke a provider, infer source provenance, or approve art.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata
from atomic_publish import _rename_no_replace
import production_workflow as workflow

SOURCE_INDEX = 'sources-index.json'
MAX_PART_BYTES = 65535
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 4096
MAX_PARTS = 8192
MAX_PART_REFERENCES = 32768
MAX_INDEX_BYTES = 4 * 1024 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key in source index: ' + key)
        result[key] = value
    return result


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(label + ' has missing or unsupported fields')


def _sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError(label + ' must be a lowercase SHA-256 digest')


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(label + ' exceeds its supported resource bound')


def _path(root, relative):
    if not isinstance(relative, str) or len(relative) > 1024:
        raise ValueError('source paths must be canonical relative strings no longer than 1024 characters')
    for name in relative.split('/'):
        if (any(ord(character) < 32 or character in '<>"|?*' for character in name)
                or name.endswith((' ', '.'))
                or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                    *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10)),
                    'COM¹', 'COM²', 'COM³', 'LPT¹', 'LPT²', 'LPT³'}):
            raise ValueError('source path is not portable across Windows, Linux and macOS: ' + repr(relative))
    return workflow.safe_path(root, relative)


def _portable_inventory(paths):
    """Reject file/directory aliases on case-insensitive and normalizing volumes."""
    namespace = {}
    for relative in paths:
        pieces = relative.split('/')
        for count in range(1, len(pieces) + 1):
            name = '/'.join(pieces[:count])
            key = unicodedata.normalize('NFC', name).casefold()
            if key in namespace and namespace[key] != name:
                raise ValueError('source paths alias on a case-insensitive or Unicode-normalizing filesystem')
            namespace[key] = name


def _publish_stage(stage, destination):
    # All source-transport writers share the existing cross-platform kernel lock.
    # Recheck after streaming so a destination created during staging is retained.
    with workflow.hatch.ownership(destination, resolve_leaf=False):
        _new_destination(destination)
        _rename_no_replace(stage, destination)


def _new_destination(value):
    candidate = Path(value)
    if candidate.exists() or candidate.is_symlink():
        raise ValueError('destination must not exist; preserve the existing source files')
    # Preserve the leaf if another writer creates a link after the check above.
    return candidate.parent.resolve() / candidate.name


def _hash_bounded(path, expected_bytes):
    digest, count = hashlib.sha256(), 0
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(MAX_PART_BYTES), b''):
            count += len(block)
            if count > expected_bytes or count > MAX_FILE_BYTES:
                raise ValueError('source changed or exceeded its bounded file size')
            digest.update(block)
    if count != expected_bytes:
        raise ValueError('source size changed during verification')
    return digest.hexdigest()


def _load_index(bundle_index):
    path = Path(bundle_index)
    if path.is_symlink():
        raise ValueError('source index or bundle cannot be a symlink')
    path = path.resolve()
    if path.is_dir():
        path = path / SOURCE_INDEX
    if path.is_symlink():
        raise ValueError('source index cannot be a symlink')
    if not path.is_file() or path.stat().st_size > MAX_INDEX_BYTES:
        raise ValueError('source index is missing or exceeds the 4 MiB index bound')
    raw = path.read_bytes()
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError('source index exceeds the 4 MiB index bound')
    index = json.loads(raw, object_pairs_hook=_unique_object)
    _keys(index, ('schema_version', 'kind', 'fixture', 'origin', 'files', 'parts',
                  'provenance_paths', 'total_bytes', 'unique_part_bytes'), 'source index')
    if type(index['schema_version']) is not int or index['schema_version'] != 1 or index['kind'] != 'hatch-production-sources':
        raise ValueError('unsupported source transport schema or kind')
    if type(index['fixture']) is not bool:
        raise ValueError('source transport requires an explicit fixture label')
    if not isinstance(index['origin'], str) or not index['origin'].strip() or len(index['origin']) > 16384:
        raise ValueError('source transport requires a bounded explicit origin')
    files, parts = index['files'], index['parts']
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError('source file inventory exceeds its 4096-file bound or is empty')
    if not isinstance(parts, list) or len(parts) > MAX_PARTS:
        raise ValueError('unique source part inventory exceeds its 8192-part bound')
    _integer(index['total_bytes'], 0, MAX_TOTAL_BYTES, 'total assembled bytes')
    _integer(index['unique_part_bytes'], 0, MAX_TOTAL_BYTES, 'unique part bytes')
    part_map, unique_bytes = {}, 0
    for part in parts:
        _keys(part, ('path', 'bytes', 'sha256'), 'source part')
        _sha(part['sha256'], 'part.sha256')
        _integer(part['bytes'], 1, MAX_PART_BYTES, 'part bytes')
        _path(path.parent, part['path'])
        if part['path'] != 'parts/' + part['sha256'] + '.bin' or part['path'] in part_map:
            raise ValueError('source parts must have unique content-addressed canonical paths')
        part_map[part['path']] = part
        unique_bytes += part['bytes']
    if unique_bytes != index['unique_part_bytes']:
        raise ValueError('unique part byte count differs from its inventory')
    original_paths, referenced, total_bytes, references = set(), set(), 0, 0
    for file in files:
        _keys(file, ('path', 'bytes', 'sha256', 'parts'), 'source file')
        _path(path.parent, file['path'])
        _sha(file['sha256'], 'file.sha256')
        _integer(file['bytes'], 0, MAX_FILE_BYTES, 'file bytes')
        if file['path'] in original_paths:
            raise ValueError('duplicate original source path')
        original_paths.add(file['path'])
        if not isinstance(file['parts'], list):
            raise ValueError('source file needs ordered part references')
        references += len(file['parts'])
        if references > MAX_PART_REFERENCES:
            raise ValueError('source part-reference count exceeds its resource bound')
        size = 0
        for relative in file['parts']:
            if not isinstance(relative, str) or relative not in part_map:
                raise ValueError('source file references a missing inventory part')
            referenced.add(relative)
            size += part_map[relative]['bytes']
        if size != file['bytes']:
            raise ValueError('ordered parts do not match the original source file size')
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('total assembled source bytes exceed the 256 MiB bound')
    for relative in original_paths:
        if any(parent.as_posix() in original_paths for parent in Path(relative).parents if parent.as_posix() != '.'):
            raise ValueError('source file paths conflict with a parent file')
    _portable_inventory(original_paths)
    if total_bytes != index['total_bytes'] or referenced != set(part_map):
        raise ValueError('source totals or exact referenced part inventory differ')
    provenance = index['provenance_paths']
    if (not isinstance(provenance, list) or any(not isinstance(item, str) for item in provenance) or
            len(provenance) != len(set(provenance)) or not set(provenance) <= original_paths):
        raise ValueError('provenance paths must be a unique subset of the exact source inventory')
    expected_paths = {path.name, *part_map}
    expected_directories = {parent.as_posix() for relative in expected_paths
                            for parent in Path(relative).parents if parent.as_posix() != '.'}
    actual_paths = set()
    for item in path.parent.rglob('*'):
        relative = item.relative_to(path.parent).as_posix()
        _path(path.parent, relative)
        if item.is_symlink():
            raise ValueError('source bundle cannot contain symlinks')
        if item.is_file():
            actual_paths.add(relative)
        elif not item.is_dir() or relative not in expected_directories:
            raise ValueError('source bundle contains an unlisted directory or special file')
    if actual_paths != expected_paths:
        raise ValueError('source bundle has missing or extra files outside its exact part inventory')
    return path, index, part_map, hashlib.sha256(raw).hexdigest()


def _copy_and_verify(index_root, index, part_map, destination=None):
    """The same byte verifier serves read-only verification and private staging."""
    for file in index['files']:
        target = _path(destination, file['path']) if destination is not None else None
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
        output = target.open('xb') if target is not None else None
        file_digest, file_bytes = hashlib.sha256(), 0
        try:
            for relative in file['parts']:
                part = part_map[relative]
                source = _path(index_root, relative)
                if not source.is_file() or source.stat().st_size != part['bytes']:
                    raise ValueError('source part is missing or its byte count changed: ' + relative)
                part_digest, part_bytes = hashlib.sha256(), 0
                with source.open('rb') as stream:
                    for block in iter(lambda: stream.read(MAX_PART_BYTES), b''):
                        part_bytes += len(block)
                        file_bytes += len(block)
                        if part_bytes > part['bytes'] or file_bytes > file['bytes']:
                            raise ValueError('source part grew beyond its declared resource bound')
                        part_digest.update(block)
                        file_digest.update(block)
                        if output is not None:
                            output.write(block)
                if part_bytes != part['bytes'] or part_digest.hexdigest() != part['sha256']:
                    raise ValueError('source part hash differs from its inventory: ' + relative)
            if file_bytes != file['bytes'] or file_digest.hexdigest() != file['sha256']:
                raise ValueError('assembled source file hash differs; preserve the original ordered parts: ' + file['path'])
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
        finally:
            if output is not None:
                output.close()


def _report(path, index, digest):
    return {'verified': True, 'bundle': str(path), 'bundle_sha256': digest,
            'fixture': index['fixture'], 'origin': index['origin'],
            'file_count': len(index['files']), 'unique_part_count': len(index['parts']),
            'total_bytes': index['total_bytes'], 'unique_part_bytes': index['unique_part_bytes'],
            'files': [{key: file[key] for key in ('path', 'bytes', 'sha256')} for file in index['files']],
            'provenance_paths': list(index['provenance_paths']), 'provider_called': False,
            'visual_review_performed': False, 'production_art_acceptance': False,
            'scope': 'Exact selected source and provenance bytes; complete project and artwork review are separate.'}


def verify_source_bundle(bundle_index):
    path, index, parts, digest = _load_index(bundle_index)
    _copy_and_verify(path.parent, index, parts)
    return _report(path, index, digest)


def create_source_bundle(source_root, paths, destination, *, fixture, origin,
                         provenance_paths=(), part_size=MAX_PART_BYTES):
    source_root = Path(source_root).resolve()
    destination = _new_destination(destination)
    if not source_root.is_dir():
        raise ValueError('source root must be an existing directory')
    if type(fixture) is not bool or not isinstance(origin, str) or not origin.strip() or len(origin) > 16384:
        raise ValueError('source creation requires an explicit fixture label and bounded origin')
    _integer(part_size, 1, MAX_PART_BYTES, 'part_size')
    if (not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_FILES or
            any(not isinstance(relative, str) for relative in paths) or len(paths) != len(set(paths))):
        raise ValueError('explicit source inventory must contain 1 to 4096 unique relative paths')
    _portable_inventory(paths)
    if (not isinstance(provenance_paths, (list, tuple)) or
            any(not isinstance(relative, str) for relative in provenance_paths) or
            len(provenance_paths) != len(set(provenance_paths)) or not set(provenance_paths) <= set(paths)):
        raise ValueError('provenance paths must be a unique subset of the selected files')
    files, total, references = [], 0, 0
    for relative in sorted(paths):
        source = _path(source_root, relative)
        if not source.is_file():
            raise ValueError('selected original source is missing: ' + relative)
        size = source.stat().st_size
        _integer(size, 0, MAX_FILE_BYTES, 'file bytes')
        total += size
        references += (size + part_size - 1) // part_size
        if total > MAX_TOTAL_BYTES or references > MAX_PART_REFERENCES:
            raise ValueError('selected source inventory exceeds the total-byte or part-reference bound')
        files.append({'path': relative, 'bytes': size, 'sha256': _hash_bounded(source, size), 'parts': []})
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.hatch-source-parts-', dir=destination.parent))
    try:
        unique = {}
        for file in files:
            source = _path(source_root, file['path'])
            digest, copied = hashlib.sha256(), 0
            with source.open('rb') as stream:
                for block in iter(lambda: stream.read(part_size), b''):
                    copied += len(block)
                    if copied > file['bytes']:
                        raise ValueError('original source changed while creating its transport parts')
                    sha256 = hashlib.sha256(block).hexdigest()
                    relative = 'parts/' + sha256 + '.bin'
                    if relative not in unique:
                        if len(unique) >= MAX_PARTS:
                            raise ValueError('unique part inventory exceeds its 8192-part bound')
                        target = _path(stage, relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(block)
                        unique[relative] = {'path': relative, 'bytes': len(block), 'sha256': sha256}
                    elif unique[relative]['bytes'] != len(block):
                        raise ValueError('content-addressed part size conflict')
                    file['parts'].append(relative)
                    digest.update(block)
            if copied != file['bytes'] or digest.hexdigest() != file['sha256']:
                raise ValueError('original source changed while creating its transport parts')
        index = {'schema_version': 1, 'kind': 'hatch-production-sources', 'fixture': fixture,
                 'origin': origin, 'files': files, 'parts': [unique[key] for key in sorted(unique)],
                 'provenance_paths': sorted(provenance_paths), 'total_bytes': total,
                 'unique_part_bytes': sum(part['bytes'] for part in unique.values())}
        raw = (json.dumps(index, sort_keys=True, indent=2) + '\n').encode('utf-8')
        if len(raw) > MAX_INDEX_BYTES:
            raise ValueError('source index exceeds the 4 MiB bound')
        (stage / SOURCE_INDEX).write_bytes(raw)
        report = verify_source_bundle(stage)
        _publish_stage(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {**report, 'bundle': str(destination / SOURCE_INDEX), 'created': True}


def restore_source_bundle(bundle_index, destination):
    destination = _new_destination(destination)
    path, index, parts, digest = _load_index(bundle_index)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.hatch-source-restore-', dir=destination.parent))
    try:
        _copy_and_verify(path.parent, index, parts, stage)
        _publish_stage(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {**_report(path, index, digest), 'restored': str(destination)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--root', required=True)
    create.add_argument('--file', action='append', required=True)
    create.add_argument('--destination', required=True)
    create.add_argument('--origin', required=True)
    create.add_argument('--provenance', action='append', default=[])
    create.add_argument('--part-bytes', type=int, default=MAX_PART_BYTES)
    label = create.add_mutually_exclusive_group(required=True)
    label.add_argument('--fixture', action='store_true')
    label.add_argument('--production', action='store_true')
    verify = commands.add_parser('verify')
    verify.add_argument('--bundle', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--bundle', required=True)
    restore.add_argument('--destination', required=True)
    delivery = commands.add_parser('verify-delivery', help='Verify the pinned public pilot inventory and authentic history')
    delivery.add_argument('--pilot', type=Path, default=Path(__file__).resolve().parents[1] / 'examples/production-pilot')
    pilot = commands.add_parser('restore-pilot', help='Reconstruct the complete retained pilot without generation or new reviews')
    pilot.add_argument('--pilot', type=Path, default=Path(__file__).resolve().parents[1] / 'examples/production-pilot')
    pilot.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ('verify-delivery', 'restore-pilot'):
            from production_delivery import verify_delivery, restore_pilot
            result = (verify_delivery(args.pilot) if args.command == 'verify-delivery'
                      else restore_pilot(args.pilot, args.destination))
        elif args.command == 'create':
            result = create_source_bundle(args.root, args.file, args.destination, fixture=args.fixture,
                         origin=args.origin, provenance_paths=args.provenance, part_size=args.part_bytes)
        elif args.command == 'verify':
            result = verify_source_bundle(args.bundle)
        else:
            result = restore_source_bundle(args.bundle, args.destination)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc), 'command': args.command, 'provider_called': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
