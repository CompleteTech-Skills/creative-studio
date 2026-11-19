"""Completed-process capture scheduling with installed FFmpeg/ffprobe fixtures.

The independent probe determines an actual numeric-slash byte boundary. During
public verification, a transparent Popen observer waits for each real child to
complete before returning that same child for ordinary transcript extraction.
This isolates completed-process truncation from live budget termination. No
process result, return code or output bytes are replaced. A separate bounded
wait kills and reaps a stuck fixture child before failing the test.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_silent_video as core

video = core.video
NATIVE_AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
if os.environ.get('HATCH_SILENT_MEDIA_TEST') == '1' and not NATIVE_AVAILABLE:
    raise RuntimeError('HATCH_SILENT_MEDIA_TEST requires installed FFmpeg and ffprobe')


@contextmanager
def completed_process_capture_schedule():
    """Wait for actual fixture children before the verifier extracts transcripts.

    The six-stream probe writes only a small transcript. Its original output
    handles remain unread by this observer; the verifier reads the real bytes.
    Returning the original Popen object preserves its actual identity and status.
    """
    actual_popen = subprocess.Popen

    def launch_and_wait(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            raise
        return child

    with patch.object(subprocess, 'Popen', new=launch_and_wait):
        yield


@unittest.skipUnless(NATIVE_AVAILABLE, 'installed FFmpeg/ffprobe unavailable')
class NativeDiagnosticBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='silent-native-boundary-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        image = self.root / 'synthetic-frame.png'
        self.artifact = self.root / 'synthetic-six-streams.mp4'
        Image.new('RGB', (16, 16), 'green').save(image)
        self.request = video.make_request(video.build_source(self.root, [image.name],
            {'numerator': 24, 'denominator': 1}, origin='synthetic'))
        generated = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-nostdin',
            '-loop', '1', '-framerate', '24', '-i', str(image),
            *(['-map', '0:v:0'] * 6), '-vf', 'setsar=1', '-frames:v', '1',
            '-threads', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.artifact)],
            capture_output=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr.decode(errors='replace'))
        self.assertEqual(generated.stderr, b'')
        entries = ('stream=' + ','.join(video.PROBE_STREAM_FIELDS) +
                   ':format=' + ','.join(video.PROBE_FORMAT_FIELDS))
        independent = subprocess.run([shutil.which('ffprobe'), *video.VERIFY_INPUT_OPTIONS,
            '-v', 'error', '-count_frames', '-show_entries', entries, '-of', 'json',
            str(self.artifact)], capture_output=True, timeout=30)
        self.assertEqual(independent.returncode, 0, independent.stderr.decode(errors='replace'))
        self.assertEqual(independent.stderr, b'')
        document = json.loads(independent.stdout)
        self.assertEqual(len(document['streams']), 6)
        self.assertTrue(all(stream['codec_type'] == 'video' for stream in document['streams']))
        # Choose a real avg_frame_rate value after the minimum supported log
        # limit, leaving its slash as the final retained byte. Do not assume
        # installed-tool whitespace, field ordering, or output size.
        matches = list(re.finditer(rb'"avg_frame_rate"\s*:\s*"24/', independent.stdout))
        candidates = [match.end() for match in matches if match.end() >= 1024]
        self.assertTrue(candidates, 'synthetic six-stream probe lacks a supported rational capture boundary')
        self.cap = candidates[0]
        self.assertLess(self.cap, len(independent.stdout))
        self.observed = independent.stdout[:self.cap]
        self.assertEqual(self.observed[-1:], b'/')
        self.assertNotIn(str(self.root).encode(), self.observed)
        self.request['limits']['max_log_bytes'] = self.cap
        self.request_path = self.root / 'synthetic-request.json'
        self.request_path.write_text(json.dumps(self.request))
        self.before = self.artifact.read_bytes()
        self.completed_cli = self.root / 'completed-process-cli.py'
        self.completed_cli.write_text(
            'import sys\n'
            'sys.path.insert(0, %r)\n' % str(Path(__file__).resolve().parent) +
            'from test_silent_video_diagnostic_boundaries_native import '
            'completed_process_capture_schedule, video\n'
            'with completed_process_capture_schedule():\n'
            '    video.main()\n')
        print('native rational boundary: total_probe_bytes=%d capture_bytes=%d observed_tail=%r' %
              (len(independent.stdout), self.cap, self.observed[-30:]), flush=True)

    def assert_native_capture_retained(self, report):
        self.assertIs(report['verified'], False)
        record = report['probe']
        print('completed-process fixture: observed_probe_exit_status=%r' %
              record['exit_status'], flush=True)
        self.assertIs(record['stdout_truncated'], True)
        self.assertEqual(record['exit_status'], 0)
        self.assertEqual(record['stderr'], '')
        self.assertEqual(record['command'][0], 'ffprobe')
        self.assertEqual(record['stdout'].encode(), self.observed)
        self.assertEqual(len(record['stdout'].encode()), self.cap)
        self.assertEqual(self.artifact.read_bytes(), self.before)

    def test_python_preserves_actual_truncated_probe_rational(self):
        with completed_process_capture_schedule(), self.assertRaises(ValueError) as failed:
            video.verify_export(self.artifact, self.request)
        self.assert_native_capture_retained(failed.exception.evidence)

    def test_cli_preserves_actual_truncated_probe_rational(self):
        result = subprocess.run([sys.executable, str(self.completed_cli),
            'verify-export', str(self.artifact), '--request', str(self.request_path),
            '--require-silent'], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b'')
        self.assert_native_capture_retained(json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
