"""Real native-rig admission, aggregate budgets and immutable-source regressions."""
from copy import deepcopy
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import numpy as np
from PIL import Image, PngImagePlugin

from rig_errors import RigError, sha256, directory_capability
from rig_models import (TextureImage, load_rig, rig_signature, save_geometry,
                        _binary_bundle, _read_bundle, save_weights)
from rig_contracts import DEFAULT_LIMITS
from rig_load_limits import LoadBudget, effective_limits, image_dimensions

ROOT = Path(__file__).resolve().parents[1]


class NativeLoadLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'robot'
        shutil.copytree(ROOT / 'examples/rig-3d/robotic', self.root)

    def hashes(self):
        return {str(path.relative_to(self.root)): sha256(path) for path in self.root.rglob('*') if path.is_file()}

    def assert_before_arrays(self, cap, code='RIG_LIMIT_MEMORY'):
        with patch('rig_models._materialize_array', side_effect=AssertionError('array materialized before rejection')) as materialize:
            with self.assertRaises(RigError) as caught:
                load_rig(self.root, limits=cap)
            self.assertEqual(caught.exception.report['code'], code)
            materialize.assert_not_called()

    def test_real_robot_with_caller_cap_preserves_manifest_signature_and_source(self):
        before = self.hashes()
        manifest = json.loads((self.root / 'rig.json').read_bytes())
        policy = {'memory_bytes': 35 * 1024**2}
        original_policy = deepcopy(policy)
        rig = load_rig(self.root, limits=policy)
        self.assertEqual(rig.geometry.vertex_count, 706)
        self.assertEqual(rig.geometry.triangle_count, 1368)
        self.assertEqual(rig.manifest, manifest)
        self.assertEqual(rig_signature(rig.manifest), rig_signature(manifest))
        self.assertEqual(policy, original_policy)
        self.assertLess(rig.resource_usage['estimated_memory_bytes'], policy['memory_bytes'])
        self.assertEqual(before, self.hashes())
        self.assertTrue(rig.geometry.meshes[0].positions.flags.writeable)

    def test_all_bundles_and_json_admitted_before_first_array(self):
        before = self.hashes()
        full = load_rig(self.root)
        usage = full.resource_usage
        self.assertGreater(len(full.weights), 1)
        self.assertGreater(len(full.clips), 1)
        # Every individual member/bundle fits; their live aggregate plus retained
        # clip/identity objects crosses this exact conservative working-set cap.
        self.assert_before_arrays({'memory_bytes': usage['estimated_memory_bytes'] - 1})
        self.assertEqual(before, self.hashes())
        accepted = load_rig(self.root, limits={'memory_bytes': float(usage['estimated_memory_bytes'])})
        self.assertEqual(accepted.resource_usage, usage)

    def test_48_real_textures_do_not_bypass_35_mib_with_self_declared_2gib(self):
        rig = load_rig(self.root)
        stream = io.BytesIO()
        Image.new('RGB', (512, 512), 'red').save(stream, format='PNG', compress_level=0)
        encoded = stream.getvalue()
        self.assertGreater(len(encoded) * 48, 35 * 1024**2)
        rig.geometry.images = [TextureImage(str(i), 'image/png', encoded) for i in range(48)]
        rig.manifest['geometry'] = save_geometry(self.root, rig.geometry, rig.manifest['limits'])
        (self.root / 'rig.json').write_text(json.dumps(rig.manifest))
        before = self.hashes()
        self.assertEqual(rig.manifest['limits']['memory_bytes'], 2 * 1024**3)
        self.assert_before_arrays({'memory_bytes': 35 * 1024**2})
        self.assertEqual(before, self.hashes())

    def test_decoded_image_pixels_count_even_for_tiny_compressed_png(self):
        rig = load_rig(self.root)
        stream = io.BytesIO()
        Image.new('RGB', (2048, 2048), 'red').save(stream, format='PNG')
        self.assertLess(len(stream.getvalue()), 32 * 1024)
        rig.geometry.images = [TextureImage('large', 'image/png', stream.getvalue())]
        rig.manifest['geometry'] = save_geometry(self.root, rig.geometry, rig.manifest['limits'])
        (self.root / 'rig.json').write_text(json.dumps(rig.manifest))
        self.assert_before_arrays({'memory_bytes': 35 * 1024**2})

    def test_invalid_and_unknown_caller_caps_are_structured_before_arrays(self):
        for value in (True, False, 0, -1, 1.5, float('nan'), float('inf'), 10**1000, '123', None):
            with self.subTest(value=repr(value)[:30]):
                self.assert_before_arrays({'memory_bytes': value}, 'RIG_LIMITS')
        self.assert_before_arrays({'invented_limit': 5}, 'RIG_LIMITS')
        self.assert_before_arrays({'memory_bytes': 1})

    def test_requested_limits_cannot_raise_declared_caps_or_mutate_them(self):
        declared = deepcopy(DEFAULT_LIMITS)
        declared['memory_bytes'] = 35 * 1024**2
        before = deepcopy(declared)
        result = effective_limits(declared, {'memory_bytes': float(DEFAULT_LIMITS['memory_bytes'])})
        self.assertEqual(result['memory_bytes'], declared['memory_bytes'])
        self.assertEqual(declared, before)

    def test_actual_counts_enforced_before_arrays(self):
        for cap, code in (({'vertices': 705}, 'RIG_LIMIT_VERTICES'),
                          ({'triangles': 1367}, 'RIG_LIMIT_TRIANGLES'),
                          ({'bones': 1}, 'RIG_LIMIT_BONES'),
                          ({'clips': 1}, 'RIG_LIMIT_CLIPS'),
                          ({'keyframes': 1}, 'RIG_LIMIT_KEYFRAMES'),
                          ({'duration': 1}, 'RIG_LIMIT_ANIMATION')):
            with self.subTest(cap=cap):
                self.assert_before_arrays(cap, code)

    def test_source_and_aggregate_file_caps_before_arrays(self):
        rig = load_rig(self.root)
        source = self.root / rig.manifest['source']['path']
        self.assert_before_arrays({'source_bytes': source.stat().st_size - 1}, 'RIG_LIMIT_BINARY')
        usage = rig.resource_usage
        self.assert_before_arrays({'dependency_bytes': usage['input_bytes'] - 1}, 'RIG_LIMIT_BINARY')

    def test_forged_npy_shape_does_not_reach_array_allocator(self):
        stream = io.BytesIO()
        np.lib.format.write_array_header_1_0(stream, dict(descr='<f8', fortran_order=False, shape=(1_000_000,)))
        forged = stream.getvalue() + b'12345678'
        path = Path(self.temp.name) / 'forged.npz'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('metadata.json', '{}')
            archive.writestr('forged.npy', forged)
        with patch('rig_models._materialize_array', side_effect=AssertionError('unsafe allocation')) as materialize:
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, dict(DEFAULT_LIMITS))
            self.assertEqual(caught.exception.report['code'], 'RIG_BINARY')
            materialize.assert_not_called()

    def test_pickle_traversal_duplicate_and_bomb_members_fail_before_arrays(self):
        payloads = (
            [('metadata.json', b'{}'), ('../bad.npy', b'unsafe')],
            [('metadata.json', b'{}'), ('metadata.json', b'{}')],
            [('metadata.json', b'{}'), ('large.npy', b'0' * (2 * 1024**2))],
        )
        for i, entries in enumerate(payloads):
            path = Path(self.temp.name) / f'bad{i}.npz'
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, data in entries:
                    archive.writestr(name, data)
            limits = dict(DEFAULT_LIMITS, memory_bytes=5 * 1024**2)
            with patch('rig_models._materialize_array', side_effect=AssertionError('unsafe allocation')) as materialize:
                with self.assertRaises(RigError):
                    _read_bundle(path, limits)
                materialize.assert_not_called()
        obj = Path(self.temp.name) / 'object.npz'
        # The native writer itself refuses pickles; a foreign NPY is rejected too.
        raw = io.BytesIO()
        np.lib.format.write_array(raw, np.array([{'bad': 1}], dtype=object), allow_pickle=True)
        with zipfile.ZipFile(obj, 'w') as archive:
            archive.writestr('metadata.json', '{}')
            archive.writestr('object.npy', raw.getvalue())
        with self.assertRaises(RigError) as caught:
            _read_bundle(obj, DEFAULT_LIMITS)
        self.assertEqual(caught.exception.report['code'], 'RIG_BINARY')

    def test_forged_zip_directory_count_is_rejected_before_zipinfo_allocation(self):
        path = Path(self.temp.name) / 'directory.npz'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('metadata.json', '{}')
            for i in range(30):
                archive.writestr(f'entry{i}.npy', b'')
        payload = bytearray(path.read_bytes())
        offset = payload.rfind(b'PK\x05\x06')
        struct.pack_into('<HH', payload, offset + 8, 1, 1)
        path.write_bytes(payload)
        with patch('rig_load_limits.zipfile.ZipFile', side_effect=AssertionError('entry list allocated')):
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, dict(DEFAULT_LIMITS, archive_files=20))
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_BINARY')

    def test_array_order_endianness_and_writeability_survive_bounded_materialization(self):
        arrays = {'fortran': np.asfortranarray(np.arange(12).reshape(3, 4)),
                  'bigendian': np.asarray([.25, .5, .75], dtype='>f8'),
                  'empty': np.zeros((0, 4))}
        path = Path(self.temp.name) / 'arrays.npz'
        path.write_bytes(_binary_bundle({}, arrays))
        _, loaded = _read_bundle(path, DEFAULT_LIMITS)
        for name, expected in arrays.items():
            np.testing.assert_array_equal(loaded[name], expected)
            self.assertEqual(loaded[name].dtype, expected.dtype)
            self.assertTrue(loaded[name].flags.writeable)

    def test_loading_uses_the_same_immutable_snapshot_as_header_admission(self):
        import rig_load_limits
        path = Path(self.temp.name) / 'changing.npz'
        path.write_bytes(_binary_bundle({}, {'safe': np.arange(9).reshape(3, 3)}))
        expected_hash = sha256(path)
        real = rig_load_limits.scan_bundle
        def changed_after_snapshot(*args, **kwargs):
            admitted = real(*args, **kwargs)
            path.write_bytes(b'new bytes after verified snapshot')
            return admitted
        with patch('rig_load_limits.scan_bundle', side_effect=changed_after_snapshot):
            _, arrays = _read_bundle(path, DEFAULT_LIMITS, expected_hash=expected_hash)
        np.testing.assert_array_equal(arrays['safe'], np.arange(9).reshape(3, 3))
        self.assertNotEqual(sha256(path), expected_hash)

    def test_noncanonical_zip_offsets_rejected_before_directory_construction(self):
        # A harmless metadata-only archive with an extra directory prefix tests
        # the core profile's no-concatenation boundary without allocating entries.
        path = Path(self.temp.name) / 'prefixed.npz'
        canonical = bytearray(_binary_bundle({}, {}))
        end = canonical.rfind(b'PK\x05\x06')
        size, start = struct.unpack_from('<LL', canonical, end + 12)
        prefix = canonical[start:start + size]
        struct.pack_into('<L', canonical, end + 16, 0)
        path.write_bytes(prefix + canonical)
        with patch('rig_load_limits.zipfile.ZipFile', side_effect=AssertionError('directory constructed too early')):
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, DEFAULT_LIMITS)
            self.assertEqual(caught.exception.report['code'], 'RIG_BINARY')

    def test_unsupported_zip_methods_reject_before_member_decoder(self):
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(method=method):
                path = Path(self.temp.name) / f'method{method}.npz'
                with zipfile.ZipFile(path, 'w', compression=method) as archive:
                    archive.writestr('metadata.json', '{}')
                with patch('zipfile.ZipFile.read', side_effect=AssertionError('unsupported decoder invoked')):
                    with self.assertRaises(RigError) as caught:
                        _read_bundle(path, DEFAULT_LIMITS)
                    self.assertEqual(caught.exception.report['code'], 'RIG_BINARY')

    def test_png_metadata_is_not_decoded_for_resource_admission(self):
        rig = load_rig(self.root)
        stream = io.BytesIO()
        text = PngImagePlugin.PngInfo()
        text.add_text('description', 'ordinary bounded fixture text', zip=True)
        Image.new('RGB', (16, 24), 'red').save(stream, format='PNG', pnginfo=text)
        encoded = stream.getvalue()
        rig.geometry.images = [TextureImage('with-text', 'image/png', encoded)]
        rig.manifest['geometry'] = save_geometry(self.root, rig.geometry, rig.manifest['limits'])
        (self.root / 'rig.json').write_text(json.dumps(rig.manifest))
        before = self.hashes()
        with patch('PIL.Image.open', side_effect=AssertionError('image metadata decoder invoked')):
            reopened = load_rig(self.root, limits={'memory_bytes': 35 * 1024**2})
        self.assertEqual(reopened.geometry.images[0].data, encoded)
        self.assertEqual(reopened.resource_usage['texture_rgba_bytes'], 16 * 24 * 4)
        self.assertEqual(self.hashes(), before)

    def test_dimension_probes_cover_real_png_jpeg_modes_without_decoders(self):
        for mode in ('1', 'L', 'P', 'RGB', 'RGBA', 'I;16'):
            stream = io.BytesIO()
            Image.new(mode, (13, 19)).save(stream, format='PNG')
            with patch('PIL.Image.open', side_effect=AssertionError('PNG decoder invoked')):
                self.assertEqual(image_dimensions(stream.getvalue()), (13, 19))
        for mode in ('L', 'RGB', 'CMYK'):
            for progressive in (False, True):
                stream = io.BytesIO()
                Image.new(mode, (17, 23)).save(stream, format='JPEG', progressive=progressive)
                with patch('PIL.Image.open', side_effect=AssertionError('JPEG decoder invoked')):
                    self.assertEqual(image_dimensions(stream.getvalue()), (17, 23))

    def test_image_records_sharing_data_retain_one_immutable_encoded_copy(self):
        rig = load_rig(self.root)
        stream = io.BytesIO()
        Image.new('RGB', (12, 12), 'red').save(stream, format='PNG')
        rig.geometry.images = [TextureImage('first', 'image/png', stream.getvalue())]
        desc = save_geometry(self.root, rig.geometry, rig.manifest['limits'])
        path = self.root / desc['data']['path']
        meta, arrays = _read_bundle(path, DEFAULT_LIMITS)
        meta['images'].append(dict(meta['images'][0], name='second'))
        payload = _binary_bundle(meta, arrays)
        path.write_bytes(payload)
        desc['data']['sha256'] = sha256(path)
        rig.manifest['geometry'] = desc
        (self.root / 'rig.json').write_text(json.dumps(rig.manifest))
        reopened = load_rig(self.root)
        self.assertEqual([i.name for i in reopened.geometry.images], ['first', 'second'])
        self.assertIs(reopened.geometry.images[0].data, reopened.geometry.images[1].data)
        self.assertEqual(reopened.geometry.images[0].data, stream.getvalue())

    def test_mismatched_locked_header_rejects_before_array_and_list_allocation(self):
        rig = load_rig(self.root)
        name = next(iter(rig.weights))
        skin = rig.weights[name]
        skin.locked = np.zeros(8, dtype='u4')
        desc = save_weights(self.root, skin)
        desc['locked_vertices'] = []
        rig.manifest['skins'] = [desc if item['mesh'] == name else item for item in rig.manifest['skins']]
        (self.root / 'rig.json').write_text(json.dumps(rig.manifest))
        self.assert_before_arrays({}, 'RIG_SKIN')

    def test_clip_preflight_cannot_admit_unhashed_shorter_replacement(self):
        import rig_models
        manifest = json.loads((self.root / 'rig.json').read_bytes())
        before = self.hashes()
        original = rig_models._preflight_load
        changed = []
        def shorter_during_preflight(*args, **kwargs):
            for descriptor in manifest['animations']:
                path = self.root / descriptor['path']
                payload = path.read_bytes()
                value = json.loads(payload)
                value['duration'] /= 10
                for track in value['tracks']:
                    track['times'] = [time / 10 for time in track['times']]
                path.write_text(json.dumps(value))
                changed.append((path, payload))
            try:
                return original(*args, **kwargs)
            finally:
                for path, payload in changed:
                    path.write_bytes(payload)
        with patch('rig_models._preflight_load', side_effect=shorter_during_preflight):
            self.assert_before_arrays({'duration': 1}, 'RIG_HASH')
        self.assertTrue(changed)
        self.assertEqual(before, self.hashes())

    def test_live_clip_load_reapplies_all_caller_caps(self):
        # Exercise live admission independently of a cached preflight result.
        # Real source-bound clips and arrays are still read and verified.
        for cap, code in (({'duration': 1}, 'RIG_LIMIT_ANIMATION'),
                          ({'fps': 1}, 'RIG_LIMIT_ANIMATION'),
                          ({'keyframes': 1}, 'RIG_LIMIT_KEYFRAMES')):
            with self.subTest(cap=cap), patch('rig_models._preflight_load', return_value=None):
                with self.assertRaises(RigError) as caught:
                    load_rig(self.root, limits=cap)
                self.assertEqual(caught.exception.report['code'], code)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO unavailable.')
    def test_fifo_replacement_between_admission_and_read_is_bounded(self):
        # A subprocess timeout turns a blocking path.open regression into a
        # bounded failure instead of hanging the whole suite.
        code = r'''
import os, tempfile
from pathlib import Path
from rig_errors import RigError
from rig_contracts import DEFAULT_LIMITS
from rig_load_limits import LoadBudget, scan_bundle
from rig_models import _binary_bundle
for method in ('json', 'hash', 'bundle'):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        path = root / 'payload'
        payload = _binary_bundle({}, {}) if method == 'bundle' else b'{}'
        path.write_bytes(payload)
        original = root / 'original'
        budget = LoadBudget(DEFAULT_LIMITS)
        real_file = budget.file
        def replaced_after_admission(selected, **kwargs):
            size = real_file(selected, **kwargs)
            selected.rename(original)
            os.mkfifo(selected)
            return size
        budget.file = replaced_after_admission
        try:
            try:
                if method == 'bundle': scan_bundle(path, DEFAULT_LIMITS, budget)
                else: getattr(budget, method)(path)
            except RigError as exc:
                assert exc.report['code'] in ('RIG_MISSING_ASSET', 'RIG_PAYLOAD_TYPE'), exc.report
            else:
                raise AssertionError('FIFO admitted by ' + method)
        finally:
            path.unlink()
            original.rename(path)
        assert path.read_bytes() == payload
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_matching_raw_manifest_pin_preserves_sources_and_caller_limits(self):
        before = self.hashes()
        pin = sha256(self.root / 'rig.json')
        rig = load_rig(self.root, expected_manifest_sha256=pin,
                       limits={'memory_bytes': 35 * 1024**2})
        self.assertEqual(rig.geometry.vertex_count, 706)
        self.assertEqual(before, self.hashes())
        with self.assertRaises(RigError) as caught:
            load_rig(self.root, expected_manifest_sha256=pin, limits={'duration': 1})
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_ANIMATION')

    def test_raw_manifest_pin_rejects_whitespace_change_before_json_decode(self):
        path = self.root / 'rig.json'
        original = path.read_bytes()
        pin = sha256(path)
        path.write_bytes(b' ' + original)
        # Semantically identical JSON remains a different immutable byte record.
        self.assertEqual(json.loads(path.read_bytes()), json.loads(original))
        with patch('rig_load_limits.decode_json', side_effect=AssertionError('unpinned JSON decoded')) as decoder:
            with patch('rig_models._materialize_array', side_effect=AssertionError('unpinned array loaded')) as arrays:
                with self.assertRaises(RigError) as caught:
                    load_rig(self.root, expected_manifest_sha256=pin)
                self.assertEqual(caught.exception.report['code'], 'RIG_HASH')
                decoder.assert_not_called()
                arrays.assert_not_called()
        self.assertEqual(path.read_bytes(), b' ' + original)

    def test_manifest_pin_rejects_valid_alternate_geometry_manifest(self):
        path = self.root / 'rig.json'
        pin = sha256(path)
        rig = load_rig(self.root, expected_manifest_sha256=pin)
        original_x = rig.geometry.meshes[0].positions[:, 0].copy()
        rig.geometry.meshes[0].positions[:, 0] *= 2
        rig.manifest['geometry'] = save_geometry(self.root, rig.geometry, rig.manifest['limits'])
        path.write_text(json.dumps(rig.manifest))
        alternate = load_rig(self.root)
        np.testing.assert_allclose(alternate.geometry.meshes[0].positions[:, 0], original_x * 2)
        with patch('rig_load_limits.decode_json', side_effect=AssertionError('alternate manifest decoded')):
            with self.assertRaises(RigError) as caught:
                load_rig(self.root, expected_manifest_sha256=pin)
            self.assertEqual(caught.exception.report['code'], 'RIG_HASH')

    def write_two_mesh_morph_fixture(self):
        rig = load_rig(self.root)
        self.assertGreaterEqual(len(rig.geometry.meshes), 2)
        for mesh in rig.geometry.meshes:
            mesh.morphs = {}
            mesh.morph_defaults = {}
        for mesh in rig.geometry.meshes[:2]:
            # Same target name on two mesh instances counts twice; POSITION and
            # NORMAL inside one target remain one morph, not two.
            mesh.morphs['shared'] = {'POSITION': np.zeros_like(mesh.positions),
                                     'NORMAL': np.zeros_like(mesh.positions)}
        rig.manifest['geometry'] = save_geometry(self.root, rig.geometry, rig.manifest['limits'])
        (self.root / 'rig.json').write_text(json.dumps(rig.manifest))

    def test_total_morph_cap_rejects_cross_mesh_excess_before_first_array(self):
        self.write_two_mesh_morph_fixture()
        before = self.hashes()
        for bypass_preflight in (False, True):
            with self.subTest(bypass_preflight=bypass_preflight):
                with patch('rig_models._materialize_array', side_effect=AssertionError('array before morph admission')) as arrays:
                    with patch('rig_models._preflight_load', return_value=None) if bypass_preflight else nullcontext():
                        with self.assertRaises(RigError) as caught:
                            load_rig(self.root, limits={'morphs': 1}, max_total_morph_targets=1)
                        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MORPHS')
                        self.assertEqual(caught.exception.report['observed'], 2)
                        arrays.assert_not_called()
        self.assertEqual(before, self.hashes())

    def test_total_morph_cap_preserves_per_mesh_profile_and_source_pin(self):
        self.write_two_mesh_morph_fixture()
        before = self.hashes()
        pin = sha256(self.root / 'rig.json')
        for total in (None, 2, 2.0, 10**400):
            with self.subTest(total=repr(total)[:30]):
                rig = load_rig(self.root, limits={'morphs': 1}, max_total_morph_targets=total,
                               expected_manifest_sha256=pin)
                self.assertEqual(sum(len(mesh.morphs) for mesh in rig.geometry.meshes), 2)
                self.assertEqual(rig.manifest['limits']['morphs'], DEFAULT_LIMITS['morphs'])
        self.assertEqual(before, self.hashes())

    def test_zero_total_morph_cap_admits_actual_zero_morph_geometry(self):
        rig = load_rig(self.root)
        for mesh in rig.geometry.meshes:
            mesh.morphs = {}
            mesh.morph_defaults = {}
        rig.manifest['geometry'] = save_geometry(self.root, rig.geometry, rig.manifest['limits'])
        (self.root / 'rig.json').write_text(json.dumps(rig.manifest))
        before = self.hashes()
        loaded = load_rig(self.root, max_total_morph_targets=0, limits={'morphs': 1})
        self.assertEqual(sum(len(mesh.morphs) for mesh in loaded.geometry.meshes), 0)
        self.assertEqual(before, self.hashes())

    def test_invalid_total_morph_caps_fail_before_manifest_decode(self):
        for value in (True, False, -1, 1.5, float('nan'), float('inf'), '1', {}, []):
            with self.subTest(value=repr(value)):
                with patch('rig_load_limits.decode_json', side_effect=AssertionError('invalid cap decoded source')):
                    with self.assertRaises(RigError) as caught:
                        load_rig(self.root, max_total_morph_targets=value)
                    self.assertEqual(caught.exception.report['code'], 'RIG_LIMITS')

    def test_runtime_bundle_snapshot_hash_is_checked_before_arrays(self):
        path = Path(self.temp.name) / 'valid.npz'
        path.write_bytes(_binary_bundle({}, {'safe': np.zeros(3)}))
        with patch('rig_models._materialize_array', side_effect=AssertionError('unsafe allocation')) as materialize:
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, DEFAULT_LIMITS, expected_hash='0' * 64)
            self.assertEqual(caught.exception.report['code'], 'RIG_HASH')
            materialize.assert_not_called()


    @unittest.skipUnless(os.name == 'posix' and Path('/proc/self/fd').is_dir(),
                         'Linux directory descriptor capability unavailable.')
    def test_caller_budget_preserves_pinned_root_after_path_replacement(self):
        before = self.hashes()
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with directory_capability(fd) as capability:
                kept = self.root.with_name('retained')
                self.root.rename(kept)
                self.root.mkdir()
                (self.root / 'rig.json').write_bytes(b'foreign replacement')
                with patch('rig_models._materialize_array', side_effect=AssertionError('unadmitted array')) as materialize:
                    with self.assertRaises(RigError) as caught:
                        load_rig(capability.path, limits={'memory_bytes': 3 * 1024**2})
                    self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
                    materialize.assert_not_called()
                rig = load_rig(capability.path, limits={'memory_bytes': 35 * 1024**2})
                self.assertEqual(rig.geometry.vertex_count, 706)
                self.assertEqual(rig.root, capability.path)
                self.assertEqual((self.root / 'rig.json').read_bytes(), b'foreign replacement')
                self.assertEqual(before, {str(path.relative_to(kept)): sha256(path)
                                         for path in kept.rglob('*') if path.is_file()})
        finally:
            os.close(fd)


    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO unavailable.')
    def test_binary_fifo_is_rejected_by_shared_reader_before_snapshot(self):
        path = Path(self.temp.name) / 'fifo.npz'
        os.mkfifo(path)
        with patch('rig_models._materialize_array', side_effect=AssertionError('unadmitted array')) as materialize:
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, DEFAULT_LIMITS)
            self.assertEqual(caught.exception.report['code'], 'RIG_MISSING_ASSET')
            materialize.assert_not_called()


    def test_forged_metadata_size_cannot_expand_beyond_bounded_read_requests(self):
        path = Path(self.temp.name) / 'forged-metadata-size.npz'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('metadata.json', b'{}' + b' ' * (8 * 1024**2))
        data = bytearray(path.read_bytes())
        cursor = data.index(b'PK\x01\x02')
        struct.pack_into('<I', data, cursor + 16, zlib.crc32(b'{}'))
        struct.pack_into('<I', data, cursor + 24, 2)
        path.write_bytes(data)
        real = zipfile._get_decompressor
        observations = []
        class Observed:
            def __init__(self, value): self.value = value
            def __getattr__(self, name): return getattr(self.value, name)
            def decompress(self, data, *args):
                result = self.value.decompress(data, *args)
                observations.append((args[0] if args else None, len(result)))
                return result
        with patch('zipfile._get_decompressor', side_effect=lambda *a, **kw: Observed(real(*a, **kw))):
            metadata, arrays = _read_bundle(path, dict(DEFAULT_LIMITS, memory_bytes=5 * 1024**2))
        self.assertEqual(metadata, {})
        self.assertEqual(arrays, {})
        self.assertTrue(observations)
        self.assertTrue(all(request is not None and request <= 64 * 1024 and output <= 64 * 1024
                            for request, output in observations), observations)


    def test_forged_array_size_uses_bounded_materialization_reads(self):
        path = Path(self.temp.name) / 'forged-array-size.npz'
        stream = io.BytesIO()
        np.lib.format.write_array(stream, np.zeros((0,), dtype='<f8'), allow_pickle=False)
        admitted = stream.getvalue()
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('metadata.json', '{}')
            archive.writestr('empty.npy', admitted + b' ' * (8 * 1024**2))
        data = bytearray(path.read_bytes())
        cursor = data.index(b'PK\x01\x02')
        while True:
            name_size, extra_size, comment_size = struct.unpack_from('<3H', data, cursor + 28)
            if data[cursor + 46:cursor + 46 + name_size] == b'empty.npy': break
            cursor += 46 + name_size + extra_size + comment_size
        struct.pack_into('<I', data, cursor + 16, zlib.crc32(admitted))
        struct.pack_into('<I', data, cursor + 24, len(admitted))
        path.write_bytes(data)
        real = zipfile._get_decompressor
        observations = []
        class Observed:
            def __init__(self, value): self.value = value
            def __getattr__(self, name): return getattr(self.value, name)
            def decompress(self, data, *args):
                result = self.value.decompress(data, *args)
                observations.append((args[0] if args else None, len(result)))
                return result
        with patch('zipfile._get_decompressor', side_effect=lambda *a, **kw: Observed(real(*a, **kw))):
            _, arrays = _read_bundle(path, dict(DEFAULT_LIMITS, memory_bytes=5 * 1024**2))
        self.assertEqual(arrays['empty'].shape, (0,))
        self.assertTrue(observations)
        self.assertTrue(all(request is not None and request <= 64 * 1024 and output <= 64 * 1024
                            for request, output in observations), observations)


    def test_forged_numpy_header_length_rejected_before_large_read(self):
        path = Path(self.temp.name) / 'forged-header-length.npz'
        fake = b'\x93NUMPY\x02\x00' + struct.pack('<I', 8 * 1024**2) + b' ' * (8 * 1024**2)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('metadata.json', '{}')
            archive.writestr('data.npy', fake)
        data = bytearray(path.read_bytes())
        cursor = data.index(b'PK\x01\x02')
        while True:
            name_size, extra_size, comment_size = struct.unpack_from('<3H', data, cursor + 28)
            if data[cursor + 46:cursor + 46 + name_size] == b'data.npy': break
            cursor += 46 + name_size + extra_size + comment_size
        struct.pack_into('<I', data, cursor + 16, zlib.crc32(fake[:32 * 1024]))
        struct.pack_into('<I', data, cursor + 24, 32 * 1024)
        path.write_bytes(data)
        real = zipfile.ZipExtFile.read
        requests = []
        def observed(stream, size=-1):
            requests.append(size)
            self.assertGreaterEqual(size, 0)
            self.assertLessEqual(size, 10032)
            return real(stream, size)
        with patch.object(zipfile.ZipExtFile, 'read', observed):
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, dict(DEFAULT_LIMITS, memory_bytes=5 * 1024**2))
            self.assertEqual(caught.exception.report['code'], 'RIG_BINARY')
        self.assertTrue(requests)


    def test_prefixed_decoy_directory_rejected_before_zipinfo_allocation(self):
        path = Path(self.temp.name) / 'decoy-directory.npz'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('metadata.json', '{}')
            for i in range(30): archive.writestr(f'entry{i}.npy', b'')
        data = bytearray(path.read_bytes())
        eocd = data.rfind(b'PK\x05\x06')
        directory_size, directory_offset = struct.unpack_from('<II', data, eocd + 12)
        decoy = bytearray(data[directory_offset:directory_offset + 46])
        struct.pack_into('<3H', decoy, 28, directory_size - 46, 0, 0)
        decoy.extend(b'a' * (directory_size - 46))
        struct.pack_into('<HH', data, eocd + 8, 1, 1)
        struct.pack_into('<I', data, eocd + 16, 0)
        path.write_bytes(decoy + data)
        with patch('rig_load_limits.zipfile.ZipFile', side_effect=AssertionError('ZipInfo allocated before guard')):
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, dict(DEFAULT_LIMITS, archive_files=1))
            self.assertEqual(caught.exception.report['code'], 'RIG_BINARY')



if __name__ == '__main__':
    unittest.main()
