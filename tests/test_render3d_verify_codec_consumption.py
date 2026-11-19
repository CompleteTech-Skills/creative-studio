"""Tiny native writer fixtures and bounded pre-decoder consumption controls.

Refusal cases add at most a few bytes to a valid stream or its transformed
samples. They must be rejected before the native decoder is constructed.
Successful compatibility controls execute the real installed decoder.
"""
import io
import struct
import tempfile
from pathlib import Path
import tracemalloc
import unittest
from unittest import mock
import zlib

from test_render3d_verify import DEPENDENCIES, Render3DError, output, verify_image, decode_image
from exr_consumption import _consume_dwa_ac
from render3d_verify import _exr_envelope

if DEPENDENCIES:
    import numpy as np
    import OpenEXR


def chunk(data):
    stream = io.BytesIO(data)
    stream.seek(8)
    def name():
        result = bytearray()
        while True:
            value = stream.read(1)
            if not value or value == b'\0':
                return bytes(result)
            result.extend(value)
    while name():
        name()
        stream.seek(struct.unpack('<I', stream.read(4))[0], 1)
    offset = struct.unpack('<Q', stream.read(8))[0]
    size = struct.unpack_from('<I', data, offset + 4)[0]
    assert offset + 8 + size == len(data), 'The fixture has exactly one chunk.'
    return offset, data[offset + 8:]


def replace_chunk(data, payload):
    offset, _ = chunk(data)
    return data[:offset + 4] + struct.pack('<I', len(payload)) + payload


def dwa_parts(payload):
    header = list(struct.unpack_from('<11Q', payload))
    offset = 88 + (struct.unpack_from('<H', payload, 88)[0] if header[0] == 2 else 0)
    prefix = payload[88:offset]
    parts = []
    for index in (2, 3, 4, 5):
        parts.append(payload[offset:offset + header[index]])
        offset += header[index]
    assert offset == len(payload)
    return header, prefix, parts


