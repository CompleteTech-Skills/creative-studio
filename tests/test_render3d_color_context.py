"""Color-profile cache binding and renderer-owned receipt boundaries."""
from copy import deepcopy
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'tests'))
from render3d_contracts import Render3DError
from render3d_quality_binding import bind_quality_request, color_profile, validate_quality_settings
from render3d_color_context import color_context
from test_render3d_contracts import request


class ColorContext(unittest.TestCase):
    def test_custom_or_unresolved_config_fails_closed(self):
        for name in ('OCIO','BLENDER_OCIO'):
            with patch.dict(os.environ,{name:'/untrusted/config.ocio'}):
                with self.assertRaises(Render3DError):color_profile('/missing/blender')

    def test_changed_lut_closure_invalidates_bound_request(self):
        first=dict(closure_sha256='a'*64,files=[])
        second=deepcopy(first);second['closure_sha256']='b'*64
        with patch('render3d_quality_binding.color_profile',return_value=(Path('/unused'),first)):
            bound=bind_quality_request(request())
        with patch('render3d_quality_binding.color_profile',return_value=(Path('/unused'),second)):
            with self.assertRaises(Render3DError):validate_quality_settings(bound['settings'])
            self.assertNotEqual(bind_quality_request(request())['settings']['quality_sha256'],bound['settings']['quality_sha256'])

    def test_actual_state_receipt_retains_threshold_and_unknowns(self):
        inventory=dict(config_path='config.ocio',config_sha256='a'*64,closure_sha256='b'*64,
                       declared_roles={'scene_linear':'actual-config-scene-role'})
        scene=NS(render=NS(image_settings=NS(color_management='FOLLOW_SCENE',file_format='OPEN_EXR',color_mode='RGBA',color_depth='32'),film_transparent=True),
                 view_settings=NS(view_transform='Standard',look='None',exposure=1.25,gamma=1),
                 display_settings=NS(display_device='sRGB'),
                 view_layers=[NS(name='native-layer',use_pass_z=True,use_pass_normal=False,use_pass_diffuse_color=True,pass_alpha_threshold=.5)])
        with patch.dict(os.environ,OCIO='/owned/profile/config.ocio'),patch('cinematics3d_color.verify_config_bytes',return_value='b'*64):
            receipt=color_context(scene,'/owned/profile',inventory,{'alpha':'straight'})
            self.assertEqual(receipt['exposure'],1.25)
            self.assertEqual(receipt['passes'][0]['alpha_threshold'],.5)
            self.assertIsNone(receipt['numeric_primaries'])
            self.assertEqual(receipt['alpha_association'],'unverified')
        with patch.dict(os.environ,OCIO='/different/config.ocio'):
            with self.assertRaises(Render3DError):color_context(scene,'/owned/profile',inventory,{'alpha':'straight'})


class CompletedCacheBinding(unittest.TestCase):
    def test_complete_cache_rejects_changed_profile_or_backend_before_return(self):
        from render3d_quality_jobs import BoundQualityJob
        from test_render3d_contracts import backend as backend_identity
        inventory=dict(closure_sha256='a'*64,files=[])
        with patch('render3d_quality_binding.color_profile',return_value=(Path('/unused'),inventory)):
            value=bind_quality_request(request())
            current=backend_identity()
            store=NS(status=lambda job:dict(state='complete',request=value,backend=deepcopy(current),snapshot='/owned/snapshot'))
            backend=NS(executable='/owned/blender',inspect_version=lambda:deepcopy(current))
            client=BoundQualityJob(store,backend,value,'/owned/snapshot',lambda *args:None)
            self.assertEqual(client.status('job')['state'],'complete')
            changed=deepcopy(current);changed['worker_sha256']='c'*64
            client.identity_provider=lambda:changed
            with self.assertRaises(Render3DError):client.status('job')
            client.identity_provider=lambda:deepcopy(current)
            with patch('render3d_quality_binding.adapter_hash',return_value='f'*64):
                with self.assertRaises(Render3DError):client.run_one('job')


class ArtifactContracts(unittest.TestCase):
    def candidate(self):
        from test_render3d_contracts import backend
        return dict(schema_version=1,kind='render3d-result',job='job',request_sha256='0'*64,
            snapshot_sha256='1'*64,backend=backend(),status='partial',requested_frames=[0],
            successful=[0],failed=[],skipped=[],reused=[],images=[],diagnostics=[],
            usage=dict(elapsed_seconds=None,peak_memory_bytes=None,output_bytes=None,measurement='unavailable'),
            logs=[],manifest=None,verification=dict(status='not-run',verifier=None,evidence=None))
    def test_optional_companion_refs_are_bounded_and_portable(self):
        from render3d_contracts import validate
        value=self.candidate();validate(value,'RenderResult')
        value['artifacts']=[];validate(value,'RenderResult')
        ref=dict(path='color-config/config.ocio',sha256='a'*64,bytes=123)
        value['artifacts']=[ref];validate(value,'RenderResult')
        for refs in ([ref,ref],[ref,dict(ref,path='Color-Config/config.ocio')],
                     [ref,dict(ref,path='color-config')],[dict(ref,bytes=256*1024**2+1)],
                     [dict(ref,path='../escape')]):
            with self.subTest(refs=refs),self.assertRaises(Render3DError):
                validate(dict(value,artifacts=refs),'RenderResult')
