"""Canonical saved graph state, actual world vertices and supported mouse edits."""
from contextlib import closing
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
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

    def fixture(self, mode, *, limits=None):
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
        elif mode == 'multi-drag':
            root['transform']['scale'] = [2, 1, 1]
            free = deepcopy(mesh)
            free.update(id='free', name='Free mesh', parent=None)
            free['transform']['translation'] = [-2, 0, 0]
            document['nodes'].append(free)
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
        if mode == 'removable-camera':
            extra=deepcopy(next(n for n in document['nodes'] if n['id']=='camera'))
            extra.update(id='preview-camera',name='Unreferenced preview camera')
            document['nodes'].insert(0,extra)
            document['cameras'].insert(0,'preview-camera')
        if mode == 'collection-hidden':
            peer=deepcopy(mesh);peer.update(id='peer',name='Visible peer',collections=[])
            peer['transform']['translation']=[-2,0,0];document['nodes'].append(peer)
        graph.replace_document(document, expected_revision=graph.revision)
        saved = snapshot_project(source, work/'portable', graph, scene_path='example-scene.json',
                                 expected_project_sha256=bundle['inventory']['project.json']['sha256'])
        repository = EditorRepository.create(work/'portable', work/'repo', scene_path=saved['active_scene']['path'],
                                             limits=limits)
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
        saved=snapshot_project(source,work/'portable',graph,scene_path='example-scene.json',
                               expected_project_sha256=bundle['inventory']['project.json']['sha256'])
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
    def test_browser_edits_debounce_one_exact_checkpoint_and_reopen(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium

        delay = 5.0
        repository, _ = self.fixture('plain', limits={'autosave_delay': delay})
        checkpoint = repository.root / 'autosave.json'
        with EditorServer(repository) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as runtime, closing(launch_chromium(runtime)) as browser:
                    page = browser.new_page()
                    page.goto(server.url)
                    page.wait_for_function('() => window.editorReady === true')
                    page.locator('[data-id="mesh"]').click()
                    field = page.get_by_label('translation X', exact=True)
                    def edit(revision, value):
                        field.fill(value)
                        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                                                  and response.request.method == 'POST') as acknowledged:
                            field.press('Tab')
                        self.assertEqual(acknowledged.value.status, 200)
                        page.wait_for_function('''revision => window.hatchEditor.snapshot.revision === revision &&
                            document.querySelector('#status').textContent === `Saved revision ${revision}`''', arg=revision)

                    edit(1, '3')
                    first_acknowledged = time.monotonic()
                    time.sleep(2.0)
                    second_started = time.monotonic()
                    edit(2, '4')
                    self.assertFalse(checkpoint.exists())
                    time.sleep(max(0, second_started + delay - 0.75 - time.monotonic()))
                    observed = time.monotonic()
                    self.assertGreater(observed, first_acknowledged + delay + 0.5)
                    self.assertLess(observed, second_started + delay)
                    self.assertFalse(checkpoint.exists())

                    deadline = time.monotonic() + delay + 5.0
                    while not checkpoint.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(checkpoint.exists())
                    saved = EditorRepository(repository.root).snapshot()
                    record = json.loads(checkpoint.read_text())
                    self.assertEqual((record['revision'], record['sha256']), (2, saved['sha256']))

                    page.evaluate('window.autosaveReopenSentinel = true')
                    page.goto('about:blank')
                    page.goto(server.url)
                    page.wait_for_function('() => window.editorReady === true')
                    self.assertTrue(page.evaluate("() => typeof window.autosaveReopenSentinel === 'undefined'"))
                    self.assertEqual(page.evaluate('() => window.hatchEditor.snapshot.sha256'), record['sha256'])
                    self.assertEqual(page.evaluate("() => window.hatchEditor.objects.get('mesh').position.x"), 4)
                    self.assertEqual(page.evaluate('() => window.hatchEditor.errors'), [])

                    page.locator('[data-id="mesh"]').click()
                    edit(3, '5')
            finally:
                server.shutdown()
                thread.join(timeout=5)
        self.assertEqual(json.loads(checkpoint.read_text()), record)
        reopened = EditorRepository(repository.root).snapshot()
        self.assertEqual(reopened['revision'], 3)
        self.assertEqual(next(node['transform']['translation'][0] for node in reopened['scene']['nodes']
                              if node['id'] == 'mesh'), 5)

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
                    before = repository.snapshot()
                    original = next(node['transform'] for node in before['scene']['nodes'] if node['id'] == 'mesh')
                    original_world = SceneGraph(before['scene']).evaluate_base('mesh')['world_matrix']
                    read_position = "() => window.hatchEditor.objects.get('mesh').position.toArray()"
                    command_requests = []

                    def record_command(request):
                        if request.method == 'POST' and request.url.endswith('/api/commands'):
                            command_requests.append(request.url)

                    page.on('request', record_command)
                    page.mouse.move(point['x'], point['y'])
                    page.mouse.down()
                    page.mouse.move(point['x'] + 70, point['y'], steps=12)
                    self.assertTrue(page.evaluate('() => window.hatchEditor.gizmo.dragging'))
                    self.assertFalse(np.allclose(page.evaluate(read_position), original['translation']))
                    page.keyboard.press('Escape')
                    np.testing.assert_allclose(page.evaluate(read_position), original['translation'], atol=1e-7)
                    page.mouse.up()
                    self.assertFalse(page.evaluate('() => window.hatchEditor.gizmo.dragging'))
                    np.testing.assert_allclose(page.evaluate(read_position), original['translation'], atol=1e-7)
                    after = EditorRepository(repository.root).snapshot()
                    self.assertEqual((after['revision'], after['sha256']), (before['revision'], before['sha256']))
                    np.testing.assert_allclose(SceneGraph(after['scene']).evaluate_base('mesh')['world_matrix'],
                                               original_world, atol=1e-7)
                    self.assertEqual(command_requests, [])

                    page.locator('[data-id="mesh"]').click()
                    page.mouse.move(point['x'],point['y']);page.mouse.down()
                    page.mouse.move(point['x']+70,point['y'],steps=12);page.mouse.up()
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                    self.assertEqual(len(command_requests), 1)
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
        saved=snapshot_project(source,self.root/'pivot-import-portable',graph,scene_path=before['scene_path'],
                               expected_project_sha256=before['inventory']['project.json']['sha256'])
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
            return snapshot_project(source,destination,graph,scene_path=scene_path,
                                    expected_project_sha256=bundle['inventory']['project.json']['sha256'])['active_scene']['path']
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

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_isolation_keeps_selected_ancestor_path_and_restores_native_visibility(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with sync_playwright() as runtime:
            browser=launch_chromium(runtime)
            try:
                for mode,selected,hidden in [('plain','mesh','camera'),('collection-hidden','peer','mesh')]:
                    repository,_=self.fixture(mode)
                    server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                    try:
                        page=browser.new_page();page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                        before=repository.snapshot()
                        page.locator('[data-id="'+selected+'"]').click();page.click('#isolate')
                        value=page.evaluate('''({selected,hidden}) => {
                            const object=window.hatchEditor.objects.get(selected);let effective=true;
                            for(let parent=object;parent;parent=parent.parent)effective=effective&&parent.visible;
                            return {effective,hidden:window.hatchEditor.objects.get(hidden).visible};
                        }''',dict(selected=selected,hidden=hidden))
                        self.assertTrue(value['effective']);self.assertFalse(value['hidden'])
                        page.click('#show-all')
                        native=SceneGraph(before['scene']).evaluate_base()
                        actual=page.evaluate('() => Object.fromEntries([...window.hatchEditor.objects].map(([id,object])=>[id,object.visible]))')
                        self.assertEqual(actual,{identifier:state['editor_visible'] for identifier,state in native.items()})
                        self.assertEqual(repository.snapshot()['sha256'],before['sha256'])
                        page.close()
                    finally:server.shutdown();server.server_close();thread.join(timeout=5)
            finally:browser.close()

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_active_native_camera_rebinds_after_save_refresh_history_and_reopen(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        repository,_=self.fixture('removable-camera')
        server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as runtime:
                browser=launch_chromium(runtime)
                try:
                    page=browser.new_page();page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                    page.select_option('#view','camera')
                    def assert_camera(expected_x):
                        actual=page.evaluate('''() => {
                            const current=window.hatchEditor.objects.get('preview-camera').children.find(object=>object.isCamera);
                            window.hatchEditor.camera.updateWorldMatrix(true,false);
                            return {same:current===window.hatchEditor.camera,matrix:window.hatchEditor.camera.matrixWorld.elements,
                                native:window.hatchEditor.snapshot.viewport_state.nodes['preview-camera'].world_matrix,
                                mode:document.querySelector('#view').value};
                        }''')
                        self.assertTrue(actual['same']);self.assertEqual(actual['mode'],'camera')
                        np.testing.assert_allclose(np.asarray(actual['matrix']).reshape(4,4).T,actual['native'],atol=1e-7)
                        self.assertAlmostEqual(actual['matrix'][12],expected_x)
                    assert_camera(0)
                    page.locator('[data-id="preview-camera"]').click()
                    page.get_by_label('translation X',exact=True).fill('10');page.get_by_label('translation X',exact=True).press('Tab')
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1 && document.querySelector("#status").textContent.includes("Saved revision")')
                    assert_camera(10)
                    page.evaluate('window.previousCamera = window.hatchEditor.camera')
                    page.click('#refresh');page.wait_for_function('() => window.editorReady === true && window.hatchEditor.camera !== window.previousCamera')
                    assert_camera(10)
                    page.click('#undo');page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2')
                    assert_camera(0)
                    page.click('#redo');page.wait_for_function('() => window.hatchEditor.snapshot.revision === 3')
                    assert_camera(10)
                    page.evaluate('window.cameraReopenSentinel = true')
                    page.goto('about:blank');page.goto(server.url)
                    page.wait_for_function('() => window.editorReady === true')
                    self.assertTrue(page.evaluate("() => typeof window.cameraReopenSentinel === 'undefined'"))
                    self.assertEqual(page.evaluate('window.hatchEditor.snapshot.revision'),3)
                    page.select_option('#view','camera');assert_camera(10)
                    page.locator('[data-id="preview-camera"]').click();page.click('#delete')
                    page.wait_for_function('() => window.hatchEditor.snapshot.revision === 4')
                    self.assertEqual(page.locator('#view').input_value(),'perspective')
                    self.assertEqual(page.locator('#view-label').inner_text(),'perspective')
                    self.assertFalse(page.evaluate('window.hatchEditor.camera.parent !== null'))
                    self.assertEqual(page.evaluate('window.hatchEditor.errors'),['No native camera is available.'])
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_outliner_restores_own_flags_without_enabling_viewport_selection(self):
        from playwright.sync_api import expect, sync_playwright
        from browser_runtime import launch_chromium

        repository, _ = self.fixture('plain')
        server = EditorServer(repository)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as runtime, closing(launch_chromium(runtime)) as browser:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.goto(server.url)
                page.wait_for_function('() => window.editorReady === true')
                row = page.locator('[data-id="mesh"]')
                locked, visible = page.locator('#locked'), page.locator('#visible')
                x = page.get_by_label('translation X', exact=True)
                row.click()
                locked.click()
                page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                row.click()
                expect(locked).to_be_enabled()
                expect(x).to_be_disabled()
                self.assertEqual(page.evaluate('window.hatchEditor.selection'), [])
                self.assertFalse(page.evaluate('!!window.hatchEditor.gizmo.object'))
                locked.click()
                page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2')
                row.click()
                expect(x).to_be_enabled()
                visible.click()
                page.wait_for_function('() => window.hatchEditor.snapshot.revision === 3')

                page.goto('about:blank')
                page.goto(server.url)
                page.wait_for_function('() => window.editorReady === true')
                self.assertFalse(page.evaluate("window.hatchEditor.objects.get('mesh').visible"))
                row.click()
                expect(visible).to_be_enabled()
                expect(x).to_be_disabled()
                self.assertEqual(page.evaluate('window.hatchEditor.selection'), [])
                self.assertFalse(page.evaluate('!!window.hatchEditor.gizmo.object'))
                visible.click()
                page.wait_for_function('() => window.hatchEditor.snapshot.revision === 4')
                row.click()
                expect(x).to_be_enabled()
                node = next(node for node in repository.snapshot()['scene']['nodes'] if node['id'] == 'mesh')
                self.assertTrue(node['visible'])
                self.assertFalse(node['locked'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_multi_drag_rejects_shear_and_saves_representable_translation(self):
        from playwright.sync_api import expect, sync_playwright
        from browser_runtime import launch_chromium

        repository, _ = self.fixture('multi-drag')
        before = repository.snapshot()
        server = EditorServer(repository)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as runtime, closing(launch_chromium(runtime)) as browser:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.goto(server.url)
                page.wait_for_function('() => window.editorReady === true')
                page.locator('[data-id="free"]').click()
                page.locator('[data-id="mesh"]').click(modifiers=['Shift'])
                page.locator('#isolate').click()
                self.assertFalse(page.evaluate("window.hatchEditor.objects.get('camera').visible"))
                page.select_option('#tool', 'rotate')
                requests = []
                page.on('request', lambda request: requests.append(request.url)
                        if request.method == 'POST' and request.url.endswith('/api/commands') else None)
                page.evaluate("""async () => {
                  const THREE = await import('/vendor/build/three.module.min.js');
                  const {gizmo, objects} = window.hatchEditor;
                  if (gizmo.object !== objects.get('free')) throw Error('Expected the root-level primary');
                  gizmo.dragging = true;
                  gizmo.object.quaternion.setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 4);
                  gizmo.dispatchEvent({type: 'objectChange'});
                  gizmo.dragging = false;
                }""")
                expect(page.locator('#diagnostic')).to_contain_text('would require shear')
                self.assertEqual(requests, [])
                self.assertFalse(page.evaluate("window.hatchEditor.objects.get('camera').visible"))
                self.assertEqual(repository.snapshot()['sha256'], before['sha256'])
                np.testing.assert_allclose(page.evaluate("window.hatchEditor.objects.get('free').quaternion.toArray()"),
                                           [0, 0, 0, 1], atol=1e-7)
                np.testing.assert_allclose(page.evaluate("window.hatchEditor.objects.get('mesh').scale.toArray()"),
                                           [1, 1, 1], atol=1e-7)

                page.select_option('#tool', 'translate')
                page.evaluate("""() => {
                  const {gizmo} = window.hatchEditor;
                  gizmo.dragging = true;
                  gizmo.object.position.x += 2;
                  gizmo.dispatchEvent({type: 'objectChange'});
                  gizmo.dragging = false;
                }""")
                page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                self.assertEqual(len(requests), 1)
                saved = repository.snapshot()
                for identifier in ('free', 'mesh'):
                    source = SceneGraph(before['scene']).evaluate_base(identifier)['world_matrix']
                    result = SceneGraph(saved['scene']).evaluate_base(identifier)['world_matrix']
                    offset = np.asarray(result)[:3, 3] - np.asarray(source)[:3, 3]
                    np.testing.assert_allclose(offset, [2, 0, 0], atol=1e-7)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_shared_shading_reloads_dispose_native_material_and_texture_only(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium

        repository, _ = self.fixture('plain')
        server = EditorServer(repository)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as runtime, closing(launch_chromium(runtime)) as browser:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.goto(server.url)
                page.wait_for_function('() => window.editorReady === true')
                for mode in ('solid', 'normals'):
                    page.select_option('#shading', 'material')
                    page.evaluate("""async () => {
                      const THREE = await import('/vendor/build/three.module.min.js');
                      const mesh = window.hatchEditor.objects.get('mesh').children.find(child => child.isMesh)
                        || window.hatchEditor.objects.get('mesh').getObjectByProperty('isMesh', true);
                      const native = mesh.material;
                      const texture = new THREE.CanvasTexture(document.createElement('canvas'));
                      native.map = texture;
                      native.needsUpdate = true;
                      const fixture = window.resourceFixture = {native: 0, texture: 0, shared: 0};
                      fixture.mesh = mesh;
                      native.addEventListener('dispose', () => fixture.native++);
                      texture.addEventListener('dispose', () => fixture.texture++);
                    }""")
                    page.select_option('#shading', mode)
                    page.evaluate("""() => {
                      const fixture = window.resourceFixture;
                      fixture.preview = fixture.mesh.material;
                      fixture.preview.addEventListener('dispose', () => fixture.shared++);
                    }""")
                    page.click('#refresh')
                    page.wait_for_function("""() => {
                      const object = window.hatchEditor.objects.get('mesh');
                      const mesh = object?.getObjectByProperty('isMesh', true);
                      return !!mesh && mesh !== window.resourceFixture.mesh &&
                        mesh.material === window.resourceFixture.preview;
                    }""")
                    self.assertEqual(page.evaluate("""() => {
                      const {native, texture, shared} = window.resourceFixture;
                      return [native, texture, shared];
                    }"""), [1, 1, 0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual native-state browser proof explicitly.')
    def test_unavailable_exposure_keeps_saved_history_and_disables_scene_tools(self):
        from playwright.sync_api import expect, sync_playwright
        from browser_runtime import launch_chromium

        repository, _ = self.fixture('plain')
        initial = repository.snapshot()
        server = EditorServer(repository)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as runtime, closing(launch_chromium(runtime)) as browser:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(server.url)
                page.wait_for_function('() => window.editorReady === true')
                page.select_option('#view', 'camera')
                page.evaluate('window.previousCamera = window.hatchEditor.camera')
                repository.apply(0, dict(schema_version=1, commands=[
                    dict(operation='configure-world', parameters=dict(properties=dict(exposure=21)))
                ]), 'high-exposure', expected_sha256=initial['sha256'])
                unavailable = repository.snapshot()
                page.locator('#refresh').click()
                expect(page.locator('#status')).to_have_text('Saved revision 1; viewport unavailable.')
                expect(page.locator('#diagnostic')).to_contain_text('exposure is supported')
                expect(page.locator('#revision')).to_contain_text(unavailable['sha256'][:10])
                expect(page.locator('#view-label')).to_have_text('Viewport unavailable')
                expect(page.locator('#outliner [data-id]')).to_have_count(0)
                expect(page.locator('#stats')).to_be_empty()
                for control in ('view', 'filter', 'all', 'frame', 'play', 'show-all', 'locked'):
                    expect(page.locator('#' + control)).to_be_disabled()
                expect(page.locator('#refresh')).to_be_enabled()
                expect(page.locator('#undo')).to_be_enabled()
                self.assertFalse(page.evaluate('window.editorReady'))
                self.assertFalse(page.evaluate('window.previousCamera === window.hatchEditor.camera'))
                self.assertEqual(page.evaluate('window.hatchEditor.objects.size'), 0)

                page.locator('#viewport').focus()
                page.keyboard.press('r')
                expect(page.locator('#tool')).to_have_value('translate')
                page.locator('#undo').click()
                expect(page.locator('#status')).to_have_text('Loaded saved revision 2')
                expect(page.locator('#diagnostic')).to_be_hidden()
                expect(page.locator('#view')).to_be_enabled()
                expect(page.locator('#filter')).to_be_enabled()
                expect(page.locator('#view-label')).to_have_text('camera')
                expect(page.locator('#outliner [data-id="mesh"]')).to_be_visible()
                self.assertEqual(repository.snapshot()['sha256'], initial['sha256'])

                page.locator('#redo').click()
                expect(page.locator('#status')).to_have_text('Saved revision 3; viewport unavailable.')
                self.assertEqual(repository.snapshot()['sha256'], unavailable['sha256'])
                page.locator('#refresh').click()
                expect(page.locator('#diagnostic')).to_contain_text('exposure is supported')
                self.assertEqual(errors, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
