"""Real installed-binary diagnostic privacy with synthetic media and env paths."""
import json
import os
from pathlib import Path
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


@unittest.skipUnless(core.MEDIA_AVAILABLE, 'FFmpeg/ffprobe integration tools unavailable')
class NativeDiagnosticEnvironment(unittest.TestCase):
    def setUp(self):
        core.SilentContracts.setUp(self)
        self.marker = 'SYNTHETIC_PRIVATE_NATIVE_DIAGNOSTIC'
        self.private = self.root / self.marker
        self.private.mkdir()
        # The nonexistent parent makes report creation fail without writing a
        # report, while the unmodified native tool still discloses its name.
        self.report_file = self.root / ('REPORT_' + self.marker) / 'fixture.log'

    def environment(self, mode):
        env = dict(os.environ)
        for name in ('FFREPORT', 'AV_LOG_FORCE_COLOR', 'AV_LOG_FORCE_NOCOLOR'):
            env.pop(name, None)
        env.update(TMPDIR=str(self.private), TEMP=str(self.private), TMP=str(self.private))
        env['AV_LOG_FORCE_COLOR' if 'color' in mode else 'AV_LOG_FORCE_NOCOLOR'] = '1'
        if 'report' in mode:
            # FFREPORT uses backslash escapes for its colon-separated options.
            report_path = str(self.report_file).replace('\\', '\\\\').replace(':', '\\:')
            env['FFREPORT'] = 'file=' + report_path + ':level=16'
        return env

    def independent_probe(self, artifact, env):
        entries = ('stream=' + ','.join(video.PROBE_STREAM_FIELDS) +
                   ':format=' + ','.join(video.PROBE_FORMAT_FIELDS))
        return subprocess.run([shutil.which('ffprobe'), *video.VERIFY_INPUT_OPTIONS,
            '-v', 'error', '-count_frames', '-show_entries', entries, '-of', 'json', str(artifact)],
            env=env, capture_output=True, text=True, timeout=30)

    def reports(self, artifact, request, env, *, root=None):
        with patch.dict(os.environ, env, clear=True), patch.object(tempfile, 'tempdir', str(self.private)):
            before = dict(os.environ)
            with self.assertRaises(ValueError) as failed:
                video.verify_export(artifact, request, root=root)
            self.assertTrue(dict(os.environ) == before, 'parent environment changed')
        request_path = self.root / 'environment-request.json'
        request_path.write_text(json.dumps(request))
        command = [sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
                   str(artifact), '--request', str(request_path), '--require-silent']
        if root is not None:
            command += ['--root', str(root)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        return {'python': {**failed.exception.evidence, 'error': str(failed.exception)},
                'cli': json.loads(result.stdout)}

    def assert_identity_and_privacy(self, report, artifact, request):
        self.assertIs(report['verified'], False)
        self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))
        self.assertEqual(report['request_sha256'], video._digest(request))
        self.assertEqual(report['source_manifest_sha256'], video._digest(request['source']))
        public = json.dumps(report)
        self.assertNotIn(self.marker, public)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn(str(self.report_file), public)
        self.assertNotIn('\x1b', report['probe']['stderr'])
        self.assertFalse(self.report_file.exists())
        self.assertEqual(report['probe']['command'][0], 'ffprobe')
        for field in ('stdout', 'stderr'):
            self.assertIsInstance(report['probe'][field], str)
            self.assertLessEqual(len(report['probe'][field].encode()), request['limits']['max_log_bytes'])

    def test_actual_corrupt_media_color_and_report_failures_remain_private_in_python_and_cli(self):
        artifact = self.root / 'corrupt.mp4'
        artifact.write_bytes(b'ordinary synthetic corrupt media')
        control = self.independent_probe(artifact, self.environment('plain'))
        self.assertEqual(control.returncode, 1)
        self.assertTrue(control.stderr)
        for mode in ('plain', 'color', 'report', 'color-report'):
            env = self.environment(mode)
            native = self.independent_probe(artifact, env)
            self.assertEqual(native.returncode, control.returncode)
            self.assertEqual(native.stdout, control.stdout)
            if 'color' in mode:
                self.assertIn('\x1b[', native.stderr)
            if 'report' in mode:
                self.assertIn(str(self.report_file), native.stderr)
            reports = self.reports(artifact, self.request, env)
            for interface, report in reports.items():
                with self.subTest(mode=mode, interface=interface):
                    self.assert_identity_and_privacy(report, artifact, self.request)
                    probe = report['probe']
                    self.assertEqual(probe['exit_status'], native.returncode)
                    self.assertEqual(probe['stdout'], native.stdout)
                    self.assertIn('Invalid data found when processing input', probe['stderr'])
                    self.assertEqual(report['command_failure'], probe)
                    for field in ('video_stream_count', 'audio_stream_count', 'other_stream_count'):
                        self.assertNotIn(field, report)

    def test_actual_successful_probe_keeps_raw_facts_under_color_and_report_environment(self):
        artifact = self.root / 'six-frames.mp4'
        video.encode(self.request, self.root, artifact)
        expected = self.independent_probe(artifact, self.environment('plain'))
        self.assertEqual(expected.returncode, 0, expected.stderr)
        self.assertEqual(expected.stderr, '')
        self.assertEqual(json.loads(expected.stdout)['streams'][0]['codec_type'], 'video')
        # Retain successful raw probe evidence after a later real pixel mismatch.
        Image.new('RGB', (64, 48), (255, 0, 255)).save(self.root / self.paths[0])
        source = video.build_source(self.root, self.paths, self.source['fps'], origin='synthetic')
        request = video.make_request(source)
        for interface, report in self.reports(artifact, request,
                self.environment('color-report'), root=self.root).items():
            with self.subTest(interface=interface):
                self.assert_identity_and_privacy(report, artifact, request)
                self.assertIn('representative pixels differ', report['error'])
                self.assertEqual(report['probe']['exit_status'], expected.returncode)
                self.assertEqual(report['probe']['stdout'], expected.stdout)
                self.assertEqual(report['probe']['stderr'], expected.stderr)
                self.assertEqual(report['video_stream_count'], 1)
                self.assertEqual(report['audio_stream_count'], 0)
                self.assertEqual(report['other_stream_count'], 0)
                for stage in ('timeline', 'decode', 'pixel_decode'):
                    self.assertEqual(report[stage]['exit_status'], 0)
                self.assertNotIn('command_failure', report)


if __name__ == '__main__':
    unittest.main()
