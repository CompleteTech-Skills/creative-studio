"""Exact synthetic timeline tests; fixture pixels are not 3D render acceptance."""
from copy import deepcopy
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_timeline as timeline
import silent_video as video


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shots = []
        for number, count in enumerate((72, 120, 48)):
            root = self.root / ('shot%d' % number)
            root.mkdir()
            paths = []
            for index in range(count):
                path = root / ('%06d.png' % index)
                image = Image.new('RGB', (64, 48), (number * 80, index, 180))
                image.paste((220, 210, 70), (index % 40, 10, index % 40 + 10, 30))
                image.save(path)
                paths.append(path.name)
            source = video.build_source(root, paths, {'numerator': 24, 'denominator': 1})
            self.shots.append({'id': 'shot%d' % number, 'root': root, 'source': source, 'start': 0, 'end': count})
        self.destination = self.root / 'timeline'

    def test_three_shots_exact_240_frames_ten_seconds_and_boundaries(self):
        result = timeline.assemble(self.shots, self.destination)
        self.assertEqual(result['duration'], {'numerator': 10, 'denominator': 1})
        self.assertEqual(len(result['source']['frames']), 240)
        self.assertEqual([shot['output_start'] for shot in result['shots']], [0, 72, 192])
        for index, identity, source_index in [(71, 'shot0', 71), (72, 'shot1', 0),
                                             (191, 'shot1', 119), (192, 'shot2', 0), (239, 'shot2', 47)]:
            frame = result['mapping'][index]
            self.assertEqual((frame['shot_id'], frame['source_index']), (identity, source_index))
            self.assertEqual((self.destination / result['source']['frames'][index]['path']).read_bytes(),
                             (self.shots[int(identity[-1])]['root'] / ('%06d.png' % source_index)).read_bytes())
        self.assertEqual(timeline.validate(self.destination), result)

    def test_half_open_ranges_and_fractional_pts(self):
        shots = deepcopy(self.shots)
        for shot in shots:
            shot['source']['fps'] = {'numerator': 24000, 'denominator': 1001}
            shot['start'], shot['end'] = 1, 3
        result = timeline.assemble(shots, self.destination)
        self.assertEqual(len(result['mapping']), 6)
        self.assertEqual(result['duration'], {'numerator': 1001, 'denominator': 4000})
        self.assertEqual(result['mapping'][3]['pts'], {'numerator': 1001, 'denominator': 8000})
        self.assertEqual([x['source_index'] for x in result['mapping']], [1, 2, 1, 2, 1, 2])

    def test_owner_context_survives_assembly_and_document_verification(self):
        context = {key: {'id': key + '-a', 'version': '1.0.0'} for key in ('project', 'scene', 'shot')}
        self.shots[0]['context'] = context
        result = timeline.assemble(self.shots, self.destination)
        self.assertEqual(result['shots'][0]['context'], context)
        self.assertEqual(timeline.validate_document(result), result)
        self.shots[0]['context'] = {'unrecognized': 'value'}
        with self.assertRaises(ValueError):
            timeline.assemble(self.shots, self.root / 'invalid')

    def test_incompatible_sources_and_invalid_ranges_fail_before_publication(self):
        for field, value in [('fps', {'numerator': 30, 'denominator': 1}), ('width', 128)]:
            shots = deepcopy(self.shots)
            shots[1]['source'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'matching'):
                timeline.assemble(shots, self.destination)
        for start, end in [(-1, 2), (1, 1), (0, 73), (True, 2), (0, 1.0)]:
            shots = deepcopy(self.shots)
            shots[0].update(start=start, end=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                timeline.assemble(shots, self.destination)
        self.assertFalse(self.destination.exists())

    def test_unselected_source_frame_is_checked_for_completeness(self):
        self.shots[0]['start'] = 1
        (self.shots[0]['root'] / '000000.png').unlink()
        with self.assertRaises(ValueError):
            timeline.assemble(self.shots, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob('.silent-timeline-*')))

    def test_tampered_output_and_forged_mapping_are_rejected(self):
        timeline.assemble(self.shots, self.destination)
        manifest_path = self.destination / 'timeline.json'
        original = manifest_path.read_text()
        for change in ('index', 'pts', 'source', 'range'):
            manifest = json.loads(original)
            if change == 'index': manifest['mapping'][1]['index'] = True
            if change == 'pts': manifest['mapping'][1]['pts']['denominator'] = 1
            if change == 'source': manifest['shots'][0]['source_sha256'] = '0' * 64
            if change == 'range': manifest['shots'][1]['output_start'] -= 1
            manifest_path.write_text(json.dumps(manifest))
            with self.subTest(change=change), self.assertRaises(ValueError):
                timeline.validate(self.destination)
        manifest_path.write_text(original)
        (self.destination / 'frames/000000.png').write_bytes((self.destination / 'frames/000001.png').read_bytes())
        with self.assertRaisesRegex(ValueError, 'stale'):
            timeline.validate(self.destination)

    def test_cancellation_and_destination_collision_preserve_existing_data(self):
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            timeline.assemble(self.shots, self.destination, cancel=lambda: True)
        self.assertFalse(self.destination.exists())
        self.destination.mkdir()
        marker = self.destination / 'unrelated.txt'
        marker.write_text('preserve')
        with self.assertRaises(ValueError):
            timeline.assemble(self.shots, self.destination)
        self.assertEqual(marker.read_text(), 'preserve')

    def test_duplicate_identity_and_extra_frame_are_rejected(self):
        shots = deepcopy(self.shots)
        shots[1]['id'] = shots[0]['id']
        with self.assertRaises(ValueError):
            timeline.assemble(shots, self.destination)
        timeline.assemble(self.shots, self.destination)
        (self.destination / 'frames/extra.png').write_bytes(b'not part of timeline')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            timeline.validate(self.destination)

    @unittest.skipUnless(os.environ.get('HATCH_SILENT_MEDIA_TEST') == '1', 'explicit real encoding gate')
    def test_actual_three_shot_mp4_and_webm_have_exact_frames_no_audio(self):
        result = timeline.assemble(self.shots, self.destination)
        for profile, suffix in [('h264-mp4', '.mp4'), ('vp9-webm', '.webm')]:
            with self.subTest(profile=profile):
                request = video.make_request(result['source'], profile)
                artifact = self.root / (profile + suffix)
                envelope = timeline.encode(self.destination, artifact, profile)
                receipt = envelope['verification']
                self.assertEqual(receipt['frame_count'], 240)
                self.assertEqual(receipt['audio_stream_count'], 0)
                self.assertEqual(receipt['duration'], {'numerator': 10, 'denominator': 1})
                self.assertTrue(timeline.verify_export(artifact, envelope, root=self.destination)['verified'])
                forged = deepcopy(envelope)
                forged['timeline']['shots'][0]['id'] = 'forged'
                with self.assertRaises(ValueError):
                    timeline.verify_export(artifact, forged)
                forged = deepcopy(envelope)
                forged['timeline_sha256'] = '0' * 64
                with self.assertRaises(ValueError):
                    timeline.verify_export(artifact, forged)


if __name__ == '__main__': unittest.main()
