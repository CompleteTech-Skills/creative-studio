"""Explicit local learned-model execution, separate from mandatory 2D discovery.

Set HATCH_CLIPSEG_BACKEND_TEST=1 and provision the exact pinned local snapshot.
This tests immutable loading and real execution, not accepted semantic quality.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import decomposition_clipseg as clip


@unittest.skipUnless(os.environ.get('HATCH_CLIPSEG_BACKEND_TEST') == '1',
                     'Explicit optional local CLIPSeg execution required')
class CapturedModelExecution(unittest.TestCase):
    def test_actual_model_loads_only_captured_bytes_after_all_external_names_replaced(self):
        if sys.platform != 'linux':
            self.fail('This explicit memory-admitted execution receipt is scoped to Linux')
        import resource
        available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                             if line.startswith('MemAvailable:')))
        self.assertGreaterEqual(available, 4*1024*1024, 'At least4GiB headroom required before model load')
        started = time.monotonic()
        import torch
        from PIL import Image
        from transformers import CLIPSegProcessor
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
        original = clip.model_directory()
        with tempfile.TemporaryDirectory(prefix='clipseg-model-proof-') as temporary:
            private = Path(temporary)
            for name in clip.FILES:
                shutil.copyfile(original/name, private/name)
            captured = clip._capture_model(private)
            baseline = CLIPSegProcessor.from_pretrained(private, local_files_only=True)
            for name in clip.FILES:
                (private/name).write_bytes(b'UNVERIFIED REPLACEMENT')
            with self.assertRaisesRegex(ValueError, 'differs from pinned'):
                clip._capture_model(private)
            model, processor = clip._model_and_processor(captured)
            self.assertGreater(sum(parameter.numel() for parameter in model.parameters()), 100_000_000)
            corrupt = dict(captured)
            corrupt['config.json'] = b'{}'
            with self.assertRaisesRegex(ValueError, 'changed before loading'):
                clip._model_and_processor(corrupt)
            del corrupt, captured
            vocabulary = ['head', 'eyes', 'mouth', 'ears', 'scarf', 'left eye', 'right eye',
                          "puppy's ears", 'résumé', 'MAß', '🦊']
            for text in vocabulary:
                self.assertEqual(baseline.tokenizer(text)['input_ids'], processor.tokenizer(text)['input_ids'])
            source = Path(__file__).resolve().parents[2]/'examples/decomposition/analysis/astronaut.png'
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            with Image.open(source) as image:
                rgb = image.convert('RGB')
            expected = baseline(text=['eyes', 'head'], images=[rgb, rgb], padding=True, return_tensors='pt')
            inputs = processor(text=['eyes', 'head'], images=[rgb, rgb], padding=True, return_tensors='pt')
            self.assertEqual(set(inputs), set(expected))
            for name in inputs:
                self.assertTrue(torch.equal(inputs[name], expected[name]), name)
            with torch.inference_mode():
                logits = model(**inputs).logits
            self.assertEqual(tuple(logits.shape), (2, 352, 352))
            self.assertTrue(bool(torch.isfinite(logits).all()))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            self.assertTrue(all((private/name).read_bytes() == b'UNVERIFIED REPLACEMENT' for name in clip.FILES))
            original_hashes = {name: clip._hash(original/name) for name in clip.FILES}
            self.assertEqual(original_hashes, clip.FILES)
            evidence = Path(os.environ.get('HATCH_CLIPSEG_EVIDENCE_DIRECTORY', temporary))
            evidence.mkdir(parents=True, exist_ok=True)
            import numpy as np
            np.save(evidence/'logits.npy', logits.cpu().numpy(), allow_pickle=False)
            receipt = {'kind': 'actual-captured-clipseg-model-execution', 'semantic_approval': False,
                'source_hashes': {name: hashlib.sha256((Path(clip.__file__).parent/name).read_bytes()).hexdigest()
                                  for name in ('decomposition_clipseg.py', 'decomposition_analysis.py', 'rig_errors.py')},
                'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'model_files': clip.FILES, 'original_model_hashes_after_proof': original_hashes,
                'all_external_model_names_replaced': True,
                'replacement_recapture_rejected': True, 'modified_captured_config_rejected': True,
                'tokenizer_parity_queries': len(vocabulary), 'processor_tensor_parity': list(inputs),
                'actual_logits_shape': list(logits.shape),
                'logits_sha256': hashlib.sha256((evidence/'logits.npy').read_bytes()).hexdigest(),
                'source_image_sha256': source_hash, 'source_unchanged': True,
                'torch_threads': torch.get_num_threads(), 'interop_threads': torch.get_num_interop_threads(),
                'memory_admission_kib': available, 'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'elapsed_seconds': time.monotonic()-started}
            (evidence/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')


if __name__ == '__main__':
    unittest.main()
