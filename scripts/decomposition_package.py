"""Bounded, hash-verified portable character decompositions, separate from pet packages.

An exported directory or ZIP is an immutable transport snapshot. Import creates a
new editable run, preserving the transport receipt as historical provenance.
Neither numerical validation nor a transport receipt supplies semantic approval.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from asset_contracts import hash_object, sha, strict_keys, versions

RECEIPT = 'decomposition-package.json'
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_PACKAGE_FILES = 10000
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}')
# Preserve the established transport helper import/API.
from portable_paths import existing_relative as _relative


def _inside(root, name):
    target = root / _relative(name)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('decomposition package reference escapes its directory: ' + name)
    return target


def _inventory(root, exclude_receipt=True):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('decomposition package requires a regular directory')
    files, aliases, total = {}, set(), 0
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError('decomposition packages cannot contain symbolic links')
        for name in sorted(names):
            path = Path(parent) / name
            relative = _relative(path.relative_to(root).as_posix())
            if exclude_receipt and relative == RECEIPT:
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('decomposition packages contain regular files only')
            if relative.casefold() in aliases:
                raise ValueError('case-colliding decomposition package paths are not portable')
            aliases.add(relative.casefold())
            total += path.stat().st_size
            if len(files) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                raise ValueError('decomposition package exceeds file-count or 1 GiB size bound')
            files[relative] = sha(path)
    return dict(sorted(files.items()))


def _read_json(path):
    return _decode_json(Path(path).read_bytes())


def _decode_json(payload):
    def unique(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise ValueError('duplicate decomposition package JSON key: ' + key)
            value[key] = child
        return value
    try:
        return json.loads(payload, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite decomposition package JSON number')))
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid decomposition package JSON: ' + str(exc)) from exc


def _validate_receipt(value):
    strict_keys(value, {'schema_version', 'kind', 'files', 'inventory_sha256',
        'decomposition_sha256', 'identity_sha256', 'source_sha256', 'fixture',
        'accepted', 'semantic_status', 'versions', 'codex_pet_package'}, 'decomposition package receipt')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1 or value.get('kind') != 'character-decomposition-package':
        raise ValueError('unsupported decomposition package receipt')
    files = value.get('files')
    if not isinstance(files, dict) or not files or len(files) > MAX_PACKAGE_FILES:
        raise ValueError('decomposition package needs a bounded nonempty file inventory')
    aliases = set()
    for name, digest in files.items():
        _relative(name)
        if name == RECEIPT or name.casefold() in aliases or not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ValueError('invalid decomposition package inventory')
        aliases.add(name.casefold())
    for name in ('decomposition.json', 'identity.json', 'validation.json', 'provenance.json'):
        if name not in files:
            raise ValueError('decomposition package is missing ' + name)
    for name in ('inventory_sha256', 'decomposition_sha256', 'identity_sha256', 'source_sha256'):
        if not isinstance(value.get(name), str) or not _HASH.fullmatch(value[name]):
            raise ValueError('invalid decomposition package hash: ' + name)
    if value['decomposition_sha256'] != files['decomposition.json'] or value['identity_sha256'] != files['identity.json'] or value['inventory_sha256'] != hash_object(files):
        raise ValueError('decomposition package receipt disagrees with its inventory')
    if type(value.get('fixture')) is not bool or type(value.get('accepted')) is not bool or value.get('codex_pet_package') is not False:
        raise ValueError('decomposition package requires explicit fixture, review and format labels')
    if not isinstance(value.get('semantic_status'), str) or not value['semantic_status'].strip() or not isinstance(value.get('versions'), dict):
        raise ValueError('decomposition package requires semantic status and tool versions')
    return value


def _validated(root, require_approved=False):
    from character_decomposition import load_decomposition
    manifest = load_decomposition(root, verify=True)
    # The verified loader recomputes deterministic validation and compares every
    # result with this report; avoid decoding all masks a second time here.
    validation = manifest['validation']
    if validation.get('ok') is not True or validation.get('faithful_fidelity_passed') is not True:
        raise ValueError('decomposition package requires passing current faithful reconstruction validation')
    accepted = manifest.get('review', {}).get('status') == 'approved'
    if require_approved and not accepted:
        raise ValueError('decomposition requires current component approval before accepted export')
    return manifest, validation, accepted


def _receipt(root, files, manifest, validation, accepted):
    return {'schema_version': 1, 'kind': 'character-decomposition-package', 'files': files,
            'inventory_sha256': hash_object(files), 'decomposition_sha256': files['decomposition.json'],
            'identity_sha256': files['identity.json'], 'source_sha256': manifest['source']['sha256'],
            'fixture': manifest['source']['fixture'], 'accepted': accepted,
            'semantic_status': str(validation.get('semantic_status', manifest.get('review', {}).get('status', 'pending'))),
            'versions': versions(), 'codex_pet_package': False}


def _copy_inventory(source, destination, files):
    for name, expected in files.items():
        path = _inside(source, name)
        if path.is_symlink() or not path.is_file():
            raise ValueError('decomposition package input changed while copying: ' + name)
        target = _inside(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha(target) != expected:
            raise ValueError('decomposition package input changed while copying: ' + name)


def _write_zip(path, root, files, receipt):
    """Stable ordering, metadata and JSON make repeated exports reproducible."""
    encoded = (json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted([*files, RECEIPT]):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            if name == RECEIPT:
                archive.writestr(info, encoded)
            else:
                digest = hashlib.sha256()
                with _inside(root, name).open('rb') as source, archive.open(info, 'w') as target:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
                if digest.hexdigest() != files[name]:
                    raise ValueError('decomposition package input changed while archiving: ' + name)


def _zip_receipt(archive):
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(names) > MAX_PACKAGE_FILES + 1 or len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise ValueError('decomposition archive has duplicate paths or too many files')
    if sum(entry.file_size for entry in entries) > MAX_PACKAGE_BYTES + MAX_RECEIPT_BYTES:
        raise ValueError('decomposition archive exceeds decompressed size bound')
    for entry in entries:
        # zipfile normalizes native separators and truncates NULs while reading.
        # Validate the original archive name before any host normalization.
        _relative(entry.orig_filename)
        if entry.orig_filename != entry.filename:
            raise ValueError('unsafe normalized decomposition archive member: ' + entry.orig_filename)
        _relative(entry.filename)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if mode not in (0, stat.S_IFREG) or entry.is_dir() or entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError('unsupported or unsafe decomposition archive member: ' + entry.filename)
    if RECEIPT not in names or archive.getinfo(RECEIPT).file_size > MAX_RECEIPT_BYTES:
        raise ValueError('missing or oversized decomposition package receipt')
    receipt = _validate_receipt(_decode_json(archive.read(RECEIPT)))
    if set(names) != set(receipt['files']) | {RECEIPT}:
        raise ValueError('decomposition archive members differ from its receipt')
    return receipt


def verify_package(source):
    """Verify transport integrity without extracting or following any supplied path."""
    source = Path(source).expanduser()
    if source.is_symlink():
        raise ValueError('decomposition package source cannot be a symbolic link')
    source = source.resolve()
    if source.is_dir():
        path = source / RECEIPT
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
            raise ValueError('missing or oversized decomposition package receipt')
        receipt = _validate_receipt(_read_json(path))
        if _inventory(source) != receipt['files']:
            raise ValueError('decomposition package file inventory or hashes changed')
        return receipt
    if not source.is_file() or source.stat().st_size > MAX_PACKAGE_BYTES + MAX_RECEIPT_BYTES:
        raise ValueError('decomposition archive is absent or exceeds size bound')
    try:
        with zipfile.ZipFile(source) as archive:
            receipt = _zip_receipt(archive)
            for name, expected in receipt['files'].items():
                digest = hashlib.sha256()
                with archive.open(name) as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError('decomposition archive hash mismatch: ' + name)
            return receipt
    except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid decomposition package: ' + str(exc)) from exc


def export_decomposition(root, destination, require_approved=False):
    root = Path(root).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('decomposition export needs a new destination outside its source tree')
    manifest, validation, accepted = _validated(root, require_approved)
    files = _inventory(root)
    receipt = _validate_receipt(_receipt(root, files, manifest, validation, accepted))
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.decomposition-export-', dir=destination.parent))
    try:
        if destination.suffix.lower() == '.zip':
            archive_path = stage / 'package.zip'
            _write_zip(archive_path, root, files, receipt)
            verify_package(archive_path)
            current, _, current_accepted = _validated(root, require_approved)
            if current != manifest or current_accepted != accepted or _inventory(root) != files:
                raise ValueError('source decomposition changed during archive export')
            os.replace(archive_path, destination)
        else:
            package = stage / 'package'
            package.mkdir()
            _copy_inventory(root, package, files)
            (package / RECEIPT).write_text(json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            verify_package(package)
            _validated(package, require_approved)
            os.replace(package, destination)
    finally:
        shutil.rmtree(stage)
    return {'export': str(destination), 'inventory_sha256': receipt['inventory_sha256'],
            'sha256': sha(destination) if destination.is_file() else None,
            'accepted': accepted, 'fixture': receipt['fixture'], 'semantic_status': receipt['semantic_status'],
            'codex_pet_package': False, 'integration_verified': False}


def import_decomposition(source, destination):
    source = Path(source).expanduser()
    receipt = verify_package(source)
    source = source.resolve()
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('decomposition import needs a new destination outside the source tree')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.decomposition-import-', dir=destination.parent))
    try:
        if source.is_dir():
            _copy_inventory(source, stage, receipt['files'])
        else:
            with zipfile.ZipFile(source) as archive:
                # Recheck metadata and stream-copy with hashes: the source may have
                # changed after preflight and no unverified file is committed.
                if _zip_receipt(archive) != receipt:
                    raise ValueError('decomposition archive changed during import')
                for name, expected in receipt['files'].items():
                    target = _inside(stage, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as stream, target.open('wb') as output:
                        shutil.copyfileobj(stream, output, length=1024 * 1024)
                    if sha(target) != expected:
                        raise ValueError('decomposition archive changed during import: ' + name)
        manifest, validation, accepted = _validated(stage)
        if manifest['source']['sha256'] != receipt['source_sha256'] or manifest['source']['fixture'] != receipt['fixture'] or accepted != receipt['accepted']:
            raise ValueError('decomposition package labels disagree with current source and review evidence')
        # Keep the transport receipt as history, not an active inventory which
        # would necessarily become stale when this editable run is corrected.
        historical = stage / 'review' / 'imports' / ('package-' + hash_object(receipt) + '.json')
        historical.parent.mkdir(parents=True, exist_ok=True)
        historical.write_text(json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'imported': str(destination), 'source_package_inventory_sha256': receipt['inventory_sha256'],
            'source_sha256': receipt['source_sha256'], 'accepted': accepted,
            'fixture': receipt['fixture'], 'validation': validation,
            'codex_pet_package': False, 'external_integration_verified': False}
