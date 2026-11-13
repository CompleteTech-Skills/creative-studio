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
