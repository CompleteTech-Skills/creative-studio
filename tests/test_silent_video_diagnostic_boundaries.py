"""Public refusal boundaries; every probe transcript here is explicitly simulated.

Real request files and CLI subprocesses exercise request admission. Simulated
probe records isolate transcript retention without claiming native media proof.
"""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_silent_video as core

video = core.video

REQUEST_REFUSAL = {
    'schema_version': 1,
    'verified': False,
    'error': 'silent media request could not be safely read or validated',
}


class RequestDiagnosticBoundaries(unittest.TestCase):
    setUp = core.SilentContracts.setUp

    def assert_cli_refuses(self, raw_request, operation):
        request_path = self.root / 'SYNTHETIC_REQUEST_BOUNDARY.json'
        request_path.write_bytes(raw_request)
        destination = self.root / 'SYNTHETIC_UNPUBLISHED.mp4'
        command = [sys.executable, str(Path(video.__file__).resolve()), operation]
        if operation == 'encode':
            command += ['--request', str(request_path), '--root', str(self.root),
                        '--destination', str(destination)]
        else:
            command += [str(self.root / 'SYNTHETIC_UNREAD.mp4'), '--request',
                        str(request_path), '--require-silent']
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b'', 'request refusal must not emit a traceback')
        self.assertEqual(json.loads(result.stdout), REQUEST_REFUSAL)
        self.assertFalse(destination.exists())

    def test_modest_invalid_integer_is_a_fixed_cli_refusal_control(self):
        for operation in ('encode', 'verify-export'):
            with self.subTest(operation=operation):
                self.assert_cli_refuses(b'{"schema_version":256}', operation)

    def test_valid_json_out_of_range_integers_are_fixed_cli_refusals(self):
        # These 309-digit integers are valid JSON and well below Python's
        # integer-string parser guard. They exceed finite float conversion.
        fields = (
            ('schema_version',), ('source', 'schema_version'),
            ('source', 'precision'), ('source', 'width'), ('source', 'height'),
            ('source', 'fps', 'numerator'), ('source', 'fps', 'denominator'),
            ('source', 'frames', 0, 'index'), ('source', 'frames', 0, 'bytes'),
            ('limits', 'timeout_seconds'), ('limits', 'max_output_bytes'),
            ('limits', 'max_log_bytes'),
        )
        for field in fields:
            for sign in (1, -1):
                request = deepcopy(self.request)
                owner = request
                for key in field[:-1]:
                    owner = owner[key]
                owner[field[-1]] = sign * (1 << 1025)
                raw = json.dumps(request).encode()
                self.assertEqual(json.loads(raw), request)
                for operation in ('encode', 'verify-export'):
                    with self.subTest(field=field, sign=sign, operation=operation):
                        self.assert_cli_refuses(raw, operation)

    def test_actual_json_parser_depth_boundary_is_a_fixed_cli_refusal(self):
        depth = max(10000, 4 * sys.getrecursionlimit())
        raw = ('{"schema_version":' + '[' * depth + '1' + ']' * depth + '}').encode()
        with self.assertRaises(RecursionError):
            json.loads(raw)
        for operation in ('encode', 'verify-export'):
            with self.subTest(operation=operation):
                self.assert_cli_refuses(raw, operation)

    def test_invalid_request_utf8_is_a_fixed_cli_refusal(self):
        for operation in ('encode', 'verify-export'):
            with self.subTest(operation=operation):
                self.assert_cli_refuses(b'{"schema_version":"\xff"}', operation)


class SimulatedTranscriptBoundaries(unittest.TestCase):
    setUp = core.SilentContracts.setUp

    def refusal_reports(self, observed):
        artifact = self.root / 'simulated.mp4'
        artifact.write_bytes(b'simulated artifact; no native media evidence')
        request_path = self.root / 'simulated-request.json'
        request_path.write_text(json.dumps(self.request))
        with patch.object(video, '_binary', return_value=sys.executable):
            with patch.object(video, '_run', return_value=observed), \
                    self.assertRaises(ValueError) as failed:
                video.verify_export(artifact, self.request)
            yield 'python', failed.exception.evidence
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(video, '_run', return_value=observed), \
                    patch.object(sys, 'argv', ['silent-video', 'verify-export', str(artifact),
                        '--request', str(request_path), '--require-silent']), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as stopped:
                video.main()
            self.assertEqual(stopped.exception.code, 1)
            self.assertEqual(stderr.getvalue(), '')
            yield 'cli', json.loads(stdout.getvalue())

    def test_simulated_truncated_numeric_slash_remains_byte_identical(self):
        for field, numerator in (('avg_frame_rate', '24'), ('time_base', '1')):
            stdout = ' ' * 1100 + '{"streams":[{"' + field + '":"' + numerator + '/'
            observed = dict(command=[sys.executable, str(self.root / 'simulated.mp4')],
                            exit_status=0, stdout=stdout, stderr='', stdout_truncated=True)
            for interface, report in self.refusal_reports(observed):
                with self.subTest(field=field, interface=interface):
                    self.assertIs(report['verified'], False)
                    self.assertEqual(report['probe']['stdout'].encode(), stdout.encode())
                    self.assertIs(report['probe']['stdout_truncated'], True)
                    self.assertEqual(report['probe']['exit_status'], 0)

    def test_simulated_actual_command_path_prefix_still_redacts(self):
        path = str(self.root / 'SYNTHETIC_PRIVATE_COMMAND_PATH' / 'nested' / 'artifact.mp4')
        for cut in (1, 4, len(path) - 2):
            observed = dict(command=[sys.executable, path], exit_status=1,
                            stdout='', stderr='simulated input error: ' + path[:cut],
                            stderr_truncated=True)
            for interface, report in self.refusal_reports(observed):
                with self.subTest(cut=cut, interface=interface):
                    self.assertIs(report['verified'], False)
                    self.assertIs(report['probe']['stderr_truncated'], True)
                    self.assertNotIn('SYNTHETIC_PRIVATE_COMMAND_PATH', json.dumps(report))
                    self.assertNotIn(path[:cut], report['probe']['stderr'])
                    self.assertIn('<media-path-truncated>', report['probe']['stderr'])


if __name__ == '__main__':
    unittest.main()
