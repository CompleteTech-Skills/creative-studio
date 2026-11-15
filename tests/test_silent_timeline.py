"""Portable timeline document admission; no native execution is claimed."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import silent_timeline as timeline
import silent_video as video


def source():
    return {'schema_version': 1, 'kind': 'silent-frame-source', 'origin': 'synthetic',
        'format': 'png', 'precision': 8, 'alpha': 'opaque', 'color': 'untagged-sdr',
        'width': 8, 'height': 8, 'fps': {'numerator': 24000, 'denominator': 1001},
        'frames': [{'index': i, 'path': '%d.png' % i, 'sha256': '%064x' % (i + 1), 'bytes': 128}
                   for i in range(3)]}


def document():
    original = source()
    output = deepcopy(original); output['origin'] = 'assembled'
    for i, frame in enumerate(output['frames']): frame['path'] = 'frames/%06d.png' % i
    return {'schema_version': 1, 'kind': 'silent-timeline', 'source': output,
        'shots': [{'id': 'shot', 'context': None, 'source': original,
            'source_sha256': video._digest(original), 'start': 0, 'end': 3,
            'output_start': 0, 'output_end': 3}],
        'mapping': [{'index': i, 'shot_id': 'shot', 'source_index': i,
            'source_sha256': original['frames'][i]['sha256'],
            'pts': {'numerator': value.numerator, 'denominator': value.denominator}}
            for i, value in enumerate((Fraction(0), Fraction(1001, 24000), Fraction(1001, 12000)))],
        'duration': {'numerator': 1001, 'denominator': 8000},
        'audio_policy': 'forbidden', 'visual_review': 'not-performed'}


class TimelineContracts(unittest.TestCase):
    def test_fractional_time_and_half_open_selection_are_exact(self):
        shots = [{'id': 'shot', 'root': None, 'source': source(), 'start': 1, 'end': 3}]
        _, signature, total = timeline._prepare(shots)
        self.assertEqual(signature[2], Fraction(24000, 1001))
        self.assertEqual(total, 256)
        self.assertEqual(timeline.validate_document(document())['duration'],
                         {'numerator': 1001, 'denominator': 8000})

    def test_forged_mapping_source_or_cut_cannot_validate(self):
        cases = []
        value = document(); value['mapping'][1]['pts']['denominator'] = 1; cases.append(value)
        value = document(); value['shots'][0]['source_sha256'] = '0' * 64; cases.append(value)
        value = document(); value['shots'][0]['output_end'] = 2; cases.append(value)
        value = document(); value['mapping'][1]['index'] = True; cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                timeline.validate_document(value)

    def test_incompatible_sources_and_fractional_range_indices_are_rejected(self):
        original = [{'id': 'a', 'root': None, 'source': source(), 'start': 0, 'end': 3},
                    {'id': 'b', 'root': None, 'source': source(), 'start': 0, 'end': 3}]
        for field, value in [('fps', {'numerator': 30, 'denominator': 1}), ('width', 16)]:
            shots = deepcopy(original); shots[1]['source'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'matching'):
                timeline._prepare(shots)
        for start, end in ((True, 2), (0, 2.0), (-1, 2), (1, 1), (0, 4)):
            shots = deepcopy(original); shots[0].update(start=start, end=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                timeline._prepare(shots)


if __name__ == '__main__': unittest.main()
