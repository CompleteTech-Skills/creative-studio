"""Independent, bounded PNG/EXR verification outside the Blender worker.

Decoded pixels prove image integrity, not correct scene interpretation or artistic
approval. Tests of geometry, lighting and animation remain separate evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import hashlib
import os
from pathlib import Path
import platform
import re
import stat
import struct
import sys
import tempfile
import zlib

import exr_consumption
from render3d_contracts import Render3DError, require, validate, VERSION, frame_times, rational, definitions, _validator
from rig_errors import RigError, object_hash, open_payload, scoped_directory_descriptor


_CODE_SHA256 = object_hash({
    'render3d_verify.py': exr_consumption.loaded_source_sha256(__file__, sys._getframe().f_code),
    'exr_consumption.py': exr_consumption.SOURCE_SHA256,
})


def _image_identity(name=None):
    """Expose only canonical frame/pass names, never a caller's basename."""
    if isinstance(name, str) and re.fullmatch(
            r'(?:frames/[0-9]{6,}|passes/(?:albedo|normal|depth)/[0-9]{6,})\.(?:png|exr)', name):
        return name
    return 'image'


@contextmanager
def _private_errors(message, *, path=''):
    """Keep owner/native details private while retaining categorized failures."""
    try:
        yield
    except Render3DError as exc:
        raise Render3DError(exc.report['category'], message, path=path) from None
    except (RigError, OSError, ValueError):
        raise Render3DError('corrupt-output', message, path=path) from None


def _contract(value, kind):
    with _private_errors('Render document does not satisfy its contract.'):
        return validate(value, kind)


def _result_boundary(operation):
    @wraps(operation)
    def checked(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Render3DError:
            raise
        except Exception:
            raise Render3DError('corrupt-output', 'Render artifact verification failed.') from None
    return checked


@contextmanager
def _image_source(path, *, root=None, name=None, root_identity=None, require_single_link=False):
    """Keep ordinary and scoped reads under the existing payload authority."""
    root = path.parent if root is None else root
    name = path.name if name is None else name
    def admit_opened(info):
        require(not require_single_link or getattr(info, 'st_nlink', None) == 1,
                'corrupt-output', 'Result images must be independently materialized files.')

    # Windows pins deny ancestor replacement while the ordinary payload reader
    # verifies the opened file handle. POSIX open_payload walks nofollow dirfds.
    if os.name == 'nt':
        from atomic_windows import pinned_directory
        with pinned_directory(root), pinned_directory(path.parent):
            with open_payload(root, name, root_identity=root_identity) as (source, opened):
                admit_opened(opened)
                yield source
                admit_opened(os.fstat(source.fileno()))
    else:
        with open_payload(root, name, root_identity=root_identity) as (source, opened):
            admit_opened(opened)
            yield source
            admit_opened(os.fstat(source.fileno()))


@contextmanager
def _captured(path, max_bytes, *, diagnostic_path='image', **source_options):
    """Hash and decode the same privately captured bytes, not a mutable path."""
    path = Path(path)
    try:
        with tempfile.TemporaryFile() as frozen:
            digest, size = hashlib.sha256(), 0
            with _image_source(path, **source_options) as source:
                while chunk := source.read(min(1024*1024, max_bytes-size+1)):
                    size += len(chunk)
                    require(size <= max_bytes, 'storage-exhaustion', 'Image exceeds its verification byte budget.', path=diagnostic_path)
                    digest.update(chunk)
                    frozen.write(chunk)
            require(size > 0, 'corrupt-output', 'Image is empty.', path=diagnostic_path)
            frozen.seek(0)
            yield frozen, dict(bytes=size, sha256=digest.hexdigest())
    except Render3DError as exc:
        exc.report['path'] = diagnostic_path
        raise
    except (RigError, OSError, ValueError):
        raise Render3DError('corrupt-output', 'Image could not be captured safely.', path=diagnostic_path) from None


@contextmanager
def _output_authority(root):
    """Pin the inventory root with the existing native platform adapter."""
    if os.name == 'nt':
        from atomic_windows import pinned_directory
        with pinned_directory(root):
            info = root.stat()
            yield None, (info.st_dev, info.st_ino)
    else:
        from atomic_publish import pinned_directory_fd
        with pinned_directory_fd(root) as descriptor:
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            yield descriptor, identity
            # A held POSIX directory can be renamed. Recheck the original
            # namespace through the same nofollow adapter before accepting it.
            with pinned_directory_fd(root) as current:
                info = os.fstat(current)
                require((info.st_dev, info.st_ino) == identity,
                        'corrupt-output', 'Output root changed during verification.')


def _image_inventory(root, descriptor, passes, limit):
    """Enumerate only beneath a pinned root, never through replaceable parents."""
    actual = set()

    def walk(directory, relative, fd):
        with os.scandir(fd if fd is not None else directory) as entries:
            for entry in entries:
                name = entry.name
                child = directory/name
                parts = (*relative, name)
                info = (os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else child.lstat())
                if stat.S_ISDIR(info.st_mode):
                    require(len(parts) == 2 and parts[0] == 'passes' and parts[1] in passes,
                            'corrupt-output', 'Unexpected image directory.')
                    with child_directory(directory, name, fd) as opened:
                        walk(child, parts, opened)
                else:
                    require(stat.S_ISREG(info.st_mode), 'corrupt-output', 'Nonregular entry in image inventory.')
                    require(getattr(info, 'st_nlink', None) == 1, 'corrupt-output',
                            'Result images must be independently materialized files.')
                    actual.add('/'.join(parts))
                    require(len(actual) <= limit, 'incomplete-output', 'Unexpected extra image files.')

    @contextmanager
    def child_directory(parent, name, fd):
        if fd is None:
            from atomic_windows import pinned_directory
            with pinned_directory(parent/name):
                yield None
        else:
            opened = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=fd)
            try:
                before = os.fstat(opened)
                yield opened
                after = os.stat(name, dir_fd=fd, follow_symlinks=False)
                require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
                        'corrupt-output', 'Image inventory directory changed during verification.')
            finally:
                os.close(opened)

    for name in ('frames', 'passes'):
        try:
            info = (os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if descriptor is not None else (root/name).lstat())
        except FileNotFoundError:
            continue
        require(stat.S_ISDIR(info.st_mode), 'corrupt-output', 'Image directory is not an ordinary directory.')
        with child_directory(root, name, descriptor) as opened:
            walk(root/name, (name,), opened)
    return actual


