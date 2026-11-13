"""Real pinned library payloads, independent scene scopes and selective undo."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from asset_library import AssetLibrary,file_record
from asset_library_contracts import seal
from asset_library_dependencies import DependencyGraph,locator
from asset_library_reuse import AssetBinding,ProjectStore,SceneLibraryAdapter,new_project,validate_project
from rig_demo import create_demo
from rig_errors import RigError,object_hash
from rig_gltf import export_gltf
from rig_import import load_character_model
from rig_models import load_rig
from scene3d_contracts import load_project
from scene3d_example import node

ROOT=Path(__file__).resolve().parents[1]


class Reuse(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='reuse149-tests-');self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.library=AssetLibrary(self.root/'library',create=True);self.adapter=SceneLibraryAdapter(DependencyGraph({'local':self.library}))
        create_demo(self.root/'robot','robotic',fps=4);self.rig=load_rig(self.root/'robot');self.source=self.root/'robot.glb';export_gltf(self.rig,self.source)
        self.license=self.root/'license.txt';self.license.write_text('Procedural engineering fixtures; CC0-1.0.\n')
        self.bindings={role:self.register(role,kind,self.source) for role,kind in [('character','character'),('rig','rig'),('animation','animation'),('material','material')]}
        self.project=self.adapter.add_scene(new_project('example'),'scene-a')
        self.project=self.adapter.add_scene(self.project,'scene-b')

    def register(self,id,kind,path,version='1.0.0'):
        manifest=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
        target='runtime/asset.glb' if path.suffix=='.glb' else 'native/source'+path.suffix
        files=[file_record(path,target,'runtime'),file_record(self.license,'source/license.txt','license')]
        manifest.update(version=version,files=files);manifest['identity'].update(id=id,slug=id,kind=kind)
        manifest['provenance']['sources']=[files[0]];manifest['provenance']['license_evidence']=[files[1]]
        manifest['publication']['inventory']=[item['path'] for item in files]
        reference=self.library.register(seal(manifest),{target:path,'source/license.txt':self.license})
        return dict(location=locator('local',reference),path=target)

    def instance(self,project,scene,**kwargs):
        return self.adapter.instantiate(project,scene,'robot',bindings=self.bindings,
            animation_state=dict(clip='hinge-sweep',time='0'),**kwargs)

    def test_shared_exact_sources_independent_transforms_materials_and_clips(self):
        a=dict(translation=[-2,0,0],rotation=[0,0,0,1],scale=[1,1,1])
        b=dict(translation=[3,0,0],rotation=[0,0,0,1],scale=[1,1,1])
        project=self.instance(self.project,'scene-a',transform=a,material_overrides={'0':dict(base_color=[1,0,0,1],metallic=.2,roughness=.3)})
        project=self.instance(project,'scene-b',transform=b)
        project=self.adapter.set_animation(project,'scene-b','robot',dict(clip='hinge-sweep',time='1/2'))
        before=self.library.read_own_file(self.bindings['character']['location']['reference'],'runtime/asset.glb')
        snapshots={}
        for scene in ('scene-a','scene-b'):
            report=self.adapter.load_scene(project,scene,self.root/scene)
            self.assertEqual(report['animation'],'evaluated-single-sample')
            loaded=load_project(self.root/scene)
            doc=loaded['documents'][loaded['project']['scenes'][0]['path']]
            snapshots[scene]=load_character_model(self.root/scene/'robot.glb').geometry
            self.assertEqual(next(n for n in doc['nodes'] if n['id']=='robot')['transform'],a if scene=='scene-a' else b)
        self.assertEqual(project['scenes']['scene-a']['instances']['robot']['bindings'],project['scenes']['scene-b']['instances']['robot']['bindings'])
        points_a=np.concatenate([m.positions for m in snapshots['scene-a'].meshes]);points_b=np.concatenate([m.positions for m in snapshots['scene-b'].meshes])
        self.assertGreater(np.abs(points_a-points_b).max(),.01)
        self.assertEqual(snapshots['scene-a'].materials[0]['pbrMetallicRoughness']['baseColorFactor'],[1,0,0,1])
        self.assertNotEqual(snapshots['scene-b'].materials[0]['pbrMetallicRoughness']['baseColorFactor'],[1,0,0,1])
        self.assertEqual(self.library.read_own_file(self.bindings['character']['location']['reference'],'runtime/asset.glb'),before)

    def test_replacement_undo_impact_preserves_unrelated_scene_and_receipts(self):
        receipt_path=self.root/'review.json';receipt_path.write_text('{"verdict":"rejected","fixture":true}')
        receipt=self.register('receipt','reference',receipt_path)
        project=deepcopy(self.project);project['scenes']['scene-b']['receipts']=[receipt]
        project=self.instance(project,'scene-a');project=self.instance(project,'scene-b')
        unrelated=deepcopy(project['scenes']['scene-b']);old=deepcopy(self.bindings['character'])
        replacement=self.register('character','character',self.source,'1.0.1')
        self.assertEqual(len(self.adapter.impact(project,old)['affected']),2)
        changed=self.adapter.replace(project,'scene-a','robot','character',replacement)
        self.assertEqual(changed['project']['scenes']['scene-b'],unrelated)
        self.assertEqual(changed['invalidated'],[dict(scene='scene-a',instance='robot')])
        locks=self.adapter.dependencies(changed['project'])
        self.assertIn('history-0-before',locks)
        self.assertEqual(changed['project']['scenes']['scene-a']['instances']['robot']['bindings']['character'],replacement)
        transform=dict(translation=[8,1,0],rotation=[0,0,0,1],scale=[1,1,1])
        moved=self.adapter.set_overrides(changed['project'],'scene-a','robot',transform=transform)
        restored=self.adapter.undo(moved,changed['receipt'])
        self.assertEqual(restored['scenes']['scene-a']['instances']['robot']['bindings']['character'],old)
        self.assertEqual(restored['scenes']['scene-a']['instances']['robot']['transform'],transform)
        self.assertEqual(restored['scenes']['scene-b'],unrelated)
        self.assertEqual(len(restored['history']),2)
        with self.assertRaises(RigError):self.adapter.undo(restored,changed['receipt'])

    def test_incompatible_or_missing_replacement_preserves_input(self):
        project=self.instance(self.project,'scene-a');before=deepcopy(project)
        changed=deepcopy(self.rig);changed.geometry.meshes[0].positions[:,0]*=2
        source=self.root/'wrong.glb';export_gltf(changed,source)
        replacement=self.register('character','character',source,'1.0.1')
        with self.assertRaises(RigError):self.adapter.replace(project,'scene-a','robot','character',replacement)
        self.assertEqual(project,before)
        wrong_animation=self.register('animation','animation',source,'1.0.1')
        mismatched=deepcopy(self.bindings);mismatched['animation']=wrong_animation
        with self.assertRaises(RigError):self.adapter.instantiate(project,'scene-b','unsampled',bindings=mismatched)
        broken=deepcopy(self.bindings);broken['rig']['location']['reference']['sha256']='0'*64
        with self.assertRaises(RigError):self.adapter.instantiate(project,'scene-b','broken',bindings=broken)

    def test_camera_light_native_nodes_and_renderable_shot(self):
        project=self.instance(self.project,'scene-a')
        for kind in ('camera','light'):
            value=node(kind,kind,'root')
            if kind=='camera':value['camera']=dict(projection='perspective',near=.01,far=100,vertical_fov=.8,orthographic_height=4)
            else:value['light']=dict(type='point',color=[1,1,1],intensity=500)
            path=self.root/(kind+'.json');path.write_text(json.dumps(value))
            item=self.register(kind,kind,path)
            project=self.adapter.instantiate(project,'scene-a',kind,bindings={'asset':item},transform=dict(translation=[0,2,5],rotation=[0,0,0,1],scale=[1,1,1]))
        report=self.adapter.load_scene(project,'scene-a',self.root/'loaded')
        loaded=load_project(self.root/'loaded');self.assertEqual(len(loaded['project']['shots']),1)
        scene=loaded['documents'][loaded['project']['scenes'][0]['path']]
        self.assertEqual(scene['cameras'],['camera']);self.assertEqual(scene['lights'],['light'])
        self.assertEqual(report['shot'],loaded['project']['shots'][0]['id'])

    def test_legacy_profile_styles_states_and_receipts_keep_exact_2d_identity(self):
        profile_path=ROOT/'profiles/codex-v1.json'
        profile=self.register('profile','legacy-pet',profile_path)
        adapted=self.adapter.legacy(profile)
        self.assertEqual(adapted['payload'],profile_path.read_bytes());self.assertEqual(adapted['dimensionality'],'2d');self.assertIsNone(adapted['geometry'])
        from style_registry import resolve_style
        style_path=self.root/'style.json';style_path.write_text(json.dumps(resolve_style('pixel-art')))
        style=self.register('style','reference',style_path)
        self.assertEqual(self.adapter.legacy(style,legacy_type='style-snapshot')['payload'],style_path.read_bytes())
        receipt_path=self.root/'receipt.json';receipt_path.write_bytes(b'{"review":"rejected"}')
        item=self.register('receipt','reference',receipt_path)
        self.assertEqual(self.adapter.legacy(item,legacy_type='review-receipt')['payload'],receipt_path.read_bytes())
        with self.assertRaises(RigError):self.adapter.instantiate(self.project,'scene-a','flat',bindings={'asset':profile})

    def test_document_limits_native_failures_and_no_publication(self):
        with self.assertRaises(RigError):self.adapter.instantiate(self.project,'scene-a','root',bindings=self.bindings)
        with self.assertRaises(RigError):self.adapter.instantiate(self.project,'scene-a','bad',bindings=self.bindings,animation_state=dict(clip='missing',time='0'))
        with self.assertRaises(RigError):self.adapter.instantiate(self.project,'scene-a','bad',bindings=self.bindings,animation_state=dict(clip='hinge-sweep',time='99999999999999999999999999999999'))
        project=self.instance(self.project,'scene-a')
        with self.assertRaises(RigError):self.adapter.set_overrides(project,'scene-a','robot',material_overrides={'999':dict(base_color=[1,1,1,1],metallic=0,roughness=.5)})
        destination=self.root/'existing';destination.mkdir();(destination/'keep').write_text('unrelated')
        with self.assertRaises(RigError):self.adapter.load_scene(project,'scene-a',destination)
        self.assertEqual((destination/'keep').read_text(),'unrelated')

    def test_cas_store_retains_revisions_rejects_stale_and_links(self):
        project=self.instance(self.project,'scene-a');store=ProjectStore(self.root/'project-store')
        receipt=store.save(project,expected_sha256=None);self.assertEqual(store.load(),project)
        modified=self.adapter.set_animation(project,'scene-a','robot',dict(clip='hinge-sweep',time='1/2'))
        with self.assertRaises(RigError):store.save(modified,expected_sha256='0'*64)
        store.save(modified,expected_sha256=receipt['sha256'])
        self.assertEqual(len(list((store.root/'revisions').glob('*.json'))),2)
        self.assertEqual(store.load(),modified)
        link=self.root/'linked';link.symlink_to(store.root,target_is_directory=True)
        with self.assertRaises(RigError):ProjectStore(link)

    def test_receipt_scope_cannot_shadow_an_instance(self):
        receipt_path=self.root/'scope-receipt.json';receipt_path.write_text('{"review":"rejected"}')
        receipt=self.register('receipt','reference',receipt_path)
        project=deepcopy(self.project);project['scenes']['scene-a']['receipts']=[receipt]
        project=self.adapter.instantiate(project,'scene-a','receipt-0',bindings=self.bindings)
        scopes=self.adapter.dependencies(project)
        self.assertIn('scene-a/instance/receipt-0',scopes)
        self.assertIn('scene-a/receipt/0',scopes)
        self.assertEqual(len(scopes),2)
        self.assertEqual(len(scopes['scene-a/instance/receipt-0']['graph']['roots']),4)

    def test_binding_dataclass_and_unknown_fields_are_explicit(self):
        bindings={key:AssetBinding(**value) for key,value in self.bindings.items()}
        project=self.adapter.instantiate(self.project,'scene-a','robot',bindings=bindings)
        self.assertEqual(project['scenes']['scene-a']['instances']['robot']['bindings'],self.bindings)
        malformed=deepcopy(project);malformed['unknown']=True
        with self.assertRaises(RigError):validate_project(malformed)


if __name__=='__main__':unittest.main()
