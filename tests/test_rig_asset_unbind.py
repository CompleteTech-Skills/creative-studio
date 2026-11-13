"""Canonical CAS unbind, retained pins and real Scene3D removal/undo."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import unittest

from asset_library import AssetLibrary
from asset_library_versions import ScopedResolver
from asset_library_reuse import ProjectStore
from rig_errors import RigError, object_hash
from scene3d_contracts import load_project
import test_asset_library_versions as version_fixture
import test_rig_asset_reuse as scene_fixture


class CanonicalUnbind(unittest.TestCase):
    setUp=version_fixture.VersionTests.setUp

    def test_exact_slot_removal_durable_history_and_restore(self):
        ref=self.versions.create(self.manifest,self.sources)
        resolver=ScopedResolver([('local',self.library)],project_library='local')
        bound=dict(library='local',reference=ref)
        resolver.bind('project','selected',bound,expected_revision=0)
        resolver.bind('project','unrelated',bound,expected_revision=1)
        before={name:self.library.read_file(ref,name) for name in self.sources}
        with self.assertRaises(RigError):resolver.unbind('project','selected',expected_revision=1)
        wrong=deepcopy(bound);wrong['reference']['sha256']='0'*64
        with self.assertRaises(RigError):resolver.unbind('project','selected',expected_revision=2,expected_binding=wrong)
        result=resolver.unbind('project','selected',expected_revision=2,expected_binding=bound)
        self.assertEqual(result['removed'],bound);self.assertEqual(set(result['locks']),{'unrelated'})
        reopened=ScopedResolver([('local',AssetLibrary(self.library.root))],project_library='local')
        self.assertEqual(reopened.bindings('project'),dict(revision=3,bindings={'unrelated':bound}))
        reopened.undo_unbind('project','selected',expected_revision=3,unbind_revision=3)
        self.assertEqual(reopened.bindings('project')['bindings'],{'selected':bound,'unrelated':bound})
        self.assertEqual(len(self.library.events(result['event']['stream'])),4)
        for name,data in before.items():self.assertEqual(self.library.read_file(ref,name),data)

    def test_concurrent_canonical_unbind_has_one_winner(self):
        ref=self.versions.create(self.manifest,self.sources)
        resolver=ScopedResolver([('local',self.library)],project_library='local')
        resolver.bind('project','slot',dict(library='local',reference=ref),expected_revision=0)
        barrier=threading.Barrier(2)
        def remove(_):
            own=ScopedResolver([('local',AssetLibrary(self.library.root))],project_library='local')
            barrier.wait(timeout=30)
            try:own.unbind('project','slot',expected_revision=1);return 'saved'
            except RigError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(remove,range(2)))
        self.assertCountEqual(results,['saved','conflict'])
        self.assertEqual(resolver.bindings('project'),dict(revision=2,bindings={}))


class SceneUnbind(unittest.TestCase):
    setUp=scene_fixture.Reuse.setUp
    register=scene_fixture.Reuse.register
    instance=scene_fixture.Reuse.instance

    def test_native_scene_removal_undo_preserves_other_scene_receipts_and_locks(self):
        receipt_path=self.root/'receipt.json';receipt_path.write_text('{"review":"rejected"}')
        receipt=self.register('receipt','reference',receipt_path)
        project=deepcopy(self.project);project['scenes']['scene-b']['receipts']=[receipt]
        project=self.instance(project,'scene-a');project=self.instance(project,'scene-b')
        unrelated=deepcopy(project['scenes']['scene-b']);old_assets=self.library.list_references()
        changed=self.adapter.unbind(project,'scene-a','robot',expected_revision=project['revision'])
        self.assertNotIn('scene-a/instance/robot',changed['locks'])
        self.assertTrue(any(key.startswith('history-0-before') for key in changed['locks']))
        self.assertEqual(changed['project']['scenes']['scene-b'],unrelated)
        self.adapter.load_scene(changed['project'],'scene-a',self.root/'removed')
        loaded=load_project(self.root/'removed')
        scene=loaded['documents'][loaded['project']['scenes'][0]['path']]
        self.assertEqual([n['id'] for n in scene['nodes']],['root'])
        moved=self.adapter.set_overrides(changed['project'],'scene-b','robot',transform=dict(translation=[5,0,0],rotation=[0,0,0,1],scale=[1,1,1]))
        restored=self.adapter.undo_unbind(moved,changed['receipt'],expected_revision=moved['revision'])
        self.assertEqual(restored['scenes']['scene-a'],project['scenes']['scene-a'])
        self.assertEqual(restored['scenes']['scene-b'],moved['scenes']['scene-b'])
        self.assertEqual(restored['scenes']['scene-b']['receipts'],[receipt])
        self.adapter.load_scene(restored,'scene-a',self.root/'restored')
        self.assertTrue((self.root/'restored'/'robot.glb').is_file())
        self.assertEqual(self.library.list_references(),old_assets)
        with self.assertRaises(RigError):self.adapter.undo_unbind(restored,changed['receipt'],expected_revision=restored['revision'])
        with self.assertRaises(RigError):self.adapter.unbind(project,'scene-a','robot',expected_revision=project['revision']-1)
        removed_again=self.adapter.unbind(restored,'scene-a','robot',expected_revision=restored['revision'])
        with self.assertRaises(RigError):self.adapter.undo_unbind(removed_again['project'],changed['receipt'],expected_revision=removed_again['project']['revision'])
        latest=self.adapter.undo_unbind(removed_again['project'],removed_again['receipt'],expected_revision=removed_again['project']['revision'])
        self.assertEqual(latest['scenes'],restored['scenes'])

    def test_optional_binding_exact_selection_and_active_animation(self):
        project=self.instance(self.project,'scene-a')
        with self.assertRaises(RigError):self.adapter.unbind(project,'scene-a','robot',role='character',expected_revision=project['revision'])
        with self.assertRaises(RigError):self.adapter.unbind(project,'scene-a','robot',role='animation',expected_revision=project['revision'])
        store=ProjectStore(self.root/'repair-store');saved=store.save(project,expected_sha256=None)
        with self.assertRaises(RigError):store.unbind(self.adapter,'scene-a','robot',role='animation',expected_revision=project['revision'],expected_sha256=saved['sha256'])
        repair=store.set_animation(self.adapter,'scene-a','robot',None,expected_revision=project['revision'],expected_sha256=saved['sha256'])
        saved_removal=store.unbind(self.adapter,'scene-a','robot',role='animation',expected_revision=repair['project']['revision'],expected_sha256=repair['saved']['sha256'])
        self.assertNotIn('animation',saved_removal['project']['scenes']['scene-a']['instances']['robot']['bindings'])
        clear=self.adapter.set_animation(project,'scene-a','robot',None)
        with self.assertRaises(RigError):self.adapter.unbind(clear,'scene-a','robot',role='material',expected_revision=clear['revision'],expected_binding=self.bindings['rig'])
        changed=self.adapter.unbind(clear,'scene-a','robot',role='animation',expected_revision=clear['revision'],expected_binding=self.bindings['animation'])
        self.assertNotIn('animation',changed['project']['scenes']['scene-a']['instances']['robot']['bindings'])
        restored=self.adapter.undo_unbind(changed['project'],changed['receipt'],expected_revision=changed['project']['revision'])
        self.assertEqual(restored['scenes'],clear['scenes'])

    def test_project_store_cas_concurrent_unbind_and_undo(self):
        project=self.instance(self.project,'scene-a');project=self.instance(project,'scene-b')
        store=ProjectStore(self.root/'store');saved=store.save(project,expected_sha256=None)
        barrier=threading.Barrier(2)
        def remove(scene):
            own=ProjectStore(store.root);barrier.wait(timeout=30)
            try:return own.unbind(self.adapter,scene,'robot',expected_revision=project['revision'],expected_sha256=saved['sha256'])
            except RigError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(remove,['scene-a','scene-b']))
        winners=[r for r in results if r is not None];self.assertEqual(len(winners),1)
        current=store.load();winner=winners[0]
        undone=store.undo_unbind(self.adapter,winner['receipt'],expected_revision=current['revision'],expected_sha256=object_hash(current))
        self.assertEqual(undone['project']['scenes'],project['scenes'])
        self.assertEqual(len(list((store.root/'revisions').glob('*.json'))),3)
        with self.assertRaises(RigError):store.unbind(self.adapter,'scene-a','robot',expected_revision=project['revision'],expected_sha256=saved['sha256'])


if __name__=='__main__':unittest.main()
