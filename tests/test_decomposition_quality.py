from pathlib import Path
import sys
import unittest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from decomposition_quality import binary_mask_metrics, semantic_label_metrics

class EvaluationMetrics(unittest.TestCase):
    def test_exact_pixels_do_not_grant_semantic_approval(self):
        im = Image.new('L', (5, 4), 255)
        score = binary_mask_metrics(im, im)
        self.assertEqual(score['iou'], 1)
        self.assertFalse(score['semantic_approval'])
        wrong = semantic_label_metrics(['eye'], ['helmet'])
        self.assertEqual(wrong['precision'], 0)
        self.assertEqual(wrong['missing_parts'], {'eye': 1})

    def test_partial_masks_report_missing_and_extra_pixels(self):
        ref = Image.new('L', (5, 4)); ref.paste(255, (0, 0, 3, 4))
        out = Image.new('L', (5, 4)); out.paste(255, (2, 0, 5, 4))
        score = binary_mask_metrics(ref, out)
        self.assertEqual(score['intersection_pixels'], 4)
        self.assertEqual(score['union_pixels'], 20)
        self.assertEqual(score['missing_pixels'], 8)
        self.assertEqual(score['extra_pixels'], 8)
        self.assertEqual(score['iou'], .2)

    def test_empty_masks_are_unscored_not_perfect(self):
        score = binary_mask_metrics(Image.new('L', (2, 2)), Image.new('L', (2, 2)))
        self.assertIsNone(score['iou'])
        self.assertIsNone(score['precision'])
        self.assertIsNone(score['recall'])

    def test_no_resizing_fractional_alpha_or_rgb_masks(self):
        ref = Image.new('L', (2, 2), 255)
        for bad in [Image.new('L', (3, 2)), Image.new('L', (2, 2), 127), Image.new('RGB', (2, 2))]:
            with self.subTest(mode=bad.mode, size=bad.size), self.assertRaises(ValueError):
                binary_mask_metrics(ref, bad)

    def test_two_eyes_are_not_counted_as_one_complete_part(self):
        score = semantic_label_metrics(['eye', 'eye', 'mouth'], ['eye', 'mouth', 'horn'])
        self.assertEqual(score['matched_parts'], 2)
        self.assertEqual(score['missing_parts'], {'eye': 1})
        self.assertEqual(score['unsupported_or_incorrect_parts'], {'horn': 1})
        self.assertEqual(score['recall'], 2/3)

    def test_absent_proposals_and_references_are_explicitly_unscored(self):
        score = semantic_label_metrics(['eye'], [])
        self.assertIsNone(score['precision']); self.assertEqual(score['recall'], 0)
        self.assertIsNone(semantic_label_metrics([], [])['recall'])

    def test_malformed_or_excessive_labels_are_rejected(self):
        for values in ['eye', [True], [''], ['x'*257], ['eye']*129]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                semantic_label_metrics([], values)

if __name__ == '__main__': unittest.main()
