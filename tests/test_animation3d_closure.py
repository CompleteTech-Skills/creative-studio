"""Actual owner admission regressions, not rendering/visual approval."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))

from animation3d_assets import load_project
from animation3d_test_helpers import samples, LAYER
from rig_errors import RigError
from rig_test_helpers import fixture


class NativeClosureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.values = samples(); self.project = self.values['project']
        self.project['budgets'].update(max_work_bytes=10000000,max_memory_bytes=10000000)

    def put(self, name, value, kind=None):
        path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value))
        return dict(path=name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    kind=kind or value['kind'],id=value['id'],version=value['version'])

    def check(self):
        self.put('animation-project.json',self.project)
        return load_project(self.root)

    def native_scene(self):
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples/scene3d-contracts',self.root/'native')
        native=json.loads((self.root/'native/project.json').read_text())
        ref=self.put('native/project.json',native,'scene')
        scene=self.values['scene'];scene['scene_asset']=ref
        return scene,ref

    def bound_clip(self):
        rig=fixture(self.root,animated=False)
        manifest=json.loads((rig.root/'rig.json').read_text())
        ref=self.put('fixture/rig.json',manifest,'rig')
        clip=self.values['clip'];clip['rig_binding']=ref
        clip['tracks'][0]['target']['bone_id']='b0'
        self.project['rig_assets']=[ref]
        self.project['clips']=[self.put('clip.json',clip,'clip')]
        return rig,clip,ref

    def test_project_memory_caps_native_texture_capacity_before_materialization(self):
        import io
        from unittest.mock import patch
        from PIL import Image
        from rig_models import load_rig, TextureImage, save_geometry
        rig, clip, _ = self.bound_clip()
        stream = io.BytesIO()
        Image.new('RGB', (1024,1024), 'red').save(stream, format='PNG')
        rig.geometry.images = [TextureImage('texture', 'image/png', stream.getvalue())]
        rig.manifest['geometry'] = save_geometry(rig.root, rig.geometry, rig.manifest['limits'])
        reference = self.put('fixture/rig.json', rig.manifest, 'rig')
        clip['rig_binding'] = reference
        self.project['rig_assets'] = [reference]
        self.project['clips'] = [self.put('clip.json', clip, 'clip')]
        before = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(RigError) as native_rejection:
            load_rig(rig.root, limits={'memory_bytes': self.project['budgets']['max_memory_bytes']})
        self.assertEqual(native_rejection.exception.report['code'], 'RIG_LIMIT_MEMORY')
        with patch('rig_models.load_geometry', side_effect=AssertionError('native materialization before project admission')):
            with self.assertRaises(RigError) as rejected:
                self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        self.project['budgets']['max_memory_bytes'] = 64*1024*1024
        result = self.check()
        self.assertTrue(result['source_ready'])
        self.assertLessEqual(result['memory_admission']['estimated_bytes'], result['memory_admission']['limit_bytes'])
        self.assertGreater(result['memory_admission']['native_estimated_bytes'], 10_000_000)
        for name, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root/name).read_bytes()).hexdigest(), digest)

    def test_project_memory_aggregates_distinct_retained_native_rigs(self):
        from unittest.mock import patch
        from rig_models import load_rig
        first, _, first_reference = self.bound_clip()
        second = fixture(self.root, name='second', animated=False)
        second_reference = self.put('second/rig.json', second.manifest, 'rig')
        self.project['rig_assets'] = [first_reference, second_reference]
        first_cost = load_rig(first.root).resource_usage['estimated_memory_bytes']
        second_cost = load_rig(second.root).resource_usage['estimated_memory_bytes']
        self.project['budgets']['max_memory_bytes'] = first_cost + second_cost - 1
        with self.assertRaises(RigError) as rejected:
            self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        self.project['budgets']['max_memory_bytes'] = 4*(first_cost + second_cost)
        with patch('rig_models.load_rig', wraps=load_rig) as native_loads:
            result = self.check()
        self.assertTrue(result['source_ready'])
        self.assertEqual(native_loads.call_count, 2)
        caps = [call.kwargs['limits']['memory_bytes'] for call in native_loads.call_args_list]
        self.assertGreater(caps[0], caps[1])
        self.assertGreaterEqual(result['memory_admission']['native_estimated_bytes'], first_cost + second_cost)
        self.assertLessEqual(result['memory_admission']['estimated_bytes'], result['memory_admission']['limit_bytes'])

    def test_json_object_capacity_is_admitted_before_dependency_decode(self):
        from animation3d_assets import Snapshot
        source = self.root/'large.json'
        source.write_text(json.dumps(['small']*100))
        destination = self.root/'snapshot'
        snapshot = Snapshot(self.root, destination)
        snapshot.memory_limit = 1000
        self.assertLess(source.stat().st_size, snapshot.memory_limit)
        with self.assertRaises(RigError) as rejected:
            snapshot.copy('large.json')
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')

    def test_unbound_or_unknown_skeletal_target_rejects(self):
        clip=self.values['clip'];self.project['clips']=[self.put('clip.json',clip,'clip')]
        with self.assertRaisesRegex(RigError,'rig binding'):self.check()
        _,clip,_=self.bound_clip()
        self.assertTrue(self.check()['source_ready'])
        clip['tracks'][0]['target']['bone_id']='absent'
        self.project['clips']=[self.put('clip.json',clip,'clip')]
        with self.assertRaisesRegex(RigError,'Bone is absent'):self.check()

    def test_project_native_bone_vertex_and_morph_budgets(self):
        self.bound_clip()
        for key in ('max_bones_per_rig','max_vertices'):
            previous=self.project['budgets'][key];self.project['budgets'][key]=1
            with self.subTest(key=key),self.assertRaisesRegex(RigError,'budget'):self.check()
            self.project['budgets'][key]=previous
        self.assertTrue(self.check()['source_ready'])

    def test_project_memory_and_native_node_budgets(self):
        scene,_=self.native_scene();self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        self.assertTrue(self.check()['source_ready'])
        self.project['budgets']['max_scene_nodes']=1
        with self.assertRaisesRegex(RigError,'node budget'):self.check()
        self.project['budgets']['max_scene_nodes']=100
        self.project['budgets']['max_vertices']=1
        with self.assertRaisesRegex(RigError,'vertex budget'):self.check()
        self.project['budgets']['max_vertices']=100
        self.project['budgets']['max_memory_bytes']=1
        with self.assertRaisesRegex(RigError,'memory'):self.check()

    def test_contextual_rig_and_shot_placement_bindings(self):
        _,clip,rigref=self.bound_clip();scene,nativeref=self.native_scene()
        layer=deepcopy(LAYER)
        actor=dict(actor_id='actor',model={**nativeref,'kind':'model'},rig=rigref,
                   initial_transform=dict(position=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1]),
                   animation_layers=[layer],constraints=[])
        scene['actors']=[actor]
        self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        sequence=self.values['sequence']
        value=dict(id='placement',clip=self.project['clips'][0],actor_id='actor',layer_id='body',
                   start_frame=0,duration_frames=24,source_in_frame=0,source_out_frame=24,
                   playback_rate=dict(numerator=1,denominator=1),blend_in_frames=0,blend_out_frames=0,weight=1,enabled=True)
        sequence['shots'][0]['clip_placements']=[value]
        def refresh():self.project['sequences']=[self.put('sequence.json',sequence,'sequence')]
        refresh();self.assertTrue(self.check()['source_ready'])
        for key,bad in (('source_in_frame',1000),('source_out_frame',1000),('actor_id','absent'),('layer_id','absent')):
            previous=value[key];value[key]=bad;refresh()
            with self.subTest(key=key),self.assertRaises(RigError):self.check()
            value[key]=previous
        refresh()
        # Clip binding is optional only when a real placement supplies actor.rig.
        del clip['rig_binding'];self.project['clips']=[self.put('clip.json',clip,'clip')]
        value['clip']=self.project['clips'][0];refresh()
        self.assertTrue(self.check()['source_ready'])

    def test_scene_track_rejects_missing_native_node(self):
        scene,_=self.native_scene();track=deepcopy(self.values['clip']['tracks'][0])
        track['target']=dict(kind='node',node_id='root',property='translation');scene['object_tracks']=[track]
        self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        self.assertTrue(self.check()['source_ready'])
        track['target']['node_id']='absent'
        self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        with self.assertRaisesRegex(RigError,'absent or ambiguous'):self.check()

    def test_native_wrong_json_type_rejects_with_typed_error(self):
        path=self.root/'bad.json';path.write_text('[]')
        self.project['rig_assets']=[dict(path='bad.json',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                       kind='rig',id='bad',version='1.0.0')]
        with self.assertRaises(RigError):self.check()

    def test_binary_expansion_and_false_array_header_reject_before_load(self):
        import io
        import zipfile
        import numpy as np
        from animation3d_assets import Snapshot
        for forged_header in (False,True):
            stream=io.BytesIO()
            if forged_header:
                np.lib.format.write_array_header_1_0(stream,dict(descr='<f8',fortran_order=False,shape=(2000000,)))
                payload=stream.getvalue()
            else:payload=b'0'*10000
            with zipfile.ZipFile(self.root/'payload.npz','w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('data.npy',payload)
            for name in ('payload.npz','renamed.bin'):
                if name != 'payload.npz': shutil.copyfile(self.root/'payload.npz',self.root/name)
                with tempfile.TemporaryDirectory() as target:
                    snapshot=Snapshot(self.root,target);snapshot.memory_limit=1000
                    with self.subTest(forged_header=forged_header,name=name),self.assertRaises(RigError):snapshot.copy(name)

    def test_supported_foreign_properties_and_typed_corrupt_native_error(self):
        from animation3d_owners import resolve_property
        base=Path(__file__).resolve().parents[1]
        shutil.copytree(base/'examples/camera3d',self.root/'camera')
        camera=json.loads((self.root/'camera/registry.json').read_text())
        camera_ref=self.put('camera/registry.json',camera)
        material=json.loads((base/'examples/lookdev-contracts/ceramic.json').read_text())
        material['surface']['emission']=dict(color=[1,0,0],strength_watts_per_square_meter=2)
        material_ref=self.put('material.json',material)
        light=json.loads((base/'examples/lookdev-contracts/key.json').read_text())
        light_ref=self.put('light.json',light)
        for owner,source,domain,identifier,properties in (
            ('camera3d',camera_ref,'camera','camera',('translation','rotation','focal_length','focus_distance','aperture')),
            ('lookdev',material_ref,'material',material['id'],('base_color','opacity','metallic','roughness','emissive_color','emissive_intensity')),
            ('lookdev',light_ref,'light',light['id'],('translation','rotation','color','intensity'))):
            for prop in properties:
                value=dict(kind=domain,property=prop,**{domain+'_id':identifier})
                with self.subTest(domain=domain,prop=prop):
                    self.assertIsNotNone(resolve_property(self.root,dict(owner=owner,source=source,target=value)))
        (self.root/'camera/optics.json').write_text('{}')
        with self.assertRaises(RigError):
            resolve_property(self.root,dict(owner='camera3d',source=camera_ref,
                target=dict(kind='camera',camera_id='camera',property='focal_length')))

    def test_aggregate_morph_budget_and_missing_native_skeleton(self):
        _,clip,_=self.bound_clip()
        other=fixture(self.root,name='other',animated=False)
        manifest=json.loads((other.root/'rig.json').read_text())
        self.project['rig_assets'].append(self.put('other/rig.json',manifest,'rig'))
        self.project['budgets']['max_morph_targets']=1
        with self.assertRaisesRegex(RigError,'morph budget'):self.check()
        self.project['budgets']['max_morph_targets']=100
        self.assertTrue(self.check()['source_ready'])
        manifest['skeleton']=None
        self.project['rig_assets'][-1]=self.put('other/rig.json',manifest,'rig')
        with self.assertRaises(RigError):self.check()

    def test_scene_visibility_and_unsupported_camera_properties(self):
        from animation3d_owners import resolve_property
        scene,ref=self.native_scene()
        ref['kind']='scene3d-project'
        value=resolve_property(self.root,dict(owner='scene3d',source=ref,
             target=dict(kind='node',node_id='root',property='visibility')))
        self.assertIs(type(value),bool)
        base=Path(__file__).resolve().parents[1]
        shutil.copytree(base/'examples/camera3d',self.root/'camera')
        ref=self.put('camera/registry.json',json.loads((self.root/'camera/registry.json').read_text()))
        for prop in ('exposure','field_of_view','orthographic_size'):
            with self.subTest(prop=prop),self.assertRaises(RigError):
                resolve_property(self.root,dict(owner='camera3d',source=ref,
                    target=dict(kind='camera',camera_id='camera',property=prop)))

    def test_native_identity_aliases_reject(self):
        rig,_,_=self.bound_clip()
        shutil.copytree(rig.root,self.root/'alias')
        manifest=json.loads((self.root/'alias/rig.json').read_text())
        self.project['rig_assets'].append(self.put('alias/rig.json',manifest,'rig'))
        # A list can contain duplicate native identities only via another typed
        # reference path; the project schema rejects duplicate IDs in one list.
        self.project['rig_assets'].pop()
        self.project['character_assets']=[self.put('alias/rig.json',manifest,'character')]
        with self.assertRaisesRegex(RigError,'Duplicate native'):self.check()
