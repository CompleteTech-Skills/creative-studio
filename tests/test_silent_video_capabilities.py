"""Contract, parser, bounded subprocess and actual installed-tool probes."""
import copy
import json
import os
import signal
import time
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_video_capabilities as capabilities


OUTPUTS = [
    'ffmpeg version test-build\n', 'ffprobe version test-build\n',
    'Encoders:\n V..... libx264 H.264\n A..... aac audio\n',
    'Formats:\n  E  mp4 MP4\n',
    'Pixel formats:\nIO... yuv420p 3 12 8-8-8\n',
    'Encoder libx264 [H264]:\n    Supported pixel formats: yuv420p nv12\n',
]


class ProfileTests(unittest.TestCase):
    def test_exact_profile_and_independent_copy(self):
        profile = capabilities.validate_profile(copy.deepcopy(capabilities.silent_profile()))
        self.assertEqual(profile, capabilities.silent_profile())
        profile['audio']['policy'] = 'allowed'
        self.assertEqual(capabilities.silent_profile()['audio']['policy'], 'forbidden')

    def test_reject_unknown_missing_or_non_object(self):
        for value in [None, [], {}, {**capabilities.silent_profile(), 'ffmpeg_args': ['-c:a', 'aac']}]:
            with self.subTest(value=value), self.assertRaises(capabilities.CapabilityError):
                capabilities.validate_profile(value)
        for key in capabilities.silent_profile():
            value = copy.deepcopy(capabilities.silent_profile())
            del value[key]
            with self.subTest(key=key), self.assertRaises(capabilities.CapabilityError):
                capabilities.validate_profile(value)

    def test_no_audio_or_codec_fallback(self):
        variants = [('container', 'webm'), ('codec', 'hevc'), ('encoder', 'h264_nvenc'),
                    ('pixel_format', 'rgba'), ('alpha', 'straight'), ('schema_version', True),
                    ('schema_version', 1.0), ('schema_version', 2)]
        for key, value in variants:
            profile = copy.deepcopy(capabilities.silent_profile())
            profile[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(capabilities.CapabilityError):
                capabilities.validate_profile(profile)
        for audio in [{'policy': 'forbidden', 'audio_stream_count': False},
                      {'policy': 'forbidden', 'audio_stream_count': 0.0},
                      {'policy': 'forbidden', 'audio_stream_count': 1},
                      {'policy': 'silent', 'audio_stream_count': 0},
                      {'policy': 'forbidden', 'audio_stream_count': 0, 'codec': 'aac'}, None]:
            with self.subTest(audio=audio), self.assertRaises(capabilities.CapabilityError):
                capabilities.validate_profile({**capabilities.silent_profile(), 'audio': audio})


class CapabilityTests(unittest.TestCase):
    def probe(self, outputs):
        with patch.object(capabilities, '_executable', side_effect=['/trusted/ffmpeg', '/trusted/ffprobe']), \
             patch.object(capabilities, '_run', side_effect=outputs) as run:
            report = capabilities.probe_capabilities()
            return report, run.call_args_list

    def test_available_is_only_advertised_and_fixed_arguments(self):
        report, calls = self.probe(OUTPUTS)
        self.assertEqual(report['status'], 'available')
        self.assertEqual(report['media_execution'], 'not-performed')
        self.assertEqual(report['evidence'], 'advertised-capabilities-only')
        self.assertEqual([call.args[0] for call in calls], [
            ['/trusted/ffmpeg', '-version'], ['/trusted/ffprobe', '-version'],
            ['/trusted/ffmpeg', '-hide_banner', '-encoders'],
            ['/trusted/ffmpeg', '-hide_banner', '-muxers'],
            ['/trusted/ffmpeg', '-hide_banner', '-pix_fmts'],
            ['/trusted/ffmpeg', '-hide_banner', '-h', 'encoder=libx264']])

    def test_missing_component_is_unsupported_without_fallback(self):
        for index, output in [(2, 'Encoders:\n V..... other other\n'),
                              (3, 'Formats:\n  E  webm WebM\n'),
                              (4, 'Pixel formats:\nIO... rgb24 3 24 8-8-8\n'),
                              (5, 'Encoder libx264 [H264]:\n Supported pixel formats: nv12\n')]:
            outputs = OUTPUTS.copy()
            outputs[index] = output
            with self.subTest(index=index):
                report, calls = self.probe(outputs)
                self.assertEqual(report['status'], 'unsupported')
                self.assertLessEqual(len(calls), 6)

    def test_malformed_successful_output_is_unavailable(self):
        for index in range(6):
            outputs = OUTPUTS.copy()
            outputs[index] = 'not a capability report\n'
            with self.subTest(index=index):
                self.assertEqual(self.probe(outputs)[0]['status'], 'unavailable')
        for index, output in [(2, 'Encoders:\n'), (3, 'Formats:\n'),
                              (4, 'Pixel formats:\n'), (5, 'Encoder libx264 [H264]:\n')]:
            outputs = OUTPUTS.copy()
            outputs[index] = output
            self.assertEqual(self.probe(outputs)[0]['status'], 'unavailable')

    def test_failures_are_unavailable(self):
        for error in [OSError('permission denied'), capabilities.CapabilityError('timeout')]:
            with self.subTest(error=error):
                report, _ = self.probe([error])
                self.assertEqual(report['status'], 'unavailable')

    def test_missing_binary_does_not_execute(self):
        with patch.object(capabilities.shutil, 'which', return_value=None), \
             patch.object(capabilities, '_run') as run:
            report = capabilities.probe_capabilities()
            self.assertEqual(report['status'], 'unavailable')
            run.assert_not_called()

    def test_invalid_override_never_falls_back(self):
        with patch.object(capabilities.shutil, 'which') as which:
            self.assertIsNone(capabilities._executable('ffmpeg', 'relative/ffmpeg'))
            self.assertIsNone(capabilities._executable('ffmpeg', '/nonexistent/ffmpeg'))
            which.assert_not_called()

    def test_invalid_timeouts_do_not_execute(self):
        for timeout in [True, 0, -1, float('nan'), float('inf'), 61, '10', None]:
            with self.subTest(timeout=timeout), patch.object(capabilities, '_run') as run, \
                 self.assertRaises(capabilities.CapabilityError):
                capabilities.probe_capabilities(timeout=timeout)
            run.assert_not_called()

    def test_listing_does_not_confuse_audio_input_or_substring(self):
        self.assertEqual(capabilities._table('Encoders:\n A..... libx264 fake\n V..... libx264_fake other\n',
                                            'encoders'), {'libx264_fake'})
        self.assertEqual(capabilities._table('Formats:\n D   mp4 input\n  E  webm,mkv output\n',
                                            'muxers'), {'webm', 'mkv'})
        self.assertEqual(capabilities._table('Pixel formats:\nI.... yuv420p 3 12\nIO... rgb24 3 24\n',
                                            'pixels'), {'rgb24'})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'external media tools absent')
    def test_installed_tools(self):
        report = capabilities.probe_capabilities()
        self.assertIn(report['status'], {'available', 'unsupported'})
        self.assertTrue(report['tools']['ffmpeg']['version'].startswith('ffmpeg version '))
        self.assertTrue(report['tools']['ffprobe']['version'].startswith('ffprobe version '))
        self.assertEqual(report['media_execution'], 'not-performed')
        json.dumps(report, allow_nan=False)


