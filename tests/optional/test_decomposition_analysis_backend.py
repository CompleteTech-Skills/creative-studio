"""Optional genuine local detector execution; no hand-authored semantic masks."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from asset_contracts import sha
from character_decomposition import create_decomposition, load_decomposition
import decomposition_analysis as analysis

ROOT=Path(__file__).resolve().parents[2]

@unittest.skipUnless(os.environ.get('HATCH_ANALYSIS_BACKEND_TEST') == '1',
                     'optional OpenCV backend; enable HATCH_ANALYSIS_BACKEND_TEST=1')
class RealAnalysisBackendTests(unittest.TestCase):
    def test_new_import_detects_face_and_eyes_without_masks_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'run'
            source=ROOT/'examples/decomposition/analysis/astronaut.png'
            original=sha(source)
            create_decomposition(source,root,background='keep',origin='NASA public-domain photograph from scikit-image v0.25.2; no semantic masks')
            job=analysis.prepare(root,'opencv-haar',enabled=True)
            result=analysis.run(root,job['job'])
            self.assertEqual(result['status'],'proposed')
            self.assertEqual(result['attempts'],1)
            folder=root/'analysis-jobs'/job['job']
            response=json.loads((folder/'response.json').read_text())
            categories=[x['category'] for x in response['annotation']['components']]
            self.assertIn('face/region',categories)
            self.assertIn('face/eye-region',categories)
            self.assertEqual(response['model'],job['request']['model'])
            self.assertEqual(response['evidence']['execution'],'local')
            self.assertGreaterEqual(response['evidence']['elapsed_seconds'],0)
            self.assertEqual(analysis.run(root,job['job'])['attempts'],1)
            analysis.apply(root,job['job'])
            current=load_decomposition(root)
            self.assertTrue(current['analysis']['semantic_model_called'])
            self.assertEqual(current['review']['status'],'pending')
            self.assertEqual(current['validation']['reconstruction_error']['max_channel_error'],0)
            self.assertEqual(current['source']['sha256'],original)
            self.assertEqual(sha(source),original)

    def test_unsupported_source_is_explicit_and_does_not_claim_semantics(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);source=home/'blank.png';Image.new('RGB',(64,64),'white').save(source)
            root=home/'run'
            create_decomposition(source,root,background='keep',origin='Synthetic blank test',fixture=True)
            before=(root/'decomposition.json').read_bytes()
            job=analysis.prepare(root,'opencv-haar',enabled=True)
            result=analysis.run(root,job['job'])
            self.assertEqual(result['status'],'unsupported')
            with self.assertRaises(ValueError):analysis.apply(root,job['job'])
            self.assertEqual(before,(root/'decomposition.json').read_bytes())

if __name__=='__main__':unittest.main()
