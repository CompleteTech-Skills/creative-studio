"""Caller-controlled, conservative admission for native rig loading.

This bounds admitted input/working-set estimates, not process RSS or later rendering.
No source document is rewritten to apply runtime policy.
"""
from __future__ import annotations

import io
import hashlib
import math
from pathlib import Path
import struct
import warnings
import zipfile

import numpy as np

from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, MAX_JSON_BYTES, decode_json, require, open_payload


# Includes streamed hash buffers, ZIP/header probes and the image header reader.
_IO_RESERVE = 2 * 1024**2
_JSON_FACTOR = 64


def effective_limits(declared: dict, requested: dict | None) -> dict:
    """Take restrictive integer-valued runtime copies, never changing declared data."""
    require(requested is None or isinstance(requested, dict), 'RIG_LIMITS',
            'Caller limits must be an object.')
    require(not requested or set(requested) <= set(DEFAULT_LIMITS), 'RIG_LIMITS',
            'Unknown caller resource limit.')
    result = {}
    for key in DEFAULT_LIMITS:
        maximum = max(DEFAULT_LIMITS[key], 8 if key == 'influences' else DEFAULT_LIMITS[key])
        values = [declared[key]]
        if requested and key in requested:
            values.append(requested[key])
        for value in values:
            require(type(value) in (int, float) and 1 <= value <= maximum
                    and (type(value) is int or math.isfinite(value) and value.is_integer()),
                    'RIG_LIMITS', 'Resource limits must be bounded positive integers.', asset=key)
        result[key] = min(int(value) for value in values)
    return result


