"""Ambiguous simulated probe JSON is retained, never promoted to observed facts."""
import contextlib
import copy
import hashlib
import io
import json
import sys
import unittest
from unittest.mock import patch

import test_silent_video as core

video = core.video


class ProbeMemberAdmission(unittest.TestCase):
    setUp = core.SilentContracts.setUp

    def refusal(self, records, *, cli):
        artifact = self.root / 'simulated.mp4'
        artifact.write_bytes(b'portable simulated artifact, no codec execution')
        request = self.root / 'request.json'
        request.write_text(json.dumps(self.request))
        with patch.object(video, '_binary', return_value=sys.executable), \
                patch.object(video, '_run', side_effect=copy.deepcopy(records)) as commands:
            if cli:
                output = io.StringIO()
                with patch.object(sys, 'argv', ['silent_video', 'verify-export', str(artifact),
                        '--request', str(request), '--require-silent']), \
                        contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as ended:
                    video.main()
                self.assertEqual(ended.exception.code, 1)
                evidence = json.loads(output.getvalue())
            else:
                with self.assertRaises(ValueError) as refused:
                    video.verify_export(artifact, self.request)
                evidence = refused.exception.evidence
            self.assertEqual(commands.call_count, len(records))
        self.assertEqual(evidence['artifact_sha256'], hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual(len(evidence['request_sha256']), 64)
        self.assertEqual(len(evidence['source_manifest_sha256']), 64)
        return evidence

    def test_duplicate_probe_members_never_produce_stream_counts(self):
        payloads = [
            '{"streams":[{"codec_type":"audio"},{"codec_type":"audio"}],"streams":[{"codec_type":"audio"}],"format":{}}',
            '{"streams":[{"codec_type":"audio","codec_type":"video"}],"format":{}}',
            '{"streams":[{"codec_type":"audio"}],"format":{"format_name":"mov","format_name":"mp4"}}',
            '{"streams":[{"codec_type":"audio","tags":{"label":"first","label":"second"}}],"format":{}}',
        ]
        for transcript in payloads:
            for cli in (False, True):
                with self.subTest(transcript=transcript, cli=cli):
                    record = core.SilentContracts._simulated_verification_records(self)[0]
                    record['stdout'] = transcript
                    evidence = self.refusal([record], cli=cli)
                    self.assertEqual(evidence['probe']['stdout'], transcript)
                    self.assertEqual(evidence['probe']['exit_status'], 0)
                    for key in ('audio_stream_count', 'video_stream_count', 'other_stream_count', 'frame_count', 'timeline', 'decode'):
                        self.assertNotIn(key, evidence)

    def test_unambiguous_audio_probe_retains_its_observed_counts(self):
        for cli in (False, True):
            record = core.SilentContracts._simulated_verification_records(self)[0]
            record['stdout'] = '{"streams":[{"codec_type":"audio"}],"format":{}}'
            evidence = self.refusal([record], cli=cli)
            self.assertEqual([evidence[key] for key in
                ('video_stream_count', 'audio_stream_count', 'other_stream_count')], [0, 1, 0])
            self.assertEqual(evidence['probe']['stdout'], record['stdout'])

    def test_duplicate_timeline_members_never_produce_frame_facts(self):
        frames = [{"best_effort_timestamp": index * 512} for index in range(6)]
        for transcript in ('{"frames":[],"frames":' + json.dumps(frames) + '}',
                           '{"frames":[{"best_effort_timestamp":0,"best_effort_timestamp":512}]}'):
            for cli in (False, True):
                with self.subTest(cli=cli, transcript=transcript):
                    records = core.SilentContracts._simulated_verification_records(self)[:2]
                    records[1]['stdout'] = transcript
                    evidence = self.refusal(records, cli=cli)
                    self.assertEqual(evidence['timeline']['stdout'], transcript)
                    self.assertEqual(evidence['timeline']['exit_status'], 0)
                    self.assertEqual(evidence['video_stream_count'], 1)
                    self.assertNotIn('frame_count', evidence)
                    self.assertNotIn('decode', evidence)


if __name__ == '__main__':
    unittest.main()
