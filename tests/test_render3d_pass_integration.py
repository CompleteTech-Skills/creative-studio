"""Bound quality helper use in the actual Render3D backend, separate from legacy."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'tests'))
from render3d_contracts import Render3DError, make_frame_spec
from render3d_quality_binding import bind_quality_request, validate_quality_settings
from render3d_blender import _output_storage_estimate
from test_render3d_contracts import request


class QualityBinding(unittest.TestCase):
    def test_binding_is_explicit_immutable_and_rejects_changed_plan_or_code(self):
        source=request();bound=bind_quality_request(source)
        self.assertNotIn('quality_sha256',source['settings'])
        self.assertIsNotNone(validate_quality_settings(bound['settings']))
        self.assertIsNone(validate_quality_settings(source['settings']))
        changed=deepcopy(bound);changed['settings']['samples']+=1
        with self.assertRaises(Render3DError):validate_quality_settings(changed['settings'])
        with patch('render3d_quality_binding.adapter_hash',return_value='f'*64):
            with self.assertRaises(Render3DError):validate_quality_settings(bound['settings'])
            self.assertNotEqual(bind_quality_request(source)['settings']['quality_sha256'],bound['settings']['quality_sha256'])

    def test_auxiliary_admission_requires_binding_and_reserves_all_outputs(self):
        value=request();value['settings']['output'].update(format='exr',bit_depth=32,color_space='linear',passes=['combined','depth','normal','albedo'])
        with self.assertRaises(Render3DError):validate_quality_settings(value['settings'])
        bound=bind_quality_request(value);validate_quality_settings(bound['settings'])
        width,height=bound['settings']['resolution'];count=bound['frame_range']['end']-bound['frame_range']['start']
        self.assertEqual(_output_storage_estimate(bound),width*height*16*count*8)

    def test_code_binding_changes_frame_cache_before_reuse(self):
        from rig_test_helpers import fixture
        from render3d_snapshot import create_snapshot,NativeRigAdapter
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'native').mkdir();rig=fixture(root/'native',animated=False)
            create_snapshot(rig.root,root/'snapshot',NativeRigAdapter())
            value=request();payload=(root/'snapshot/snapshot.json').read_bytes()
            value['snapshot']=dict(path='snapshot.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())
            first=bind_quality_request(value)
            with patch('render3d_quality_binding.adapter_hash',return_value='f'*64):second=bind_quality_request(value)
            index=first['frame_range']['start']
            a=make_frame_spec(first,index,root/'snapshot/snapshot.json');b=make_frame_spec(second,index,root/'snapshot/snapshot.json')
            self.assertNotEqual(a['cache_key'],b['cache_key'])


@unittest.skipUnless(os.environ.get('HATCH_RENDER3D_PASS_TEST')=='1','actual bound backend pass gate not enabled')
class BoundNativeOutput(unittest.TestCase):
    def test_actual_bound_formats_passes_and_transparent_albedo(self):
        from rig_test_helpers import fixture
        from render3d_snapshot import create_snapshot,NativeRigAdapter
        from render3d_blender import BlenderBackend
        from render3d_verify import decode_image
        import numpy as np
        retained=os.environ.get('HATCH_RENDER3D_PASS_DIR')
        temporary=tempfile.TemporaryDirectory() if not retained else None
        root=Path(temporary.name if temporary else retained);root.mkdir(parents=True,exist_ok=True)
        (root/'native').mkdir();rig=fixture(root/'native',animated=False)
        create_snapshot(rig.root,root/'snapshot',NativeRigAdapter());payload=(root/'snapshot/snapshot.json').read_bytes()
        reports=[]
        for name,fmt,depth,passes in [('png8','png',8,['combined']),('png16','png',16,['combined']),
                                     ('exr16','exr',16,['combined','albedo']),
                                     ('exr32','exr',32,['combined','depth','normal','albedo'])]:
            value=request();value.update(id=name,camera='front',frame_range=dict(start=0,end=1))
            value['snapshot']=dict(path='snapshot.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())
            value['settings'].update(resolution=[64,64],samples=2,denoise=False)
            value['settings']['resources'].update(memory_bytes=2*1024**3,storage_bytes=128*1024**2,timeout_seconds=120)
            value['settings']['output'].update(format=fmt,bit_depth=depth,passes=passes,alpha='straight',color_space='srgb' if fmt=='png' else 'linear')
            value=bind_quality_request(value)
            result=BlenderBackend().render_frames(value,root/'snapshot',root/name)
            self.assertEqual(result['verification']['status'],'passed')
            self.assertEqual({v['pass_name'] for v in result['images']},set(passes))
            self.assertEqual(len(result['images']),len(passes))
            pixels={}
            for image in result['images']:
                metadata,pixels[image['pass_name']]=decode_image(root/name/image['file']['path'],output=value['settings']['output'],resolution=[64,64],pass_name=image['pass_name'])
                self.assertEqual(metadata['bit_depth'],depth)
            if 'albedo' in passes:
                self.assertGreater(np.count_nonzero(pixels['combined']['A']==0),0)
                np.testing.assert_array_equal(pixels['combined']['A'],pixels['albedo']['A'])
            manifest=json.loads((root/name/'manifest.json').read_text())
            self.assertEqual(manifest['worker']['quality']['plan']['passes'],passes)
            reports.append(dict(request=value,result=result))
        (root/'accepted.json').write_text(json.dumps(reports,indent=2))
        if temporary:temporary.cleanup()
