"""Real mode-000 read refusals after successful synthetic H.264 encoding.

No descriptor or subprocess mocks are used here. Permission enforcement is
measured, so privileged accounts and filesystems which ignore chmod skip with
an explicit reason instead of being claimed as native refusal evidence.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_silent_video_read_privacy as privacy

video = privacy.video
MEDIA_AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and
                       video.secure_publication_supported())
if os.environ.get('HATCH_SILENT_MEDIA_TEST') == '1' and not MEDIA_AVAILABLE:
    raise RuntimeError('HATCH_SILENT_MEDIA_TEST requires real FFmpeg and ffprobe')


@unittest.skipUnless(MEDIA_AVAILABLE, 'Real FFmpeg/ffprobe and secure encoding unavailable')
class NativeReadPrivacy(privacy.ReadPrivacyAssertions, unittest.TestCase):
    def make_unreadable(self, path):
        mode = path.stat().st_mode & 0o777
        self.addCleanup(path.chmod, mode)
        try:
            path.chmod(0)
        except (OSError, NotImplementedError) as error:
            self.skipTest('Filesystem cannot apply mode 000: ' + type(error).__name__)
        try:
            with path.open('rb') as stream:
                stream.read(1)
        except PermissionError:
            return
        self.skipTest('Filesystem/account does not enforce mode-000 read refusal')

    def real_cli_failure(self):
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()),
            'verify-export', str(self.artifact), '--request', str(self.request_path),
            '--root', str(self.root), '--require-silent'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assert_private(result.stdout, result.stderr)
        return json.loads(result.stdout)

    def encoded_control(self):
        receipt = video.encode(self.request, self.root, self.artifact)
        self.assertIs(receipt['verified'], True)
        self.assertEqual(receipt['frame_count'], len(self.paths))
        self.assertEqual(receipt['audio_stream_count'], 0)
        for stage in ('probe', 'timeline', 'decode'):
            self.assertEqual(receipt[stage]['exit_status'], 0)
        return self.artifact.read_bytes()

    def test_actual_source_mode000_preserves_real_completed_stages_and_snapshot(self):
        original = self.encoded_control()
        # Only revoke source access after a successful actual encoding/control.
        self.make_unreadable(self.root / self.paths[2])
        for interface in (self.api_failure, self.real_cli_failure):
            with self.subTest(interface=interface.__name__):
                evidence = interface()
                self.assert_completed_snapshot(evidence, original)
        self.assertEqual(self.artifact.read_bytes(), original)

    def test_actual_artifact_mode000_fails_before_snapshot_without_invented_facts(self):
        self.encoded_control()
        self.make_unreadable(self.artifact)
        for interface in (self.api_failure, self.real_cli_failure):
            with self.subTest(interface=interface.__name__):
                self.assert_before_snapshot(interface())


if __name__ == '__main__':
    unittest.main()
