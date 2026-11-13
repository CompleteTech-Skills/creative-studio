"""Bounded deterministic archives with a verified inventory and atomic restore."""
from pathlib import Path
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from atomic_publish import _rename_no_replace
from rig_contracts import DEFAULT_LIMITS
from rig_errors import MAX_JSON_BYTES, atomic_bytes, decode_json, job_lock, open_payload, portable_name, require, safe_path, sha256
from rig_models import load_rig, rig_signature


def _package_paths(root):
    """Yield directory entries lazily so the caller can stop at its budget."""
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                yield Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _package_entries(root, limits):
    """Inventory bounded regular files; reads are pinned to admitted descriptors."""
    entries, names, total, visited = [], set(), 0, 0
    for path in _package_paths(root):
        visited += 1
        require(visited <= limits['archive_files'] * 2, 'RIG_ARCHIVE_LIMIT',
                'Archive directory traversal exceeds its entry budget.')
        require(not path.is_symlink(), 'RIG_ARCHIVE_SYMLINK',
                'Symlinks cannot enter a rig package.', asset=str(path))
        if path.is_dir():
            continue
        relative = portable_name(path.relative_to(root).as_posix())
        require(relative.casefold() not in names and relative != 'inventory.json',
                'RIG_ARCHIVE_COLLISION', 'Portable filenames collide.')
        require(len(entries) + 2 <= limits['archive_files'], 'RIG_ARCHIVE_LIMIT',
                'Archive exceeds its file budget.')
        names.add(relative.casefold())
        digest, size = hashlib.sha256(), 0
        with open_payload(root, relative) as (source, info):
            require(info.st_size <= limits['archive_bytes'] - total,
                    'RIG_ARCHIVE_LIMIT', 'Archive exceeds its expanded byte budget.')
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                size += len(chunk)
                require(size <= info.st_size, 'RIG_ARCHIVE_CHANGED',
                        'Archive input grew while reading.', asset=relative)
                digest.update(chunk)
            require(size == info.st_size, 'RIG_ARCHIVE_CHANGED',
                    'Archive input changed while reading.', asset=relative)
        total += size
        entries.append(dict(path=relative, sha256=digest.hexdigest(), bytes=size))
    return sorted(entries, key=lambda item: item['path']), total


def _write_package_entry(archive, root, entry):
    name = entry['path']
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    digest, size = hashlib.sha256(), 0
    with open_payload(root, name) as (source, metadata), archive.open(info, 'w') as output:
        require(metadata.st_size == entry['bytes'], 'RIG_ARCHIVE_CHANGED',
                'Archive input changed after inventory.', asset=name)
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            size += len(chunk)
            require(size <= entry['bytes'], 'RIG_ARCHIVE_CHANGED',
                    'Archive input grew after inventory.', asset=name)
            digest.update(chunk)
            output.write(chunk)
    require(size == entry['bytes'] and digest.hexdigest() == entry['sha256'],
            'RIG_ARCHIVE_CHANGED', 'Archive input differs from inventory.', asset=name)


