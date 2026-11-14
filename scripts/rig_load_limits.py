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
import binascii
import copy
import zipfile

import numpy as np

from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, MAX_JSON_BYTES, decode_json, require, open_payload


# Includes streamed hash buffers, ZIP/header probes and the image header reader.
_IO_RESERVE = 2 * 1024**2
_JSON_FACTOR = 64
# Bound both retained strings and dictionary growth, including zero-byte entries.
_RESERVATION_STRING_MAX = 256
_RESERVATION_COUNT_MAX = 65536
_RESERVATION_OVERHEAD = 4096
# A binary64 ratio has numerator <= 1024 bits and denominator <= 1075 bits.
# On supported CPython, two 1075-bit integers need at most 2 * 168 bytes;
# a ratio dict (184), list slot (8), and transient Fraction fit comfortably
# within 1024 bytes. Track allowance covers lists, dictionaries and duration.
_REPORT_KEY_BYTES = 1024


def effective_limits(declared: dict, requested: dict | None) -> dict:
    """Take restrictive integer-valued runtime copies, never changing declared data."""
    require(requested is None or isinstance(requested, dict), 'RIG_LIMITS',
            'Caller limits must be an object.')
    require(not requested or set(requested) <= set(DEFAULT_LIMITS), 'RIG_LIMITS',
            'Unknown caller resource limit.')
    require(isinstance(declared, dict) and set(declared) == set(DEFAULT_LIMITS),
            'RIG_LIMITS', 'Declared resource limits must be a complete object.')
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
    """Conservative aggregate estimate; charge before materializing each input.

    Named reservations live for the entire ledger lifetime, including after a
    commit; there is deliberately no release operation. Keys identify allocation
    instances, not paths or content hashes: reuse a key only for the same live
    allocation, and use a fresh key for every retained copy or subsequent load.
    Discovery that discards its objects runs in an uncommitted fork. A successful
    retaining operation commits its fork only after all its allocations succeed.
    Callers serialize access and must reserve before the allocation, including
    any separately retained snapshot, parsed document, image, output, or report.
    The ledger does not itself capture, pin, or authenticate source bytes.
    """
    def __init__(self, limits: dict):
        self.limits = limits
        self.memory = 0
        self.files = {}
        self.input_bytes = 0
        self.json_bytes = 0
        self.array_bytes = 0
        self.texture_rgba_bytes = 0
        self.animation_keyframes = 0
        self.animation_tracks = 0
        self._reservations = {}
        self.report_bytes = 0
        self._transaction = None
        self._generation = 0
        self.add(_IO_RESERVE, 'streaming/header scratch reserve')

    def _state(self):
        return {key: value for key, value in self.__dict__.items()
                if key not in ('_transaction', '_generation')}

    def _check_totals(self):
        self.add(0, 'existing reservations')
        require(self.input_bytes <= self.limits['dependency_bytes'], 'RIG_LIMIT_BINARY',
                'Aggregate native input files exceed the dependency byte budget.',
                expected=self.limits['dependency_bytes'], observed=self.input_bytes)
        require(self.animation_keyframes <= self.limits['keyframes'], 'RIG_LIMIT_KEYFRAMES',
                'Aggregate animation keys exceed the load budget.',
                expected=self.limits['keyframes'], observed=self.animation_keyframes)

    def fork(self, limits: dict):
        """Create a speculative native-load ledger; the original is untouched.

        File identity is local to one native load: loading the same path again
        retains another set of objects and must be charged again.
        """
        validated = effective_limits(DEFAULT_LIMITS, self.limits)
        effective = effective_limits(validated, limits)
        snapshot = copy.deepcopy(self._state())
        child = object.__new__(LoadBudget)
        child.__dict__.update(copy.deepcopy(snapshot))
        child._transaction = (self, self._generation, snapshot)
        child._generation = 0
        child.limits = effective
        child.files = {}
        child._check_totals()
        return child

    def commit(self, child):
        """Publish a successful speculative ledger, retaining caller limits."""
        require(type(child) is LoadBudget, 'RIG_LIMITS', 'Invalid speculative budget.')
        transaction = child._transaction
        require(transaction is not None and transaction[0] is self
                and transaction[1] == self._generation and transaction[2] == self._state(),
                'RIG_LIMITS', 'Speculative budget is unrelated, stale, or already committed.')
        effective = effective_limits(self.limits, child.limits)
        require(effective == child.limits, 'RIG_LIMITS', 'Speculative limits were loosened.')
        child._check_totals()
        state = copy.deepcopy(child._state())
        state['limits'] = self.limits
        self.__dict__.update(state)
        self._generation += 1
        child._transaction = None

    def add(self, amount: int, asset: str) -> None:
        require(type(amount) is int and amount >= 0, 'RIG_LIMITS', 'Invalid memory reservation.')
        total = self.memory + amount
        require(total <= self.limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
                'Aggregate native load estimate exceeds the caller/manifest budget.',
                asset=asset, expected=self.limits['memory_bytes'], observed=total)
        self.memory = total

    def reserve(self, key, amount: int, asset: str) -> bool:
        """Reserve once for a named allocation; conflicting reuse fails closed.

        Keys are caller-owned stable identities, scoped to this load. Return False
        on an identical repeat so counters can also avoid double charging.
        """
        return self._reserve(key, amount, asset, ('generic', amount, asset))

    def captured_input(self, *, key: str, size: int, source: bool = False) -> bool:
        """Admit one retained immutable input copy before reading its bytes.

        Use a distinct key per retained copy; source selects the entry-file cap.
        Size comes from a verified open handle, not an untrusted path re-open.
        """
        require(type(size) is int and size >= 0 and type(source) is bool,
                'RIG_LIMITS', 'Invalid captured input dimensions.')
        require(type(key) is str and 0 < len(key) <= _RESERVATION_STRING_MAX,
                'RIG_LIMITS', 'Reservation strings must be bounded.')
        cap = self.limits['source_bytes'] if source else self.limits['dependency_bytes']
        require(size <= cap, 'RIG_LIMIT_BINARY', 'Input exceeds its byte budget.',
                expected=cap, observed=size)
        identity = ('captured_input', size, source)
        if key in self._reservations:
            return self._reserve(key, size, 'captured input', identity)
        require(self.input_bytes + size <= self.limits['dependency_bytes'], 'RIG_LIMIT_BINARY',
                'Aggregate native input files exceed the dependency byte budget.',
                expected=self.limits['dependency_bytes'], observed=self.input_bytes + size)
        added = self._reserve(key, size, 'captured input', identity)
        if added:
            self.input_bytes += size
        return added

    def parsed_json(self, *, key: str, size: int) -> bool:
        """Admit a parsed JSON object before decoding already-accounted bytes."""
        require(type(size) is int and 0 <= size <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON',
                'Invalid JSON byte count.')
        added = self._reserve(key, size * _JSON_FACTOR, 'parsed JSON object',
                              ('parsed_json', size))
        if added:
            self.json_bytes += size
        return added

    def _reserve(self, key, amount, asset, identity):
        require(type(key) is str and 0 < len(key) <= _RESERVATION_STRING_MAX
                and type(asset) is str and len(asset) <= _RESERVATION_STRING_MAX,
                'RIG_LIMITS', 'Reservation strings must be bounded.')
        require(type(amount) is int and 0 <= amount < 2**64, 'RIG_LIMITS',
                'Invalid memory reservation.')
        if key in self._reservations:
            require(self._reservations[key] == identity, 'RIG_LIMITS',
                    'Conflicting memory reservation.')
            return False
        require(len(self._reservations) < _RESERVATION_COUNT_MAX, 'RIG_LIMITS',
                'Too many memory reservations.')
        self.add(amount + _RESERVATION_OVERHEAD, asset)
        self._reservations[key] = identity
        return True

    def image(self, *, key: str, encoded_bytes: int, pixels: int) -> None:
        """Reserve retained encoded bytes and decoded-image working memory."""
        require(type(encoded_bytes) is int and encoded_bytes >= 0 and
                type(pixels) is int and pixels > 0, 'RIG_LIMITS', 'Invalid image dimensions.')
        if self._reserve(key, encoded_bytes + pixels * 16, 'retained image and decoding workspace',
                         ('image', encoded_bytes, pixels)):
            self.texture_rgba_bytes += pixels * 4

    def animation_report(self, *, key: str, keys: int, tracks: int) -> None:
        """Reserve exact-time report objects before constructing any of them.

        keys is the sum of retained key counts over *all* destination tracks,
        including repeated samplers. This is additional to animation admission.
        """
        require(type(keys) is int and keys >= 0 and type(tracks) is int and tracks >= 0,
                'RIG_LIMITS', 'Invalid animation report dimensions.')
        amount = keys * _REPORT_KEY_BYTES + tracks * 1024
        if self._reserve(key, amount, 'animation exact-time report expansion',
                         ('animation_report', keys, tracks)):
            self.report_bytes += amount

    def animation(self, *, keys: int, width: int, destinations: int, cubic: bool = False,
                  key: str | None = None) -> None:
        """Admit one channel's expanded tracks before decoding or list creation.

        Counts and widths describe each destination, including shared samplers;
        repeated calls share the same cumulative memory and keyframe ceilings.
        """
        require(all(type(v) is int and v > 0 for v in (keys, width, destinations))
                and type(cubic) is bool, 'RIG_ANIMATION_VALUES',
                'Animation admission requires positive integer dimensions.')
        require(key is None or type(key) is str and 0 < len(key) <= _RESERVATION_STRING_MAX,
                'RIG_LIMITS', 'Reservation strings must be bounded.')
        expanded = keys * destinations
        identity = ('animation', keys, width, destinations, cubic)
        if key is not None and key in self._reservations:
            self._reserve(key, 0, 'expanded animation', identity)
            return
        total = self.animation_keyframes + expanded
        require(total <= self.limits['keyframes'], 'RIG_LIMIT_KEYFRAMES',
                'Aggregate animation keys exceed the load budget.',
                expected=self.limits['keyframes'], observed=total)
        # Python floats, list slots and nested lists, plus track/dict overhead.
        # Cubic retains three independently materialized value/tangent lists.
        groups = 3 if cubic else 1
        amount = destinations * (1024 + keys * (32 + groups * (64 + 32 * width)))
        if key is None:
            self.add(amount, 'expanded animation tracks, times, values and tangents')
        else:
            self._reserve(key, amount, 'expanded animation tracks, times, values and tangents', identity)
        self.animation_keyframes = total
        self.animation_tracks += destinations

    def native_animation(self, *, keys: int, tracks: int) -> None:
        """Count native clip objects already covered by JSON memory admission."""
        require(type(keys) is int and keys >= 0 and type(tracks) is int and tracks >= 0,
                'RIG_ANIMATION_VALUES', 'Invalid native animation dimensions.')
        total = self.animation_keyframes + keys
        require(total <= self.limits['keyframes'], 'RIG_LIMIT_KEYFRAMES',
                'Aggregate animation keys exceed the load budget.',
                expected=self.limits['keyframes'], observed=total)
        self.animation_keyframes = total
        self.animation_tracks += tracks

    def file(self, path: Path, *, source=False) -> int:
        with open_payload(path.parent, path.name) as (_, info):
            size = info.st_size
        return self._record_file_size(path, size, source=source)

    def _record_file_size(self, path: Path, size: int, *, source=False) -> int:
        """Account an already captured size without reopening its mutable path."""
        require(type(size) is int and size >= 0, 'RIG_LIMIT_BINARY',
                'Captured input size must be a nonnegative integer.', asset=path.name)
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

    def charge_json(self, path: Path, *, captured_size=None):
        size = (self.file(path) if captured_size is None
                else self._record_file_size(path, captured_size))
        require(size <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON', 'JSON exceeds 32 MiB.', asset=path.name)
        self.add(size * _JSON_FACTOR, path.name + ': JSON object estimate')
        self.json_bytes += size
        return size

    def charge_immutable_json(self, payload: bytes):
        """Account for a captured immutable JSON document without reopening it."""
        require(type(payload) is bytes, 'RIG_JSON', 'Expected immutable JSON bytes.')
        size = len(payload)
        require(size <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON', 'JSON exceeds 32 MiB.')
        require(self.input_bytes + size <= self.limits['dependency_bytes'], 'RIG_LIMIT_BINARY',
                'Aggregate native input files exceed the dependency byte budget.')
        self.add(size * _JSON_FACTOR, 'immutable JSON object estimate')
        self.input_bytes += size
        self.json_bytes += size

    def immutable_json(self, payload: bytes):
        self.charge_immutable_json(payload)
        return decode_json(payload)

    def capture_json(self, path: Path, *, expected_hash=None, hash_code='RIG_HASH'):
        size = self.charge_json(path)
        # Bounded read also rejects a file growing between stat and read.
        with open_payload(path.parent, path.name) as (stream, _):
            payload = stream.read(size + 1)
        require(len(payload) == size, 'RIG_SOURCE_CHANGED', 'JSON changed during admission.', asset=path.name)
        require(expected_hash is None or hashlib.sha256(payload).hexdigest() == expected_hash,
                hash_code, 'Native JSON bytes differ from their immutable reference.', asset=path.name)
        return payload

    def json(self, path: Path, *, expected_hash=None, hash_code='RIG_HASH'):
        return decode_json(self.capture_json(path, expected_hash=expected_hash, hash_code=hash_code))

    def report(self):
        return dict(method='native-load-conservative-v1', estimated_memory_bytes=self.memory,
                    input_bytes=self.input_bytes, array_bytes=self.array_bytes,
                    texture_rgba_bytes=self.texture_rgba_bytes, json_bytes=self.json_bytes,
                    animation_keyframes=self.animation_keyframes, animation_tracks=self.animation_tracks,
                    report_bytes=self.report_bytes,
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
    # Canonical directory equality above rejects offset reconciliation for
    # prepended/concatenated ZIPs; our scan and zipfile must inspect the same bytes.
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


def image_dimensions(payload: bytes) -> tuple[int, int]:
    """Probe only bounded PNG/JPEG framing, never invoke an image decoder.

    PNG IHDR: W3C PNG section 11.2.2. JPEG frame header: ITU-T T.81 B.2.2.
    This is resource admission, not full image/raster integrity validation.
    """
    if payload.startswith(b'\x89PNG\r\n\x1a\n'):
        require(len(payload) >= 33 and payload[8:16] == b'\x00\x00\x00\x0dIHDR',
                'RIG_TEXTURE_FORMAT', 'Expected a complete first PNG IHDR chunk.')
        require((binascii.crc32(payload[12:29]) & 0xffffffff) == struct.unpack_from('>L', payload, 29)[0],
                'RIG_TEXTURE_FORMAT', 'PNG IHDR checksum differs.')
        width, height, depth, color, compression, filtering, interlace = struct.unpack_from('>LL5B', payload, 16)
        depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
        require(0 < width <= 0x7fffffff and 0 < height <= 0x7fffffff and depth in depths.get(color, ())
                and compression == filtering == 0 and interlace in (0, 1),
                'RIG_TEXTURE_FORMAT', 'Unsupported or invalid PNG IHDR.')
        return width, height
    require(payload.startswith(b'\xff\xd8'), 'RIG_TEXTURE_FORMAT', 'Expected PNG/JPEG native image bytes.')
    cursor = 2
    frame_markers = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
    while cursor < len(payload):
        require(payload[cursor] == 0xff, 'RIG_TEXTURE_FORMAT', 'Invalid JPEG header marker.')
        while cursor < len(payload) and payload[cursor] == 0xff:
            cursor += 1
        require(cursor < len(payload), 'RIG_TEXTURE_FORMAT', 'Incomplete JPEG marker.')
        marker = payload[cursor]
        cursor += 1
        require(marker not in (0, 0xd8, 0xd9, 0xda), 'RIG_TEXTURE_FORMAT', 'JPEG has no bounded frame header.')
        if marker == 0x01:  # TEM is the only standalone pre-frame marker.
            continue
        require(cursor + 2 <= len(payload), 'RIG_TEXTURE_FORMAT', 'Incomplete JPEG segment length.')
        length = struct.unpack_from('>H', payload, cursor)[0]
        require(length >= 2 and cursor + length <= len(payload), 'RIG_TEXTURE_FORMAT', 'Invalid JPEG segment range.')
        if marker in frame_markers:
            require(length >= 8, 'RIG_TEXTURE_FORMAT', 'Incomplete JPEG frame header.')
            precision, height, width, components = struct.unpack_from('>BHHB', payload, cursor + 2)
            require(precision in (8, 12, 16) and width > 0 and height > 0 and components in (1, 3, 4)
                    and length == 8 + 3 * components, 'RIG_TEXTURE_FORMAT', 'Unsupported JPEG frame dimensions.')
            return width, height
        cursor += length
    raise RigError('RIG_TEXTURE_FORMAT', 'Missing JPEG frame dimensions.')


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


class _HeaderReader:
    """Bound NumPy header reads before its parser consumes declared lengths."""
    def __init__(self, stream):
        self.stream = stream
        self.total = 0

    def read(self, size):
        require(type(size) is int and 0 <= size <= 10000 and self.total + size <= 10032,
                'RIG_BINARY', 'Native NumPy header exceeds its bounded profile.')
        data = self.stream.read(size)
        self.total += len(data)
        return data

    def tell(self):
        return self.stream.tell()



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
                require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        and not info.flag_bits & 1, 'RIG_BINARY',
                        'Native bundles require unencrypted stored/deflate members.')
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
                with archive.open(info) as raw:
                    stream = _HeaderReader(raw)
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
                width, height = image_dimensions(payload)
                pixels = width * height * 4
                budget.texture_rgba_bytes += pixels
                budget.add(pixels * 4, name + ': decoded texture working set')
            return metadata, headers, snapshot
    except RigError:
        raise
    except (OSError, ValueError, EOFError, KeyError, TypeError, zipfile.BadZipFile, OverflowError, RuntimeError, struct.error) as exc:
        raise RigError('RIG_BINARY', 'Invalid native binary data: ' + str(exc), asset=path.name) from exc
