"""Portable transcript contracts; simulated records are not native evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_silent_video as core

video = core.video


class TruthfulRedaction(unittest.TestCase):
    def test_synthetic_path_flavors_redact_without_host_filesystem_semantics(self):
        # These are lexical transcript fixtures, not native Windows execution.
        # Rooted Windows paths lack a drive but still identify private paths.
        for prefix, separator in (('/', '/'), ('\\', '\\'), ('Z:\\', '\\'),
                                  ('Z:/', '/'), ('\\\\synthetic-host\\share\\', '\\')):
            executable = prefix + 'synthetic-private-tools' + separator + 'video'
            artifact = prefix + 'synthetic-private-work' + separator + 'artifact.mp4'
            unrelated = prefix + 'synthetic-private-work-archive' + separator + 'artifact.mp4'
            payload = '{ "streams": [{"codec_type": "video", "codec_name": "h264"}] }\n'
            for spelling in (artifact, artifact.replace('\\', '/'), json.dumps(artifact)[1:-1]):
                raw = dict(command=[executable, '--literal=' + artifact, artifact],
                           exit_status=1, stdout=payload + unrelated,
                           stderr='video: actual diagnostic\n' + spelling + ': input error\n' + unrelated)
                with self.subTest(prefix=prefix, spelling=spelling):
                    result, message = video._failure_diagnostics({'probe': raw}, artifact + ': failed', 4096)
                    self.assertEqual(result['probe']['command'],
                                     ['ffprobe', '--literal=<media-workdir>/artifact.mp4',
                                      '<media-workdir>/artifact.mp4'])
                    self.assertEqual(result['probe']['stdout'], raw['stdout'])
                    self.assertEqual(result['probe']['stderr'],
                                     'ffprobe: actual diagnostic\n<media-workdir>/artifact.mp4: input error\n' + unrelated)
                    self.assertEqual(message, '<media-workdir>/artifact.mp4: failed')
                    self.assertEqual(result['probe']['exit_status'], 1)
                    self.assertEqual(raw['command'][0], executable)

    def test_executable_identity_does_not_rewrite_arbitrary_transcript_data(self):
        for basename in ('video', 'h264', 'streams', '6', 'codec_type'):
            executable = str(Path('/synthetic-private-tools') / basename)
            payload = '{ "streams": [{"codec_type": "video", "codec_name": "h264", "nb_read_frames": "6"}], "extra": "' + basename + '" }\n'
            # Also cover malformed raw output: redaction must not parse/rewrite it.
            for stdout in (payload, payload[:-4]):
                record = dict(command=[executable, '-show_entries', 'stream=codec_type,codec_name',
                                       basename, '/synthetic-private-work/artifact.mp4'],
                              exit_status=0, stdout=stdout, stderr='observed '+basename+' value\n')
                with self.subTest(basename=basename, stdout=stdout):
                    result, _ = video._failure_diagnostics({'probe': record}, 'verification failed', 4096)
                    observed = result['probe']
                    self.assertEqual(observed['stdout'], stdout)
                    self.assertEqual(observed['stderr'], record['stderr'])
                    self.assertEqual(observed['command'][0], 'ffprobe')
                    self.assertEqual(observed['command'][1:4], record['command'][1:4])
                    self.assertEqual(observed['command'][4], '<media-workdir>/artifact.mp4')
                    self.assertEqual(observed['exit_status'], 0)
                    self.assertEqual(record['command'][0], executable)

    def test_path_redaction_preserves_unrelated_longer_names_and_handles_option_values(self):
        executable = '/synthetic-private-tools/video'
        artifact = '/synthetic-private-work/artifact.mp4'
        unrelated = '/synthetic-private-tools-archive/video and /synthetic-private-work-archive/artifact.mp4'
        raw = dict(command=[executable, '--literal=' + artifact, artifact],
                   exit_status=1, stdout=unrelated,
                   stderr=artifact + ': actual input error\n' + unrelated)
        result, _ = video._failure_diagnostics({'probe': raw}, 'failed', 4096)
        self.assertEqual(result['probe']['command'],
                         ['ffprobe', '--literal=<media-workdir>/artifact.mp4', '<media-workdir>/artifact.mp4'])
        self.assertEqual(result['probe']['stdout'], unrelated)
        self.assertEqual(result['probe']['stderr'],
                         '<media-workdir>/artifact.mp4: actual input error\n' + unrelated)

    def test_only_stderr_executable_error_labels_use_logical_tool_role(self):
        marker = 'SYNTHETIC_PRIVATE_EXECUTABLE'
        for stage, role in (('probe', 'ffprobe'), ('timeline', 'ffprobe'),
                            ('decode', 'ffmpeg'), ('pixel_decode', 'ffmpeg')):
            executable = '/synthetic-private-tools/' + marker
            raw = dict(command=[executable], exit_status=1,
                       stdout=marker + ': observed data\n',
                       stderr=marker + ': actual diagnostic\ncontext '+marker+' remains data\n')
            with self.subTest(stage=stage):
                result, _ = video._failure_diagnostics({stage: raw}, 'failed', 4096)
                self.assertEqual(result[stage]['command'], [role])
                self.assertEqual(result[stage]['stdout'], raw['stdout'])
                self.assertEqual(result[stage]['stderr'], role + ': actual diagnostic\ncontext '+marker+' remains data\n')

    def test_simulated_python_and_cli_failures_preserve_raw_successful_probe(self):
        core.SilentContracts.setUp(self)
        artifact = self.root / 'simulated.mp4'
        artifact.write_bytes(b'simulated media; no native codec evidence')
        request_path = self.root / 'request.json'
        request_path.write_text(json.dumps(self.request))
        for basename in ('video', 'h264', 'streams', 'codec_type'):
            observed = core.SilentContracts._simulated_verification_records(self)[0]
            observed['command'] = [str(self.root / 'SYNTHETIC_PRIVATE_TOOLS' / basename), '-of', 'json']
            # Valid stream observations precede a deterministic format rejection.
            payload = json.loads(observed['stdout'])
            payload['format']['format_name'] = 'unsupported-container'
            observed['stdout'] = json.dumps(payload, indent=2) + '\n'
            raw = observed['stdout']
            for interface in ('python', 'cli'):
                with self.subTest(basename=basename, interface=interface), \
                        patch.object(video, '_binary', return_value=observed['command'][0]), \
                        patch.object(video, '_run', return_value=observed):
                    if interface == 'python':
                        with self.assertRaisesRegex(ValueError, 'container mismatch') as failed:
                            video.verify_export(artifact, self.request)
                        report = failed.exception.evidence
                    else:
                        output = io.StringIO()
                        with patch.object(sys, 'argv', ['silent-video', 'verify-export', str(artifact),
                                '--request', str(request_path), '--require-silent']), \
                                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                            video.main()
                        self.assertEqual(stopped.exception.code, 1)
                        report = json.loads(output.getvalue())
                    self.assertIs(report['verified'], False)
                    self.assertEqual(report['probe']['command'][0], 'ffprobe')
                    self.assertEqual(report['probe']['stdout'], raw)
                    self.assertEqual(report['probe']['exit_status'], 0)
                    self.assertEqual(report['video_stream_count'], 1)
                    self.assertEqual(report['audio_stream_count'], 0)
                    self.assertEqual(report['other_stream_count'], 0)
                    self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))
                    self.assertEqual(report['request_sha256'], video._digest(self.request))
                    self.assertEqual(report['source_manifest_sha256'], video._digest(self.source))
                    self.assertNotIn('SYNTHETIC_PRIVATE_TOOLS', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
