"""Contract/security mechanics only; synthetic sources do not prove model quality."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import decomposition_clipseg as clip
import decomposition_analysis as analysis
from character_decomposition import create_decomposition

class ClipSegContracts(unittest.TestCase):
    def test_missing_snapshot_never_downloads(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ValueError,'explicitly provision'):
            clip.model_directory()

    def test_model_files_are_pinned_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json';path.write_bytes(b'contract fixture')
            with patch.dict(os.environ,{'HATCH_CLIPSEG_MODEL_DIR':temp}),patch.object(clip,'FILES',{'config.json':clip._hash(path)}):
                self.assertEqual(clip.model_directory(),Path(temp).resolve())
                path.write_bytes(b'altered')
                with self.assertRaisesRegex(ValueError,'pinned'):clip.model_directory()
                path.unlink();target=Path(temp)/'actual.json';target.write_bytes(b'contract fixture');path.symlink_to(target)
                with self.assertRaisesRegex(ValueError,'pinned'):clip.model_directory()

    def test_streamed_model_hash_uses_explicit_byte_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'model.bin';path.write_bytes(b'123456789')
            with patch.object(clip,'MAX_MODEL_FILE_BYTES',8):
                with self.assertRaisesRegex(ValueError,'byte bound'):clip._hash(path)
            self.assertEqual(clip._hash(path), __import__('hashlib').sha256(b'123456789').hexdigest())

    def test_capture_binds_immutable_bytes_and_rejects_later_replacement(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();path=root/'config.json';path.write_bytes(b'{"fixture":true}')
            pinned={'config.json':hashlib.sha256(path.read_bytes()).hexdigest()}
            with patch.object(clip,'FILES',pinned):
                captured=clip._capture_model(root)
                path.write_bytes(b'{"fixture":false}')
                self.assertEqual(captured['config.json'],b'{"fixture":true}')
                with self.assertRaisesRegex(ValueError,'differs from pinned'):clip._capture_model(root)

    def test_capture_rejects_symlink_directory_instead_of_resolving_it(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp).resolve();root=base/'actual';root.mkdir()
            path=root/'config.json';path.write_bytes(b'{}')
            alias=base/'alias';alias.symlink_to(root,target_is_directory=True)
            with patch.object(clip,'FILES',{'config.json':hashlib.sha256(b'{}').hexdigest()}):
                with self.assertRaises(ValueError):clip._capture_model(alias)

    def test_capture_rejects_symlink_ancestor_and_accepts_canonical_path(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp).resolve();actual=base/'actual';actual.mkdir()
            root=actual/'model';root.mkdir();(root/'config.json').write_bytes(b'{}')
            alias=base/'alias';alias.symlink_to(actual,target_is_directory=True)
            with patch.object(clip,'FILES',{'config.json':hashlib.sha256(b'{}').hexdigest()}):
                with self.assertRaises(ValueError):clip._capture_model(alias/'model')
                self.assertEqual(clip._capture_model((alias/'model').resolve()),{'config.json':b'{}'})

    def test_capture_enforces_configuration_and_aggregate_bounds(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();path=root/'config.json';path.write_bytes(b'123456789')
            pinned={'config.json':hashlib.sha256(path.read_bytes()).hexdigest()}
            with patch.object(clip,'FILES',pinned):
                with patch.object(clip,'MAX_CONFIG_FILE_BYTES',8),self.assertRaisesRegex(ValueError,'byte bound'):
                    clip._capture_model(root)
                with patch.object(clip,'MAX_CAPTURE_BYTES',8),self.assertRaisesRegex(ValueError,'byte bound'):
                    clip._capture_model(root)

    def test_invalid_thresholds_duplicate_taxonomy_and_prompt_rejected(self):
        request={'taxonomy':['anatomy/head'],'prompt':'','parameters':dict(clip.DEFAULT_PARAMETERS)}
        clip.validate_options(request)
        cases=[{**request,'taxonomy':['head','head']},{**request,'taxonomy':['x']*33},
               {**request,'prompt':'invent hidden wings'},
               {**request,'taxonomy':['x'*129]},
               {**request,'parameters':{'mask_threshold':float('nan'),'presence_threshold':.7}},
               {**request,'parameters':{'mask_threshold':True,'presence_threshold':.7}},
               {**request,'parameters':{'mask_threshold':.5,'presence_threshold':1}},
               {**request,'parameters':{**clip.DEFAULT_PARAMETERS,'unsupported':1}}]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(ValueError):clip.validate_options(case)

    def test_input_hash_and_decode_share_admitted_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'source.png';mask=root/'mask.png'
            Image.new('RGBA',(8,6),(40,80,120,77)).save(source)
            Image.new('L',(8,6),255).save(mask)
            request={'source':{'path':'source.png','sha256':clip._hash(source),'width':8,'height':6},
                     'references':[{'path':'mask.png','sha256':clip._hash(mask)}],
                     'policy':{'max_pixels':48}}
            from decomposition_analysis import _record_bytes
            def replace_after_read(path, maximum):
                data=_record_bytes(path,maximum)
                if Path(path)==source:Image.new('RGBA',(8,6),(255,0,0,255)).save(source)
                return data
            with patch.object(analysis,'_record_bytes',side_effect=replace_after_read):
                rgb,foreground=clip._request_images(root,request)
            self.assertEqual(rgb.getpixel((0,0)),(40,80,120))
            self.assertEqual(foreground.size,(8,6))
            with self.assertRaisesRegex(ValueError,'bytes changed'):clip._request_images(root,request)

    def test_mismatched_reference_geometry_and_resource_bounds_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'source.png';mask=root/'mask.png'
            Image.new('RGBA',(8,6)).save(source);Image.new('L',(4,6),255).save(mask)
            request={'source':{'path':'source.png','sha256':clip._hash(source),'width':8,'height':6},
                     'references':[{'path':'mask.png','sha256':clip._hash(mask)}],
                     'policy':{'max_pixels':48}}
            with self.assertRaisesRegex(ValueError,'size changed'):clip._request_images(root,request)
            request['policy']['max_pixels']=47
            with self.assertRaisesRegex(ValueError,'admitted image contract'):clip._request_images(root,request)

    def test_analysis_job_binds_new_backend_and_parameters_without_inference(self):
        model={'provider':'test-fixture','name':'mechanics-only','version':'0', 'files':[],
               'identity_evidence':'Synthetic contract fixture; no model call.'}
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.png';Image.new('RGBA',(8,8),(50,100,200,127)).save(source)
            run=Path(temp)/'run';create_decomposition(source,run,background='alpha',origin='Synthetic mechanics fixture',fixture=True)
            with patch.object(clip,'identity',return_value=model):
                prepared=analysis.prepare(run,'clipseg',enabled=True)
            self.assertEqual(prepared['status'],'ready')
            self.assertEqual(prepared['attempts'],0)
            self.assertEqual(prepared['request']['parameters'],clip.DEFAULT_PARAMETERS)
            self.assertEqual(prepared['request']['taxonomy'],clip.DEFAULT_TAXONOMY)
            self.assertEqual(prepared['request']['model'],model)
            self.assertFalse((run/'analysis-jobs'/prepared['job']/'worker-response.json').exists())
            with patch.object(clip,'identity',return_value=model),self.assertRaises(ValueError):
                analysis.prepare(run,'clipseg',enabled=True,parameters={'mask_threshold':2,'presence_threshold':.7})

if __name__=='__main__':unittest.main()
