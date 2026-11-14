"""Exact selective invalidation and process-independent native lifecycle tests."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from rig_errors import RigError
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_snapshot import snapshot_project
from scene3d_lifecycle import dependency_receipt, compare_dependencies, inspect_revision


class SceneLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source'
        self.bundle=build_example(self.source)

    def test_camera_edit_invalidates_scene_but_not_asset_geometry_review(self):
        asset_before=dependency_receipt(self.source,['triangle.json'],purpose='geometry-review')
        scene_before=dependency_receipt(self.source,['example-scene.json'],purpose='scene-preview')
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.source)
        transform=deepcopy(next(n for n in graph.document()['nodes'] if n['id']=='camera')['transform'])
        transform['translation'][0]=5
        graph.set_transform('camera',transform)
        saved=snapshot_project(self.source,self.root/'changed',graph,scene_path='example-scene.json')
        asset_after=dependency_receipt(self.root/'changed',['triangle.json'],purpose='geometry-review')
        scene_after=dependency_receipt(self.root/'changed',[saved['active_scene']['path']],purpose='scene-preview')
        self.assertFalse(compare_dependencies(asset_before,asset_after)['stale'])
        self.assertTrue(compare_dependencies(scene_before,scene_after)['stale'])
        self.assertNotIn('project.json',asset_before['files'])
        self.assertEqual(set(asset_before['files']),{'triangle.json','triangle.obj'})

    def test_unused_prefab_change_does_not_invalidate_shot(self):
        shot_before=dependency_receipt(self.source,['example-shot.json'],purpose='render')
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.source)
        prefab=deepcopy(self.bundle['documents']['example-prefab.json']);prefab['version']='1.0.1'
        prefab['name']='Unused changed prefab'
        snapshot_project(self.source,self.root/'changed',derived_documents={'unused-prefab.json':prefab})
        shot_after=dependency_receipt(self.root/'changed',['example-shot.json'],purpose='render')
        self.assertFalse(compare_dependencies(shot_before,shot_after)['stale'])
        self.assertNotIn('unused-prefab.json',shot_after['files'])

    def test_receipts_reject_tampering_raw_roots_and_nonfinite_labels(self):
        receipt=dependency_receipt(self.source,['triangle.json'],purpose='geometry-review')
        bad=deepcopy(receipt);bad['files']['triangle.obj']['sha256']='0'*64
        with self.assertRaises(RigError):compare_dependencies(receipt,bad)
        for targets,purpose in [(['triangle.obj'],'review'),(['../escape'],'review'),(['triangle.json']*2,'review'),(['triangle.json'],5)]:
            with self.subTest(targets=targets,purpose=purpose),self.assertRaises((RigError,ValueError)):
                dependency_receipt(self.source,targets,purpose=purpose)

    def test_derived_shots_are_cataloged_once_and_survive_edit_promotion(self):
        from scene3d_contracts import load_project
        extra=deepcopy(self.bundle['documents']['example-shot.json'])
        extra.update(id='alternate-shot',name='Alternate persisted shot')
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.source)
        scene=graph.document();scene['shots'].append('alternate-shot');graph.replace_document(scene,expected_revision=graph.revision)
        initial=snapshot_project(self.source,self.root/'extra',graph,scene_path='example-scene.json',shot_documents=[extra])
        bundle=load_project(self.root/'extra')
        self.assertEqual([r['id'] for r in bundle['project']['shots']],['example-shot','example-shot','alternate-shot'])
        graph=SceneGraph.from_bundle(bundle,initial['active_scene']['path'],source_root=self.root/'extra')
        snapshot_project(self.root/'extra',self.root/'promoted',graph,scene_path=initial['active_scene']['path'])
        promoted=load_project(self.root/'promoted')
        self.assertEqual(len(promoted['project']['shots']),5)
        self.assertEqual(len({r['path'] for r in promoted['project']['shots']}),5)
        self.assertEqual(len([d for d in promoted['documents'].values() if d['kind']=='scene3d-shot']),5)

    def test_derived_existing_scene_shot_version_does_not_enter_original_promotion(self):
        from scene3d_contracts import load_project
        extra=deepcopy(self.bundle['documents']['example-shot.json']);extra['version']='1.2.0'
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.source)
        snapshot_project(self.source,self.root/'extra-version',graph,scene_path='example-scene.json',
                         derived_documents={'new-shot.json':extra})
        bundle=load_project(self.root/'extra-version')
        self.assertEqual(bundle['documents']['new-shot.json']['scene']['path'],'example-scene.json')
        self.assertEqual(len(bundle['project']['shots']),3)
        self.assertEqual(len({r['path'] for r in bundle['project']['shots']}),3)

    def test_inspection_expands_saved_prefabs_without_mutating_revision(self):
        from editor3d_repository import EditorRepository
        from scene3d_prefabs import PrefabLibrary
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.source)
        library=PrefabLibrary.from_files(self.source,self.bundle)
        library.instantiate(graph,self.bundle['project']['prefabs'][0],'instance')
        result=snapshot_project(self.source,self.root/'prefab-source',graph,scene_path='example-scene.json')
        repository=EditorRepository.create(self.root/'prefab-source',self.root/'repository',scene_path=result['active_scene']['path'])
        before=repository.snapshot()
        report=inspect_revision(repository.root)
        self.assertGreater(len(report['nodes']),len(before['scene']['nodes']))
        self.assertEqual(before,repository.snapshot())
        self.assertEqual(report['evaluation'],'authored-base-pose')
        self.assertFalse(report['production_approved'])

    def test_repository_edit_restore_and_new_process_reopen(self):
        try:
            from editor3d_repository import EditorRepository
        except ImportError:
            self.skipTest('Editor3D repository integration dependency is unavailable.')
        repository=EditorRepository.create(self.source,self.root/'repository')
        before=inspect_revision(repository.root)
        request={'schema_version':1,'commands':[{'operation':'transform','parameters':{'identifier':'mesh',
            'transform':{'translation':[8,0,0],'rotation':[0,0,0,1],'scale':[1,1,1]}}}]}
        repository.apply(0,request,'move',expected_sha256=before['snapshot_sha256'])
        moved=inspect_revision(repository.root)
        self.assertEqual(moved['nodes']['mesh']['world_matrix'][0][3],8)
        repository.restore_revision(0,1,'restore',expected_sha256=moved['snapshot_sha256'])
        restored=inspect_revision(repository.root)
        self.assertEqual(before['nodes'],restored['nodes'])
        result=subprocess.run([sys.executable,'-c',
            'import json,sys;sys.path.insert(0,sys.argv[2]);from scene3d_lifecycle import inspect_revision;print(json.dumps(inspect_revision(sys.argv[1])))',
            str(repository.root),str(Path(__file__).resolve().parents[1]/'scripts')],check=True,capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout),restored)
        self.assertEqual((self.source/'triangle.obj').read_bytes(),repository.read_file('triangle.obj',restored['snapshot_sha256']))


if __name__=='__main__':unittest.main()
