"""Real media and adversarial acceptance for the separate video-only export path.

All input images in this module are synthetic test fixtures, never production
art. The integration cases execute the installed FFmpeg and ffprobe binaries.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import errno
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import silent_video as video

MEDIA_AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and video.secure_publication_supported())
if os.environ.get('HATCH_SILENT_MEDIA_TEST') == '1' and not MEDIA_AVAILABLE:
    raise RuntimeError('HATCH_SILENT_MEDIA_TEST requires real FFmpeg and ffprobe')


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_silent_video as core

@unittest.skipUnless(MEDIA_AVAILABLE, 'FFmpeg/ffprobe integration tools unavailable')
class SilentMedia(core.SilentContracts):
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

    def test_actual_avc_major_mp4_passes_independent_silent_verifier(self):
        source = video.build_source(self.root, self.paths[:1], fps=self.source['fps'], origin='synthetic')
        request = video.make_request(source)
        output = self.root / 'avc-major.mp4'
        self.ffmpeg('-framerate', '24', '-i', self.root / '%06d.png', '-frames:v', '1',
                    '-an', '-vf', 'setsar=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-movflags', '+default_base_moof+frag_keyframe+empty_moov', '-brand', 'avc1', output)
        probe = self.independent_probe(output)
        tags = probe['format']['tags']
        self.assertEqual(tags['major_brand'], 'avc1')
        self.assertIn('iso6', tags['compatible_brands'])
        self.assertIn('mp41', tags['compatible_brands'])
        self.assertEqual([(s['codec_type'], s['codec_name'], s['nb_read_frames']) for s in probe['streams']],
                         [('video', 'h264', '1')])
        self.independent_decode(output)
        receipt = video.verify_export(output, request, root=self.root)
        self.assert_receipt(receipt, output)
        self.assertEqual(receipt['frame_count'], 1)
        self.assertEqual(receipt['decode']['exit_status'], 0)

    def test_actual_fragmented_mp4_with_large_leading_free_passes_public_verifier(self):
        source = video.build_source(self.root, self.paths[:1], fps=self.source['fps'], origin='synthetic')
        request = video.make_request(source)
        original = self.root / 'fragmented-control.mp4'
        output = self.root / 'fragmented-leading-free.mp4'
        self.ffmpeg('-framerate', '24', '-i', self.root / '%06d.png', '-frames:v', '1',
                    '-an', '-vf', 'setsar=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-movflags', '+default_base_moof+frag_keyframe+empty_moov', '-brand', 'avc1', original)
        # Fragment offsets are relative to moof; the leading padding does not
        # move media relative to its fragment's declared base.
        output.write_bytes(core.mp4_box(b'free', b'\0' * 65528) + original.read_bytes())
        control = self.independent_probe(original)
        observed = self.independent_probe(output)
        self.assertEqual(observed['streams'], control['streams'])
        self.independent_decode(output)
        control_receipt = video.verify_export(original, request, root=self.root)
        receipt = video.verify_export(output, request, root=self.root)
        self.assert_receipt(receipt, output)
        self.assertEqual(receipt['frame_count'], 1)
        self.assertEqual(receipt['timeline']['stdout'], control_receipt['timeline']['stdout'])
        for stage in ('probe', 'timeline', 'decode'):
            self.assertEqual(receipt[stage]['exit_status'], 0)
        request_path = self.root / 'fragmented-request.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        result = subprocess.run(
            [sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
             str(output), '--request', str(request_path), '--root', str(self.root), '--require-silent'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_receipt(json.loads(result.stdout), output)

    def test_actual_mp4_final_media_size_zero_passes_public_verifier(self):
        original = self.root / 'sized-media-control.mp4'
        output = self.root / 'media-to-eof.mp4'
        video.encode(self.request, self.root, original)
        data = bytearray(original.read_bytes())
        offset = 0
        while offset < len(data):
            size, kind = struct.unpack_from('>I4s', data, offset)
            self.assertGreaterEqual(size, 8)
            if kind == b'mdat':
                self.assertEqual(offset + size, len(data))
                data[offset:offset + 4] = b'\0' * 4
                break
            offset += size
        else:
            self.fail('control has no final media box')
        output.write_bytes(data)
        self.independent_decode(output)
        receipt = video.verify_export(output, self.request, root=self.root)
        self.assert_receipt(receipt, output)
        self.assertEqual(receipt['decode']['exit_status'], 0)

    def test_native_per_frame_fragmented_mp4_exceeding_base_box_budget(self):
        count = 2048
        frame = self.root / 'fragment-template.png'
        Image.new('RGB', (32, 32), 'black').save(frame)
        pixels = frame.read_bytes()
        paths = []
        for index in range(count):
            name = f'fragment-{index:06d}.png'
            (self.root / name).write_bytes(pixels)
            paths.append(name)
        source = video.build_source(self.root, paths,
                                    fps={'numerator': 24, 'denominator': 1}, origin='synthetic')
        request = video.make_request(source)
        output = self.root / 'fragmented.mp4'
        self.ffmpeg('-framerate', '24', '-i', self.root / 'fragment-%06d.png',
                    '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-vf', 'setsar=1', '-bf', '0',
                    '-movflags', 'empty_moov+default_base_moof+frag_every_frame', output)
        probe = self.independent_probe(output)
        self.assertEqual(len(probe['streams']), 1)
        self.assertEqual(int(probe['streams'][0]['nb_read_frames']), count)
        self.independent_decode(output)
        self.assert_receipt(video.verify_export(output, request, root=self.root), output)

    def test_distinct_containers_with_shared_probe_family_are_rejected(self):
        cases = (('vp9-webm', '.webm', 'matroska', '.mkv'),
                 ('h264-mp4', '.mp4', 'mov', '.mov'),
                 ('h264-mp4', '.mp4', '3gp', '.3gp'),
                 ('h264-mp4', '.mp4', '3g2', '.3g2'))
        for profile, extension, other_muxer, other_extension in cases:
            with self.subTest(profile=profile, actual=other_muxer):
                request = video.make_request(self.source, profile=profile)
                expected = self.root / ('expected-' + other_muxer + extension)
                other = self.root / ('other' + other_extension)
                video.encode(request, self.root, expected)
                self.assert_receipt(video.verify_export(expected, request, root=self.root), expected)
                self.ffmpeg('-i', expected, '-map', '0:v:0', '-c:v', 'copy', '-f', other_muxer, other)
                self.independent_decode(other)
                self.assertEqual(self.independent_probe(other)['format']['format_name'],
                                 self.independent_probe(expected)['format']['format_name'])
                with self.assertRaisesRegex(ValueError, 'encoded container mismatch') as caught:
                    video.verify_export(other, request, root=self.root)
                self.assertIs(caught.exception.evidence['verified'], False)
                self.assertEqual(caught.exception.evidence['audio_stream_count'], 0)

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

    def test_verify_export_cli_rejects_concatenated_aac_and_accepts_silent_video(self):
        contaminated = self.root / 'concatenated-aac.mp4'
        # Join two three-frame segments, each with 125 ms of silent audio.
        segments = (
            '[0:v]split[v0][v1];'
            '[v0]trim=end_frame=3,setpts=PTS-STARTPTS[first_video];'
            '[v1]trim=start_frame=3,setpts=PTS-STARTPTS[second_video];'
            '[1:a]asplit[a0][a1];'
            '[a0]atrim=duration=0.125,asetpts=PTS-STARTPTS[first_audio];'
            '[a1]atrim=duration=0.125,asetpts=PTS-STARTPTS[second_audio];'
            '[first_video][first_audio][second_video][second_audio]'
            'concat=n=2:v=1:a=1[joined_video][audio];'
            '[joined_video]setsar=1[video]'
        )
        self.ffmpeg('-framerate', '24', '-i', self.root / '%06d.png',
                    '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono',
                    '-filter_complex', segments, '-map', '[video]', '-map', '[audio]',
                    '-r', '24', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', contaminated)
        streams = self.independent_probe(contaminated)['streams']
        self.assertEqual([(stream['codec_type'], stream['codec_name'])
                          for stream in streams], [('video', 'h264'), ('audio', 'aac')])
        self.assertEqual(int(streams[0]['nb_read_frames']), len(self.paths))
        self.assertGreater(int(streams[1]['nb_read_frames']), 0)

        silent = self.root / 'silent-control.mp4'
        video.encode(self.request, self.root, silent)
        request_path = self.root / 'request.json'
        request_path.write_text(json.dumps(self.request), encoding='utf-8')

        def verify(path):
            return subprocess.run(
                [sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
                 str(path), '--request', str(request_path), '--root', str(self.root),
                 '--require-silent'],
                capture_output=True, text=True, timeout=30,
            )

        result = verify(contaminated)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        refusal = json.loads(result.stdout)
        self.assertEqual(refusal['schema_version'], 1)
        self.assertIs(refusal['verified'], False)
        self.assertEqual(refusal['error'], 'silent artifact requires exactly one '
                                          'video stream and zero audio/other streams')
        self.assertEqual(refusal['video_stream_count'], 1)
        self.assertEqual(refusal['audio_stream_count'], 1)
        self.assertEqual(refusal['other_stream_count'], 0)
        self.assertEqual(refusal['probe']['exit_status'], 0)

        result = verify(silent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assert_receipt(receipt, silent)
        self.assertEqual(receipt['schema_version'], 1)
        self.assertEqual(receipt['kind'], 'silent-video-verification')
        self.assertEqual(receipt['other_stream_count'], 0)
        self.assertEqual(receipt['frame_count'], len(self.paths))
        self.assertEqual(receipt['decode']['exit_status'], 0)
        self.assertEqual(receipt['decode']['stderr'], '')

    def test_zero_valued_opus_track_is_not_silent_delivery(self):
        request = video.make_request(self.source, profile='vp9-webm')
        silent = self.root / 'video.webm'
        video.encode(request, self.root, silent)
        output = self.root / 'zero-opus.webm'
        self.ffmpeg('-i', silent, '-f', 'lavfi', '-i',
                    'anullsrc=r=48000:cl=mono', '-map', '0:v:0', '-map', '1:a:0',
                    '-c:v', 'copy', '-c:a', 'libopus', '-shortest', output)
        streams = self.independent_probe(output)['streams']
        self.assertEqual(len(streams), 2)
        self.assertEqual([(stream['codec_type'], stream['codec_name'])
                          for stream in streams], [('video', 'vp9'), ('audio', 'opus')])
        self.assertGreater(int(streams[1]['nb_read_frames']), 0)
        with self.assertRaisesRegex(ValueError, 'exactly one video stream') as refusal:
            video.verify_export(output, request)
        self.assertIs(refusal.exception.evidence['verified'], False)
        self.assertEqual(refusal.exception.evidence['audio_stream_count'], 1)

    def test_subtitle_stream_is_rejected_even_without_audio(self):
        silent = self.root / 'video.mp4'
        video.encode(self.request, self.root, silent)
        subtitles = self.root / 'captions.srt'
        subtitles.write_text('1\n00:00:00,000 --> 00:00:00,200\nTest caption\n',
                             encoding='utf-8')
        output = self.root / 'video-with-captions.mp4'
        self.ffmpeg('-i', silent, '-i', subtitles, '-map', '0:v:0', '-map', '1:s:0',
                    '-c:v', 'copy', '-c:s', 'mov_text', output)
        streams = self.independent_probe(output)['streams']
        self.assertEqual([(stream['codec_type'], stream['codec_name'])
                          for stream in streams], [('video', 'h264'), ('subtitle', 'mov_text')])
        self.independent_decode(output)
        with self.assertRaisesRegex(ValueError, 'exactly one video stream') as refusal:
            video.verify_export(output, self.request)
        evidence = refusal.exception.evidence
        self.assertIs(evidence['verified'], False)
        self.assertEqual(evidence['video_stream_count'], 1)
        self.assertEqual(evidence['audio_stream_count'], 0)
        self.assertEqual(evidence['other_stream_count'], 1)
        self.assertEqual(evidence['probe']['exit_status'], 0)

    def test_malformed_probe_metadata_is_rejected_with_evidence(self):
        output = self.root / 'video.mp4'
        receipt = video.encode(self.request, self.root, output)
        refusals = {
            'display_aspect_ratio': 'invalid display aspect ratio in probe',
            'format_name': 'invalid container format in probe',
            'invalid JSON': 'malformed/incomplete ffprobe evidence',
        }
        for field, reason in refusals.items():
            with self.subTest(field=field):
                inspected = deepcopy(receipt['probe'])
                if field == 'invalid JSON':
                    inspected['stdout'] = '{'
                else:
                    data = json.loads(inspected['stdout'])
                    target = data['streams'][0] if field == 'display_aspect_ratio' else data['format']
                    target[field] = None
                    inspected['stdout'] = json.dumps(data)
                with patch.object(video, '_run', return_value=inspected) as run:
                    with self.assertRaisesRegex(ValueError, reason) as refusal:
                        video.verify_export(output, self.request)
                run.assert_called_once()
                evidence = refusal.exception.evidence
                self.assertIs(evidence['verified'], False)
                if field == 'invalid JSON':
                    self.assertNotIn('audio_stream_count', evidence)
                else:
                    self.assertEqual(evidence['audio_stream_count'], 0)
                # Failure evidence retains the observation, with private paths
                # replaced by the current public diagnostic aliases.
                expected = deepcopy(inspected)
                expected['command'][0] = 'ffprobe'
                expected['command'][-1] = '<media-workdir>/artifact.mp4'
                self.assertEqual(evidence['probe'], expected)

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
            str(Path(__file__).resolve().parents[2] / 'scripts' / 'production.py'),
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

    def test_corrupt_media_preserves_actual_ffprobe_failure_in_python_and_cli(self):
        artifact=self.root/'corrupt-evidence.mp4';artifact.write_bytes(b'not a media artifact')
        with self.assertRaises(ValueError) as failed:
            video.verify_export(artifact,self.request)
        evidence=failed.exception.evidence
        self.assertEqual(evidence['artifact_sha256'],hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual(evidence['request_sha256'],video._digest(self.request))
        probe=evidence['probe']
        self.assertEqual(Path(probe['command'][0]).name,Path(shutil.which('ffprobe')).name)
        self.assertNotEqual(probe['exit_status'],0)
        self.assertIsInstance(probe['stdout'],str)
        self.assertTrue(probe['stderr'])
        self.assertLessEqual(len(probe['stderr'].encode()),self.request['limits']['max_log_bytes'])
        request_path=self.root/'failure-request.json';request_path.write_text(json.dumps(self.request))
        result=subprocess.run([sys.executable,str(Path(video.__file__).resolve()),'verify-export',str(artifact),'--request',str(request_path),'--require-silent'],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,1)
        report=json.loads(result.stdout)
        self.assertFalse(report['verified'])
        self.assertEqual(report['artifact_sha256'],evidence['artifact_sha256'])
        self.assertEqual(report['probe']['exit_status'],probe['exit_status'])
        self.assertTrue(report['probe']['stderr'])
        for value in (evidence,report):
            for field in ('video_stream_count','audio_stream_count','other_stream_count'):
                self.assertNotIn(field,value)

    def test_os_decoder_launch_failure_retains_completed_stages_and_hashes(self):
        artifact = self.root / 'launch-control.mp4'
        video.encode(self.request, self.root, artifact)
        before = artifact.read_bytes()
        private = self.root / 'SYNTHETIC_PRIVATE_DECODER_DIR'
        private.mkdir()
        decoder = private / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        decoder.write_bytes(b'Offline placeholder in an unsupported executable format.\n')
        decoder.chmod(0o700)
        with self.assertRaisesRegex(ValueError, 'launch failed') as failed:
            video.verify_export(artifact, self.request, ffmpeg=decoder)
        request_path = self.root / 'launch-request.json'
        request_path.write_text(json.dumps(self.request), encoding='utf-8')
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()),
            'verify-export', str(artifact), '--request', str(request_path), '--require-silent'],
            env=dict(os.environ, PATH=str(private) + os.pathsep + os.environ['PATH']),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        for evidence in (failed.exception.evidence, json.loads(result.stdout)):
            self.assertFalse(evidence['verified'])
            self.assertEqual(evidence['artifact_sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(evidence['request_sha256'], video._digest(self.request))
            self.assertEqual(evidence['source_manifest_sha256'], video._digest(self.source))
            for stage in ('probe', 'timeline'):
                self.assertEqual(evidence[stage]['exit_status'], 0)
                self.assertTrue(evidence[stage]['stdout'])
            self.assertIsInstance(evidence['decode']['launch_error']['errno'], int)
            self.assertIs(evidence['decode']['started'], False)
            self.assertEqual((evidence['decode']['stdout'], evidence['decode']['stderr']), ('', ''))
            self.assertNotIn('exit_status', evidence['decode'])
            self.assertNotIn(private.name, json.dumps(evidence) + str(failed.exception))
        self.assertEqual(artifact.read_bytes(), before)

    def _failed_verification_reports(self, artifact, request, *, root=None, ffprobe=None, env=None):
        with self.assertRaises(ValueError) as failed:
            video.verify_export(artifact, request, root=root, ffprobe=ffprobe)
        python_report = {**failed.exception.evidence, 'error': str(failed.exception)}
        request_path = self.root / 'diagnostic-contract-request.json'
        request_path.write_text(json.dumps(request))
        command = [sys.executable, video.__file__, 'verify-export', str(artifact),
                   '--request', str(request_path), '--require-silent']
        if root is not None:
            command += ['--root', str(root)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        return {'python': python_report, 'cli': json.loads(result.stdout)}

    def test_real_probe_executable_basename_is_private_in_python_and_cli(self):
        marker = 'SYNTHETIC_PRIVATE_EXECUTABLE'
        artifact = self.root / 'corrupt.mp4'
        artifact.write_bytes(b'synthetic non-media')
        installed = Path(shutil.which('ffprobe')).resolve()
        for variant in ('public-name', 'private-target'):
            directory = self.root / variant
            directory.mkdir()
            executable = directory / 'ffprobe'
            target = executable if variant == 'public-name' else directory / (marker + '-ffprobe')
            shutil.copy2(installed, target)
            self.assertEqual(video.file_hash(target), video.file_hash(installed))
            if target != executable:
                executable.symlink_to(target.name)
            reports = self._failed_verification_reports(
                artifact, self.request, ffprobe=executable,
                env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ['PATH']))
            for interface, report in reports.items():
                with self.subTest(variant=variant, interface=interface):
                    self.assertFalse(report['verified'])
                    self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))
                    self.assertNotIn('video_stream_count', report)
                    for key in ('probe', 'command_failure'):
                        self.assertNotEqual(report[key]['exit_status'], 0)
                        self.assertTrue(report[key]['stderr'])
                        self.assertEqual(report[key]['command'][0], 'ffprobe')
                    self.assertNotIn(marker, json.dumps(report))

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() != 0,
                         'actual unreadable-file check requires an unprivileged POSIX process')
    def test_unreadable_source_omits_private_frame_basename_in_python_and_cli(self):
        marker = 'SYNTHETIC_PRIVATE_FRAME'
        renamed = self.root / (marker + '.png')
        (self.root / self.paths[0]).rename(renamed)
        source = video.build_source(self.root, [renamed.name, *self.paths[1:]], self.source['fps'])
        request = video.make_request(source)
        artifact = self.root / 'six-frames.mp4'
        video.encode(request, self.root, artifact)
        self.assert_receipt(video.verify_export(artifact, request, root=self.root), artifact)
        original_mode = renamed.stat().st_mode
        try:
            renamed.chmod(0)
            with self.assertRaises(PermissionError):
                renamed.read_bytes()
            reports = self._failed_verification_reports(artifact, request, root=self.root)
        finally:
            renamed.chmod(original_mode)
        for interface, report in reports.items():
            with self.subTest(interface=interface):
                self.assertFalse(report['verified'])
                self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))
                self.assertEqual(report['request_sha256'], video._digest(request))
                self.assertEqual(report['source_manifest_sha256'], video._digest(source))
                for stage in ('probe', 'timeline', 'decode'):
                    self.assertEqual(report[stage]['exit_status'], 0)
                self.assertNotIn(marker, json.dumps(report))

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() != 0,
                         'actual unreadable-file check requires an unprivileged POSIX process')
    def test_unreadable_artifact_has_only_available_identity_in_python_and_cli(self):
        marker = 'SYNTHETIC_PRIVATE_ARTIFACT'
        artifact = self.root / (marker + '.mp4')
        video.encode(self.request, self.root, artifact)
        self.assert_receipt(video.verify_export(artifact, self.request), artifact)
        original_mode = artifact.stat().st_mode
        try:
            artifact.chmod(0)
            with self.assertRaises(PermissionError):
                artifact.read_bytes()
            reports = self._failed_verification_reports(artifact, self.request)
        finally:
            artifact.chmod(original_mode)
        for interface, report in reports.items():
            with self.subTest(interface=interface):
                self.assertFalse(report['verified'])
                self.assertEqual(report['request_sha256'], video._digest(self.request))
                self.assertEqual(report['source_manifest_sha256'], video._digest(self.source))
                for unavailable in ('artifact_sha256', 'artifact_bytes', 'probe', 'timeline', 'decode'):
                    self.assertNotIn(unavailable, report)
                self.assertNotIn(marker, json.dumps(report))

    @unittest.skipUnless(os.name == 'posix', 'actual ENOEXEC fixture requires POSIX executable semantics')
    def test_actual_decode_launch_failure_preserves_two_successful_probes(self):
        artifact = self.root / 'launch-evidence.mp4'
        video.encode(self.request, self.root, artifact)
        digest = video.file_hash(artifact)
        control = video.verify_export(artifact, self.request)
        self.assert_receipt(control, artifact)
        tool_dir = self.root / 'SYNTHETIC_PRIVATE_LAUNCH_TOOL_DIRECTORY'
        tool_dir.mkdir()
        invalid = tool_dir / 'ffmpeg'
        invalid.write_bytes(b'synthetic executable with no executable format\n')
        invalid.chmod(0o755)
        with self.assertRaises(ValueError) as failed:
            video.verify_export(artifact, self.request, ffmpeg=invalid)
        request_path = self.root / 'launch-evidence-request.json'
        request_path.write_text(json.dumps(self.request))
        env = dict(os.environ, PATH=str(tool_dir) + os.pathsep + os.environ.get('PATH', ''))
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
            str(artifact), '--request', str(request_path), '--require-silent'],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        report = json.loads(result.stdout)
        for evidence in (failed.exception.evidence, report):
            self.assertFalse(evidence['verified'])
            self.assertEqual(evidence['artifact_sha256'], digest)
            self.assertEqual(evidence['artifact_sha256_scope'], 'verification-snapshot')
            self.assertEqual(evidence['request_sha256'], video._digest(self.request))
            self.assertEqual(evidence['source_manifest_sha256'], video._digest(self.source))
            self.assertEqual(evidence['video_stream_count'], 1)
            self.assertEqual(evidence['audio_stream_count'], 0)
            for stage in ('probe', 'timeline'):
                self.assertEqual(evidence[stage]['exit_status'], 0)
                self.assertTrue(json.loads(evidence[stage]['stdout']))
            self.assertEqual(len(json.loads(evidence['timeline']['stdout'])['frames']), 6)
            self.assertIs(evidence['decode']['started'], False)
            self.assertEqual(evidence['decode']['launch_error']['errno'], errno.ENOEXEC)
            self.assertNotIn('exit_status', evidence['decode'])
            self.assertEqual(evidence['decode']['stdout'], '')
            self.assertEqual(evidence['decode']['stderr'], '')
            self.assertNotIn(tool_dir.name, json.dumps(evidence))
        self.assertNotIn(tool_dir.name, str(failed.exception))
        self.assertEqual(video.file_hash(artifact), digest)

    def test_actual_caller_artifact_change_retains_completed_snapshot_verification(self):
        artifact = self.root / 'SYNTHETIC_PRIVATE_CALLER_ARTIFACT.mp4'
        video.encode(self.request, self.root, artifact)
        original_bytes = artifact.read_bytes()
        digest = hashlib.sha256(original_bytes).hexdigest()
        self.assert_receipt(video.verify_export(artifact, self.request), artifact)
        changed = []
        def mutate_after_snapshot():
            if not changed:
                artifact.write_bytes(b'revised synthetic caller artifact')
                changed.append(True)
            return False
        with self.assertRaisesRegex(ValueError, 'artifact changed') as failed:
            video.verify_export(artifact, self.request, cancel=mutate_after_snapshot)
        self.assertEqual(changed, [True])
        self.assertNotEqual(video.file_hash(artifact), digest)
        artifact.write_bytes(original_bytes)
        tool_dir = self.root / 'SYNTHETIC_PRIVATE_MUTATION_TOOL_DIRECTORY'
        tool_dir.mkdir()
        wrapper = tool_dir / 'ffmpeg'
        # This synthetic local wrapper really decodes through installed FFmpeg,
        # then revises only the caller fixture. It does not forge process results.
        wrapper.write_text('#!' + sys.executable + '\n'
            'import os, subprocess, sys\nfrom pathlib import Path\n'
            'result = subprocess.run([os.environ["SILENT_TEST_REAL_FFMPEG"], *sys.argv[1:]])\n'
            'Path(os.environ["SILENT_TEST_CALLER_ARTIFACT"]).write_bytes(b"revised synthetic caller artifact")\n'
            'sys.exit(result.returncode)\n')
        wrapper.chmod(0o755)
        request_path = self.root / 'mutation-evidence-request.json'
        request_path.write_text(json.dumps(self.request))
        env = dict(os.environ, PATH=str(tool_dir) + os.pathsep + os.environ.get('PATH', ''),
                   SILENT_TEST_REAL_FFMPEG=shutil.which('ffmpeg'), SILENT_TEST_CALLER_ARTIFACT=str(artifact))
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
            str(artifact), '--request', str(request_path), '--require-silent'],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        self.assertNotEqual(video.file_hash(artifact), digest)
        for evidence in (failed.exception.evidence, json.loads(result.stdout)):
            self.assertFalse(evidence['verified'])
            self.assertEqual(evidence['artifact_sha256'], digest)
            self.assertEqual(evidence['artifact_bytes'], len(original_bytes))
            self.assertEqual(evidence['artifact_sha256_scope'], 'verification-snapshot')
            self.assertEqual(evidence['request_sha256'], video._digest(self.request))
            self.assertEqual(evidence['source_manifest_sha256'], video._digest(self.source))
            for stage in ('probe', 'timeline', 'decode'):
                self.assertEqual(evidence[stage]['exit_status'], 0)
            self.assertEqual(len(json.loads(evidence['timeline']['stdout'])['frames']), 6)
            for marker in (tool_dir.name, artifact.name):
                self.assertNotIn(marker, json.dumps(evidence))

    def test_failed_probe_redacts_owned_temporary_paths_in_python_and_cli(self):
        marker = 'SYNTHETIC_WORKDIR_MARKER'
        private = self.root / marker
        private.mkdir()
        artifact = self.root / 'path-error.mp4'
        artifact.write_bytes(b'synthetic corrupt media')
        request = deepcopy(self.request)
        request['limits']['max_log_bytes'] = 1024
        with patch.object(tempfile, 'tempdir', str(private)), self.assertRaises(ValueError) as failed:
            video.verify_export(artifact, request)
        evidence = failed.exception.evidence
        self.assertNotIn(marker, str(failed.exception) + json.dumps(evidence))
        self.assertIn('<media-workdir>', json.dumps(evidence))
        self.assertNotEqual(evidence['probe']['exit_status'], 0)
        for field in ('stdout', 'stderr'):
            self.assertLessEqual(len(evidence['probe'][field].encode()), 1024)
        request_path = self.root / 'path-error-request.json'
        request_path.write_text(json.dumps(request))
        env = dict(os.environ, TMPDIR=str(private), TEMP=str(private), TMP=str(private))
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
            str(artifact), '--request', str(request_path), '--require-silent'],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['verified'])

    def test_failed_probe_excludes_unneeded_editorial_metadata(self):
        artifact=self.root/'tagged-audio.mp4'
        marker='SYNTHETIC_EDITORIAL_MARKER'
        stream_marker='SYNTHETIC_STREAM_MARKER'
        self.ffmpeg('-f','lavfi','-i','anullsrc=r=48000:cl=mono','-t','0.25','-c:a','aac',
                    '-metadata','title='+marker,'-metadata:s:a:0','handler_name='+stream_marker,artifact)
        before=hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.assertIn(marker,json.dumps(self.independent_probe(artifact)))
        self.assertIn(stream_marker,json.dumps(self.independent_probe(artifact)))
        with self.assertRaises(ValueError) as failed:
            video.verify_export(artifact,self.request)
        evidence=failed.exception.evidence
        self.assertNotIn(marker,evidence['probe']['stdout'])
        self.assertNotIn(stream_marker,evidence['probe']['stdout'])
        self.assertEqual(evidence['video_stream_count'],0)
        self.assertEqual(evidence['audio_stream_count'],1)
        self.assertEqual(evidence['other_stream_count'],0)
        request_path=self.root/'metadata-request.json';request_path.write_text(json.dumps(self.request))
        result=subprocess.run([sys.executable,str(Path(video.__file__).resolve()),'verify-export',str(artifact),'--request',str(request_path),'--require-silent'],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,1)
        report=json.loads(result.stdout)
        self.assertFalse(report['verified'])
        self.assertNotIn(marker,result.stdout)
        self.assertNotIn(stream_marker,result.stdout)
        self.assertEqual(report['audio_stream_count'],1)
        self.assertEqual(report['artifact_sha256'],before)
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(),before)

    def test_real_probe_truncated_long_workdir_is_private_in_python_and_cli(self):
        marker = 'SYNTHETIC_PRIVATE_LONG_WORKDIR'
        private = self.root / marker
        for index in range(6):
            private = private / (str(index) + 'x' * 180)
        private.mkdir(parents=True)
        artifact = self.root / 'long-path-error.mp4'
        artifact.write_bytes(b'synthetic corrupt media')
        request = deepcopy(self.request)
        request['limits']['max_log_bytes'] = 1024
        with patch.object(tempfile, 'tempdir', str(private)), self.assertRaises(ValueError) as failed:
            video.verify_export(artifact, request)
        evidence = failed.exception.evidence
        self.assertNotIn(marker, str(failed.exception) + json.dumps(evidence))
        self.assertTrue(evidence['probe']['stderr_truncated'])
        self.assertIn('<media-path-truncated>', evidence['probe']['stderr'])
        self.assertLessEqual(len(evidence['probe']['stderr'].encode()), 1024)
        self.assertEqual(evidence['artifact_sha256'], video.file_hash(artifact))
        request_path = self.root / 'long-path-request.json'
        request_path.write_text(json.dumps(request))
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
            str(artifact), '--request', str(request_path), '--require-silent'],
            env=dict(os.environ, TMPDIR=str(private), TEMP=str(private), TMP=str(private)),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(marker, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['verified'])
        self.assertTrue(report['probe']['stderr_truncated'])
        self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))

    def test_stale_source_failure_does_not_disclose_relative_source_names(self):
        marker = 'SYNTHETIC_PRIVATE_SOURCE_NAME'
        folder = self.root / marker
        folder.mkdir()
        paths = []
        for relative in self.paths:
            (self.root / relative).rename(folder / relative)
            paths.append(marker + '/' + relative)
        source = video.build_source(self.root, paths, fps=self.source['fps'], origin='synthetic')
        request = video.make_request(source)
        artifact = self.root / 'private-source.mp4'
        video.encode(request, self.root, artifact)
        with Image.new('RGB', (64, 48), (250, 0, 0)) as changed:
            changed.save(self.root / paths[0])
        with self.assertRaisesRegex(ValueError, 'index 0') as failed:
            video.verify_export(artifact, request, root=self.root)
        self.assertNotIn(marker, str(failed.exception) + json.dumps(failed.exception.evidence))
        self.assertEqual(failed.exception.evidence['decode']['exit_status'], 0)
        request_path = self.root / 'private-source-request.json'
        request_path.write_text(json.dumps(request))
        result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
            str(artifact), '--request', str(request_path), '--require-silent', '--root', str(self.root)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['verified'])

    def test_unicode_workdir_capture_boundaries_are_private_in_python_and_cli(self):
        marker = 'SYNTHETIC_UNICODE_WORKDIR'
        artifact = self.root / 'unicode-path-error.mp4'
        artifact.write_bytes(b'synthetic corrupt media')
        request = deepcopy(self.request)
        request['limits']['max_log_bytes'] = 1024
        request_path = self.root / 'unicode-path-request.json'
        request_path.write_text(json.dumps(request))
        for pad in range(4):
            private = self.root / (marker + 'x' * pad)
            for index in range(6):
                private = private / (str(index) + '\U0001f600' * 45)
            private.mkdir(parents=True)
            with self.subTest(pad=pad):
                with patch.object(tempfile, 'tempdir', str(private)), self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact, request)
                self.assertNotIn(marker, str(failed.exception) + json.dumps(failed.exception.evidence))
                record = failed.exception.evidence['probe']
                self.assertTrue(record['stderr_truncated'])
                self.assertIn('<media-path-truncated>', record['stderr'])
                self.assertLessEqual(len(record['stderr'].encode()), 1024)
                result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
                    str(artifact), '--request', str(request_path), '--require-silent'],
                    env=dict(os.environ, TMPDIR=str(private), TEMP=str(private), TMP=str(private)),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(marker, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['verified'])
                self.assertTrue(report['probe']['stderr_truncated'])
                self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))

    def test_disguised_edit_lists_cannot_open_or_echo_referenced_clip_names(self):
        marker = 'SYNTHETIC_EDITORIAL_CLIP_NAME'
        for profile in ('h264-mp4', 'vp9-webm'):
            with self.subTest(profile=profile):
                request = video.make_request(self.source, profile)
                artifact = self.root / ('workprint' + video.PROFILES[profile]['extension'])
                artifact.write_text("ffconcat version 1.0\nfile '" + marker + ".mp4'\n")
                with self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact, request)
                self.assertNotIn(marker, str(failed.exception) + json.dumps(failed.exception.evidence))
                self.assertNotEqual(failed.exception.evidence['probe']['exit_status'], 0)
                self.assertNotIn('video_stream_count', failed.exception.evidence)
                request_path = self.root / ('edit-list-request-' + profile + '.json')
                request_path.write_text(json.dumps(request))
                result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
                    str(artifact), '--request', str(request_path), '--require-silent'],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(marker, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['verified'])
                self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))

    def test_filesystem_byte_workdir_is_private_in_python_and_cli(self):
        from test_silent_video_diagnostic_boundaries_native import completed_process_capture_schedule

        # This case asserts the actual completed probe's exit 1. Let that real
        # child finish before bounded capture, so overflow cannot race it into
        # SIGKILL. The adjacent termination case retains the ordinary schedule.
        completed_cli = self.root / 'completed-byte-path-cli.py'
        completed_cli.write_text(
            'import sys\n'
            'sys.path.insert(0, %r)\n' % str(Path(__file__).resolve().parent) +
            'from test_silent_video_diagnostic_boundaries_native import '
            'completed_process_capture_schedule, video\n'
            'with completed_process_capture_schedule():\n'
            '    video.main()\n', encoding='utf-8')
        marker = 'SYNTHETIC_NON_UTF8_WORKDIR'
        # POSIX exercises an actual non-UTF-8 filename byte. Windows uses its
        # actual Unicode replacement-character filename as a native control.
        suffix = os.fsdecode(b'\xff') if os.name == 'posix' else '\ufffd'
        artifact = self.root / 'byte-path-error.mp4'
        artifact.write_bytes(b'synthetic corrupt media')
        request = deepcopy(self.request)
        request['limits']['max_log_bytes'] = 1024
        request_path = self.root / 'byte-path-request.json'
        request_path.write_text(json.dumps(request))
        for long_path in (False, True):
            private = self.root / (marker + suffix + str(long_path))
            if long_path:
                for index in range(6):
                    private /= str(index) + 'x' * 180
            private.mkdir(parents=True)
            with self.subTest(long_path=long_path):
                with completed_process_capture_schedule(), \
                        patch.object(tempfile, 'tempdir', str(private)), self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact, request)
                self.assertNotIn(marker, str(failed.exception) + json.dumps(failed.exception.evidence))
                self.assertEqual(failed.exception.evidence['probe']['exit_status'], 1)
                self.assertEqual(bool(failed.exception.evidence['probe'].get('stderr_truncated')), long_path)
                self.assertEqual(failed.exception.evidence['artifact_sha256'], video.file_hash(artifact))
                result = subprocess.run([sys.executable, str(completed_cli), 'verify-export',
                    str(artifact), '--request', str(request_path), '--require-silent'],
                    env=dict(os.environ, TMPDIR=str(private), TEMP=str(private), TMP=str(private)),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(marker, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['verified'])
                self.assertEqual(report['probe']['exit_status'], 1)
                self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))

    def test_filesystem_byte_workdir_records_native_termination_and_evidence_budget(self):
        marker = 'SYNTHETIC_NON_UTF8_WORKDIR'
        # POSIX exercises an actual non-UTF-8 filename byte. Windows uses its
        # actual Unicode replacement-character filename as a native control.
        suffix = os.fsdecode(b'\xff') if os.name == 'posix' else '\ufffd'
        artifact = self.root / 'byte-path-error.mp4'
        artifact.write_bytes(b'synthetic corrupt media')
        request = deepcopy(self.request)
        request['limits']['max_log_bytes'] = 1024
        request_path = self.root / 'byte-path-request.json'
        request_path.write_text(json.dumps(request))
        for long_path in (False, True):
            private = self.root / (marker + suffix + str(long_path))
            if long_path:
                for index in range(6):
                    private /= str(index) + 'x' * 180
            private.mkdir(parents=True)
            with self.subTest(long_path=long_path):
                with patch.object(tempfile, 'tempdir', str(private)), self.assertRaises(ValueError) as failed:
                    video.verify_export(artifact, request)
                self.assertNotIn(marker, str(failed.exception) + json.dumps(failed.exception.evidence))
                record = failed.exception.evidence['probe']
                if long_path:
                    self.assertTrue(record['evidence_limit_exceeded'])
                    self.assertIn('evidence byte budget', str(failed.exception))
                    self.assertIs(type(record['termination_requested']), bool)
                    # POSIX kill is -SIGKILL; Windows TerminateProcess uses 1.
                    # A process already finished at the limit retains exit 1.
                    if os.name == 'posix' and record['termination_requested']:
                        # The process can finish naturally between poll/kill.
                        self.assertIn(record['exit_status'], (-9, 1))
                    else:
                        self.assertEqual(record['exit_status'], 1)
                    self.assertLessEqual(len(record['stderr'].encode()), 1024)
                else:
                    self.assertEqual(record['exit_status'], 1)
                self.assertEqual(bool(failed.exception.evidence['probe'].get('stderr_truncated')), long_path)
                self.assertEqual(failed.exception.evidence['artifact_sha256'], video.file_hash(artifact))
                result = subprocess.run([sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
                    str(artifact), '--request', str(request_path), '--require-silent'],
                    env=dict(os.environ, TMPDIR=str(private), TEMP=str(private), TMP=str(private)),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(marker, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['verified'])
                record = report['probe']
                if long_path:
                    self.assertTrue(record['evidence_limit_exceeded'])
                    self.assertIn('evidence byte budget', report['error'])
                    self.assertIs(type(record['termination_requested']), bool)
                    if os.name == 'posix' and record['termination_requested']:
                        self.assertIn(record['exit_status'], (-9, 1))
                    else:
                        self.assertEqual(record['exit_status'], 1)
                    self.assertTrue(record['stderr_truncated'])
                    self.assertLessEqual(len(record['stderr'].encode()), 1024)
                else:
                    self.assertEqual(record['exit_status'], 1)
                self.assertEqual(report['artifact_sha256'], video.file_hash(artifact))

    def test_historical_full_probe_receipt_accepts_only_matching_authoritative_facts(self):
        artifact=self.root/'historical-receipt.mp4'
        receipt=video.encode(self.request,self.root,artifact)
        command=[shutil.which('ffprobe'),'-v','error','-count_frames','-show_streams','-show_format','-of','json',str(artifact)]
        full=subprocess.run(command,capture_output=True,text=True,timeout=30)
        self.assertEqual(full.returncode,0)
        historical=deepcopy(receipt)
        historical['probe']=dict(command=command,exit_status=full.returncode,stdout=full.stdout,stderr=full.stderr)
        self.assertIn('tags',json.loads(full.stdout)['format'])
        self.assertNotEqual(historical['probe']['stdout'],receipt['probe']['stdout'])
        accepted=video.validate_receipt(historical,artifact,self.request,root=self.root)
        self.assertTrue(accepted['verified'])
        for change in ('codec','missing-width','float-width','extra-stream'):
            tampered=deepcopy(historical);raw=json.loads(tampered['probe']['stdout'])
            if change=='codec':raw['streams'][0]['codec_name']='vp9'
            elif change=='missing-width':del raw['streams'][0]['width']
            elif change=='float-width':raw['streams'][0]['width']=float(raw['streams'][0]['width'])
            else:raw['streams'].append({'codec_type':'audio'})
            tampered['probe']['stdout']=json.dumps(raw)
            with self.subTest(change=change),self.assertRaises(ValueError):
                video.validate_receipt(tampered,artifact,self.request,root=self.root)

    def test_ffprobe_nonzero_exit_is_rejected(self):
        output = self.root / 'video.mp4'
        video.encode(self.request, self.root, output)
        with self.assertRaises(ValueError):
            video.verify_export(output, self.request, ffprobe=sys.executable)

    def test_ffprobe_nonzero_exit_is_rejected_with_cli_evidence(self):
        output = self.root / 'invalid.mp4'
        output.write_bytes(b'not a media artifact')
        request_path = self.root / 'request.json'
        request_path.write_text(json.dumps(self.request), encoding='utf-8')
        result = subprocess.run(
            [sys.executable, str(Path(video.__file__).resolve()), 'verify-export',
             str(output), '--request', str(request_path), '--require-silent'],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        evidence = json.loads(result.stdout)
        self.assertIs(evidence['verified'], False)
        self.assertNotIn('audio_stream_count', evidence)
        probe = evidence['probe']
        # The verifier now limits its query to required metadata and exposes a
        # logical tool identity, while preserving the actual arguments/status.
        self.assertEqual(probe['command'][:-1],
                         ['ffprobe', '-format_whitelist', 'mov,matroska,webm',
                          '-protocol_whitelist', 'file', '-v', 'error', '-count_frames',
                          '-show_entries',
                          'stream=codec_type,codec_name,pix_fmt,width,height,nb_read_frames,'
                          'sample_aspect_ratio,display_aspect_ratio,color_range,color_space,'
                          'color_transfer,color_primaries,avg_frame_rate,time_base:'
                          'format=format_name,duration', '-of', 'json'])
        self.assertEqual(probe['command'][-1], '<media-workdir>/artifact.mp4')
        self.assertGreater(probe['exit_status'], 0)
        self.assertIn('media command failed (%s)' % probe['exit_status'], evidence['error'])

        actual = subprocess.run([shutil.which('ffprobe'), *probe['command'][1:-1], str(output)],
                                capture_output=True, timeout=30)
        self.assertEqual(probe['exit_status'], actual.returncode)
        self.assertEqual(probe['stdout'], actual.stdout.decode('utf-8', errors='replace'))
        self.assertTrue(probe['stderr'].strip())
        self.assertEqual(
            probe['stderr'].splitlines()[-1].replace(probe['command'][-1], str(output)),
            actual.stderr.decode('utf-8', errors='replace').splitlines()[-1],
        )
        self.assertNotIn(str(self.root), json.dumps(evidence))

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

    def test_progress_cannot_change_an_unsampled_private_input(self):
        changed = []
        output = self.root / 'changed-input.mp4'

        def progress(event):
            if event['phase'] == 'encode':
                frame = next(self.root.glob('.silent-export-*/frames/000001.png'))
                with Image.new('RGB', (64, 48), (250, 0, 0)) as image:
                    image.save(frame)
                changed.append(frame.name)

        with self.assertRaisesRegex(ValueError, 'private encoder input'):
            video.encode(self.request, self.root, output, progress=progress)
        self.assertEqual(changed, ['000001.png'])
        self.assertFalse(output.exists())
        video._snapshot(self.request, self.root, None)

    def test_private_input_modify_restore_during_actual_encoding_is_rejected(self):
        actual_run = video._run
        mutations = []
        output = self.root / 'restored-input.mp4'

        def mutate_during_real_encoder(command, *args, **kwargs):
            if '-framerate' not in command or mutations:
                return actual_run(command, *args, **kwargs)
            frame = next(self.root.glob('.silent-export-*/frames/000001.png'))
            before, original = frame.stat(), frame.read_bytes()
            with Image.new('RGB', (64, 48), (250, 0, 0)) as image:
                image.save(frame)
            try:
                result = actual_run(command, *args, **kwargs)
                artifact = next(self.root.glob('.silent-export-*/output.partial.mp4'))
                decoded = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-threads', '1', '-i', str(artifact),
                    '-threads', '1', '-map', '0:v:0', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    capture_output=True, check=True, timeout=30).stdout
                pixel = tuple(decoded[64 * 48 * 3:64 * 48 * 3 + 3])
                self.assertGreater(pixel[0], 200)
                self.assertLess(pixel[2], 40)
                self.assertEqual(result['exit_status'], 0)
                return result
            finally:
                frame.write_bytes(original)
                os.utime(frame, ns=(before.st_atime_ns, before.st_mtime_ns))
                after = frame.stat()
                mutations.append({'bytes_restored': frame.read_bytes() == original,
                                  'mtime_restored': after.st_mtime_ns == before.st_mtime_ns,
                                  'ctime_changed': after.st_ctime_ns != before.st_ctime_ns})

        with patch.object(video, '_run', side_effect=mutate_during_real_encoder):
            with self.assertRaisesRegex(ValueError, 'private encoder input'):
                video.encode(self.request, self.root, output)
        self.assertEqual(mutations, [{'bytes_restored': True, 'mtime_restored': True, 'ctime_changed': True}])
        self.assertFalse(output.exists())
        video._snapshot(self.request, self.root, None)

    def test_frame_directory_replacement_cannot_change_consumed_input(self):
        changed = []
        output = self.root / 'replaced-input-directory.mp4'

        def progress(event):
            if event['phase'] == 'encode':
                folder = next(self.root.glob('.silent-export-*/frames'))
                retained = folder.with_name('retained-frames')
                folder.rename(retained)
                shutil.copytree(retained, folder)
                with Image.new('RGB', (64, 48), (250, 0, 0)) as image:
                    image.save(folder / '000001.png')
                changed.append(str(folder))

        try:
            receipt = video.encode(self.request, self.root, output, progress=progress)
        except ValueError:
            self.assertFalse(output.exists())
        else:
            self.assertTrue(receipt['verified'])
            decoded = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-threads', '1', '-i', str(output),
                '-threads', '1', '-map', '0:v:0', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                capture_output=True, check=True, timeout=30).stdout
            actual = tuple(decoded[64 * 48 * 3:64 * 48 * 3 + 3])
            with Image.open(self.root / self.paths[1]) as source:
                expected = source.getpixel((0, 0))
            self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 10)
        self.assertEqual(len(changed), 1)
        video._snapshot(self.request, self.root, None)

    def test_output_size_budget_prevents_publication(self):
        output = self.root / 'oversize.mp4'
        request = deepcopy(self.request)
        request['limits']['max_output_bytes'] = 1024
        with self.assertRaises(ValueError):
            video.encode(request, self.root, output)
        self.assertFalse(output.exists())



class DescriptorPublication(unittest.TestCase):
    def test_source_open_preserves_pinned_root_during_namespace_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with video._pin_directory(root) as parent_fd:
                with video._private_stage(parent_fd) as (stage, descriptor):
                    (stage/'source.bin').write_bytes(b'owned source bytes')
                    name = next(root.glob('.silent-export-*'))
                    original_path = video._path
                    swapped = []
                    def swap_before_payload_open(source_root, relative):
                        selected = original_path(source_root, relative)
                        if not swapped:
                            name.rename(root/'moved-owned-stage')
                            name.mkdir(mode=0o700)
                            (name/'source.bin').write_bytes(b'foreign replacement bytes')
                            swapped.append(True)
                        return selected
                    with patch.object(video, '_path', side_effect=swap_before_payload_open):
                        with video._open_source(stage, 'source.bin') as stream:
                            self.assertEqual(stream.read(), b'owned source bytes')
                    self.assertTrue(swapped)
                    self.assertEqual((name/'source.bin').read_bytes(), b'foreign replacement bytes')

    @unittest.skipUnless(video.secure_publication_supported(), 'Linux descriptor path adapter')
    def test_stage_cleanup_does_not_follow_replaced_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root/'outside'
            outside.mkdir()
            sentinel = outside/'original.txt'
            sentinel.write_bytes(b'unrelated original bytes')
            with video._pin_directory(root) as parent_fd:
                with video._private_stage(parent_fd) as (stage, descriptor):
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
