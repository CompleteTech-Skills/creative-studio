"""Functional camera catalog commands over immutable, exact version references.

Persistence belongs to the project/asset owners. Every command returns a new
validated catalog revision; no shared document or source file is modified.
"""
from copy import deepcopy
import re

from camera3d_contracts import canonical_bytes, validate, MAX_BYTES


def _next_version(old, new):
    pattern = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    if not isinstance(new, str) or re.fullmatch(pattern, new) is None:
        raise ValueError('camera3d revision requires an exact semantic version')
    if tuple(map(int, new.split('.'))) <= tuple(map(int, old.split('.'))):
        raise ValueError('camera3d revision version must increase')


def update_catalog(catalog, version, *, add=None, remove=None, enabled=None, active=...,
                   referenced=None):
    """Apply an explicit immutable revision; referenced removals always fail.

    ``referenced`` contains the exact camera refs used by project shots/rigs.
    The owning project must obtain these from its actual dependency closure;
    this function does not infer reachability from a filename or an ID alone.
    """
    validate(catalog, 'catalog')
    _next_version(catalog['version'], version)
    result = deepcopy(catalog)
    result['version'] = version
    if add is not None:
        candidate = deepcopy(result)
        candidate['cameras'].append(deepcopy(add))
        validate(candidate, 'catalog')
        result = candidate
    if remove is not None:
        if remove not in result['cameras']:
            raise ValueError('camera3d cannot delete an unknown exact camera version')
        if not isinstance(referenced, (list, tuple)) or len(referenced) > 4096:
            raise ValueError('camera3d removal requires an explicit bounded project dependency closure')
        reference_keys = set()
        for reference in referenced:
            if (not isinstance(reference, dict) or not {'kind', 'id', 'version'} <= reference.keys()
                    or any(not isinstance(reference[key], str) for key in ('kind', 'id', 'version'))):
                raise ValueError('camera3d malformed project dependency reference')
            reference_keys.add((reference['kind'], reference['id'], reference['version']))
        if (remove.get('kind'), remove.get('id'), remove.get('version')) in reference_keys:
            raise ValueError('camera3d cannot delete a camera referenced by a shot or rig')
        result['cameras'].remove(remove)
        if remove in result['enabled']:
            result['enabled'].remove(remove)
        if result['active'] == remove:
            result['active'] = None
    if enabled is not None:
        result['enabled'] = deepcopy(enabled)
    if active is not ...:
        result['active'] = deepcopy(active)
    validate(result, 'catalog')
    return result


def revise_camera(camera, version, *, name=None, transform=None, optics=None,
                  new_id=None):
    """Rename/edit or duplicate a camera without mutating shared old versions."""
    validate(camera, 'camera')
    result = deepcopy(camera)
    if new_id is None:
        _next_version(camera['version'], version)
    elif new_id == camera['id']:
        raise ValueError('camera3d duplication requires a new stable ID')
    else:
        result['id'] = new_id
    result['version'] = version
    if name is not None:
        result['metadata']['name'] = name
    if transform is not None:
        result['transform'] = deepcopy(transform)
    if optics is not None:
        result['optics'] = deepcopy(optics)
    validate(result, 'camera')
    return result


def resolve_camera(camera, optics, *, overrides=None, override_optics=None):
    """Resolve a shot override into independent values; no hidden shared edits."""
    validate(camera, 'camera'); validate(optics, 'optics')
    if (camera['optics']['id'], camera['optics']['version']) != (optics['id'], optics['version']):
        raise ValueError('camera3d optics identity disagrees with selected camera')
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - {'transform', 'optics'}:
        raise ValueError('camera3d unsupported shot override')
    resolved = deepcopy(camera)
    resolved_optics = deepcopy(optics)
    if 'transform' in overrides:
        resolved['transform'] = deepcopy(overrides['transform'])
    if 'optics' in overrides:
        if override_optics is None:
            raise ValueError('camera3d alternate optics must be resolved by its exact source owner')
        validate(override_optics, 'optics')
        reference = overrides['optics']
        if (reference['kind'], reference['id'], reference['version']) != (
                override_optics['kind'], override_optics['id'], override_optics['version']):
            raise ValueError('camera3d alternate optics identity differs from its reference')
        resolved['optics'] = deepcopy(reference)
        resolved_optics = deepcopy(override_optics)
    elif override_optics is not None:
        raise ValueError('camera3d unexpected alternate optics without an override reference')
    validate(resolved, 'camera')
    return dict(camera=resolved, optics=resolved_optics,
                source_identity=dict(id=camera['id'], version=camera['version']),
                evaluation_sha256=__import__('hashlib').sha256(canonical_bytes(
                    dict(camera=resolved, optics=resolved_optics))).hexdigest())


def export_document(document, destination):
    """Export one validated immutable document to a new path atomically.

    The caller selects its project or global preset directory. Existing files,
    including previous versions, are never overwritten or deleted here.
    """
    validate(document)
    return _publish_document(document, destination)


def _publish_document(document, destination):
    """Publish prevalidated canonical data with the shared exclusive authority."""
    import os
    from pathlib import Path
    import tempfile
    from atomic_publish import _rename_no_replace
    import hatch
    data = canonical_bytes(document)
    if len(data) > MAX_BYTES:
        raise ValueError('camera3d document exceeds its native byte budget')
    target = Path(destination).absolute()
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError('camera3d document export cannot pass through a symlink')
    if target.exists() or not target.parent.is_dir():
        raise ValueError('camera3d export requires a new path in an existing directory')
    with tempfile.TemporaryDirectory(prefix='.camera3d-document-', dir=target.parent) as directory:
        staged = Path(directory) / 'document.json'
        with staged.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        with hatch.ownership(target, resolve_leaf=False):
            _rename_no_replace(staged, target)
    return __import__('hashlib').sha256(data).hexdigest()
