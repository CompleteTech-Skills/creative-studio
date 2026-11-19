"""Bounded scanline codec consumption checks before native EXR decoding.

Layouts follow OpenEXR v3.5.4 OpenEXRCore. Native pixel decoding remains
required; a successful native decoder alone does not prove that it used every
declared compressed byte or transformed sample. No declared count allocates
memory until it has been bounded by admitted geometry and the working budget.
"""
from array import array
import struct
import zlib

from render3d_contracts import Render3DError, require


def _check(value):
    require(value, 'corrupt-output', 'EXR compressed payload is not consumed exactly.')


class _Cursor:
    def __init__(self, data):
        self.data, self.position = memoryview(data), 0

    def take(self, count):
        _check(0 <= count <= len(self.data) - self.position)
        start = self.position
        self.position += count
        return self.data[start:self.position]

    def integer(self, code):
        return struct.unpack('<' + code, self.take(struct.calcsize('<' + code)))[0]

    def finish(self):
        _check(self.position == len(self.data))


class _Bits:
    def __init__(self, data, count=None):
        self.data, self.position = memoryview(data), 0
        self.count = len(self.data) * 8 if count is None else count
        _check(0 <= self.count <= len(self.data) * 8)

    def take(self, count):
        _check(count <= self.count - self.position)
        value = 0
        for _ in range(count):
            offset, bit = divmod(self.position, 8)
            value = (value << 1) | ((self.data[offset] >> (7 - bit)) & 1)
            self.position += 1
        return value


def _inflate(data, expected, memory_bytes):
    require(0 <= expected and len(data) + expected + 1 <= memory_bytes,
            'system-memory', 'EXR codec verification exceeds its working budget.')
    try:
        decoder = zlib.decompressobj()
        result = decoder.decompress(data, expected + 1)
    except zlib.error:
        raise Render3DError('corrupt-output', 'Malformed EXR compressed stream.') from None
    _check(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
           and len(result) == expected)
    return result


def _rle(data, expected):
    cursor, produced = _Cursor(data), 0
    while cursor.position < len(data):
        control = cursor.integer('b')
        count = -control if control < 0 else control + 1
        cursor.take(count if control < 0 else 1)
        produced += count
        _check(produced <= expected)
    _check(produced == expected)


