"""Real native-rig admission, aggregate budgets and immutable-source regressions."""
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from PIL import Image

from rig_errors import RigError, sha256
from rig_models import (TextureImage, load_rig, rig_signature, save_geometry,
                        _binary_bundle, _read_bundle)
from rig_contracts import DEFAULT_LIMITS
from rig_load_limits import LoadBudget, effective_limits

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

    def test_runtime_bundle_snapshot_hash_is_checked_before_arrays(self):
        path = Path(self.temp.name) / 'valid.npz'
        path.write_bytes(_binary_bundle({}, {'safe': np.zeros(3)}))
        with patch('rig_models._materialize_array', side_effect=AssertionError('unsafe allocation')) as materialize:
            with self.assertRaises(RigError) as caught:
                _read_bundle(path, DEFAULT_LIMITS, expected_hash='0' * 64)
            self.assertEqual(caught.exception.report['code'], 'RIG_HASH')
            materialize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
