"""Warning-aware, bounded JPEG admission for the optional native 3D importer.

Source bytes are never repaired or encoded.  The dependency is imported only
when JPEG verification is requested, keeping ordinary 2D imports independent.
"""
from __future__ import annotations

import hashlib
from importlib import import_module, metadata

from rig_errors import RigError, require

DECODER_VERSION = '1.9.0'
MAX_PIXELS = 16_777_216
MAX_SCANS = 256


def _budget(value, name, maximum):
    require(type(value) is int and 0 < value <= maximum, 'RIG_LIMIT_TEXTURE',
            name + ' must be a positive bounded integer.')
    return value


def _scan_count(data: bytes, limit: int) -> int:
    """Bound marker/scan work; libjpeg-turbo owns entropy interpretation.

Length-delimited segments are skipped as bytes, not searched for false SOS
markers inside EXIF/ICC data.  Stuffed bytes and restart markers remain in the
entropy stream.  This is an admission budget, not a replacement JPEG decoder.
"""
    require(data.startswith(b'\xff\xd8'), 'RIG_TEXTURE_DECODE', 'JPEG SOI is missing.')
    offset, scans, entropy = 2, 0, False
    while offset < len(data):
        if entropy:
            offset = data.find(b'\xff', offset)
            require(offset >= 0, 'RIG_TEXTURE_DECODE', 'JPEG EOI is missing.')
        require(data[offset] == 255, 'RIG_TEXTURE_DECODE', 'Invalid JPEG marker framing.')
        while offset < len(data) and data[offset] == 255:
            offset += 1
        require(offset < len(data), 'RIG_TEXTURE_DECODE', 'Truncated JPEG marker.')
        marker = data[offset]
        offset += 1
        if entropy and (marker in (0, 1) or 208 <= marker <= 215):
            continue
        entropy = False
        if marker == 217:
            require(scans > 0 and offset == len(data), 'RIG_TEXTURE_DECODE',
                    'JPEG must end at EOI after at least one scan.')
            return scans
        require(marker not in (0, 216) and not 208 <= marker <= 215,
                'RIG_TEXTURE_DECODE', 'Unexpected JPEG standalone marker.')
        if marker == 1:  # TEM has no length field.
            continue
        require(offset + 2 <= len(data), 'RIG_TEXTURE_DECODE', 'Missing JPEG segment length.')
        length = int.from_bytes(data[offset:offset + 2], 'big')
        require(length >= 2 and length <= len(data) - offset,
                'RIG_TEXTURE_DECODE', 'Truncated JPEG segment.')
        offset += length
        if marker == 218:
            scans += 1
            require(scans <= limit, 'RIG_LIMIT_TEXTURE', 'JPEG scan budget exceeded.',
                    expected=limit, observed=scans)
            entropy = True
    raise RigError('RIG_TEXTURE_DECODE', 'JPEG EOI is missing.')


def _decoder():
    try:
        module = import_module('simplejpeg')
        version = metadata.version('simplejpeg')
    except (ImportError, metadata.PackageNotFoundError) as exc:
        raise RigError('RIG_TEXTURE_VERIFIER_UNAVAILABLE',
                       'Strict JPEG verification requires simplejpeg==' + DECODER_VERSION + '.',
                       correction='Install the optional requirements-rig.txt dependencies.') from exc
    require(version == DECODER_VERSION, 'RIG_TEXTURE_VERIFIER_UNAVAILABLE',
            'The installed JPEG verifier version is not the tested profile.',
            expected=DECODER_VERSION, observed=version,
            correction='Install the pinned optional requirements-rig.txt dependencies.')
    return module


def verify_jpeg(data: bytes, *, max_encoded_bytes: int, max_working_bytes: int,
                max_pixels: int = MAX_PIXELS, max_scans: int = MAX_SCANS,
                expected_size: tuple[int, int] | None = None) -> dict:
    """Fully decode with recoverable JPEG warnings treated as input errors.

The caller supplies its remaining working-memory allowance.  A conservative
16 bytes/pixel admission accounts for output and codec coefficient workspaces;
this is an estimate, not an OS-enforced allocator sandbox.  Encoded bytes,
dimensions and scan count are bounded before full pixel allocation.  Returned
metadata establishes numeric decode admission, never artistic approval.
"""
    require(type(data) is bytes, 'RIG_TEXTURE_DECODE', 'Supply immutable encoded JPEG bytes.')
    encoded = _budget(max_encoded_bytes, 'Encoded JPEG budget', 1 << 30)
    working = _budget(max_working_bytes, 'JPEG working budget', 1 << 40)
    pixels_limit = _budget(max_pixels, 'JPEG pixel budget', MAX_PIXELS)
    scans_limit = _budget(max_scans, 'JPEG scan budget', MAX_SCANS)
    require(0 < len(data) <= encoded, 'RIG_LIMIT_TEXTURE', 'Encoded JPEG byte budget exceeded.')
    scans = _scan_count(data, scans_limit)
    decoder = _decoder()
    try:
        height, width, colorspace, subsampling = decoder.decode_jpeg_header(data, strict=True)
        require(type(width) is int and type(height) is int and width > 0 and height > 0,
                'RIG_TEXTURE_DECODE', 'JPEG has invalid dimensions.')
        pixels = width * height
        require(pixels <= pixels_limit and pixels * 16 <= working,
                'RIG_LIMIT_TEXTURE', 'JPEG decoded pixel/working-memory budget exceeded.',
                expected={'pixels': pixels_limit, 'working_bytes': working},
                observed={'pixels': pixels, 'estimated_working_bytes': pixels * 16})
        if expected_size is not None:
            require(isinstance(expected_size, tuple) and len(expected_size) == 2
                    and all(type(v) is int and v > 0 for v in expected_size),
                    'RIG_TEXTURE_DECODE', 'Expected JPEG dimensions must be positive integer (width, height).')
            require(expected_size == (width, height), 'RIG_TEXTURE_DECODE',
                    'JPEG dimensions differ from the supplied source metadata.')
        output_color = 'CMYK' if colorspace.upper() in ('CMYK', 'YCCK') else (
            'GRAY' if colorspace.upper() == 'GRAY' else 'RGB')
        channels = {'CMYK': 4, 'GRAY': 1, 'RGB': 3}[output_color]
        decoded = decoder.decode_jpeg(data, colorspace=output_color, strict=True)
        require(decoded.shape == (height, width, channels) and decoded.dtype.name == 'uint8'
                and decoded.nbytes == pixels * channels,
                'RIG_TEXTURE_DECODE', 'JPEG verifier returned an invalid decoded raster.')
    except RigError:
        raise
    except (ValueError, RuntimeError, MemoryError, OverflowError) as exc:
        raise RigError('RIG_TEXTURE_DECODE',
                       'Strict JPEG decoding rejected source data: ' + str(exc)[:1000]) from exc
    return {'decoder': 'simplejpeg', 'decoder_version': DECODER_VERSION,
            'width': width, 'height': height, 'colorspace': colorspace,
            'subsampling': subsampling, 'scan_count': scans,
            'estimated_working_bytes': pixels * 16,
            'sha256': hashlib.sha256(data).hexdigest(), 'encoded_bytes': len(data),
            'strict_warnings': True}