def _huffman_frame(data, *, values=False, memory_bytes=0):
    cursor = _Cursor(data)
    low, high, _advisory_table_bytes, bit_count, _future = struct.unpack('<5I', cursor.take(20))
    _check(0 <= low <= high < 65537)
    if values:
        require(len(data) + 512 * 1024 <= memory_bytes, 'system-memory',
                'EXR Huffman verification exceeds its working budget.')
    lengths = bytearray(high - low + 1) if values else None
    bits, symbol = _Bits(cursor.data[20:]), low
    # The advisory table length is ignored by the native format. Walk the
    # actual bounded six-bit code lengths and zero runs to locate data bits.
    while symbol <= high:
        length = bits.take(6)
        count = 1 if length < 59 else length - 57 if length < 63 else bits.take(8) + 6
        _check(count <= high - symbol + 1)
        if values and length < 59:
            lengths[symbol - low] = length
        symbol += count
    start = 20 + (bits.position + 7) // 8
    _check(start + (bit_count + 7) // 8 == len(data))
    return low, high, lengths, _Bits(cursor.data[start:], bit_count)


def _huffman_values(data, expected, memory_bytes):
    low, marker, lengths, bits = _huffman_frame(data, values=True, memory_bytes=memory_bytes)
    symbols = [array('I') for _ in range(59)]
    for offset, length in enumerate(lengths):
        if length:
            symbols[length].append(low + offset)
    first, code = [0] * 59, 0
    for length in range(58, 0, -1):
        first[length] = code
        _check(code + len(symbols[length]) <= 1 << length)
        code = (code + len(symbols[length])) >> 1
    produced, previous = 0, None
    while bits.position < bits.count:
        code, symbol = 0, None
        for length in range(1, 59):
            code = (code << 1) | bits.take(1)
            index = code - first[length]
            if 0 <= index < len(symbols[length]):
                symbol = symbols[length][index]
                break
        _check(symbol is not None)
        if symbol == marker:
            count = bits.take(8)
            _check(previous is not None)
        else:
            previous, count = symbol, 1
        produced += count
        _check(produced <= expected)
        for _ in range(count):
            yield previous
    _check(produced == expected)


def _dwa_schemes(cursor, version, channels):
    if version < 2:
        names = {b'r', b'red', b'g', b'grn', b'green', b'b', b'blu', b'blue', b'y', b'by', b'ry'}
        return [1 if name.rsplit(b'.', 1)[-1].lower() in names else
                2 if name.rsplit(b'.', 1)[-1].lower() == b'a' else 0 for name, _ in channels]
    size = cursor.integer('H')
    _check(size >= 2)
    rules = _Cursor(cursor.take(size - 2))
    schemes = [0] * len(channels)
    while rules.position < len(rules.data):
        suffix = bytearray()
        for _ in range(129):
            value = rules.integer('B')
            if value == 0:
                break
            suffix.append(value)
        else:
            _check(False)
        value, precision = rules.integer('B'), rules.integer('B')
        scheme = (value >> 2) & 3
        _check((value >> 4) <= 3 and scheme < 3 and precision < 3)
        for index, (name, channel_precision) in enumerate(channels):
            actual = name.rsplit(b'.', 1)[-1]
            matches = actual.lower() == suffix.lower() if value & 1 else actual == suffix
            if precision == channel_precision and matches:
                schemes[index] = scheme  # The last matching rule wins.
    return schemes


def _dwa(data, width, height, channels, memory_bytes):
    cursor = _Cursor(data)
    (version, unknown_raw, unknown_size, ac_size, dc_size, rle_size,
     rle_encoded, rle_raw, ac_count, dc_count, strategy) = struct.unpack('<11Q', cursor.take(88))
    _check(version <= 2)
    schemes = _dwa_schemes(cursor, version, channels)
    expected_unknown = sum(width * height * (2 if kind == 1 else 4)
                           for (_, kind), scheme in zip(channels, schemes) if scheme == 0)
    expected_rle = sum(width * height * (2 if kind == 1 else 4)
                       for (_, kind), scheme in zip(channels, schemes) if scheme == 2)
    blocks = ((width + 7) // 8) * ((height + 7) // 8) * schemes.count(1)
    _check(unknown_raw == expected_unknown and rle_raw == expected_rle and dc_count == blocks)
    unknown, ac, dc, rle = [cursor.take(size) for size in (unknown_size, ac_size, dc_size, rle_size)]
    cursor.finish()
    # Component views borrow the whole chunk. Charge that retained chunk, not
    # just the selected view, when bounding additional decoding workspace.
    available = memory_bytes - len(data)
    if expected_unknown:
        _inflate(unknown, expected_unknown, available + len(unknown))
    else:
        _check(not unknown_size)
    if expected_rle:
        _check(0 < rle_encoded <= 2 * expected_rle)
        _rle(_inflate(rle, rle_encoded, available + len(rle)), expected_rle)
    else:
        _check(not (rle_size or rle_encoded))
    if not blocks:
        _check(not (ac_size or dc_size or ac_count))
        return
    _check(blocks <= ac_count <= 63 * blocks and ac_size > 0 and strategy in (0, 1))
    _inflate(dc, 2 * blocks, available + len(dc))
    if strategy == 0:
        words = _huffman_values(ac, ac_count, available + len(ac))
    else:
        words = (word for (word,) in struct.iter_unpack('<H', _inflate(ac, 2 * ac_count, available + len(ac))))
    # DCT channels use one DC and at most 63 run-coded AC words per 8x8
    # block. Check all decoded AC words are consumed, including clipped edges.
    missing = object()
    for _ in range(blocks):
        coefficient = 1
        while coefficient < 64:
            word = next(words, missing)
            _check(word is not missing)
            coefficient += ((word & 255) or 64) if word & 0xff00 == 0xff00 else 1
    _check(next(words, missing) is missing)


def verify_chunk(data, compression, width, height, channels, memory_bytes):
    """Validate one bounded chunk from already admitted unsampled channels."""
    raw = sum(width * height * (2 if kind == 1 else 4) for _, kind in channels)
    require(len(data) <= memory_bytes, 'system-memory', 'EXR compressed chunk exceeds its working budget.')
    if len(data) == raw:
        return  # Every legacy codec uses this full-size raw fallback.
    _check(len(data) > 0 and compression != 0)
    if compression == 1:
        _rle(data, raw)
    elif compression in (2, 3):
        _inflate(data, raw, memory_bytes)
    elif compression == 4:
        cursor = _Cursor(data)
        lower, upper = cursor.integer('H'), cursor.integer('H')
        _check(upper < 8192)
        cursor.take(upper - lower + 1 if lower <= upper else 0)
        compressed = cursor.take(cursor.integer('I'))
        cursor.finish()
        _huffman_frame(compressed)
    elif compression == 5:
        transformed = sum(width * height * (2 if kind == 1 else 3) for _, kind in channels)
        _inflate(data, transformed, memory_bytes)
    elif compression in (6, 7):
        cursor = _Cursor(data)
        for _, kind in channels:
            if kind != 1:
                cursor.take(width * height * 4)
                continue
            for _ in range(((width + 3) // 4) * ((height + 3) // 4)):
                prefix = cursor.take(3)
                if prefix[2] < 52:
                    cursor.take(11)
        cursor.finish()
    elif compression in (8, 9):
        _dwa(data, width, height, channels, memory_bytes)
    else:
        _check(False)