def _png(stream, resolution, bit_depth, memory_bytes, pixels=False):
    try:
        import png
        import numpy as np
    except ImportError:
        raise Render3DError('unsupported-feature', 'PNG verification requires the optional pypng and numpy packages.') from None
    # The private capture is seekable; admit each declared payload plus its CRC
    # against these actual bytes before issuing any payload-sized read.
    extent = stream.seek(0, 2)
    stream.seek(0)
    # Strict structure and bounded inflate before the independent PNG decoder.
    require(stream.read(8) == b'\x89PNG\r\n\x1a\n', 'corrupt-output', 'Not a PNG file.')
    header, ended, count, inflated, inflater = None, False, 0, 0, zlib.decompressobj()
    idat_started = idat_ended = False
    while not ended:
        prefix = stream.read(8)
        require(len(prefix) == 8, 'corrupt-output', 'Truncated PNG chunk.')
        length, kind = struct.unpack('>I4s', prefix)
        require(length <= memory_bytes, 'system-memory', 'PNG chunk exceeds verification memory budget.')
        require(length+4 <= extent-stream.tell(), 'corrupt-output', 'Truncated PNG payload.')
        if count == 0:
            require(kind == b'IHDR' and length == 13, 'corrupt-output', 'PNG must start with IHDR.')
        elif kind == b'IHDR':
            raise Render3DError('corrupt-output','Duplicate PNG header.')

        crc = zlib.crc32(kind)
        if kind == b'IDAT':
            require(not idat_ended, 'corrupt-output', 'PNG IDAT chunks must be consecutive.')
            idat_started = True
            require(not inflater.eof or length == 0, 'corrupt-output', 'Trailing compressed PNG stream.')
            remaining = length
            while remaining:
                # Reserve the bounded output (including its overrun sentinel)
                # and space for zlib to return an unconsumed input suffix.
                output_limit = header[3] - inflated + 1
                size = min(remaining, (memory_bytes - output_limit) // 2)
                require(size > 0, 'system-memory', 'PNG inflate buffers exceed memory budget.')
                block = stream.read(size)
                require(len(block) == size, 'corrupt-output', 'Truncated PNG payload.')
                crc = zlib.crc32(block, crc)
                inflated += len(inflater.decompress(block, output_limit))
                del block
                require(inflated <= header[3] and not inflater.unconsumed_tail and not inflater.unused_data,
                        'corrupt-output', 'PNG decompressed data exceeds declared dimensions.')
                remaining -= size
        else:
            payload = stream.read(length)
            require(len(payload) == length, 'corrupt-output', 'Truncated PNG payload.')
            crc = zlib.crc32(payload, crc)
            if kind == b'IHDR':
                width,height,depth,color,compression,filtering,interlace = struct.unpack('>IIBBBBB',payload)
            del payload
            if idat_started:
                idat_ended = True
        checksum = stream.read(4)
        require(len(checksum) == 4, 'corrupt-output', 'Truncated PNG payload.')
        require(crc == struct.unpack('>I', checksum)[0], 'corrupt-output', 'PNG CRC mismatch.')
        if kind == b'IHDR':
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
        require(kind not in (b'acTL',b'fcTL',b'fdAT'),'corrupt-output','Animated PNG is not a single render frame.')
        require(kind in (b'IHDR',b'IDAT',b'IEND') or kind[0]&32,
                'unsupported-feature','Unsupported critical PNG chunk.')
        require(kind != b'tRNS', 'unsupported-feature',
                'PNG color-key transparency is unsupported; use explicit RGBA samples.')
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
        values=np.asarray(row,dtype=np.float64).reshape((width,len(names)))
        values /= (1<<bit_depth)-1
        minima=np.minimum(minima,values.min(axis=0));maxima=np.maximum(maxima,values.max(axis=0));row_count+=1
        if pixels:pixel_rows.append(values)
        del values
    require(row_count == height,'corrupt-output','PNG row count differs from dimensions.')
    result=dict(channels={name:dict(type='uint'+str(bit_depth),minimum=float(lo),maximum=float(hi)) for name,lo,hi in zip(names,minima,maxima)},
                decoder='pypng',decoder_version=png.__version__, resolution=[width,height],bit_depth=bit_depth)
    if pixels:
        data=np.stack(pixel_rows);result['_pixels']={name:data[:,:,index] for index,name in enumerate(names)}
    return result


def _exr_envelope(stream, resolution, memory_bytes, bit_depth, pixels=False):
    """Admit only a bounded single-part, non-deep scanline EXR header."""
    # OpenEXR v3.5.4 ImfVersion.h and OpenEXRFileLayout.rst define these
    # little-endian fields. LONG_NAMES (0x400) changes names, not storage.
    prefix = stream.read(8)
    require(len(prefix) == 8, 'corrupt-output', 'Truncated EXR envelope.')
    magic, version = struct.unpack('<II', prefix)
    require(magic == 20000630, 'corrupt-output', 'Not an EXR file.')
    require(version & 0xff == 2, 'unsupported-feature', 'Unsupported EXR format version.')
    require((version & ~0xff) in (0, 0x400), 'unsupported-feature',
            'EXR verification supports only single-part non-deep scanline images; unsupported flags.')
    # The type attribute selects storage too. Inspect it before InputFile can
    # initialize a deep compositor, even when version flags are inconsistent.
    # Cap header work independently of the artifact's compressed byte budget.
    end = stream.seek(0, 2)
    stream.seek(8)
    header_limit = min(memory_bytes, 1024**2)
    name_limit = 255 if version & 0x400 else 31

    def read(size):
        require(stream.tell()+size <= header_limit, 'system-memory', 'EXR header exceeds verification memory budget.')
        data = stream.read(size)
        require(len(data) == size, 'corrupt-output', 'Truncated EXR header.')
        return data

    def name():
        value = bytearray()
        for _ in range(name_limit+1):
            char = read(1)
            if char == b'\x00':
                return bytes(value)
            value.extend(char)
        raise Render3DError('corrupt-output', 'EXR header name exceeds its declared limit.')

    seen = set()
    compression = None
    channels = []
    expected_window = (0, 0, resolution[0]-1, resolution[1]-1)
    while True:
        attribute = name()
        if not attribute:
            break
        require(attribute not in seen, 'corrupt-output', 'Duplicate EXR header attribute.')
        seen.add(attribute)
        kind = name()
        require(bool(kind), 'corrupt-output', 'Empty EXR attribute type.')
        size = struct.unpack('<i', read(4))[0]
        require(size >= 0 and size <= end-stream.tell(), 'corrupt-output', 'Invalid EXR attribute size.')
        require(stream.tell()+size <= header_limit, 'system-memory', 'EXR header exceeds verification memory budget.')
        require(attribute != b'tiles', 'unsupported-feature', 'Tiled and multilevel EXR images are unsupported.')
        if attribute == b'type':
            require(kind == b'string' and size == len(b'scanlineimage'), 'unsupported-feature',
                    'Only EXR scanlineimage storage is supported.')
            require(read(size) == b'scanlineimage', 'unsupported-feature',
                    'Only EXR scanlineimage storage is supported.')
        elif attribute == b'channels':
            require(kind == b'chlist', 'corrupt-output', 'Malformed EXR channels attribute.')
            channel_end = stream.tell()+size
            channel_names = set()
            while True:
                require(stream.tell() < channel_end, 'corrupt-output', 'Unterminated EXR channel list.')
                channel = name()
                require(stream.tell() <= channel_end, 'corrupt-output', 'Truncated EXR channel name.')
                if not channel:
                    break
                require(channel not in channel_names, 'corrupt-output', 'Duplicate EXR channel name.')
                channel_names.add(channel)
                require(len(channel_names) <= 64, 'corrupt-output', 'EXR channel count is outside supported bounds.')
                require(stream.tell()+16 <= channel_end, 'corrupt-output', 'Truncated EXR channel metadata.')
                precision, linear, reserved, x_sampling, y_sampling = struct.unpack('<iB3sii', read(16))
                require(precision == (1 if bit_depth == 16 else 2) and x_sampling == y_sampling == 1,
                        'corrupt-output', 'EXR channel precision or sampling differs from request.')
                require(linear in (0, 1) and reserved == b'\x00'*3,
                        'corrupt-output', 'Malformed EXR channel metadata.')
                channels.append((channel, precision))
                decoded_bytes = resolution[0]*resolution[1]*len(channel_names)*(8 if pixels else 4)
                require(decoded_bytes <= memory_bytes, 'system-memory',
                        'EXR preview arrays exceed memory budget.' if pixels else 'EXR decoded channels exceed memory budget.')
            require(bool(channel_names) and stream.tell() == channel_end, 'corrupt-output', 'Malformed EXR channel list.')
        elif attribute == b'compression':
            require(kind == b'compression' and size == 1, 'corrupt-output', 'Malformed EXR compression attribute.')
            compression = read(1)[0]
        elif attribute in (b'dataWindow', b'displayWindow'):
            require(kind == b'box2i' and size == 16 and struct.unpack('<4i', read(16)) == expected_window,
                    'corrupt-output', f'EXR {attribute.decode()} must match the zero-origin requested full frame.')
        else:
            stream.seek(size, 1)
    require({b'channels', b'compression', b'dataWindow', b'displayWindow'} <= seen,
            'corrupt-output', 'EXR is missing its channels, compression or full-frame window attributes.')
    block_rows = {0: 1, 1: 1, 2: 1, 3: 16, 4: 32, 5: 16, 6: 32, 7: 32, 8: 32, 9: 256}
    require(compression in block_rows, 'unsupported-feature',
            'EXR compression envelope is not independently verified.')
    if compression in block_rows:
        rows = block_rows[compression]
        height = int(resolution[1])  # Both encoded windows matched the requested dimensions above.
        chunks = (height+rows-1)//rows
        require(chunks*8 <= memory_bytes, 'system-memory', 'EXR scanline table exceeds verification memory budget.')
        table = stream.read(chunks*8)
        require(len(table) == chunks*8, 'corrupt-output', 'Truncated EXR scanline table.')
        boundary = stream.tell()
        scanlines = set()
        for offset in sorted(value for (value,) in struct.iter_unpack('<Q', table)):
            require(offset == boundary, 'corrupt-output', 'EXR chunk table contains gaps, aliases or unreferenced bytes.')
            stream.seek(offset)
            chunk = stream.read(8)
            require(len(chunk) == 8, 'corrupt-output', 'Truncated EXR chunk.')
            line, length = struct.unpack('<iI', chunk)
            require(0 <= line < height and line % rows == 0 and line not in scanlines,
                    'corrupt-output', 'Invalid EXR scanline chunk.')
            scanlines.add(line)
            boundary = offset+8+length
            require(boundary <= end, 'corrupt-output', 'Truncated EXR pixel payload.')
            require(length <= memory_bytes, 'system-memory', 'EXR compressed chunk exceeds verification memory budget.')
            payload = stream.read(length)
            require(len(payload) == length, 'corrupt-output', 'Truncated EXR pixel payload.')
            exr_consumption.verify_chunk(payload, compression, resolution[0], min(rows, height - line), channels, memory_bytes)
        require(boundary == end, 'corrupt-output', 'Unexpected bytes after EXR chunks.')
    stream.seek(0)


def _exr(stream, resolution, bit_depth, memory_bytes, pixels=False):
    _exr_envelope(stream, resolution, memory_bytes, bit_depth, pixels)
    try:
        import OpenEXR, Imath
        import numpy as np
    except ImportError:
        raise Render3DError('unsupported-feature', 'EXR verification requires optional OpenEXR and numpy packages.') from None
    # Envelope admission precedes native decoder construction. The private
    # captured stream keeps admission, hashing and channel decoding bound.
    reader=OpenEXR.InputFile(stream)
    try:
        header=reader.header();window=header['dataWindow']
        width,height=window.max.x-window.min.x+1,window.max.y-window.min.y+1
        expected_window=(0,0,resolution[0]-1,resolution[1]-1)
        for name in ('dataWindow','displayWindow'):
            frame=header[name]
            require((frame.min.x,frame.min.y,frame.max.x,frame.max.y) == expected_window,
                    'corrupt-output',f'EXR {name} must match the zero-origin requested full frame.')
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


def _image_contract(output, resolution, pass_name, max_bytes, memory_bytes):
    """Use native field contracts; one EXR image need not include beauty."""
    properties = definitions()['RenderSettings']['properties']
    schema = deepcopy(properties['output'])
    # The native aggregate requires a combined pass across a render job. A
    # standalone depth/normal image retains the same element/format constraints
    # without requiring that separate image in this one-file operation.
    del schema['allOf'][0]['else']['properties']['passes']['contains']
    resources = properties['resources']['properties']
    for value, contract in ((output, schema), (resolution, properties['resolution']),
                            (max_bytes, resources['storage_bytes']),
                            (memory_bytes, resources['memory_bytes'])):
        error = next(iter(_validator('RenderSettings').evolve(schema=contract).iter_errors(value)), None)
        require(error is None, 'invalid-input', 'Image verification options do not satisfy the output contract.')
    require(isinstance(pass_name, str) and pass_name in output['passes'],
            'invalid-input', 'Unsupported output/pass selection.')


def _requested_backend(settings, backend):
    """Check observed identity against validated native engine/device intent."""
    _contract(backend, 'BackendIdentity')
    require(backend['engine'] == settings['engine'], 'unsupported-backend',
            'Executed engine differs from the requested engine.')
    actual, selected, policy = backend['device'], settings['device'], settings['device_policy']
    # These exact category identifiers are the current Render3D fixtures' known
    # CPU/GPU vocabulary. An opaque hardware ID needs a future authenticated
    # device-class contract; neither selection nor spelling supplies that fact.
    require(actual in ('cpu', 'cuda'), 'unsupported-device',
            'Observed device has no supported affirmative CPU/GPU classification.')
    if policy == 'cpu':
        require(actual == 'cpu', 'unsupported-device', 'CPU policy requires CPU execution.')
    elif actual == 'cpu':
        require(policy == 'gpu-preferred' and settings['allow_cpu_fallback'],
                'unsupported-device', 'CPU execution is not permitted by the requested GPU policy.')
    else:
        require(selected is None or actual == selected, 'unsupported-device',
                'Executed device differs from the explicitly selected device.')


def verify_image(path, *, output, resolution, pass_name='combined', max_bytes=1024**3, memory_bytes=1024**3, _pixels=False, _source_options=None):
    """Decode one actual output. Pixel ranges do not establish scene correctness."""
    _image_contract(output, resolution, pass_name, max_bytes, memory_bytes)
    try:
        with _captured(path,max_bytes,diagnostic_path=_image_identity((_source_options or {}).get('name')),**(_source_options or {})) as (stream,identity):
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
    except Exception:
        # Decoder-specific errors are not allowed to leak as worker crashes.
        raise Render3DError('corrupt-output', 'Image decoder could not verify the captured data.',
                            path=_image_identity((_source_options or {}).get('name'))) from None


def decode_image(path, **options):
    """Return (verified metadata, normalized channel arrays) for bounded previews.

    PNG values remain display encoded; EXR values remain scene linear or raw
    data-pass values. The caller must apply explicit display mappings.
    """
    report=verify_image(path,_pixels=True,**options)
    pixels=report.pop('_pixels')
    return report,pixels


class RenderArtifactVerifier:
    _code_sha256 = _CODE_SHA256
    _helper_sha256 = exr_consumption.SOURCE_SHA256

    def __init__(self,snapshot_root,expected_backend,*,max_total_bytes=None):
        self.snapshot_root=Path(snapshot_root)
        self.expected_backend=deepcopy(expected_backend)
        self.max_total_bytes = max_total_bytes

    @_result_boundary
    def verify(self,request,result,root):
        """Verify all expected frames, including a provisional unverified result.

        Result publication and evidence-file writing belong to the caller. The
        returned report must be committed before setting final status complete.
        """
        import render3d_snapshot as snapshot_owner
        _contract(request,'RenderRequest')
        _requested_backend(request['settings'], self.expected_backend)
        require(isinstance(result,dict) and result.get('status') in ('partial','complete'),
                'incomplete-output','Only a complete candidate sequence can be verified.')
        _contract(result.get('verification'),'Verification')
        pending=deepcopy(result);pending['status']='partial'
        pending['verification']=dict(status='not-run',verifier=None,evidence=None)
        _contract(pending,'RenderResult')
        root=Path(root)
        with _private_errors('Output root could not be inspected safely.'):
            with scoped_directory_descriptor(root) as root_fd:
                require(root_fd is not None or not root.is_symlink(),'corrupt-output','Output root may not be a symlink.')
            require(root.is_dir(),'missing-asset','Output root is unavailable.')
        # The request authenticates the snapshot file before the owner rechecks
        # its closure. Decode and hash one captured snapshot byte buffer.
        with _private_errors('Source snapshot verification failed.',path='snapshot.json'):
            snapshot_path=self.snapshot_root/'snapshot.json'
            with scoped_directory_descriptor(self.snapshot_root) as snapshot_fd:
                bounded_snapshot_owner = hasattr(snapshot_owner, 'native_memory_budget')
                require(bounded_snapshot_owner or (self.max_total_bytes is None and snapshot_fd is None),
                        'unsupported-feature', 'Scoped or byte-capped source verification requires the bounded snapshot owner.')
            with _captured(snapshot_path,32*1024**2,diagnostic_path='snapshot.json') as (stream,reference):
                from rig_errors import decode_json
                snapshot=_contract(decode_json(stream.read()),'RenderSnapshot')
            require(reference == {key:request['snapshot'][key] for key in ('bytes','sha256')},'stale-asset','Snapshot differs from request-bound bytes.')
            options = {} if self.max_total_bytes is None else {'max_total_bytes': self.max_total_bytes}
            if snapshot['scope'] == 'scene3d':
                adapter = snapshot_owner.Scene3DAdapter()
            elif bounded_snapshot_owner:
                adapter = snapshot_owner.NativeRigAdapter(memory_bytes=snapshot_owner.native_memory_budget(request))
            else:
                adapter = snapshot_owner.NativeRigAdapter()
            require(snapshot_owner.verify_snapshot(self.snapshot_root,adapter,**options)==snapshot,
                    'stale-asset','Snapshot changed during independent verification.')
            if isinstance(adapter,snapshot_owner.Scene3DAdapter):
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
            with _output_authority(root) as (root_fd, root_identity):
                for item in result['images']:
                    reference=item['file'];name=reference['path'];suffix=settings['output']['format']
                    expected_name=(f'frames/{item["frame"]:06d}.{suffix}' if item['pass_name']=='combined'
                                   else f'passes/{item["pass_name"]}/{item["frame"]:06d}.{suffix}')
                    require(name==expected_name and name not in paths,'corrupt-output','Image path does not match frame/pass identity.')
                    paths.add(name)
                    require(item['format']==suffix and item['bit_depth']==settings['output']['bit_depth'] and item['resolution']==settings['resolution'],
                            'corrupt-output','Image declaration differs from request settings.')
                    remaining = settings['resources']['storage_bytes']-total
                    require(remaining > 0, 'storage-exhaustion',
                            'Output sequence exceeds its verification byte budget.',path=name)
                    file=root/name
                    image=verify_image(file,output=settings['output'],resolution=settings['resolution'],pass_name=item['pass_name'],
                        max_bytes=remaining,memory_bytes=settings['resources']['memory_bytes'],
                        _source_options=dict(root=root,name=name,root_identity=root_identity,require_single_link=True))
                    require(image['sha256']==reference['sha256'] and image['bytes']==reference['bytes'],
                            'corrupt-output','Image bytes differ from committed reference.',path=name)
                    presentation,source=frame_times(request,item['frame'])
                    total+=image['bytes'];decoded.append(dict(frame=item['frame'],path=name,
                        presentation_time=rational(presentation),source_time=rational(source),**image))
                actual = _image_inventory(root, root_fd, settings['output']['passes'], len(paths))
                require(actual==paths,'incomplete-output','Unexpected or missing output images.')
        except Render3DError:
            raise
        except (RigError, OSError, ValueError):
            raise Render3DError('corrupt-output', 'Output inventory could not be verified safely.') from None
        # reload() reuses a module's globals even for previously created instances.
        require(self._code_sha256 == _CODE_SHA256 and self._helper_sha256 == exr_consumption.SOURCE_SHA256,
                'verification-failed', 'Verifier implementation changed after loading.')
        identity = dict(id='render3d-independent-pixels', version=VERSION,
                        code_sha256=self._code_sha256, platform=platform.platform())
        return dict(schema_version=1,kind='render3d-verification',status='passed',verifier=identity,
                    request_sha256=object_hash(request),snapshot_sha256=snapshot['snapshot_sha256'],
                    settings_sha256=object_hash(settings),backend=deepcopy(self.expected_backend),images=decoded,
                    decoded_frames=len(frames),decoded_images=len(decoded),output_bytes=total,
                    limitation='Hashes describe captured bytes; the controller must retain or recheck the publication candidate. Decoded integrity and input binding do not establish semantic scene correctness or visual approval.')


@_result_boundary
def verify_result(request,result,root,*,snapshot_root,expected_backend,max_total_bytes=None):
    return RenderArtifactVerifier(snapshot_root,expected_backend,max_total_bytes=max_total_bytes).verify(request,result,root)
