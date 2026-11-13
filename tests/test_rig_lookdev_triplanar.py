import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from lookdev_triplanar import triplanar_plan
from rig_errors import RigError


class TriplanarAdmissionTests(unittest.TestCase):
    def document(self):
        return json.loads((Path(__file__).resolve().parents[1] / 'examples/lookdev-contracts/albedo.json').read_text())

    def test_canonical_density_and_bounded_sharpness(self):
        result = triplanar_plan(self.document(), scale=3, sharpness=64)
        self.assertEqual(result['projection'], 'canonical-world-triplanar')
        self.assertEqual(result['scale_per_meter'], 3)
        self.assertEqual(result['sharpness'], 64)
        self.assertEqual(len(result['adapter_source_sha256']), 64)

    def test_conversion_cannot_escape_structured_admission(self):
        for key in ('scale', 'sharpness'):
            for value in (10**1000, float('inf'), float('nan'), True, None, '1', 0, -1):
                with self.subTest(key=key, value=type(value).__name__):
                    with self.assertRaises(RigError):
                        triplanar_plan(self.document(), **{key:value})

    def test_normal_reorientation_and_unsupported_sampling_reject(self):
        original = self.document()
        normal = copy.deepcopy(original)
        normal.update(role='normal', encoding='data', normal_orientation='opengl-positive-y')
        with self.assertRaises(RigError) as caught: triplanar_plan(normal)
        self.assertEqual(caught.exception.code, 'TEXTURE_PROJECTION_UNSUPPORTED')
        for key, value in (('wrap', 'mirror'), ('filter', 'mipmap')):
            changed = dict(original); changed[key] = value
            with self.assertRaises(RigError): triplanar_plan(changed)
        self.assertEqual(original, self.document())
