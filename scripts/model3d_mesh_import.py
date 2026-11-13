"""Model3D acquisition adapter over the existing native import authority.

No alternate parser, registry or normalization engine lives here. A returned
snapshot owns its temporary source closure: use it as a context manager or close
it after saving required source evidence through the authoritative asset owner.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import copy
import hashlib
import math
import tempfile
from typing import Any

from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, require


def _limits(values):
    result = dict(DEFAULT_LIMITS)
    require(values is None or isinstance(values, dict), 'MODEL3D_LIMITS', 'Limits must be an object.')
    for key, value in (values or {}).items():
        require(key in result and not isinstance(value, bool) and isinstance(value, (int, float))
                and (isinstance(value, int) or math.isfinite(value)) and value == int(value) and 0 < value <= 2**63-1,
                'MODEL3D_LIMITS', 'Limits must be positive whole JSON numbers with known names.', asset=key)
        result[key] = int(value)
    return result


def _temporary():
    return tempfile.TemporaryDirectory(prefix='model3d-import-')


@dataclass
class MeshImport:
    model: Any
    source_records: list[dict]
    provenance: dict
    root: Path
    _temporary: Any = field(repr=False)

    def close(self):
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _load(path, *, scale, limits):
    from rig_import import load_character_model
    try:
        return load_character_model(path, scale=scale, limits=limits)
    except RigError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, OSError) as exc:
        raise RigError('MODEL3D_MODEL', 'Native importer rejected the model payload.', asset=Path(path).name) from exc


def import_mesh(path, *, scale=1., limits=None, blender=None):
    """Snapshot sources through the shared native import authority."""
    from rig_source_snapshot import import_snapshot
    return import_snapshot(path, scale=scale, limits=limits, blender=blender)


def import_canonical(library, ref, *, entry=None, limits=None):
    """Decode an exact canonical-library version through its public reader.

    The library authenticates its manifest and payloads. This adapter verifies
    the returned bytes again, retains the complete manifest (including namespace,
    original source provenance and uncertainty), and applies no second coordinate
    conversion to an already canonical asset. Legacy-2D records are rejected.
    """
    from asset_library_contracts import validate, load_manifest, canonical_bytes, portable_path
    limits = _limits(limits)
    validate(ref, 'AssetRef')
    manifest = load_manifest(canonical_bytes(library.resolve(copy.deepcopy(ref))))
    require(manifest['manifest_sha256'] == ref['sha256'] and
            manifest['identity']['id'] == ref['id'] and manifest['version'] == ref['version'],
            'MODEL3D_REFERENCE', 'Registry returned a different immutable version.')
    require(manifest['compatibility']['coordinate_system'] == 'right-handed-y-up-z-forward'
            and manifest['compatibility']['unit_scale'] == 1,
            'MODEL3D_COORDINATES', 'Canonical mesh import requires normalized Y-up meter geometry.')
    inventory = {item['path']: item for item in manifest['files']}
    if entry is None:
        candidates = [item['path'] for item in manifest['files']
                      if item['role'] in ('geometry', 'runtime') and item['format'] in ('glb','gltf','obj')]
        require(len(candidates) == 1, 'MODEL3D_ENTRY', 'Select one inventoried geometry entry explicitly.')
        entry = candidates[0]
    portable_path(entry)
    require(entry in inventory and inventory[entry]['format'] in ('glb','gltf','obj'),
            'MODEL3D_ENTRY', 'Entry must name an inventoried supported geometry file.')
    require(len(inventory) <= limits['archive_files'] and
            sum(int(item['bytes']) for item in inventory.values()) <= min(limits['dependency_bytes'], limits['memory_bytes']//3) and
            all(item['bytes'] <= limits['source_bytes'] for item in inventory.values()),
            'MODEL3D_BUDGET', 'Canonical inventory exceeds encoded-byte limits before reading.')
    temporary = _temporary(); root = Path(temporary.name).resolve()
    try:
        records = []
        for name,item in inventory.items():
            payload = library.read_file(copy.deepcopy(ref), name)
            require(isinstance(payload, bytes) and len(payload) == item['bytes']
                    and hashlib.sha256(payload).hexdigest() == item['sha256'],
                    'MODEL3D_HASH', 'Public reader bytes disagree with immutable inventory.', asset=name)
            target = root/name; target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output: output.write(payload)
            records.append(dict(path=name, bytes=len(payload), sha256=item['sha256']))
        model = _load(root/entry, scale=1., limits=limits)
        actual = {p.relative_to(root).as_posix() for p in model.source_files}
        require(actual <= set(inventory), 'MODEL3D_INVENTORY', 'Model references a non-inventoried dependency.')
        return MeshImport(model, records, dict(authority='AssetLibrary.resolve/read_file',
                          canonical_ref=copy.deepcopy(ref), manifest=copy.deepcopy(manifest),
                          source_entry=entry, normalization='retained canonical bytes; no additional conversion'),
                          root, temporary)
    except BaseException:
        temporary.cleanup(); raise