@unittest.skipUnless(DEPENDENCIES, 'optional OpenEXR/pypng decoder packages unavailable')
class CodecConsumption(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'synthetic.exr'
        self.resolution = [16, 8]

    def original(self, codec, depth=16, scalar=False):
        kind = np.float16 if depth == 16 else np.float32
        values = {name: np.full((8, 16), value, dtype=kind)
                  for name, value in (('R', .25), ('G', .5), ('B', .75), ('A', 1.))}
        if scalar:
            values = {'Z': values['R']}
        with OpenEXR.File({'compression': getattr(OpenEXR, codec + '_COMPRESSION')}, values) as writer:
            writer.write(str(self.path))
        data = self.path.read_bytes()
        _, payload = chunk(data)
        self.assertLess(len(payload) + 1, 16 * 8 * len(values) * (depth // 8))
        return data

    def refuse_before_native(self, data, depth=16):
        self.path.write_bytes(data)
        for decode in (verify_image, decode_image):
            with self.subTest(api=decode.__name__), mock.patch.object(OpenEXR, 'InputFile') as native:
                with self.assertRaises(Render3DError) as refused:
                    decode(self.path, output=output('exr', depth), resolution=self.resolution)
                self.assertEqual(refused.exception.report['category'], 'corrupt-output')
                native.assert_not_called()

    def test_one_padding_byte_in_declared_codec_payload_is_refused(self):
        for codec in ('PIZ', 'B44', 'B44A', 'DWAA', 'DWAB'):
            with self.subTest(codec=codec):
                data = self.original(codec)
                _, payload = chunk(data)
                self.refuse_before_native(replace_chunk(data, payload + b'\0'))

    def test_piz_huffman_segment_must_consume_its_declared_bytes(self):
        data = self.original('PIZ')
        _, payload = chunk(data)
        lower, upper = struct.unpack_from('<HH', payload)
        length_offset = 4 + (upper - lower + 1 if lower <= upper else 0)
        length = struct.unpack_from('<I', payload, length_offset)[0]
        expanded = (payload[:length_offset] + struct.pack('<I', length + 1)
                    + payload[length_offset + 4:] + b'\0')
        self.refuse_before_native(replace_chunk(data, expanded))

    def test_piz_advisory_huffman_table_length_remains_compatible(self):
        data = self.original('PIZ')
        _, payload = chunk(data)
        lower, upper = struct.unpack_from('<HH', payload)
        start = 8 + (upper - lower + 1 if lower <= upper else 0)
        revised = bytearray(payload)
        struct.pack_into('<I', revised, start + 8, 12345)
        self.path.write_bytes(replace_chunk(data, revised))
        for decode in (verify_image, decode_image):
            decode(self.path, output=output('exr', 16), resolution=self.resolution)

    def test_pxr24_transformed_sample_tail_is_refused(self):
        for depth in (16, 32):
            with self.subTest(depth=depth):
                data = self.original('PXR24', depth)
                _, payload = chunk(data)
                expanded = zlib.compress(zlib.decompress(payload) + b'\0')
                self.refuse_before_native(replace_chunk(data, expanded), depth)

    def test_dwa_unknown_component_requires_exact_deflate_input(self):
        data = self.original('DWAA', scalar=True)
        _, payload = chunk(data)
        header, prefix, parts = dwa_parts(payload)
        parts[0] += b'\0'
        header[2] += 1
        self.refuse_before_native(replace_chunk(data, struct.pack('<11Q', *header) + prefix + b''.join(parts)))

    def test_explicit_dwa_zero_runs_fit_the_ac_slots(self):
        _consume_dwa_ac(iter((0xff3f, 0xff00)), 2)
        _consume_dwa_ac(iter((1, 0xff3e)), 1)
        for words in ((0xff40,), (1, 0xff3f)):
            with self.subTest(words=words), self.assertRaises(Render3DError) as refused:
                _consume_dwa_ac(iter(words), 1)
            self.assertEqual(refused.exception.report['category'], 'corrupt-output')

    def test_narrow_exr_preflight_streams_scanlines_in_both_orders(self):
        height, budget = 1024, 8192
        values = np.arange(height, dtype=np.float32).reshape(height, 1)
        for order in (OpenEXR.INCREASING_Y, OpenEXR.DECREASING_Y):
            with self.subTest(order=order):
                with OpenEXR.File({'compression': OpenEXR.NO_COMPRESSION, 'lineOrder': order},
                                  {'Z': values}) as writer:
                    writer.write(str(self.path))
                stream = io.BytesIO(self.path.read_bytes())
                tracemalloc.start()
                try:
                    baseline, _ = tracemalloc.get_traced_memory()
                    _exr_envelope(stream, [1, height], budget, 32, pixels=True)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLessEqual(peak - baseline, budget)
                _, decoded = decode_image(self.path, output=output('exr', 32, passes=['depth']), resolution=[1, height],
                                          pass_name='depth', memory_bytes=budget)
                np.testing.assert_array_equal(decoded['Z'], values)

    def test_dwa_decoded_ac_words_are_consumed_by_all_native_blocks(self):
        for codec in ('DWAA', 'DWAB'):
            with self.subTest(codec=codec):
                data = self.original(codec)
                _, payload = chunk(data)
                header, prefix, parts = dwa_parts(payload)
                # A constant 16x8 RGB fixture has two DCT blocks per channel.
                # Each block's 63 AC coefficients are zero: one end marker.
                blocks = 2 * 3
                self.assertEqual(header[9], blocks)
                header[8], header[10] = blocks, 1
                parts[1] = zlib.compress(struct.pack('<H', 0xff00) * blocks)
                header[3] = len(parts[1])
                compatible = struct.pack('<11Q', *header) + prefix + b''.join(parts)
                self.path.write_bytes(replace_chunk(data, compatible))
                for decode in (verify_image, decode_image):
                    decode(self.path, output=output('exr', 16), resolution=self.resolution)
                # One additional transformed word is bounded and still lies
                # inside the declared component, but belongs to no DCT block.
                header[8] += 1
                parts[1] = zlib.compress(struct.pack('<H', 0xff00) * (blocks + 1))
                header[3] = len(parts[1])
                expanded = struct.pack('<11Q', *header) + prefix + b''.join(parts)
                self.refuse_before_native(replace_chunk(data, expanded))


if __name__ == '__main__':
    unittest.main()
