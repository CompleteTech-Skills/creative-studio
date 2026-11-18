"""Optional genuine local detector execution; no hand-authored semantic masks."""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_memory_classifier_hash_matches_loaded_bytes_after_model_path_replacement(self):
        import cv2
        installed=Path(cv2.data.haarcascades)
        originals={name:(installed/name).read_bytes() for name in analysis.MODEL_FILES}
        alternate=(installed/'haarcascade_frontalface_alt2.xml').read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            models=Path(temp).resolve()/'models';models.mkdir()
            for name,payload in originals.items(): (models/name).write_bytes(payload)
            with mock.patch.object(cv2.data,'haarcascades',str(models)+os.sep):
                model,captured=analysis._capture_model()
            face_name=analysis.MODEL_FILES[0]
            expected=next(item['sha256'] for item in model['files'] if item['name']==face_name)
            self.assertEqual(hashlib.sha256(captured[face_name]).hexdigest(),expected)
            (models/face_name).write_bytes(alternate)
            alternate_window=cv2.CascadeClassifier(str(models/face_name)).getOriginalWindowSize()
            constructor=cv2.CascadeClassifier;storage=cv2.FileStorage;seen=[]
            def memory_storage(xml,flags):
                self.assertTrue(flags & cv2.FILE_STORAGE_MEMORY)
                seen.append(hashlib.sha256(xml.encode('utf-8')).hexdigest())
                return storage(xml,flags)
            def no_filename_constructor(*args,**kwargs):
                self.assertEqual(args,());self.assertEqual(kwargs,{})
                return constructor()
            with mock.patch.object(cv2,'FileStorage',side_effect=memory_storage), \
                    mock.patch.object(cv2,'CascadeClassifier',side_effect=no_filename_constructor):
                classifier=analysis._classifier_from_xml(cv2,captured[face_name],face_name)
            self.assertEqual(seen,[expected])
            self.assertNotEqual(classifier.getOriginalWindowSize(),alternate_window)
            self.assertEqual(classifier.getOriginalWindowSize(),(24,24))
        for name,payload in originals.items(): self.assertEqual((installed/name).read_bytes(),payload)

    def test_actual_worker_identity_survives_model_replacement_inside_real_constructor(self):
        import cv2
        installed=Path(cv2.data.haarcascades)
        originals={name:(installed/name).read_bytes() for name in analysis.MODEL_FILES}
        alternate=(installed/'haarcascade_frontalface_alt2.xml').read_bytes()
        source=ROOT/'examples/decomposition/analysis/astronaut.png';source_hash=sha(source)
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp).resolve();models=home/'models';models.mkdir();root=home/'run'
            for name,payload in originals.items(): (models/name).write_bytes(payload)
            create_decomposition(source,root,background='keep',origin='NASA source; real model-byte race regression')
            constructor=cv2.CascadeClassifier;loader=analysis._classifier_from_xml;calls=[];loaded=[]
            def replace_and_restore(*args,**kwargs):
                self.assertEqual(args,());self.assertEqual(kwargs,{})
                face=models/analysis.MODEL_FILES[0];prior=face.read_bytes();face.write_bytes(alternate)
                try:
                    calls.append(hashlib.sha256(face.read_bytes()).hexdigest())
                    return constructor()
                finally: face.write_bytes(prior)
            replace_and_restore.read=constructor.read
            def witness(module,payload,name):
                result=loader(module,payload,name)
                loaded.append((name,hashlib.sha256(payload).hexdigest(),result.getOriginalWindowSize()))
                return result
            with mock.patch.object(cv2.data,'haarcascades',str(models)+os.sep):
                job=analysis.prepare(root,'opencv-haar',enabled=True,max_seconds=120)
                with mock.patch.object(cv2,'CascadeClassifier',new=replace_and_restore), \
                        mock.patch.object(analysis,'_classifier_from_xml',side_effect=witness):
                    analysis._worker(root,job['job'])
                response=json.loads((root/'analysis-jobs'/job['job']/'worker-response.json').read_text())
            self.assertEqual(response['status'],'proposed')
            self.assertEqual(response['model'],job['request']['model'])
            expected={item['name']:item['sha256'] for item in job['request']['model']['files']}
            self.assertEqual({name:digest for name,digest,_ in loaded},expected)
            self.assertEqual(loaded[0][2],(24,24))
            self.assertTrue(calls);self.assertNotEqual(calls[0],expected[analysis.MODEL_FILES[0]])
            self.assertEqual(sha(source),source_hash)
            for name,payload in originals.items(): self.assertEqual((models/name).read_bytes(),payload)
        for name,payload in originals.items(): self.assertEqual((installed/name).read_bytes(),payload)

    def test_model_changed_before_capture_rejects_without_publishing_response(self):
        import cv2
        installed=Path(cv2.data.haarcascades)
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp).resolve();models=home/'models';models.mkdir();root=home/'run'
            for name in analysis.MODEL_FILES: (models/name).write_bytes((installed/name).read_bytes())
            source=ROOT/'examples/decomposition/analysis/astronaut.png'
            create_decomposition(source,root,background='keep',origin='NASA source; model mismatch rejection regression')
            before=(root/'decomposition.json').read_bytes()
            with mock.patch.object(cv2.data,'haarcascades',str(models)+os.sep):
                job=analysis.prepare(root,'opencv-haar',enabled=True)
                (models/analysis.MODEL_FILES[0]).write_bytes((installed/'haarcascade_frontalface_alt2.xml').read_bytes())
                with self.assertRaisesRegex(ValueError,'identity changed'): analysis._worker(root,job['job'])
            self.assertFalse((root/'analysis-jobs'/job['job']/'worker-response.json').exists())
            self.assertEqual((root/'decomposition.json').read_bytes(),before)

    def test_malformed_captured_cascade_fails_explicitly(self):
        import cv2
        for payload in (b'not an XML cascade',b'\xff',b'<?xml version="1.0"?><opencv_storage><number>1</number></opencv_storage>'):
            with self.subTest(payload=repr(payload)):
                with self.assertRaisesRegex(ValueError,'cannot load captured cascade'):
                    analysis._classifier_from_xml(cv2,payload,'synthetic-malformed.xml')

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
