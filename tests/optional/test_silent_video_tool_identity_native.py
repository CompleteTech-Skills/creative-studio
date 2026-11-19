"""Actual copied ffprobe under a synthetic filename and PATH symlink."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_silent_video as core

video = core.video


@unittest.skipUnless(os.name == 'posix' and shutil.which('ffprobe') and shutil.which('ffmpeg'),
                     'Actual POSIX executable-copy and PATH-symlink control')
class ToolIdentityPrivacy(unittest.TestCase):
    setUp = core.SilentContracts.setUp

    def test_actual_override_and_path_symlink_export_logical_probe_identity(self):
        private = self.root/'SYNTHETIC_TOOL_LABEL'
        shutil.copy2(shutil.which('ffprobe'), private)
        artifact = self.root/'synthetic-corrupt.mp4'
        artifact.write_bytes(b'synthetic corrupt MP4')
        with self.assertRaises(ValueError) as stopped:
            video.verify_export(artifact, self.request, ffprobe=str(private))
        evidence = stopped.exception.evidence
        for stage in ('probe', 'command_failure'):
            self.assertEqual(evidence[stage]['command'][0], 'ffprobe')
            self.assertEqual(evidence[stage]['exit_status'], 1)
        self.assertNotIn(private.name, str(stopped.exception)+json.dumps(evidence))
        self.assertEqual(evidence['artifact_sha256'], video.file_hash(artifact))
        directory = self.root/'tools'
        directory.mkdir()
        (directory/'ffprobe').symlink_to(private)
        request = self.root/'request.json'
        request.write_text(json.dumps(self.request), encoding='utf-8')
        process = subprocess.run([sys.executable, str(Path(video.__file__).resolve()),
            'verify-export', str(artifact), '--request', str(request), '--require-silent'],
            env=dict(os.environ, PATH=str(directory)+os.pathsep+os.environ.get('PATH', '')),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 1)
        self.assertNotIn(private.name, process.stdout+process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['probe']['command'][0], 'ffprobe')
        self.assertEqual(result['probe']['exit_status'], 1)
        self.assertEqual(result['artifact_sha256'], video.file_hash(artifact))


if __name__ == '__main__':
    unittest.main()
