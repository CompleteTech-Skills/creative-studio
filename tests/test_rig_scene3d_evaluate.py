"""Real Scene3D time/property evaluation, separate from optional-free 2D tests."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

import numpy as np
from rig_errors import RigError
from scene3d_example import build_example, node
from scene3d_evaluate import evaluate_scene
from rig_math import axis_angle


class SceneEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'project'
        self.bundle = build_example(self.root)
        self.scene = self.bundle['documents']['example-scene.json']
        self.shot = self.bundle['documents']['example-shot.json']
        self.clip = self.bundle['documents']['example-animation.json']

    def evaluate(self, frame=0, **kwargs):
        return evaluate_scene(self.bundle, 'example-scene', 'example-shot', frame, **kwargs)

    def test_exact_rational_sampling_and_unchanged_sources(self):
        self.shot['fps'] = dict(numerator=30000, denominator=1001)
        self.shot['tracks'] = [dict(target='mesh', property='translation', interpolation='linear',
            keys=[dict(frame=0,value=[2,0,0]),dict(frame=24,value=[26,0,0])])]
        before = deepcopy(self.bundle)
        out = self.evaluate(Fraction(3,2))
        self.assertEqual(out['frame'], dict(numerator=3,denominator=2))
        self.assertEqual(out['time'], dict(numerator=1001,denominator=20000))
        self.assertEqual(out['nodes']['mesh']['world_matrix'][0][3],3.5)
        self.assertEqual(self.bundle,before)
        self.assertEqual(self.evaluate(1)['nodes']['mesh']['world_matrix'][0][3],3.)
        self.assertEqual(self.evaluate(0)['nodes']['mesh']['world_matrix'][0][3],2.)

    def test_half_open_interval_and_exact_input_types(self):
        for value in [-1,24,24.0,True,Fraction(49,2)]:
            with self.subTest(value=value),self.assertRaises(RigError):self.evaluate(value)
        self.assertEqual(self.evaluate(Fraction(479,20))['frame'],dict(numerator=479,denominator=20))

    def test_clip_rates_loop_and_shot_override(self):
        ref = self.bundle['project']['animations'][0]
        self.shot['fps'] = dict(numerator=12,denominator=1)
        self.shot['bindings'] = [dict(target='mesh',clip=ref,start_frame=0,end_frame=24,speed=1,loop=True)]
        self.assertAlmostEqual(self.evaluate(3)['nodes']['mesh']['world_matrix'][0][3],2.25)
        self.assertAlmostEqual(self.evaluate(12)['nodes']['mesh']['world_matrix'][0][3],2.)
        self.shot['tracks'] = [dict(target='mesh',property='translation',interpolation='step',
             keys=[dict(frame=0,value=[9,0,0])])]
        self.assertEqual(self.evaluate(3)['nodes']['mesh']['world_matrix'][0][3],9.)

    def test_independent_nodes_lights_world_camera(self):
        light = node('key','light','root')
        light['light'] = dict(type='point',color=[1,1,1],intensity=100)
        self.scene['nodes'].append(light);self.scene['lights'].append('key')
        self.shot['tracks'] = [dict(target='key',property='light_intensity',interpolation='linear',
             keys=[dict(frame=0,value=0),dict(frame=24,value=24)]),
             dict(target='world',property='world_exposure',interpolation='linear',
             keys=[dict(frame=0,value=-1),dict(frame=24,value=1)]),
             dict(target='camera',property='translation',interpolation='linear',
             keys=[dict(frame=0,value=[0,2,4]),dict(frame=24,value=[24,2,4])])]
        out=self.evaluate(12)
        self.assertEqual(out['nodes']['key']['light']['intensity'],12.)
        self.assertEqual(out['world']['exposure'],0.)
        self.assertEqual(out['nodes']['camera']['world_matrix'][0][3],12.)
        self.assertEqual(out['nodes']['mesh']['world_matrix'][0][3],2.)
        self.assertEqual(out['nodes']['mesh']['world_bounds']['minimum'],[2.,0.,0.])

    def test_slerp_camera_override_and_visibility_inheritance(self):
        self.shot['tracks']=[dict(target='root',property='rotation',interpolation='slerp',
             keys=[dict(frame=0,value=[0,0,0,1]),dict(frame=24,value=axis_angle([0,1,0],np.pi).tolist())]),
             dict(target='mesh',property='visible',interpolation='step',
             keys=[dict(frame=0,value=True),dict(frame=12,value=False)])]
        self.shot['camera_overrides']={'camera':dict(projection='orthographic',near=.1,far=100,vertical_fov=.8,orthographic_height=5)}
        out=self.evaluate(12)
        self.assertFalse(out['nodes']['mesh']['visible'])
        self.assertEqual(out['nodes']['camera']['camera']['projection'],'orthographic')
        self.assertAlmostEqual(out['nodes']['mesh']['world_matrix'][2][3],-2.)

    def test_two_real_native_characters_sample_independently(self):
        import hashlib
        from rig_test_helpers import fixture
        from rig_skinning import evaluate_deformation
        from scene3d_contracts import canonical_bytes
        rig = fixture(self.root, name='actor')
        asset = deepcopy(self.bundle['documents']['triangle.json'])
        def file(path):
            return dict(path=path,sha256=hashlib.sha256((self.root/path).read_bytes()).hexdigest())
        rig_ref=dict(kind='hatch-3d-rig',id=rig.manifest['id'],version=rig.manifest['version'],**file('actor/rig.json'))
        clips={entry['id']:dict(kind='hatch-3d-animation',id=entry['id'],version=entry['version'],
                 **file('actor/'+entry['path'])) for entry in rig.manifest['animations']}
        asset.update(id='actor-asset',asset_type='character',skeleton=rig_ref,animations=list(clips.values()))
        self.bundle['documents']['actor-asset.json']=asset
        asset_ref=dict(kind='scene3d-asset',id=asset['id'],version=asset['version'],path='actor-asset.json',
                       sha256=hashlib.sha256(canonical_bytes(asset)).hexdigest())
        for identifier,clip in [('first','bend'),('second','neutral')]:
            character=node(identifier,'skinned_character','root');character['asset']=asset_ref
            self.scene['nodes'].append(character)
            self.shot['bindings'].append(dict(target=identifier,clip=clips[clip],start_frame=0,end_frame=24,speed=1,loop=False))
        out=self.evaluate(12,source_root=self.root,sample_geometry=True)
        first=np.asarray(next(iter(out['native_geometry']['first'].values())))
        second=np.asarray(next(iter(out['native_geometry']['second'].values())))
        self.assertEqual(out['nodes']['first']['world_bounds'],
                         dict(minimum=first.min(axis=0).tolist(),maximum=first.max(axis=0).tolist()))
        owned=[state['own_world_bounds'] for ident,state in out['nodes'].items()
               if ident != 'root' and state['own_world_bounds'] is not None]
        self.assertEqual(out['nodes']['root']['world_bounds'],dict(
            minimum=np.min([box['minimum'] for box in owned],axis=0).tolist(),
            maximum=np.max([box['maximum'] for box in owned],axis=0).tolist()))
        self.assertGreater(np.max(np.abs(first-second)),.05)
        self.assertTrue(out['native_geometry_sampled'])
        self.assertTrue(out['nodes']['first']['native_animation_evaluated'])
        self.assertEqual(out['nodes']['first']['native_clip']['reference'],clips['bend'])
        # An arbitrary file with the same ID cannot select a different rig clip.
        original_reference = deepcopy(clips['bend'])
        clips['bend']['version'] = '2.0.0'
        with self.assertRaises(RigError):self.evaluate(12,source_root=self.root,sample_geometry=True)
        clips['bend'].update(original_reference)
        second_binding=self.shot['bindings'][1]
        saved_reference=second_binding['clip']
        bad_reference=deepcopy(clips['bend']);bad_reference['version']='9.9.9'
        asset['animations'].append(bad_reference)
        second_binding['clip']=bad_reference
        with self.assertRaises(RigError):self.evaluate(12,source_root=self.root,sample_geometry=True)
        second_binding['clip']=saved_reference
        asset['animations'].remove(bad_reference)
        # Exact current native payload, not a stale source descriptor, is sampled.
        path=self.root/clips['bend']['path'];path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(RigError):self.evaluate(12,source_root=self.root,sample_geometry=True)

    def test_actual_imported_subtree_loop_parent_transform_and_other_instance(self):
        from asset_library import AssetLibrary
        from rig_gltf import export_gltf
        from rig_test_helpers import fixture
        from scene3d_graph import SceneGraph
        from scene3d_import import import_gltf,materialize,add_import,evaluate_imported_vertices
        rig=fixture(self.root,name='original')
        source=self.root/'original.glb';export_gltf(rig,source)
        license_path=self.root/'license.txt';license_path.write_text('CC0-1.0 procedural test fixture')
        library=AssetLibrary(self.root/'library',create=True)
        imported=import_gltf(source,library,asset_id='imported',attribution='test fixture',license_file=license_path)
        copied=self.root/'copied';materialize(library,imported.library_reference,copied)
        graph=SceneGraph(self.scene,external_references={})
        first_mapping=add_import(graph,imported,'one')['mapping']
        second_mapping=add_import(graph,imported,'two')['mapping']
        transform=graph.transform('one');transform['translation']=[7,0,0];graph.set_transform('one',transform)
        self.bundle['documents']['example-scene.json']=graph.document()
        self.bundle['documents'][imported.asset_reference['path']]=imported.asset
        self.shot['fps']=dict(numerator=6,denominator=1)
        clips={r['id']:r for r in imported.asset['animations']}
        bend=clips[imported.report['native_clip_map']['bend']]
        neutral=clips[imported.report['native_clip_map']['neutral']]
        self.shot['bindings']=[dict(target=target,clip=ref,start_frame=0,end_frame=24,speed=1,loop=True)
                              for target,ref in [('one',bend),('two',neutral)]]
        out=self.evaluate(15,source_root=copied,sample_geometry=True)
        expected=evaluate_imported_vertices(graph,imported,copied/'imports/imported/1.0.0/native',
                                          clip=bend['id'],time=2.5,loop=True)['nodes']
        selected=[i for i in out['native_geometry'] if i in first_mapping.values()]
        self.assertTrue(selected)
        for identifier in selected:
            self.assertEqual(out['native_geometry'][identifier],expected[identifier])
        first=next(out['native_geometry'][i] for i in out['native_geometry'] if i in first_mapping.values())
        second=next(out['native_geometry'][i] for i in out['native_geometry'] if i in second_mapping.values())
        a=np.asarray(next(iter(first.values())));b=np.asarray(next(iter(second.values())))
        self.assertGreater(float(np.linalg.norm(a-b)),1.)
        exact_budget=sum(len(points) for meshes in out['native_geometry'].values() for points in meshes.values())
        self.assertEqual(self.evaluate(15,source_root=copied,sample_geometry=True,
                         max_geometry_vertices=exact_budget)['native_geometry'],out['native_geometry'])
        with self.assertRaises(RigError):self.evaluate(15,source_root=copied,sample_geometry=True,max_geometry_vertices=1)

    def test_budgets_and_invalid_channel_fail(self):
        self.shot['tracks']=[dict(target='mesh',property='light_intensity',interpolation='step',keys=[dict(frame=0,value=2)])]
        with self.assertRaises(RigError):self.evaluate()
        self.shot['tracks']=[dict(target='mesh',property='translation',interpolation='step',keys=[dict(frame=0,value=[0,0,0])]),
                             dict(target='camera',property='translation',interpolation='step',keys=[dict(frame=0,value=[0,0,0])])]
        with self.assertRaises(RigError):self.evaluate(max_track_samples=1)

    def test_active_constraints_evaluate_after_tracks_and_leave_authored_state(self):
        target=node('aim','locator','root');target['transform']['translation']=[2,0,-2]
        self.scene['nodes'].append(target)
        self.shot['temporal_constraints']=[dict(id='shot-aim',target='mesh',start_frame=1,end_frame=5,
            constraint=dict(id='look',type='look-at',target='aim',influence=1))]
        baseline=self.evaluate(0)['nodes']['mesh']['world_matrix']
        active=self.evaluate(1)['nodes']['mesh']['world_matrix']
        self.assertNotEqual(active,baseline)
        self.assertEqual(self.evaluate(5)['nodes']['mesh']['world_matrix'],baseline)
        self.assertEqual(self.scene['nodes'][1]['constraints'],[])

    def test_material_owner_resolves_exact_instance_pin_without_mutation(self):
        from lookdev_contracts import Registry, canonical_bytes
        from test_lookdev_contracts import material, common, policy
        from scene3d_evaluate import resolve_materials
        base=material()
        registry=Registry([('ceramic.json',canonical_bytes(base))])
        base_ref=registry.reference('ceramic','1.0.0')
        instance=common('material-instance3d','red-ceramic')
        instance.update(base_material=base_ref,dependencies=[base_ref],
            overrides={'base_color':[1,0,0,1]},render_policy=policy())
        registry=Registry([('ceramic.json',canonical_bytes(base)),('red.json',canonical_bytes(instance))])
        ref=registry.reference('red-ceramic','1.0.0');ref['kind']='material'
        out=self.evaluate()
        out['nodes']['mesh']['material_overrides']=[dict(slot='default',material=ref)]
        before=deepcopy(out)
        resolved=resolve_materials(out,registry)
        self.assertEqual(resolved['mesh']['default']['surface']['base_color'],[1,0,0,1])
        self.assertEqual(out,before)
        out['nodes']['mesh']['material_overrides'][0]['material']['sha256']='0'*64
        with self.assertRaises(RigError):resolve_materials(out,registry)

    def test_camera_owner_projects_both_native_models_and_rejects_inherited_scale(self):
        from scene3d_evaluate import camera_matrices
        out=self.evaluate()
        before=deepcopy(out)
        mapped=camera_matrices(out,dict(width=640,height=480))
        self.assertAlmostEqual(mapped['projection_matrix'][1][1],
                               1/np.tan(out['nodes']['camera']['camera']['vertical_fov']/2))
        self.assertEqual(out,before)
        out['nodes']['camera']['camera']['projection']='orthographic'
        out['nodes']['camera']['camera']['orthographic_height']=8
        mapped=camera_matrices(out,dict(width=640,height=480))
        self.assertAlmostEqual(mapped['projection_matrix'][1][1],.25)
        self.assertEqual(mapped['optics']['projection']['type'],'orthographic')
        self.scene['nodes'][0]['transform']['rotation']=axis_angle([1,2,3],.1).tolist()
        rotated=self.evaluate()
        mapped=camera_matrices(rotated,dict(width=640,height=480))
        np.testing.assert_allclose(mapped['world_matrix'],rotated['nodes']['camera']['world_matrix'])
        out['nodes']['camera']['world_matrix'][0][0]=2
        with self.assertRaises(ValueError):camera_matrices(out,dict(width=640,height=480))

    def test_scenes_share_asset_identity_but_not_instance_animation(self):
        other=deepcopy(self.scene);other['id']='other-scene'
        other['nodes'][1]['transform']['translation']=[10,0,0]
        other_shot=deepcopy(self.shot);other_shot['id']='other-shot'
        other_shot['scene']=dict(other_shot['scene'],id='other-scene',path='other-scene.json')
        other_shot['tracks']=[];other_shot['bindings']=[]
        self.bundle['documents'].update({'other-scene.json':other,'other-shot.json':other_shot})
        before=deepcopy(self.bundle)
        first=self.evaluate(12)
        second=evaluate_scene(self.bundle,'other-scene','other-shot',12)
        self.assertEqual(first['nodes']['mesh']['asset'],second['nodes']['mesh']['asset'])
        self.assertNotEqual(first['nodes']['mesh']['world_matrix'],second['nodes']['mesh']['world_matrix'])
        self.assertEqual(self.bundle,before)

    def test_persisted_path_is_exact_world_motion_after_parent_and_pivot(self):
        self.scene['nodes'][0]['transform']['translation']=[3,4,5]
        self.scene['nodes'][0]['transform']['rotation']=axis_angle([0,1,0],.3).tolist()
        mesh=self.scene['nodes'][1]
        mesh['transform']['pivot']=[.5,0,0]
        mesh['transform']['rotation']=axis_angle([0,1,0],.5).tolist()
        self.shot['path_bindings']=[dict(schema_version=1,id='travel',target='mesh',
            points=[[0,1,0],[0,1,4],[4,1,4]],start_frame=0,end_frame=20,
            start_fraction=0,end_fraction=1,space='world',orientation='preserve')]
        before=deepcopy(self.bundle)
        out=self.evaluate(10)
        np.testing.assert_allclose(np.asarray(out['nodes']['mesh']['world_matrix'])[:3,3],[0,1,4],atol=1e-12)
        np.testing.assert_allclose(np.asarray(self.evaluate(Fraction(5,2))['nodes']['mesh']['world_matrix'])[:3,3],
                                   [0,1,1],atol=1e-12)
        self.assertEqual(self.bundle,before)
        self.assertNotEqual(self.evaluate(20)['nodes']['mesh']['world_matrix'],out['nodes']['mesh']['world_matrix'])
        self.shot['path_bindings'][0]['points']=[[0,0,0],[0,0,0]]
        with self.assertRaises(RigError):self.evaluate()

    def test_canonical_light_binding_uses_explicit_units_and_animated_multiplier(self):
        from lookdev_contracts import Registry,canonical_bytes
        from test_lookdev_contracts import light
        from scene3d_evaluate import light_instances
        source=light();registry=Registry([('key.json',canonical_bytes(source))])
        binding=dict(schema_version=1,light=registry.reference(source['id'],source['version']),
                     intensity_scale=1,color_multiplier=[1,1,1])
        lamp=node('key','light','root');lamp['light']=dict(type='point',color=[1,1,1],intensity=999)
        lamp['light_binding']=binding
        self.scene['nodes'].append(lamp);self.scene['lights'].append('key')
        self.shot['tracks']=[dict(target='key',property='light_intensity_scale',interpolation='linear',
            keys=[dict(frame=0,value=1),dict(frame=24,value=3)])]
        out=self.evaluate(12);before=deepcopy(out)
        resolved=light_instances(out,registry)
        self.assertEqual(resolved['lights']['key']['intensity_scale'],2)
        self.assertEqual(registry.get(binding['light'])['intensity'],source['intensity'])
        self.assertEqual(out['nodes']['key']['light']['intensity'],999)
        self.assertEqual(out,before)
        out['nodes']['key']['light_binding']['light']['sha256']='0'*64
        with self.assertRaises(RigError):light_instances(out,registry)


if __name__=='__main__':unittest.main()
