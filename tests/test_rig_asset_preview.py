"""Actual owner geometry and optional Blender asset-preview integration."""
from copy import deepcopy
from fractions import Fraction
from itertools import product
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from asset_library import AssetLibrary, file_record
from asset_library_contracts import seal
from asset_library_preview import settings, _geometry, _scene, _request, _cache_path, _check_geometry_budget, create_preview, verify_preview
from rig_demo import ellipsoid, create_demo
from rig_gltf import export_geometry, export_gltf
from rig_jobs import create_run
from rig_models import Geometry
from rig_errors import RigError
from render3d_contracts import validate, Render3DError
from render3d_snapshot import Scene3DAdapter, create_snapshot

ROOT=Path(__file__).resolve().parents[1]


def register(library, source, version='1.0.0'):
    license=source.parent/'license.txt';license.write_text('Procedural engineering test fixture; CC0-1.0.\n')
    manifest=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
    files=[file_record(source,'runtime/asset.glb','runtime'),file_record(license,'source/license.txt','license')]
    manifest.update(version=version,files=files)
    manifest['provenance']['sources']=[files[0]];manifest['provenance']['license_evidence']=[files[1]]
    manifest['publication']['inventory']=[file['path'] for file in files]
    return library.register(seal(manifest),{'runtime/asset.glb':source,'source/license.txt':license})


