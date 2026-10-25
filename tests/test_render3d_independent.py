"""Independent boundary tests for the Render3D foundation.

Inputs are explicitly synthetic files and native rig fixtures. Passing these
tests does not establish a Blender render, hardware support, or visual approval.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from render3d_contracts import (
    MAX_FILE_BYTES, Render3DError, blender_frame, canonical_to_blender, frame_times,
    load, make_frame_spec, validate,
)
from render3d_snapshot import (
    NativeRigAdapter, SourceDescription, create_snapshot, verify_snapshot,
)
from rig_test_helpers import fixture


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_ref(path, data=b'evidence'):
    return {'path': path, 'sha256': digest(data), 'bytes': len(data)}


def backend():
    return dict(id='synthetic-worker', adapter_version='1.0.0',
                application_version='not-an-executed-backend',
                executable_sha256='a' * 64, worker_sha256='b' * 64,
                platform='synthetic', engine='cycles', device='cpu')


def request():
    identity = {'id': 'synthetic', 'version': '1.0.0'}
    return dict(schema_version=1, kind='render3d-request', id='request',
                project=deepcopy(identity), scene=deepcopy(identity),
                shot=deepcopy(identity), mode='3d',
                snapshot=file_ref('snapshot/snapshot.json'),
                fps={'numerator': 30000, 'denominator': 1001},
                frame_range={'start': 17, 'end': 43},
                source_start={'numerator': 5, 'denominator': 7},
                playback_rate={'numerator': 3, 'denominator': 2},
                camera='camera', settings=dict(
                    engine='cycles', device_policy='cpu', device=None,
                    allow_cpu_fallback=False, resolution=[320, 180],
                    samples=8, seed=3, denoise=False,
                    output=dict(format='png', bit_depth=8, passes=['combined'],
                                alpha='straight', color_space='srgb'),
                    exposure=0, view_transform='standard',
                    resources=dict(memory_bytes=1048576, storage_bytes=1048576,
                                   timeout_seconds=60, workers=1, max_frames=100)),
                priority=0, max_attempts=1, cache_policy='verify-reuse',
                destination={'path': 'output/frames', 'owner_token': 'owner'})


class SyntheticFileAdapter:
    """A test owner that validates exact file bytes, not a production adapter."""
    id = 'independent-synthetic-files'
    version = '1.0.0'

    def __init__(self, files=None):
        self.files = files or {'source.bin': b'original synthetic source'}

    def describe(self, root):
        for name, expected in self.files.items():
            if (root / name).read_bytes() != expected:
                raise ValueError('Synthetic owner rejects changed bytes')
        return SourceDescription(
            'synthetic-source', '1.0.0', sorted(self.files)[0],
            tuple((name, digest(data)) for name, data in sorted(self.files.items())),
            True, 'native-rig-preview')


class Render3DIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def sources(self, adapter=None):
        adapter = adapter or SyntheticFileAdapter()
        source = self.root / 'source'
        source.mkdir()
        for name, data in adapter.files.items():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return source, adapter

    def test_ntsc_absolute_presentation_and_local_source_clocks(self):
        value = request()
        for index in range(17, 43):
            presentation, source = frame_times(value, index)
            self.assertIsInstance(presentation, Fraction)
            self.assertIsInstance(source, Fraction)
            self.assertEqual(presentation, Fraction(index * 1001, 30000))
            self.assertEqual(source, Fraction(5, 7) + Fraction((index - 17) * 1001, 20000))
        for outside in (16, 43, True, 17.0):
            with self.subTest(outside=outside), self.assertRaises(Render3DError):
                frame_times(value, outside)

    def test_rational_blender_frame_mapping_preserves_subframe_and_long_duration(self):
        rate = Fraction(30000, 1001)
        for time in (Fraction(0), Fraction(1, 120), Fraction(123456789, 1001)):
            frame, subframe = blender_frame(time, rate)
            self.assertIsInstance(subframe, Fraction)
            self.assertGreaterEqual(subframe, 0)
            self.assertLess(subframe, 1)
            self.assertEqual((frame - 1 + subframe) / rate, time)
        self.assertEqual(blender_frame(Fraction(0), rate), (1, Fraction(0)))
        for time, fps in ((0.0, rate), (Fraction(-1), rate), (Fraction(0), 24),
                          (Fraction(0), Fraction(0))):
            with self.subTest(time=time, fps=fps), self.assertRaises(Render3DError):
                blender_frame(time, fps)

    def test_coordinate_conversion_has_correct_axes_and_composes(self):
        # Reference transform explicitly maps (x,y,z) to (x,-z,y).
        basis = np.array([[1., 0., 0., 0.], [0., 0., -1., 0.],
                          [0., 1., 0., 0.], [0., 0., 0., 1.]])
        left = np.array([[0., -1., 0., 2.], [1., 0., 0., 3.],
                         [0., 0., 1., 5.], [0., 0., 0., 1.]])
        right = np.diag([2., 3., 4., 1.])
        converted = canonical_to_blender(left)
        np.testing.assert_allclose(converted, basis @ left @ np.linalg.inv(basis), atol=1e-14)
        np.testing.assert_allclose(converted[:3, 3], [2., -5., 3.], atol=1e-14)
        np.testing.assert_allclose(np.linalg.inv(basis) @ converted @ basis, left, atol=1e-14)
        np.testing.assert_allclose(canonical_to_blender(left @ right),
                                   converted @ canonical_to_blender(right), atol=1e-14)
        self.assertAlmostEqual(np.linalg.det(converted[:3, :3]), 1.)

    def test_coordinate_conversion_rejects_perspective_and_nonfinite_matrices(self):
        perspective = np.eye(4); perspective[3, 2] = -1.
        nonfinite = np.eye(4); nonfinite[0, 0] = np.nan
        for matrix in (perspective, nonfinite, np.eye(3), 'not-a-matrix'):
            with self.subTest(matrix=str(matrix)), self.assertRaises(Render3DError):
                canonical_to_blender(matrix)

    def test_every_public_validation_input_has_typed_failure(self):
        for value in (None, [], '', 7, True, {'kind': None}, {'kind': []}, {'kind': 17}):
            with self.subTest(value=value), self.assertRaises(Render3DError):
                validate(value)

    def test_strict_request_rejects_unknown_fields_unsafe_paths_and_bad_numbers(self):
        variants = []
        value = request(); value['undeclared'] = True; variants.append(value)
        value = request(); value['mode'] = '2d'; variants.append(value)
        value = request(); value['snapshot']['path'] = '../private'; variants.append(value)
        value = request(); value['destination']['path'] = '/tmp/escape'; variants.append(value)
        value = request(); value['settings']['exposure'] = float('inf'); variants.append(value)
        value = request(); value['fps'] = {'numerator': 60000, 'denominator': 2002}; variants.append(value)
        value = request(); value['settings']['resources']['max_frames'] = 25; variants.append(value)
        value = request(); value['frame_range']['end'] = 17; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(Render3DError):
                validate(value)

    def test_output_policy_does_not_silently_downgrade_requested_features(self):
        cases = [dict(bit_depth=32), dict(passes=['depth']), dict(color_space='linear')]
        for changed in cases:
            value = request(); value['settings']['output'].update(changed)
            with self.subTest(changed=changed), self.assertRaises(Render3DError):
                validate(value)
        value = request(); value['settings'].update(device_policy='gpu-required', allow_cpu_fallback=True)
        with self.assertRaises(Render3DError):
            validate(value)

    def test_duplicate_json_keys_rejected_from_actual_bytes(self):
        path = self.root / 'duplicate.json'
        path.write_text('{"kind":"render3d-request","kind":"render3d-result"}', encoding='utf-8')
        with self.assertRaises(Render3DError):
            load(path)

    def test_snapshot_preserves_bytes_after_source_changes_and_detects_corruption(self):
        source, adapter = self.sources()
        destination = self.root / 'snapshot'
        snapshot = create_snapshot(source, destination, adapter)
        self.assertEqual(verify_snapshot(destination, adapter), snapshot)
        (source / 'source.bin').write_bytes(b'modified independently')
        self.assertEqual((destination / 'source.bin').read_bytes(), adapter.files['source.bin'])
        self.assertEqual(verify_snapshot(destination, adapter), snapshot)
        (destination / 'source.bin').write_bytes(b'corrupt')
        with self.assertRaises(Render3DError):
            verify_snapshot(destination, adapter)

    def test_snapshot_never_replaces_an_existing_destination(self):
        source, adapter = self.sources()
        destination = self.root / 'existing'
        destination.mkdir(); marker = destination / 'belongs-to-another-job'
        marker.write_text('keep', encoding='utf-8')
        with self.assertRaises(Render3DError):
            create_snapshot(source, destination, adapter)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_snapshot_creation_rejects_its_own_metadata_filename(self):
        for name in ('snapshot.json', 'Snapshot.json', 'snapshot.json/child'):
            with self.subTest(name=name):
                adapter = SyntheticFileAdapter({name: b'source-owned metadata'})
                source = self.root / ('source-' + str(len(list(self.root.iterdir()))))
                source.mkdir()
                target = source / name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(adapter.files[name])
                destination = self.root / ('destination-' + str(len(list(self.root.iterdir()))))
                with self.assertRaises(Render3DError):
                    create_snapshot(source, destination, adapter)
                self.assertFalse(destination.exists())
                self.assertEqual(target.read_bytes(), adapter.files[name])

    def test_snapshot_rejects_symlink_source_and_in_source_destination(self):
        source, adapter = self.sources()
        with self.assertRaises(Render3DError):
            create_snapshot(source, source / 'new-snapshot', adapter)
        link = self.root / 'source-alias'
        try:
            link.symlink_to(source, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest('Symlink creation unavailable: ' + str(exc))
        with self.assertRaises(Render3DError):
            create_snapshot(link, self.root / 'destination', adapter)

    def test_snapshot_verification_rejects_wrong_adapter_and_missing_file(self):
        source, adapter = self.sources()
        destination = self.root / 'snapshot'
        create_snapshot(source, destination, adapter)
        wrong = SyntheticFileAdapter(); wrong.version = '2.0.0'
        with self.assertRaises(Render3DError):
            verify_snapshot(destination, wrong)
        (destination / 'source.bin').unlink()
        with self.assertRaises(Render3DError):
            verify_snapshot(destination, adapter)

    def test_snapshot_rejects_case_aliases_before_publication(self):
        source, adapter = self.sources(SyntheticFileAdapter(
            {'Models/a.bin': b'a', 'models/b.bin': b'b'}))
        destination = self.root / 'case-aliased-snapshot'
        with self.assertRaises(Render3DError):
            create_snapshot(source, destination, adapter)
        self.assertFalse(destination.exists())

    def test_snapshot_rejects_oversized_file_before_copying(self):
        source, adapter = self.sources()
        oversized = source / 'source.bin'
        with oversized.open('r+b') as stream:
            stream.truncate(MAX_FILE_BYTES + 1)

        class InventoryOnlyAdapter(SyntheticFileAdapter):
            def describe(self, root):
                return SourceDescription('oversized', '1.0.0', 'source.bin',
                                         (('source.bin', 'a' * 64),), True, 'native-rig-preview')

        destination = self.root / 'oversized-snapshot'
        with self.assertRaises(Render3DError):
            create_snapshot(source, destination, InventoryOnlyAdapter())
        self.assertFalse(destination.exists())

    def test_snapshot_rejects_internal_symlink_without_copying_target(self):
        source, adapter = self.sources()
        target = self.root / 'outside.bin'
        target.write_bytes(adapter.files['source.bin'])
        (source / 'source.bin').unlink()
        try:
            (source / 'source.bin').symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest('Symlink creation unavailable: ' + str(exc))
        destination = self.root / 'symlink-snapshot'
        with self.assertRaises(Render3DError):
            create_snapshot(source, destination, adapter)
        self.assertFalse(destination.exists())
        self.assertEqual(target.read_bytes(), adapter.files['source.bin'])

    def test_failed_frozen_owner_validation_leaves_no_published_snapshot(self):
        class SourceOnlyAdapter(SyntheticFileAdapter):
            def describe(self, root):
                if root.name != 'source':
                    raise ValueError('Independent owner rejects frozen closure')
                return super().describe(root)

        source, adapter = self.sources(SourceOnlyAdapter())
        before = (source / 'source.bin').read_bytes()
        destination = self.root / 'invalid-frozen-snapshot'
        with self.assertRaises(Render3DError):
            create_snapshot(source, destination, adapter)
        self.assertFalse(destination.exists())
        self.assertEqual((source / 'source.bin').read_bytes(), before)
        self.assertFalse(list(self.root.glob('.render3d-snapshot-*')))

    def test_frame_plan_is_bound_to_exact_snapshot_bytes(self):
        source, adapter = self.sources()
        destination = self.root / 'snapshot'
        snapshot = create_snapshot(source, destination, adapter)
        path = destination / 'snapshot.json'
        value = request()
        value['snapshot'] = file_ref('snapshot/snapshot.json', path.read_bytes())
        planned = make_frame_spec(value, 17, path, adapter=adapter)
        self.assertEqual(planned['snapshot_sha256'], snapshot['snapshot_sha256'])
        self.assertEqual(planned['presentation_time'], {'numerator': 17017, 'denominator': 30000})
        self.assertEqual(planned['source_time'], {'numerator': 5, 'denominator': 7})
        self.assertEqual(planned['local_index'], 0)
        self.assertEqual(planned['dependencies'], [snapshot['files'][0]['sha256']])
        # Valid JSON with the same decoded value still has different source bytes.
        path.write_text(json.dumps(json.loads(path.read_bytes())), encoding='utf-8')
        with self.assertRaises(Render3DError):
            make_frame_spec(value, 17, path, adapter=adapter)

    def test_frame_fingerprint_detects_changed_clock_camera_or_dependencies(self):
        source, adapter = self.sources()
        destination = self.root / 'snapshot'
        create_snapshot(source, destination, adapter)
        path = destination / 'snapshot.json'
        value = request(); value['snapshot'] = file_ref('snapshot/snapshot.json', path.read_bytes())
        planned = make_frame_spec(value, 17, path, adapter=adapter)
        for field, replacement in [('camera', 'different-camera'),
                                   ('source_time', {'numerator': 1, 'denominator': 1}),
                                   ('dependencies', ['f' * 64]),
                                   ('settings_sha256', 'e' * 64)]:
            changed = deepcopy(planned); changed[field] = replacement
            with self.subTest(field=field), self.assertRaises(Render3DError):
                validate(changed)

    def test_native_rig_snapshot_reloads_real_geometry_and_animation(self):
        rig = fixture(self.root)
        destination = self.root / 'native-snapshot'
        value = create_snapshot(rig.root, destination, NativeRigAdapter())
        self.assertTrue(value['fixture'])
        self.assertEqual(value['scope'], 'native-rig-preview')
        self.assertEqual(verify_snapshot(destination, NativeRigAdapter()), value)
        from rig_models import load_rig
        reopened = load_rig(destination)
        self.assertGreater(sum(len(mesh.positions) for mesh in reopened.geometry.meshes), 0)
        self.assertEqual(set(reopened.clips), {'bend', 'neutral'})

    def test_complete_result_requires_actual_verifier_identity_and_evidence(self):
        value = dict(schema_version=1, kind='render3d-result', job='job',
                     request_sha256='a' * 64, snapshot_sha256='b' * 64,
                     backend=backend(), status='complete', requested_frames=[0],
                     successful=[0], failed=[], skipped=[], reused=[],
                     images=[dict(frame=0, file=file_ref('frames/000000.png'),
                                  format='png', bit_depth=8, pass_name='combined', resolution=[320, 180])],
                     diagnostics=[], usage=dict(elapsed_seconds=None, peak_memory_bytes=None,
                                                output_bytes=None, measurement='unavailable'),
                     logs=[], manifest=file_ref('manifest.json'),
                     verification=dict(status='passed', verifier=None, evidence=None))
        with self.assertRaises(Render3DError):
            validate(value)
        value['verification'].update(
            verifier=dict(id='independent-file-inspector', version='1.0.0',
                          code_sha256='c' * 64, platform='synthetic'),
            evidence=file_ref('verification.json'))
        validate(value)
        value['images'].clear()
        with self.assertRaises(Render3DError):
            validate(value)

    def test_successful_result_categories_cannot_overlap(self):
        # Failed status does not waive complete, disjoint accounting.
        value = dict(schema_version=1, kind='render3d-result', job='job',
                     request_sha256='a' * 64, snapshot_sha256='b' * 64,
                     backend=backend(), status='failed', requested_frames=[0],
                     successful=[0], failed=[0], skipped=[], reused=[], images=[], diagnostics=[],
                     usage=dict(elapsed_seconds=None, peak_memory_bytes=None,
                                output_bytes=None, measurement='unavailable'),
                     logs=[], manifest=None,
                     verification=dict(status='not-run', verifier=None, evidence=None))
        with self.assertRaises(Render3DError):
            validate(value)

    def test_empty_job_cannot_be_presented_as_completed_render(self):
        job = dict(schema_version=1, kind='render3d-job', id='job',
                   request_sha256='a' * 64, snapshot_sha256='b' * 64,
                   backend=backend(), destination_owner='owner',
                   submitted_at='2026-10-25T00:00:00Z', updated_at='2026-10-25T00:00:00Z',
                   worker=None, heartbeat_at=None, attempt=0,
                   counts=dict(requested=0, queued=0, active=0, completed=0, failed=0),
                   state='complete', cancellation_requested=False,
                   manifest=file_ref('manifest.json'), error=None)
        with self.assertRaises(Render3DError):
            validate(job)

    def test_unavailable_backend_cannot_claim_successful_initialization(self):
        capability = dict(schema_version=1, kind='render3d-capabilities',
                          adapter=dict(id='adapter', version='1.0.0'), backend=None,
                          discovery='unavailable', initialization='passed', execution='not-run',
                          evidence=[], engines=[], features=[], diagnostics=[])
        with self.assertRaises(Render3DError):
            validate(capability)


if __name__ == '__main__':
    unittest.main()