class SubprocessTests(unittest.TestCase):
    def run_python(self, source, timeout=2):
        return capabilities._run([sys.executable, '-c', source], timeout)

    def test_actual_stdout_and_stderr(self):
        self.assertIn('hello', self.run_python("print('hello')"))
        self.assertIn('error stream', self.run_python("import sys; print('error stream', file=sys.stderr)"))

    def test_actual_nonzero_utf8_and_timeout(self):
        for source in ['raise SystemExit(3)', "import sys; sys.stdout.buffer.write(b'\\xff')",
                       'import time; time.sleep(2)']:
            with self.subTest(source=source), self.assertRaises(capabilities.CapabilityError):
                self.run_python(source, timeout=.1)

    def test_inherited_stdout_does_not_extend_deadline(self):
        # The direct child exits, but its child retains the output handle. A
        # buffered pipe-reader implementation blocked close until that exit.
        pid = None
        start = time.monotonic()
        try:
            output = self.run_python(
                "import subprocess, sys; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']); "
                "print(p.pid, flush=True)", timeout=.5)
            pid = int(output.strip())
            self.assertLess(time.monotonic() - start, 1.5)
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def test_actual_output_overflow(self):
        with patch.object(capabilities, 'MAX_PROBE_BYTES', 100), \
             self.assertRaisesRegex(capabilities.CapabilityError, 'byte limit'):
            self.run_python("print('x' * 20000)")


if __name__ == '__main__':
    unittest.main()
