"""Portable read-refusal contracts; simulated commands are not native evidence.

The optional companion exercises actual filesystem permissions and real codecs.
These tests deliberately inject the shared descriptor reader's path-bearing
RigError, rather than assuming a particular operating system's errno message.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_silent_video as core

video = core.video
from rig_errors import RigError


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


class ReadPrivacyAssertions:
    def setUp(self):
        core.SilentContracts.setUp(self)
        # Distinct caller names must not be confused with verifier-owned aliases.
        renamed = []
        for index, name in enumerate(self.paths):
            replacement = 'SYNTHETIC_PRIVATE_SOURCE_%d.png' % index
            (self.root / name).rename(self.root / replacement)
            renamed.append(replacement)
        self.paths = renamed
        self.source = video.build_source(self.root, self.paths,
            fps={'numerator': 24, 'denominator': 1}, origin='synthetic')
        self.request = video.make_request(self.source)
        self.artifact = self.root / 'SYNTHETIC_PRIVATE_CALLER_MOVIE.mp4'
        self.request_path = self.root / 'SYNTHETIC_PRIVATE_REQUEST.json'
        self.request_path.write_text(json.dumps(self.request), encoding='utf-8')

    def assert_private(self, *values):
        public = '\n'.join(value if isinstance(value, str) else
                           json.dumps(value, default=str) for value in values)
        for private in (str(self.root), self.artifact.name,
                        self.request_path.name, *self.paths):
            self.assertNotIn(private, public)

    def assert_identity(self, evidence):
        self.assertIs(evidence['verified'], False)
        self.assertEqual(evidence['request_sha256'], identity(self.request))
        self.assertEqual(evidence['source_manifest_sha256'], identity(self.source))

    def assert_completed_snapshot(self, evidence, original):
        self.assert_identity(evidence)
        self.assertEqual(evidence['artifact_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(evidence['artifact_bytes'], len(original))
        self.assertEqual(evidence['artifact_sha256_scope'], 'verification-snapshot')
        for stage in ('probe', 'timeline', 'decode'):
            self.assertEqual(evidence[stage]['exit_status'], 0)
            self.assertTrue(evidence[stage]['command'])
            self.assertIsInstance(evidence[stage]['stdout'], str)
            self.assertIsInstance(evidence[stage]['stderr'], str)
        self.assertTrue(evidence['probe']['stdout'])
        self.assertTrue(evidence['timeline']['stdout'])

    def assert_before_snapshot(self, evidence):
        self.assert_identity(evidence)
        for unobserved in ('artifact_sha256', 'artifact_sha256_scope', 'artifact_bytes',
                           'probe', 'timeline', 'decode', 'pixel_decode',
                           'video_stream_count', 'audio_stream_count', 'other_stream_count',
                           'frame_count', 'duration', 'source_pixel_comparison'):
            self.assertNotIn(unobserved, evidence)

    def api_failure(self):
        with self.assertRaises(ValueError) as failed:
            video.verify_export(self.artifact, self.request, root=self.root)
        error = failed.exception
        self.assert_private(str(error), getattr(error, 'report', {}),
                            getattr(error, 'evidence', {}))
        return error.evidence

    def simulated_cli_failure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['silent_video', 'verify-export', str(self.artifact),
                '--request', str(self.request_path), '--root', str(self.root), '--require-silent']), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as stopped:
            video.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assert_private(stdout.getvalue(), stderr.getvalue())
        return json.loads(stdout.getvalue())


class PortableReadPrivacy(ReadPrivacyAssertions, unittest.TestCase):
    def test_simulated_source_read_refusal_preserves_completed_stage_evidence(self):
        original = core.SIMULATED_MP4_FTYP + core.mp4_box(b'free', b'portable simulated media fixture')
        self.artifact.write_bytes(original)
        descriptor_reader = video.open_payload

        def refuse(root, relative, **options):
            if Path(root) == self.root.resolve() and relative == self.paths[2]:
                raise RigError('RIG_PAYLOAD_IO',
                    'Cannot safely read payload: ' + str(self.root / relative), asset=relative)
            return descriptor_reader(root, relative, **options)

        for interface in (self.api_failure, self.simulated_cli_failure):
            records = core.SilentContracts._simulated_verification_records(self)
            with self.subTest(interface=interface.__name__), \
                    patch.object(video, 'open_payload', side_effect=refuse), \
                    patch.object(video, '_binary', return_value=sys.executable), \
                    patch.object(video, '_run', side_effect=records) as run:
                evidence = interface()
                self.assert_completed_snapshot(evidence, original)
                self.assertEqual(run.call_count, 3)

    def test_simulated_artifact_read_refusal_has_identity_without_media_observations(self):
        self.artifact.write_bytes(b'portable unreadable artifact fixture')
        descriptor_reader = video.open_payload

        def refuse(root, relative, **options):
            if Path(root) == self.root.resolve() and relative == self.artifact.name:
                raise RigError('RIG_PAYLOAD_IO',
                    'Cannot safely read payload: ' + str(self.artifact), asset=relative)
            return descriptor_reader(root, relative, **options)

        for interface in (self.api_failure, self.simulated_cli_failure):
            with self.subTest(interface=interface.__name__), \
                    patch.object(video, 'open_payload', side_effect=refuse), \
                    patch.object(video, '_run', side_effect=AssertionError('No media process may run')) as run:
                evidence = interface()
                self.assert_before_snapshot(evidence)
                run.assert_not_called()


class HistoricalProbeReadPrivacy(ReadPrivacyAssertions, unittest.TestCase):
    def assert_historical_refusal(self, recorded, observed, *, digest_error=None):
        original = core.SIMULATED_MP4_FTYP + core.mp4_box(b'free', b'portable historical receipt fixture')
        self.artifact.write_bytes(original)
        records = core.SilentContracts._simulated_verification_records(self)
        records.append(dict(command=['simulated-private-historical-probe'], exit_status=0,
                            stdout=observed, stderr=''))
        digest = video._digest

        def guarded_digest(value):
            if digest_error is not None and value == {'format': {'private_metadata': str(self.root)}}:
                raise digest_error(str(self.root))
            return digest(value)

        with patch.object(video, '_binary', return_value=sys.executable), \
                patch.object(video, '_run', side_effect=records) as run, \
                patch.object(video, '_digest', side_effect=guarded_digest), \
                self.assertRaisesRegex(ValueError, '^historical silent probe could not be independently verified$') as failed:
            video.verify_export(self.artifact, self.request, _historical_probe=recorded)
        self.assertEqual(run.call_count, 4)
        self.assert_completed_snapshot(failed.exception.evidence, original)
        self.assert_private(str(failed.exception), failed.exception.evidence)
        self.assertNotIn('private_metadata', json.dumps(failed.exception.evidence))

    def test_actual_historical_json_nesting_refusal_retains_primary_evidence(self):
        nested = '[' * max(10000, 4 * sys.getrecursionlimit()) + '0'
        nested += ']' * max(10000, 4 * sys.getrecursionlimit())
        with self.assertRaises(RecursionError):
            json.loads(nested)
        ordinary = json.dumps({'format': {'private_metadata': str(self.root)}})
        for recorded, observed in ((nested, ordinary), (ordinary, nested)):
            with self.subTest(nested_recorded=recorded == nested):
                self.assert_historical_refusal(recorded, observed)

    def test_simulated_historical_digest_limits_retain_primary_evidence(self):
        ordinary = json.dumps({'format': {'private_metadata': str(self.root)}})
        for error in (RecursionError, OverflowError):
            with self.subTest(error=error.__name__):
                self.assert_historical_refusal(ordinary, ordinary, digest_error=error)


if __name__ == '__main__':
    unittest.main()
