"""Genuine textured source fixtures must decode, not merely have valid headers."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
import zlib

import numpy as np
from PIL import Image, ImageFile, PngImagePlugin

from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from rig_gltf import export_geometry
from rig_import import _decode_texture, _verify_png_raster, load_character_model
from rig_jobs import create_run
from rig_models import TextureImage
from test_rig_skinning import test_geometry


ROOT = Path(__file__).resolve().parents[1]


def image_bytes(mode='RGB', fmt='PNG', size=(16, 16), *, progressive=False):
    rng = np.random.default_rng(7)
    channels = {'L': 1, 'RGB': 3, 'RGBA': 4, 'CMYK': 4}[mode]
    shape = (*size[::-1], channels) if channels > 1 else size[::-1]
    image = Image.fromarray(rng.integers(0, 256, shape, dtype=np.uint8), mode=mode)
    stream = io.BytesIO();image.save(stream, format=fmt, **({'progressive': progressive} if fmt == 'JPEG' else {}))
    return stream.getvalue()


def png_chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png_with_bad_pixels():
    # Valid container/chunk CRCs, but no valid zlib scanlines. verify() accepts it.
    return (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
            + png_chunk(b'IDAT', b'not a zlib stream') + png_chunk(b'IEND', b''))


def png_raster(width, height, compressed, *, depth=8, color=2, interlace=0, palette=b'', fragments=0):
    header = b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, interlace))
    if palette: header += png_chunk(b'PLTE', palette)
    parts = [compressed] if not fragments else [compressed[i:i + fragments] for i in range(0, len(compressed), fragments)]
    return header + b''.join(png_chunk(b'IDAT', p) for p in parts) + png_chunk(b'IEND', b'')


class TextureIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def model(self, data, suffix='.glb', *, mime='image/png', external=False, data_uri=False, images=1):
        geometry = test_geometry()
        geometry.meshes[0].attributes['TEXCOORD_0'] = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], float)
        geometry.images = [TextureImage('synthetic texture', mime, data) for _ in range(images)]
        geometry.textures = [dict(source=0)]
        geometry.materials[0]['pbrMetallicRoughness'] = dict(baseColorTexture=dict(index=0))
        path = self.root / ('synthetic-' + str(len(list(self.root.iterdir()))) + suffix)
        export_geometry(geometry, path, provenance=dict(fixture=True))
        if external or data_uri:
            value = json.loads(path.read_bytes())
            uri = 'texture-' + path.stem + ('.jpg' if mime == 'image/jpeg' else '.png')
            if external: (self.root / uri).write_bytes(data)
            else: uri = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
            value['images'] = [dict(uri=uri, mimeType=mime)]
            path.write_text(json.dumps(value), 'utf-8')
        return path

    def obj(self, data, extension='.png', *, images=1):
        parent = self.root / ('obj-' + str(len(list(self.root.iterdir()))));parent.mkdir()
        lines = []
        for i in range(images):
            (parent / (str(i) + extension)).write_bytes(data)
            lines += [f'newmtl material{i}', f'map_Kd {i}{extension}']
        (parent / 'texture.mtl').write_text('\n'.join(lines) + '\n')
        path = parent / 'mesh.obj'
        path.write_text('mtllib texture.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n'
                        'vt 0 0\nvt 1 0\nvt 0 1\nusemtl material0\nf 1/1 2/2 3/3\n')
        return path

    def assert_rejected(self, path, code, **kwargs):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(RigError) as caught: load_character_model(path, **kwargs)
        self.assertEqual(caught.exception.report['code'], code)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()})

    def test_actual_png_jpeg_modes_preserve_encoded_bytes_and_native_roundtrip(self):
        for mode, fmt in [('L', 'PNG'), ('RGB', 'PNG'), ('RGBA', 'PNG'), ('L', 'JPEG'), ('RGB', 'JPEG'), ('CMYK', 'JPEG')]:
            for suffix in ('.glb', '.gltf'):
                with self.subTest(mode=mode, fmt=fmt, suffix=suffix):
                    data = image_bytes(mode, fmt);mime = 'image/png' if fmt == 'PNG' else 'image/jpeg'
                    path = self.model(data, suffix, mime=mime)
                    before = path.read_bytes();imported = load_character_model(path)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(imported.geometry.images[0].data, data)
                    self.assertEqual(imported.geometry.images[0].mime_type, mime)
                    output = self.root / ('again-' + path.stem + suffix)
                    export_geometry(imported.geometry, output, provenance=dict(fixture=True))
                    self.assertEqual(load_character_model(output).geometry.images[0].data, data)

    def test_truncated_jpeg_is_rejected_in_all_gltf_storage_forms(self):
        data = image_bytes(fmt='JPEG');self.assertEqual(data[-2:], b'\xff\xd9')
        damaged = data[:-2]
        Image.open(io.BytesIO(damaged)).verify()
        with self.assertRaises(OSError): Image.open(io.BytesIO(damaged)).load()
        for suffix, external, data_uri in [('.glb', False, False), ('.gltf', False, False), ('.gltf', True, False), ('.gltf', False, True)]:
            with self.subTest(suffix=suffix, external=external, data_uri=data_uri):
                self.assert_rejected(self.model(damaged, suffix, mime='image/jpeg', external=external, data_uri=data_uri), 'RIG_TEXTURE_DECODE')

    def test_entropy_recovery_is_rejected_by_all_real_import_routes(self):
        data = image_bytes(fmt='JPEG', size=(32, 32))
        self.assertEqual(hashlib.sha256(data).hexdigest(), 'f7aa64cf9f5f369a45ee48c602577ff4f7d7da1bdb1fc674ea907dae6cd284ec')
        for cut in (5, 20, 100, 300):
            damaged = data[:-cut] + b'\xff\xd9'
            # This is the actual old failure: strict Pillow still returns pixels.
            with Image.open(io.BytesIO(damaged)) as image: image.load()
            for path in [self.model(damaged, mime='image/jpeg'), self.model(damaged, '.gltf', mime='image/jpeg'),
                         self.model(damaged, '.gltf', mime='image/jpeg', external=True),
                         self.model(damaged, '.gltf', mime='image/jpeg', data_uri=True), self.obj(damaged, '.jpg')]:
                with self.subTest(cut=cut, source=path.name): self.assert_rejected(path, 'RIG_TEXTURE_DECODE')
        for path in [self.model(damaged, mime='image/jpeg'), self.model(damaged, '.gltf', mime='image/jpeg', external=True), self.obj(damaged, '.jpg')]:
            destination = path.parent / (path.stem + '-strict-rejected')
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.iterdir() if p.is_file()}
            response = subprocess.run([sys.executable, str(ROOT/'scripts/rig.py'), 'import', '--model', str(path),
                                       '--run-dir', str(destination)], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(response.returncode, 0)
            self.assertEqual(json.loads(response.stdout)['error']['code'], 'RIG_TEXTURE_DECODE')
            self.assertFalse(destination.exists())
            after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.iterdir() if p.is_file()}
            after.pop('.' + destination.name + '.rig.lock', None)
            self.assertEqual(before, after)

    def test_valid_progressive_arithmetic_and_native_byte_identity(self):
        samples = [image_bytes(mode, 'JPEG', size=(19, 17), progressive=True) for mode in ('L', 'RGB', 'CMYK')]
        samples += [(ROOT/'tests/fixtures'/name).read_bytes() for name in (
            'strict-jpeg-arithmetic.jpg', 'strict-jpeg-arithmetic-progressive.jpg')]
        for data in samples:
            for path in [self.model(data, mime='image/jpeg'), self.model(data, '.gltf', mime='image/jpeg', external=True), self.obj(data, '.jpg')]:
                with self.subTest(source=path.name, digest=hashlib.sha256(data).hexdigest()):
                    self.assertEqual(load_character_model(path).geometry.images[0].data, data)

    def test_missing_strict_jpeg_verifier_never_falls_back_or_publishes(self):
        path = self.model(image_bytes(fmt='JPEG'), mime='image/jpeg')
        with patch.dict(sys.modules, {'simplejpeg': None}):
            self.assert_rejected(path, 'RIG_TEXTURE_VERIFIER_UNAVAILABLE')
            png = image_bytes()
            self.assertEqual(_decode_texture(png, DEFAULT_LIMITS), ('image/png', 16 * 16))
        shadow = self.root/'missing-strict';shadow.mkdir()
        (shadow/'simplejpeg.py').write_text('raise ImportError("Simulated absent optional verifier")\n')
        destination = self.root/'unavailable-jpeg-run'
        response = subprocess.run([sys.executable, str(ROOT/'scripts/rig.py'), 'import', '--model', str(path),
                                   '--run-dir', str(destination)], env={**os.environ, 'PYTHONPATH': str(shadow)},
                                  capture_output=True, text=True, timeout=30)
        self.assertNotEqual(response.returncode, 0)
        self.assertEqual(json.loads(response.stdout)['error']['code'], 'RIG_TEXTURE_VERIFIER_UNAVAILABLE')
        self.assertFalse(destination.exists())

    def test_strict_jpeg_aggregate_allowance_is_checked_before_native_pixels(self):
        import simplejpeg
        data = image_bytes(fmt='JPEG', size=(4, 4));path = self.model(data, mime='image/jpeg', images=2)
        with patch.object(simplejpeg, 'decode_jpeg', wraps=simplejpeg.decode_jpeg) as decode:
            self.assert_rejected(path, 'RIG_LIMIT_TEXTURE', limits={**DEFAULT_LIMITS, 'memory_bytes': 800})
            self.assertEqual(decode.call_count, 1)

    def test_png_valid_crc_but_invalid_pixels_and_broken_crc_are_structured(self):
        damaged = png_with_bad_pixels();Image.open(io.BytesIO(damaged)).verify()
        with self.assertRaises(OSError): Image.open(io.BytesIO(damaged)).load()
        crc_bad = bytearray(image_bytes());pos = crc_bad.index(b'IDAT') + 4;crc_bad[pos] ^= 1
        for data in (damaged, bytes(crc_bad)):
            for suffix in ('.glb', '.gltf'):
                self.assert_rejected(self.model(data, suffix), 'RIG_TEXTURE_DECODE')

    def test_png_missing_rows_zlib_footer_or_end_crc_cannot_publish(self):
        raw = b'\0' + b'\xff\0\0' * 2 + b'\0' + b'\0\xff\0' * 2
        compressed = zlib.compress(raw)
        complete = png_raster(2, 2, compressed)
        cases = [png_raster(2, 2, zlib.compress(raw[:7])),
                 png_raster(2, 2, zlib.compress(b'')),
                 png_raster(2, 2, zlib.compress(raw + b'\0' * 7)),
                 png_raster(2, 2, compressed[:-4]),
                 png_raster(2, 2, compressed[:-1] + bytes([compressed[-1] ^ 1])),
                 png_raster(2, 2, compressed + zlib.compress(raw)),
                 complete[:-12], complete[:-4], complete[:-1] + bytes([complete[-1] ^ 1])]
        for index, data in enumerate(cases):
            with self.subTest(index=index):
                for path in [self.model(data), self.model(data, '.gltf'),
                             self.model(data, '.gltf', external=True), self.model(data, '.gltf', data_uri=True), self.obj(data)]:
                    self.assert_rejected(path, 'RIG_TEXTURE_DECODE')
        # The same missing-row source must also fail at the actual CLI boundary.
        path = self.model(cases[0]);destination = self.root / 'missing-row-run'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/rig.py'), 'import', '--model', str(path),
                                 '--run-dir', str(destination)], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'RIG_TEXTURE_DECODE')
        self.assertFalse(destination.exists())

    def test_complete_adam7_packed_and_sixteen_bit_pngs_remain_supported(self):
        values = np.random.default_rng(81).integers(0, 256, (11, 9, 3), dtype=np.uint8)
        raw = bytearray()
        for x, y, dx, dy in [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                              (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]:
            for row in range(y, len(values), dy):
                selected = values[row, x::dx]
                if len(selected): raw.extend(b'\0' + selected.tobytes())
        adam = png_raster(9, 11, zlib.compress(raw), interlace=1, fragments=1)
        with Image.open(io.BytesIO(adam)) as image:
            image.load();np.testing.assert_array_equal(np.asarray(image), values)
        examples = [adam,
                    png_raster(5, 2, zlib.compress(b'\0\xa8\0\x50'), depth=1, color=0),
                    png_raster(3, 2, zlib.compress(b'\0\x18\0\xe4'), depth=2, color=3,
                               palette=bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])),
                    png_raster(2, 2, zlib.compress(b'\0\0\0\xff\xff\0\x12\x34\xab\xcd'), depth=16, color=0)]
        for data in examples:
            with self.subTest(header=data[16:29].hex()):
                self.assertEqual(_decode_texture(data, DEFAULT_LIMITS)[0], 'image/png')
                for suffix in ('.glb', '.gltf'):
                    result = load_character_model(self.model(data, suffix))
                    self.assertEqual(result.geometry.images[0].data, data)

    def test_png_raster_validation_never_materializes_full_inflation(self):
        width = height = 4096
        compressor = zlib.compressobj()
        blocks = [compressor.compress(b'\0' * (width + 1)) for _ in range(height)]
        data = png_raster(width, height, b''.join(blocks) + compressor.flush(), color=0)
        tracemalloc.start()
        try:
            _verify_png_raster(data, (width, height))
            _, peak = tracemalloc.get_traced_memory()
        finally: tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024, f'Expected bounded inflation, observed {peak} bytes')

    def test_obj_uses_same_pixel_integrity_and_aggregate_budget(self):
        for fmt, extension in [('PNG', '.png'), ('JPEG', '.jpg')]:
            data = image_bytes(fmt=fmt)
            path = self.obj(data, extension)
            imported = load_character_model(path)
            self.assertEqual(imported.geometry.images[0].data, data)
            self.assertEqual(set(imported.source_files),
                             {p.resolve() for p in (path, path.parent / 'texture.mtl', path.parent / ('0' + extension))})
        self.assert_rejected(self.obj(image_bytes(fmt='JPEG')[:-2], '.jpg'), 'RIG_TEXTURE_DECODE')
        self.assert_rejected(self.obj(png_with_bad_pixels()), 'RIG_TEXTURE_DECODE')
        data = image_bytes(size=(4, 4))
        self.assert_rejected(self.obj(data, images=2), 'RIG_LIMIT_TEXTURE', limits={**DEFAULT_LIMITS, 'memory_bytes': 800})

    def test_obj_parent_alias_preserves_exact_canonical_source_inventory(self):
        path = self.obj(image_bytes())
        actual_files = (path, path.parent / 'texture.mtl', path.parent / '0.png')
        expected = {p.resolve(): hashlib.sha256(p.read_bytes()).hexdigest() for p in actual_files}
        alias = self.root / 'parent-alias'
        if os.name == 'nt':
            # Junctions need no symbolic-link privilege and exercise the same
            # canonical-parent boundary as Windows short/long temp aliases.
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(path.parent)],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        else:
            alias.symlink_to(path.parent, target_is_directory=True)
        self.assertEqual(alias.resolve(), path.parent.resolve())
        self.assertNotEqual(str(alias), str(path.parent))
        imported = load_character_model(alias / path.name)
        self.assertEqual(set(imported.source_files), set(expected))
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in imported.source_files}, expected)
        self.assertEqual({p.resolve(): hashlib.sha256(p.read_bytes()).hexdigest() for p in actual_files}, expected)

    def test_encoded_and_decoded_limits_precede_pixel_allocation(self):
        data = image_bytes()
        with patch('PIL.Image.open', side_effect=AssertionError('encoded limit must run first')):
            with self.assertRaises(RigError) as caught: _decode_texture(data, {**DEFAULT_LIMITS, 'source_bytes': len(data) - 1})
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_TEXTURE')
        # Header has a real CRC but exceeds the core pixel cap; no raster exists.
        data = b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('>IIBBBBB', 4097, 4096, 8, 2, 0, 0, 0)) + png_chunk(b'IDAT', b'x') + png_chunk(b'IEND', b'')
        with patch.object(PngImagePlugin.PngImageFile, 'load', side_effect=AssertionError('pixel limit must run first')):
            self.assert_rejected(self.model(data), 'RIG_LIMIT_TEXTURE')

    def test_aggregate_gltf_budget_precedes_second_image_load(self):
        data = image_bytes(size=(4, 4));path = self.model(data, images=2)
        calls = []
        original = PngImagePlugin.PngImageFile.load
        def load(image, *args, **kwargs):
            calls.append(image.size)
            return original(image, *args, **kwargs)
        with patch.object(PngImagePlugin.PngImageFile, 'load', load):
            self.assert_rejected(path, 'RIG_LIMIT_TEXTURE', limits={**DEFAULT_LIMITS, 'memory_bytes': 800})
        self.assertEqual(calls, [(4, 4)])

    def test_decoded_memory_exact_boundary_is_inclusive(self):
        data = image_bytes(size=(4, 3))
        self.assertEqual(_decode_texture(data, {**DEFAULT_LIMITS, 'memory_bytes': 12 * 32}), ('image/png', 12))
        with self.assertRaises(RigError) as caught:
            _decode_texture(data, {**DEFAULT_LIMITS, 'memory_bytes': 12 * 32 - 1})
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_TEXTURE')

    def test_pillow_bomb_warning_and_error_are_structured_without_global_mutation(self):
        data = image_bytes(size=(4, 3));path = self.model(data)
        for threshold in (8, 4):
            with patch.object(Image, 'MAX_IMAGE_PIXELS', threshold):
                self.assert_rejected(path, 'RIG_LIMIT_TEXTURE')
                self.assertEqual(Image.MAX_IMAGE_PIXELS, threshold)

    def test_raster_allocation_failure_is_structured_without_source_change(self):
        path = self.model(image_bytes())
        # Fault injection checks the error boundary; successful raster evidence
        # elsewhere uses actual decoders, not a fabricated decode response.
        with patch.object(PngImagePlugin.PngImageFile, 'load', side_effect=MemoryError('allocation denied')):
            self.assert_rejected(path, 'RIG_LIMIT_TEXTURE')

    def test_mime_animation_and_lax_decoder_policy_fail_closed(self):
        self.assert_rejected(self.model(image_bytes(), mime='image/jpeg'), 'RIG_TEXTURE_FORMAT')
        stream = io.BytesIO()
        Image.new('RGBA', (2, 2), 'red').save(stream, format='PNG', save_all=True,
                                            append_images=[Image.new('RGBA', (2, 2), 'blue')], duration=100, loop=0)
        self.assert_rejected(self.model(stream.getvalue()), 'RIG_TEXTURE_FORMAT')
        path = self.model(image_bytes())
        with patch.object(ImageFile, 'LOAD_TRUNCATED_IMAGES', True):
            self.assert_rejected(path, 'RIG_TEXTURE_POLICY')
            self.assertTrue(ImageFile.LOAD_TRUNCATED_IMAGES)

    def test_actual_cli_bad_pixels_never_publish_a_run_or_modify_sources(self):
        path = self.model(image_bytes(fmt='JPEG')[:-2], mime='image/jpeg')
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir() if p.is_file()}
        destination = self.root / 'rejected-run'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/rig.py'), 'import', '--model', str(path),
                                 '--run-dir', str(destination)], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'RIG_TEXTURE_DECODE')
        self.assertFalse(destination.exists())
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()
                                  if p.is_file() and not p.name.endswith('.rig.lock')})

    @unittest.skipUnless(os.environ.get('HATCH_RIG_INTEROP_TEST') == '1', 'Enable the declared independent Khronos gate.')
    def test_actual_valid_png_jpeg_exports_pass_independent_khronos(self):
        from rig_interop import validate_gltf
        for fmt, mime in [('PNG', 'image/png'), ('JPEG', 'image/jpeg')]:
            for suffix in ('.glb', '.gltf'):
                path = self.model(image_bytes(fmt=fmt), suffix, mime=mime)
                result = validate_gltf(path)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['issues']['numErrors'], 0)
                self.assertEqual(result['issues']['numWarnings'], 0)


if __name__ == '__main__': unittest.main()
