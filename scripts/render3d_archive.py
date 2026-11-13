"""Portable exact-byte Render3D delivery archives; never authenticates a renderer.

Restoration rechecks source closure and decodes images against the recorded
backend identity. This is integrity/provenance verification, not a new render.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from functools import wraps
from rig_errors import RigError
from atomic_publish import _rename_no_replace
from rig_errors import decode_json, portable_name, safe_path, atomic_json
from render3d_contracts import load, require, Render3DError, validate

MAX_FILES=500000
MAX_TOTAL=8*1024**3
MAX_FILE=1024**3
MAX_INDEX=128*1024**2


def _translate(function):
    @wraps(function)
    def call(*args,**kwargs):
        try:return function(*args,**kwargs)
        except Render3DError:raise
        except (OSError,ValueError,RigError,zipfile.BadZipFile) as exc:
            raise Render3DError('invalid-input',str(exc)) from exc
    return call


def _inventory(root):
    result=[];total=0;aliases={};pending=[root]
    while pending:
        directory=pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                require(len(aliases)<MAX_FILES,'storage-exhaustion','Archive file budget exceeded.')
                name=Path(entry.path).relative_to(root).as_posix();portable_name(name)
                key=unicodedata.normalize('NFC',name).casefold()
                require(key not in aliases,'invalid-input','Archive paths alias across platforms.')
                aliases[key]=name
                require(not entry.is_symlink(),'invalid-input','Archive source contains a symlink.')
                if entry.is_dir(follow_symlinks=False):pending.append(Path(entry.path));continue
                require(entry.is_file(follow_symlinks=False),'invalid-input','Archive source is not a regular file.')
                size=entry.stat(follow_symlinks=False).st_size;total+=size
                require(size<=MAX_FILE and total<=MAX_TOTAL,'storage-exhaustion','Archive byte budget exceeded.')
                digest=hashlib.sha256()
                fd=os.open(entry.path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_BINARY',0))
                with os.fdopen(fd,'rb') as stream:
                    require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode),'invalid-input','Archive source changed file type.')
                    count=0
                    for block in iter(lambda:stream.read(1024**2),b''):
                        count+=len(block);require(count<=size,'stale-asset','Archive input grew during read.');digest.update(block)
                require(count==size,'stale-asset','Archive input changed during read.')
                result.append(dict(path=name,bytes=size,sha256=digest.hexdigest()))
    return sorted(result,key=lambda r:r['path'])


def _copy_file(source,target,record):
    target.parent.mkdir(parents=True,exist_ok=True)
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_BINARY',0)
    fd=os.open(source,flags);digest=hashlib.sha256();count=0
    with os.fdopen(fd,'rb') as stream,target.open('xb') as output:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode),'invalid-input','Archive input must be regular.')
        for block in iter(lambda:stream.read(1024**2),b''):
            count+=len(block);require(count<=record['bytes'],'stale-asset','Archive input grew.');digest.update(block);output.write(block)
    require(count==record['bytes'] and digest.hexdigest()==record['sha256'],'stale-asset','Archive input changed.')


def verify_delivery(root):
    from render3d_verify import RenderArtifactVerifier
    root=Path(root)
    require(not root.is_symlink() and root.is_dir(),'invalid-input','Delivery root must be a regular directory.')
    request=load(safe_path(root,'request.json',exists=True),'RenderRequest')
    result=load(safe_path(root,'result.json',exists=True),'RenderResult')
    require(result['status']=='complete','incomplete-output','Only completed deliveries can be archived.')
    for reference in [*result['logs'], result['manifest'], result['verification']['evidence']]:
        require(reference is not None,'incomplete-output','Completed delivery lacks provenance.')
        path=safe_path(root,reference['path'],exists=True)
        require(path.stat().st_size==reference['bytes'] and _hash(path)==reference['sha256'],
                'corrupt-output','Delivery provenance bytes differ from the result.')
    report=RenderArtifactVerifier(root/'snapshot',result['backend']).verify(request,result,root)
    return report


@_translate
def archive_render(delivery_root,destination,*,request=None,snapshot_root=None):
    """Archive a completed delivery, optionally supplying its request/source.

    The ZIP contains a byte inventory and a payload directory. The temporary
    immutable copy is verified before compression and exclusive publication.
    """
    source=Path(delivery_root).absolute();destination=Path(destination).absolute()
    require(not source.is_symlink() and source.is_dir(),'invalid-input','Delivery root must be a directory.')
    require(not destination.exists() and not destination.is_symlink(),'ownership','Archive destination exists.')
    require(not destination.parent.resolve().is_relative_to(source.resolve()),'ownership','Archive cannot be inside its source.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.render-archive-',dir=destination.parent) as temporary:
        stage=Path(temporary);payload=stage/'payload';payload.mkdir()
        for item in _inventory(source):_copy_file(safe_path(source,item['path'],exists=True),safe_path(payload,item['path']),item)
        if request is not None:
            validate(request,'RenderRequest')
            if (payload/'request.json').exists():require(load(payload/'request.json')==request,'stale-asset','Supplied request differs.')
            else:atomic_json(payload/'request.json',request)
        if snapshot_root is not None:
            snap=Path(snapshot_root).absolute();require(not snap.is_symlink(),'invalid-input','Snapshot root cannot be a symlink.')
            require(not (payload/'snapshot').exists(),'ownership','Delivery already contains its snapshot.')
            for item in _inventory(snap):_copy_file(safe_path(snap,item['path'],exists=True),safe_path(payload/'snapshot',item['path']),item)
        verify_delivery(payload)
        inventory=_inventory(payload)
        index=json.dumps(dict(schema_version=1,kind='render3d-archive',files=inventory),sort_keys=True,separators=(',',':')).encode()
        require(len(index)<=MAX_INDEX,'storage-exhaustion','Archive inventory too large.')
        target=stage/'archive.zip'
        with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            archive.writestr('inventory.json',index)
            for item in inventory:
                path=safe_path(payload,item['path'],exists=True)
                archive.write(path,'payload/'+item['path'])
        _rename_no_replace(target,destination)
    return dict(path=str(destination),sha256=_hash(destination),bytes=destination.stat().st_size,files=len(inventory))


def _hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda:stream.read(1024**2),b''):h.update(data)
    return h.hexdigest()


@_translate
def restore_render(archive_path,destination):
    """Restore into a new directory; reject unlisted, aliased or corrupt bytes."""
    archive_path=Path(archive_path);destination=Path(destination).absolute()
    require(not archive_path.is_symlink() and archive_path.is_file(),'invalid-input','Archive must be regular.')
    require(archive_path.stat().st_size<=MAX_TOTAL+MAX_INDEX+256*MAX_FILES,'storage-exhaustion','Archive exceeds byte budget.')
    require(not destination.exists() and not destination.is_symlink(),'ownership','Restore destination exists.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='.render-restore-',dir=destination.parent) as temporary:
            stage=Path(temporary)/'payload';stage.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                infos=archive.infolist();names=[i.filename for i in infos]
                require(len(names)<=MAX_FILES+1 and len(set(names))==len(names),'invalid-input','Archive has too many or duplicate entries.')
                require('inventory.json' in names,'invalid-input','Archive inventory missing.')
                info=archive.getinfo('inventory.json');require(info.compress_type==zipfile.ZIP_STORED and not info.flag_bits&1,'invalid-input','Invalid inventory encoding.');require(info.file_size<=MAX_INDEX,'storage-exhaustion','Archive inventory too large.')
                index=decode_json(archive.read(info),max_bytes=MAX_INDEX)
                require(isinstance(index,dict) and set(index)=={'schema_version','kind','files'} and type(index['schema_version']) is int and index['schema_version']==1 and index['kind']=='render3d-archive' and isinstance(index['files'],list) and len(index['files'])<=MAX_FILES,'invalid-input','Invalid archive inventory.')
                aliases={};records={};total=0
                for item in index['files']:
                    require(isinstance(item,dict) and set(item)=={'path','bytes','sha256'},'invalid-input','Invalid file record.')
                    name=portable_name(item['path']);key=unicodedata.normalize('NFC',name).casefold()
                    require('payload/'+name not in records,'invalid-input','Duplicate archive path.')
                    for n in range(1,len(name.split('/'))+1):
                        prefix='/'.join(name.split('/')[:n]);fold=unicodedata.normalize('NFC',prefix).casefold()
                        require(fold not in aliases or aliases[fold]==prefix,'invalid-input','Archive paths alias.')
                        aliases[fold]=prefix
                    require(type(item['bytes']) is int and 0<=item['bytes']<=MAX_FILE,'storage-exhaustion','Invalid file size.')
                    require(isinstance(item['sha256'],str) and len(item['sha256'])==64 and all(c in '0123456789abcdef' for c in item['sha256']),'invalid-input','Invalid file hash.')
                    total+=item['bytes'];require(total<=MAX_TOTAL,'storage-exhaustion','Archive exceeds expanded byte budget.')
                    records['payload/'+name]=item
                require(set(names)=={'inventory.json',*records},'invalid-input','Archive contains unlisted or missing entries.')
                for name,item in records.items():
                    info=archive.getinfo(name);mode=info.external_attr>>16
                    require(not info.is_dir() and (stat.S_IFMT(mode) in (0,stat.S_IFREG)) and not info.flag_bits&1,'invalid-input','Archive has nonregular/encrypted entries.')
                    require(info.file_size==item['bytes'] and info.compress_type==zipfile.ZIP_STORED,'invalid-input','Archive size or compression differs.')
                    target=safe_path(stage,item['path']);target.parent.mkdir(parents=True,exist_ok=True)
                    digest=hashlib.sha256();count=0
                    with archive.open(info) as stream,target.open('xb') as output:
                        for block in iter(lambda:stream.read(1024**2),b''):
                            count+=len(block);require(count<=item['bytes'],'storage-exhaustion','Expanded file exceeds inventory.');digest.update(block);output.write(block)
                    require(count==item['bytes'] and digest.hexdigest()==item['sha256'],'corrupt-output','Archived file hash differs.')
            report=verify_delivery(stage)
            _rename_no_replace(stage,destination)
            return report
    except (OSError,ValueError,zipfile.BadZipFile) as exc:
        if isinstance(exc,Render3DError):raise
        raise Render3DError('invalid-input',str(exc)) from exc
