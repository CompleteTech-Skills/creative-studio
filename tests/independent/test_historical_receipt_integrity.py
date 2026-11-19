"""Compare acceptance of one real historical command receipt across two checkouts.

The baseline module encodes synthetic frames through its usual native workflow.
The only edit in the negative control is a copied historical probe observation.
No subprocess, capture schedule, clock, or on-disk media is patched.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image
import silent_video as current


def load_base():
    path = Path(os.environ['SILENT_RECEIPT_BASE']) / 'scripts' / 'silent_video.py'
    spec = importlib.util.spec_from_file_location('baseline_silent_video', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HistoricalReceiptIntegrity(unittest.TestCase):
    def test_unchanged_historical_receipt_passes_and_wrong_observed_bitrate_rejects(self):
        base = load_base()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = ['frame-%d.png' % index for index in range(6)]
            for index, name in enumerate(names):
                Image.new('RGB', (64, 48), (32 * index, 48, 96)).save(root / name)
            source = base.build_source(root, names, {'numerator': 24, 'denominator': 1})
            request = base.make_request(source)
            artifact = root / 'synthetic.mp4'
            historical = base.encode(request, root, artifact)
            probe = historical['probe']
            self.assertIn('-show_streams', probe['command'])
            self.assertIn('-show_format', probe['command'])
            self.assertNotIn('-show_entries', probe['command'])
            self.assertEqual(probe['exit_status'], 0)
            self.assertEqual(probe['stderr'], '')
            for implementation in (base, current):
                accepted = implementation.validate_receipt(historical, artifact, request, root=root)
                self.assertTrue(accepted['verified'])
                self.assertEqual(accepted['artifact_sha256'], base.file_hash(artifact))
            changed = copy.deepcopy(historical)
            observation = json.loads(probe['stdout'])
            original = observation['streams'][0]['bit_rate']
            changed_rate = str(int(original) + 1)
            observation['streams'][0]['bit_rate'] = changed_rate
            changed['probe']['stdout'] = json.dumps(observation)
            print('baseline probe_exit=%d timeline_exit=%d decode_exit=%d encode_exit=%d'
                  % tuple(historical[stage]['exit_status']
                          for stage in ('probe', 'timeline', 'decode', 'encode')), flush=True)
            print('artifact_sha256=%s source_sha256=%s request_sha256=%s observed_bitrate=%s copied_bitrate=%s'
                  % (base.file_hash(artifact), base._digest(source), base._digest(request),
                     original, changed_rate), flush=True)
            with self.assertRaisesRegex(ValueError, 'corrupt silent verification evidence'):
                base.validate_receipt(changed, artifact, request, root=root)
            print('unchanged receipt accepted by baseline and current; changed receipt rejected by baseline', flush=True)
            with self.assertRaisesRegex(ValueError, 'corrupt silent verification evidence'):
                current.validate_receipt(changed, artifact, request, root=root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
