"""Compare admission of an identical repeated width on synthetic native receipts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from PIL import Image
import silent_video as current


class ReceiptParsers(unittest.TestCase):
    def test_same_duplicate_member_policy_for_fresh_and_saved_probe(self):
        spec = importlib.util.spec_from_file_location('base_media',
            Path(os.environ['SILENT_RECEIPT_BASE']) / 'scripts' / 'silent_video.py')
        base = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(base)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            names = ['frame-%d.png' % i for i in range(6)]
            for i, name in enumerate(names):
                Image.new('RGB', (64, 48), (32 * i, 48, 96)).save(root / name)
            req = base.make_request(base.build_source(root, names, {'numerator': 24, 'denominator': 1}))
            artifact = root / 'synthetic.mp4'
            previous = base.encode(req, root, artifact)
            fresh = current.verify_export(artifact, req, root=root)
            for style, receipt in [('current', fresh), ('historical', previous)]:
                with self.subTest(style=style):
                    self.assertTrue(current.validate_receipt(receipt, artifact, req, root=root)['verified'])
                    redundant = copy.deepcopy(receipt)
                    original = receipt['probe']['stdout']
                    # Repeat exactly the same genuine observed value. No media or claimed fact changes.
                    repeated, number = re.subn(r'("width"\s*:\s*64)(\s*,)',
                                              r'\1, \1\2', original, count=1)
                    self.assertEqual(number, 1)
                    self.assertEqual(json.loads(original), json.loads(repeated))
                    redundant['probe']['stdout'] = repeated
                    with self.assertRaisesRegex(ValueError, 'duplicate'):
                        current._probe_json(repeated)
                    print(f'{style} authentic receipt accepted; fresh parser refused repeated identical width',
                          flush=True)
                    with self.assertRaises(ValueError, msg=f'{style} saved receipt also must refuse duplicate'):
                        current.validate_receipt(redundant, artifact, req, root=root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
