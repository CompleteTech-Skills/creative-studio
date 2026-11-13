"""Immutable, bounded Render3D source snapshots using existing asset authorities.

Snapshots preserve declared source bytes. Native rig preview support does not
supply a full Scene3D evaluator or establish that any frame has been rendered.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unicodedata
import zipfile
import zlib
import errno
from typing import Protocol

from atomic_publish import _rename_no_replace
from render3d_contracts import (COORDINATES, MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES,
                                Render3DError, require, validate, load, snapshot_hash, VERSION, open_regular)
from rig_errors import RigError, atomic_json, safe_path, sha256, normalize_root


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


class Scene3DAdapter:
    """Freeze the Scene3D owner's complete project closure without rendering.

    The owner validates document identities, references and dependency bytes.
    This does not promote its declared geometry metrics to decoded geometry or
    imply that any scene property is supported by a rendering backend.
    """
    id = 'scene3d-project'
    version = VERSION

    def describe(self, root):
        from scene3d_contracts import load_project
        result = load_project(root)
        project = result['project']
        return SourceDescription(project['id'], project['version'], 'project.json',
            tuple(sorted((path, item['sha256']) for path, item in result['inventory'].items())),
            project['provenance']['fixture'], 'scene3d')

    def validate_request(self, root, request, snapshot):
        """Require the selected shot/camera to exist in the frozen project."""
        from scene3d_contracts import load_project
        result = load_project(root)
        require(result['inventory'] == {item['path']: dict(sha256=item['sha256'], bytes=item['bytes'])
                                      for item in snapshot['files']},
                'stale-asset', 'Scene changed while binding the requested shot.')
        project = result['project']
        identity = lambda value: dict(id=value['id'], version=value['version'])
        require(identity(project) == request['project'], 'stale-asset',
                'Request project identity differs from the frozen project.')
        scenes = [result['documents'][ref['path']] for ref in project['scenes']
                  if identity(ref) == request['scene']]
        shots = [result['documents'][ref['path']] for ref in project['shots']
                 if identity(ref) == request['shot']]
        require(len(scenes) == 1 and len(shots) == 1, 'missing-asset',
                'Request scene and shot must identify frozen project members.')
        scene, shot = scenes[0], shots[0]
        require(identity(shot['scene']) == identity(scene), 'invalid-input',
                'Request shot belongs to another scene.')
        require(request['camera'] == shot['camera'] and request['camera'] in scene['cameras'],
                'missing-asset', 'Request camera differs from the frozen shot camera.')
        last_local = request['frame_range']['end'] - request['frame_range']['start'] - 1
        last_source = (Fraction(**request['source_start'])
                       + last_local / Fraction(**request['fps']) * Fraction(**request['playback_rate']))
        require(last_source < Fraction(shot['duration_frames'], 1) / Fraction(**shot['fps']),
                'invalid-input', 'Requested samples exceed the frozen shot duration.')


class NativeRigAdapter:
    """Existing native geometry, skin, animation and evidence validation owner."""
    id = 'native-rig-preview'
    version = VERSION

    def describe(self, root):
        from rig_models import load_rig
        from rig_errors import decode_json
        from rig_contracts import validate_contract
        manifest_path = safe_path(root, 'rig.json', exists=True)
        with open_regular(manifest_path, 32 * 1024**2) as stream:
            manifest_bytes = stream.read(32 * 1024**2 + 1)
        manifest = validate_contract(decode_json(manifest_bytes, max_bytes=32 * 1024**2), 'Rig')
        refs = [('rig.json', hashlib.sha256(manifest_bytes).hexdigest()),
                ('identity.json', manifest['character']['identity_sha256'])]
        refs.extend((r['path'], r['sha256']) for r in manifest['source']['dependencies'])
        refs.append((manifest['geometry']['data']['path'], manifest['geometry']['data']['sha256']))
        refs.extend((r['data']['path'], r['data']['sha256']) for r in manifest['skins'])
        for item in manifest['animations'] + manifest['review']['receipts'] + manifest['validation']:
            refs.append((item['path'], item['sha256']))
        for receipt in manifest['review']['receipts']:
            with open_regular(safe_path(root, receipt['path'], exists=True), 32 * 1024**2) as stream:
                record = validate_contract(decode_json(stream.read(32 * 1024**2 + 1), max_bytes=32 * 1024**2), 'RigReviewReceipt')
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
        # Native validators may reopen payloads using their legacy readers.
        # Give them private, bounded regular files rather than caller-mutable paths.
        with tempfile.TemporaryDirectory(prefix='render3d-native-admission-') as temporary:
            private_root = Path(temporary)
            copied_bytes = 0
            for path, digest in distinct.items():
                copied = _copy_verified(root, private_root, path, digest,
                                        remaining=MAX_TOTAL_BYTES - copied_bytes)
                copied_bytes += copied['bytes']
            rig = load_rig(private_root)
            # Loading authenticates storage; the existing semantic validators own
            # executable skeleton/controller/skin/clip correctness. Missing optional
            # rigging is allowed for static imports, but present declarations must
            # not freeze malformed data as validated source.
            from rig_skeleton import validate_skeleton
            from rig_ik import validate_controllers
            from rig_skinning import validate_skin
            from rig_animation import validate_animation
            skeleton = rig.manifest['skeleton']
            if skeleton is None:
                require(not rig.weights and not rig.clips
                        and not any(rig.manifest['controllers'].values()),
                        'invalid-input', 'Skin, animation and controllers require a skeleton.')
            else:
                validate_skeleton(skeleton, rig.manifest['limits'])
                validate_controllers(skeleton, rig.manifest['controllers'])
                meshes = rig.geometry.by_id()
                for name, skin in rig.weights.items():
                    require(name in meshes, 'invalid-input', 'Skin targets a missing mesh.')
                    validate_skin(skin, meshes[name], skeleton, rig.manifest['limits'])
                for clip in rig.clips.values():
                    validate_animation(rig, clip)
            return SourceDescription(manifest['id'], manifest['version'], 'rig.json',
                                     tuple(sorted(distinct.items())), manifest['source']['fixture'], 'native-rig-preview')


def _copy_verified(root, stage, path, expected, *, remaining=None):
    try:
        source = safe_path(root, path, exists=True)
        target = safe_path(stage, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The source can change after safe_path. Never block opening a FIFO
        # substituted at that boundary; fstat below rejects nonregular files.
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
        fd = os.open(source, flags)
        digest, count = hashlib.sha256(), 0
        limit = MAX_FILE_BYTES if remaining is None else min(MAX_FILE_BYTES, remaining)
        with os.fdopen(fd, 'rb') as stream, target.open('xb') as output:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'missing-asset', 'Source must be a regular file.', path=path)
            for chunk in iter(lambda: stream.read(min(1024 * 1024, limit - count + 1)), b''):
                count += len(chunk)
                require(count <= limit, 'storage-exhaustion', 'Source exceeds remaining snapshot byte budget.', path=path)
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        require(digest.hexdigest() == expected, 'stale-asset', 'Source bytes differ from owner hash.', path=path)
        return dict(path=path, sha256=expected, bytes=count)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC,errno.EDQUOT):
            raise Render3DError('storage-exhaustion',str(exc),path=path,remediation='Free destination storage or choose another volume.') from exc
        raise Render3DError('missing-asset',str(exc),path=path) from exc
    except RigError as exc:
        raise Render3DError('missing-asset',str(exc),path=path) from exc


def create_snapshot(source_root, destination, adapter: SceneAdapter):
    """Publish a new directory only after copying and validating exact bytes.