class LoadBudget:
    """Conservative aggregate estimate; charge before materializing each input."""
    def __init__(self, limits: dict):
        self.limits = limits
        self.memory = 0
        self.files = {}
        self.input_bytes = 0
        self.json_bytes = 0
        self.array_bytes = 0
        self.texture_rgba_bytes = 0
        self.add(_IO_RESERVE, 'streaming/header scratch reserve')

    def add(self, amount: int, asset: str) -> None:
        self.memory += amount
        require(self.memory <= self.limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
                'Aggregate native load estimate exceeds the caller/manifest budget.',
                asset=asset, expected=self.limits['memory_bytes'], observed=self.memory)

    def file(self, path: Path, *, source=False) -> int:
        with open_payload(path.parent, path.name) as (_, info):
            size = info.st_size
        cap = self.limits['source_bytes'] if source else self.limits['dependency_bytes']
        require(size <= cap, 'RIG_LIMIT_BINARY', 'Input exceeds its byte budget.',
                asset=path.name, expected=cap, observed=size)
        require(path not in self.files or self.files[path] == size, 'RIG_SOURCE_CHANGED',
                'Input size changed during admission.', asset=path.name)
        if path not in self.files:
            self.files[path] = size
            self.input_bytes += size
            require(self.input_bytes <= self.limits['dependency_bytes'], 'RIG_LIMIT_BINARY',
                    'Aggregate native input files exceed the dependency byte budget.',
                    expected=self.limits['dependency_bytes'], observed=self.input_bytes)
        return size

    def hash(self, path: Path):
        size = self.file(path)
        digest = hashlib.sha256()
        with open_payload(path.parent, path.name) as (stream, _):
            remaining = size
            while remaining:
                data = stream.read(min(1024 * 1024, remaining))
                require(data, 'RIG_SOURCE_CHANGED', 'Input shrank during hashing.', asset=path.name)
                digest.update(data)
                remaining -= len(data)
            require(stream.read(1) == b'', 'RIG_SOURCE_CHANGED', 'Input grew during hashing.', asset=path.name)
        return digest.hexdigest()

    def charge_json(self, path: Path):
        size = self.file(path)
        require(size <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON', 'JSON exceeds 32 MiB.', asset=path.name)
        self.add(size * _JSON_FACTOR, path.name + ': JSON object estimate')
        self.json_bytes += size
        return size

    def json(self, path: Path):
        size = self.charge_json(path)
        # Bounded read also rejects a file growing between stat and read.
        with open_payload(path.parent, path.name) as (stream, _):
            payload = stream.read(size + 1)
        require(len(payload) == size, 'RIG_SOURCE_CHANGED', 'JSON changed during admission.', asset=path.name)
        return decode_json(payload)

    def report(self):
        return dict(method='native-load-conservative-v1', estimated_memory_bytes=self.memory,
                    input_bytes=self.input_bytes, array_bytes=self.array_bytes,
                    texture_rgba_bytes=self.texture_rgba_bytes, json_bytes=self.json_bytes,
                    scope='loading only; not process RSS, evaluation or rendering')


def _zip_directory(payload: bytes, budget: LoadBudget):
    """Bound the ZIP directory before zipfile allocates its complete entry list."""
    size = len(payload)
    tail = payload[-65557:]
    offset = tail.rfind(b'PK\x05\x06')
    require(offset >= 0 and offset + 22 <= len(tail), 'RIG_BINARY', 'Missing ZIP directory.')
    disk, directory_disk, disk_count, count, directory_size, directory_offset, comment = struct.unpack_from('<4H2LH', tail, offset + 4)
    require(disk == directory_disk == 0 and disk_count == count and count != 65535
            and directory_offset != 0xffffffff and directory_size != 0xffffffff
            and offset + 22 + comment == len(tail)
            and directory_offset + directory_size == size - len(tail) + offset,
            'RIG_BINARY', 'Unsupported or malformed native ZIP directory.')
    require(count <= budget.limits['archive_files'], 'RIG_LIMIT_BINARY', 'Too many binary members.')
    # Do not trust EOCD's entry count: zipfile itself walks the entire central
    # directory, including entries beyond a forged count, before exposing infos.
    cursor = directory_offset
    end = directory_offset + directory_size
    actual_count = 0
    while cursor < end:
        require(cursor + 46 <= end and payload[cursor:cursor+4] == b'PK\x01\x02',
                'RIG_BINARY', 'Malformed ZIP central directory entry.')
        name_size, extra_size, comment_size = struct.unpack_from('<3H', payload, cursor + 28)
        require(name_size > 0, 'RIG_BINARY', 'Empty ZIP member name.')
        cursor += 46 + name_size + extra_size + comment_size
        require(cursor <= end, 'RIG_BINARY', 'ZIP central directory length mismatch.')
        actual_count += 1
        require(actual_count <= budget.limits['archive_files'], 'RIG_LIMIT_BINARY', 'Too many binary members.')
    require(actual_count == count, 'RIG_BINARY', 'ZIP entry count differs from its directory.')
    budget.add(actual_count * 1024 + directory_size * 2, 'ZIP directory')


def read_bundle_member(archive, info):
    """Read admitted logical bytes without unbounded ZipExtFile decompression.

    read(-1) can allocate far beyond a forged central-directory file_size before
    zipfile truncates the output. Fixed-size requests bound each decompressor
    output independently of that untrusted declaration.
    """
    chunks = bytearray()
    with archive.open(info) as stream:
        remaining = info.file_size
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            require(chunk, 'RIG_BINARY', 'Truncated native ZIP member.', asset=info.filename)
            chunks.extend(chunk)
            remaining -= len(chunk)
        require(stream.read(1) == b'', 'RIG_BINARY', 'Native ZIP member exceeds its declared size.',
                asset=info.filename)
    return bytes(chunks)


def scan_bundle(path: Path, limits: dict, budget: LoadBudget, expected_hash=None):
    """Inspect all headers and image dimensions without allocating NumPy arrays."""
    size = budget.file(path)
    budget.add(size, path.name + ': immutable encoded bundle snapshot')
    with open_payload(path.parent, path.name) as (stream, _):
        snapshot = stream.read(size + 1)
    require(len(snapshot) == size, 'RIG_SOURCE_CHANGED', 'Binary changed during admission.', asset=path.name)
    require(expected_hash is None or hashlib.sha256(snapshot).hexdigest() == expected_hash,
            'RIG_HASH', 'Native bytes differ from their manifest.', asset=path.name)
    _zip_directory(snapshot, budget)
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            require(len(infos) <= limits['archive_files'] and len(names) == len(set(names))
                    and 'metadata.json' in names, 'RIG_BINARY', 'Binary needs bounded unique members and metadata.')
            expanded = sum(info.file_size for info in infos)
            require(expanded <= limits['archive_bytes'], 'RIG_LIMIT_BINARY', 'Expanded binary exceeds its byte budget.')
            # Covers live arrays, decompression buffers, geometry copies and validation temporaries.
            budget.add(expanded * 4, path.name + ': expanded binary working set')
            for info in infos:
                require(info.orig_filename == info.filename and '/' not in info.filename
                        and '\\' not in info.filename and (info.filename == 'metadata.json'
                        or info.filename.endswith('.npy')) and (info.external_attr >> 16) & 0o170000 != 0o120000,
                        'RIG_BINARY', 'Unsafe binary member name or type.')
            meta_info = archive.getinfo('metadata.json')
            require(meta_info.file_size <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON', 'Binary metadata exceeds 32 MiB.')
            budget.add(meta_info.file_size * _JSON_FACTOR, path.name + ': binary JSON estimate')
            budget.json_bytes += meta_info.file_size
            metadata = decode_json(read_bundle_member(archive, meta_info))
            require(isinstance(metadata, dict), 'RIG_BINARY', 'Binary metadata must be an object.')
            headers = {}
            for info in infos:
                if not info.filename.endswith('.npy'):
                    continue
                with archive.open(info) as stream:
                    version = np.lib.format.read_magic(stream)
                    require(version in ((1, 0), (2, 0)), 'RIG_BINARY', 'Unsupported NumPy array format.')
                    shape, fortran, dtype = (np.lib.format.read_array_header_1_0(stream) if version == (1, 0)
                                       else np.lib.format.read_array_header_2_0(stream))
                    require(0 < len(shape) <= 8 and not dtype.hasobject and dtype.kind in 'fiu'
                            and 0 < dtype.itemsize <= 16, 'RIG_BINARY', 'Unsafe NumPy array type or rank.')
                    count = 1
                    for dimension in shape:
                        require(type(dimension) is int and 0 <= dimension <= 2_000_000,
                                'RIG_BINARY', 'Invalid NumPy array dimension.')
                        count *= dimension
                        require(count * dtype.itemsize <= limits['memory_bytes'] // 4,
                                'RIG_LIMIT_MEMORY', 'NumPy header exceeds the load memory budget.')
                    array_bytes = count * dtype.itemsize
                    require(stream.tell() + array_bytes == info.file_size, 'RIG_BINARY',
                            'NumPy header and ZIP member byte count disagree.', asset=info.filename)
                    headers[info.filename[:-4]] = (shape, dtype, stream.tell(), fortran)
                    budget.array_bytes += array_bytes
            for image in metadata.get('images', []):
                require(isinstance(image, dict) and image.get('data') in headers, 'RIG_BINARY',
                        'Image refers to missing native data.')
                name = image['data']
                shape, dtype, offset, _ = headers[name]
                require(len(shape) == 1 and dtype == np.dtype('u1'), 'RIG_BINARY', 'Native image must be a byte vector.')
                with archive.open(name + '.npy') as stream:
                    stream.read(offset)
                    payload = stream.read(shape[0] + 1)
                require(len(payload) == shape[0], 'RIG_BINARY', 'Native image byte count changed.')
                try:
                    from PIL import Image
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', Image.DecompressionBombWarning)
                        with Image.open(io.BytesIO(payload)) as decoded:
                            width, height = decoded.size
                            require(decoded.format in ('PNG', 'JPEG') and width > 0 and height > 0,
                                    'RIG_TEXTURE_FORMAT', 'Expected native PNG/JPEG image data.')
                            pixels = width * height * 4
                except RigError:
                    raise
                except ImportError as exc:
                    raise RigError('RIG_DEPENDENCY', 'Pillow is required for native texture admission.') from exc
                except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
                    raise RigError('RIG_TEXTURE_FORMAT', 'Invalid or excessive native image header.', asset=name) from exc
                budget.texture_rgba_bytes += pixels
                budget.add(pixels * 4, name + ': decoded texture working set')
            return metadata, headers, snapshot
    except RigError:
        raise
    except (OSError, ValueError, EOFError, KeyError, TypeError, zipfile.BadZipFile, OverflowError, RuntimeError, struct.error) as exc:
        raise RigError('RIG_BINARY', 'Invalid native binary data: ' + str(exc), asset=path.name) from exc