def package_rig(root, destination):
    """Publish a verified, bounded snapshot without replacing any destination."""
    root = Path(root).absolute()
    destination = Path(destination).absolute()
    require(not root.is_symlink(), 'RIG_ARCHIVE_SYMLINK', 'Run root cannot be a symlink.')
    root = root.resolve()
    require(not destination.resolve().is_relative_to(root), 'RIG_ARCHIVE_DESTINATION',
            'Write the archive outside its source run.')
    require(not destination.exists() and not destination.is_symlink(), 'RIG_ARCHIVE_EXISTS',
            'Archive into a new destination; existing files are preserved.')
    with job_lock(root):
        rig = load_rig(root)
        entries, total = _package_entries(root, rig.manifest['limits'])
        inventory = dict(schema_version=1, kind='hatch-3d-rig-package',
                         rig_sha256=rig_signature(rig.manifest), files=entries)
        inventory_bytes = json.dumps(inventory, sort_keys=True, allow_nan=False).encode()
        require(total + len(inventory_bytes) <= rig.manifest['limits']['archive_bytes'],
                'RIG_ARCHIVE_LIMIT', 'Archive inventory exceeds the expanded byte budget.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.rig-package-', suffix='.zip', dir=destination.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
                for entry in entries:
                    _write_package_entry(archive, root, entry)
                info = zipfile.ZipInfo('inventory.json', (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, inventory_bytes)
            with open(temporary, 'rb') as completed:
                os.fsync(completed.fileno())
            # Refuse a mixed revision if a writer ignored the shared run lock.
            final_entries, _ = _package_entries(root, rig.manifest['limits'])
            require(final_entries == entries, 'RIG_ARCHIVE_CHANGED',
                    'Rig files changed during packaging; no archive was published.')
            # Validate the actual bytes and rig closure, not the mutable input.
            with tempfile.TemporaryDirectory(prefix='.rig-package-verify-', dir=destination.parent) as verification:
                restore_rig(temporary, Path(verification) / 'run', limits=rig.manifest['limits'])
            _rename_no_replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return dict(path=str(destination), sha256=sha256(destination), files=len(entries),
                expanded_bytes=total, rig_sha256=inventory['rig_sha256'])


def restore_rig(archive_path,destination,*,limits=None):
    limits=limits or DEFAULT_LIMITS;archive_path=Path(archive_path);destination=Path(destination).absolute()
    require(archive_path.is_file() and not archive_path.is_symlink(),'RIG_ARCHIVE','Archive must be a regular file.')
    require(archive_path.stat().st_size <= limits['archive_bytes'],'RIG_ARCHIVE_LIMIT','Compressed archive exceeds its byte budget.')
    require(not destination.exists() and not destination.is_symlink(),'RIG_RESTORE_EXISTS','Restore into a new directory.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.rig-restore-',dir=destination.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos=archive.infolist();names={};total=0
            require(len(infos)<=limits['archive_files'],'RIG_ARCHIVE_LIMIT','Archive contains too many entries.')
            for info in infos:
                require(info.orig_filename==info.filename and not info.is_dir(),'RIG_ARCHIVE_PATH','Only regular file entries are accepted.')
                name=portable_name(info.filename);key=name.casefold()
                require(key not in names,'RIG_ARCHIVE_COLLISION','Duplicate or case-aliased archive member.')
                mode=stat.S_IFMT(info.external_attr>>16)
                require(mode in (0,stat.S_IFREG) and not info.flag_bits&1,'RIG_ARCHIVE_TYPE','Links, special files and encrypted entries are unsupported.')
                names[key]=name;total+=info.file_size
                require(total<=limits['archive_bytes'],'RIG_ARCHIVE_LIMIT','Expanded archive exceeds its byte budget.')
            require('inventory.json' in names.values(),'RIG_ARCHIVE_INVENTORY','Archive inventory is missing or uses an aliased name.')
            require(archive.getinfo('inventory.json').file_size <= MAX_JSON_BYTES,
                    'RIG_ARCHIVE_INVENTORY', 'Package inventory exceeds its JSON byte budget.')
            with archive.open('inventory.json') as source:
                inventory = decode_json(source.read(MAX_JSON_BYTES + 1))
            require(isinstance(inventory, dict) and
                    set(inventory) == {'schema_version', 'kind', 'rig_sha256', 'files'} and
                    type(inventory['schema_version']) is int and inventory['schema_version'] == 1 and
                    inventory['kind'] == 'hatch-3d-rig-package' and
                    isinstance(inventory['rig_sha256'], str) and
                    re.fullmatch('[0-9a-f]{64}', inventory['rig_sha256']) is not None and
                    isinstance(inventory['files'], list),
                    'RIG_ARCHIVE_INVENTORY', 'Unsupported package inventory.')
            entries = inventory['files']
            require(len(entries) < limits['archive_files'], 'RIG_ARCHIVE_LIMIT',
                    'Inventory exceeds the file budget.')
            for entry in entries:
                require(isinstance(entry, dict) and set(entry) == {'path', 'sha256', 'bytes'} and
                        isinstance(entry['path'], str) and entry['path'] != 'inventory.json' and
                        isinstance(entry['sha256'], str) and
                        re.fullmatch('[0-9a-f]{64}', entry['sha256']) is not None and
                        type(entry['bytes']) is int and 0 <= entry['bytes'] <= limits['archive_bytes'],
                        'RIG_ARCHIVE_INVENTORY', 'Invalid package file record.')
            expected = [entry['path'] for entry in entries]
            require(len(set(expected))==len(expected) and set(expected)|{'inventory.json'}==set(names.values()),
                    'RIG_ARCHIVE_INVENTORY','Archive and inventory members differ.')
            for entry in entries:
                require(set(entry)=={'path','sha256','bytes'},'RIG_ARCHIVE_INVENTORY','Unexpected inventory fields.')
                name=entry['path'];info=archive.getinfo(name)
                require(type(entry['bytes']) is int and entry['bytes']==info.file_size,'RIG_ARCHIVE_SIZE','Inventory size mismatch.')
                target=safe_path(temporary,name);target.parent.mkdir(parents=True,exist_ok=True)
                digest=hashlib.sha256();written=0
                with archive.open(name) as source,target.open('xb') as output:
                    while True:
                        chunk=source.read(1024*1024)
                        if not chunk:break
                        written+=len(chunk);require(written<=entry['bytes'],'RIG_ARCHIVE_SIZE','Expanded file exceeds inventory.')
                        output.write(chunk);digest.update(chunk)
                require(digest.hexdigest()==entry['sha256'],'RIG_ARCHIVE_HASH','Restored bytes failed their hash.',asset=name)
        rig=load_rig(temporary);require(rig_signature(rig.manifest)==inventory['rig_sha256'],'RIG_ARCHIVE_HASH','Restored rig signature differs.')
        _rename_no_replace(temporary,destination)
        return dict(path=str(destination),rig_sha256=inventory['rig_sha256'],files=len(entries),verified=True)
    finally:
        if temporary.exists():shutil.rmtree(temporary)
