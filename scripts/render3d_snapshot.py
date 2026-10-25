"""Immutable, bounded Render3D source snapshots using existing asset authorities.

Snapshots preserve declared source bytes. Native rig preview support does not
supply a full Scene3D evaluator or establish that any frame has been rendered.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unicodedata
from typing import Protocol

from atomic_publish import _rename_no_replace
from render3d_contracts import (COORDINATES, MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES,
                                Render3DError, require, validate, load, snapshot_hash, VERSION)
from rig_errors import RigError, atomic_json, safe_path, sha256


@dataclass(frozen=True)
class SourceDescription:
    id: str
    version: str
    entrypoint: str
    files: tuple[tuple[str, str], ...]
    fixture: bool
    scope: str


class SceneAdapter(Protocol):
    """Owner validates its own semantics and returns complete exact-byte closure.

The frozen staging directory is validated again before publication. Adapters
must not execute imported code, mutate input, or infer missing dependencies.
"""
    id: str
    version: str
    def describe(self, root: Path) -> SourceDescription: ...


class NativeRigAdapter:
    """Existing native geometry, skin, animation and evidence validation owner."""
    id = 'native-rig-preview'
    version = VERSION

    def describe(self, root):
        from rig_models import load_rig
        from rig_errors import read_json
        from rig_contracts import validate_contract
        manifest = validate_contract(read_json(safe_path(root, 'rig.json', exists=True)), 'Rig')
        refs = [('rig.json', sha256(root / 'rig.json')),
                ('identity.json', manifest['character']['identity_sha256'])]
        refs.extend((r['path'], r['sha256']) for r in manifest['source']['dependencies'])
        refs.append((manifest['geometry']['data']['path'], manifest['geometry']['data']['sha256']))
        refs.extend((r['data']['path'], r['data']['sha256']) for r in manifest['skins'])
        for item in manifest['animations'] + manifest['review']['receipts'] + manifest['validation']:
            refs.append((item['path'], item['sha256']))
        for receipt in manifest['review']['receipts']:
            record = validate_contract(read_json(safe_path(root, receipt['path'], exists=True)), 'RigReviewReceipt')
            refs.extend((r['path'], r['sha256']) for r in record['evidence'])
        distinct = {}
        for path, digest in refs:
            require(path not in distinct or distinct[path] == digest, 'stale-asset', 'Conflicting source hashes.', path=path)
            distinct[path] = digest
        require(len(distinct) <= MAX_FILES, 'storage-exhaustion', 'Native rig source closure exceeds file budget.')
        sizes = [safe_path(root, path, exists=True).stat().st_size for path in distinct]
        require(all(size <= MAX_FILE_BYTES for size in sizes) and sum(sizes) <= MAX_TOTAL_BYTES,
                'storage-exhaustion', 'Native rig source closure exceeds byte budget.')
        # Enforce snapshot admission before loading binary geometry/skin payloads.
        load_rig(root)
        return SourceDescription(manifest['id'], manifest['version'], 'rig.json',
                                 tuple(sorted(distinct.items())), manifest['source']['fixture'], 'native-rig-preview')


def _copy_verified(root, stage, path, expected):
    try:
        source = safe_path(root, path, exists=True)
        target = safe_path(stage, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(source, flags)
        digest, count = hashlib.sha256(), 0
        with os.fdopen(fd, 'rb') as stream, target.open('xb') as output:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'missing-asset', 'Source must be a regular file.', path=path)
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                count += len(chunk)
                require(count <= MAX_FILE_BYTES, 'storage-exhaustion', 'Source exceeds snapshot per-file budget.', path=path)
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        require(digest.hexdigest() == expected, 'stale-asset', 'Source bytes differ from owner hash.', path=path)
        return dict(path=path, sha256=expected, bytes=count)
    except (OSError, RigError) as exc:
        raise Render3DError('missing-asset', str(exc), path=path) from exc


def create_snapshot(source_root, destination, adapter: SceneAdapter):
    """Publish a new directory only after copying and validating exact bytes.

An existing destination, including an empty directory or symlink, is preserved.
No source revision or output cache is overwritten or pruned.
"""
    source_root, destination = Path(source_root).absolute(), Path(destination).absolute()
    require(not source_root.is_symlink(), 'invalid-input', 'Snapshot root cannot be a symlink.')
    source_root = source_root.resolve()
    require(not destination.exists() and not destination.is_symlink(), 'ownership', 'Snapshot destination already exists.')
    require(not (destination.parent.resolve() / destination.name).is_relative_to(source_root), 'ownership', 'Snapshot destination cannot be inside source.')
    try:
        description = adapter.describe(source_root)
        require(0 < len(description.files) <= MAX_FILES, 'storage-exhaustion', 'Snapshot file count is outside budget.')
        require(all(unicodedata.normalize('NFC', p).casefold().split('/')[0] != 'snapshot.json' for p, _ in description.files),
                'invalid-input', 'snapshot.json is reserved for snapshot metadata.')
        # Preflight the inventory before creating a destination or copying bytes.
        inventory = [dict(path=p, sha256=h, bytes=safe_path(source_root, p, exists=True).stat().st_size)
                     for p, h in description.files]
        document = dict(schema_version=1, kind='render3d-snapshot',
                        adapter=dict(id=adapter.id, version=adapter.version),
                        source=dict(id=description.id, version=description.version),
                        coordinate_system=COORDINATES.copy(), entrypoint=description.entrypoint,
                        files=inventory, total_bytes=sum(f['bytes'] for f in inventory),
                        fixture=description.fixture, scope=description.scope)
        document['snapshot_sha256'] = snapshot_hash(document)
        validate(document)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.render3d-snapshot-', dir=destination.parent))
        try:
            copied = [_copy_verified(source_root, stage, path, digest) for path, digest in description.files]
            require(copied == inventory, 'stale-asset', 'Source inventory changed during snapshot creation.')
            require(adapter.describe(stage) == description, 'stale-asset', 'Frozen source failed its owner validation.')
            atomic_json(stage / 'snapshot.json', document)
            _rename_no_replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return document
    except Render3DError:
        raise
    except (OSError, RigError, ValueError) as exc:
        raise Render3DError('invalid-input', str(exc)) from exc


def verify_snapshot(root, adapter: SceneAdapter):
    """Re-read immutable inventory and owner semantics before each reuse."""
    root = Path(root).absolute()
    require(not root.is_symlink(), 'invalid-input', 'Snapshot root cannot be a symlink.')
    root = root.resolve()
    try:
        document = load(safe_path(root, 'snapshot.json', exists=True), 'RenderSnapshot')
        require(document['adapter'] == dict(id=adapter.id, version=adapter.version),
                'unsupported-feature', 'Snapshot requires a different source adapter.')
        for item in document['files']:
            path = safe_path(root, item['path'], exists=True)
            require(path.stat().st_size == item['bytes'] and sha256(path) == item['sha256'],
                    'stale-asset', 'Frozen source changed.', path=item['path'])
        description = adapter.describe(root)
        require(description.files == tuple((f['path'], f['sha256']) for f in document['files'])
                and description.entrypoint == document['entrypoint']
                and dict(id=description.id, version=description.version) == document['source']
                and description.fixture == document['fixture'] and description.scope == document['scope'],
                'stale-asset', 'Snapshot differs from owner-validated source.')
        return document
    except (OSError, RigError) as exc:
        raise Render3DError('stale-asset', str(exc)) from exc