An existing destination, including an empty directory or symlink, is preserved.
No source revision or output cache is overwritten or pruned.
"""
    source_root, destination = Path(source_root).absolute(), Path(destination).absolute()
    require(not source_root.is_symlink(), 'invalid-input', 'Snapshot root cannot be a symlink.')
    source_root = normalize_root(source_root)
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
            copied, copied_bytes = [], 0
            for path, digest in description.files:
                item = _copy_verified(source_root, stage, path, digest,
                                      remaining=MAX_TOTAL_BYTES - copied_bytes)
                copied.append(item); copied_bytes += item['bytes']
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
    except OSError as exc:
        if exc.errno in (errno.ENOSPC,errno.EDQUOT):
            raise Render3DError('storage-exhaustion',str(exc),remediation='Free destination storage or choose another volume.') from exc
        raise Render3DError('invalid-input',str(exc)) from exc
    except (OSError, RigError, ValueError, TypeError, KeyError, IndexError, OverflowError, zipfile.BadZipFile, zlib.error) as exc:
        raise Render3DError('invalid-input', str(exc)) from exc


def verify_snapshot(root, adapter: SceneAdapter):
    """Re-read immutable inventory and owner semantics before each reuse."""
    root = Path(root).absolute()
    require(not root.is_symlink(), 'invalid-input', 'Snapshot root cannot be a symlink.')
    root = normalize_root(root)
    try:
        document = load(safe_path(root, 'snapshot.json', exists=True), 'RenderSnapshot')
        require(document['adapter'] == dict(id=adapter.id, version=adapter.version),
                'unsupported-feature', 'Snapshot requires a different source adapter.')
        for item in document['files']:
            path = safe_path(root, item['path'], exists=True)
            digest, count = hashlib.sha256(), 0
            with open_regular(path, MAX_FILE_BYTES) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    count += len(block)
                    require(count <= MAX_FILE_BYTES, 'storage-exhaustion', 'Frozen source exceeds byte budget.')
                    digest.update(block)
            require(count == item['bytes'] and digest.hexdigest() == item['sha256'],
                    'stale-asset', 'Frozen source changed.', path=item['path'])
        description = adapter.describe(root)
        require(description.files == tuple((f['path'], f['sha256']) for f in document['files'])
                and description.entrypoint == document['entrypoint']
                and dict(id=description.id, version=description.version) == document['source']
                and description.fixture == document['fixture'] and description.scope == document['scope'],
                'stale-asset', 'Snapshot differs from owner-validated source.')
        return document
    except Render3DError:
        raise
    except (OSError, RigError, ValueError, TypeError, KeyError, IndexError, OverflowError, zipfile.BadZipFile, zlib.error) as exc:
        raise Render3DError('stale-asset', str(exc)) from exc
