"""Bounded deterministic archives with a verified inventory and atomic restore."""
from pathlib import Path
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from atomic_publish import _rename_no_replace
from rig_contracts import DEFAULT_LIMITS
from rig_errors import atomic_bytes, decode_json, portable_name, require, safe_path, sha256
from rig_models import load_rig, rig_signature


def package_rig(root,destination):
    root=Path(root).resolve();destination=Path(destination).absolute();rig=load_rig(root)
    require(not destination.is_relative_to(root),'RIG_ARCHIVE_DESTINATION','Write the archive outside its source run.')
    entries=[];names=set();total=0
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(),'RIG_ARCHIVE_SYMLINK','Symlinks cannot enter a rig package.',asset=str(path))
        if not path.is_file():continue
        relative=portable_name(path.relative_to(root).as_posix())
        require(relative.casefold() not in names and relative!='inventory.json','RIG_ARCHIVE_COLLISION','Portable filenames collide.')
        names.add(relative.casefold());size=path.stat().st_size;total+=size
        entries.append(dict(path=relative,sha256=sha256(path),bytes=size))
    limits=rig.manifest['limits']
    require(len(entries)+1 <= limits['archive_files'] and total <= limits['archive_bytes'],
            'RIG_ARCHIVE_LIMIT','Archive exceeds file or expanded byte budget.')
    inventory=dict(schema_version=1,kind='hatch-3d-rig-package',rig_sha256=rig_signature(rig.manifest),files=entries)
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.rig-package-',suffix='.zip',dir=destination.parent);os.close(fd)
    try:
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=5) as archive:
            for entry in entries+[dict(path='inventory.json')]:
                name=entry['path'];info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                info.create_system=3;info.external_attr=(stat.S_IFREG|0o644)<<16
                payload=(json.dumps(inventory,sort_keys=True,allow_nan=False).encode() if name=='inventory.json' else safe_path(root,name,exists=True).read_bytes())
                archive.writestr(info,payload)
        os.replace(temp,destination)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return dict(path=str(destination),sha256=sha256(destination),files=len(entries),expanded_bytes=total,rig_sha256=inventory['rig_sha256'])


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
            require('inventory.json' in names,'RIG_ARCHIVE_INVENTORY','Archive inventory is missing.')
            inventory=decode_json(archive.read('inventory.json'))
            require(set(inventory)=={'schema_version','kind','rig_sha256','files'} and inventory['schema_version']==1
                    and inventory['kind']=='hatch-3d-rig-package','RIG_ARCHIVE_INVENTORY','Unsupported package inventory.')
            entries=inventory['files'];expected=[x['path'] for x in entries]
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
