"""Portable #223 acceptance contracts; every media probe here is a MOCK.

Synthetic inventory metadata and bounded probe transcripts exercise admission,
not PNG provenance, native encoding, Blender execution or zero-audio media proof.
"""
from contextlib import contextmanager, ExitStack, nullcontext
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_video as video


# Independent CLI-shaped transcripts, never captured native execution evidence.
MOCK_LISTS = {
    '-encoders': ('Encoders:\n V..... = Video\n A..... = Audio\n'
                  ' .F.... = Frame-level multithreading\n'
                  ' ..S... = Slice-level multithreading\n'
                  ' ...X.. = Codec is experimental\n'
                  ' ....B. = Supports draw_horiz_band\n'
                  ' .....D = Supports direct rendering method 1\n ------\n'
                  ' V....D libx264 Mock H.264 encoder\n'
                  ' V..... libvpx-vp9 Mock VP9 encoder\n'),
    '-muxers': ('File formats:\n D. = Demuxing supported\n'
                ' .E = Muxing supported\n --\n'
                '  E mp4 Mock MP4 muxer\n  E webm Mock WebM muxer\n'),
    '-pix_fmts': ('Pixel formats:\n I.... = Supported Input format for conversion\n'
                  ' .O... = Supported Output format for conversion\n'
                  ' ..H.. = Hardware accelerated format\n'
                  ' ...P. = Paletted format\n'
                  ' ....B = Bitstream format\n'
                  'FLAGS NAME NB_COMPONENTS BITS_PER_PIXEL BIT_DEPTHS\n-----\n'
                  'IO... yuv420p 3 12 8-8-8\n'),
}
EXPECTED = {'h264-mp4': ('libx264', 'mp4', '.mp4'),
            'vp9-webm': ('libvpx-vp9', 'webm', '.webm')}


class SilentCapabilityContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='silent-capability-mock-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = {}
        for name in ('ffmpeg', 'ffprobe'):
            stub = self.root / name
            stub.write_bytes(b'MOCK ONLY: never execute this file')
            stub.chmod(0o700)
            self.tools[name] = str(stub.resolve())
        # Metadata is structurally valid but makes no on-disk media claim.
        source = {'schema_version': 1, 'kind': 'silent-frame-source',
                  'origin': 'synthetic', 'format': 'png', 'precision': 8,
                  'alpha': 'opaque', 'color': 'untagged-sdr',
                  'width': 2, 'height': 2,
                  'fps': {'numerator': 24000, 'denominator': 1001},
                  'frames': [{'index': 0, 'path': '000000.png',
                              'sha256': '0' * 64, 'bytes': 1}]}
        self.request = video.make_request(source)
        # A harness mistake must fail, never launch an executable stub/native tool.
        self.addCleanup(patch.stopall)
        patch.object(video.subprocess, 'Popen',
                     side_effect=AssertionError('unexpected real subprocess')).start()

    @contextmanager
    def mock_probes(self, *, lists=None, missing=(), failure=None, publication=True):
        """Mock only subprocess/discovery/platform boundaries, not capabilities()."""
        transcripts = dict(MOCK_LISTS if lists is None else lists)
        calls = []

        def run(command, limits, **kwargs):
            argv = tuple(command)
            allowed = {(self.tools[name], '-version') for name in self.tools}
            allowed.update((self.tools['ffmpeg'], '-hide_banner', flag)
                           for flag in MOCK_LISTS)
            self.assertIn(argv, allowed, 'encoding/fallback is forbidden in this harness')
            self.assertFalse(kwargs)
            self.assertGreater(limits['timeout_seconds'], 0)
            self.assertLessEqual(limits['timeout_seconds'], 300)
            self.assertGreater(limits['max_log_bytes'], 0)
            self.assertLessEqual(limits['max_log_bytes'], 4 * 1024**2)
            calls.append(argv)
            if argv == failure:
                raise ValueError('mock probe failure')
            stdout = ('MOCK version 1\n' if argv[-1] == '-version'
                      else transcripts[argv[-1]])
            return {'command': list(command), 'exit_status': 0,
                    'stdout': stdout, 'stderr': ''}

        with ExitStack() as stack:
            which = stack.enter_context(patch.object(
                video.shutil, 'which',
                side_effect=lambda name: None if name in missing else self.tools[name]))
            stack.enter_context(patch.object(video, '_run', side_effect=run))
            stack.enter_context(patch.object(video, 'secure_publication_supported',
                                             return_value=publication))
            yield calls, which

    def assert_refused_before_encoding(self, request=None, message='no fallback'):
        request = self.request if request is None else request
        destination = self.root / ('refused' + EXPECTED[request['profile']][2])
        with ExitStack() as stack:
            # Admission-only mock: no descriptor is consumed after refusal.
            stack.enter_context(patch.object(video, '_pin_directory',
                                             return_value=nullcontext(123)))
            guards = [stack.enter_context(patch.object(
                video, name, side_effect=AssertionError('refusal happened too late')))
                for name in ('_private_stage', '_snapshot', 'verify_export',
                             '_rename_no_replace')]
            with self.assertRaisesRegex(ValueError, message):
                video.encode(request, self.root, destination)
            for guard in guards:
                guard.assert_not_called()
        self.assertFalse(destination.exists())

    def test_supported_controls_probe_both_versions_and_all_capability_lists(self):
        with self.mock_probes() as (calls, which):
            report = video.capabilities()
        self.assertEqual(report['status'], 'available')
        self.assertEqual(report['audio_policy'], 'forbidden')
        self.assertEqual(set(report['profiles']), set(EXPECTED))
        for name, (encoder, container, _) in EXPECTED.items():
            with self.subTest(profile=name):
                capability = report['profiles'][name]
                self.assertEqual(capability['status'], 'supported')
                self.assertEqual((capability['encoder'], capability['container'],
                                  capability['pixel_format']), (encoder, container, 'yuv420p'))
                request = video.make_request(self.request['source'], profile=name)
                self.assertEqual(video.validate_request(request), request)
                self.assertEqual(request['audio'], {'policy': 'forbidden'})
        self.assertEqual(set(calls), {
            (self.tools['ffmpeg'], '-version'), (self.tools['ffprobe'], '-version'),
            *((self.tools['ffmpeg'], '-hide_banner', flag) for flag in MOCK_LISTS)})
        self.assertEqual(len(calls), 5)
        self.assertEqual([call.args for call in which.call_args_list],
                         [('ffmpeg',), ('ffprobe',)])
        self.assertEqual(report['raw_capabilities'], MOCK_LISTS)
        self.assertEqual(set(report['evidence']), {'ffmpeg', 'ffprobe'})

    def test_each_missing_encoder_muxer_or_pixel_format_refuses_without_fallback(self):
        rows = [
            ('-encoders', ' V..... libvpx-vp9 Mock VP9\n', ('h264-mp4',)),
            ('-encoders', ' V..... libx264 Mock H264\n', ('vp9-webm',)),
            ('-muxers', '  E webm Mock WebM\n', ('h264-mp4',)),
            ('-muxers', '  E mp4 Mock MP4\n', ('vp9-webm',)),
            ('-pix_fmts', 'IO... rgb24 3 24 8-8-8\n', tuple(EXPECTED)),
        ]
        for flag, transcript, unsupported in rows:
            with self.subTest(flag=flag, unsupported=unsupported):
                lists = {**MOCK_LISTS, flag: transcript}
                with self.mock_probes(lists=lists):
                    report = video.capabilities()
                    self.assertEqual(report['status'], 'available')
                    for name in EXPECTED:
                        self.assertEqual(report['profiles'][name]['status'],
                                         'unsupported' if name in unsupported else 'supported')
                    for name in unsupported:
                        self.assert_refused_before_encoding(
                            video.make_request(self.request['source'], profile=name))

    def test_wrong_media_type_or_capability_direction_is_unsupported(self):
        rows = [
            ('-encoders', ' A..... libx264 Mock audio encoder\n'
             ' V..... libvpx-vp9 Mock video encoder\n', ('h264-mp4',)),
            ('-muxers', ' D  mp4 Mock demuxer only\n'
             '  E webm Mock muxer\n', ('h264-mp4',)),
            ('-pix_fmts', 'I.... yuv420p 3 12 8-8-8\n', tuple(EXPECTED)),
        ]
        for flag, transcript, unsupported in rows:
            with self.subTest(flag=flag), self.mock_probes(
                    lists={**MOCK_LISTS, flag: transcript}):
                report = video.capabilities()
                self.assertEqual(report['status'], 'available')
                for name in EXPECTED:
                    self.assertEqual(report['profiles'][name]['status'],
                                     'unsupported' if name in unsupported else 'supported')
                for name in unsupported:
                    self.assert_refused_before_encoding(
                        video.make_request(self.request['source'], profile=name))

    def test_output_only_pixel_format_and_comma_muxer_aliases_are_supported(self):
        lists = {**MOCK_LISTS,
                 '-muxers': ('File formats:\n D. = Demuxing supported\n'
                             ' .E = Muxing supported\n --\n'
                             '  E mov,mp4,m4a,3gp,3g2,mj2 Mock MOV/MP4 aliases\n'
                             '  E matroska,webm Mock WebM aliases\n'),
                 '-pix_fmts': MOCK_LISTS['-pix_fmts'].replace('IO... yuv420p',
                                                           '.O... yuv420p')}
        with self.mock_probes(lists=lists):
            report = video.capabilities()
        self.assertEqual(report['status'], 'available')
        for name in EXPECTED:
            with self.subTest(profile=name):
                self.assertEqual(report['profiles'][name]['status'], 'supported')

    def test_ffmpeg_and_ffprobe_are_independently_required(self):
        for missing in (('ffmpeg',), ('ffprobe',), ('ffmpeg', 'ffprobe')):
            with self.subTest(missing=missing), self.mock_probes(missing=missing) as (calls, _):
                report = video.capabilities()
                self.assertEqual(report['status'], 'unavailable')
                self.assertIn(missing[0] + ' unavailable', report['reason'])
                self.assertFalse(report['profiles'])
                self.assertFalse(calls)
                self.assert_refused_before_encoding()

    def test_every_failed_version_or_list_probe_is_unavailable(self):
        commands = [(self.tools[name], '-version') for name in self.tools]
        commands += [(self.tools['ffmpeg'], '-hide_banner', flag) for flag in MOCK_LISTS]
        for command in commands:
            with self.subTest(command=command), self.mock_probes(failure=command):
                report = video.capabilities()
                self.assertEqual(report['status'], 'unavailable')
                self.assertIn('mock probe failure', report['reason'])
                self.assertFalse(report['profiles'])
                self.assert_refused_before_encoding()

    def test_missing_explicit_binary_does_not_fall_back_to_path(self):
        for name in self.tools:
            with self.subTest(binary=name), self.mock_probes() as (calls, which):
                report = video.capabilities(**{name: str(self.root / 'absent-tool')})
                self.assertEqual(report['status'], 'unavailable')
                self.assertIn(name + ' unavailable', report['reason'])
                self.assertNotIn((name,), [call.args for call in which.call_args_list])
                self.assertFalse(calls)

    def test_publication_unavailable_reports_unsupported_and_refuses(self):
        with self.mock_probes(publication=False):
            report = video.capabilities()
            self.assertEqual(report['status'], 'available')
            for name in EXPECTED:
                self.assertEqual(report['profiles'][name]['status'], 'unsupported')
            self.assert_refused_before_encoding()

    def test_capability_names_must_be_exact_advertised_tokens(self):
        # A suffixed name or a prose mention cannot advertise the desired name.
        for flag, token in (('-encoders', 'libx264'), ('-muxers', 'mp4'),
                            ('-pix_fmts', 'yuv420p')):
            for replacement in (token + '-disabled', 'unrelated description mentions ' + token):
                lists = {**MOCK_LISTS, flag: MOCK_LISTS[flag].replace(token, replacement)}
                with self.subTest(flag=flag, replacement=replacement), self.mock_probes(lists=lists):
                    report = video.capabilities()
                    self.assertEqual(report['profiles']['h264-mp4']['status'], 'unsupported')
                    self.assert_refused_before_encoding()

    def test_injected_media_fields_and_audio_enablement_fail_before_probes(self):
        mutations = [('codec', 'aac'), ('container', 'matroska'),
                     ('pixel_format', 'yuv444p'), ('ffmpeg_args', ['-map', '0:a']),
                     ('audio_input', 'speech.wav')]
        for key, value in mutations:
            request = deepcopy(self.request)
            request[key] = value
            with self.subTest(field=key), patch.object(video, '_run') as run:
                self.assert_refused_before_encoding(request, message='.')
                run.assert_not_called()
        for audio in ({'policy': 'allowed'}, {'policy': 'muted'},
                      {'policy': 'forbidden', 'args': ['-an', '0']}, {}):
            request = deepcopy(self.request)
            request['audio'] = audio
            with self.subTest(audio=audio), patch.object(video, '_run') as run:
                self.assert_refused_before_encoding(request, message='.')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
