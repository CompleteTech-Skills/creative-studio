"""Bind actual historical-format full-probe receipts to synthetic media bytes.

These tests execute the installed full ffprobe protocol now, with show_streams
and show_format and without show_entries. They exercise compatibility with the
historical receipt format; they do not claim execution by an older runtime.
No process, output capture, or clock is mocked.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_silent_video_native as native

video = native.video


def observed_leaves(value, path=()):
    """Include empty observed containers as well as nested scalar facts."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from observed_leaves(child, (*path, key))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from observed_leaves(child, (*path, index))
    else:
        yield path, value


def changed_observation(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return value + '-changed-observation'
    if isinstance(value, list):
        return [{'changed_observation': True}]
    if isinstance(value, dict):
        return {'changed_observation': True}
    return 'changed-observation'


@unittest.skipUnless(native.MEDIA_AVAILABLE, 'FFmpeg/ffprobe integration tools unavailable')
class HistoricalReceiptNative(unittest.TestCase):
    setUp = native.core.SilentContracts.setUp

    def historical_receipt(self):
        artifact = self.root / 'historical-format-synthetic-six-frames.mp4'
        current = video.encode(self.request, self.root, artifact)
        self.assertIs(current['verified'], True)
        self.assertEqual(current['frame_count'], 6)
        self.assertIn('-show_entries', current['probe']['command'])
        current_raw = json.loads(current['probe']['stdout'])
        self.assertNotIn('bit_rate', current_raw['streams'][0])
        self.assertNotIn('filename', current_raw['format'])

        command = [shutil.which('ffprobe'), '-v', 'error', '-count_frames',
                   '-show_streams', '-show_format', '-of', 'json', str(artifact)]
        self.assertNotIn('-show_entries', command)
        full = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(full.stderr, '')
        raw = json.loads(full.stdout)
        self.assertEqual(len(raw['streams']), 1)
        self.assertEqual(raw['streams'][0]['codec_name'], 'h264')
        self.assertGreater(int(raw['streams'][0]['bit_rate']), 0)
        self.assertIn('disposition', raw['streams'][0])
        self.assertIn('tags', raw['streams'][0])
        self.assertIn('tags', raw['format'])
        self.assertEqual(raw['format']['filename'], str(artifact))
        historical = deepcopy(current)
        historical['probe'] = dict(command=command, exit_status=full.returncode,
                                   stdout=full.stdout, stderr=full.stderr)
        self.assertEqual(historical['probe']['stdout'], full.stdout)
        self.inputs = (artifact.read_bytes(), deepcopy(self.request), deepcopy(self.source),
                       {name: (self.root / name).read_bytes() for name in self.paths})
        return artifact, historical, raw, current_raw

    def assert_inputs_unchanged(self, artifact):
        artifact_bytes, request, source, frames = self.inputs
        self.assertEqual(artifact.read_bytes(), artifact_bytes)
        self.assertEqual(self.request, request)
        self.assertEqual(self.source, source)
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.paths}, frames)

    def changed_receipt(self, original, raw, path, value, *, remove=False):
        changed_raw = deepcopy(raw)
        parent = changed_raw
        for key in path[:-1]:
            parent = parent[key]
        if remove:
            del parent[path[-1]]
        else:
            parent[path[-1]] = value
        changed = deepcopy(original)
        changed['probe']['stdout'] = json.dumps(changed_raw)
        restored = deepcopy(changed)
        restored['probe']['stdout'] = original['probe']['stdout']
        self.assertEqual(restored, original)
        self.assertNotEqual(changed_raw, raw)
        # Restore just the intended observed field, proving that the JSON
        # mutation did not also alter another observation or its presence.
        restored_raw = deepcopy(changed_raw)
        parent = restored_raw
        original_parent = raw
        for key in path[:-1]:
            parent, original_parent = parent[key], original_parent[key]
        if path[-1] in original_parent:
            parent[path[-1]] = original_parent[path[-1]]
        else:
            del parent[path[-1]]
        self.assertEqual(restored_raw, raw)
        return changed

    def assert_rejected(self, receipt, artifact):
        self.assert_inputs_unchanged(artifact)
        try:
            with self.assertRaises(ValueError):
                video.validate_receipt(receipt, artifact, self.request, root=self.root)
        finally:
            self.assert_inputs_unchanged(artifact)

    def assert_current_evidence(self, receipt, artifact, current_raw):
        accepted = video.validate_receipt(receipt, artifact, self.request, root=self.root)
        self.assertIs(accepted['verified'], True)
        self.assertEqual(accepted['artifact_sha256'], video.file_hash(artifact))
        self.assertEqual(accepted['source_manifest_sha256'], receipt['source_manifest_sha256'])
        self.assertEqual(accepted['request_sha256'], receipt['request_sha256'])
        self.assertIn('-show_entries', accepted['probe']['command'])
        self.assertEqual(json.loads(accepted['probe']['stdout']), current_raw)
        self.assertNotIn('bit_rate', current_raw['streams'][0])
        self.assertNotIn('filename', current_raw['format'])
        self.assert_inputs_unchanged(artifact)

    def test_untouched_actual_historical_format_receipt_returns_current_evidence(self):
        artifact, receipt, _, current_raw = self.historical_receipt()
        self.assert_current_evidence(receipt, artifact, current_raw)

    def test_only_recorded_format_filename_may_differ(self):
        artifact, receipt, raw, current_raw = self.historical_receipt()
        changed = self.changed_receipt(receipt, raw, ('format', 'filename'),
                                       'previous-verification-run/artifact.mp4')
        self.assert_current_evidence(changed, artifact, current_raw)

    def test_changed_observed_stream_bitrate_is_rejected(self):
        artifact, receipt, raw, _ = self.historical_receipt()
        path = ('streams', 0, 'bit_rate')
        changed = self.changed_receipt(receipt, raw, path,
                                       str(int(raw['streams'][0]['bit_rate']) + 1))
        self.assert_rejected(changed, artifact)

    def test_every_other_original_observation_is_bound(self):
        artifact, receipt, raw, _ = self.historical_receipt()
        excluded = {('format', 'filename'), ('streams', 0, 'bit_rate')}
        paths = [path for path, _ in observed_leaves(raw) if path not in excluded]
        # These full-probe fields are absent from current field-limited stdout;
        # nested tags/disposition and container facts must still be bound.
        self.assertIn(('streams', 0, 'codec_long_name'), paths)
        self.assertIn(('streams', 0, 'disposition', 'default'), paths)
        self.assertIn(('format', 'bit_rate'), paths)
        self.assertTrue(any(path[:2] == ('format', 'tags') for path in paths))
        for path, value in observed_leaves(raw):
            if path in excluded:
                continue
            with self.subTest(observation=path):
                changed = self.changed_receipt(receipt, raw, path, changed_observation(value))
                self.assert_rejected(changed, artifact)

    def test_removed_observed_metadata_is_rejected(self):
        artifact, receipt, raw, _ = self.historical_receipt()
        paths = [('streams', 0, 'bit_rate'), ('streams', 0, 'tags'),
                 ('streams', 0, 'disposition'), ('format', 'tags')]
        # Some ffprobe versions also observe an empty stream_groups inventory.
        # Its presence is an observation even though it contains no members.
        if 'stream_groups' in raw:
            paths.append(('stream_groups',))
        for path in paths:
            with self.subTest(observation=path):
                changed = self.changed_receipt(receipt, raw, path, None, remove=True)
                self.assert_rejected(changed, artifact)

    def test_added_unobserved_metadata_is_rejected(self):
        artifact, receipt, raw, _ = self.historical_receipt()
        for path in [('unobserved_metadata',), ('streams', 0, 'unobserved_metadata'),
                     ('format', 'unobserved_metadata')]:
            with self.subTest(observation=path):
                changed = self.changed_receipt(receipt, raw, path, 'invented-observation')
                self.assert_rejected(changed, artifact)


if __name__ == '__main__':
    unittest.main()
