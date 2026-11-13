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
