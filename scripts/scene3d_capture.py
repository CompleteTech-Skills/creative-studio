"""Admit exact registered files into private trees before legacy decoding.

The caller supplies the already selected dependency closure. All paths are
regular, bounded, hash checked and copied before the context yields; legacy
loaders must then use only the yielded tree, never caller-owned source paths.
"""
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
from pathlib import Path, PurePosixPath
import re
import tempfile

from asset_library import read_source_bytes
from asset_library_contracts import MAX_FILE_BYTES
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from scene3d_contracts import portable_path


def _require(condition, message, code='SCENE3D_CAPTURE'):
    if not condition:
        raise RigError(code, message, stage='scene3d-capture')


def _records(inventory):
    _require(isinstance(inventory, (Mapping, list, tuple)) and
             0 < len(inventory) <= DEFAULT_LIMITS['archive_files'],
             'Capture inventory must contain a bounded nonempty exact closure.')
    if isinstance(inventory, Mapping):
        records = []
        for path, record in inventory.items():
            _require(isinstance(record, Mapping) and ('path' not in record or record['path'] == path),
                     'Capture inventory key and record path disagree.')
            records.append(dict(record, path=path))
    else:
        records = [dict(record) if isinstance(record, Mapping) else record for record in inventory]
    _require(0 < len(records) <= DEFAULT_LIMITS['archive_files'],
             'Captured inventory metadata exceeds its count bound.')
    seen, aliases = set(), set()
    for record in records:
        _require(isinstance(record, Mapping) and isinstance(record.get('path'), str),
                 'Capture records need explicit portable paths.')
        path = record['path']
        portable_path(path)
        _require(path not in seen and path.casefold() not in aliases,
                 'Capture inventory repeats or aliases a path.')
        seen.add(path)
        aliases.add(path.casefold())
        _require(isinstance(record.get('sha256'), str) and
                 re.fullmatch(r'[0-9a-f]{64}', record['sha256']) is not None,
                 'Capture records need an exact SHA256 hash.')
        _require('bytes' not in record or type(record['bytes']) is int and
                 0 <= record['bytes'] <= MAX_FILE_BYTES,
                 'Capture byte declarations must be bounded nonnegative integers.')
    for path in seen:
        parts = path.casefold().split('/')
        _require(not any('/'.join(parts[:end]) in aliases for end in range(1, len(parts))),
                 'Capture inventory has a file/directory alias.')
    return sorted([dict(path=r['path'], sha256=r['sha256'],
                        **({'bytes': r['bytes']} if 'bytes' in r else {}))
                   for r in records], key=lambda record: record['path'])


@contextmanager
def capture_verified_files(root, verified_inventory):
    """Yield a private project-relative tree containing only exact selected files.

    Inventory accepts a list of exact ``{path, sha256, ...}`` references or the
    canonical project ``{path: {sha256, bytes, ...}}`` inventory. No file is
    consumed by a legacy reader until the entire selected closure is admitted.
    The private tree is removed on both success and failure.
    """
    records = _records(verified_inventory)
    try:
        root = Path(root)
    except (TypeError, ValueError) as exc:
        raise RigError('SCENE3D_CAPTURE', 'Invalid capture source root.', stage='scene3d-capture') from exc
    with tempfile.TemporaryDirectory(prefix='scene3d-verified-capture-') as temporary:
        admitted = Path(temporary)
        total = 0
        for record in records:
            remaining = DEFAULT_LIMITS['dependency_bytes'] - total
            _require(remaining >= 0, 'Capture dependency bytes exceed their aggregate bound.')
            bound = min(MAX_FILE_BYTES, remaining)
            if record['sha256'] == hashlib.sha256(b'').hexdigest():
                payload = read_source_bytes(root/record['path'], bound, allow_empty=True)
            else:
                payload = read_source_bytes(root/record['path'], bound)
            _require(hashlib.sha256(payload).hexdigest() == record['sha256'],
                     'Dependency differs from its registered exact bytes.', 'SCENE3D_SOURCE_CHANGED')
            _require('bytes' not in record or len(payload) == record['bytes'],
                     'Dependency differs from its registered byte count.', 'SCENE3D_SOURCE_CHANGED')
            total += len(payload)
            target = admitted/record['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        yield admitted


@contextmanager
def capture_verified_native(root, manifest_relative, verified_inventory):
    """Yield the private rig directory for an arbitrary registered manifest.

    ``root`` and inventory paths are project-relative, for example manifest
    ``actors/hero/rig.json``. Only its native subtree is captured. Callers that
    also need a pose or scene file outside that subtree use
    :func:`capture_verified_files` with the complete explicit selected closure.
    """
    portable_path(manifest_relative)
    _require(PurePosixPath(manifest_relative).name == 'rig.json',
             'Native capture requires an exact rig.json manifest path.')
    records = _records(verified_inventory)
    _require(any(record['path'] == manifest_relative for record in records),
             'Native manifest is absent from the verified inventory.')
    parent = PurePosixPath(manifest_relative).parent
    prefix = '' if str(parent) == '.' else str(parent)+'/'
    selected = [record for record in records if record['path'].startswith(prefix)]
    with capture_verified_files(root, selected) as captured:
        yield captured if not prefix else captured/str(parent)
