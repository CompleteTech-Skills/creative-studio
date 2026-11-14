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
import tracemalloc
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


class SharedLoadBudgetTests(unittest.TestCase):
    def test_eight_influence_budget_fork_preserves_explicit_cap_and_never_loosens(self):
        parent = LoadBudget({**DEFAULT_LIMITS, 'influences': 8})
        child = parent.fork({'influences': 8})
        self.assertEqual(child.limits['influences'], 8)
        parent.commit(child)
        self.assertEqual(parent.fork({'influences': 4}).limits['influences'], 4)
        limited = LoadBudget(DEFAULT_LIMITS)
        self.assertEqual(limited.fork({'influences': 8}).limits['influences'], 4)

    def test_file_admission_failure_does_not_poison_retry(self):
        budget = LoadBudget({**DEFAULT_LIMITS, 'dependency_bytes': 10})
        budget._record_file_size(Path('first'), 7)
        before = deepcopy(budget._state())
        with self.assertRaises(RigError):
            budget._record_file_size(Path('second'), 4)
        self.assertEqual(budget._state(), before)
        budget._record_file_size(Path('second'), 3)
        self.assertEqual(budget.input_bytes, 10)

    def test_lifecycle_capture_parse_discovery_and_retained_copies(self):
        parent = LoadBudget(DEFAULT_LIMITS)
        discovery = parent.fork(DEFAULT_LIMITS)
        discovery.captured_input(key='snapshot:entry', size=100, source=True)
        discovery.parsed_json(key='discovery:manifest', size=100)
        self.assertEqual(parent.input_bytes, 0)
        live = parent.fork(DEFAULT_LIMITS)
        self.assertTrue(live.captured_input(key='snapshot:entry', size=100, source=True))
        self.assertFalse(live.captured_input(key='snapshot:entry', size=100, source=True))
        self.assertTrue(live.parsed_json(key='load:manifest', size=100))
        self.assertFalse(live.parsed_json(key='load:manifest', size=100))
        parent.commit(live)
        before = deepcopy(parent._state())
        with self.assertRaises(RigError):
            parent.captured_input(key='snapshot:entry', size=101, source=True)
        self.assertEqual(parent._state(), before)
        parent.captured_input(key='second-copy:entry', size=100, source=True)
        self.assertEqual(parent.input_bytes, 200)
        self.assertEqual(parent.json_bytes, 100)

    def test_keyed_cubic_animation_boundary_and_rollback(self):
        probe = LoadBudget(DEFAULT_LIMITS)
        probe.animation(key='load:channel', keys=10, width=4, destinations=3, cubic=True)
        cap = probe.memory
        budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': cap, 'keyframes': 30})
        budget.animation(key='load:channel', keys=10, width=4, destinations=3, cubic=True)
        budget.animation(key='load:channel', keys=10, width=4, destinations=3, cubic=True)
        self.assertEqual((budget.memory, budget.animation_keyframes, budget.animation_tracks), (cap, 30, 3))
        before = deepcopy(budget._state())
        with self.assertRaises(RigError):
            budget.animation(key='load:channel', keys=10, width=4, destinations=3, cubic=False)
        with self.assertRaises(RigError):
            budget.animation(key='next:channel', keys=1, width=1, destinations=1)
        self.assertEqual(budget._state(), before)
        limited = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': cap - 1})
        with patch('builtins.list', side_effect=AssertionError('expanded')) as expansion:
            with self.assertRaises(RigError):
                limited.animation(key='load:channel', keys=10, width=4, destinations=3, cubic=True)
                list(range(10))
            expansion.assert_not_called()

    def test_commit_requires_fresh_owned_child_and_preserves_parent_on_failure(self):
        parent = LoadBudget(dict(DEFAULT_LIMITS, memory_bytes=3_000_000))
        stale = parent.fork(DEFAULT_LIMITS)
        parent.reserve('parent', 1000, 'parent asset')
        before = deepcopy(parent._state())
        with self.assertRaises(RigError):
            parent.commit(stale)
        self.assertEqual(parent._state(), before)
        unrelated = LoadBudget(DEFAULT_LIMITS).fork(DEFAULT_LIMITS)
        unrelated.reserve('large', 4_000_000, 'asset')
        with self.assertRaises(RigError):
            parent.commit(unrelated)
        self.assertEqual(parent._state(), before)

        first = parent.fork(DEFAULT_LIMITS)
        sibling = parent.fork(DEFAULT_LIMITS)
        parent.commit(first)
        for child in (first, sibling):
            with self.assertRaises(RigError):
                parent.commit(child)
        self.assertEqual(parent._state(), before)

    def test_commit_detaches_child_reservations_and_files(self):
        parent = LoadBudget(DEFAULT_LIMITS)
        child = parent.fork(DEFAULT_LIMITS)
        child.reserve('before', 123, 'asset')
        parent.commit(child)
        memory = parent.memory
        child.reserve('after', 1000, 'asset')
        child._record_file_size(Path('child-only'), 17)
        self.assertEqual(parent.memory, memory)
        self.assertNotIn(Path('child-only'), parent.files)
        self.assertTrue(parent.reserve('after', 1000, 'asset'))
        self.assertGreater(parent.memory, memory)

    def test_fork_checks_existing_aggregate_input_and_commit_checks_caps(self):
        parent = LoadBudget(DEFAULT_LIMITS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input'
            path.write_bytes(b'x' * 1000)
            parent.file(path)
            before = deepcopy(parent._state())
            with self.assertRaises(RigError) as caught:
                parent.fork({'dependency_bytes': 999})
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_BINARY')
            self.assertEqual(parent._state(), before)
            child = parent.fork({'dependency_bytes': 1000})
            child.input_bytes += 1
            with self.assertRaises(RigError) as caught:
                parent.commit(child)
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_BINARY')
            self.assertEqual(parent._state(), before)
        child = parent.fork({'memory_bytes': 3_000_000})
        child.memory = 3_000_001
        with self.assertRaises(RigError) as caught:
            parent.commit(child)
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
        self.assertEqual(parent._state(), before)

    def test_nested_commit_preserves_outer_transaction(self):
        parent = LoadBudget(DEFAULT_LIMITS)
        child = parent.fork(DEFAULT_LIMITS)
        grandchild = child.fork(DEFAULT_LIMITS)
        grandchild.reserve('nested', 100, 'asset')
        child.commit(grandchild)
        parent.commit(child)
        self.assertFalse(parent.reserve('nested', 100, 'asset'))

    def test_reservation_identity_and_atomic_conflicts(self):
        budget = LoadBudget(DEFAULT_LIMITS)
        budget.image(key='image', encoded_bytes=16, pixels=1)
        before = budget.report().copy()
        budget.image(key='image', encoded_bytes=16, pixels=1)
        self.assertEqual(before, budget.report())
        with self.assertRaises(RigError):
            budget.image(key='image', encoded_bytes=0, pixels=2)
        self.assertEqual(before, budget.report())
        budget.animation_report(key='report', keys=1, tracks=0)
        before = budget.report().copy()
        with self.assertRaises(RigError):
            budget.animation_report(key='report', keys=0, tracks=1)
        with self.assertRaises(RigError):
            budget.reserve('report', 1024, 'animation exact-time report expansion')
        self.assertEqual(before, budget.report())
        budget.reserve('generic', 1, 'asset')
        before = budget.report().copy()
        with self.assertRaises(RigError):
            budget.reserve('generic', True, 'asset')
        self.assertEqual(before, budget.report())

    def test_zero_reservations_are_bounded_and_charged_before_insertion(self):
        from rig_load_limits import _RESERVATION_OVERHEAD, _RESERVATION_STRING_MAX
        cap = 2 * 1024**2 + _RESERVATION_OVERHEAD
        budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': cap})
        key = '\U0010ffff' * _RESERVATION_STRING_MAX
        self.assertTrue(budget.reserve(key, 0, ''))
        self.assertFalse(budget.reserve(key, 0, ''))
        before = budget.report().copy()
        with self.assertRaises(RigError):
            budget.reserve('another', 0, '')
        with self.assertRaises(RigError):
            budget.reserve(key + 'x', 0, '')
        with self.assertRaises(RigError):
            budget.reserve('asset', 0, 'x' * (_RESERVATION_STRING_MAX + 1))
        self.assertEqual(before, budget.report())
        self.assertEqual(len(budget._reservations), 1)
        with patch('rig_load_limits._RESERVATION_COUNT_MAX', 1):
            other = LoadBudget(DEFAULT_LIMITS)
            other.reserve('one', 0, '')
            before = other.report().copy()
            with self.assertRaises(RigError):
                other.reserve('two', 0, '')
            self.assertEqual(before, other.report())

    def test_float64_extreme_ratio_expansion_fits_report_estimate(self):
        from asset_library_validation import _ratio
        from rig_load_limits import _REPORT_KEY_BYTES
        # The smallest subnormal produces a denominator of 2**1074; the
        # largest finite float produces a 1024-bit numerator.
        values = [float.fromhex('0x0.0000000000001p-1022'),
                  float.fromhex('0x1.fffffffffffffp+1023')]
        for value in values:
            with self.subTest(value=value):
                tracemalloc.start()
                try:
                    ratios = [_ratio(value) for _ in range(10000)]
                    retained, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, len(ratios) * _REPORT_KEY_BYTES)
                self.assertLessEqual(retained, peak)
                if value < 1:
                    self.assertEqual(ratios[0]['denominator'], 2**1074)
                else:
                    self.assertEqual(ratios[0]['numerator'].bit_length(), 1024)

    def test_gltf_image_shares_budget_and_rolls_back_failed_decode(self):
        import base64
        from rig_gltf import GltfBuilder
        from rig_import import load_character_model
        image = io.BytesIO()
        Image.new('RGBA', (64, 64), 'red').save(image, format='PNG')
        encoded = image.getvalue()
        builder = GltfBuilder()
        position = builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', target=34962, bounds=True)
        builder.json.update(nodes=[{'mesh': 0}], meshes=[{'primitives': [{'attributes': {'POSITION': position}}]}],
                            scenes=[{'nodes': [0]}], images=[{'uri': 'data:image/png;base64,' + base64.b64encode(encoded).decode()}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.glb'
            builder.write(path)
            probe = LoadBudget(DEFAULT_LIMITS)
            loaded = load_character_model(path, budget=probe)
            self.assertEqual(len(loaded.geometry.images), 1)
            self.assertEqual(probe.texture_rgba_bytes, 64 * 64 * 4)
            boundary = probe.memory
            limited = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': boundary - 1})
            before = deepcopy(limited._state())
            with self.assertRaises(RigError) as caught:
                load_character_model(path, budget=limited)
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
            self.assertEqual(limited._state(), before)
            exact = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': boundary})
            load_character_model(path, budget=exact)
            self.assertEqual(exact.texture_rgba_bytes, 64 * 64 * 4)
            before = deepcopy(exact._state())
            with self.assertRaises(RigError):
                load_character_model(path, budget=exact)
            self.assertEqual(exact._state(), before)
            # A valid header followed by corrupt raster data fails after image
            # admission; the caller can still reuse its ledger.
            corrupt = bytearray(encoded)
            corrupt[-1] ^= 1
            builder.json['images'][0]['uri'] = 'data:image/png;base64,' + base64.b64encode(corrupt).decode()
            builder.write(path)
            malformed = LoadBudget(DEFAULT_LIMITS)
            before = deepcopy(malformed._state())
            with self.assertRaises(RigError) as caught:
                load_character_model(path, budget=malformed)
            self.assertEqual(caught.exception.report['code'], 'RIG_TEXTURE_DECODE')
            self.assertEqual(malformed._state(), before)

    def test_real_fanout_preflight_plus_report_rejected_before_expansion(self):
        from rig_gltf import GltfBuilder, read_gltf, preflight_gltf
        builder = GltfBuilder()
        position = builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', target=34962, bounds=True)
        delta = builder.accessor([[0, 0, .1]] * 3, 'VEC3', target=34962, bounds=True)
        times = builder.accessor(np.linspace(0, 1, 1000), 'SCALAR', bounds=True)
        values = builder.accessor([0] * 1000, 'SCALAR')
        builder.json.update(nodes=[{'mesh': 0}], meshes=[{'primitives': [
            {'attributes': {'POSITION': position}, 'targets': [{'POSITION': delta}]}
            for _ in range(200)]}], scenes=[{'nodes': [0]}], animations=[{
                'samplers': [{'input': times, 'output': values}],
                'channels': [{'sampler': 0, 'target': {'node': 0, 'path': 'weights'}}]}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fanout.glb'
            builder.write(path)
            admission = preflight_gltf(read_gltf(path), encoded_bytes=path.stat().st_size)
        self.assertEqual(admission['animation_keyframes'], 200_000)
        self.assertEqual(admission['animation_tracks'], 200)
        # Until preflight accepts the caller's budget, transfer its estimate once.
        budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': admission['estimated_memory_bytes']})
        budget.add(admission['estimated_memory_bytes'] - budget.memory, 'preflight transfer')
        with patch('asset_library_validation._ratio') as expand:
            with self.assertRaises(RigError):
                budget.animation_report(key='inspection:clips', keys=200_000, tracks=200)
                expand(0.)
            expand.assert_not_called()

    def test_report_rejects_before_real_exact_time_expansion(self):
        from asset_library_validation import _ratio
        budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': 29_387_328})
        budget.animation(keys=1000, width=1, destinations=200)
        budget.animation(keys=2, width=3, destinations=1)
        expanded = []
        with self.assertRaises(RigError) as caught:
            budget.animation_report(key='inspection:clips', keys=200_002, tracks=201)
            expanded.extend(_ratio(float(i)) for i in range(200_002))
        self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
        self.assertEqual(expanded, [])
        self.assertEqual(budget.report_bytes, 0)

    def test_inspection_rejects_actual_201_track_fanout_before_ratios(self):
        from asset_library_validation import _inspect_geometry
        from rig_gltf import GltfBuilder
        builder = GltfBuilder()
        position = builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', target=34962, bounds=True)
        delta = builder.accessor([[0, 0, .1]] * 3, 'VEC3', target=34962, bounds=True)
        times = builder.accessor(np.linspace(0, 1, 1000), 'SCALAR', bounds=True)
        values = builder.accessor([0] * 1000, 'SCALAR')
        short_times = builder.accessor([0, 1], 'SCALAR', bounds=True)
        translations = builder.accessor([[0, 0, 0], [1, 0, 0]], 'VEC3')
        builder.json.update(nodes=[{'mesh': 0}], meshes=[{'primitives': [
            {'attributes': {'POSITION': position}, 'targets': [{'POSITION': delta}]}
            for _ in range(200)]}], scenes=[{'nodes': [0]}], animations=[{
                'samplers': [{'input': times, 'output': values},
                             {'input': short_times, 'output': translations}],
                'channels': [{'sampler': 0, 'target': {'node': 0, 'path': 'weights'}},
                             {'sampler': 1, 'target': {'node': 0, 'path': 'translation'}}]}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fanout.glb'
            builder.write(path)
            with patch('rig_interop.validate_gltf', return_value={'ok': True}), \
                    patch('asset_library_validation._ratio') as ratio:
                with self.assertRaises(RigError) as caught:
                    _inspect_geometry(path, limits={'memory_bytes': 29_387_328})
                self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
                ratio.assert_not_called()
    def test_cubic_and_large_report_exact_boundary_and_repeat(self):
        limits = {**DEFAULT_LIMITS, 'memory_bytes': 2 * 1024**3}
        probe = LoadBudget(limits)
        probe.animation(keys=1000, width=4, destinations=20, cubic=True)
        probe.animation_report(key='report', keys=20_000, tracks=20)
        cap = probe.memory
        budget = LoadBudget({**limits, 'memory_bytes': cap})
        budget.animation(keys=1000, width=4, destinations=20, cubic=True)
        budget.animation_report(key='report', keys=20_000, tracks=20)
        budget.animation_report(key='report', keys=20_000, tracks=20)
        self.assertEqual(budget.memory, cap)
        with self.assertRaises(RigError):
            budget.animation_report(key='report', keys=19_999, tracks=20)
        self.assertEqual(budget.memory, cap)

    def test_image_and_report_share_retained_capacity(self):
        probe = LoadBudget(DEFAULT_LIMITS)
        probe.animation(keys=10, width=3, destinations=1)
        probe.image(key='image:0', encoded_bytes=100, pixels=1000)
        probe.animation_report(key='report', keys=10, tracks=1)
        budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': probe.memory - 1})
        budget.animation(keys=10, width=3, destinations=1)
        budget.image(key='image:0', encoded_bytes=100, pixels=1000)
        budget.image(key='image:0', encoded_bytes=100, pixels=1000)
        with self.assertRaises(RigError):
            budget.animation_report(key='report', keys=10, tracks=1)
        self.assertEqual(budget.texture_rgba_bytes, 4000)


class NativeLoadLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'robot'
        shutil.copytree(ROOT / 'examples/rig-3d/robotic', self.root)

    def test_native_animation_shared_counts_and_exact_boundary(self):
        baseline = load_rig(self.root)
        keys = sum(len(t['times']) for clip in baseline.clips.values() for t in clip['tracks'])
        tracks = sum(len(clip['tracks']) for clip in baseline.clips.values())
        self.assertEqual((keys, tracks), (127, 6))
        budget = LoadBudget({**DEFAULT_LIMITS, 'keyframes': keys * 2})
        first = load_rig(self.root, budget=budget)
        self.assertEqual((budget.animation_keyframes, budget.animation_tracks), (keys, tracks))
        self.assertEqual(first.resource_usage, budget.report())
        second = load_rig(self.root, budget=budget)
        self.assertEqual((budget.animation_keyframes, budget.animation_tracks), (keys * 2, tracks * 2))
        self.assertEqual(second.resource_usage, budget.report())
        before = deepcopy(budget.__dict__)
        with patch('rig_models._materialize_array', side_effect=AssertionError('materialized')) as arrays:
            with self.assertRaises(RigError) as caught:
                load_rig(self.root, budget=budget)
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_KEYFRAMES')
            arrays.assert_not_called()
        self.assertEqual(budget.__dict__, before)
        with self.assertRaises(RigError):
            budget.fork({'keyframes': keys * 2 - 1})

    def test_prior_animation_reservation_and_phase_parity(self):
        for skip_preflight in (False, True):
            with self.subTest(skip_preflight=skip_preflight):
                budget = LoadBudget({**DEFAULT_LIMITS, 'keyframes': 127})
                budget.animation(keys=1, width=1, destinations=1)
                before = deepcopy(budget.__dict__)
                context = patch('rig_models._preflight_load', return_value=None) if skip_preflight else nullcontext()
                with context:
                    with self.assertRaises(RigError) as caught:
                        load_rig(self.root, budget=budget)
                self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_KEYFRAMES')
                self.assertEqual(budget.__dict__, before)

                exact = LoadBudget({**DEFAULT_LIMITS, 'keyframes': 128})
                exact.animation(keys=1, width=1, destinations=1)
                with (patch('rig_models._preflight_load', return_value=None) if skip_preflight else nullcontext()):
                    load_rig(self.root, budget=exact)
                self.assertEqual((exact.animation_keyframes, exact.animation_tracks), (128, 7))

    def test_native_counts_roll_back_after_live_clip_admission(self):
        budget = LoadBudget(DEFAULT_LIMITS)
        budget.animation(keys=2, width=1, destinations=1)
        before = deepcopy(budget.__dict__)
        original = LoadBudget.native_animation
        def fail_after_admission(ledger, **kwargs):
            original(ledger, **kwargs)
            raise RuntimeError('after clip admission')
        with patch('rig_models._preflight_load', return_value=None), patch.object(
                LoadBudget, 'native_animation', fail_after_admission):
            with self.assertRaisesRegex(RuntimeError, 'after clip admission'):
                load_rig(self.root, budget=budget)
        self.assertEqual(budget.__dict__, before)

    def test_shared_native_budget_transaction_and_boundary(self):
        second = Path(self.temp.name) / 'second'
        shutil.copytree(self.root, second)
        probe = LoadBudget(DEFAULT_LIMITS)
        probe.reserve('caller:prior', 1234, 'prior allocation')
        initial = probe.memory
        load_rig(self.root, budget=probe)
        first = probe.memory
        load_rig(second, budget=probe)
        boundary = probe.memory
        self.assertEqual(boundary - first, first - initial)

        budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': boundary})
        budget.reserve('caller:prior', 1234, 'prior allocation')
        load_rig(self.root, budget=budget)
        load_rig(second, budget=budget)
        self.assertEqual(budget.memory, boundary)
        self.assertFalse(budget.reserve('caller:prior', 1234, 'prior allocation'))

        limited = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': boundary - 1})
        limited.reserve('caller:prior', 1234, 'prior allocation')
        load_rig(self.root, budget=limited)
        before = deepcopy(limited.__dict__)
        with patch('rig_models._materialize_array', side_effect=AssertionError('materialized')) as materialize:
            with self.assertRaises(RigError):
                load_rig(second, budget=limited)
            materialize.assert_not_called()
        self.assertEqual(limited.__dict__, before)

    def test_shared_budget_with_captured_manifest_pin_and_substitution(self):
        path = self.root / 'rig.json'
        payload = path.read_bytes()
        pin = sha256(path)
        probe = LoadBudget(DEFAULT_LIMITS)
        probe.reserve('prior', 1234, 'prior')
        control = load_rig(self.root, budget=probe, manifest_bytes=payload,
                           expected_manifest_sha256=pin, max_total_morph_targets=10000)
        boundary = probe.memory
        normal = LoadBudget(DEFAULT_LIMITS)
        normal.reserve('prior', 1234, 'prior')
        load_rig(self.root, budget=normal, expected_manifest_sha256=pin)
        self.assertEqual(normal.report(), probe.report())
        path.write_bytes(b'{}')
        budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': boundary})
        budget.reserve('prior', 1234, 'prior')
        loaded = load_rig(self.root, budget=budget, manifest_bytes=payload,
                          expected_manifest_sha256=pin, max_total_morph_targets=10000)
        self.assertEqual(loaded.manifest, control.manifest)
        self.assertEqual(budget.memory, boundary)
        for supplied, expected, code in ((payload, '0' * 64, 'RIG_HASH'),
                                          (bytearray(payload), pin, 'RIG_JSON')):
            before = deepcopy(budget.__dict__)
            with self.assertRaises(RigError) as caught:
                load_rig(self.root, budget=budget, manifest_bytes=supplied,
                         expected_manifest_sha256=expected)
            self.assertEqual(caught.exception.report['code'], code)
            self.assertEqual(budget.__dict__, before)
        limited = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': boundary - 1})
        limited.reserve('prior', 1234, 'prior')
        before = deepcopy(limited.__dict__)
        with patch('rig_models._materialize_array', side_effect=AssertionError('materialized')) as arrays:
            with self.assertRaises(RigError) as caught:
                load_rig(self.root, budget=limited, manifest_bytes=payload, expected_manifest_sha256=pin)
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
            arrays.assert_not_called()
        self.assertEqual(limited.__dict__, before)

    def test_shared_budget_failed_live_load_and_invalid_limits_are_atomic(self):
        budget = LoadBudget(DEFAULT_LIMITS)
        budget.reserve('prior', 4, 'prior')
        before = deepcopy(budget.__dict__)
        with patch('rig_models._materialize_array', side_effect=RuntimeError('failure')):
            with self.assertRaises(RigError):
                load_rig(self.root, budget=budget)
        self.assertEqual(budget.__dict__, before)
        budget.limits = {**DEFAULT_LIMITS, 'memory_bytes': True}
        before = deepcopy(budget.__dict__)
        with self.assertRaises(RigError):
            load_rig(self.root, budget=budget)
        self.assertEqual(budget.__dict__, before)

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
        environment = dict(os.environ)
        environment['PYTHONPATH'] = os.pathsep.join(filter(None, (
            str(ROOT / 'scripts'), environment.get('PYTHONPATH'))))
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=5, env=environment)
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

    def write_large_manifest_fixture(self):
        path = self.root / 'rig.json'
        manifest = json.loads(path.read_bytes())
        manifest['state_mapping'] = {f'state-{index}': dict(status='unavailable', clips=[], reason='a' * 4096)
                                     for index in range(128)}
        payload = json.dumps(manifest).encode()
        path.write_bytes(payload)
        return path, payload, sha256(path)

    def test_captured_manifest_size_is_charged_before_arrays_in_both_budgets(self):
        import rig_models
        path, payload, pin = self.write_large_manifest_fixture()
        before = self.hashes()
        # Initial JSON admission fits by one byte; retaining that parsed object
        # together with any additional native inputs must exceed this cap.
        cap = 2 * 1024**2 + 64 * len(payload) + 1
        original = rig_models._preflight_load
        for bypass_preflight in (False, True):
            def shrink_after_capture(*args, **kwargs):
                path.write_bytes(b'{}')
                if not bypass_preflight:
                    return original(*args, **kwargs)
            try:
                with self.subTest(bypass_preflight=bypass_preflight):
                    with patch('rig_models._preflight_load', side_effect=shrink_after_capture):
                        with patch('rig_models._materialize_array', side_effect=AssertionError('undercharged manifest reached arrays')) as arrays:
                            with self.assertRaises(RigError) as caught:
                                load_rig(self.root, limits={'memory_bytes': cap}, expected_manifest_sha256=pin)
                            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
                            self.assertGreater(caught.exception.report['observed'], cap)
                            arrays.assert_not_called()
            finally:
                path.write_bytes(payload)
            self.assertEqual(before, self.hashes())

    def test_captured_manifest_accounting_matches_stable_control_after_path_shrink(self):
        import rig_models
        path, payload, pin = self.write_large_manifest_fixture()
        before = self.hashes()
        stable = load_rig(self.root, expected_manifest_sha256=pin)
        original = rig_models._preflight_load
        def shrink_after_capture(*args, **kwargs):
            path.write_bytes(b'{}')
            return original(*args, **kwargs)
        try:
            with patch('rig_models._preflight_load', side_effect=shrink_after_capture):
                captured = load_rig(self.root, expected_manifest_sha256=pin,
                                    limits={'memory_bytes': stable.resource_usage['estimated_memory_bytes']})
            self.assertEqual(captured.manifest, stable.manifest)
            self.assertEqual(len(captured.manifest['state_mapping']), 128)
            self.assertEqual(captured.resource_usage, stable.resource_usage)
            self.assertEqual(path.read_bytes(), b'{}')
        finally:
            path.write_bytes(payload)
        self.assertEqual(before, self.hashes())

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
