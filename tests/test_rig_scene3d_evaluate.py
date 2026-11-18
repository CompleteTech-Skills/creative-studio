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

    def register_authored_files(self,root,paths):
        import hashlib
        for path in paths:
            payload=path.read_bytes()
            self.bundle['inventory'][path.relative_to(root).as_posix()]=dict(
                bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())

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
        self.assertEqual(out['nodes']['mesh']['channels']['translation'],[3.5,0,0])

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

    def test_two_real_native_characters_sample_independently(self):
        import hashlib
        from rig_test_helpers import fixture
        from rig_skinning import evaluate_deformation
        from scene3d_contracts import canonical_bytes
        rig = fixture(self.root, name='actor')
        self.register_authored_files(self.root,[p for p in (self.root/'actor').rglob('*') if p.is_file()])
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
        from unittest.mock import patch
        from rig_models import load_rig
        with patch('rig_models.load_rig',wraps=load_rig) as loader:
            out=self.evaluate(12,source_root=self.root,sample_geometry=True)
        self.assertEqual(loader.call_count,1)
        from scene3d_evaluate import frame_camera
        cheap=self.evaluate(12)
        for target in ('first','root'):
            with self.assertRaises(RigError):frame_camera(cheap,dict(width=640,height=480),[target])
        from rig_skinning import evaluate_deformation
        with patch('scene3d_evaluate.MAX_NATIVE_CACHE_VERTICES',1), \
                patch('rig_skinning.evaluate_deformation',wraps=evaluate_deformation) as deformer:
            with self.assertRaises(RigError):
                self.evaluate(12,source_root=self.root,sample_geometry=True)
            self.assertEqual(deformer.call_count,0)
        source_vertices=sum(len(mesh.positions) for mesh in rig.geometry.meshes)
        with patch('scene3d_evaluate.MAX_NATIVE_CACHE_VERTICES',source_vertices), \
                patch('rig_skinning.evaluate_deformation',wraps=evaluate_deformation) as deformer:
            with self.assertRaises(RigError):
                self.evaluate(12,source_root=self.root,sample_geometry=True)
            self.assertEqual(deformer.call_count,1)
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
        for path in list(self.bundle['inventory']):
            target=copied/path;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((self.root/path).read_bytes())
        self.register_authored_files(copied,[p for p in copied.rglob('*') if p.is_file()])
        graph=SceneGraph(self.scene,external_references={})
        first_mapping=add_import(graph,imported,'one')['mapping']
        second_mapping=add_import(graph,imported,'two')['mapping']
        extras=['copy-'+str(index) for index in range(6)]
        for identifier in extras:add_import(graph,imported,identifier)
        transform=graph.transform('one');transform['translation']=[7,0,0];graph.set_transform('one',transform)
        self.bundle['documents']['example-scene.json']=graph.document()
        self.bundle['documents'][imported.asset_reference['path']]=imported.asset
        self.shot['fps']=dict(numerator=6,denominator=1)
        clips={r['id']:r for r in imported.asset['animations']}
        bend=clips[imported.report['native_clip_map']['bend']]
        neutral=clips[imported.report['native_clip_map']['neutral']]
        self.shot['bindings']=[dict(target=target,clip=ref,start_frame=0,end_frame=24,speed=1,loop=True)
                              for target,ref in [('one',bend),('two',neutral),*[(i,bend) for i in extras]]]
        from unittest.mock import patch
        from rig_models import load_rig
        from rig_skinning import evaluate_deformation
        evaluate_base = SceneGraph.evaluate_base
        with patch('rig_models.load_rig', wraps=load_rig) as loader, \
                patch('rig_skinning.evaluate_deformation', wraps=evaluate_deformation) as deformer, \
                patch.object(SceneGraph, 'evaluate_base', autospec=True, side_effect=evaluate_base) as graph_evaluator:
            out = self.evaluate(15, source_root=copied, sample_geometry=True)
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(deformer.call_count, 2)
        self.assertEqual(graph_evaluator.call_count, 1)
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

        nested = dict(self.shot['bindings'][0], target=selected[0])
        self.shot['bindings'].append(nested)
        repeated = self.evaluate(15, source_root=copied, sample_geometry=True)
        self.assertEqual(repeated['native_geometry'], out['native_geometry'])
        self.assertEqual(repeated['nodes'][selected[0]]['native_clip'],
                         out['nodes'][selected[0]]['native_clip'])
        nested['speed'] = 2
        with self.assertRaisesRegex(RigError, 'overlap'):
            self.evaluate(15, source_root=copied, sample_geometry=True)
        self.shot['bindings'].pop()
        with patch('scene3d_evaluate.MAX_NATIVE_MATCH_VISITS', 1), \
                self.assertRaisesRegex(RigError, 'traversal budget'):
            self.evaluate(15)


    def test_directory_native_replay_reopens_exact_fanout(self):
        from hashlib import sha256
        from unittest.mock import patch
        from rig_models import load_rig
        from rig_test_helpers import fixture
        from scene3d_capture import read_source_bytes
        from scene3d_contracts import canonical_bytes

        rig = fixture(self.root, name='actor')

        def pin(path, kind, identifier, version='1.0.0'):
            return dict(kind=kind, id=identifier, version=version, path=path,
                        sha256=sha256((self.root/path).read_bytes()).hexdigest())

        def persist(path, document):
            (self.root/path).write_bytes(canonical_bytes(document))
            return pin(path, document['kind'], document['id'], document['version'])

        rig_ref = pin('actor/rig.json', 'hatch-3d-rig', rig.manifest['id'], rig.manifest['version'])
        clip_refs = [pin('actor/'+item['path'], 'hatch-3d-animation', item['id'], item['version'])
                     for item in rig.manifest['animations']]
        bend = next(ref for ref in clip_refs if ref['id'] == 'bend')
        native_files = [pin(path.relative_to(self.root).as_posix(), 'evidence', 'native-'+str(index))
                        for index, path in enumerate(sorted((self.root/'actor').rglob('*')))
                        if path.is_file() and not path.name.startswith('.')]
        asset = deepcopy(self.bundle['documents']['triangle.json'])
        asset.update(id='native-actor', asset_type='character', skeleton=rig_ref,
                     animations=clip_refs, dependencies=native_files)
        asset_ref = persist('native-actor.json', asset)
        for index in range(3):
            identifier = 'actor-'+str(index)
            actor = node(identifier, 'skinned_character', 'root')
            actor['asset'] = asset_ref
            actor['transform']['translation'][0] = index
            self.scene['nodes'].append(actor)
            self.shot['bindings'].append(dict(target=identifier, clip=bend,
                start_frame=0, end_frame=24, speed=1, loop=False))
        self.scene['dependencies'].append(asset_ref)
        scene_ref = persist('example-scene.json', self.scene)
        self.shot['scene'] = scene_ref
        shot_ref = persist('example-shot.json', self.shot)
        render = self.bundle['documents']['example-render.json']
        render['shot'] = shot_ref
        render_ref = persist('example-render.json', render)
        project = self.bundle['project']
        project.update(scenes=[scene_ref], shots=[shot_ref], characters=[asset_ref])
        project['assets'].append(asset_ref)
        changed = {ref['path']: ref for ref in (scene_ref, shot_ref, render_ref)}
        project['dependency_lock'] = (
            [changed.get(ref['path'], ref) for ref in project['dependency_lock']] +
            [asset_ref, rig_ref] + clip_refs + native_files)
        project['budgets'].update(max_files=256, max_bytes=64*1024*1024)
        persist('project.json', project)
        before = {path: sha256(path.read_bytes()).hexdigest()
                  for path in self.root.rglob('*') if path.is_file()}

        first_frame = Fraction(3, 2)
        with patch('scene3d_capture.read_source_bytes', wraps=read_source_bytes) as reader, \
                patch('rig_models.load_rig', wraps=load_rig) as loader:
            first = evaluate_scene(self.root, scene_ref['id'], shot_ref['id'], first_frame,
                                   sample_geometry=True)
        reads = [call.args[0] for call in reader.call_args_list]
        self.assertEqual(len(reads), len(set(reads)))
        self.assertEqual(reads.count(self.root/rig_ref['path']), 1)
        self.assertEqual(reads.count(self.root/bend['path']), 1)
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(loader.call_args.kwargs['expected_manifest_sha256'], rig_ref['sha256'])
        self.assertFalse(loader.call_args.args[0].exists())
        later = evaluate_scene(self.root, scene_ref['path'], shot_ref['path'], 12, sample_geometry=True)
        replay = evaluate_scene(self.root, scene_ref['id'], shot_ref['id'], first_frame, sample_geometry=True)
        cheap = evaluate_scene(self.root, scene_ref['id'], shot_ref['id'], first_frame)
        self.assertEqual(first, replay)
        self.assertNotEqual(first['native_geometry'], later['native_geometry'])
        self.assertEqual(first['native_geometry_status'], 'sampled')
        self.assertEqual(set(first['native_geometry']), {'actor-0', 'actor-1', 'actor-2'})
        self.assertEqual(cheap['native_geometry_status'], 'not_requested')
        self.assertFalse(cheap['nodes']['actor-0']['native_animation_evaluated'])
        self.assertFalse(first['render_evaluated'])
        self.assertEqual(before, {path: sha256(path.read_bytes()).hexdigest()
                                  for path in self.root.rglob('*') if path.is_file()})

    def test_budgets_and_invalid_channel_fail(self):
        self.shot['tracks']=[dict(target='mesh',property='light_intensity',interpolation='step',keys=[dict(frame=0,value=2)])]
        with self.assertRaises(RigError):self.evaluate()
        self.shot['tracks']=[dict(target='mesh',property='translation',interpolation='step',keys=[dict(frame=0,value=[0,0,0])]),
                             dict(target='camera',property='translation',interpolation='step',keys=[dict(frame=0,value=[0,0,0])])]
        with self.assertRaises(RigError):self.evaluate(max_track_samples=1)

    def test_rejects_unsupported_existing_schema_extensions(self):
        for document, field, value in (
            (self.shot, 'path_bindings', []),
            (self.shot, 'light_overrides', []),
            (self.scene['nodes'][0], 'pose_binding', {}),
            (self.scene['world'], 'light_binding', {}),
        ):
            with self.subTest(field=field):
                document[field] = value
                try:
                    with self.assertRaises(RigError):
                        self.evaluate()
                finally:
                    del document[field]

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
