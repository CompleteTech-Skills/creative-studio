"""Canonical saved graph state, actual world vertices and supported mouse edits."""
from copy import deepcopy
import math
import os
from pathlib import Path
import tempfile
import threading
import unittest
import numpy as np

from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer, preview_snapshot
from rig_import import load_character_model
from rig_math import transform_points
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_snapshot import snapshot_project


class EditorNativeStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, mode):
        work = self.root/mode; work.mkdir()
        source = work/'source'; bundle = build_example(source)
        graph = SceneGraph.from_bundle(bundle, 'example-scene.json', source_root=source)
        document = graph.document()
        mesh = next(n for n in document['nodes'] if n['id'] == 'mesh')
        root = next(n for n in document['nodes'] if n['id'] == 'root')
        if mode in ('collection-hidden', 'collection-editor-hidden', 'inherited-lock'):
            document['collections'] = [dict(id='actors', name='Actors', visible=mode != 'collection-hidden',
                editor_visible=mode != 'collection-editor-hidden', locked=mode == 'inherited-lock')]
            (root if mode == 'inherited-lock' else mesh)['collections'] = ['actors']
        elif mode == 'constructor':
            extra=deepcopy(mesh);extra.update(id='constructor',name='Constructor node')
            extra['transform']['translation']=[-2,0,0];document['nodes'].append(extra)
        elif mode == 'locked-parent':
            root['locked']=True
        elif mode == 'layer-hidden':
            document['layers'] = [dict(id='actors', name='Actors layer', visible=False)]
            root['layers'] = ['actors']
        elif mode == 'pivot':
            free = deepcopy(mesh); free.update(id='free', name='Free mesh')
            free['transform']['translation'] = [-2, 0, 0]; document['nodes'].append(free)
            mesh['transform'].update(rotation=[0,0,math.sqrt(.5),math.sqrt(.5)], pivot=[1,0,0])
        elif mode == 'constraint':
            mesh['constraints'] = [dict(id='follow-camera', type='copy-transform', target='camera', influence=1)]
        elif mode == 'attachment':
            socket = deepcopy(root); socket.update(id='socket', name='Socket', type='socket', parent='root')
            socket['transform']['translation'] = [4,5,6]; document['nodes'].append(socket)
            mesh['attachment'] = dict(owner='root', socket='socket', offset=dict(translation=[1,0,0],rotation=[0,0,0,1],scale=[1,1,1]))
        elif mode == 'parent-order':
            root['transform'].update(translation=[3,4,5], scale=[2,1,.5], rotation=[0,0,math.sin(.2),math.cos(.2)])
            mesh['transform']['rotation'] = [math.sin(.1),0,0,math.cos(.1)]
            document['nodes'].reverse()
        if mode == 'collection-hidden':
            peer=deepcopy(mesh);peer.update(id='peer',name='Visible peer',collections=[])
            peer['transform']['translation']=[-2,0,0];document['nodes'].append(peer)
        graph.replace_document(document, expected_revision=graph.revision)
        saved = snapshot_project(source, work/'portable', graph, scene_path='example-scene.json')
        repository = EditorRepository.create(work/'portable', work/'repo', scene_path=saved['active_scene']['path'])
        vertices = load_character_model(source/'triangle.obj').geometry.meshes[0].positions
        return repository, vertices

    def test_projection_uses_native_world_matrices_effective_flags_and_readonly_state(self):
        for mode in ('plain','constructor','locked-parent','collection-hidden','collection-editor-hidden','layer-hidden','inherited-lock','pivot','constraint','attachment','parent-order'):
            with self.subTest(mode=mode):
                repository, _ = self.fixture(mode)
                snapshot = repository.snapshot(); value = preview_snapshot(repository, snapshot)
                projected = value['viewport_state']
                self.assertTrue(projected['available'])
                self.assertEqual((projected['revision'], projected['sha256']), (snapshot['revision'], snapshot['sha256']))
                expected = SceneGraph.from_bundle(snapshot, snapshot['scene_path']).evaluate_base()
                self.assertEqual(projected['nodes'], expected)
                if mode in ('pivot','constraint','attachment'):
                    self.assertIn('mesh', projected['transform_readonly'])
                else:
                    self.assertNotIn('mesh', projected['transform_readonly'])

    def prefab_fixture(self):
        from scene3d_prefabs import PrefabLibrary
        work=self.root/'prefab';work.mkdir();source=work/'source';bundle=build_example(source)
        graph=SceneGraph.from_bundle(bundle,'example-scene.json',source_root=source)
        library=PrefabLibrary.from_files(source,bundle)
        library.instantiate(graph,bundle['project']['prefabs'][0],'prefab-instance')
        saved=snapshot_project(source,work/'portable',graph,scene_path='example-scene.json')
        return EditorRepository.create(work/'portable',work/'repo',scene_path=saved['active_scene']['path'])

    def test_unexpanded_prefab_is_explicitly_unavailable_without_changing_saved_project(self):
        repository=self.prefab_fixture();before=repository.snapshot()
        value=preview_snapshot(repository,before)
        self.assertFalse(value['viewport_state']['available'])
        self.assertEqual(value['viewport_state']['error']['code'],'SCENE3D_PREFAB')
        self.assertEqual(repository.snapshot(),before)

    def test_unused_generic_assets_are_not_decoded(self):
        from unittest.mock import patch
        repository,_=self.fixture('plain')
        repository.apply(0,dict(schema_version=1,commands=[dict(operation='delete',parameters=dict(identifier='mesh',policy='subtree'))]),'remove-mesh')
        with patch('rig_import.load_character_model',side_effect=AssertionError('Unused asset was decoded')):
            value=preview_snapshot(repository,repository.snapshot())
        self.assertEqual(value['viewport_assets'],{})
        self.assertTrue(value['viewport_state']['available'])

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_actual_browser_native_vertices_visibility_locks_and_mixed_readonly_selection(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with sync_playwright() as runtime:
            browser = launch_chromium(runtime)
            try:
                for mode in ('plain','constructor','locked-parent','collection-hidden','collection-editor-hidden','layer-hidden','inherited-lock','pivot','constraint','attachment','parent-order'):
                    with self.subTest(mode=mode):
                        repository, vertices = self.fixture(mode)
                        server = EditorServer(repository); thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
                        try:
                            page = browser.new_page(viewport={'width':1400,'height':900})
                            page.goto(server.url); page.wait_for_function('() => window.editorReady === true')
                            saved = repository.snapshot(); native = SceneGraph(saved['scene']).evaluate_base('mesh')
                            actual = page.evaluate('''() => {
                                const object=window.hatchEditor.objects.get('mesh'); object.updateWorldMatrix(true,true);
                                const vertices=[];object.traverse(mesh=>{if(mesh.isMesh){const p=mesh.geometry.attributes.position;
                                    for(let i=0;i<p.count;i++)vertices.push(mesh.localToWorld(mesh.position.clone().set(p.getX(i),p.getY(i),p.getZ(i))).toArray());}});
                                let visible=true;for(let p=object;p;p=p.parent)visible=visible&&p.visible;
                                return {vertices,visible};
                            }''')
                            np.testing.assert_allclose(actual['vertices'], transform_points(np.asarray(native['world_matrix']), vertices),atol=1e-6)
                            self.assertEqual(actual['visible'], native['editor_visible'])
                            row = page.locator('[data-id="mesh"]')
                            if native['selectable']:
                                row.click()
                            else:
                                self.assertTrue(row.is_disabled())
                                row.evaluate('element => element.click()')
                            selected = page.evaluate('window.hatchEditor.selection')
                            self.assertEqual('mesh' in selected, native['selectable'])
                            blocked = not native['selectable'] or mode in ('pivot','constraint','attachment')
                            self.assertEqual(page.get_by_label('translation X',exact=True).is_disabled(),blocked)
                            self.assertEqual(page.evaluate("window.hatchEditor.gizmo.object === window.hatchEditor.objects.get('mesh')"),not blocked)
                            if mode == 'pivot':
                                page.evaluate("window.hatchEditor.select('free'); window.hatchEditor.select('mesh',true)")
                                self.assertTrue(page.get_by_label('translation X',exact=True).is_disabled())
                                self.assertTrue(page.evaluate('!window.hatchEditor.gizmo.object'))
                                page.locator('#viewport').focus(); page.keyboard.press('ArrowRight')
                                self.assertEqual(repository.snapshot()['revision'],0)
                            if mode == 'collection-hidden':
                                page.locator('[data-id="peer"]').click();page.click('#same-type')
                                self.assertEqual(page.evaluate('window.hatchEditor.selection'),['peer'])
                                page.locator('[data-id="root"]').click();page.click('#children')
                                self.assertNotIn('mesh',page.evaluate('window.hatchEditor.selection'))
                            elif mode == 'locked-parent':
                                page.click('#parent-select')
                                self.assertEqual(page.evaluate('window.hatchEditor.selection'),[])
                            elif mode == 'constructor':
                                page.locator('[data-id="constructor"]').click()
                                self.assertNotIn('transform read-only',page.locator('#selected-type').inner_text())
                                self.assertFalse(page.get_by_label('translation X',exact=True).is_disabled())
                                page.get_by_label('translation X',exact=True).fill('-3')
                                page.get_by_label('translation X',exact=True).press('Tab')
                                page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                                self.assertEqual(next(n for n in repository.snapshot()['scene']['nodes'] if n['id']=='constructor')['transform']['translation'][0],-3)
                            self.assertEqual(page.evaluate('window.hatchEditor.errors'),[])
                            page.close()
                        finally:server.shutdown();server.server_close();thread.join(timeout=5)
            finally:browser.close()

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_collection_control_saves_effective_visibility_without_overwriting_authored_flag(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        repository,_ = self.fixture('plain')
        repository.apply(0,dict(schema_version=1,commands=[dict(operation='collection',parameters=dict(identifier='actors',name='Actors',visible=True)),
            dict(operation='memberships',parameters=dict(identifier='mesh',collections=['actors']))]),'add-collection')
        server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as runtime:
                browser=launch_chromium(runtime)
                try:
                    page=browser.new_page();page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                    page.locator('#collections input').uncheck()
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2')
                    page.wait_for_function("window.hatchEditor.objects.get('mesh').visible === false")
                    saved=repository.snapshot()
                    self.assertTrue(next(node for node in saved['scene']['nodes'] if node['id']=='mesh')['visible'])
                    page.click('#show-all')
                    self.assertFalse(page.evaluate("window.hatchEditor.objects.get('mesh').visible"))
                    self.assertEqual(page.evaluate('window.hatchEditor.errors'),[])
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_real_mouse_gizmo_move_saves_and_reopens_native_state(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        repository,vertices=self.fixture('plain')
        server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as runtime:
                browser=launch_chromium(runtime)
                try:
                    page=browser.new_page(viewport={'width':1400,'height':900});page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                    page.locator('[data-id="mesh"]').click()
                    point=page.evaluate('''async () => {
                        const THREE=await import('/vendor/build/three.module.min.js');
                        const editor=window.hatchEditor, helper=editor.gizmo.getHelper();helper.updateWorldMatrix(true,true);
                        const handles=[];helper.traverse(object=>{if(object.name==='X'&&object.isMesh){let visible=true;
                            for(let p=object;p;p=p.parent)visible=visible&&p.visible;
                            if(visible)handles.push(object);}});
                        if(!handles.length)throw Error('No visible native X gizmo handle');
                        const center=new THREE.Box3().setFromObject(handles[0]).getCenter(new THREE.Vector3()).project(editor.camera);
                        const rect=editor.renderer.domElement.getBoundingClientRect();
                        return {x:rect.left+(center.x+1)*rect.width/2,y:rect.top+(1-center.y)*rect.height/2};
                    }''')
                    page.mouse.move(point['x'],point['y']);page.mouse.down()
                    page.mouse.move(point['x']+70,point['y'],steps=12);page.mouse.up()
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                    saved=EditorRepository(repository.root).snapshot(); native=SceneGraph(saved['scene']).evaluate_base('mesh')
                    self.assertNotEqual(next(n for n in saved['scene']['nodes'] if n['id']=='mesh')['transform']['translation'],[2,0,0])
                    page.goto('about:blank');page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                    actual=page.evaluate('''() => {const o=window.hatchEditor.objects.get('mesh');o.updateWorldMatrix(true,true);let result=[];
                        o.traverse(m=>{if(m.isMesh){const p=m.geometry.attributes.position;for(let i=0;i<p.count;i++)result.push(m.localToWorld(m.position.clone().set(p.getX(i),p.getY(i),p.getZ(i))).toArray());}});return result;}''')
                    np.testing.assert_allclose(actual,transform_points(np.asarray(native['world_matrix']),vertices),atol=1e-6)
                    self.assertEqual(page.evaluate('window.hatchEditor.errors'),[])
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_unsupported_prefab_has_no_false_ready_or_saved_render_success(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        repository=self.prefab_fixture();before=repository.snapshot()
        server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as runtime:
                browser=launch_chromium(runtime)
                try:
                    page=browser.new_page();page.goto(server.url)
                    page.wait_for_function('() => document.querySelector("#status").textContent.includes("viewport unavailable")')
                    self.assertIsNot(page.evaluate('window.editorReady'),True)
                    self.assertIn('Expand prefab',page.locator('#diagnostic').inner_text())
                    self.assertEqual(page.evaluate('window.hatchEditor.objects.size'),0)
                    self.assertEqual(page.evaluate('window.hatchEditor.snapshot.revision'),before['revision'])
                    self.assertEqual(repository.snapshot(),before)
                    self.assertTrue(page.evaluate('''() => {
                        window.nativeContextControl=window.hatchEditor.renderer.getContext().getExtension('WEBGL_lose_context');
                        return Boolean(window.nativeContextControl);
                    }'''))
                    page.evaluate('window.nativeContextControl.loseContext()')
                    page.wait_for_function('() => document.querySelector("#diagnostic").textContent.includes("context lost")')
                    page.wait_for_timeout(100)
                    page.evaluate('window.nativeContextControl.restoreContext()')
                    page.wait_for_function('() => document.querySelector("#diagnostic").textContent.includes("Expand prefab") && document.querySelector("#status").textContent.includes("viewport unavailable")')
                    self.assertFalse(page.locator('#diagnostic').is_hidden())
                    self.assertIsNot(page.evaluate('window.editorReady'),True)
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_imported_component_pivot_matches_canonical_imported_world_vertices(self):
        import test_rig_editor3d_components as component_tests
        from scene3d_import import evaluate_imported_vertices
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        imported_case=component_tests.EditorImportedComponentTests();imported_case.setUp();self.addCleanup(imported_case.doCleanups)
        original=imported_case.repository;before=original.snapshot();source=original.native_snapshot(before['revision'],before['sha256'])['root']
        graph=SceneGraph.from_bundle(before,before['scene_path'],source_root=source)
        document=graph.document();component=next(node for node in document['nodes'] if 'source_component' in node.get('metadata',{}))
        component['transform'].update(rotation=[0,0,math.sqrt(.5),math.sqrt(.5)],pivot=[.5,0,0])
        graph.replace_document(document,expected_revision=graph.revision)
        expected=evaluate_imported_vertices(graph,imported_case.imported,imported_case.native_root)['nodes']
        saved=snapshot_project(source,self.root/'pivot-import-portable',graph,scene_path=before['scene_path'])
        repository=EditorRepository.create(self.root/'pivot-import-portable',self.root/'pivot-import-repo',scene_path=saved['active_scene']['path'])
        server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as runtime:
                browser=launch_chromium(runtime)
                try:
                    page=browser.new_page();page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                    actual=page.evaluate('''ids => Object.fromEntries(ids.map(id => {
                        const root=window.hatchEditor.objects.get(id);root.updateWorldMatrix(true,true);const meshes=[];
                        root.traverse(mesh=>{if(mesh.isMesh){const p=mesh.geometry.attributes.position,vertices=[];
                            for(let i=0;i<p.count;i++)vertices.push(mesh.localToWorld(mesh.position.clone().set(p.getX(i),p.getY(i),p.getZ(i))).toArray());
                            meshes.push({id:mesh.userData.nativeMeshId,vertices});}});return [id,meshes];}))''',list(expected))
                    for identifier,meshes in expected.items():
                        self.assertEqual(len(actual[identifier]),len(meshes))
                        for observed in actual[identifier]:np.testing.assert_allclose(observed['vertices'],meshes[observed['id']],atol=1e-6)
                    page.evaluate('id => window.hatchEditor.select(id)',component['id'])
                    self.assertTrue(page.get_by_label('translation X',exact=True).is_disabled())
                    self.assertTrue(page.evaluate('!window.hatchEditor.gizmo.object'))
                    self.assertEqual(page.evaluate('window.hatchEditor.errors'),[])
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_history_preserves_unavailable_prefab_diagnostic_and_redo_recovers_view(self):
        from scene3d_contracts import load_project
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        repository=self.prefab_fixture();original=repository.snapshot()
        def remove_prefab(source,destination,scene_path):
            bundle=load_project(source);graph=SceneGraph.from_bundle(bundle,scene_path,source_root=source)
            document=graph.document();document['nodes']=[node for node in document['nodes'] if node['id']!='prefab-instance']
            graph.replace_document(document,expected_revision=graph.revision)
            return snapshot_project(source,destination,graph,scene_path=scene_path)['active_scene']['path']
        repository.transact_native(0,'remove-prefab',{'operation':'remove-prefab'},remove_prefab,expected_sha256=original['sha256'])
        server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as runtime:
                browser=launch_chromium(runtime)
                try:
                    page=browser.new_page();page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                    page.click('#undo')
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2 && document.querySelector("#status").textContent.includes("viewport unavailable")')
                    self.assertFalse(page.locator('#diagnostic').is_hidden())
                    self.assertIn('Expand prefab',page.locator('#diagnostic').inner_text())
                    self.assertIsNot(page.evaluate('window.editorReady'),True)
                    page.click('#redo');page.wait_for_function('() => window.editorReady === true && window.hatchEditor.snapshot.revision === 3')
                    self.assertTrue(page.locator('#diagnostic').is_hidden())
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
