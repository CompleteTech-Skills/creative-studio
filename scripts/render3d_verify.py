"""Independent, bounded PNG/EXR verification outside the Blender worker.

Decoded pixels prove image integrity, not correct scene interpretation or artistic
approval. Tests of geometry, lighting and animation remain separate evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
import platform
import struct
import tempfile
import zlib

from render3d_contracts import Render3DError, require, validate, VERSION, frame_times, rational, definitions, _validator
from rig_errors import RigError, object_hash, open_payload, normalize_root


@contextmanager
def captured_file_ref(root, reference, *, max_bytes=256*1024**2):
    """Yield private, hash-bound FileRef bytes through the canonical root reader.

    The stream is rewound and valid only within this context. Active directory
    capabilities are interpreted exclusively by rig_errors.open_payload.
    """
    from rig_errors import open_payload
    validate(reference,'FileRef')
    require(type(max_bytes) is int and 0<=max_bytes<=2**40,
            'invalid-input','Artifact byte budget must be a bounded nonnegative integer.')
    require(reference['bytes']<=max_bytes,'storage-exhaustion','Artifact exceeds its verification byte budget.')
    # JSON Schema integers include whole-valued JSON numbers such as 3.0.
    # Normalize only the private I/O count; preserve the authored reference.
    expected_bytes=int(reference['bytes'])
    try:
        with tempfile.TemporaryFile() as frozen:
            digest=hashlib.sha256();size=0
            with open_payload(root,reference['path']) as (source,info):
                require(info.st_size==reference['bytes'],'corrupt-output','Artifact size differs from its reference.')
                while chunk:=source.read(min(1024*1024,expected_bytes-size+1)):
                    size+=len(chunk)
                    require(size<=reference['bytes'],'corrupt-output','Artifact grew beyond its reference.')
                    digest.update(chunk);frozen.write(chunk)
            require(size==reference['bytes'] and digest.hexdigest()==reference['sha256'],
                    'corrupt-output','Artifact bytes differ from their committed reference.')
            frozen.seek(0)
            yield frozen,dict(path=reference['path'],bytes=size,sha256=digest.hexdigest())
    except Render3DError:
        raise
    except (RigError,OSError,ValueError) as exc:
        raise Render3DError('corrupt-output',str(exc),path=reference['path']) from exc


def read_file_ref(root,reference,*,max_bytes=32*1024**2,memory_bytes=32*1024**2):
    """Return verified captured bytes for bounded semantic manifest consumers."""
    require(type(memory_bytes) is int and 0<=memory_bytes<=2**40,
            'invalid-input','Artifact memory budget must be a bounded nonnegative integer.')
    validate(reference,'FileRef')
    require(reference['bytes']<=memory_bytes,'system-memory','Artifact exceeds the semantic reader memory budget.')
    with captured_file_ref(root,reference,max_bytes=max_bytes) as (stream,_):
        return stream.read(int(reference['bytes']))


@contextmanager
def _captured(path, max_bytes):
    """Hash and decode the same privately captured bytes, not a mutable path."""
    path = Path(path)
    try:
        with tempfile.TemporaryFile() as frozen:
            digest, size = hashlib.sha256(), 0
            with open_payload(path.parent,path.name) as (source,_):
                while chunk := source.read(min(1024*1024, max_bytes-size+1)):
                    size += len(chunk)
                    require(size <= max_bytes, 'storage-exhaustion', 'Image exceeds its verification byte budget.', path=path)
                    digest.update(chunk); frozen.write(chunk)
            require(size > 0, 'corrupt-output', 'Image is empty.', path=path)
            frozen.seek(0)
            yield frozen, dict(bytes=size, sha256=digest.hexdigest())
    except Render3DError:
        raise
    except (RigError, OSError, ValueError) as exc:
        raise Render3DError('corrupt-output', str(exc), path=path) from exc


def _png(stream, resolution, bit_depth, memory_bytes, pixels=False):
    try:
        import png
        import numpy as np
    except ImportError as exc:
        raise Render3DError('unsupported-feature', 'PNG verification requires the optional pypng and numpy packages.') from exc
    # Strict structure and bounded inflate before the independent PNG decoder.
    require(stream.read(8) == b'\x89PNG\r\n\x1a\n', 'corrupt-output', 'Not a PNG file.')
    header, ended, count, inflated, inflater = None, False, 0, 0, zlib.decompressobj()
    idat_started,idat_finished=False,False
    while not ended:
        prefix = stream.read(8)
        require(len(prefix) == 8, 'corrupt-output', 'Truncated PNG chunk.')
        length, kind = struct.unpack('>I4s', prefix)
        require(length <= memory_bytes, 'system-memory', 'PNG chunk exceeds verification memory budget.')
        payload = stream.read(length); checksum = stream.read(4)
        require(len(payload) == length and len(checksum) == 4, 'corrupt-output', 'Truncated PNG payload.')
        require(zlib.crc32(kind+payload) & 0xffffffff == struct.unpack('>I',checksum)[0], 'corrupt-output', 'PNG CRC mismatch.')
        if count == 0:
            require(kind == b'IHDR' and length == 13, 'corrupt-output', 'PNG must start with IHDR.')
            width,height,depth,color,compression,filtering,interlace = struct.unpack('>IIBBBBB',payload)
            require([width,height] == list(resolution) and depth == bit_depth, 'corrupt-output', 'PNG dimensions or precision differ from request.')
            require(color in (2,6) and compression == filtering == interlace == 0,
                    'unsupported-feature', 'PNG verification supports noninterlaced RGB/RGBA output.')
            channels = 4 if color == 6 else 3
            expected = height*(1+width*channels*(depth//8))
            require(width*height*channels*8 <= memory_bytes and expected <= memory_bytes,
                    'system-memory', 'PNG decoded pixels exceed memory budget.')
            if pixels:
                require(width*height*channels*24 <= memory_bytes,'system-memory','PNG preview arrays exceed memory budget.')
            header=(width,height,channels,expected)
        elif kind == b'IHDR':
            raise Render3DError('corrupt-output','Duplicate PNG header.')
        require(kind not in (b'acTL',b'fcTL',b'fdAT'),'corrupt-output','Animated PNG is not a single render frame.')
        require(kind != b'tRNS','unsupported-feature','PNG color-key transparency is not supported; use explicit RGBA.')
        require(kind in (b'IHDR',b'IDAT',b'IEND') or kind[0]&32,
                'unsupported-feature','Unsupported critical PNG chunk.')
        if kind == b'IDAT':
            require(not idat_finished,'corrupt-output','PNG IDAT chunks must be consecutive.')
            idat_started=True
            require(not inflater.eof, 'corrupt-output', 'Trailing compressed PNG stream.')
            expanded=inflater.decompress(payload,header[3]-inflated+1);inflated+=len(expanded)
            require(inflated <= header[3] and not inflater.unconsumed_tail and not inflater.unused_data,
                    'corrupt-output', 'PNG decompressed data exceeds declared dimensions.')
        elif idat_started:
            idat_finished=True
        if kind == b'IEND':
            require(length == 0 and inflater.eof and inflated == header[3], 'corrupt-output', 'PNG pixel stream is incomplete.')
            ended=True
        count+=1
    require(not stream.read(1), 'corrupt-output', 'Unexpected bytes after PNG end.')
    stream.seek(0)
    width,height,rows,metadata=png.Reader(file=stream).read()
    require(metadata['planes'] == header[2] and metadata['bitdepth'] == bit_depth,
            'corrupt-output', 'PNG decoder metadata differs from header.')
    names=list('RGBA' if header[2] == 4 else 'RGB')
    minima=np.full(len(names),np.inf);maxima=np.full(len(names),-np.inf);row_count=0
    pixel_rows=[]
    for row in rows:
        values=np.asarray(row,dtype=np.float64).reshape((width,len(names))) / ((1<<bit_depth)-1)
        minima=np.minimum(minima,values.min(axis=0));maxima=np.maximum(maxima,values.max(axis=0));row_count+=1
        if pixels:pixel_rows.append(values)
    require(row_count == height,'corrupt-output','PNG row count differs from dimensions.')
    result=dict(channels={name:dict(type='uint'+str(bit_depth),minimum=float(lo),maximum=float(hi)) for name,lo,hi in zip(names,minima,maxima)},
                decoder='pypng',decoder_version=png.__version__, resolution=[width,height],bit_depth=bit_depth)
    if pixels:
        data=np.stack(pixel_rows);result['_pixels']={name:data[:,:,index] for index,name in enumerate(names)}
    return result


def _exr_structure(stream,resolution,memory_bytes):
    """Admit flat scanline EXR and account for every serialized chunk byte."""
    stream.seek(0,2);size=stream.tell();stream.seek(0)
    prefix=stream.read(8)
    require(len(prefix)==8,'corrupt-output','Truncated EXR prefix.')
    magic,version=struct.unpack('<II',prefix)
    require(magic==20000630,'corrupt-output','Not an OpenEXR file.')
    require(version&255==2 and version&~0x4ff==0,
            'unsupported-feature','Only single-part flat scanline EXR version 2 is supported.')
    def terminated():
        value=bytearray()
        while True:
            byte=stream.read(1)
            require(byte,'corrupt-output','Truncated EXR header.')
            if byte==b'\0':return bytes(value)
            value.extend(byte)
            require(len(value)<=255,'corrupt-output','EXR header name exceeds bounds.')
    attributes={}
    while True:
        name=terminated()
        if not name:break
        type_name=terminated();raw=stream.read(4)
        require(len(raw)==4,'corrupt-output','Truncated EXR attribute size.')
        length=struct.unpack('<I',raw)[0]
        require(length<=min(memory_bytes,16*1024**2) and stream.tell()+length<=size,
                'corrupt-output','EXR attribute exceeds bounds.')
        require(name not in attributes,'corrupt-output','Duplicate EXR attribute.')
        attributes[name]=(type_name,stream.read(length))
        require(stream.tell()<=min(memory_bytes,16*1024**2),'system-memory','EXR header exceeds budget.')
    require(attributes.get(b'compression',(None,None))[0]==b'compression',
            'corrupt-output','Missing EXR compression attribute.')
    raw=attributes[b'compression'][1]
    require(len(raw)==1,'corrupt-output','Malformed EXR compression attribute.')
    block_rows={0:1,1:1,2:1,3:16,4:32,5:16,6:32,7:32,8:32,9:256}
    require(raw[0] in block_rows,'unsupported-feature','EXR compression is outside the verified scanline subset.')
    rows=block_rows[raw[0]];chunks=(resolution[1]+rows-1)//rows
    table=stream.read(chunks*8)
    require(len(table)==chunks*8,'corrupt-output','Truncated EXR chunk table.')
    boundary=stream.tell();offsets=sorted(struct.unpack('<'+'Q'*chunks,table));seen=set()
    for offset in offsets:
        require(offset==boundary,'corrupt-output','EXR chunk table contains gaps, aliases or unreferenced bytes.')
        stream.seek(offset);raw=stream.read(8)
        require(len(raw)==8,'corrupt-output','Truncated EXR chunk.')
        line,length=struct.unpack('<iI',raw)
        require(0<=line<resolution[1] and line%rows==0 and line not in seen,
                'corrupt-output','Invalid EXR scanline chunk.')
        seen.add(line);boundary=offset+8+length
        require(boundary<=size,'corrupt-output','Truncated EXR pixel payload.')
    require(boundary==size,'corrupt-output','Unexpected bytes after EXR chunks.')
    stream.seek(0)


def _exr(stream, resolution, bit_depth, memory_bytes, pixels=False):
    try:
        import OpenEXR, Imath
        import numpy as np
    except ImportError as exc:
        raise Render3DError('unsupported-feature', 'EXR verification requires optional OpenEXR and numpy packages.') from exc
    # InputFile reads only the header until channel() is requested. A private
    # captured stream prevents a hash/decode race with worker-owned filenames.
    _exr_structure(stream,resolution,memory_bytes)
    reader=OpenEXR.InputFile(stream)
    try:
        header=reader.header();window=header['dataWindow']
        width,height=window.max.x-window.min.x+1,window.max.y-window.min.y+1
        require([width,height] == list(resolution),'corrupt-output','EXR dimensions differ from request.')
        display=header['displayWindow']
        require(window.min.x==window.min.y==display.min.x==display.min.y==0
                and window.max.x==display.max.x==resolution[0]-1
                and window.max.y==display.max.y==resolution[1]-1,
                'corrupt-output','EXR data/display windows must match the complete requested frame.')
        channels=header['channels']
        require(0 < len(channels) <= 64,'corrupt-output','EXR channel count is outside supported bounds.')
        require(width*height*4*len(channels) <= memory_bytes,'system-memory','EXR decoded channels exceed memory budget.')
        if pixels:
            require(width*height*8*len(channels) <= memory_bytes,'system-memory','EXR preview arrays exceed memory budget.')
        expected=Imath.PixelType.HALF if bit_depth == 16 else Imath.PixelType.FLOAT
        result={};pixel_channels={}
        for name,channel in sorted(channels.items()):
            require(channel.type.v == expected and channel.xSampling == channel.ySampling == 1,
                    'corrupt-output','EXR channel precision or sampling differs from request.')
            data=reader.channel(name,Imath.PixelType(Imath.PixelType.FLOAT))
            require(len(data) == width*height*4,'corrupt-output','EXR channel is incomplete.')
            values=np.frombuffer(data,dtype=np.float32)
            require(np.isfinite(values).all(),'corrupt-output','EXR contains nonfinite pixels.')
            result[name]=dict(type='half' if bit_depth==16 else 'float',minimum=float(values.min()),maximum=float(values.max()))
            if pixels:pixel_channels[name]=values.reshape((height,width))
        report=dict(channels=result, decoder='OpenEXR',decoder_version=OpenEXR.__version__,resolution=[width,height],bit_depth=bit_depth)
        if pixels:report['_pixels']=pixel_channels
        return report
    finally:
        reader.close()


def verify_image(path, *, output, resolution, pass_name='combined', max_bytes=1024**3, memory_bytes=1024**3, _pixels=False):
    """Decode one actual output. Pixel ranges do not establish scene correctness."""
    from rig_errors import finite_json
    try:
        finite_json(output)
        error=next(iter(_validator('RenderSettings').evolve(schema=definitions()['RenderSettings']['properties']['output']).iter_errors(output)),None)
        require(error is None,'invalid-input',error.message if error else '')
        require(type(resolution) in (list,tuple) and len(resolution)==2
                and all(type(n) is int and 1<=n<=16384 for n in resolution),
                'invalid-input','Image resolution must contain two bounded positive integers.')
        require(all(type(n) is int and 1<=n<=2**40 for n in (max_bytes,memory_bytes)),
                'invalid-input','Image verification budgets must be bounded positive integers.')
        require(pass_name in output['passes'],'invalid-input','Unsupported output/pass selection.')
    except RigError as exc:
        raise Render3DError('invalid-input',str(exc)) from exc
    try:
        with _captured(path,max_bytes) as (stream,identity):
            report=(_png if output['format']=='png' else _exr)(stream,resolution,output['bit_depth'],memory_bytes,_pixels)
        channels=report['channels']
        if pass_name in ('combined','albedo'):
            require(set(channels) == set('RGBA') if output['alpha']=='straight' else set(channels) in (set('RGB'),set('RGBA')),
                    'corrupt-output','Color output lacks the required RGB/alpha channels.')
        elif pass_name == 'normal':
            require(set(channels) in (set('XYZ'),set('RGB'),set('RGBA')), 'corrupt-output','Normal pass lacks vector channels.')
            require(all(value['minimum'] >= -1.00001 and value['maximum'] <= 1.00001
                        for name,value in channels.items() if name != 'A'),
                    'corrupt-output','Normal vectors exceed normalized component bounds.')
        elif pass_name == 'depth':
            require(set(channels) in ({'Z'},{'V'},{'R'}), 'corrupt-output','Depth pass must have one scalar channel.')
            require(all(value['minimum'] >= 0 for value in channels.values()),'corrupt-output','Depth must be nonnegative.')
        if 'A' in channels:
            alpha=channels['A']
            require(alpha['minimum'] >= 0 and alpha['maximum'] <= 1,'corrupt-output','Alpha pixels lie outside [0,1].')
            require(output['alpha'] != 'opaque' or alpha['minimum'] == 1,'corrupt-output','Opaque output contains transparency.')
        return dict(**identity,**report,format=output['format'],pass_name=pass_name,
                    requested_color_space=output['color_space'],requested_alpha=output['alpha'])
    except Render3DError:
        raise
    except Exception as exc:
        # Decoder-specific errors are not allowed to leak as worker crashes.
        raise Render3DError('corrupt-output',str(exc),path=path) from exc


def decode_image(path, **options):
    """Return (verified metadata, normalized channel arrays) for bounded previews.

    PNG values remain display encoded; EXR values remain scene linear or raw
    data-pass values. The caller must apply explicit display mappings.
    """
    report=verify_image(path,_pixels=True,**options)
    pixels=report.pop('_pixels')
    return report,pixels


class RenderArtifactVerifier:
    def __init__(self,snapshot_root,expected_backend,*,max_total_bytes=None):
        # Preserve the lexical root until canonical payload admission; ordinary
        # symlink ancestors must never gain authority through path resolution.
        self.snapshot_root=Path(snapshot_root)
        self.expected_backend=deepcopy(expected_backend)
        self.max_total_bytes=max_total_bytes

    def verify(self,request,result,root):
        """Verify all expected frames, including a provisional unverified result.

        Result publication and evidence-file writing belong to the caller. The
        returned report must be committed before setting final status complete.
        """
        from render3d_snapshot import verify_snapshot,Scene3DAdapter,NativeRigAdapter
        validate(request,'RenderRequest')
        require(isinstance(result,dict) and result.get('status') in ('partial','complete'),
                'incomplete-output','Only a complete candidate sequence can be verified.')
        validate(result.get('verification'),'Verification')
        pending=deepcopy(result);pending['status']='partial'
        validate(pending,'RenderResult')
        root=Path(root)
        require(root.is_dir(),'missing-asset','Output root is unavailable.')
        # The request authenticates the snapshot file before the owner rechecks
        # its closure. Decode and hash one captured snapshot byte buffer.
        snapshot_path=self.snapshot_root/'snapshot.json'
        with _captured(snapshot_path,32*1024**2) as (stream,reference):
            from rig_errors import decode_json
            snapshot=validate(decode_json(stream.read()),'RenderSnapshot')
        # Admission above has already rejected ordinary symlink ancestors.
        # Normalize only now, retaining any explicitly active descriptor anchor.
        self.snapshot_root=normalize_root(self.snapshot_root)
        require(reference == {key:request['snapshot'][key] for key in ('bytes','sha256')},'stale-asset','Snapshot differs from request-bound bytes.')
        adapter=Scene3DAdapter() if snapshot['scope']=='scene3d' else NativeRigAdapter()
        verified=(verify_snapshot(self.snapshot_root,adapter) if self.max_total_bytes is None else
                  verify_snapshot(self.snapshot_root,adapter,max_total_bytes=self.max_total_bytes))
        require(verified==snapshot,'stale-asset','Snapshot changed during independent verification.')
        if isinstance(adapter,Scene3DAdapter):
            try:
                adapter.validate_request(self.snapshot_root,request,snapshot)
            except (RigError,OSError) as exc:
                raise Render3DError('stale-asset',str(exc)) from exc
        require(result['snapshot_sha256']==snapshot['snapshot_sha256'] and result['request_sha256']==object_hash(request),
                'stale-asset','Result request or snapshot identity differs.')
        require(result['backend']==self.expected_backend,'verification-failed','Result backend differs from executed backend identity.')
        frames=list(range(request['frame_range']['start'],request['frame_range']['end']))
        require(result['requested_frames']==frames and not result['failed'] and not result['skipped'],
                'incomplete-output','Result does not cover the complete requested frame sequence.')
        expected={(frame,p) for frame in frames for p in request['settings']['output']['passes']}
        require({(image['frame'],image['pass_name']) for image in result['images']}==expected,
                'incomplete-output','Result lacks the exact requested frame/pass inventory.')
        settings=request['settings'];decoded=[];paths=set();total=0;artifacts=[]
        try:
            for item in result['images']:
                reference=item['file'];name=reference['path'];suffix=settings['output']['format']
                expected_name=(f'frames/{item["frame"]:06d}.{suffix}' if item['pass_name']=='combined'
                               else f'passes/{item["pass_name"]}/{item["frame"]:06d}.{suffix}')
                require(name==expected_name and name not in paths,'corrupt-output','Image path does not match frame/pass identity.')
                paths.add(name)
                require(item['format']==suffix and item['bit_depth']==settings['output']['bit_depth'] and item['resolution']==settings['resolution'],
                        'corrupt-output','Image declaration differs from request settings.')
                # Do not resolve ordinary root ancestors before the canonical
                # reader can reject them. Explicit capabilities remain lexical.
                file=root/name
                image=verify_image(file,output=settings['output'],resolution=settings['resolution'],pass_name=item['pass_name'],
                    max_bytes=settings['resources']['storage_bytes']-total,memory_bytes=settings['resources']['memory_bytes'])
                require(image['sha256']==reference['sha256'] and image['bytes']==reference['bytes'],
                        'corrupt-output','Image bytes differ from committed reference.',path=name)
                presentation,source=frame_times(request,item['frame'])
                total+=image['bytes'];decoded.append(dict(frame=item['frame'],path=name,
                    presentation_time=rational(presentation),source_time=rational(source),**image))
            for reference in result.get('artifacts',[]):
                remaining=settings['resources']['storage_bytes']-total
                require(remaining>=0,'storage-exhaustion','Output verification exceeds its storage budget.')
                with captured_file_ref(root,reference,max_bytes=min(256*1024**2,remaining)) as (_,record):
                    artifacts.append(record);total+=record['bytes']
            root=normalize_root(root)
            actual=set()
            for directory in ('frames','passes'):
                folder=root/directory
                if folder.exists():
                    require(not folder.is_symlink(),'corrupt-output','Image directory may not be a symlink.')
                    for path in folder.rglob('*'):
                        require(not path.is_symlink(),'corrupt-output','Image inventory contains a symlink.')
                        if path.is_dir():
                            relative=path.relative_to(root).parts
                            require(len(relative)==2 and relative[0]=='passes'
                                    and relative[1] in settings['output']['passes'],
                                    'corrupt-output','Unexpected image directory.')
                        elif path.is_file():
                            actual.add(path.relative_to(root).as_posix())
                            require(len(actual)<=len(paths),'incomplete-output','Unexpected extra image files.')
                        else:
                            raise Render3DError('corrupt-output','Nonregular entry in image inventory.')
            require(actual==paths,'incomplete-output','Unexpected or missing output images.')
        except RigError as exc:
            raise Render3DError('corrupt-output',str(exc)) from exc
        identity=dict(id='render3d-independent-pixels',version=VERSION,code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),platform=platform.platform())
        return dict(schema_version=1,kind='render3d-verification',status='passed',verifier=identity,
                    request_sha256=object_hash(request),snapshot_sha256=snapshot['snapshot_sha256'],
                    settings_sha256=object_hash(settings),backend=deepcopy(self.expected_backend),images=decoded,
                    artifacts=artifacts,
                    decoded_frames=len(frames),decoded_images=len(decoded),output_bytes=total,
                    limitation='Decoded integrity and input binding do not establish semantic scene correctness or visual approval.')


def verify_result(request,result,root,*,snapshot_root,expected_backend,max_total_bytes=None):
    return RenderArtifactVerifier(snapshot_root,expected_backend,max_total_bytes=max_total_bytes).verify(request,result,root)
