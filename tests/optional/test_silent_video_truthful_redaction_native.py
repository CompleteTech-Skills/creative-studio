"""Real installed codec collision regression using synthetic six-frame media."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_silent_video as core

video = core.video


@unittest.skipUnless(core.MEDIA_AVAILABLE, 'FFmpeg/ffprobe integration tools unavailable')
class NativeTruthfulRedaction(unittest.TestCase):
    def setUp(self):
        core.SilentContracts.setUp(self)

    def test_actual_probe_named_video_preserves_raw_evidence_in_python_and_cli(self):
        installed = Path(shutil.which('ffprobe')).resolve()
        private = self.root / 'SYNTHETIC_PRIVATE_TOOLS'
        private.mkdir()
        # This is the installed binary, byte for byte, not a mocked probe or
        # wrapper. Its physical name collides with an actual codec_type value.
        physical = private / 'video'
        shutil.copy2(installed, physical)
        self.assertEqual(video.file_hash(physical), video.file_hash(installed))
        executable = private / ('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
        executable.symlink_to(physical.name)
        artifact = self.root / 'six-frames.mp4'
        video.encode(self.request, self.root, artifact)
        accepted = video.verify_export(artifact, self.request, root=self.root, ffprobe=executable)
        self.assertIs(accepted['verified'], True)
        self.assertEqual(accepted['video_stream_count'], 1)
        before = artifact.read_bytes()

        entries = ('stream=' + ','.join(video.PROBE_STREAM_FIELDS) +
                   ':format=' + ','.join(video.PROBE_FORMAT_FIELDS))
        independent = subprocess.run([str(executable), *video.VERIFY_INPUT_OPTIONS,
            '-v', 'error', '-count_frames', '-show_entries', entries, '-of', 'json', str(artifact)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(independent.returncode, 0, independent.stderr)
        self.assertEqual(json.loads(independent.stdout)['streams'][0]['codec_type'], 'video')
        self.assertEqual(independent.stderr, '')

        # The unchanged video is real and valid. A new, hash-bound source
        # request deliberately disagrees at a representative pixel sample.
        Image.new('RGB', (64, 48), (255, 0, 255)).save(self.root / self.paths[0])
        source = video.build_source(self.root, self.paths, self.source['fps'], origin='synthetic')
        request = video.make_request(source)
        with self.assertRaisesRegex(ValueError, 'representative pixels differ') as failed:
            video.verify_export(artifact, request, root=self.root, ffprobe=executable)
        request_path = self.root / 'mismatched-source-request.json'
        request_path.write_text(json.dumps(request))
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()),
            'verify-export', str(artifact), '--request', str(request_path), '--root', str(self.root),
            '--require-silent'], env=dict(os.environ, PATH=str(private) + os.pathsep + os.environ['PATH']),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        cli = json.loads(result.stdout)
        self.assertIn('representative pixels differ', cli['error'])
        for interface, report in (('python', failed.exception.evidence), ('cli', cli)):
            with self.subTest(interface=interface):
                self.assertIs(report['verified'], False)
                self.assertEqual(report['probe']['command'][0], 'ffprobe')
                self.assertEqual(report['probe']['exit_status'], independent.returncode)
                self.assertEqual(report['probe']['stdout'], independent.stdout)
                self.assertEqual(report['probe']['stderr'], independent.stderr)
                self.assertEqual(json.loads(report['probe']['stdout'])['streams'][0]['codec_type'], 'video')
                for stage in ('timeline', 'decode', 'pixel_decode'):
                    self.assertEqual(report[stage]['exit_status'], 0)
                    self.assertTrue(report[stage]['command'])
                self.assertEqual(report['pixel_decode']['command'][0], 'ffmpeg')
                self.assertEqual(report['video_stream_count'], 1)
                self.assertEqual(report['audio_stream_count'], 0)
                self.assertEqual(report['other_stream_count'], 0)
                self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))
                self.assertEqual(report['request_sha256'], video._digest(request))
                self.assertEqual(report['source_manifest_sha256'], video._digest(source))
                self.assertNotIn(private.name, json.dumps(report))
        self.assertEqual(artifact.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
