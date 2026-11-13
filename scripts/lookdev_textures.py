"""Bounded, deterministic surface maps; originals and derivations remain distinct.

Optional NumPy/Pillow imports are local to execution. RGB color inputs decode to
linear sRGB; data maps never receive a color transform. All public pixel arrays
are float32 RGBA, with finite nonnegative scene-linear values.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import warnings
import secrets
import sys
import ctypes

from lookdev_contracts import canonical_bytes, portable_path
from rig_errors import RigError, require

VERSION = "1.0.0"
KINDS = ("wood", "stone", "marble", "concrete", "fabric", "leather", "scratched-metal",
         "brushed-metal", "clay", "sand", "painted-plastic", "ceramic", "toon-ramp")
COLOR_ROLES = {"base-color", "emission", "sheen-color", "environment"}
DATA_ROLES = {"normal", "metallic", "roughness", "occlusion", "height", "displacement", "opacity",
              "transmission", "thickness", "clearcoat", "clearcoat-roughness", "sheen-roughness",
              "anisotropy", "iridescence"}


@dataclass(frozen=True)
class Limits:
    encoded_bytes: int = 64 * 1024 * 1024
    pixels: int = 4 * 1024 * 1024
    working_bytes: int = 512 * 1024 * 1024
    output_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        for value in vars(self).values():
            require(type(value) is int and 0 < value <= 4 * 1024**3,
                    "TEXTURE_LIMIT", "Limits must be positive bounded integer bytes/pixels.")


@dataclass
class DecodedTexture:
    pixels: object
    format: str
    source_sha256: str
    role: str
    encoding: str
    channels: int = 4
    bit_depth: int = 8

    @property
    def width(self): return int(self.pixels.shape[1])
    @property
    def height(self): return int(self.pixels.shape[0])


def _np():
    import numpy
    return numpy


def _size(width, height, limits, multiplier=96):
    require(type(width) is int and type(height) is int and 0 < width <= 16384 and 0 < height <= 16384,
            "TEXTURE_DIMENSIONS", "Texture dimensions must be integers in 1..16384.")
    require(width * height <= limits.pixels and width * height * multiplier <= limits.working_bytes,
            "TEXTURE_LIMIT", "Texture decoded working-set budget exceeded.")


def _finite(value, low=None, high=None, name="value"):
    require(type(value) in (int, float), "TEXTURE_NUMBER", name + " must be finite.")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    require(finite, "TEXTURE_NUMBER", name + " must be finite.")
    require((low is None or value >= low) and (high is None or value <= high),
            "TEXTURE_NUMBER", name + " is outside its supported range.")
    return float(value)


def srgb_to_linear(array):
    np = _np()
    return np.where(array <= 0.04045, array / 12.92, ((array + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(array):
    np = _np()
    return np.where(array <= 0.0031308, 12.92 * array, 1.055 * np.maximum(array, 0) ** (1 / 2.4) - 0.055)


def _radiance(data, limits):
    """Decode canonical Radiance RGBE, including flat and per-channel scanline RLE."""
    np = _np()
    stream = io.BytesIO(data)
    require(stream.readline() in (b"#?RADIANCE\n", b"#?RGBE\n"), "TEXTURE_HDR", "Invalid Radiance signature.")
    header_bytes = 0
    format_ok = False
    while True:
        line = stream.readline(8193)
        header_bytes += len(line)
        require(line and len(line) <= 8192 and header_bytes <= 65536, "TEXTURE_HDR", "Truncated or excessive Radiance header.")
        if line in (b"\n", b"\r\n"): break
        if line.strip() == b"FORMAT=32-bit_rle_rgbe": format_ok = True
        if line.startswith((b"EXPOSURE=", b"COLORCORR=", b"PRIMARIES=")):
            raise RigError("TEXTURE_HDR", "Radiance exposure/color metadata requires an explicit color adapter.")
    require(format_ok, "TEXTURE_HDR", "Only Radiance RGBE format is supported.")
    match = re.fullmatch(rb"([+-])Y (\d+) ([+-])X (\d+)\s*", stream.readline(8193))
    require(match is not None, "TEXTURE_HDR", "Expected Radiance Y/X resolution.")
    height, width = int(match[2]), int(match[4])
    _size(width, height, limits)
    image = np.empty((height, width, 4), dtype=np.uint8)
    for row in range(height):
        prefix = stream.read(4)
        require(len(prefix) == 4, "TEXTURE_HDR", "Truncated Radiance row.")
        if 8 <= width <= 32767 and prefix[:2] == b"\x02\x02" and prefix[2] < 128:
            require((prefix[2] << 8 | prefix[3]) == width, "TEXTURE_HDR", "Radiance scanline width mismatch.")
            for channel in range(4):
                cursor = 0
                while cursor < width:
                    count_b = stream.read(1)
                    require(count_b, "TEXTURE_HDR", "Truncated Radiance run.")
                    code = count_b[0]
                    count = code - 128 if code > 128 else code
                    require(count > 0 and cursor + count <= width, "TEXTURE_HDR", "Invalid Radiance run length.")
                    payload = stream.read(1 if code > 128 else count)
                    require(len(payload) == (1 if code > 128 else count), "TEXTURE_HDR", "Truncated Radiance run data.")
                    image[row, cursor:cursor+count, channel] = payload[0] if code > 128 else np.frombuffer(payload, dtype=np.uint8)
                    cursor += count
        else:
            payload = prefix + stream.read(width * 4 - 4)
            require(len(payload) == width * 4, "TEXTURE_HDR", "Truncated Radiance pixels.")
            values = np.frombuffer(payload, dtype=np.uint8).reshape(width, 4)
            require(not np.any(np.all(values[:, :3] == 1, axis=1)), "TEXTURE_HDR", "Legacy Radiance repeated-pixel encoding is unsupported.")
            image[row] = values
    require(stream.read(1) == b"", "TEXTURE_HDR", "Unexpected trailing Radiance bytes.")
    if match[1] == b"+": image = image[::-1]
    if match[3] == b"-": image = image[:, ::-1]
    exponent = image[:, :, 3].astype(np.int32)
    rgb = np.ldexp(image[:, :, :3].astype(np.float32), exponent[:, :, None] - 136)
    rgb[exponent == 0] = 0
    return np.concatenate((rgb, np.ones((height, width, 1), np.float32)), axis=2)


def _exr(data, limits):
    """Bounded RGB(A) half/float scanline EXR with NONE/ZIPS/ZIP compression.

    Implements the published OpenEXR file layout. Multipart, deep/tiled, sampled
    channels and other compression schemes require a separate native adapter.
    """
    import struct
    import zlib
    np = _np()
    require(len(data) >= 8 and data[:4] == b'v/1\x01', 'TEXTURE_EXR', 'Invalid EXR signature.')
    require(struct.unpack_from('<I', data, 4)[0] == 2, 'TEXTURE_EXR', 'Only single-part scanline EXR version 2 is supported.')
    cursor = 8
    def string():
        nonlocal cursor
        end = data.find(b'\x00', cursor, min(len(data),cursor+256))
        require(end >= 0, 'TEXTURE_EXR', 'Invalid bounded EXR attribute string.')
        result=data[cursor:end];cursor=end+1
        return result
    attributes={}
    while True:
        name=string()
        if not name:break
        require(name not in attributes and len(attributes)<128,'TEXTURE_EXR','Duplicate or excessive EXR attributes.')
        kind=string()
        require(cursor+4<=len(data),'TEXTURE_EXR','Truncated EXR attribute.')
        length=struct.unpack_from('<I',data,cursor)[0];cursor+=4
        require(length<=65536 and cursor+length<=len(data) and cursor+length<=1024*1024,
                'TEXTURE_EXR','EXR attribute exceeds header budget.')
        attributes[name]=(kind,data[cursor:cursor+length]);cursor+=length
    def attr(name,kind,length=None):
        require(name in attributes and attributes[name][0]==kind and
                (length is None or len(attributes[name][1])==length), 'TEXTURE_EXR','Missing or invalid EXR attribute '+name.decode())
        return attributes[name][1]
    bounds=struct.unpack('<iiii',attr(b'dataWindow',b'box2i',16))
    x0,y0,x1,y1=bounds;width=x1-x0+1;height=y1-y0+1
    _size(width,height,limits,128)
    attr(b'displayWindow',b'box2i',16)
    compression=attr(b'compression',b'compression',1)[0]
    require(compression in (0,2,3),'TEXTURE_EXR','EXR decoder supports NONE, ZIPS and ZIP compression.')
    require(attr(b'lineOrder',b'lineOrder',1)[0] in (0,1),'TEXTURE_EXR','Unsupported EXR line ordering.')
    if b'chromaticities' in attributes:
        colors=struct.unpack('<8f',attr(b'chromaticities',b'chromaticities',32))
        require(np.allclose(colors,(.64,.33,.30,.60,.15,.06,.3127,.3290),atol=1e-5,rtol=0),
                'TEXTURE_EXR','Non-sRGB EXR primaries require an explicit color conversion adapter.')
    channels_payload=attr(b'channels',b'chlist');offset=0;channels=[]
    while offset<len(channels_payload):
        end=channels_payload.find(b'\x00',offset)
        require(end>=0,'TEXTURE_EXR','Truncated EXR channel name.')
        name=channels_payload[offset:end];offset=end+1
        if not name:break
        require(name in (b'R',b'G',b'B',b'A') and name not in [n for n,_ in channels] and offset+16<=len(channels_payload),
                'TEXTURE_EXR','Expected unique RGB(A) channels only.')
        dtype,linear,reserved,x_sampling,y_sampling=struct.unpack_from('<iB3sii',channels_payload,offset);offset+=16
        require(dtype in (1,2) and linear in (0,1) and reserved==b'\x00'*3 and x_sampling==y_sampling==1,
                'TEXTURE_EXR','Expected full-resolution half/float EXR channels.')
        channels.append((name,dtype))
    require(offset==len(channels_payload) and {n for n,_ in channels} in ({b'R',b'G',b'B'},{b'R',b'G',b'B',b'A'}),
            'TEXTURE_EXR','EXR requires a complete RGB or RGBA channel set.')
    row_bytes=width*sum(2 if dtype==1 else 4 for _,dtype in channels)
    block_rows=16 if compression==3 else 1
    count=(height+block_rows-1)//block_rows
    require(cursor+count*8<=len(data),'TEXTURE_EXR','Truncated EXR chunk index.')
    offsets=struct.unpack_from('<'+'Q'*count,data,cursor);cursor+=count*8
    pixels=np.ones((height,width,4),np.float32);spans=[]
    for index,position in enumerate(offsets):
        require(cursor<=position<=len(data)-8,'TEXTURE_EXR','EXR chunk offset is outside the pixel section.')
        y,packed_bytes=struct.unpack_from('<iI',data,position)
        require(y==y0+index*block_rows and packed_bytes>0 and position+8+packed_bytes<=len(data),
                'TEXTURE_EXR','Invalid EXR chunk row or size.')
        rows=min(block_rows,height-index*block_rows);expected=rows*row_bytes
        require(packed_bytes<=expected,'TEXTURE_EXR','EXR chunk cannot exceed its raw sample budget.')
        payload=data[position+8:position+8+packed_bytes]
        if packed_bytes==expected:
            raw=payload
        else:
            require(compression!=0,'TEXTURE_EXR','Uncompressed EXR chunk size mismatch.')
            try:
                inflater=zlib.decompressobj();filtered=inflater.decompress(payload,expected+1)
                require(len(filtered)==expected and inflater.eof and not inflater.unconsumed_tail and not inflater.unused_data,
                        'TEXTURE_EXR','EXR zlib payload is truncated, excessive or concatenated.')
            except zlib.error as exc:raise RigError('TEXTURE_EXR','EXR ZIP decompression failed.') from exc
            # Invert the byte predictor by bounded prefix sum, then undo the
            # even-byte/odd-byte shuffle defined by ZIP compression.
            values=np.frombuffer(filtered,dtype=np.uint8).astype(np.int64)
            values[1:]-=128
            values=np.cumsum(values,dtype=np.int64).astype(np.uint8)
            shuffled=np.empty(expected,np.uint8);middle=(expected+1)//2
            shuffled[::2]=values[:middle];shuffled[1::2]=values[middle:]
            raw=shuffled.tobytes()
        sample_offset=0
        for row in range(rows):
            for name,dtype in channels:
                item_size=2 if dtype==1 else 4
                samples=np.frombuffer(raw,dtype='<f2' if dtype==1 else '<f4',count=width,offset=sample_offset)
                pixels[index*block_rows+row,:,b'RGBA'.index(name)]=samples
                sample_offset+=width*item_size
        spans.append((position,position+8+packed_bytes))
    spans.sort()
    require(spans[0][0]==cursor and spans[-1][1]==len(data) and all(a[1]==b[0] for a,b in zip(spans,spans[1:])),
            'TEXTURE_EXR','EXR chunk spans overlap, contain gaps or leave trailing data.')
    return pixels,len(channels),max(16 if dtype==1 else 32 for _,dtype in channels)


def _png16(data, limits):
    """Precision-preserving noninterlaced 16-bit grayscale/RGB(+alpha) PNG."""
    import struct,zlib
    np=_np()
    width,height,depth,color,compression,filtering,interlace=struct.unpack_from('>IIBBBBB',data,16)
    require(depth==16 and color in (0,2,4,6) and compression==filtering==interlace==0,
            'TEXTURE_FORMAT','16-bit PNG requires noninterlaced grayscale/RGB with optional alpha.')
    channels={0:1,2:3,4:2,6:4}[color];bpp=channels*2;stride=width*bpp
    _size(width,height,limits,160)
    offset=8;pieces=[];transparent=None
    while offset<len(data):
        length=struct.unpack_from('>I',data,offset)[0];kind=data[offset+4:offset+8]
        if kind==b'IDAT':pieces.append(data[offset+8:offset+8+length])
        if kind==b'tRNS':
            require(transparent is None and color in (0,2) and length==(2 if color==0 else 6),
                    'TEXTURE_DECODE','Invalid or repeated 16-bit PNG transparency key.')
            transparent=data[offset+8:offset+8+length]
        offset+=length+12
    expected=(stride+1)*height
    inflater=zlib.decompressobj()
    try:raw=inflater.decompress(b''.join(pieces),expected+1)
    except zlib.error as exc:raise RigError('TEXTURE_DECODE','Invalid 16-bit PNG raster.') from exc
    require(len(raw)==expected and inflater.eof and not inflater.unused_data and not inflater.unconsumed_tail,
            'TEXTURE_DECODE','Invalid 16-bit PNG decoded length.')
    encoded=np.frombuffer(raw,np.uint8).reshape(height,stride+1)
    reconstructed=np.empty((height,stride),np.uint8)
    for y in range(height):
        mode=int(encoded[y,0]);current=encoded[y,1:].astype(np.int32)
        previous=reconstructed[y-1].astype(np.int32) if y else np.zeros(stride,np.int32)
        require(0<=mode<=4,'TEXTURE_DECODE','Invalid PNG row filter.')
        if mode==1:
            current=np.cumsum(current.reshape(width,bpp),axis=0,dtype=np.int64).reshape(-1)%256
        elif mode==2:current=(current+previous)%256
        elif mode in (3,4):
            for x in range(0,stride,bpp):
                left=current[x-bpp:x] if x else np.zeros(bpp,np.int32)
                above=previous[x:x+bpp]
                if mode==3:predict=(left+above)//2
                else:
                    upperleft=previous[x-bpp:x] if x else np.zeros(bpp,np.int32)
                    estimate=left+above-upperleft
                    a=abs(estimate-left);b=abs(estimate-above);c=abs(estimate-upperleft)
                    predict=np.where((a<=b)&(a<=c),left,np.where(b<=c,above,upperleft))
                current[x:x+bpp]=(current[x:x+bpp]+predict)%256
        reconstructed[y]=current
    integers=reconstructed.reshape(-1).view('>u2').reshape(height,width,channels)
    samples=integers.astype(np.float32)/65535
    pixels=np.ones((height,width,4),np.float32)
    if color in (0,4):pixels[:,:,:3]=samples[:,:,:1]
    else:pixels[:,:,:3]=samples[:,:,:3]
    if color in (4,6):pixels[:,:,3]=samples[:,:,-1]
    elif transparent is not None:
        key=np.frombuffer(transparent,dtype='>u2')
        pixels[:,:,3]=(~np.all(integers==key,axis=-1)).astype(np.float32)
    return pixels,channels


def decode_texture(data, *, role, encoding, limits=Limits()):
    """Decode exact supplied bytes, with budget admission before raster allocation.

    PNG/JPEG reuse the rig importer's strict container/entropy verifier. WebP is
    static, fully decoded and must have an exact RIFF length. HDR is scene-linear.
    EXR admits only bounded RGB(A) scanline half/float NONE/ZIPS/ZIP layouts.
    Multipart, tiled, deep and other compressed layouts fail before decoding.
    """
    np = _np()
    require(isinstance(data, bytes) and 0 < len(data) <= limits.encoded_bytes,
            "TEXTURE_LIMIT", "Expected nonempty encoded bytes within the source budget.")
    require(role in COLOR_ROLES | DATA_ROLES, "TEXTURE_ROLE", "Unsupported texture role.")
    require(encoding in ({"srgb", "linear-srgb"} if role in COLOR_ROLES else {"data"}),
            "TEXTURE_ENCODING", "Texture role and encoding disagree.")
    if data.startswith((b"#?RADIANCE\n", b"#?RGBE\n")):
        require(encoding == "linear-srgb" and role in COLOR_ROLES, "TEXTURE_ENCODING", "Radiance RGBE is scene-linear color.")
        pixels = _radiance(data, limits)
        format_name = "HDR"
        channels, bit_depth = 3, 32
    elif data.startswith(b'v/1\x01'):
        require(encoding == 'linear-srgb' and role in COLOR_ROLES, 'TEXTURE_ENCODING', 'RGB(A) EXR inputs require scene-linear color.')
        pixels, channels, bit_depth = _exr(data, limits)
        format_name = 'EXR'
    else:
        from PIL import Image, ImageFile
        require(not ImageFile.LOAD_TRUNCATED_IMAGES, "TEXTURE_POLICY", "Truncated-image loading must remain disabled.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    format_name = image.format
                    channels = len(image.getbands())
                    bit_depth = 32 if image.mode == "F" else (16 if image.mode.startswith("I") else 8)
                    if image.mode == "P": channels = 4 if "transparency" in image.info else 3
                    _size(image.width, image.height, limits)
                    require(format_name in ("PNG", "JPEG", "WEBP") and getattr(image, "n_frames", 1) == 1,
                            "TEXTURE_FORMAT", "Supported static formats are PNG, JPEG, WebP and Radiance HDR; EXR requires the renderer decoder.")
                    if format_name in ("PNG", "JPEG"):
                        from rig_import import _decode_texture
                        _decode_texture(data, {"source_bytes": limits.encoded_bytes, "memory_bytes": limits.working_bytes})
                    else:
                        require(data[:4] == b"RIFF" and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data),
                                "TEXTURE_DECODE", "Invalid WebP container length.")
                    image.load()
                    if format_name == 'PNG' and data[24] == 16:
                        pixels, channels = _png16(data, limits)
                        bit_depth = 16
                    elif image.mode.startswith("I;16") or image.mode in ("I", "F"):
                        require(role in DATA_ROLES, "TEXTURE_ENCODING", "High-bit scalar raster requires a data role.")
                        scalar = np.array(image, dtype=np.float32) / (65535 if image.mode != "F" else 1)
                        pixels = np.stack((scalar, scalar, scalar, np.ones_like(scalar)), axis=-1)
                    else:
                        pixels = np.array(image.convert("RGBA"), dtype=np.float32) / 255
        except RigError: raise
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise RigError("TEXTURE_DECODE", "Texture cannot be decoded safely: " + str(exc)) from exc
        if encoding == "srgb": pixels[:, :, :3] = srgb_to_linear(pixels[:, :, :3])
    require(np.isfinite(pixels).all() and (pixels >= 0).all() and (pixels[:,:,3] <= 1).all(),
            "TEXTURE_PIXELS", "Decoded pixels must be finite/nonnegative with alpha in [0,1].")
    return DecodedTexture(pixels.astype(np.float32), format_name, hashlib.sha256(data).hexdigest(), role, encoding, channels, bit_depth)


def verify_texture_document(document, payload, *, limits=Limits()):
    """Verify canonical Texture3D metadata against actual frozen source content."""
    from lookdev_contracts import validate_resource
    validate_resource(document)
    require(document['kind'] == 'texture3d', 'TEXTURE_KIND', 'Expected a Texture3D resource.')
    require(isinstance(payload, bytes) and len(payload) == document['file']['bytes']
            and hashlib.sha256(payload).hexdigest() == document['file']['sha256'],
            'TEXTURE_HASH', 'Texture bytes differ from the exact canonical reference.')
    decoded = decode_texture(payload, role=document['role'], encoding=document['encoding'], limits=limits)
    require((decoded.width, decoded.height, decoded.channels, decoded.bit_depth) ==
            (document['width'], document['height'], document['channels'], document['bit_depth']),
            'TEXTURE_METADATA', 'Texture dimensions/channels/bit depth differ from decoded content.')
    return decoded


def encode_png(pixels, *, encoding="data", limits=Limits()):
    from PIL import Image
    np = _np()
    require(isinstance(pixels, np.ndarray) and pixels.ndim == 3 and pixels.shape[2] in (3, 4),
            "TEXTURE_PIXELS", "Expected an RGB/RGBA pixel array.")
    _size(int(pixels.shape[1]), int(pixels.shape[0]), limits)
    require(np.isfinite(pixels).all() and (pixels >= 0).all() and (pixels <= 1).all(),
            "TEXTURE_PIXELS", "PNG export requires explicit [0,1] display/data values; HDR clipping is not implicit.")
    require(encoding in ("srgb", "linear-srgb", "data"), "TEXTURE_ENCODING", "Invalid encoding.")
    converted = pixels.copy()
    if encoding == "srgb": converted[:, :, :3] = linear_to_srgb(converted[:, :, :3])
    output = io.BytesIO()
    Image.fromarray(np.rint(converted * 255).astype(np.uint8)).save(output, format="PNG", compress_level=9)
    data = output.getvalue()
    require(len(data) <= limits.output_bytes, "TEXTURE_LIMIT", "PNG exceeds output budget.")
    return data


def procedural_maps(kind, *, width=256, height=256, seed=0, scale=1.0, frequency=8.0,
                    variation=0.4, turbulence=0.25, color_low=(0.08, 0.04, 0.02),
                    color_high=(0.6, 0.4, 0.2), limits=Limits()):
    """Actual deterministic base-color/height/normal/roughness/metallic map set.

    RGB colors are linear-sRGB; no texture is represented as provider output.
    Procedural detail is analytic and periodic, with seeded phases.
    """
    np = _np()
    require(kind in KINDS, "TEXTURE_GENERATOR", "Unknown procedural material.")
    _size(width, height, limits, 192)
    require(type(seed) is int and 0 <= seed <= 2**32-1, "TEXTURE_SEED", "Seed must be uint32.")
    scale = _finite(scale, 0.001, 1000, "scale")
    frequency = _finite(frequency, 0.01, 1024, "frequency")
    variation = _finite(variation, 0, 1, "variation")
    turbulence = _finite(turbulence, 0, 1, "turbulence")
    low, high = np.asarray(color_low, np.float32), np.asarray(color_high, np.float32)
    require(low.shape == high.shape == (3,) and np.isfinite(low).all() and np.isfinite(high).all()
            and (low >= 0).all() and (high <= 1).all() and (high >= low).all(),
            "TEXTURE_COLOR", "Colors must be ordered linear RGB values in [0,1].")
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    x, y = x / width * scale, y / height * scale
    phase = rng.uniform(-math.pi, math.pi, 6)
    noise = (np.sin(2*math.pi*(x*3+y*5)+phase[0]) + np.sin(2*math.pi*(x*11-y*7)+phase[1])
             + np.cos(2*math.pi*(x*23+y*19)+phase[2])) / 6 + 0.5
    warp = turbulence * np.sin(2*math.pi*y*3 + phase[3])
    wave = np.sin(2*math.pi*frequency*(x+warp)) * 0.5 + 0.5
    if kind == "wood": field = np.sin(2*math.pi*frequency*np.sqrt((x-.5)**2+(y-.5)**2)+warp*4) * .5 + .5
    elif kind == "marble": field = wave**8
    elif kind == "fabric": field = (np.cos(2*math.pi*x*frequency)*np.cos(2*math.pi*y*frequency)+1)/2
    elif kind == "leather": field = np.clip((noise-.35)*2, 0, 1)**2
    elif kind == "scratched-metal": field = np.maximum(0, wave-.92)*12
    elif kind == "brushed-metal": field = wave*.65+noise*.35
    elif kind == "sand": field = (np.sin(x*frequency*59+phase[4])*np.sin(y*frequency*61+phase[5])+1)/2
    elif kind == "toon-ramp": field = np.floor(x / scale * 4) / 3
    elif kind == "ceramic": field = noise**4
    elif kind == "painted-plastic": field = noise*.2+.4
    elif kind == "concrete": field = noise*.7+.15
    else: field = noise
    field = np.clip(.5+(field-.5)*variation*2, 0, 1).astype(np.float32)
    rgb = low + field[:, :, None]*(high-low)
    height_map = field * (0.12 if kind in ("clay", "painted-plastic", "ceramic") else 1)
    roughness = np.clip(.25 + field*.65, 0, 1)
    if kind in ("brushed-metal", "scratched-metal"): roughness *= .55
    elif kind == "ceramic": roughness *= .35
    def rgba(values):
        if values.ndim == 2: values = np.repeat(values[:, :, None], 3, axis=2)
        return np.concatenate((values, np.ones((height, width, 1), np.float32)), axis=2)
    maps = {"base-color": encode_png(rgba(rgb), encoding="srgb", limits=limits),
            "height": encode_png(rgba(height_map), limits=limits),
            "roughness": encode_png(rgba(roughness), limits=limits),
            "metallic": encode_png(rgba(np.full((height, width), float("metal" in kind), np.float32)), limits=limits),
            "normal": encode_png(normal_from_height(height_map), limits=limits)}
    require(sum(map(len, maps.values())) <= limits.output_bytes, "TEXTURE_LIMIT", "Map-set output budget exceeded.")
    return {"maps": maps, "recipe": {"generator": "lookdev-procedural", "version": VERSION, "kind": kind,
            "width": width, "height": height, "seed": seed, "scale": scale, "frequency": frequency,
            "variation": variation, "turbulence": turbulence, "color_low": low.tolist(), "color_high": high.tolist(),
            "normal_orientation": "opengl-positive-y", "origin": "procedural", "review_status": "unreviewed"}}


def normal_from_height(values, *, strength=1.0, wrap="repeat"):
    np = _np()
    require(isinstance(values, np.ndarray) and values.ndim == 2 and min(values.shape) >= 1
            and values.size <= 4*1024*1024 and max(values.shape)<=16384 and np.isfinite(values).all()
            and (values>=0).all() and (values<=1).all(), "TEXTURE_HEIGHT", "Expected bounded normalized finite height values.")
    values = values.astype(np.float64)
    strength = _finite(strength, 0, 1000, "normal strength")
    require(wrap in ("repeat", "clamp", "mirror"), "TEXTURE_WRAP", "Unsupported wrap mode.")
    mode = {"repeat": "wrap", "clamp": "edge", "mirror": "symmetric"}[wrap]
    padded = np.pad(values, 1, mode=mode)
    dx = (padded[1:-1, 2:]-padded[1:-1, :-2]) * strength * values.shape[1]/2
    # Raster rows increase downwards; glTF v increases upward in tangent space.
    dy = (padded[:-2, 1:-1]-padded[2:, 1:-1]) * strength * values.shape[0]/2
    normals = np.stack((-dx, -dy, np.ones_like(dx)), axis=-1)
    normals /= np.linalg.norm(normals, axis=-1, keepdims=True)
    return np.concatenate((normals*.5+.5, np.ones((*values.shape, 1), np.float32)), axis=-1).astype(np.float32)


def mipmaps(texture, *, limits=Limits()):
    """Area-filter in linear light/data space; renormalize tangent normals."""
    np = _np()
    pixels = texture.pixels
    _size(texture.width, texture.height, limits, 128)
    result = []
    while pixels.shape[0] > 1 or pixels.shape[1] > 1:
        h, w = pixels.shape[:2]
        nh, nw = max(1, h//2), max(1, w//2)
        # Piecewise-constant area integration is vectorized and handles odd
        # dimensions without dropping the last row/column.
        def downsample(values, count, axis):
            moved = np.moveaxis(values, axis, 0)
            length = len(moved)
            cumulative = np.concatenate((np.zeros_like(moved[:1], dtype=np.float64),
                                         np.cumsum(moved, axis=0, dtype=np.float64)), axis=0)
            edges = np.linspace(0, length, count+1)
            indices = np.floor(edges).astype(int)
            tail = np.minimum(indices, length-1)
            fractions = (edges-indices).reshape((-1,)+(1,)*(moved.ndim-1))
            integrated = cumulative[indices] + moved[tail]*fractions
            result = np.diff(integrated, axis=0)/(length/count)
            return np.moveaxis(result.astype(np.float32), 0, axis)
        working = pixels
        if texture.role in COLOR_ROLES:
            working = pixels.copy()
            working[:,:,:3] *= working[:,:,3:4]
        output = downsample(downsample(working, nh, 0), nw, 1)
        if texture.role in COLOR_ROLES:
            output[:,:,:3] = np.divide(output[:,:,:3], output[:,:,3:4],
                                      out=np.zeros_like(output[:,:,:3]), where=output[:,:,3:4]>1e-8)
        if texture.role == "normal":
            normals = output[:,:,:3]*2-1
            norm = np.linalg.norm(normals, axis=-1, keepdims=True)
            normals = np.divide(normals, norm, out=np.zeros_like(normals), where=norm>1e-8)
            normals[norm[:,:,0]<=1e-8] = (0,0,1)
            output[:,:,:3] = normals*.5+.5
        result.append(output)
        pixels = output
    return result


def pack_channels(textures, *, channels=("r", "r", "r"), limits=Limits()):
    """Pack actual scalar data maps, conventionally occlusion/roughness/metallic."""
    np = _np()
    require(len(textures) == len(channels) == 3, "TEXTURE_PACK", "Exactly three scalar data maps are required.")
    first = textures[0]
    _size(first.width, first.height, limits)
    out = np.ones((first.height, first.width, 4), np.float32)
    for i, (texture, channel) in enumerate(zip(textures, channels)):
        require(texture.encoding == "data" and texture.role in DATA_ROLES and channel in "rgba" and len(channel)==1,
                "TEXTURE_PACK", "Packing requires explicit scalar data channels.")
        require(texture.pixels.shape == out.shape, "TEXTURE_PACK", "Packed maps must have identical dimensions.")
        out[:,:,i] = texture.pixels[:,:,"rgba".index(channel)]
    return encode_png(out, limits=limits)


def transform_uv(uv, *, scale=(1,1), offset=(0,0), rotation=0.0, wrap="repeat"):
    """glTF KHR_texture_transform order: scale, rotate around origin, translate."""
    np = _np()
    values = np.asarray(uv, np.float64)
    scale, offset = np.asarray(scale, np.float64), np.asarray(offset, np.float64)
    require(values.ndim==2 and values.shape[1]==2 and len(values)<=1_000_000 and scale.shape==offset.shape==(2,)
            and np.isfinite(values).all() and np.isfinite(scale).all() and np.isfinite(offset).all(),
            "TEXTURE_UV", "UV transform requires bounded finite Nx2 UVs and 2D scale/offset.")
    rotation = _finite(rotation, -1e9, 1e9, "UV rotation")
    require(wrap in ("repeat", "clamp", "mirror", "none"), "TEXTURE_WRAP", "Unsupported wrap mode.")
    c,s=math.cos(rotation),math.sin(rotation)
    result=(values*scale) @ np.array(((c,s),(-s,c))) + offset
    require(np.isfinite(result).all(), "TEXTURE_UV", "UV transform overflowed.")
    if wrap=="repeat":result=result-np.floor(result)
    elif wrap=="clamp":result=np.clip(result,0,1)
    elif wrap=="mirror":result=1-np.abs((result%2)-1)
    return result


def validate_uvs(uv_sets, triangles, *, tangents=None, max_pairs=1_000_000):
    """Report missing, degenerate, mirrored and overlapping UV triangles.

    Overlap uses positive-area convex clipping, so shared edges are not overlaps.
    Tangents are the existing glTF XYZW convention and must have unit XYZ and W
    equal to +/-1. Tangent generation remains owned by the rig importer.
    """
    np = _np()
    require(type(max_pairs) is int and 0<=max_pairs<=1_000_000, "TEXTURE_LIMIT", "Invalid pair-work bound.")
    faces=np.asarray(triangles)
    require(faces.ndim==2 and faces.shape[1]==3 and len(faces)<=100_000 and np.issubdtype(faces.dtype,np.integer)
            and (faces>=0).all(), "TEXTURE_UV", "Expected bounded nonnegative integer triangle indices.")
    require(isinstance(uv_sets,(list,tuple)) and len(uv_sets)<=8, "TEXTURE_UV", "At most eight UV sets are supported.")
    report={"missing":not bool(uv_sets),"sets":[],"tangent_convention":"gltf-xyzw-opengl-positive-y"}
    def cross(a,b): return float(a[0]*b[1]-a[1]*b[0])
    def area(poly):return abs(sum(cross(poly[i],poly[(i+1)%len(poly)]) for i in range(len(poly))))/2 if len(poly)>2 else 0
    def overlap(a,b):
        poly=[p.copy() for p in a]
        if cross(b[1]-b[0],b[2]-b[0])<0:b=b[::-1]
        for j in range(3):
            p,q=b[j],b[(j+1)%3];old=poly;poly=[]
            if not old:break
            for k,current in enumerate(old):
                previous=old[k-1];dc=cross(q-p,current-p);dp=cross(q-p,previous-p)
                if (dc>=0)!=(dp>=0):poly.append(previous+(current-previous)*(dp/(dp-dc)))
                if dc>=0:poly.append(current)
        return area(poly)>1e-12
    vertex_count=None
    for index,values in enumerate(uv_sets):
        values=np.asarray(values,np.float64)
        require(values.ndim==2 and values.shape[1]==2 and len(values)<=1_000_000 and np.isfinite(values).all()
                and (not faces.size or int(faces.max())<len(values)), "TEXTURE_UV", "UV set does not cover finite mesh vertices.")
        if vertex_count is None:vertex_count=len(values)
        require(len(values)==vertex_count,"TEXTURE_UV","UV sets must cover the same vertices.")
        mapped=values[faces];signed=np.array([cross(t[1]-t[0],t[2]-t[0])/2 for t in mapped])
        pairs=[];tested=0;exhausted=False
        for i in range(len(faces)):
            for j in range(i):
                if tested>=max_pairs:exhausted=True;break
                tested+=1
                if np.any(mapped[i].max(axis=0)<=mapped[j].min(axis=0)) or np.any(mapped[j].max(axis=0)<=mapped[i].min(axis=0)):continue
                if overlap(mapped[i],mapped[j]):pairs.append([j,i])
            if exhausted:break
        report['sets'].append({"set":index,"degenerate_triangles":np.flatnonzero(np.abs(signed)<=1e-12).tolist(),
                 "mirrored_triangles":np.flatnonzero(signed<0).tolist(),"overlap_pairs":pairs,
                 "overlap_complete":not exhausted,"overlap_pairs_tested":tested})
    if tangents is not None:
        tangent=np.asarray(tangents,np.float64)
        require(vertex_count is not None and tangent.shape==(vertex_count,4) and np.isfinite(tangent).all()
                and np.allclose(np.linalg.norm(tangent[:,:3],axis=1),1,atol=1e-5)
                and np.isin(tangent[:,3],(-1,1)).all(),"TEXTURE_TANGENT","Tangents require unit XYZ and +/-1 handedness.")
    return report


def project_uvs(positions, *, mode="planar", axis="z"):
    np=_np();vertices=np.asarray(positions,np.float64)
    require(vertices.ndim==2 and vertices.shape[1]==3 and len(vertices)<=1_000_000 and np.isfinite(vertices).all(),
            "TEXTURE_UV","Expected bounded finite positions.")
    require(mode in ("planar","cylindrical","spherical") and axis in "xyz" and len(axis)==1,
            "TEXTURE_UV","Projection supports planar/cylindrical/spherical; box/triplanar require per-face shader coordinates.")
    rotated=vertices[:,[("xyz".index(axis)+1)%3,("xyz".index(axis)+2)%3,"xyz".index(axis)]]
    if mode=="planar":return rotated[:,:2].copy()
    u=np.arctan2(rotated[:,1],rotated[:,0])/(2*math.pi)+.5
    if mode=="cylindrical":v=rotated[:,2]
    else:
        radius=np.linalg.norm(rotated,axis=1)
        require((radius>0).all(),"TEXTURE_UV","Spherical projection is undefined at the origin.")
        v=np.arccos(np.clip(rotated[:,2]/radius,-1,1))/math.pi
    return np.stack((u,v),axis=1)


def atlas_udim(tiles, *, limits=Limits()):
    """Explicit tiled-image atlas conversion; caller remaps UVs using receipt scale."""
    np=_np()
    require(isinstance(tiles,dict) and 1<=len(tiles)<=100,"TEXTURE_UDIM","Expected 1..100 UDIM tiles.")
    require(all(type(n) is int and 1001<=n<=1100 for n in tiles),"TEXTURE_UDIM","UDIM numbers must be 1001..1100.")
    first=next(iter(tiles.values()));columns=max((n-1001)%10 for n in tiles)+1;rows=max((n-1001)//10 for n in tiles)+1
    _size(first.width*columns,first.height*rows,limits)
    out=np.zeros((first.height*rows,first.width*columns,4),np.float32)
    for number,tile in tiles.items():
        require(tile.width==first.width and tile.height==first.height and tile.encoding==first.encoding and tile.role==first.role,
                "TEXTURE_UDIM","UDIM tiles must have identical dimensions/role/encoding.")
        x=(number-1001)%10;y=(number-1001)//10
        # Pixel rows run top-down; UDIM v rows increase upward.
        out[(rows-1-y)*first.height:(rows-y)*first.height,x*first.width:(x+1)*first.width]=tile.pixels
    return {"png":encode_png(out,encoding=first.encoding,limits=limits),"uv_scale":[1/columns,1/rows],
            "tiles":sorted(tiles),"missing_tiles":[1001+y*10+x for y in range(rows) for x in range(columns) if 1001+y*10+x not in tiles],
            "columns":columns,"rows":rows}


def publish_texture(source, destination, *, role, encoding, attribution, license_id, operations=(), limits=Limits()):
    """Exclusive immutable source package. Existing/racing paths are never replaced.

    The destination parent must already be a real directory. Sources are supplied
    as bytes so this function cannot follow an untrusted asset filename.
    """
    require(isinstance(attribution,str) and 0<len(attribution)<=4096 and isinstance(license_id,str) and 0<len(license_id)<=256,
            "TEXTURE_PROVENANCE","Explicit attribution and license are required.")
    operations = list(operations)
    finite_ops=canonical_bytes(operations)
    require(len(finite_ops)<=65536,"TEXTURE_LIMIT","Operation metadata exceeds its budget.")
    texture=decode_texture(source,role=role,encoding=encoding,limits=limits)
    destination=Path(destination).absolute();parent=destination.parent
    # Reject every parent symlink before creating files. Publication callers own
    # this parent; the returned receipt makes later byte verification explicit.
    for part in (parent,*parent.parents):
        require(not part.is_symlink() and part.is_dir(),"TEXTURE_PATH","Destination parents must be existing real directories.")
    require(not destination.exists() and not destination.is_symlink(),"TEXTURE_EXISTS","Destination already exists.")
    receipt={"schema_version":1,"generator_version":VERSION,"source":{"sha256":texture.source_sha256,"bytes":len(source),
             "file":"source.bin","format":texture.format,"width":texture.width,"height":texture.height,"role":role,"encoding":encoding},
             "attribution":attribution,"license":license_id,"operations":list(operations),"review_status":"unreviewed"}
    require(os.name == 'posix' and (sys.platform.startswith('linux') or sys.platform == 'darwin'),
            'TEXTURE_PLATFORM', 'Safe texture-package publication currently requires Linux/macOS; use the asset-library owner on other systems.')
    fd = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY)
    stage_name = '.texture-stage-' + secrets.token_hex(16)
    stage_fd = None
    created = False
    try:
        for part in parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd);fd = child
        os.mkdir(stage_name, mode=0o700, dir_fd=fd);created = True
        stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        for name, data in (('source.bin', source), ('receipt.json', canonical_bytes(receipt))):
            file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage_fd)
            with os.fdopen(file_fd, 'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.fsync(stage_fd)
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
            operation = library.renameat2
            operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            operation.restype = ctypes.c_int
            status = operation(fd, os.fsencode(stage_name), fd, os.fsencode(destination.name), 1)
        elif sys.platform == 'darwin' and hasattr(library, 'renameatx_np'):
            operation = library.renameatx_np
            operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            operation.restype = ctypes.c_int
            status = operation(fd, os.fsencode(stage_name), fd, os.fsencode(destination.name), 4)
        else:
            raise RigError('TEXTURE_PLATFORM', 'Atomic relative no-replace publication is unavailable.')
        if status:
            error=ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        created = False
        os.fsync(fd)
    finally:
        if stage_fd is not None:
            if created:
                for name in ('source.bin','receipt.json'):
                    try:os.unlink(name,dir_fd=stage_fd)
                    except FileNotFoundError:pass
            os.close(stage_fd)
        if created:os.rmdir(stage_name,dir_fd=fd)
        os.close(fd)

    return receipt


def create_bpy_image(data, *, name, role, encoding, limits=Limits()):
    """Build a packed Blender image from already validated decoded pixels.

    No source path or native Blender decoder touches untrusted bytes. Returned
    image pixels are scene-linear/data and its source hash is a custom property.
    Caller owns removal if scene construction is rolled back.
    """
    require(isinstance(name, str) and 0 < len(name) <= 128, "TEXTURE_NAME", "Expected a bounded image name.")
    texture = decode_texture(data, role=role, encoding=encoding, limits=limits)
    return create_blender_image(texture, name=name)


def create_blender_image(texture, *, name):
    """Install a decoded texture without asking Blender to decode source bytes."""
    np = _np()
    require(isinstance(texture, DecodedTexture) and isinstance(name, str) and 0<len(name)<=128,
            'TEXTURE_IMAGE', 'Expected a decoded texture and bounded name.')
    _size(texture.width, texture.height, Limits())
    require(texture.pixels.shape == (texture.height, texture.width, 4)
            and np.isfinite(texture.pixels).all() and (texture.pixels>=0).all(),
            'TEXTURE_IMAGE', 'Decoded image shape/values are invalid.')
    import bpy
    image = bpy.data.images.new(name, width=texture.width, height=texture.height,
                                alpha=True, float_buffer=True, is_data=True)
    try:
        image.colorspace_settings.name = 'Non-Color'
        # Blender pixel arrays start at the lower-left; decoded raster rows do not.
        image.pixels.foreach_set(texture.pixels[::-1].reshape(-1))
        image['source_sha256'] = texture.source_sha256
        image['source_encoding'] = texture.encoding
        image['texture_role'] = texture.role
        image.pack()
    except Exception:
        bpy.data.images.remove(image)
        raise
    return image
