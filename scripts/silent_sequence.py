"""Byte-preserving, independently decoded PNG/EXR image-sequence masters.

No renderer, color conversion, precision conversion or semantic approval is
implemented here. Canonical Render3D owners authenticate native source closure.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import re
import unicodedata

from atomic_publish import _rename_no_replace
import silent_video as media

MAX_FRAME_BYTES = 256 * 1024**2
MAX_TOTAL_BYTES = 2 * 1024**3
MAX_JSON_BYTES = 16 * 1024**2
MAX_MEMORY_BYTES = 512 * 1024**2
_NATIVE_TOKEN = object()
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1024}
SHA = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
OUTPUT = media._obj({'format': {'enum': ['png', 'exr']}, 'bit_depth': {'enum': [8,16,32]},
    'passes': {'const': ['combined']}, 'alpha': {'enum': ['straight','opaque']},
    'color_space': {'enum': ['srgb','linear']}})
FRAME = media._obj({'index': media._int(0,media.MAX_FRAMES-1),
    'source_index': media._int(0,100000), 'source_path': TEXT,
    'path': TEXT, 'sha256': SHA, 'bytes': media._int(1,MAX_FRAME_BYTES)})
SOURCE = media._obj({'schema_version': {'const':1}, 'kind': {'const':'silent-sequence-source'},
    'origin': {'enum':['imported','blender3d']}, 'identity_sha256': SHA,
    'resolution': {'type':'array','minItems':2,'maxItems':2,'items':media._int(1,4096)},
    'fps': media.FPS, 'output': OUTPUT,
    'frames': {'type':'array','minItems':1,'maxItems':media.MAX_FRAMES,'items':FRAME}})


def _relative(name):
    if (not isinstance(name,str) or not 0<len(name)<=1024 or name.startswith('/') or
            any(c in name for c in ('\\',':','\0'))):
        raise ValueError('sequence path must be portable and relative')
    for part in name.split('/'):
        if (part in ('','.','..') or part[-1:] in (' ','.') or any(ord(c)<32 for c in part) or
                re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',part)):
            raise ValueError('sequence path must be portable and relative')
    return name


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    raw = (json.dumps(value,indent=2,allow_nan=False)+'\n').encode()
    if len(raw)>MAX_JSON_BYTES:
        raise ValueError('sequence metadata exceeds byte budget')
    return raw


def _check_source(source):
    media._structural(source,SOURCE,'sequence source')
    media.fps_fraction(source['fps'])
    output=source['output'];w,h=source['resolution']
    if w*h>media.MAX_PIXELS:
        raise ValueError('sequence exceeds pixel budget')
    if (output['format']=='png' and (output['bit_depth'] not in (8,16) or output['color_space']!='srgb') or
            output['format']=='exr' and (output['bit_depth'] not in (16,32) or output['color_space']!='linear')):
        raise ValueError('sequence precision/color contract incompatible with format; no implicit HDR conversion')
    total=0;seen=set();indices=[]
    for i,f in enumerate(source['frames']):
        _relative(f['source_path'])
        if f['index']!=i or f['path']!=f'frames/{i:06d}.{output["format"]}':
            raise ValueError('sequence output indices/names must be contiguous')
        key=unicodedata.normalize('NFC',f['source_path']).casefold()
        if key in seen:
            raise ValueError('duplicate source frame path')
        seen.add(key);indices.append(f['source_index'])
        total+=f['bytes']
    if len(set(indices))!=len(indices) or indices!=list(range(indices[0],indices[0]+len(indices))):
        raise ValueError('source frame indices must be contiguous and ordered')
    if total>MAX_TOTAL_BYTES:
        raise ValueError('sequence exceeds aggregate byte budget')
    _json_bytes(source)
    return source


def schemas():
    request=media._obj({'schema_version':{'const':1},'kind':{'const':'silent-sequence-request'},
        'source':SOURCE,'provenance':{'type':'object'},'audio':media._obj({'policy':{'const':'forbidden'}})})
    artifact=media._obj({'schema_version':{'const':1},'kind':{'const':'silent-image-sequence'},
        'source':SOURCE,'source_sha256':SHA,'request_sha256':SHA,'provenance':{'type':'object'},'provenance_sha256':SHA,
        'decoded':{'type':'array','minItems':1,'maxItems':media.MAX_FRAMES,'items':{'type':'object'}},
        'transform':{'const':'byte-preserving-copy'},'visual_review':{'const':'not-performed'}})
    return {name:{'$schema':'https://json-schema.org/draft/2020-12/schema',**copy.deepcopy(value)}
            for name,value in [('source',SOURCE),('request',request),('artifact',artifact)]}


def make_request(source,provenance):
    source=_check_source(copy.deepcopy(source));_json_bytes(provenance)
    if not isinstance(provenance,dict) or not provenance:
        raise ValueError('explicit sequence provenance required')
    return {'schema_version':1,'kind':'silent-sequence-request','source':source,
            'provenance':copy.deepcopy(provenance),'audio':{'policy':'forbidden'}}


def _read(root, relative, maximum):
    try:
        with media._open_source(root,relative) as stream:
            info=os.fstat(stream.fileno())
            if not 0<info.st_size<=maximum:
                raise ValueError('sequence file size exceeds budget')
            data=stream.read(maximum+1)
            if len(data)!=info.st_size:
                raise ValueError('sequence file changed while reading')
            return data
    except OSError as exc:
        raise ValueError('sequence file cannot be read safely: '+relative) from exc


def _read_json(root,name):
    data=_read(root,name,MAX_JSON_BYTES)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate sequence JSON key')
            result[key]=value
        return result
    try:
        return json.loads(data,object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite sequence JSON')))
    except (ValueError,UnicodeError,RecursionError) as exc:
        raise ValueError('invalid sequence JSON') from exc


def _decoder():
    try:
        from render3d_verify import verify_image
        return verify_image
    except ImportError as exc:
        raise ValueError('canonical Render3D image verifier is unavailable') from exc


def _decode(path,source):
    report=_decoder()(path,output=source['output'],resolution=source['resolution'],
        max_bytes=MAX_FRAME_BYTES,memory_bytes=MAX_MEMORY_BYTES)
    result={key:report[key] for key in ('sha256','bytes','format','resolution','bit_depth','channels',
                                        'requested_color_space','requested_alpha','decoder','decoder_version')}
    result['compression']='deflate'
    if source['output']['format']=='exr':
        # Canonical validation already checked the complete EXR structure and
        # pixels. Inspect this exact captured header only to enforce lossless
        # storage policy; B44/DWA/PXR24 are not advertised as lossless masters.
        import OpenEXR
        data=_read(Path(path).parent,Path(path).name,MAX_FRAME_BYTES)
        if _hash(data)!=report['sha256']:raise ValueError('EXR changed during compression inspection')
        reader=OpenEXR.InputFile(io.BytesIO(data))
        try:compression=reader.header()['compression'].v
        finally:reader.close()
        names={0:'none',1:'rle',2:'zips',3:'zip',4:'piz'}
        if compression not in names:raise ValueError('lossy or unsupported EXR compression cannot be a lossless sequence master')
        result['compression']=names[compression]
    return result


def _verify_frame(root,frame,source):
    data=_read(root,frame['path'],MAX_FRAME_BYTES)
    if len(data)!=frame['bytes'] or _hash(data)!=frame['sha256']:
        raise ValueError('sequence frame checksum/size mismatch')
    decoded=_decode(media._path(root,frame['path']),source)
    if decoded['sha256']!=frame['sha256'] or decoded['bytes']!=frame['bytes']:
        raise ValueError('sequence frame changed during independent decode')
    return decoded


def build_source(root,paths,*,fps,output,identity,source_start=0):
    """Admit imported PNG/EXR masters without claiming native render provenance.

    Color attribution is supplied explicitly by the caller. EXR input must
    actually decode as half/float; this API never converts display PNG to EXR.
    """
    if (not isinstance(paths,list) or not 1<=len(paths)<=media.MAX_FRAMES or
            type(source_start) is not int or not 0<=source_start<=100000-len(paths)):
        raise ValueError('bounded ordered source paths/index required')
    if not isinstance(identity,dict) or not identity:
        raise ValueError('explicit source identity required')
    _json_bytes(identity)
    source={'schema_version':1,'kind':'silent-sequence-source','origin':'imported',
        'identity_sha256':media._digest(identity),'resolution':[1,1], 'fps':copy.deepcopy(fps),
        'output':copy.deepcopy(output),'frames':[]}
    # Decode through the canonical owner; dimensions must first be provided in
    # identity to avoid guessing from a lossy Pillow representation.
    resolution=identity.get('resolution');source['resolution']=copy.deepcopy(resolution)
    total=0
    media._structural(source['output'],OUTPUT,'sequence output')
    media.fps_fraction(fps)
    if (output['format']=='png' and (output['bit_depth'] not in (8,16) or output['color_space']!='srgb') or
            output['format']=='exr' and (output['bit_depth'] not in (16,32) or output['color_space']!='linear')):
        raise ValueError('incompatible image format/precision/color contract')
    if (not isinstance(resolution,list) or len(resolution)!=2 or
            any(type(n) is not int or not 1<=n<=4096 for n in resolution) or resolution[0]*resolution[1]>media.MAX_PIXELS):
        raise ValueError('source resolution exceeds sequence bounds')
    for i,path in enumerate(paths):
        if not isinstance(path,str):raise ValueError('source path must be a string')
        data=_read(root,path,MAX_FRAME_BYTES)
        total+=len(data)
        if total>MAX_TOTAL_BYTES:raise ValueError('sequence exceeds aggregate byte budget')
        source['frames'].append({'index':i,'source_index':source_start+i,'source_path':path,
            'path':f'frames/{i:06d}.{output.get("format", "invalid")}','sha256':_hash(data),'bytes':len(data)})
    _check_source(source)
    for frame in source['frames']:
        report=_decode(media._path(root,frame['source_path']),source)
        if report['sha256']!=frame['sha256']:raise ValueError('source changed during decode')
    return source


def native_source(request,result,render_root,*,snapshot_root,expected_backend):
    """Validate complete native source using canonical snapshot/result owners."""
    if not isinstance(request,dict) or not isinstance(request.get('settings'),dict):
        raise ValueError('native render settings required')
    settings=request['settings'];output=settings.get('output');resolution=settings.get('resolution')
    if not isinstance(output,dict) or not isinstance(resolution,list):
        raise ValueError('native image format and resolution required')
    interval=request.get('frame_range',{})
    start,end=interval.get('start'),interval.get('end')
    if type(start) is not int or type(end) is not int or not 0<=start<end<=100000 or end-start>media.MAX_FRAMES:
        raise ValueError('native frame range exceeds sequence bounds')
    images=result.get('images') if isinstance(result,dict) else None
    if not isinstance(images,list) or len(images)!=end-start:
        raise ValueError('complete combined native inventory required')
    source={'schema_version':1,'kind':'silent-sequence-source','origin':'blender3d',
        'identity_sha256':media._digest({'request':request,'result':result,'backend':expected_backend}),
        'resolution':copy.deepcopy(resolution),'fps':copy.deepcopy(request.get('fps')),
        'output':copy.deepcopy(output),'frames':[]}
    if any(not isinstance(item,dict) or type(item.get('frame')) is not int for item in images):
        raise ValueError('native image records require integer frame indices')
    for i,item in enumerate(sorted(images,key=lambda value:value['frame'])):
        file=item.get('file',{})
        if not isinstance(file,dict):raise ValueError('native image file record required')
        if (item.get('frame')!=start+i or item.get('pass_name')!='combined' or
                file.get('path')!=f'frames/{start+i:06d}.{output.get("format")}'):
            raise ValueError('native source indices/pass/path mismatch')
        source['frames'].append({'index':i,'source_index':start+i,'source_path':file['path'],
            'path':f'frames/{i:06d}.{output.get("format")}','sha256':file.get('sha256'),'bytes':file.get('bytes')})
    _check_source(source)  # Resource admission precedes canonical decoding.
    _json_bytes({'request':request,'result':result,'backend':expected_backend})
    try:
        from render3d_verify import verify_result
    except ImportError as exc:
        raise ValueError('canonical Render3D verifier is unavailable') from exc
    proof=verify_result(request,result,render_root,snapshot_root=snapshot_root,expected_backend=expected_backend)
    return source,{'request':copy.deepcopy(request),'result':copy.deepcopy(result),
                   'expected_backend':copy.deepcopy(expected_backend),'verification':proof}


def _write(root,name,data):
    _relative(name)
    descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:
        parts=name.split('/')
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        partial='.'+parts[-1]+'.partial'
        try:
            fd=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=descriptor)
            with os.fdopen(fd,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            _rename_no_replace(partial,parts[-1],source_dir_fd=descriptor,destination_dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            try:os.unlink(partial,dir_fd=descriptor)
            except FileNotFoundError:pass
    finally:os.close(descriptor)


def _clear_partial(root,name):
    """Remove only a known request-owned incomplete write, never a cache frame."""
    path=Path(root)/name
    if path.exists() or path.is_symlink():
        try:
            with media._open_source(root,name) as stream:
                if os.fstat(stream.fileno()).st_size>MAX_FRAME_BYTES:
                    raise ValueError('incomplete sequence write exceeds byte budget')
        except OSError as exc:raise ValueError('unsafe incomplete sequence write') from exc
        path.unlink()


def _inventory(root,source,*,staging=False):
    expected={f['path'] for f in source['frames']}
    actual=set()
    frame_dir=Path(root)/'frames'
    if frame_dir.is_symlink() or not frame_dir.is_dir():raise ValueError('unsafe/missing sequence frame directory')
    for path in frame_dir.iterdir():
        if path.is_symlink() or not path.is_file():raise ValueError('nonregular sequence frame entry')
        actual.add('frames/'+path.name)
        if len(actual)>len(expected):raise ValueError('unexpected sequence frame')
    if (not staging and actual!=expected) or not actual<=expected:
        raise ValueError('missing or unexpected sequence frames')


def verify_sequence(root,*,expected_receipt_sha256=None):
    """Decode every exported image and verify portable inventory/identity.

    Owner evidence is retained as attributed source evidence; this offline
    verifier does not assert access to an absent original native snapshot.
    """
    root=Path(root)
    with media._pin_directory(root) as descriptor:
        pinned=Path('/proc/self/fd')/str(descriptor)
        raw=_read(pinned,'sequence.json',MAX_JSON_BYTES)
        if expected_receipt_sha256 is not None and _hash(raw)!=expected_receipt_sha256:
            raise ValueError('sequence receipt identity changed')
        report=_read_json(pinned,'sequence.json')
        if not isinstance(report,dict) or set(report)!={'schema_version','kind','source','source_sha256','provenance','provenance_sha256','request_sha256','decoded','transform','visual_review'}:
            raise ValueError('unsupported sequence receipt fields')
        if (report['schema_version']!=1 or report['kind']!='silent-image-sequence' or
                report['transform']!='byte-preserving-copy' or report['visual_review']!='not-performed'):
            raise ValueError('invalid sequence receipt')
        if set(os.listdir(pinned))!={'lock','plan.json','frames','sequence.json'}:
            raise ValueError('unexpected sequence root inventory')
        if _read_json(pinned,'plan.json')!={'source':report['source'],'provenance':report['provenance']}:
            raise ValueError('sequence staging identity differs from final receipt')
        source=_check_source(report['source'])
        if report['request_sha256']!=media._digest(make_request(source,report['provenance'])):
            raise ValueError('sequence request identity changed')
        if media._digest(source)!=report['source_sha256'] or media._digest(report['provenance'])!=report['provenance_sha256']:
            raise ValueError('sequence source/provenance identity mismatch')
        if source['origin']=='blender3d':
            proof=report['provenance']
            if not isinstance(proof,dict) or set(proof)!={'request','result','expected_backend','verification'}:
                raise ValueError('native sequence source evidence missing')
            identity=media._digest({'request':proof['request'],'result':proof['result'],'backend':proof['expected_backend']})
            verification=proof['verification']
            if (identity!=source['identity_sha256'] or verification.get('status')!='passed' or
                    verification.get('backend')!=proof['expected_backend'] or
                    verification.get('request_sha256')!=media._digest(proof['request'])):
                raise ValueError('native sequence provenance identity changed')
        _inventory(pinned,source)
        if not isinstance(report['decoded'],list) or len(report['decoded'])!=len(source['frames']):
            raise ValueError('sequence decoded metadata inventory mismatch')
        for f,prior in zip(source['frames'],report['decoded']):
            current=_verify_frame(pinned,f,source)
            # Decoder version may change independently of preserved pixel data.
            if any(current.get(k)!=v for k,v in prior.items() if k not in ('decoder','decoder_version')):
                raise ValueError('sequence decoded metadata changed')
            if set(prior)!=set(current):raise ValueError('sequence decoded metadata fields differ')
        return report


def export_sequence(source,root,destination,*,provenance,stage_name=None,cancel=None,_native_token=None):
    """Export exact bytes; interrupted staging is reusable on explicit retry.

    Destination is immutable and exclusive. Staging is a sibling directory
    bound to the complete source/provenance identity, not a guessed frame cache.
    Damaged or foreign staging is rejected, never overwritten.
    """
    source=_check_source(copy.deepcopy(source));_json_bytes(provenance)
    if source['origin']=='blender3d' and _native_token is not _NATIVE_TOKEN:
        raise ValueError('native sources require export_native_sequence and canonical closure verification')
    if not isinstance(provenance,dict) or not provenance:raise ValueError('explicit sequence provenance required')
    plan={'source':source,'provenance':copy.deepcopy(provenance)}
    destination=media._destination(destination)
    if stage_name is None:stage_name='.'+destination.name+'.sequence-stage'
    if not isinstance(stage_name,str) or not stage_name or '/' in stage_name or '\\' in stage_name or stage_name in ('.','..') or stage_name==destination.name:
        raise ValueError('sequence stage must be a separate sibling basename')
    with media._pin_directory(destination.parent) as parent_fd:
        parent=Path('/proc/self/fd')/str(parent_fd)
        try:os.mkdir(stage_name,dir_fd=parent_fd)
        except FileExistsError:pass
        stage_fd=os.open(stage_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent_fd)
        try:
            stage=Path('/proc/self/fd')/str(stage_fd)
            lock_fd=os.open('lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=stage_fd)
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):raise ValueError('invalid sequence stage lock')
                import fcntl
                try:fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError as exc:raise ValueError('sequence staging is already owned') from exc
                marker=stage/'plan.json'
                if marker.exists():
                    if _read_json(stage,'plan.json')!=plan:raise ValueError('staging belongs to another source/provenance')
                    if not (stage/'frames').exists():os.mkdir('frames',dir_fd=stage_fd)
                    for f in source['frames']:
                        _clear_partial(stage,'frames/.'+Path(f['path']).name+'.partial')
                    _clear_partial(stage,'.sequence.json.partial')
                else:
                    names=set(os.listdir(stage))
                    if names=={'lock','.plan.json.partial'}:
                        partial=_read(stage,'.plan.json.partial',MAX_JSON_BYTES)
                        if partial!=_json_bytes(plan):raise ValueError('incomplete staging plan does not match request')
                        (stage/'.plan.json.partial').unlink()
                    elif names!={'lock'}:raise ValueError('unrecognized sequence staging')
                    _write(stage,'plan.json',_json_bytes(plan));os.mkdir('frames',dir_fd=stage_fd)
                if not set(os.listdir(stage))<={'lock','plan.json','frames','sequence.json'}:
                    raise ValueError('unrecognized sequence staging entry')
                os.fsync(stage_fd)
                _inventory(stage,source,staging=True)
                for f in source['frames']:
                    if cancel is not None and cancel():raise ValueError('sequence export cancelled; verified staging retained')
                    if (stage/f['path']).exists():_verify_frame(stage,f,source);continue
                    data=_read(root,f['source_path'],MAX_FRAME_BYTES)
                    if len(data)!=f['bytes'] or _hash(data)!=f['sha256']:
                        raise ValueError('source frame changed after sequence admission')
                    _write(stage,f['path'],data)
                    _verify_frame(stage,f,source)
                decoded=[_verify_frame(stage,f,source) for f in source['frames']]
                report={'schema_version':1,'kind':'silent-image-sequence','source':source,
                    'source_sha256':media._digest(source),'request_sha256':media._digest(make_request(source,plan['provenance'])),
                    'provenance':plan['provenance'],
                    'provenance_sha256':media._digest(plan['provenance']),'decoded':decoded,
                    'transform':'byte-preserving-copy','visual_review':'not-performed'}
                if (stage/'sequence.json').exists():
                    if _read_json(stage,'sequence.json')!=report:raise ValueError('staged sequence receipt differs')
                else:_write(stage,'sequence.json',_json_bytes(report))
                # Independent final verification uses a pinned directory, too.
                _inventory(stage,source)
                if cancel is not None and cancel():raise ValueError('sequence export cancelled before publication')
                _rename_no_replace(stage_name,destination.name,source_dir_fd=parent_fd,destination_dir_fd=parent_fd)
                os.fsync(parent_fd)
                return report
            finally:os.close(lock_fd)
        finally:os.close(stage_fd)


def export_native_sequence(request,result,render_root,destination,*,snapshot_root,expected_backend,**options):
    source,proof=native_source(request,result,render_root,snapshot_root=snapshot_root,expected_backend=expected_backend)
    return export_sequence(source,render_root,destination,provenance=proof,_native_token=_NATIVE_TOKEN,**options)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    export=commands.add_parser('export-native');export.add_argument('--request',required=True,type=Path)
    export.add_argument('--result',required=True,type=Path);export.add_argument('--root',required=True,type=Path)
    export.add_argument('--snapshot-root',required=True,type=Path);export.add_argument('--backend',required=True,type=Path)
    export.add_argument('--destination',required=True,type=Path)
    verify=commands.add_parser('verify');verify.add_argument('--root',required=True,type=Path);verify.add_argument('--receipt-sha256')
    args=parser.parse_args(argv)
    try:
        if args.command=='verify':report=verify_sequence(args.root,expected_receipt_sha256=args.receipt_sha256)
        else:
            load=lambda p:_read_json(p.parent,p.name)
            report=export_native_sequence(load(args.request),load(args.result),args.root,args.destination,
                snapshot_root=args.snapshot_root,expected_backend=load(args.backend))
        print(json.dumps(report,indent=2,allow_nan=False))
    except (ValueError,OSError) as exc:parser.exit(2,str(exc)+'\n')


if __name__=='__main__':main()
