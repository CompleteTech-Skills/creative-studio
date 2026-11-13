"""Independent, bounded PNG/EXR verification outside the Blender worker.

Decoded pixels prove image integrity, not correct scene interpretation or artistic
approval. Tests of geometry, lighting and animation remain separate evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import platform
import stat
import struct
import tempfile
import zlib

from render3d_contracts import Render3DError, require, validate, VERSION, frame_times, rational
from rig_errors import RigError, object_hash, safe_path


@contextmanager
def _captured(path, max_bytes):
    """Hash and decode the same privately captured bytes, not a mutable path."""
    path = Path(path)
    try:
        require(not path.is_symlink(), 'corrupt-output', 'Image may not be a symlink.', path=path)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as source, tempfile.TemporaryFile() as frozen:
            require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), 'corrupt-output', 'Image is not a regular file.', path=path)
            digest, size = hashlib.sha256(), 0
            while chunk := source.read(min(1024*1024, max_bytes-size+1)):
                size += len(chunk)
                require(size <= max_bytes, 'storage-exhaustion', 'Image exceeds its verification byte budget.', path=path)
                digest.update(chunk); frozen.write(chunk)
            require(size > 0, 'corrupt-output', 'Image is empty.', path=path)
            frozen.seek(0)
            yield frozen, dict(bytes=size, sha256=digest.hexdigest())
    except Render3DError:
        raise
    except (OSError, ValueError) as exc:
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
        require(kind in (b'IHDR',b'IDAT',b'IEND') or kind[0]&32,
                'unsupported-feature','Unsupported critical PNG chunk.')
        if kind == b'IDAT':
            require(not inflater.eof, 'corrupt-output', 'Trailing compressed PNG stream.')
            expanded=inflater.decompress(payload,header[3]-inflated+1);inflated+=len(expanded)
            require(inflated <= header[3] and not inflater.unconsumed_tail and not inflater.unused_data,
                    'corrupt-output', 'PNG decompressed data exceeds declared dimensions.')
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


def _exr(stream, resolution, bit_depth, memory_bytes, pixels=False):
    try:
        import OpenEXR, Imath
        import numpy as np
    except ImportError as exc:
        raise Render3DError('unsupported-feature', 'EXR verification requires optional OpenEXR and numpy packages.') from exc
    # InputFile reads only the header until channel() is requested. A private
    # captured stream prevents a hash/decode race with worker-owned filenames.
    reader=OpenEXR.InputFile(stream)
    try:
        header=reader.header();window=header['dataWindow']
        width,height=window.max.x-window.min.x+1,window.max.y-window.min.y+1
        require([width,height] == list(resolution),'corrupt-output','EXR dimensions differ from request.')
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
    require(output['format'] in ('png','exr') and pass_name in output['passes'], 'invalid-input','Unsupported output/pass selection.')
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
    def __init__(self,snapshot_root,expected_backend):
        self.snapshot_root=Path(snapshot_root)
        self.expected_backend=deepcopy(expected_backend)

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
        pending['verification']=dict(status='not-run',verifier=None,evidence=None)
        validate(pending,'RenderResult')
        root=Path(root)
        require(not root.is_symlink(),'corrupt-output','Output root may not be a symlink.')
        require(root.is_dir(),'missing-asset','Output root is unavailable.')
        # The request authenticates the snapshot file before the owner rechecks
        # its closure. Decode and hash one captured snapshot byte buffer.
        snapshot_path=self.snapshot_root/'snapshot.json'
        with _captured(snapshot_path,32*1024**2) as (stream,reference):
            from rig_errors import decode_json
            snapshot=validate(decode_json(stream.read()),'RenderSnapshot')
        require(reference == {key:request['snapshot'][key] for key in ('bytes','sha256')},'stale-asset','Snapshot differs from request-bound bytes.')
        adapter=Scene3DAdapter() if snapshot['scope']=='scene3d' else NativeRigAdapter()
        require(verify_snapshot(self.snapshot_root,adapter)==snapshot,'stale-asset','Snapshot changed during independent verification.')
        if isinstance(adapter,Scene3DAdapter):
            adapter.validate_request(self.snapshot_root,request,snapshot)
        require(result['snapshot_sha256']==snapshot['snapshot_sha256'] and result['request_sha256']==object_hash(request),
                'stale-asset','Result request or snapshot identity differs.')
        require(result['backend']==self.expected_backend,'verification-failed','Result backend differs from executed backend identity.')
        frames=list(range(request['frame_range']['start'],request['frame_range']['end']))
        require(result['requested_frames']==frames and not result['failed'] and not result['skipped'],
                'incomplete-output','Result does not cover the complete requested frame sequence.')
        expected={(frame,p) for frame in frames for p in request['settings']['output']['passes']}
        require({(image['frame'],image['pass_name']) for image in result['images']}==expected,
                'incomplete-output','Result lacks the exact requested frame/pass inventory.')
        settings=request['settings'];decoded=[];paths=set();total=0
        try:
            for item in result['images']:
                reference=item['file'];name=reference['path'];suffix=settings['output']['format']
                expected_name=(f'frames/{item["frame"]:06d}.{suffix}' if item['pass_name']=='combined'
                               else f'passes/{item["pass_name"]}/{item["frame"]:06d}.{suffix}')
                require(name==expected_name and name not in paths,'corrupt-output','Image path does not match frame/pass identity.')
                paths.add(name)
                require(item['format']==suffix and item['bit_depth']==settings['output']['bit_depth'] and item['resolution']==settings['resolution'],
                        'corrupt-output','Image declaration differs from request settings.')
                file=safe_path(root,name,exists=True)
                image=verify_image(file,output=settings['output'],resolution=settings['resolution'],pass_name=item['pass_name'],
                    max_bytes=settings['resources']['storage_bytes']-total,memory_bytes=settings['resources']['memory_bytes'])
                require(image['sha256']==reference['sha256'] and image['bytes']==reference['bytes'],
                        'corrupt-output','Image bytes differ from committed reference.',path=name)
                presentation,source=frame_times(request,item['frame'])
                total+=image['bytes'];decoded.append(dict(frame=item['frame'],path=name,
                    presentation_time=rational(presentation),source_time=rational(source),**image))
            actual=set()
            for directory in ('frames','passes'):
                folder=root/directory
                if folder.exists():
                    require(not folder.is_symlink(),'corrupt-output','Image directory may not be a symlink.')
                    for path in folder.rglob('*'):
                        require(not path.is_symlink(),'corrupt-output','Image inventory contains a symlink.')
                        if path.is_file():actual.add(path.relative_to(root).as_posix())
            require(actual==paths,'incomplete-output','Unexpected or missing output images.')
        except RigError as exc:
            raise Render3DError('corrupt-output',str(exc)) from exc
        identity=dict(id='render3d-independent-pixels',version=VERSION,code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),platform=platform.platform())
        return dict(schema_version=1,kind='render3d-verification',status='passed',verifier=identity,
                    request_sha256=object_hash(request),snapshot_sha256=snapshot['snapshot_sha256'],
                    settings_sha256=object_hash(settings),backend=deepcopy(self.expected_backend),images=decoded,
                    decoded_frames=len(frames),decoded_images=len(decoded),output_bytes=total,
                    limitation='Decoded integrity and input binding do not establish semantic scene correctness or visual approval.')


def verify_result(request,result,root,*,snapshot_root,expected_backend):
    return RenderArtifactVerifier(snapshot_root,expected_backend).verify(request,result,root)
