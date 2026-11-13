"""Real immutable Pose binding and imported-component deformation tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np

import test_rig_scene3d_evaluate as evaluation_tests
from rig_test_helpers import fixture
from rig_skeleton import neutral_pose,rotate_bone
from rig_math import axis_angle
from rig_errors import RigError
from scene3d_contracts import canonical_bytes,all_refs,validate_document
from scene3d_example import node


class PoseBindingTests(unittest.TestCase):
    setUp=evaluation_tests.SceneEvaluationTests.setUp
    evaluate=evaluation_tests.SceneEvaluationTests.evaluate

    def file(self,path):
        return dict(path=path,sha256=hashlib.sha256((self.root/path).read_bytes()).hexdigest())

    def register_authored_files(self,root,paths):
        # Trust is established by the controlled fixture author, never by a
        # sampler discovering mutable files at evaluation time.
        for path in paths:
            payload=path.read_bytes()
            self.bundle['inventory'][path.relative_to(root).as_posix()]=dict(
                bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())

    def bind(self,rig,pose,path,rig_ref=None):
        (self.root/path).write_bytes(json.dumps(pose,sort_keys=True).encode()+b"\n")
        self.register_authored_files(self.root,[self.root/path])
        rig_ref=rig_ref or dict(kind='hatch-3d-rig',id=rig.manifest['id'],version=rig.manifest['version'],
                              **self.file('actor/rig.json'))
        return dict(schema_version=1,rig=rig_ref,pose=dict(id=pose['id'],rig_version=pose['rig_version'],file=self.file(path)))

    def actors(self,*,with_socket=False):
        rig=fixture(self.root,name='actor')
        if with_socket:
            from rig_jobs import save_rig
            rig.manifest['attachments']=[dict(schema_version=1,id='tool',bone='b1',
                transform=dict(translation=[.2,.3,0],rotation=[0,0,0,1],scale=[1,1,1]),
                role='test-tool',owner=rig.manifest['character']['id'])]
            save_rig(rig,operation='author-test-socket')
        asset=deepcopy(self.bundle['documents']['triangle.json'])
        asset.update(id='actor',asset_type='character',skeleton=dict(kind='hatch-3d-rig',
            id=rig.manifest['id'],version=rig.manifest['version'],**self.file('actor/rig.json')),
            animations=[dict(kind='hatch-3d-animation',id=e['id'],version=e['version'],
                             **self.file('actor/'+e['path'])) for e in rig.manifest['animations']])
        self.bundle['documents']['actor.json']=asset
        (self.root/'actor.json').write_bytes(canonical_bytes(asset))
        self.register_authored_files(self.root,[self.root/'actor.json',
            *(p for p in (self.root/'actor').rglob('*') if p.is_file())])
        ref=dict(kind='scene3d-asset',id='actor',version=asset['version'],path='actor.json',
                 sha256=hashlib.sha256(canonical_bytes(asset)).hexdigest())
        one=node('one','skinned_character','root');one['asset']=ref
        two=deepcopy(one);two['id']='two'
        self.scene['nodes'].extend([one,two])
        first=rotate_bone(rig.skeleton,neutral_pose(rig.skeleton,'bent'),'b1',axis_angle([0,0,1],.8))
        first['morphs']={rig.geometry.meshes[0].id:{'blink':.5}}
        one['pose_binding']=self.bind(rig,first,'bent-pose.json')
        two['pose_binding']=self.bind(rig,neutral_pose(rig.skeleton,'rest'),'rest-pose.json')
        return rig,asset,one,two

    def test_actual_two_instance_pose_and_mesh_qualified_state_without_rest_mutation(self):
        rig,asset,one,two=self.actors()
        before=deepcopy(self.bundle);manifest=(self.root/'actor/rig.json').read_bytes()
        out=self.evaluate(source_root=self.root,sample_geometry=True)
        a=np.vstack(list(out['native_geometry']['one'].values()));b=np.vstack(list(out['native_geometry']['two'].values()))
        self.assertGreater(float(np.linalg.norm(a-b)),.1)
        state=out['nodes']['one']['native_state']
        self.assertEqual(state['pose']['id'],'bent')
        self.assertEqual(state['pose']['rig_version'],rig.skeleton['version'])
        self.assertNotEqual(rig.manifest['version'],rig.skeleton['version'])
        self.assertIn('b1',state['bone_matrices'])
        self.assertIn('blink',state['morph_targets'][rig.geometry.meshes[0].id])
        self.assertEqual(state['socket_matrices'],{})
        self.assertEqual(self.bundle,before);self.assertEqual((self.root/'actor/rig.json').read_bytes(),manifest)
        self.assertTrue(out['nodes']['one']['native_pose_evaluated'])
        without_vertices=self.evaluate(source_root=self.root)
        self.assertNotIn('native_geometry',without_vertices)
        self.assertEqual(without_vertices['nodes']['one']['native_state'],state)
        self.assertIn(one['pose_binding']['pose']['file'],list(all_refs(validate_document(self.scene))))

    def test_exact_hash_pose_and_skeleton_versions_and_missing_morph_fail(self):
        rig,asset,one,two=self.actors()
        binding=one['pose_binding'];saved=deepcopy(binding)
        for update in [lambda:binding['pose']['file'].update(sha256='0'*64),
                       lambda:binding['pose'].update(id='wrong'),
                       lambda:binding['pose'].update(rig_version='9.9.9'),
                       lambda:binding['rig'].update(version='9.9.9')]:
            update()
            with self.assertRaises(RigError):self.evaluate(source_root=self.root,sample_geometry=True)
            binding.clear();binding.update(deepcopy(saved))
        pose=neutral_pose(rig.skeleton);pose['morphs']={'unknown':{'blink':1}}
        one['pose_binding']=self.bind(rig,pose,'bad-pose.json')
        with self.assertRaises(RigError):self.evaluate(source_root=self.root)

    def test_active_clip_overlap_rejected_and_geometry_budget_shared(self):
        rig,asset,one,two=self.actors()
        self.shot['bindings']=[dict(target='one',clip=asset['animations'][0],
            start_frame=0,end_frame=1,speed=1,loop=False)]
        with self.assertRaises(RigError):self.evaluate(source_root=self.root)
        self.evaluate(1,source_root=self.root)
        self.shot['bindings']=[]
        with self.assertRaises(RigError):self.evaluate(source_root=self.root,sample_geometry=True,max_geometry_vertices=1)

    def test_changed_native_payload_rejected_before_any_legacy_decoder(self):
        rig,asset,one,two=self.actors()
        geometry=self.root/'actor'/rig.manifest['geometry']['data']['path']
        geometry.write_bytes(geometry.read_bytes()+b'changed')
        with patch('rig_models.load_rig') as decoder:
            with self.assertRaises(RigError):self.evaluate(source_root=self.root,sample_geometry=True)
            decoder.assert_not_called()

    def test_authored_socket_follows_pose_and_explicit_instance_world_mapping(self):
        from rig_attachments import socket_world
        rig,asset,one,two=self.actors(with_socket=True)
        one['transform']['translation']=[7,2,3]
        one['transform']['rotation']=axis_angle([0,1,0],.4).tolist()
        before=deepcopy(self.bundle);source=(self.root/'actor/rig.json').read_bytes()
        out=self.evaluate(source_root=self.root)
        state=out['nodes']['one']['native_state'];other=out['nodes']['two']['native_state']
        self.assertEqual(set(state['socket_matrices']),{'tool'})
        self.assertEqual(state['socket_matrix_space'],'rig-bind')
        matrix=np.asarray(state['socket_matrices']['tool'])
        np.testing.assert_allclose(matrix,socket_world(rig,state['pose'],'tool'))
        self.assertFalse(np.allclose(matrix,other['socket_matrices']['tool']))
        np.testing.assert_allclose(np.asarray(state['world_from_rig'])@matrix,
            np.asarray(out['nodes']['one']['world_matrix'])@socket_world(rig,state['pose'],'tool'))
        self.assertEqual(self.bundle,before)
        self.assertEqual((self.root/'actor/rig.json').read_bytes(),source)

    def test_decoder_uses_private_capture_after_original_source_changes(self):
        from rig_models import load_rig
        rig,asset,one,two=self.actors()
        before=self.evaluate(source_root=self.root,sample_geometry=True)
        geometry=self.root/'actor'/rig.manifest['geometry']['data']['path']
        original=geometry.read_bytes()
        def decode(private_root):
            self.assertNotEqual(Path(private_root).resolve(),(self.root/'actor').resolve())
            geometry.write_bytes(b'mutated after complete admission')
            return load_rig(private_root)
        try:
            with patch('rig_models.load_rig',side_effect=decode):
                after=self.evaluate(source_root=self.root,sample_geometry=True)
            self.assertEqual(after['native_geometry'],before['native_geometry'])
            self.assertEqual(after['nodes']['one']['native_state'],before['nodes']['one']['native_state'])
        finally:
            geometry.write_bytes(original)

    def test_imported_root_pose_matches_canonical_importer_and_keeps_other_instance(self):
        from asset_library import AssetLibrary
        from rig_gltf import export_gltf
        from scene3d_graph import SceneGraph
        from scene3d_import import import_gltf,materialize,add_import,evaluate_imported_vertices
        from rig_models import load_rig
        original=fixture(self.root,name='original');source=self.root/'original.glb';export_gltf(original,source)
        license_path=self.root/'license.txt';license_path.write_text('CC0-1.0 self-authored fixture')
        library=AssetLibrary(self.root/'library',create=True)
        imported=import_gltf(source,library,asset_id='imported',attribution='self-authored fixture',license_file=license_path)
        materialize(library,imported.library_reference,self.root/'copied')
        copied=self.root/'copied';native=copied/Path(imported.asset['skeleton']['path']).parent
        rig=load_rig(native);pose=rotate_bone(rig.skeleton,neutral_pose(rig.skeleton,'imported-bend'),
            rig.skeleton['bones'][1]['id'],axis_angle([0,0,1],.8))
        (copied/'pose.json').write_bytes(json.dumps(pose,sort_keys=True).encode()+b"\n")
        for path in list(self.bundle['inventory']):
            destination=copied/path;destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes((self.root/path).read_bytes())
        self.register_authored_files(copied,[p for p in copied.rglob('*') if p.is_file()])
        binding=dict(schema_version=1,rig=imported.asset['skeleton'],pose=dict(id=pose['id'],rig_version=pose['rig_version'],
            file=dict(path='pose.json',sha256=hashlib.sha256((copied/'pose.json').read_bytes()).hexdigest())))
        graph=SceneGraph(self.scene)
        mapping=add_import(graph,imported,'actor')['mapping'];add_import(graph,imported,'other')
        scene=graph.document();root=next(n for n in scene['nodes'] if n['id']=='actor')
        root['pose_binding']=binding;root['transform']['translation']=[7,0,0]
        graph=SceneGraph(scene)
        self.bundle['documents']['example-scene.json']=scene
        self.bundle['documents'][imported.asset_reference['path']]=imported.asset
        out=self.evaluate(source_root=copied,sample_geometry=True)
        expected=evaluate_imported_vertices(graph,imported,native,pose=pose)['nodes']
        self.assertEqual(set(out['native_geometry']),set(mapping.values()) & set(expected))
        for identifier in out['native_geometry']:
            self.assertEqual(out['native_geometry'][identifier],expected[identifier])
            self.assertEqual(out['nodes'][identifier]['native_state']['pose']['id'],pose['id'])
        self.assertNotIn('native_pose',out['nodes']['other'])
        from scene3d_snapshot import snapshot_project
        from scene3d_contracts import load_project
        from scene3d_evaluate import evaluate_scene
        restored=self.root.parent/'restored'
        snapshot=snapshot_project(self.root,restored,graph,scene_path='example-scene.json',
            derived_documents={imported.asset_reference['path']:imported.asset},
            extra_payloads={p.relative_to(copied).as_posix():p.read_bytes() for p in copied.rglob('*')
                            if p.is_file() and p.relative_to(copied).as_posix()!=imported.asset_reference['path']})
        reopened=load_project(restored)
        active=snapshot['active_scene']
        shots=[path for path,d in reopened['documents'].items() if d['kind']=='scene3d-shot' and d['scene']['path']==active['path']]
        self.assertEqual(len(shots),1)
        after=evaluate_scene(restored,active['path'],shots[0],0,sample_geometry=True)
        self.assertEqual(after['native_geometry'],out['native_geometry'])


if __name__=='__main__':unittest.main()
