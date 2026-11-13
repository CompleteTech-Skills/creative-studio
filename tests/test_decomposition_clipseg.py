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
                self.assertEqual(clip.model_directory(),Path(temp))
                path.write_bytes(b'altered')
                with self.assertRaisesRegex(ValueError,'pinned'):clip.model_directory()
                path.unlink();target=Path(temp)/'actual.json';target.write_bytes(b'contract fixture');path.symlink_to(target)
                with self.assertRaisesRegex(ValueError,'pinned'):clip.model_directory()

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
