"""Portable public-error contracts; simulated probe records are explicitly fixtures."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_silent_video as core

video = core.video


class PublicErrors(unittest.TestCase):
    setUp = core.SilentContracts.setUp

    def test_stage_records_use_logical_tools_without_private_executable_names(self):
        for stage, name in (('probe', 'ffprobe'), ('timeline', 'ffprobe'),
                            ('decode', 'ffmpeg'), ('pixel_decode', 'ffmpeg')):
            command = [str(self.root/'SYNTHETIC_TOOL_LABEL'), '-version']
            raw = dict(command=command, exit_status=1, stdout='', stderr=command[0])
            with self.subTest(stage=stage):
                evidence, message = video._failure_diagnostics({stage: raw}, command[0], 1024)
                self.assertEqual(evidence[stage]['command'][0], name)
                self.assertEqual(evidence[stage]['stderr'], name)
                self.assertEqual(message, name)
                self.assertNotIn('SYNTHETIC_TOOL_LABEL', json.dumps(evidence))

    def test_custom_tool_launch_refusal_keeps_logical_command_and_errno(self):
        import errno
        command = [str(self.root/'SYNTHETIC_TOOL_LABEL'), '-version']
        for name in ('ffprobe', 'ffmpeg'):
            with self.subTest(tool=name), patch.object(video.subprocess, 'Popen',
                    side_effect=OSError(errno.EACCES, 'synthetic refusal', command[0])):
                with self.assertRaises(ValueError) as stopped:
                    video._run(command, video.DEFAULT_LIMITS, tool_role=name)
            record = stopped.exception.evidence['command_failure']
            self.assertEqual(record['command'][0], name)
            self.assertIs(record['started'], False)
            self.assertEqual(record['launch_error'], {'errno': errno.EACCES})
            self.assertNotIn('exit_status', record)
            self.assertNotIn('SYNTHETIC_TOOL_LABEL', str(stopped.exception)+json.dumps(record))

    def test_cli_missing_malformed_or_directory_request_is_private(self):
        artifact = self.root/'SYNTHETIC_CALLER_ARTIFACT.mp4'
        artifact.write_bytes(b'synthetic unread media')
        for kind in ('missing', 'malformed', 'directory'):
            request = self.root/('SYNTHETIC_REQUEST_LABEL-'+kind)
            if kind == 'malformed':
                request.write_text('{"private":', encoding='utf-8')
            elif kind == 'directory':
                request.mkdir()
            with self.subTest(kind=kind):
                process = subprocess.run([sys.executable, str(Path(video.__file__).resolve()),
                    'verify-export', str(artifact), '--request', str(request), '--require-silent'],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(process.returncode, 1)
                result = json.loads(process.stdout)
                self.assertIs(result['verified'], False)
                self.assertIn('request', result['error'])
                self.assertEqual(process.stderr, '')
                for private in (str(self.root), request.name, artifact.name):
                    self.assertNotIn(private, process.stdout)
                for unobserved in ('artifact_sha256', 'request_sha256', 'source_manifest_sha256',
                                   'probe', 'decode', 'frame_count'):
                    self.assertNotIn(unobserved, result)

    def test_invalid_request_values_do_not_escape_public_verifier(self):
        request = dict(self.request, profile='SYNTHETIC_PRIVATE_REQUEST_VALUE')
        with patch.object(video, '_run') as command, self.assertRaises(ValueError) as stopped:
            video.verify_export(self.root/'SYNTHETIC_CALLER_ARTIFACT', request)
        command.assert_not_called()
        self.assertNotIn('SYNTHETIC_', str(stopped.exception))
        self.assertEqual(getattr(stopped.exception, 'report', {}), {})

    def test_simulated_probe_dimensions_require_integer_types(self):
        artifact = self.root/'dimension-fixture.mp4'
        artifact.write_bytes(core.SIMULATED_MP4_FTYP + core.mp4_box(b'free', b'portable simulated media only'))
        for dimension in ('width', 'height'):
            for invalid in (float(self.source[dimension]), str(self.source[dimension]), True):
                records = core.SilentContracts._simulated_verification_records(self)
                raw = json.loads(records[0]['stdout'])
                raw['streams'][0][dimension] = invalid
                records[0]['stdout'] = json.dumps(raw)
                with self.subTest(dimension=dimension, value=invalid), \
                        patch.object(video, '_binary', return_value=sys.executable), \
                        patch.object(video, '_run', side_effect=records) as command:
                    with self.assertRaises(ValueError) as stopped:
                        video.verify_export(artifact, self.request)
                self.assertEqual(command.call_count, 1)
                evidence = stopped.exception.evidence
                self.assertEqual(evidence['probe']['stdout'], records[0]['stdout'])
                self.assertEqual(evidence['probe']['exit_status'], 0)
                self.assertEqual(evidence['artifact_sha256'], hashlib.sha256(artifact.read_bytes()).hexdigest())
        with patch.object(video, '_binary', return_value=sys.executable), \
                patch.object(video, '_run', side_effect=core.SilentContracts._simulated_verification_records(self)):
            self.assertIs(video.verify_export(artifact, self.request)['verified'], True)


if __name__ == '__main__':
    unittest.main()
