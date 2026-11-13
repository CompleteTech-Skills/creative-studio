"""Real imported-rig Pose publication and selected-shot deformation acceptance."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import numpy as np

from asset_library import AssetLibrary
from editor3d_pose_inspector import apply_pose_edit,describe_pose
from editor3d_repository import EditorRepository
from editor3d_timeline import evaluate_timeline
from rig_errors import RigError
from rig_gltf import export_gltf
from rig_math import axis_angle
from rig_skeleton import rotate_bone
from rig_test_helpers import fixture
from scene3d_contracts import load_project
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_import import add_import,import_gltf,evaluate_imported_vertices
from scene3d_import_capture import resolve_captured_import
from scene3d_snapshot import snapshot_project


class PoseInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        source_rig=fixture(self.root,name='source-rig');source=self.root/'character.glb';export_gltf(source_rig,source)
        library=AssetLibrary(self.root/'library',create=True)
        license=self.root/'license.txt';license.write_text('CC0-1.0 self-authored engineering fixture')
        self.imported=import_gltf(source,library,asset_id='character',attribution='Self-authored engineering fixture',license_file=license)
        original=build_example(self.root/'source');graph=SceneGraph.from_bundle(original,'example-scene.json')
        self.actor_mapping=add_import(graph,self.imported,'actor')['mapping']
        self.other_mapping=add_import(graph,self.imported,'other')['mapping']
        manifest=library.resolve(self.imported.library_reference)
        payloads={item['path']:library.read_file(self.imported.library_reference,item['path']) for item in manifest['files'] if item['path']!=self.imported.asset_reference['path']}
        saved=snapshot_project(self.root/'source',self.root/'portable',graph,scene_path='example-scene.json',
            derived_documents={self.imported.asset_reference['path']:self.imported.asset},extra_payloads=payloads)
        self.repository=EditorRepository.create(self.root/'portable',self.root/'repository',scene_path=saved['active_scene']['path'])

    def timeline(self,snapshot):
        refs=[ref for ref in snapshot['project']['shots'] if snapshot['documents'][ref['path']]['scene']['path']==snapshot['scene_path']]
        self.assertEqual(len(refs),1)
        return dict(shot_path=refs[0]['path'],frame=dict(numerator=0,denominator=1))

    def request(self,operation,transaction_id,**values):
        snapshot=self.repository.snapshot()
        return dict(node_id='actor',operation=operation,expected_revision=snapshot['revision'],expected_sha256=snapshot['sha256'],
            transaction_id=transaction_id,timeline=self.timeline(snapshot),**values)

    def evaluation(self,snapshot):
        selected=self.timeline(snapshot)
        return evaluate_timeline(self.repository,dict(revision=snapshot['revision'],sha256=snapshot['sha256'],
            shot=selected['shot_path'],frame=selected['frame'],sample_geometry=True))

    def test_bone_pose_persists_reopens_and_evaluates_real_geometry_without_rest_mutation(self):
        before=self.repository.snapshot();descriptor=describe_pose(self.repository,before,'actor')
        self.assertTrue(descriptor['editable']);self.assertNotIn(str(self.root),str(descriptor))
        pin=descriptor['rig_reference'];original=self.repository.read_file(pin['path'],before['sha256'])
        native=self.repository.native_snapshot(before['revision'],before['sha256'])
        with resolve_captured_import({key:before[key] for key in ('project','documents','inventory')},self.imported.asset_reference,native['root']) as captured:
            graph=SceneGraph.from_bundle(captured['bundle'],before['scene_path'],source_root=captured['root'])
            baseline=evaluate_imported_vertices(graph,captured['imported'],captured['native_root'])['nodes']
        bone=descriptor['skeleton']['bones'][1]['id']
        authored=rotate_bone(descriptor['skeleton'],descriptor['authored_pose'],bone,axis_angle([0,0,1],.8))
        request=self.request('set','pose-bone',bones={bone:authored['bones'][bone]})
        after=apply_pose_edit(self.repository,request)
        self.assertEqual(apply_pose_edit(self.repository,request)['sha256'],after['sha256'])
        evaluation=self.evaluation(after)
        posed=evaluation['nodes']['actor']['native_pose_components'];self.assertTrue(posed)
        self.assertLessEqual(set(posed),set(self.actor_mapping.values()))
        self.assertFalse(set(posed)&set(self.other_mapping.values()))
        difference=max(float(np.linalg.norm(np.asarray(evaluation['native_geometry'][identifier][mesh])-np.asarray(baseline[identifier][mesh])))
            for identifier in posed for mesh in evaluation['native_geometry'][identifier])
        self.assertGreater(difference,.01)
        self.assertEqual(self.repository.read_file(pin['path'],after['sha256']),original)
        reopened=EditorRepository(self.root/'repository')
        self.assertEqual(describe_pose(reopened,reopened.snapshot(),'actor')['authored_pose']['bones'][bone],authored['bones'][bone])
        undone=self.repository.undo(after['revision'],'undo-pose',expected_sha256=after['sha256'])
        self.assertNotIn('pose_binding',next(n for n in undone['scene']['nodes'] if n['id']=='actor'))
        redone=self.repository.redo(undone['revision'],'redo-pose',expected_sha256=undone['sha256'])
        self.assertIn('pose_binding',next(n for n in redone['scene']['nodes'] if n['id']=='actor'))

    def test_mesh_qualified_morph_and_reset_use_real_native_pose(self):
        descriptor=describe_pose(self.repository,self.repository.snapshot(),'actor')
        self.assertTrue(descriptor['morph_targets'])
        mesh,names=next(iter(descriptor['morph_targets'].items()));self.assertTrue(names)
        after=apply_pose_edit(self.repository,self.request('set','pose-morph',morphs={mesh:{names[0]:.5}}))
        pose=describe_pose(self.repository,after,'actor')['authored_pose']
        self.assertEqual(pose['morphs'][mesh][names[0]],.5)
        self.assertTrue(self.evaluation(after)['native_geometry'])
        reset=apply_pose_edit(self.repository,self.request('reset','pose-reset'))
        self.assertEqual(describe_pose(self.repository,reset,'actor')['authored_pose']['morphs'],{})

    def test_invalid_bones_morphs_quaternions_mirror_approval_and_stale_requests_fail_atomically(self):
        before=self.repository.snapshot()
        invalid=[self.request('set','bad-bone',bones={'missing':{'translation':[1,0,0]}}),
                 self.request('set','bad-morph',morphs={'missing':{'unknown':.5}}),
                 self.request('set','bad-quaternion',bones={'b1':{'rotation':[0,0,0,0]}}),
                 self.request('mirror','unapproved-mirror',symmetry_approved=False)]
        for request in invalid:
            with self.subTest(operation=request['transaction_id']),self.assertRaises(RigError):apply_pose_edit(self.repository,request)
            self.assertEqual(self.repository.snapshot()['sha256'],before['sha256'])
        stale=self.request('reset','stale-reset');stale['expected_revision']=99
        with self.assertRaises(RigError):apply_pose_edit(self.repository,stale)
        self.assertEqual(self.repository.snapshot()['sha256'],before['sha256'])

    def test_pose_requires_selected_shot_and_rejects_native_clip_overlap_before_commit(self):
        request=self.request('reset','missing-shot');request['timeline']['shot_path']='missing.json'
        before=self.repository.snapshot()
        with self.assertRaises(RigError):apply_pose_edit(self.repository,request)
        self.assertEqual(self.repository.snapshot()['sha256'],before['sha256'])
        clip=self.imported.asset['animations'][0]
        # A real native clip on this imported subtree has no declared pose blend.
        bindings=[dict(target='actor',clip=clip,start_frame=0,end_frame=1,speed=1,loop=False)]
        # Use the graph's canonical binding field rather than a fabricated clip.
        native=self.repository.native_snapshot(before['revision'],before['sha256'])
        bundle=load_project(native['root']);graph=SceneGraph.from_bundle(bundle,before['scene_path'],source_root=native['root'])
        scene=graph.document();next(n for n in scene['nodes'] if n['id']=='actor')['animation_bindings']=bindings
        graph=SceneGraph(scene)
        saved=snapshot_project(native['root'],self.root/'with-clip',graph,scene_path=before['scene_path'])
        self.repository=EditorRepository.create(self.root/'with-clip',self.root/'clip-repository',scene_path=saved['active_scene']['path'])
        unchanged=self.repository.snapshot()
        with self.assertRaises(RigError):apply_pose_edit(self.repository,self.request('reset','overlap-reset'))
        self.assertEqual(self.repository.snapshot()['sha256'],unchanged['sha256'])


if __name__=='__main__':unittest.main()
