"""Real WebGL prefab vertices, geometry picking and persisted anchor editing."""
import os
from pathlib import Path
import threading
import unittest

import numpy as np

from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer
import test_rig_editor3d_prefabs as prefab_tests


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual prefab browser acceptance explicitly.')
class EditorPrefabBrowserTests(unittest.TestCase):
    def test_derived_api_targets_are_rejected_and_keyboard_selection_uses_the_anchor(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        fixture = prefab_tests.EditorPrefabProjectionTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        repository, snapshot, _ = fixture.fixture(upright=True)
        server = EditorServer(repository)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with sync_playwright() as runtime:
                browser = launch_chromium(runtime)
                try:
                    page = browser.new_page(viewport={'width':1400,'height':900})
                    requests=[]
                    page.on('request',lambda request: requests.append(request.post_data_json)
                        if request.method=='POST' and request.url.endswith('/api/commands') else None)
                    page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                    self.assertEqual(page.evaluate('window.hatchEditor.errors'),[])
                    proof=page.evaluate("""async () => {
                        const editor=window.hatchEditor, snapshot=editor.snapshot;
                        const id=snapshot.viewport_prefabs.mappings['instance-0']['prefab-mesh'];
                        const transform={translation:[0,0,0],rotation:[0,0,0,1],scale:[1,1,1]};
                        const node=structuredClone(snapshot.scene.nodes.find(node=>node.id==='mesh'));
                        node.id='forbidden-child';node.parent=id;
                        const commands=[
                          {operation:'transform',parameters:{identifier:id,transform}},
                          {operation:'rename',parameters:{identifier:id,name:'Must not save'}},
                          {operation:'delete',parameters:{identifier:id,policy:'subtree'}},
                          {operation:'duplicate',parameters:{identifier:id,new_id:'forbidden-copy'}},
                          {operation:'node-flags',parameters:{identifier:id,visible:false}},
                          {operation:'reparent',parameters:{identifier:'mesh',parent:id,preserve_world:true}},
                          {operation:'memberships',parameters:{identifier:id,collections:[]}},
                          {operation:'prefab-detach',parameters:{identifier:id}},
                          {operation:'create',parameters:{node}},
                        ];
                        const outcomes=[];
                        for(const command of commands)outcomes.push(await editor.commit([command]));
                        editor.select(id);
                        return {outcomes,selection:editor.selection,errors:editor.errors,revision:editor.snapshot.revision};
                    }""")
                    self.assertEqual(proof['outcomes'],[False]*9)
                    self.assertEqual(proof['selection'],['instance-0'])
                    self.assertEqual(proof['revision'],0)
                    self.assertEqual(len(proof['errors']),9)
                    self.assertTrue(all('Derived prefab' in message for message in proof['errors']))
                    self.assertEqual(requests,[])
                    self.assertEqual(repository.snapshot(),snapshot)
                    page.locator('#viewport').focus();page.keyboard.press('ArrowRight')
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision===1 && window.editorReady===true')
                    saved=EditorRepository(repository.root).snapshot()
                    anchor=next(node for node in saved['scene']['nodes'] if node['id']=='instance-0')
                    self.assertAlmostEqual(anchor['transform']['translation'][0],1.1)
                    self.assertEqual(anchor['type'],'prefab_instance')
                    self.assertEqual(len(requests),1)
                    self.assertTrue(all(identifier not in str(requests) for identifier in page.evaluate('window.hatchEditor.snapshot.viewport_prefabs.readonly_nodes')))
                    self.assertEqual(saved['project']['prefabs'],snapshot['project']['prefabs'])
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_real_geometry_pick_edits_only_the_authored_anchor_and_reopens(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        fixture = prefab_tests.EditorPrefabProjectionTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        repository, snapshot, _ = fixture.fixture(upright=True)
        prefab_bytes = {reference['path']: repository.read_file(reference['path'], snapshot['sha256'])
                        for reference in snapshot['project']['prefabs']}
        server = EditorServer(repository)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with sync_playwright() as runtime:
                browser = launch_chromium(runtime)
                try:
                    page = browser.new_page(viewport={'width':1400, 'height':900})
                    requests = []
                    page.on('request', lambda request: requests.append(request.post_data_json)
                            if request.method == 'POST' and request.url.endswith('/api/commands') else None)
                    page.goto(server.url); page.wait_for_function('() => window.editorReady === true')
                    displayed = page.evaluate('''() => {
                        const editor=window.hatchEditor, snapshot=editor.snapshot, projection=snapshot.viewport_prefabs;
                        const instances={};
                        for(const [anchor,mapping] of Object.entries(projection.mappings)) {
                            if(!snapshot.scene.nodes.some(node=>node.id===anchor))continue;
                            const id=mapping['prefab-mesh'], object=editor.objects.get(id), meshes=[];
                            object.updateWorldMatrix(true,true);
                            object.traverse(mesh=>{if(mesh.isMesh){const position=mesh.geometry.attributes.position,vertices=[];
                                for(let i=0;i<position.count;i++)vertices.push(mesh.localToWorld(mesh.position.clone().set(position.getX(i),position.getY(i),position.getZ(i))).toArray());
                                meshes.push(vertices);}});
                            instances[anchor]={id,meshes};
                        }
                        return {instances,sourceNodes:snapshot.scene.nodes.map(node=>({id:node.id,type:node.type})),readonly:projection.readonly_nodes};
                    }''')
                    self.assertEqual(set(displayed['instances']), {'instance-0','instance-1'})
                    for index in range(2):
                        meshes = displayed['instances']['instance-'+str(index)]['meshes']
                        self.assertEqual(len(meshes), 1)
                        np.testing.assert_allclose(meshes[0], [[index*3+1,0,0],[index*3+2,0,0],[index*3+1,1,0]], atol=1e-6, rtol=0)
                    self.assertEqual(len(displayed['sourceNodes']), 5)
                    self.assertTrue(all(node['type']=='prefab_instance' for node in displayed['sourceNodes'] if node['id'].startswith('instance-')))
                    self.assertTrue(set(displayed['readonly']).isdisjoint(node['id'] for node in displayed['sourceNodes']))
                    mesh_id = displayed['instances']['instance-0']['id']
                    click = page.evaluate('''id => {
                        const editor=window.hatchEditor, object=editor.objects.get(id);let mesh;
                        object.traverse(child=>{if(child.isMesh)mesh=child;});
                        const position=mesh.geometry.attributes.position;
                        const point=mesh.position.clone().set(0,0,0);
                        for(let i=0;i<position.count;i++)point.add(mesh.position.clone().set(position.getX(i),position.getY(i),position.getZ(i)));
                        point.divideScalar(position.count);mesh.localToWorld(point);point.project(editor.camera);
                        const rect=editor.renderer.domElement.getBoundingClientRect();
                        return {x:rect.left+(point.x+1)*rect.width/2,y:rect.top+(1-point.y)*rect.height/2};
                    }''', mesh_id)
                    page.mouse.click(click['x'],click['y'])
                    page.wait_for_function('() => window.hatchEditor.selection.length===1 && window.hatchEditor.selection[0]==="instance-0"')
                    x = page.get_by_label('translation X', exact=True)
                    self.assertTrue(x.is_enabled()); x.fill('2'); x.press('Tab')
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1 && window.editorReady === true')
                    self.assertTrue(requests)
                    for request in requests:
                        encoded = str(request)
                        self.assertFalse(any(identifier in encoded for identifier in displayed['readonly']))
                    saved = EditorRepository(repository.root).snapshot()
                    anchor = next(node for node in saved['scene']['nodes'] if node['id']=='instance-0')
                    self.assertEqual(anchor['type'],'prefab_instance')
                    self.assertEqual(anchor['transform']['translation'],[2,0,0])
                    for name,payload in prefab_bytes.items():
                        self.assertEqual(repository.read_file(name,saved['sha256']),payload)
                    page.goto(server.url); page.wait_for_function('() => window.editorReady === true')
                    self.assertEqual(page.evaluate('window.hatchEditor.snapshot.revision'),1)
                    actual = page.evaluate('''id => {const object=window.hatchEditor.objects.get(id);let mesh;
                        object.traverse(child=>{if(child.isMesh)mesh=child;});object.updateWorldMatrix(true,true);
                        return mesh.localToWorld(mesh.position.clone().set(0,0,0)).toArray();}''',mesh_id)
                    np.testing.assert_allclose(actual,[2,0,0],atol=1e-6,rtol=0)
                    self.assertEqual(page.evaluate('window.hatchEditor.errors'),[])
                    evidence = os.environ.get('HATCH_EDITOR_PREFAB_EVIDENCE')
                    if evidence:
                        output = Path(evidence); output.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(output/'prefab-browser.png'),full_page=True)
                finally:
                    browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
