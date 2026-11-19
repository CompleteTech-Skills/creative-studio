"""Actual installed ffprobe privacy control; all media and paths are synthetic."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


@unittest.skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'Native media tools are optional')
class NativeLoaderPrivacy(unittest.TestCase):
    def test_inherited_instrumentation_is_absent_from_python_and_cli_evidence(self):
        root = Path(__file__).resolve().parents[2]
        environment = dict(os.environ, PYTHONPATH=str(root / 'scripts'))
        result = subprocess.run([sys.executable,
            str(root / 'tests/independent/test_silent_video_loader_privacy.py')],
            cwd=root, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
