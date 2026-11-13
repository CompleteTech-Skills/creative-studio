"""Actual CLI encode/verify with independently decoded zero-audio output."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import production
import silent_cli
import silent_video as media

class NativeCLI(unittest.TestCase):
    def test_preview_preparation_and_verified_movie(self):
        self.assertTrue(media.secure_publication_supported())
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); names=[]
            for index in range(3):
                name=f'{index:06}.png'; names.append(name)
                Image.new('RGB',(64,48),(index*60,80,40)).save(root/name)
            source=media.build_source(root,names,{'numerator':30000,'denominator':1001})
            result=silent_cli.preview(source,root,root/'preview.mp4',root/'preview-frames',width=32)
            self.assertEqual(result['prepared']['source']['width'],32)
            self.assertEqual(result['prepared']['source']['height'],24)
            self.assertEqual(result['verification']['audio_stream_count'],0)
            self.assertEqual(result['verification']['fps'],source['fps'])
            self.assertEqual(result['verification']['frame_count'],3)

    def test_cli_legacy_source_explicit_preparation_admission(self):
        # Malformed preparation fails before media execution; a complete legacy
        # source is validated by its authority rather than invented here.
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); Image.new('RGB',(64,48),(10,50,80)).save(root/'0.png')
            source=media.build_source(root,['0.png'],{'numerator':24,'denominator':1})
            output=io.StringIO()
            with patch.object(media,'source_from_production',return_value=source), contextlib.redirect_stdout(output):
                status=production.main(['export-video','--render-dir',str(root),'--destination',str(root/'film.mp4'),
                    '--silent','--resolution','32x24','--fit','letterbox','--fps','30','--fps-policy','nearest',
                    '--prepared-dir',str(root/'prepared')])
            self.assertEqual(status,0)
            result=json.loads(output.getvalue())
            self.assertEqual(result['verification']['audio_stream_count'],0)
            self.assertEqual(result['preparation']['source']['width'],32)
            self.assertEqual(result['verification']['fps'],{'numerator':30,'denominator':1})

    def test_request_encode_and_independent_verify(self):
        self.assertTrue(media.secure_publication_supported())
        self.assertTrue(shutil.which('ffmpeg'))
        self.assertTrue(shutil.which('ffprobe'))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); names=[]
            for index in range(3):
                name=f'{index:06}.png'; names.append(name)
                Image.new('RGB',(32,24),(index*60,50,80)).save(root/name)
            source=media.build_source(root,names,{'numerator':24,'denominator':1})
            request=root/'request.json'; request.write_text(json.dumps(media.make_request(source)))
            output=root/'movie.mp4'; receipt=root/'receipt.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(production.main(['export-video','--request',str(request),'--root',str(root),
                    '--destination',str(output),'--silent','--output',str(receipt)]),0)
            saved=json.loads(receipt.read_text()); self.assertEqual(saved['audio_stream_count'],0)
            result=io.StringIO()
            with contextlib.redirect_stdout(result):
                self.assertEqual(production.main(['verify-export','--request',str(request),'--root',str(root),
                    '--artifact',str(output),'--require-silent']),0)
            checked=json.loads(result.getvalue())
            self.assertEqual(checked['audio_stream_count'],0)
            self.assertEqual(checked['frame_count'],3)

class ReviewAssets(unittest.TestCase):
    def test_actual_source_pixels_and_hashes_are_retained(self):
        import hashlib
        from PIL import Image
        import silent_video as media
        self.assertTrue(media.secure_publication_supported())
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); frames=[]
            for i in range(2):
                name=f'{i}.png'; frames.append(name)
                Image.new('RGB',(40,30),(50+i*100,20,30)).save(root/name)
            source=media.build_source(root,frames,{'numerator':24,'denominator':1})
            out=root/'review'; report=silent_cli.review_assets(source,root,out,indices=[1,0])
            self.assertEqual((out/'poster.png').read_bytes(),(root/'1.png').read_bytes())
            self.assertEqual([v['index'] for v in report['frames']],[1,0])
            for artifact in report['artifacts']:
                data=(out/artifact['path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(),artifact['sha256'])
            with Image.open(out/'contact-sheet.png') as image:self.assertEqual(image.size,(640,180))
            (root/'0.png').write_bytes(b'changed')
            with self.assertRaises(ValueError):silent_cli.review_assets(source,root,root/'stale',indices=[0])
            self.assertFalse((root/'stale').exists())

if __name__=='__main__':unittest.main()