class Preview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        mesh=ellipsoid('sphere',[0,0,0],[1,.8,1],0,latitudes=6,longitudes=10)
        mesh.attributes['TEXCOORD_0']=np.column_stack(((mesh.positions[:,0]+1)/2,(mesh.positions[:,1]+1)/2))
        self.geometry=Geometry([mesh],[dict(pbrMetallicRoughness=dict(baseColorFactor=[.7,.1,.05,1],metallicFactor=.1,roughnessFactor=.4))])
        self.source=self.root/'sphere.glb';export_geometry(self.geometry,self.source,provenance=dict(fixture=True))
        self.rig=create_run(self.source,self.root/'rig',fixture=True)

    def test_strict_settings_and_rational_times(self):
        self.assertEqual(settings({'times':['0.5']})['times'],['1/2'])
        for value in ({'unknown':True},{'width':True},{'mode':'invented'},{'times':['1/0']},
                      {'times':['1e100']},{'times':['0','1']},{'mode':'motion'},
                      {'background':[float('nan'),0,0]},{'material':{}}):
            with self.subTest(value=value),self.assertRaises(RigError):settings(value)

    def test_cache_paths_admit_owned_hidden_artifacts_without_traversal(self):
        self.assertEqual(_cache_path('.native.rig.lock'),'.native.rig.lock')
        self.assertEqual(_cache_path('.config/blender/4.5/extensions/.cache/compat.dat'),'.config/blender/4.5/extensions/.cache/compat.dat')
        for value in ('../escape','.', '..double','.hidden/../escape','a/../b','/absolute','a\\b'):
            with self.subTest(value=value),self.assertRaises(RigError):_cache_path(value)

    def test_sample_memory_and_inspection_edges_are_bounded(self):
        self.geometry.meshes[0].morphs={'stretch':{'POSITION':self.geometry.meshes[0].positions.copy()}}
        _check_geometry_budget(self.geometry,1)
        with patch('asset_library_preview.MAX_GEOMETRY_BYTES',1),self.assertRaises(RigError):
            _check_geometry_budget(self.geometry,32)
        mesh=ellipsoid('dense',[0,0,0],[1,1,1],0,latitudes=40,longitudes=80)
        dense=deepcopy(self.rig);dense.geometry=Geometry([mesh],deepcopy(self.geometry.materials))
        with patch('rig_demo.tube',side_effect=AssertionError('must reject before tube allocation')):
            with self.assertRaises(RigError):_geometry(dense,settings({'mode':'wireframe'}),Fraction(0))

    def test_true_wireframe_and_uv_geometry(self):
        wire=_geometry(self.rig,settings({'mode':'wireframe'}),Fraction(0))
        uv=_geometry(self.rig,settings({'mode':'uv'}),Fraction(0))
        self.assertGreater(wire.triangle_count,self.geometry.triangle_count)
        self.assertTrue(np.allclose(np.concatenate([m.positions for m in uv.meshes])[:,2],0,atol=.01))
        self.assertFalse(np.array_equal(wire.meshes[0].positions,uv.meshes[0].positions))
        self.assertEqual(self.rig.geometry.triangle_count,self.geometry.triangle_count)

    def test_missing_skeleton_uv_and_clip_fail_explicitly(self):
        with self.assertRaises(RigError):_geometry(self.rig,settings({'mode':'skeleton'}),Fraction(0))
        self.rig.geometry.meshes[0].attributes.pop('TEXCOORD_0')
        with self.assertRaises(RigError):_geometry(self.rig,settings({'mode':'uv'}),Fraction(0))
        with self.assertRaises(RigError):_geometry(self.rig,settings({'mode':'motion','clip':'missing'}),Fraction(0))

    def test_scene_snapshot_and_request_use_existing_owners(self):
        for mode in ('perspective','orthographic','material','environment'):
            config=settings({'mode':mode});target=self.root/mode
            _scene(target,self.geometry,config,dict(origin='procedural-fixture',fixture=True,tool='test',inputs=[],attribution='fixture',license='CC0-1.0'))
            snapshot=self.root/(mode+'-snapshot');create_snapshot(target,snapshot,Scene3DAdapter())
            validate(_request(snapshot,config))
            scene=json.loads((target/'scene.json').read_text())
            camera=next(n for n in scene['nodes'] if n['id']=='camera')
            self.assertEqual(camera['camera']['projection'],'perspective' if mode=='perspective' else 'orthographic')

    def test_camera_fits_portrait_landscape_and_shared_motion_bounds(self):
        from rig_math import qmatrix
        bounds=(np.array([-3,-2,-1]),np.array([3,2,1]))
        corners=np.array(list(product(*zip(*bounds))))
        for mode in ('perspective','orthographic'):
            for width,height in ((64,1024),(1024,64),(256,256)):
                config=settings(dict(mode=mode,width=width,height=height))
                target=self.root/f'{mode}-{width}-{height}'
                _scene(target,self.geometry,config,dict(origin='procedural-fixture',fixture=True,tool='test',inputs=[],attribution='fixture',license='CC0-1.0'),bounds)
                camera=next(n for n in json.loads((target/'scene.json').read_text())['nodes'] if n['id']=='camera')
                rotation=qmatrix(camera['transform']['rotation'])[:3,:3]
                local=(corners-np.array(camera['transform']['translation']))@rotation
                info=camera['camera'];aspect=width/height
                if mode=='perspective':
                    halfheight=-local[:,2]*np.tan(info['vertical_fov']/2)
                else:
                    halfheight=np.full(len(corners),info['orthographic_height']/2)
                self.assertTrue(np.all(np.abs(local[:,0])<halfheight*aspect))
                self.assertTrue(np.all(np.abs(local[:,1])<halfheight))

    def test_material_override_is_explicit_and_does_not_modify_source(self):
        before=deepcopy(self.rig.geometry.materials)
        config=settings({'mode':'material','material':{'base_color':[0,1,0,1],'metallic':.9,'roughness':.1}})
        changed=_geometry(self.rig,config,Fraction(0))
        self.assertEqual(changed.materials[0]['pbrMetallicRoughness']['baseColorFactor'],[0,1,0,1])
        self.assertEqual(self.rig.geometry.materials,before)

    def test_missing_renderer_does_not_publish_or_change_readiness(self):
        library=AssetLibrary(self.root/'library',create=True);reference=register(library,self.source)
        before=library.resolve(reference);cache=self.root/'absent-cache'
        with self.assertRaises(Render3DError):
            create_preview(library,reference,cache,executable=str(self.root/'missing-blender'))
        self.assertFalse(cache.exists())
        self.assertEqual(library.resolve(reference),before)

    @unittest.skipUnless(os.environ.get('HATCH_ASSET_PREVIEW_TEST')=='1','Explicit real Blender preview acceptance not requested')
    def test_real_render_cache_changes_and_corruption(self):
        library=AssetLibrary(self.root/'library',create=True);reference=register(library,self.source)
        cache=self.root/'previews';executable=os.environ['HATCH_BLENDER']
        red=create_preview(library,reference,cache,config={'width':64,'height':64,'samples':2},executable=executable)
        self.assertEqual(red['asset_readiness'],'unchanged')
        self.assertEqual(create_preview(library,reference,cache,config={'width':64,'height':64,'samples':2},executable=executable),red)
        green=create_preview(library,reference,cache,config={'width':64,'height':64,'samples':2,'material':{'base_color':[0,1,0,1],'metallic':0,'roughness':.5}},executable=executable)
        self.assertNotEqual(red['images'][0]['sha256'],green['images'][0]['sha256'])
        from PIL import Image
        with Image.open(cache/red['key']/red['images'][0]['path']) as picture:
            red_pixels=np.asarray(picture).copy()
        with Image.open(cache/green['key']/green['images'][0]['path']) as picture:
            self.assertGreater(np.abs(red_pixels.astype(float)-np.asarray(picture)).mean(),1)
        changed=deepcopy(self.geometry)
        changed.meshes[0].positions[:,0]*=0.3
        model=self.root/'changed.glb';export_geometry(changed,model,provenance=dict(fixture=True))
        next_reference=register(library,model,version='1.0.1')
        next_preview=create_preview(library,next_reference,cache,config={'width':64,'height':64,'samples':2},executable=executable)
        self.assertNotEqual(red['key'],next_preview['key'])
        with Image.open(cache/next_preview['key']/next_preview['images'][0]['path']) as picture:
            self.assertGreater(np.abs(red_pixels.astype(float)-np.asarray(picture)).mean(),1)
        retained={p.name for p in cache.iterdir()}
        with self.assertRaises(RigError):
            create_preview(library,reference,cache,config={'mode':'idle','width':64,'height':64,'samples':1},
                           executable=executable,cancelled=lambda:True)
        self.assertEqual({p.name for p in cache.iterdir()},retained)
        with Image.open(cache/red['key']/red['images'][0]['path']) as picture:
            self.assertGreater(np.asarray(picture).std(),1)
        for mode in ('orthographic','wireframe','uv','environment','material'):
            record=create_preview(library,reference,cache,config={'mode':mode,'width':64,'height':64,'samples':1},executable=executable)
            self.assertEqual(record['verification'],'decoded-and-hash-verified')
        (cache/red['key']/red['images'][0]['path']).write_bytes(b'corrupt')
        with self.assertRaises(RigError):create_preview(library,reference,cache,config={'width':64,'height':64,'samples':2},executable=executable)
        self.assertTrue((cache/green['key']/green['images'][0]['path']).is_file())

    @unittest.skipUnless(os.environ.get('HATCH_ASSET_PREVIEW_TEST')=='1','Explicit real Blender preview acceptance not requested')
    def test_real_motion_and_skeleton(self):
        demo=self.root/'robot';create_demo(demo,'robotic',fps=4)
        from rig_models import load_rig
        robot=load_rig(demo);source=self.root/'robot.glb';export_gltf(robot,source)
        library=AssetLibrary(self.root/'library',create=True);reference=register(library,source)
        config={'mode':'motion','clip':'hinge-sweep','times':['0','1/2'],'width':64,'height':64,'samples':1}
        result=create_preview(library,reference,self.root/'cache',config=config,executable=os.environ['HATCH_BLENDER'])
        self.assertNotEqual(result['images'][0]['sha256'],result['images'][1]['sha256'])
        from PIL import Image
        with Image.open(self.root/'cache'/result['key']/result['images'][0]['path']) as picture:
            first=np.asarray(picture).copy()
        with Image.open(self.root/'cache'/result['key']/result['images'][1]['path']) as picture:
            self.assertGreater(np.abs(first.astype(float)-np.asarray(picture)).mean(),.01)
        skeleton=create_preview(library,reference,self.root/'cache',config={'mode':'skeleton','width':64,'height':64,'samples':1},executable=os.environ['HATCH_BLENDER'])
        self.assertEqual(len(skeleton['images']),1)


if __name__=='__main__':unittest.main()
