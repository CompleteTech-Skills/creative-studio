"""Bind optional catalog compilation evidence to the existing run and review lifecycle.

Selection metadata records definition provenance. It never certifies artwork, grants
prop approval, or changes the strict runtime profile contract.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from asset_contracts import strict_keys
from profile_spec import profile_hash


def read_selection(path, profile, identity):
    """Validate a compiled sidecar and the independent production identity contract."""
    from state_catalog import validate_selection_manifest

    source = Path(path).expanduser().resolve()
    if source.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('state selection exceeds the 8 MiB metadata bound')
    value = json.loads(source.read_text(encoding='utf-8'))
    validate_selection_manifest(value, profile)
    approved = {item.casefold() for item in identity.get('approved_props', [])}
    missing = [prop for prop in value['required_props'] if prop.casefold() not in approved]
    if missing:
        raise ValueError('selected states require identity.approved_props: ' + ', '.join(missing))
    for name, requirement in value['state_requirements'].items():
        facing = requirement['direction']['facing']
        declared = identity.get('directions', {}).get(name, 'any')
        if facing in ('left', 'right', 'front', 'back') and declared not in ('any', facing):
            raise ValueError('identity direction contradicts selected state ' + name + ': ' + facing)
    return value


def prepared_selection(root, *, reader=None):
    """Read immutable original compilation evidence, including after profile revision."""
    root = Path(root)
    request_path = root / 'pet_request.json'
    if not request_path.is_file():
        return None
    reader = reader or (lambda path: path.read_bytes())
    request = json.loads(reader(request_path))
    record = request.get('state_selection')
    path = root / 'state-selection.json'
    if record is None:
        if path.exists():
            raise ValueError('unbound state-selection.json; prepare with --state-selection')
        return None
    strict_keys(record, {'path', 'sha256', 'profile_sha256'}, 'state selection reference')
    if record.get('path') != 'state-selection.json' or not path.is_file() or path.is_symlink():
        raise ValueError('missing or invalid original state selection evidence')
    raw = reader(path)
    if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
        raise ValueError('state selection evidence changed after preparation')
    value = json.loads(raw.decode('utf-8'))
    if value.get('profile_sha256') != record.get('profile_sha256'):
        raise ValueError('state selection profile reference changed')
    return value

def export_selection(root, output, profile):
    value = prepared_selection(root)
    if value is None:
        return None
    source = Path(root) / 'state-selection.json'
    (Path(output) / source.name).write_bytes(source.read_bytes())
    return {
        'path': source.name,
        'catalog_version': value['catalog_version'],
        'catalog_sha256': value['catalog_sha256'],
        'original_profile_sha256': value['profile_sha256'],
        'current_profile_matches_selection': profile_hash(profile) == value['profile_sha256'],
        'note': 'Original definition selection only. Profile revisions and artwork review are recorded separately.',
    }
