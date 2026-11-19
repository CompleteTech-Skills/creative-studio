"""Bind current raw receipts to real synthetic media and retain full-probe compatibility.

The historical-format control executes the installed full-probe command now; it
is compatibility evidence, not a claim of execution by an older runtime.
"""
from copy import deepcopy
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_silent_video_native as native

video = native.video


@unittest.skipUnless(native.MEDIA_AVAILABLE, 'FFmpeg/ffprobe integration tools unavailable')
class CurrentReceiptNative(unittest.TestCase):
    # Reuse the existing six-frame synthetic PNG fixture without inheriting its
    # unrelated contract tests into this focused native regression module.
    setUp = native.core.SilentContracts.setUp

    def current_receipt(self):
        artifact = self.root / 'synthetic-six-frames.mp4'
        video.encode(self.request, self.root, artifact)
        receipt = video.verify_export(artifact, self.request, root=self.root)
        self.assertIs(receipt['verified'], True)
        self.assertEqual(receipt['frame_count'], 6)
        self.assertEqual(receipt['profile'], 'h264-mp4')
        self.assertIn('-show_entries', receipt['probe']['command'])
        raw = json.loads(receipt['probe']['stdout'])
        self.assertEqual(len(raw['streams']), 1)
        self.assertEqual(raw['streams'][0]['codec_name'], 'h264')
        self.assertNotIn('bit_rate', raw['streams'][0])
        return artifact, receipt, raw

    def assert_only_probe_stdout_changed(self, original, changed):
        restored = deepcopy(changed)
        restored['probe']['stdout'] = original['probe']['stdout']
        self.assertEqual(restored, original)
        self.assertNotEqual(changed['probe']['stdout'], original['probe']['stdout'])

    def test_untouched_current_receipt_verifies_against_actual_movie(self):
        artifact, receipt, raw = self.current_receipt()
        accepted = video.validate_receipt(receipt, artifact, self.request, root=self.root)
        self.assertIs(accepted['verified'], True)
        self.assertEqual(accepted['artifact_sha256'], video.file_hash(artifact))
        self.assertEqual(json.loads(accepted['probe']['stdout']), raw)

    def test_current_and_full_probe_refuse_identical_repeated_members(self):
        artifact, receipt, _ = self.current_receipt()
        command = [shutil.which('ffprobe'), '-v', 'error', '-count_frames',
                   '-show_streams', '-show_format', '-of', 'json', str(artifact)]
        full = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(full.stderr, '')
        historical_format = deepcopy(receipt)
        historical_format['probe'] = dict(command=command, exit_status=full.returncode,
                                          stdout=full.stdout, stderr=full.stderr)
        for kind, original in (('current', receipt), ('historical-format', historical_format)):
            with self.subTest(kind=kind):
                self.assertIs(video.validate_receipt(original, artifact, self.request,
                                                     root=self.root)['verified'], True)
                changed = deepcopy(original)
                changed['probe']['stdout'], count = re.subn(
                    r'("width"\s*:\s*\d+)(\s*,)', r'\1, \1\2',
                    original['probe']['stdout'], count=1)
                self.assertEqual(count, 1)
                self.assert_only_probe_stdout_changed(original, changed)
                self.assertEqual(json.loads(changed['probe']['stdout']),
                                 json.loads(original['probe']['stdout']))
                with self.assertRaises(ValueError):
                    video.validate_receipt(changed, artifact, self.request, root=self.root)

    def test_current_receipt_rejects_added_unqueried_stream_bitrate(self):
        artifact, receipt, raw = self.current_receipt()
        forged = deepcopy(receipt)
        raw['streams'][0]['bit_rate'] = '987654321'
        forged['probe']['stdout'] = json.dumps(raw)
        self.assert_only_probe_stdout_changed(receipt, forged)
        with self.assertRaises(ValueError):
            video.validate_receipt(forged, artifact, self.request, root=self.root)

    def test_current_receipt_rejects_changed_observed_stream_groups(self):
        artifact, receipt, raw = self.current_receipt()
        # ffprobe versions differ in whether this empty top-level field is
        # emitted. Preserve the original empty-field assertion when present;
        # inserting a group must reject even when the tool omitted the field.
        if 'stream_groups' in raw:
            self.assertEqual(raw['stream_groups'], [])
        forged = deepcopy(receipt)
        raw['stream_groups'] = [{'index': 0}]
        forged['probe']['stdout'] = json.dumps(raw)
        self.assert_only_probe_stdout_changed(receipt, forged)
        with self.assertRaises(ValueError):
            video.validate_receipt(forged, artifact, self.request, root=self.root)

    def test_current_receipt_binds_empty_stream_group_field_presence(self):
        artifact, receipt, raw = self.current_receipt()
        if 'stream_groups' in raw:
            self.assertEqual(raw['stream_groups'], [])
            del raw['stream_groups']
        else:
            raw['stream_groups'] = []
        forged = deepcopy(receipt)
        forged['probe']['stdout'] = json.dumps(raw)
        self.assert_only_probe_stdout_changed(receipt, forged)
        with self.assertRaises(ValueError):
            video.validate_receipt(forged, artifact, self.request, root=self.root)

    def test_actual_full_probe_command_retains_historical_format_compatibility(self):
        artifact, receipt, _ = self.current_receipt()
        # This is the existing native historical-format procedure: record an
        # actual full ffprobe invocation and its actual output for these bytes.
        command = [shutil.which('ffprobe'), '-v', 'error', '-count_frames',
                   '-show_streams', '-show_format', '-of', 'json', str(artifact)]
        full = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(full.stderr, '')
        full_raw = json.loads(full.stdout)
        self.assertIn('tags', full_raw['format'])
        self.assertIn('bit_rate', full_raw['streams'][0])
        historical_format = deepcopy(receipt)
        historical_format['probe'] = dict(command=command, exit_status=full.returncode,
                                          stdout=full.stdout, stderr=full.stderr)
        self.assertNotEqual(historical_format['probe']['stdout'], receipt['probe']['stdout'])
        accepted = video.validate_receipt(historical_format, artifact, self.request, root=self.root)
        self.assertIs(accepted['verified'], True)
        self.assertEqual(accepted['artifact_sha256'], video.file_hash(artifact))
        # The accepted result is freshly verified current evidence, so the
        # historical full-probe extras are not carried into the returned record.
        self.assertNotIn('bit_rate', json.loads(accepted['probe']['stdout'])['streams'][0])


if __name__ == '__main__':
    unittest.main()
