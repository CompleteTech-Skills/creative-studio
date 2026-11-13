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
    raise RuntimeError('HATCH_SILENT_MEDIA_TEST requires real FFmpeg/ffprobe and tested secure publication support')


class SilentContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='silent-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
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

    def test_descriptor_destinations_reject_nonbasename_and_invalid_handles(self):
        for name in ('../escape.mp4', 'nested/movie.mp4', 'CON.mp4', 'movie:.mp4', 'movie.mp4 '):
            with self.subTest(name=name), self.assertRaises(ValueError):
                video.encode(self.request, self.root, name, destination_dir_fd=-1)
        with self.assertRaises(ValueError):
            video.encode(self.request, self.root, 'movie.mp4', destination_dir_fd=True)
        descriptor = os.open(self.root / self.paths[0], os.O_RDONLY)
        try:
            with self.assertRaises(ValueError):
                video.encode(self.request, self.root, 'movie.mp4', destination_dir_fd=descriptor)
        finally:
            os.close(descriptor)


@unittest.skipUnless(MEDIA_AVAILABLE, 'FFmpeg/ffprobe or tested secure publication adapter unavailable')
class SilentMedia(SilentContracts):
    def ffmpeg(self, *arguments):
        result = subprocess.run([shutil.which('ffmpeg'), '-hide_banner',
                                 '-loglevel', 'error', '-nostdin', '-y',
                                 *map(str, arguments)], capture_output=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def independent_probe(self, path):
        result = subprocess.run([shutil.which('ffprobe'), '-v', 'error',
                                 '-show_streams', '-show_format', '-count_frames',
                                 '-of', 'json', str(path)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return json.loads(result.stdout)

    def independent_decode(self, path):
        self.ffmpeg('-xerror', '-err_detect', 'explode', '-i', path,
                    '-map', '0:v:0', '-f', 'null', '-')

    def assert_receipt(self, receipt, path):
        self.assertIs(receipt['verified'], True)
        self.assertEqual(receipt['video_stream_count'], 1)
        self.assertEqual(receipt['audio_stream_count'], 0)
        self.assertEqual(receipt['artifact_sha256'],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        for key in ('source_manifest_sha256', 'request_sha256'):
            self.assertRegex(receipt[key], r'^[0-9a-f]{64}$')

    def test_private_destination_fd_survives_parent_swap_through_both_passes(self):
        request = video.make_request(self.source, 'vp9-webm', encoding={
            'speed': 6, 'threads': 1, 'gop': 24, 'rate_control': {'mode': 'two-pass', 'bitrate': 100000}})
        foreign = []
        with video._pin_directory(self.root) as parent_fd:
            with video._private_stage(parent_fd, prefix='.job-attempt-') as (private, private_fd):
                (private / 'attempt').mkdir()
                attempt_fd = os.open('attempt', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=private_fd)
                try:
                    def replace_parent(event):
                        if event['phase'] == 'encode-pass-1' and not foreign:
                            wrapper = Path(os.readlink('/proc/self/fd/' + str(private_fd)))
                            wrapper.rename(wrapper.with_name(wrapper.name + '-retained'))
                            replacement = wrapper / 'attempt'; replacement.mkdir(parents=True)
                            sentinel = replacement / 'movie.webm'; sentinel.write_bytes(b'unrelated replacement')
                            foreign.append(sentinel)
                    receipt = video.encode(request, self.root, 'movie.webm', destination_dir_fd=attempt_fd,
                                           progress=replace_parent)
                    artifact = private / 'attempt/movie.webm'
                    video.validate_receipt(receipt, artifact, request, self.root)
                    self.assertEqual(len(receipt['passes']), 2)
                finally:
                    os.close(attempt_fd)
        self.assertEqual(foreign[0].read_bytes(), b'unrelated replacement')

    def test_actual_h264_all_required_rates_and_adjacent_wav(self):
        with wave.open(str(self.root / 'speech.wav'), 'wb') as audio:
            audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x00\x00' * 48000)
        for numerator, denominator in ((24, 1), (25, 1), (30, 1),
                                       (60, 1), (30000, 1001)):
            with self.subTest(fps=(numerator, denominator)):
                source = video.build_source(self.root, self.paths,
                    fps={'numerator': numerator, 'denominator': denominator},
                    origin='synthetic')
                request = video.make_request(source)
                output = self.root / ('rate-%s-%s.mp4' % (numerator, denominator))
                receipt = video.encode(request, self.root, output)
                self.assert_receipt(receipt, output)
                data = self.independent_probe(output)
                self.assertEqual(len(data['streams']), 1)
                stream = data['streams'][0]
                self.assertEqual(stream['codec_type'], 'video')
                self.assertEqual(stream['codec_name'], 'h264')
                self.assertEqual(stream['pix_fmt'], 'yuv420p')
                self.assertEqual(int(stream['nb_read_frames']), len(self.paths))
                self.assertEqual(Fraction(stream['avg_frame_rate']),
                                 Fraction(numerator, denominator))
                self.independent_decode(output)
                independently_verified = video.verify_export(output, request,
                                                               root=self.root)
                self.assert_receipt(independently_verified, output)

    def test_actual_vp9_webm_passes_independent_silent_verifier(self):
        request = video.make_request(self.source, profile='vp9-webm')
        output = self.root / 'silent.webm'
        video.encode(request, self.root, output)
        self.assert_receipt(video.verify_export(output, request, root=self.root), output)
        self.independent_decode(output)

    def test_actual_optional_av1_all_required_rational_rates(self):
        capability = video.capabilities()['profiles']['av1-webm']
        if capability['status'] != 'supported':
            self.skipTest('installed FFmpeg lacks libaom AV1 encoding/decoding/WebM support')
        for numerator, denominator in ((24, 1), (25, 1), (30, 1), (60, 1),
                                       (24000, 1001), (30000, 1001), (60000, 1001)):
            with self.subTest(fps=(numerator, denominator)):
                source = video.build_source(self.root, self.paths,
                    fps={'numerator': numerator, 'denominator': denominator})
                request = video.make_request(source, 'av1-webm')
                output = self.root / ('av1-%d-%d.webm' % (numerator, denominator))
                receipt = video.encode(request, self.root, output)
                self.assert_receipt(receipt, output)
                probe = self.independent_probe(output)
                self.assertEqual([stream['codec_type'] for stream in probe['streams']], ['video'])
                self.assertEqual(probe['streams'][0]['codec_name'], 'av1')
                self.assertEqual(receipt['frame_count'], len(self.paths))
                self.independent_decode(output)
        with self.assertRaises(ValueError):
            video.verify_export(output, video.make_request(source, 'vp9-webm'))
        source_60 = video.build_source(self.root, self.paths, {'numerator': 60, 'denominator': 1})
        wrong = self.root / 'wrong-av1-60.webm'
        video.encode(video.make_request(source_60, 'av1-webm'), self.root, wrong)
        # At six frames, 60 and 60000/1001 can share the same millisecond PTS.
        # The track's real nanosecond duration must still distinguish them.
        with self.assertRaisesRegex(ValueError, 'rational frame rate'):
            video.verify_export(wrong, request)

    def test_zero_valued_aac_track_is_not_silent_delivery(self):
        silent = self.root / 'video.mp4'
        video.encode(self.request, self.root, silent)
        output = self.root / 'zero-aac.mp4'
        self.ffmpeg('-i', silent, '-f', 'lavfi', '-i',
                    'anullsrc=r=48000:cl=mono', '-map', '0:v:0', '-map', '1:a:0',
                    '-c:v', 'copy', '-c:a', 'aac', '-shortest', output)
        self.assertEqual(len(self.independent_probe(output)['streams']), 2)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request)

    def test_zero_valued_opus_track_is_not_silent_delivery(self):
        request = video.make_request(self.source, profile='vp9-webm')
        silent = self.root / 'video.webm'
        video.encode(request, self.root, silent)
        output = self.root / 'zero-opus.webm'
        self.ffmpeg('-i', silent, '-f', 'lavfi', '-i',
                    'anullsrc=r=48000:cl=mono', '-map', '0:v:0', '-map', '1:a:0',
                    '-c:v', 'copy', '-c:a', 'libopus', '-shortest', output)
        self.assertEqual(len(self.independent_probe(output)['streams']), 2)
        with self.assertRaises(ValueError):
            video.verify_export(output, request)

    def test_extra_video_stream_is_rejected(self):
        silent = self.root / 'video.mp4'
        video.encode(self.request, self.root, silent)
        output = self.root / 'two-video.mp4'
        self.ffmpeg('-i', silent, '-map', '0:v:0', '-map', '0:v:0', '-c', 'copy', output)
        self.assertEqual(len(self.independent_probe(output)['streams']), 2)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request)

    def test_truncated_and_corrupt_artifacts_are_rejected(self):
        silent = self.root / 'video.mp4'
        video.encode(self.request, self.root, silent)
        data = silent.read_bytes()
        for name, content in [('truncated.mp4', data[:len(data) // 2]),
                              ('corrupt.mp4', b'not a media artifact')]:
            path = self.root / name
            path.write_bytes(content)
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    video.verify_export(path, self.request)

    def test_wrong_source_frame_count_is_rejected(self):
        output = self.root / 'video.mp4'
        video.encode(self.request, self.root, output)
        shorter = video.build_source(self.root, self.paths[:-1],
            fps={'numerator': 24, 'denominator': 1}, origin='synthetic')
        with self.assertRaises(ValueError):
            video.verify_export(output, video.make_request(shorter))

    def test_wrong_fps_is_rejected(self):
        output = self.root / 'video.mp4'
        video.encode(self.request, self.root, output)
        other = video.build_source(self.root, self.paths,
            fps={'numerator': 25, 'denominator': 1}, origin='synthetic')
        with self.assertRaises(ValueError):
            video.verify_export(output, video.make_request(other))

    def test_real_presentation_timestamp_gap_is_rejected(self):
        output = self.root / 'gap.mp4'
        self.ffmpeg('-framerate', '24', '-i', self.root / '%06d.png',
                    '-vf', r"setpts=if(gte(N\,3)\,PTS+2/TB\,PTS)",
                    '-fps_mode', 'vfr', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-frames:v', '6', output)
        self.assertEqual(int(self.independent_probe(output)['streams'][0]['nb_read_frames']), 6)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request)

    def test_wrong_sample_aspect_ratio_is_rejected(self):
        output = self.root / 'stretched.mp4'
        self.ffmpeg('-framerate', '24', '-i', self.root / '%06d.png',
                    '-vf', 'setsar=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-frames:v', '6', output)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request)

    def test_unrequested_color_metadata_is_rejected(self):
        output = self.root / 'tagged.mp4'
        self.ffmpeg('-framerate', '24', '-i', self.root / '%06d.png',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-color_primaries', 'bt2020',
                    '-color_trc', 'smpte2084', '-colorspace', 'bt2020nc', '-frames:v', '6', output)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request)

    def test_corrupt_receipt_cannot_claim_verification(self):
        output = self.root / 'video.mp4'
        receipt = video.encode(self.request, self.root, output)
        video.validate_receipt(receipt, output, self.request, root=self.root)
        for key, value in [('artifact_sha256', '0' * 64), ('audio_stream_count', 1),
                           ('verified', False), ('frame_count', 999)]:
            corrupt = deepcopy(receipt)
            corrupt[key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    video.validate_receipt(corrupt, output, self.request, root=self.root)
        corrupt = deepcopy(receipt)
        data = json.loads(corrupt['probe']['stdout'])
        data['streams'][0]['width'] = 128
        corrupt['probe']['stdout'] = json.dumps(data)
        with self.assertRaises(ValueError):
            video.validate_receipt(corrupt, output, self.request, root=self.root)

    def test_legacy_render_video_cli_keeps_aac_audio(self):
        from test_production_render import fixture_project
        project = self.root / 'legacy-project'
        fixture_project(project)
        destination = self.root / 'legacy-render'
        result = subprocess.run([sys.executable,
            str(Path(__file__).resolve().parents[1] / 'scripts' / 'production.py'),
            'render', '--project', str(project), '--shot', 'first',
            '--destination', str(destination), '--video'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.independent_probe(destination / 'review.mp4')
        self.assertEqual([stream['codec_name'] for stream in data['streams']], ['h264', 'aac'])
        self.assertTrue((destination / 'mux-receipt.json').is_file())
        self.assertTrue((destination / 'audio.wav').is_file())

    def test_wrong_dimensions_are_rejected(self):
        output = self.root / 'wrong-dimensions.mp4'
        self.ffmpeg('-framerate', '24', '-i', self.root / '%06d.png',
                    '-vf', 'scale=32:24', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-frames:v', '6', output)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request)

    def test_ffprobe_nonzero_exit_is_rejected(self):
        output = self.root / 'video.mp4'
        video.encode(self.request, self.root, output)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request, ffprobe=sys.executable)

    def test_stale_source_is_rejected_without_publishing(self):
        output = self.root / 'stale.mp4'
        Image.new('RGB', (64, 48), (0, 0, 0)).save(self.root / self.paths[0])
        with self.assertRaises(ValueError):
            video.encode(self.request, self.root, output)
        self.assertFalse(output.exists())

    def test_missing_source_is_rejected_without_publishing(self):
        output = self.root / 'missing.mp4'
        (self.root / self.paths[0]).unlink()
        with self.assertRaises(ValueError):
            video.encode(self.request, self.root, output)
        self.assertFalse(output.exists())

    def test_cancellation_never_publishes_partial_output(self):
        output = self.root / 'cancelled.mp4'
        with self.assertRaises(ValueError):
            video.encode(self.request, self.root, output, cancel=lambda: True)
        self.assertFalse(output.exists())

    def test_existing_destination_is_preserved(self):
        output = self.root / 'existing.mp4'
        output.write_bytes(b'pre-existing unrelated user data')
        with self.assertRaises((ValueError, FileExistsError)):
            video.encode(self.request, self.root, output)
        self.assertEqual(output.read_bytes(), b'pre-existing unrelated user data')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux descriptor path adapter')
    def test_parent_symlink_swap_cannot_redirect_publication(self):
        destination_parent = self.root/'delivery'
        destination_parent.mkdir()
        outside = self.root/'outside'
        outside.mkdir()
        moved = self.root/'pinned-delivery'
        original = video._rename_no_replace
        def swap(source, target, **kwargs):
            destination_parent.rename(moved)
            destination_parent.symlink_to(outside, target_is_directory=True)
            return original(source, target, **kwargs)
        with patch.object(video, '_rename_no_replace', side_effect=swap):
            receipt = video.encode(self.request, self.root, destination_parent/'film.mp4')
        self.assertFalse((outside/'film.mp4').exists())
        self.assertEqual(hashlib.sha256((moved/'film.mp4').read_bytes()).hexdigest(), receipt['artifact_sha256'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_verified_stage_replacement_cannot_publish_or_delete_foreign_bytes(self):
        output = self.root/'stage-race.mp4'
        original_verify = video.verify_export
        state = {'verified': False, 'replaced': False}
        replacement = []
        def verified(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            state['verified'] = True
            stage = next(self.root.glob('.silent-export-*'))
            stage.rename(self.root/'original-verified-stage')
            stage.mkdir()
            (stage/'output.partial.mp4').write_bytes(b'unverified replacement movie')
            (stage/'foreign.txt').write_bytes(b'unrelated original content')
            replacement.append(stage)
            state['replaced'] = True
            return result
        with patch.object(video, 'verify_export', side_effect=verified):
            receipt = video.encode(self.request, self.root, output)
        self.assertTrue(state['replaced'])
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), receipt['artifact_sha256'])
        self.assertEqual((replacement[0]/'output.partial.mp4').read_bytes(), b'unverified replacement movie')
        self.assertEqual((replacement[0]/'foreign.txt').read_bytes(), b'unrelated original content')

    def test_no_cancellation_callback_after_final_byte_verification(self):
        original_verify = video.verify_export
        state = {'verified': False}
        def verified(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            state['verified'] = True
            return result
        def cancel():
            if state['verified']:
                self.fail('user callback ran between final byte verification and publication')
            return False
        with patch.object(video, 'verify_export', side_effect=verified):
            receipt = video.encode(self.request, self.root, self.root/'no-late-callback.mp4', cancel=cancel)
        self.assertTrue(receipt['verified'])

    def test_output_size_budget_prevents_publication(self):
        output = self.root / 'oversize.mp4'
        request = deepcopy(self.request)
        request['limits']['max_output_bytes'] = 1024
        with self.assertRaises(ValueError):
            video.encode(request, self.root, output)
        self.assertFalse(output.exists())



class DescriptorPublication(unittest.TestCase):
    @unittest.skipUnless(video.secure_publication_supported(), 'Linux descriptor path adapter')
    def test_stage_cleanup_does_not_follow_replaced_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root/'outside'
            outside.mkdir()
            sentinel = outside/'original.txt'
            sentinel.write_bytes(b'unrelated original bytes')
            with video._pin_directory(root) as parent_fd:
                with video._private_stage(parent_fd, prefix='.silent-export-') as (stage, descriptor):
                    (stage/'owned.txt').write_bytes(b'owned temporary bytes')
                    name = next(root.glob('.silent-export-*'))
                    name.rename(root/'moved-owned-stage')
                    name.symlink_to(outside, target_is_directory=True)
            self.assertEqual(sentinel.read_bytes(), b'unrelated original bytes')
            self.assertEqual(list((root/'moved-owned-stage').iterdir()), [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX directory descriptors')
    def test_shared_atomic_publisher_pins_both_parents(self):
        from atomic_publish import _rename_no_replace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination, outside = root/'source', root/'destination', root/'outside'
            for path in (source, destination, outside): path.mkdir()
            (source/'part').write_bytes(b'exact verified bytes')
            oldfd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
            newfd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                destination.rename(root/'moved')
                destination.symlink_to(outside, target_is_directory=True)
                _rename_no_replace('part', 'final', source_dir_fd=oldfd, destination_dir_fd=newfd)
                self.assertEqual((root/'moved'/'final').read_bytes(), b'exact verified bytes')
                self.assertFalse((outside/'final').exists())
                (source/'second').write_bytes(b'keep source')
                (root/'moved'/'existing').mkdir()
                with self.assertRaises(FileExistsError):
                    _rename_no_replace('second', 'existing', source_dir_fd=oldfd, destination_dir_fd=newfd)
                self.assertEqual((source/'second').read_bytes(), b'keep source')
                with self.assertRaises(ValueError):
                    _rename_no_replace('../escape', 'final', source_dir_fd=oldfd, destination_dir_fd=newfd)
                with self.assertRaises(ValueError):
                    _rename_no_replace('second', 'final', source_dir_fd=oldfd)
            finally:
                os.close(oldfd); os.close(newfd)

if __name__ == '__main__':
    unittest.main()
