"""Real media and adversarial acceptance for the separate video-only export path.

All input images in this module are synthetic test fixtures, never production
art. The integration cases execute the installed FFmpeg and ffprobe binaries.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import zlib

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_video as video

MEDIA_AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and video.secure_publication_supported())
if os.environ.get('HATCH_SILENT_MEDIA_TEST') == '1' and not MEDIA_AVAILABLE:
    raise RuntimeError('HATCH_SILENT_MEDIA_TEST requires real FFmpeg and ffprobe')


class SilentContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='silent-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = []
        for index in range(6):
            path = self.root / ('%06d.png' % index)
            # Each frame has a distinct moving patch so pixel verification can
            # detect reordered or frozen content, not merely frame cardinality.
            image = Image.new('RGB', (64, 48), (18 + index * 20, 36, 80))
            image.paste((230, 200, 20), (index * 6, 12, index * 6 + 12, 32))
            image.save(path)
            self.paths.append(path.name)
        self.source = video.build_source(self.root, self.paths,
                                         fps={'numerator': 24, 'denominator': 1},
                                         origin='synthetic')
        self.request = video.make_request(self.source)

    def test_nonzero_command_retains_structured_evidence(self):
        command=[sys.executable,'-c',"import sys; print('fixture stdout'); print('fixture stderr',file=sys.stderr); sys.exit(7)"]
        with self.assertRaises(ValueError) as failed:
            video._run(command,video.DEFAULT_LIMITS)
        raw=failed.exception.evidence['command_failure']
        self.assertEqual(raw['command'],[Path(command[0]).name,*command[1:]])
        self.assertEqual(raw['exit_status'],7)
        self.assertIn('fixture stdout',raw['stdout'])
        self.assertIn('fixture stderr',raw['stderr'])

    def test_timeout_and_log_ceiling_preserve_bounded_observations(self):
        cases=[("import sys,time; print('before timeout',flush=True); time.sleep(10)",
                dict(video.DEFAULT_LIMITS,timeout_seconds=.2),'timeout'),
               ("import sys; sys.stdout.write('x'*8192); sys.stderr.write('y'*8192)",
                dict(video.DEFAULT_LIMITS,max_log_bytes=1024),'evidence byte budget')]
        for script,limits,reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError,reason) as failed:
                    video._run([sys.executable,'-c',script],limits)
                raw=failed.exception.evidence['command_failure']
                self.assertIsInstance(raw['exit_status'],int)
                for field in ('stdout','stderr'):
                    self.assertLessEqual(len(raw[field].encode()),limits['max_log_bytes'])
                if reason=='timeout':
                    self.assertIn('before timeout',raw['stdout'])
                    self.assertTrue(raw['timed_out'])
                else:
                    self.assertTrue(raw['stdout_truncated'])
                    self.assertTrue(raw['stderr_truncated'])

    def test_malformed_successful_probe_survives_python_and_cli_failure(self):
        import contextlib,io
        artifact=self.root/'corrupt.mp4';artifact.write_bytes(b'synthetic malformed media')
        request_path=self.root/'request.json';request_path.write_text(json.dumps(self.request))
        for payload in ('not json','[]','{"streams":null}','{"streams":[1]}'):
            raw=dict(command=['fixture-ffprobe'],exit_status=0,stdout=payload,stderr='fixture diagnostic')
            with self.subTest(payload=payload),patch.object(video,'_binary',return_value=sys.executable),patch.object(video,'_run',return_value=raw):
                with self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact,self.request)
                evidence=failed.exception.evidence
                self.assertEqual(evidence['probe'],raw)
                self.assertEqual(evidence['artifact_sha256'],hashlib.sha256(artifact.read_bytes()).hexdigest())
                self.assertEqual(evidence['request_sha256'],video._digest(self.request))
                for field in ('video_stream_count','audio_stream_count','other_stream_count'):
                    self.assertNotIn(field,evidence)
                transcript=io.StringIO()
                with patch.object(sys,'argv',['silent_video','verify-export',str(artifact),'--request',str(request_path),'--require-silent']),contextlib.redirect_stdout(transcript),self.assertRaises(SystemExit) as exit_result:
                    video.main()
                self.assertEqual(exit_result.exception.code,1)
                report=json.loads(transcript.getvalue())
                self.assertFalse(report['verified'])
                self.assertEqual(report['probe'],raw)
                for field in ('video_stream_count','audio_stream_count','other_stream_count'):
                    self.assertNotIn(field,report)

    def test_probe_field_types_preserve_python_and_cli_failure_evidence(self):
        import contextlib,io
        artifact=self.root/'probe-fields.mp4';artifact.write_bytes(b'synthetic fixture')
        request_path=self.root/'request.json';request_path.write_text(json.dumps(self.request))
        valid={'streams':[{'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p',
                           'width':64,'height':48,'nb_read_frames':'6','sample_aspect_ratio':'1:1',
                           'display_aspect_ratio':'4:3'}],
               'format':{'format_name':'mp4','duration':'0.25'}}
        for key,value in [('display_aspect_ratio',[]),('display_aspect_ratio',None),('nb_read_frames',float('inf'))]:
            payload=deepcopy(valid);payload['streams'][0][key]=value
            raw=dict(command=['fixture-ffprobe'],exit_status=0,stdout=json.dumps(payload),stderr='')
            with self.subTest(field=key,value=value),patch.object(video,'_binary',return_value=sys.executable),patch.object(video,'_run',return_value=raw):
                with self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact,self.request)
                self.assertEqual(failed.exception.evidence['probe'],raw)
                self.assertEqual(failed.exception.evidence['artifact_sha256'],video.file_hash(artifact))
                transcript=io.StringIO()
                with patch.object(sys,'argv',['silent_video','verify-export',str(artifact),'--request',str(request_path),'--require-silent']),contextlib.redirect_stdout(transcript),self.assertRaises(SystemExit) as exit_result:
                    video.main()
                self.assertEqual(exit_result.exception.code,1)
                report=json.loads(transcript.getvalue())
                self.assertFalse(report['verified'])
                self.assertEqual(report['probe'],raw)
                self.assertEqual(report['artifact_sha256'],video.file_hash(artifact))

    def test_invalid_utf8_normalization_marks_clipping_without_allowing_stdout(self):
        limits=dict(video.DEFAULT_LIMITS,max_log_bytes=1024)
        for field,status in [('stderr',7),('stdout',0)]:
            script="import sys; sys.%s.buffer.write(bytes([255])*400); sys.exit(%d)" % (field,status)
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as failed:
                    video._run([sys.executable,'-c',script],limits)
                raw=failed.exception.evidence['command_failure']
                self.assertTrue(raw[field+'_truncated'])
                self.assertEqual(raw[field],'\ufffd'*341)
                self.assertEqual(len(raw[field].encode()),1023)
                self.assertEqual(raw['exit_status'],status)
                if field=='stdout':
                    self.assertIn('invalid UTF-8',str(failed.exception))


    def test_unknown_probe_stream_types_never_invent_stream_counts(self):
        import contextlib,io
        artifact=self.root/'unknown-type.mp4';artifact.write_bytes(b'synthetic fixture')
        request_path=self.root/'request.json';request_path.write_text(json.dumps(self.request))
        for stream in ({},{'codec_type':None},{'codec_type':[]},{'codec_type':'unknown'},{'codec_type':'invented'}):
            raw=dict(command=['fixture-ffprobe'],exit_status=0,stdout=json.dumps({'streams':[stream]}),stderr='')
            with self.subTest(stream=stream),patch.object(video,'_binary',return_value=sys.executable),patch.object(video,'_run',return_value=raw):
                with self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact,self.request)
                evidence=failed.exception.evidence
                self.assertEqual(evidence['probe'],raw)
                for field in ('video_stream_count','audio_stream_count','other_stream_count'):
                    self.assertNotIn(field,evidence)
                transcript=io.StringIO()
                with patch.object(sys,'argv',['silent_video','verify-export',str(artifact),'--request',str(request_path),'--require-silent']),contextlib.redirect_stdout(transcript),self.assertRaises(SystemExit) as exit_result:
                    video.main()
                self.assertEqual(exit_result.exception.code,1)
                report=json.loads(transcript.getvalue())
                self.assertEqual(report['probe'],raw)
                self.assertFalse(report['verified'])
                for field in ('video_stream_count','audio_stream_count','other_stream_count'):
                    self.assertNotIn(field,report)

    def test_strict_decode_rejects_truncated_whitespace_diagnostics(self):
        artifact = self.root / 'decode-contract.mp4'
        artifact.write_bytes(b'synthetic contract fixture; not native media evidence')
        probe = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'pix_fmt': 'yuv420p',
            'width': 64, 'height': 48, 'nb_read_frames': '6', 'sample_aspect_ratio': '1:1',
            'display_aspect_ratio': '4:3', 'avg_frame_rate': '24/1', 'time_base': '1/12288'}],
            'format': {'format_name': 'mp4', 'duration': '0.25'}}
        timeline = {'frames': [{'best_effort_timestamp': i * 512} for i in range(6)]}
        records = [dict(command=['fixture-probe'], exit_status=0, stdout=json.dumps(probe), stderr=''),
                   dict(command=['fixture-timeline'], exit_status=0, stdout=json.dumps(timeline), stderr=''),
                   dict(command=['fixture-decode'], exit_status=0, stdout='', stderr=' \t ', stderr_truncated=True)]
        with patch.object(video, '_binary', return_value=sys.executable), patch.object(video, '_run', side_effect=records):
            with self.assertRaisesRegex(ValueError, 'truncated') as failed:
                video.verify_export(artifact, self.request)
        self.assertTrue(failed.exception.evidence['decode']['stderr_truncated'])

    def test_truncated_command_path_prefixes_are_redacted_at_capture_boundary(self):
        marker = 'SYNTHETIC_PRIVATE_PATH_FRAGMENT'
        path = str(self.root / marker / 'nested-folder' / 'artifact.mp4')
        for cut in (1, 4, len(path) - len('artifact.mp4') - 3, len(path) - 2):
            raw = dict(command=[sys.executable, path], exit_status=1, stdout='',
                       stderr='fixture diagnostic: ' + path[:cut], stderr_truncated=True)
            with self.subTest(cut=cut):
                evidence, message = video._failure_diagnostics({'probe': raw}, 'failed', 1024)
                self.assertNotIn(marker, json.dumps(evidence))
                self.assertNotIn(path[:cut], evidence['probe']['stderr'])
                self.assertIn('<media-path-truncated>', evidence['probe']['stderr'])
                self.assertTrue(evidence['probe']['stderr_truncated'])
                self.assertLessEqual(len(evidence['probe']['stderr'].encode()), 1024)

    def test_incomplete_utf8_capture_does_not_hide_known_path_prefixes(self):
        marker = 'SYNTHETIC_UNICODE_COMMAND_PATH'
        for pad in range(4):
            path = self.root / (marker + 'x' * pad)
            for index in range(6):
                path = path / (str(index) + '\U0001f600' * 45)
            command = [sys.executable, '-c', 'import sys; sys.stderr.write(sys.argv[1]); sys.exit(7)', str(path)]
            with self.subTest(pad=pad), self.assertRaises(ValueError) as failed:
                video._run(command, dict(video.DEFAULT_LIMITS, max_log_bytes=1024))
            record = failed.exception.evidence['command_failure']
            self.assertNotIn(marker, json.dumps(failed.exception.evidence) + str(failed.exception))
            self.assertTrue(record['stderr_truncated'])
            self.assertIn('<media-path-truncated>', record['stderr'])
            self.assertLessEqual(len(record['stderr'].encode()), 1024)

    def test_filesystem_byte_spelling_is_redacted_after_diagnostic_decoding(self):
        marker = 'SYNTHETIC_FILESYSTEM_BYTE_PATH'
        path = self.root / (marker + '\udcff')
        for index in range(6):
            path /= str(index) + 'x' * 180
        path /= 'artifact.mp4'
        displayed = os.fsencode(str(path)).decode('utf-8', 'replace')
        for truncated in (False, True):
            transcript = displayed[:150] if truncated else displayed + ': synthetic error'
            evidence = {'probe': {'command': [sys.executable, str(path)], 'exit_status': 1,
                                 'stdout': '', 'stderr': transcript, 'stderr_truncated': truncated}}
            with self.subTest(truncated=truncated):
                result, message = video._failure_diagnostics(evidence, transcript, 1024)
                self.assertNotIn(marker, json.dumps(result) + message)
                self.assertLessEqual(len(result['probe']['stderr'].encode()), 1024)

    def test_probe_and_timeline_nesting_errors_keep_python_and_cli_evidence(self):
        import contextlib, io
        artifact = self.root / 'nested-probe.mp4'
        artifact.write_bytes(b'synthetic parser contract fixture')
        request_path = self.root / 'nested-request.json'
        request_path.write_text(json.dumps(self.request))
        depth = max(10000, 4 * sys.getrecursionlimit())
        nested = '[' * depth + '0' + ']' * depth
        self.assertLess(len(nested), self.request['limits']['max_log_bytes'])
        with self.assertRaises(RecursionError):
            json.loads(nested)
        probe = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'pix_fmt': 'yuv420p',
            'width': 64, 'height': 48, 'nb_read_frames': '6', 'sample_aspect_ratio': '1:1',
            'display_aspect_ratio': '4:3', 'avg_frame_rate': '24/1', 'time_base': '1/12288'}],
            'format': {'format_name': 'mp4', 'duration': '0.25'}}
        observed = dict(command=['fixture-probe'], exit_status=0, stdout=json.dumps(probe), stderr='')
        for stage in ('probe', 'timeline'):
            raw = dict(command=['fixture-' + stage], exit_status=0,
                       stdout=nested if stage == 'probe' else '{"frames":' + nested + '}', stderr='')
            records = [raw] if stage == 'probe' else [observed, raw]
            with self.subTest(stage=stage), patch.object(video, '_binary', return_value=sys.executable):
                with patch.object(video, '_run', side_effect=records), self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact, self.request)
                self.assertEqual(failed.exception.evidence[stage], raw)
                self.assertEqual(failed.exception.evidence['artifact_sha256'], video.file_hash(artifact))
                transcript = io.StringIO()
                with patch.object(video, '_run', side_effect=records), patch.object(sys, 'argv',
                        ['silent_video', 'verify-export', str(artifact), '--request', str(request_path), '--require-silent']), \
                        contextlib.redirect_stdout(transcript), self.assertRaises(SystemExit) as stopped:
                    video.main()
                self.assertEqual(stopped.exception.code, 1)
                report = json.loads(transcript.getvalue())
                self.assertFalse(report['verified'])
                self.assertEqual(report[stage], raw)

    def test_recognized_unwanted_stream_types_remain_counted_and_rejected(self):
        artifact=self.root/'extra-stream.mp4';artifact.write_bytes(b'synthetic fixture')
        for codec_type in ('subtitle','data','attachment'):
            raw=dict(command=['fixture-ffprobe'],exit_status=0,stdout=json.dumps({'streams':[{'codec_type':'video'},{'codec_type':codec_type}]}),stderr='')
            with self.subTest(codec_type=codec_type),patch.object(video,'_binary',return_value=sys.executable),patch.object(video,'_run',return_value=raw):
                with self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact,self.request)
                self.assertEqual(failed.exception.evidence['video_stream_count'],1)
                self.assertEqual(failed.exception.evidence['audio_stream_count'],0)
                self.assertEqual(failed.exception.evidence['other_stream_count'],1)
                self.assertEqual(failed.exception.evidence['probe'],raw)

    def test_committed_schema_parity(self):
        root = Path(__file__).resolve().parents[1] / 'schemas' / 'silent-video-v1'
        actual = video.schemas()
        self.assertEqual(set(path.stem.removesuffix('.schema') for path in root.glob('*.schema.json')),
                         set(actual))
        for name, schema in actual.items():
            self.assertEqual(json.loads((root / (name + '.schema.json')).read_text()), schema)

    def test_default_request_is_explicitly_audio_forbidden(self):
        self.assertEqual(self.request['audio'], {'policy': 'forbidden'})
        self.assertEqual(self.request['profile'], 'h264-mp4')
        video.validate_request(self.request)

    def test_blender_manifest_requires_integer_v1(self):
        manifest = {'schema_version': 1, 'kind': 'blender-frame-manifest',
                    'engine': 'CYCLES', 'fps': self.source['fps'],
                    'frames': self.source['frames']}
        adapted = video.source_from_blender_manifest(self.root, manifest)
        self.assertEqual(adapted['origin'], 'blender3d')
        self.assertEqual(adapted['frames'], self.source['frames'])
        for version in (2, True, 1.0, '1'):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, 'unsupported Blender frame manifest'):
                    video.source_from_blender_manifest(
                        self.root, dict(manifest, schema_version=version)
                    )

    def test_audio_policy_cannot_be_relaxed_or_silence_substituted(self):
        for policy in ('silent', 'muted', 'optional', 'allowed', None):
            with self.subTest(policy=policy):
                value = deepcopy(self.request)
                value['audio']['policy'] = policy
                with self.assertRaises(ValueError):
                    video.validate_request(value)

    def test_unknown_fields_and_injected_flags_are_rejected(self):
        for key, value in [('ffmpeg_args', ['-map', '1:a']),
                           ('audio_input', 'speech.wav'),
                           ('codec', 'copy'), ('container', 'mkv')]:
            with self.subTest(key=key):
                request = deepcopy(self.request)
                request[key] = value
                with self.assertRaises(ValueError):
                    video.validate_request(request)

    def test_unsupported_or_injected_profile_is_rejected(self):
        for profile in ('h264', 'h264-mp4 -map 1:a', '../mp4', 'audio-mp4', ''):
            with self.subTest(profile=profile):
                with self.assertRaises(ValueError):
                    video.make_request(self.source, profile=profile)

    def test_invalid_exact_fps_is_rejected(self):
        for fps in ({'numerator': 0, 'denominator': 1},
                    {'numerator': 24, 'denominator': 0},
                    {'numerator': True, 'denominator': 1},
                    {'numerator': 24.0, 'denominator': 1},
                    {'numerator': 24, 'denominator': 1, 'extra': 1}):
            with self.subTest(fps=fps):
                with self.assertRaises(ValueError):
                    video.build_source(self.root, self.paths, fps=fps,
                                       origin='synthetic')

    def test_unsafe_source_paths_fail_before_media_execution(self):
        for path in ('../outside.png', '/tmp/outside.png', 'C:\\outside.png',
                     'sub/../../outside.png'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    video.build_source(self.root, [path],
                                       fps={'numerator': 24, 'denominator': 1},
                                       origin='synthetic')

    def test_symlink_source_is_rejected(self):
        link = self.root / 'linked.png'
        try:
            link.symlink_to(self.root / self.paths[0])
        except (OSError, NotImplementedError):
            self.skipTest('OS does not permit test symlink creation')
        with self.assertRaises(ValueError):
            video.build_source(self.root, ['linked.png'],
                               fps={'numerator': 24, 'denominator': 1},
                               origin='synthetic')

    def test_non_image_source_is_rejected(self):
        (self.root / 'pretend.png').write_bytes(b'not an image')
        with self.assertRaises(ValueError):
            video.build_source(self.root, ['pretend.png'],
                               fps={'numerator': 24, 'denominator': 1},
                               origin='synthetic')

    def test_high_precision_png_is_rejected_before_lossy_pillow_decode(self):
        def chunk(kind, payload):
            return (struct.pack('>I', len(payload)) + kind + payload +
                    struct.pack('>I', zlib.crc32(kind + payload)))
        for color_type, pixel in ((2, b'\x12\x34\x56\x78\x9a\xbc'),
                                  (6, b'\x12\x34\x56\x78\x9a\xbc\xff\x00')):
            # Real 16-bit PNGs; ff00 alpha is nonopaque but Pillow presents
            # it as 255 after reducing precision to RGBA8.
            raw = b'\x00' + pixel * 2
            png = (b'\x89PNG\r\n\x1a\n' +
                   chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 16, color_type, 0, 0, 0)) +
                   chunk(b'IDAT', zlib.compress(raw * 2)) + chunk(b'IEND', b''))
            path = self.root / ('precision-%d.png' % color_type)
            path.write_bytes(png)
            with self.subTest(color_type=color_type):
                with Image.open(path) as im:
                    im.load()
                    self.assertIn(im.mode, ('RGB', 'RGBA'))
                with self.assertRaisesRegex(ValueError, '8-bit'):
                    video.build_source(self.root, [path.name],
                                       fps={'numerator': 24, 'denominator': 1})

    def test_rgb_png_transparency_is_not_silently_discarded(self):
        path = self.root / 'transparent-rgb.png'
        Image.new('RGB', (2, 2), (10, 20, 30)).save(path, transparency=(10, 20, 30))
        with Image.open(path) as im:
            self.assertEqual(im.mode, 'RGB')
            self.assertEqual(im.convert('RGBA').getchannel('A').getextrema(), (0, 0))
        with self.assertRaisesRegex(ValueError, 'transparency'):
            video.build_source(self.root, [path.name], fps={'numerator': 24, 'denominator': 1})

    def test_mismatched_frame_dimensions_are_rejected(self):
        Image.new('RGB', (32, 48)).save(self.root / self.paths[-1])
        with self.assertRaises(ValueError):
            video.build_source(self.root, self.paths,
                               fps={'numerator': 24, 'denominator': 1},
                               origin='synthetic')

    def test_missing_tools_are_reported_as_unavailable(self):
        with patch('silent_video.shutil.which', return_value=None):
            report = video.capabilities()
        self.assertIn('unavailable', json.dumps(report).lower())

    def test_huge_integer_fails_as_contract_error(self):
        request = deepcopy(self.request)
        request['limits']['timeout_seconds'] = 10**1000
        with self.assertRaises(ValueError):
            video.validate_request(request)

    def test_profile_schema_rejects_mixed_codec_and_container(self):
        import jsonschema
        invalid = deepcopy(video.PROFILES['h264-mp4'])
        invalid['container'] = 'webm'
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid, video.schemas()['profile'])

    def test_resource_limits_reject_unbounded_or_nonfinite_values(self):
        for key, value in [('max_output_bytes', 0), ('max_log_bytes', -1),
                           ('timeout_seconds', float('inf')),
                           ('timeout_seconds', True)]:
            request = deepcopy(self.request)
            request['limits'][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    video.validate_request(request)


if __name__ == '__main__':
    unittest.main()
