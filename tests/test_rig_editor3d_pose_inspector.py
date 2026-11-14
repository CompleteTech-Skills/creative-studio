"""Real imported-rig Pose publication and selected-shot deformation acceptance."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import os
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

    def test_canonical_collection_and_inherited_layer_locks_disable_authoring(self):
        snapshot=self.repository.snapshot()
        commands=[dict(operation='collection',parameters=dict(identifier='locked-actors',name='Locked actors')),
                  dict(operation='memberships',parameters=dict(identifier='actor',collections=['locked-actors'],layers=[])),
                  dict(operation='collection',parameters=dict(identifier='locked-actors',locked=True))]
        saved=self.repository.apply(snapshot['revision'],dict(schema_version=1,commands=commands),'lock-actors',expected_sha256=snapshot['sha256'])
        child=next(n['id'] for n in saved['scene']['nodes'] if n['parent']=='actor' and n.get('asset'))
        for identifier in ['actor',child]:
            descriptor=describe_pose(self.repository,saved,identifier)
            self.assertFalse(descriptor['editable']);self.assertIn('locked',descriptor['read_only_reason'])
            request=self.request('reset','locked-'+identifier);request['node_id']=identifier
            with self.assertRaises(RigError):apply_pose_edit(self.repository,request)
            self.assertEqual(self.repository.snapshot()['sha256'],saved['sha256'])
        commands=[dict(operation='collection',parameters=dict(identifier='locked-actors',locked=False)),
                  dict(operation='collection',parameters=dict(identifier='locked-layer',name='Locked layer',layer=True)),
                  dict(operation='memberships',parameters=dict(identifier='actor',collections=[],layers=['locked-layer'])),
                  dict(operation='collection',parameters=dict(identifier='locked-layer',layer=True,locked=True))]
        layered=self.repository.apply(saved['revision'],dict(schema_version=1,commands=commands),'lock-layer',expected_sha256=saved['sha256'])
        self.assertFalse(describe_pose(self.repository,layered,child)['editable'])
        request=self.request('reset','locked-child-layer');request['node_id']=child
        with self.assertRaises(RigError):apply_pose_edit(self.repository,request)
        self.assertEqual(self.repository.snapshot()['sha256'],layered['sha256'])

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

    def test_future_native_clip_rejects_static_pose_without_damaging_later_frames(self):
        before=self.repository.snapshot();native=self.repository.native_snapshot(before['revision'],before['sha256'])
        bundle=load_project(native['root']);graph=SceneGraph.from_bundle(bundle,before['scene_path'],source_root=native['root'])
        scene=graph.document();next(node for node in scene['nodes'] if node['id']=='actor')['animation_bindings']=[dict(
            target='actor',clip=self.imported.asset['animations'][0],start_frame=1,end_frame=2,speed=1,loop=False)]
        saved=snapshot_project(native['root'],self.root/'future-clip',SceneGraph(scene),scene_path=before['scene_path'])
        self.repository=EditorRepository.create(self.root/'future-clip',self.root/'future-repository',scene_path=saved['active_scene']['path'])
        before=self.repository.snapshot();selected=self.timeline(before)
        request=dict(revision=before['revision'],sha256=before['sha256'],shot=selected['shot_path'],frame=1,sample_geometry=True)
        prior=evaluate_timeline(self.repository,request)
        self.assertTrue(prior['native_geometry'])
        descriptor=describe_pose(self.repository,before,'actor')
        self.assertFalse(descriptor['editable']);self.assertIn('future frames',descriptor['read_only_reason'])
        with self.assertRaises(RigError):
            apply_pose_edit(self.repository,self.request('set','future-pose',bones={'b1':{'translation':[.2,.1,0]}}))
        self.assertEqual(self.repository.snapshot()['sha256'],before['sha256'])
        self.assertEqual(evaluate_timeline(self.repository,request)['native_geometry'],prior['native_geometry'])

    def test_other_character_native_clip_does_not_disable_independent_pose(self):
        before=self.repository.snapshot();native=self.repository.native_snapshot(before['revision'],before['sha256'])
        bundle=load_project(native['root']);graph=SceneGraph.from_bundle(bundle,before['scene_path'],source_root=native['root'])
        scene=graph.document();next(node for node in scene['nodes'] if node['id']=='other')['animation_bindings']=[dict(
            target='other',clip=self.imported.asset['animations'][0],start_frame=1,end_frame=2,speed=1,loop=False)]
        saved=snapshot_project(native['root'],self.root/'other-clip',SceneGraph(scene),scene_path=before['scene_path'])
        self.repository=EditorRepository.create(self.root/'other-clip',self.root/'other-repository',scene_path=saved['active_scene']['path'])
        before=self.repository.snapshot();self.assertTrue(describe_pose(self.repository,before,'actor')['editable'])
        after=apply_pose_edit(self.repository,self.request('set','independent-pose',bones={'b1':{'translation':[.2,.1,0]}}))
        selected=self.timeline(after)
        sampled=evaluate_timeline(self.repository,dict(revision=after['revision'],sha256=after['sha256'],shot=selected['shot_path'],frame=1,sample_geometry=True))
        self.assertTrue(sampled['native_geometry_sampled'])
        self.assertEqual(describe_pose(self.repository,after,'actor')['authored_pose']['bones']['b1']['translation'],[.2,.1,0])

    def test_other_active_scene_shot_native_clip_blocks_global_static_pose(self):
        before=self.repository.snapshot();native=self.repository.native_snapshot(before['revision'],before['sha256'])
        bundle=load_project(native['root']);graph=SceneGraph.from_bundle(bundle,before['scene_path'],source_root=native['root'])
        source=next(ref for ref in before['project']['shots'] if before['documents'][ref['path']]['scene']['path']==before['scene_path'])
        second=deepcopy(before['documents'][source['path']]);second.update(id='native-second',version='1.0.0',name='Native second')
        second['bindings']=[dict(target='actor',clip=self.imported.asset['animations'][0],start_frame=1,end_frame=2,speed=1,loop=False)]
        scene=graph.document();scene['shots'].append(second['id'])
        saved=snapshot_project(native['root'],self.root/'another-shot',SceneGraph(scene),scene_path=before['scene_path'],shot_documents=[second])
        self.repository=EditorRepository.create(self.root/'another-shot',self.root/'another-shot-repository',scene_path=saved['active_scene']['path'])
        before=self.repository.snapshot()
        refs={ref['id']:ref for ref in before['project']['shots'] if before['documents'][ref['path']]['scene']['path']==before['scene_path']}
        evaluation=dict(revision=before['revision'],sha256=before['sha256'],shot=refs['native-second']['path'],frame=1,sample_geometry=True)
        prior=evaluate_timeline(self.repository,evaluation)
        self.assertTrue(prior['native_geometry'])
        self.assertFalse(describe_pose(self.repository,before,'actor')['editable'])
        request=dict(node_id='actor',operation='reset',expected_revision=before['revision'],expected_sha256=before['sha256'],
            transaction_id='another-shot-pose',timeline=dict(shot_path=refs[source['id']]['path'],frame=dict(numerator=0,denominator=1)))
        with self.assertRaises(RigError):apply_pose_edit(self.repository,request)
        self.assertEqual(self.repository.snapshot()['sha256'],before['sha256'])
        self.assertEqual(evaluate_timeline(self.repository,evaluation)['native_geometry'],prior['native_geometry'])

    def test_standard_server_pose_routes_authenticate_and_publish_exact_native_pose(self):
        import json
        import threading
        import urllib.error
        import urllib.request
        from editor3d_viewer import EditorServer
        server=EditorServer(self.repository)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        def post(route,body,authenticated=True):
            headers={'Content-Type':'application/json'}
            headers['Authorization']='Bearer '+(server.token if authenticated else 'invalid-local-pose-token')
            request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/'+route,
                data=json.dumps(body).encode(),headers=headers,method='POST')
            try:
                with urllib.request.urlopen(request,timeout=30) as response:
                    return response.status,json.load(response)
            except urllib.error.HTTPError as error:
                return error.code,json.load(error)
        before=self.repository.snapshot()
        request=self.request('set','http-pose',bones={'b1':{'translation':[.1,.2,.3]}})
        self.assertEqual(post('inspectors/pose/edit',request,False)[0],401)
        self.assertEqual(post('inspectors/pose/describe',{'node_id':'actor','path':'/etc/passwd'})[0],400)
        self.assertEqual(self.repository.snapshot()['sha256'],before['sha256'])
        status,description=post('inspectors/pose/describe',{'node_id':'actor'})
        self.assertEqual(status,200,description)
        self.assertEqual(description['owner_id'],'actor')
        status,result=post('inspectors/pose/edit',request)
        self.assertEqual(status,200,result)
        after=self.repository.snapshot()
        self.assertEqual(after['revision'],before['revision']+1)
        self.assertEqual(result['sha256'],after['sha256'])
        self.assertEqual(post('inspectors/pose/edit',request)[1]['sha256'],after['sha256'])
        reopened=EditorRepository(self.repository.root)
        pose=describe_pose(reopened,reopened.snapshot(),'actor')['authored_pose']
        self.assertEqual(pose['bones']['b1']['translation'],[.1,.2,.3])
        self.assertTrue(self.evaluation(after)['native_geometry_sampled'])

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST')=='1','Enable actual Chromium pose acceptance explicitly.')
    def test_actual_browser_authors_and_displays_exact_native_pose_geometry(self):
        import json
        from urllib.parse import urlsplit
        import threading
        from editor3d_viewer import EditorServer
        from browser_runtime import launch_chromium
        from playwright.sync_api import sync_playwright
        server=EditorServer(self.repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(thread.join);self.addCleanup(server.shutdown)
        with sync_playwright() as runtime:
            browser=launch_chromium(runtime)
            try:
                page=browser.new_page(viewport={'width':1400,'height':1100})
                page_errors=[];failed_requests=[]
                page.on('pageerror',lambda error:page_errors.append(str(error)))
                page.on('requestfailed',lambda request:failed_requests.append(dict(path=urlsplit(request.url).path,error=request.failure)))
                page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                page.evaluate("window.hatchEditor.select('actor')")
                section=page.get_by_role('region',name='Native pose inspector')
                # A named section maps to a region; use its explicit DOM identity
                # so browser accessibility differences cannot hide test coverage.
                section=page.locator('section[aria-label="Native pose inspector"]')
                section.get_by_role('button',name='Save selected bone pose',exact=True).wait_for()
                section.get_by_label('Pose bone',exact=True).select_option('b1')
                section.get_by_label('Bone translation 0',exact=True).fill('.35')
                before=self.repository.snapshot()
                section.get_by_role('button',name='Save selected bone pose',exact=True).click()
                page.wait_for_function('() => window.hatchEditor.snapshot.revision === '+str(before['revision']+1))
                page.get_by_text('Native pose saved, reloaded and evaluated.',exact=True).wait_for()
                saved=self.repository.snapshot();evaluated=self.evaluation(saved)
                def world_vertices():
                    return page.evaluate('''() => {
                      const result={};
                      for (const [id,object] of window.hatchEditor.objects) {
                        object.updateWorldMatrix(true,true);
                        object.traverse(child=>{
                          if(!child.isMesh || child.userData.nativeMeshId===undefined)return;
                          const values=child.geometry.attributes.position,points=[];
                          for(let i=0;i<values.count;i++) {
                            const m=child.matrixWorld.elements,x=values.getX(i),y=values.getY(i),z=values.getZ(i);
                            points.push([m[0]*x+m[4]*y+m[8]*z+m[12],m[1]*x+m[5]*y+m[9]*z+m[13],m[2]*x+m[6]*y+m[10]*z+m[14]]);
                          }
                          (result[id]??={})[child.userData.nativeMeshId]=points;
                        });
                      }
                      return result;
                    }''')
                observed=world_vertices()
                for identifier,meshes in evaluated['native_geometry'].items():
                    for mesh,points in meshes.items():np.testing.assert_allclose(observed[identifier][mesh],points,atol=1e-6,rtol=1e-6)
                damaged=deepcopy(evaluated)
                first=next(iter(damaged['native_geometry']));mesh=next(iter(damaged['native_geometry'][first]))
                damaged['native_geometry'][first][mesh].pop()
                self.assertIn('topology',page.evaluate('(value)=>{try{window.hatchEditor.preview(value);return "accepted";}catch(error){return error.message;}}',damaged))
                self.assertEqual(observed,world_vertices())
                page.evaluate('window.hatchEditor.clearPreview()')
                native_nodes={node['id']:node for node in saved['scene']['nodes']}
                def belongs_to_actor(node):
                    identifier=node['id']
                    while identifier is not None:
                        if identifier=='actor':return True
                        identifier=native_nodes[identifier]['parent']
                    return False
                posed=[node['id'] for node in saved['scene']['nodes'] if node.get('metadata',{}).get('source_component') is not None and belongs_to_actor(node)]
                self.assertTrue(posed)
                for identifier in posed:self.assertFalse(page.evaluate('(id)=>window.hatchEditor.objects.get(id).visible',identifier))
                for control in ['show-all','isolate']:
                    page.locator('#'+control).click()
                    for identifier in posed:self.assertFalse(page.evaluate('(id)=>window.hatchEditor.objects.get(id).visible',identifier))
                timeline=page.locator('#timeline')
                timeline.get_by_label('Exact frame',exact=True).fill('1')
                timeline.get_by_role('button',name='Scrub',exact=True).click()
                page.wait_for_function("() => document.querySelector('#timeline [role=status]').textContent.includes('frame 1/1')")
                scrubbed=world_vertices()
                for identifier,meshes in evaluated['native_geometry'].items():
                    for mesh,points in meshes.items():np.testing.assert_allclose(scrubbed[identifier][mesh],points,atol=1e-6,rtol=1e-6)
                for identifier in posed:self.assertTrue(page.evaluate('(id)=>window.hatchEditor.objects.get(id).visible',identifier))
                timeline.get_by_role('button',name='Return to authored scene',exact=True).click()
                for identifier in posed:self.assertFalse(page.evaluate('(id)=>window.hatchEditor.objects.get(id).visible',identifier))
                timeline.get_by_role('button',name='Play',exact=True).click()
                page.wait_for_function('(ids) => ids.every(id=>window.hatchEditor.objects.get(id).visible)',arg=posed)
                if timeline.get_by_role('button',name='Pause',exact=True).count():
                    timeline.get_by_role('button',name='Pause',exact=True).click()
                page.reload()
                try:page.wait_for_function('() => window.editorReady === true')
                except Exception:
                    print('POSE_REOPEN_DIAGNOSTIC',json.dumps(dict(page_errors=page_errors,failed_requests=failed_requests,
                        state=page.evaluate('()=>({ready:window.editorReady,status:document.querySelector("#status")?.textContent,errors:window.hatchEditor?.errors})'))),flush=True)
                    raise
                for identifier in posed:self.assertFalse(page.evaluate('(id)=>window.hatchEditor.objects.get(id).visible',identifier))
                page.locator('#show-all').click()
                for identifier in posed:self.assertFalse(page.evaluate('(id)=>window.hatchEditor.objects.get(id).visible',identifier))
                timeline.get_by_role('button',name='Scrub',exact=True).click()
                page.wait_for_function('(ids) => ids.every(id=>window.hatchEditor.objects.get(id).visible)',arg=posed)
                for identifier,meshes in evaluated['native_geometry'].items():
                    for mesh,points in meshes.items():np.testing.assert_allclose(world_vertices()[identifier][mesh],points,atol=1e-6,rtol=1e-6)
                reopened=EditorRepository(self.repository.root)
                self.assertEqual(describe_pose(reopened,reopened.snapshot(),'actor')['authored_pose']['bones']['b1']['translation'][0],.35)
            finally:browser.close()


if __name__=='__main__':unittest.main()
