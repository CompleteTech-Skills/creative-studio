"""Real keyboard and conventional control acceptance; no canvas substitutes."""
import os
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest

from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer
from scene3d_example import build_example


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual keyboard accessibility browser proof explicitly.')
class EditorKeyboardAccessibilityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);build_example(self.root/'source')
        self.repository=EditorRepository.create(self.root/'source',self.root/'repository')
        self.server=EditorServer(self.repository)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        self.runtime=sync_playwright().start();self.addCleanup(self.runtime.stop)
        self.browser=launch_chromium(self.runtime);self.addCleanup(self.browser.close)
        self.page=self.browser.new_page(viewport={'width':1400,'height':900})
        self.page.goto(self.server.url);self.page.wait_for_function('() => window.editorReady === true')

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)

    def test_outliner_keyboard_create_numeric_edit_save_undo_redo_and_reopen(self):
        page=self.page
        rows=page.get_by_role('tree',name='Scene objects').get_by_role('treeitem')
        self.assertEqual(rows.count(),3)
        self.assertEqual(page.locator('#outliner [tabindex="0"]').count(),1)
        root=page.locator('[data-id="root"]');root.focus()
        page.keyboard.press('ArrowRight')
        self.assertEqual(page.evaluate('document.activeElement.dataset.id'),'mesh')
        page.keyboard.press('Space')
        self.assertEqual(page.evaluate('window.hatchEditor.selection'),['mesh'])
        page.keyboard.press('ArrowDown');self.assertEqual(page.evaluate('document.activeElement.dataset.id'),'camera')
        page.keyboard.press('ArrowLeft');self.assertEqual(page.evaluate('document.activeElement.dataset.id'),'root')
        page.keyboard.press('End');self.assertEqual(page.evaluate('document.activeElement.dataset.id'),'camera')
        page.keyboard.press('Home');page.keyboard.press('ArrowDown');page.keyboard.press('Enter')
        self.assertEqual(self.repository.snapshot()['revision'],0)
        page.locator('#duplicate').focus();page.keyboard.press('Enter')
        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1 && document.querySelector("#status").textContent.includes("Saved revision")')
        copied=next(node['id'] for node in self.repository.snapshot()['scene']['nodes'] if node['id'].startswith('copy-'))
        # Home/End navigate without transforming either original or duplicate.
        root=page.locator('[data-id="root"]');root.focus();page.keyboard.press('Home')
        for _ in range(rows.count()):
            if page.evaluate('document.activeElement.dataset.id') == copied: break
            page.keyboard.press('ArrowDown')
        self.assertEqual(page.evaluate('document.activeElement.dataset.id'),copied)
        page.keyboard.press('Enter')
        self.assertEqual(page.evaluate('window.hatchEditor.selection'),[copied])
        field=page.get_by_label('translation X',exact=True);field.focus();field.fill('3.25');field.press('Tab')
        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2 && document.querySelector("#status").textContent.includes("Saved revision")')
        self.assertEqual(next(n for n in self.repository.snapshot()['scene']['nodes'] if n['id']==copied)['transform']['translation'][0],3.25)
        page.locator('#viewport').focus();page.keyboard.press('Control+z')
        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 3')
        page.keyboard.press('Control+Shift+Z');page.wait_for_function('() => window.hatchEditor.snapshot.revision === 4')
        page.goto('about:blank');page.goto(self.server.url);page.wait_for_function('() => window.editorReady === true')
        self.assertEqual(page.evaluate('window.hatchEditor.snapshot.revision'),4)
        node=next(n for n in self.repository.snapshot()['scene']['nodes'] if n['id']==copied)
        self.assertEqual(node['transform']['translation'][0],3.25)
        self.assertEqual(page.locator('#status').get_attribute('aria-live'),'polite')
        self.assertEqual(page.evaluate('window.hatchEditor.errors'),[])

    def test_searchable_modal_keyboard_reference_traps_focus_and_restores_invoker(self):
        page=self.page;button=page.get_by_role('button',name='Keyboard reference (?)',exact=True)
        button.focus();page.keyboard.press('Enter')
        dialog=page.get_by_role('dialog',name='Keyboard reference',exact=True)
        self.assertTrue(dialog.is_visible())
        search=page.get_by_role('searchbox',name='Find a shortcut');self.assertTrue(search.evaluate('element => element === document.activeElement'))
        search.fill('undo');self.assertEqual(page.locator('#shortcut-results li').count(),1)
        self.assertIn('Ctrl/Cmd+Z',page.locator('#shortcut-results').inner_text())
        search.fill('<script>alert(1)</script>');self.assertEqual(page.locator('#shortcut-results li').count(),0)
        self.assertEqual(page.locator('#shortcut-count').inner_text(),'0 matching shortcuts')
        search.fill('');page.keyboard.press('Tab');self.assertTrue(page.locator('#shortcut-close').evaluate('element => element === document.activeElement'))
        page.keyboard.press('Tab');self.assertTrue(search.evaluate('element => element === document.activeElement'))
        page.keyboard.press('Escape');page.wait_for_function('() => !document.querySelector("#shortcut-dialog").open')
        self.assertTrue(button.evaluate('element => element === document.activeElement'))
        page.locator('#viewport').focus();page.keyboard.press('Shift+Slash');self.assertTrue(dialog.is_visible())
        page.keyboard.press('Escape');page.wait_for_function('() => !document.querySelector("#shortcut-dialog").open')
        self.assertTrue(page.locator('#viewport').evaluate('element => element === document.activeElement'))
        self.assertEqual(self.repository.snapshot()['revision'],0)

    def test_browser_modifiers_text_editing_and_nonviewport_arrows_do_not_run_scene_shortcuts(self):
        page=self.page;page.select_option('#tool','rotate')
        page.locator('[data-id="mesh"]').focus();page.keyboard.press('Enter')
        page.keyboard.press('Control+f');page.keyboard.press('Escape')
        self.assertEqual(page.locator('#tool').input_value(),'rotate')
        page.locator('[data-id="mesh"]').focus();page.keyboard.press('Enter')
        page.locator('#duplicate').focus();page.keyboard.press('ArrowRight')
        self.assertEqual(self.repository.snapshot()['revision'],0)
        # Extension-provided rich text must retain native editing keys too.
        page.evaluate('''() => {const input=document.createElement('div');input.contentEditable='true';input.id='rich-text';document.body.append(input);input.focus();}''')
        page.keyboard.press('g');self.assertEqual(page.locator('#rich-text').inner_text(),'g')
        self.assertEqual(page.locator('#tool').input_value(),'rotate')
        self.assertEqual(self.repository.snapshot()['revision'],0)
        page.get_by_label('translation X',exact=True).focus();page.keyboard.press('Control+a');page.keyboard.press('g')
        self.assertEqual(page.locator('#tool').input_value(),'rotate')

    def test_native_hidden_and_locked_rows_remain_discoverable_without_selection_or_mutation(self):
        page=self.page
        self.repository.apply(0,dict(schema_version=1,commands=[
            dict(operation='node-flags',parameters=dict(identifier='mesh',locked=True)),
            dict(operation='node-flags',parameters=dict(identifier='camera',visible=False))]),'accessibility-native-flags')
        page.goto('about:blank');page.goto(self.server.url);page.wait_for_function('() => window.editorReady === true')
        before=self.repository.snapshot()
        root=page.locator('[data-id="root"]');root.focus();page.keyboard.press('ArrowDown')
        self.assertEqual(page.evaluate('document.activeElement.dataset.id'),'mesh')
        self.assertEqual(page.locator('[data-id="mesh"]').get_attribute('aria-disabled'),'true')
        page.keyboard.press('Enter');self.assertEqual(page.evaluate('window.hatchEditor.selection'),[])
        page.keyboard.press('ArrowDown');self.assertEqual(page.evaluate('document.activeElement.dataset.id'),'camera')
        self.assertEqual(page.locator('[data-id="camera"]').get_attribute('aria-disabled'),'true')
        page.keyboard.press('Space');self.assertEqual(page.evaluate('window.hatchEditor.selection'),[])
        page.keyboard.press('Home');page.keyboard.press('Enter')
        self.assertEqual(page.evaluate('window.hatchEditor.selection'),['root'])
        page.keyboard.press('Tab')
        self.assertFalse(page.evaluate('document.querySelector("#outliner").contains(document.activeElement)'))
        self.assertEqual(self.repository.snapshot()['sha256'],before['sha256'])
        self.assertEqual(self.repository.snapshot()['revision'],1)

    def test_help_restores_native_row_identity_after_acknowledgement_or_stable_fallback_after_delete(self):
        page=self.page
        for operation,revision,expected in [('duplicate',1,'mesh'),('delete',2,None)]:
            with self.subTest(operation=operation):
                page.locator('[data-id="mesh"]').focus();page.keyboard.press('Enter')
                def delayed_ack(route):
                    response=route.fetch()
                    self.assertEqual(self.repository.snapshot()['revision'],revision)
                    page.locator('[data-id="mesh"]').focus()
                    page.evaluate('window.helpInvoker = document.activeElement')
                    page.keyboard.press('Shift+Slash')
                    self.assertTrue(page.get_by_role('dialog',name='Keyboard reference',exact=True).is_visible())
                    route.fulfill(response=response)
                page.route('**/api/commands',delayed_ack)
                try:
                    page.locator('#'+operation).focus();page.keyboard.press('Enter')
                    page.wait_for_function('revision => window.hatchEditor.snapshot.revision === revision && window.helpInvoker && !window.helpInvoker.isConnected',arg=revision)
                    page.keyboard.press('Escape')
                    page.wait_for_function('() => !document.querySelector("#shortcut-dialog").open')
                    focused=page.evaluate('({tag:document.activeElement.tagName,id:document.activeElement.id,node:document.activeElement.dataset.id})')
                    if expected:
                        self.assertEqual(focused['node'],expected)
                        self.assertEqual(page.locator('#outliner [tabindex="0"]').count(),1)
                    else:
                        self.assertEqual(focused['id'],'keyboard-help')
                    self.assertNotEqual(focused['tag'],'BODY')
                    self.assertEqual(self.repository.snapshot()['revision'],revision)
                finally:page.unroute('**/api/commands',delayed_ack)

    def test_bare_page_pending_command_reports_retry_without_losing_or_duplicating_edit(self):
        page=self.page;requests=[]
        page.locator('[data-id="mesh"]').focus();page.keyboard.press('Enter')
        def hold_command(route):
            requests.append(route.request)
            body=page.locator('body');body.evaluate("node => node.setAttribute('tabindex','-1')");body.focus()
            self.assertTrue(page.evaluate('() => document.activeElement === document.body'))
            page.keyboard.press('ArrowRight')
            self.assertIn('retry this edit',page.locator('#status').inner_text())
            self.assertTrue(page.get_by_label('translation X',exact=True).is_disabled())
            route.continue_()
        page.route('**/api/commands',hold_command)
        try:
            field=page.get_by_label('translation X',exact=True);field.fill('3');field.press('Tab')
            page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1 && document.querySelector("#status").textContent.includes("Saved revision")')
            self.assertEqual(len(requests),1)
            snapshot=self.repository.snapshot();self.assertEqual(snapshot['revision'],1)
            self.assertEqual(next(node for node in snapshot['scene']['nodes'] if node['id']=='mesh')['transform']['translation'],[3,0,0])
        finally:page.unroute('**/api/commands',hold_command)

    def install_area_fixture(self, count=1):
        snapshot=self.repository.snapshot();root=next(node for node in snapshot['scene']['nodes'] if node['id']=='root')
        commands=[]
        for index in range(count):
            area=deepcopy(root)
            area.update(id='area-'+str(index),name='<b>Area key</b>' if index==0 else 'Area '+str(index),type='light',parent='root',
                        light=dict(type='area',color=[1,1,1],intensity=10))
            commands.append(dict(operation='create',parameters=dict(node=area)))
        self.repository.apply(snapshot['revision'],dict(schema_version=1,commands=commands),'area-fixture-'+str(count))
        self.page.goto('about:blank');self.page.goto(self.server.url)
        self.page.wait_for_function('() => window.editorReady === true')

    def assert_current_preview_warning(self, revision, count=1):
        diagnostic=self.page.locator('#diagnostic')
        self.assertFalse(diagnostic.is_hidden())
        self.assertEqual(diagnostic.get_attribute('data-revision'),str(revision))
        self.assertEqual(diagnostic.get_attribute('data-sha256'),self.repository.snapshot()['sha256'])
        self.assertEqual(diagnostic.locator('[data-code="EDITOR_VIEW_LIGHT_UNAVAILABLE"]').count(),count)

    def test_native_warning_survives_save_reload_real_context_restore_and_fresh_reopen_then_recomputes_history(self):
        page=self.page;self.install_area_fixture()
        self.assert_current_preview_warning(1)
        self.assertEqual(page.locator('#diagnostic').get_attribute('role'),'status')
        self.assertIn('<b>Area key</b>',page.locator('#diagnostic').inner_text())
        self.assertEqual(page.locator('#diagnostic b').count(),0)
        page.locator('[data-id="mesh"]').click();field=page.get_by_label('translation X',exact=True)
        field.fill('3');field.press('Tab')
        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2 && document.querySelector("#status").textContent.includes("Saved revision")')
        self.assert_current_preview_warning(2)
        page.evaluate('window.previousWarningObject = window.hatchEditor.objects.get("area-0")')
        page.click('#refresh');page.wait_for_function('() => window.hatchEditor.objects.get("area-0") !== window.previousWarningObject')
        self.assert_current_preview_warning(2)
        supported=page.evaluate('''() => {window.diagnosticContextLoss=window.hatchEditor.renderer.getContext().getExtension('WEBGL_lose_context');return !!window.diagnosticContextLoss;}''')
        self.assertTrue(supported,'Actual context-loss proof requires WEBGL_lose_context; do not substitute a synthetic event.')
        page.evaluate('window.diagnosticContextLoss.loseContext()')
        page.wait_for_function('() => document.querySelector("#diagnostic").textContent.includes("WebGL context lost")')
        page.evaluate('window.diagnosticContextLoss.restoreContext()')
        page.wait_for_function('() => document.querySelector("#status").textContent.includes("WebGL restored") && window.editorReady === true')
        self.assert_current_preview_warning(2)
        page.goto('about:blank');page.goto(self.server.url);page.wait_for_function('() => window.editorReady === true')
        self.assert_current_preview_warning(2)
        page.locator('[data-id="area-0"]').click();page.click('#delete')
        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 3')
        self.assertTrue(page.locator('#diagnostic').is_hidden())
        page.click('#undo');page.wait_for_function('() => window.hatchEditor.snapshot.revision === 4')
        self.assert_current_preview_warning(4)
        page.click('#redo');page.wait_for_function('() => window.hatchEditor.snapshot.revision === 5')
        self.assertTrue(page.locator('#diagnostic').is_hidden())
        self.assertEqual(self.repository.snapshot()['revision'],5)

    def test_transient_rejection_clears_after_success_without_hiding_current_native_warning_or_erasing_history(self):
        page=self.page;self.install_area_fixture()
        page.locator('[data-id="mesh"]').click();field=page.get_by_label('translation X',exact=True)
        field.fill('1000001');field.press('Tab')
        self.assertIn('Enter a finite value',page.locator('#diagnostic').inner_text())
        self.assertEqual(page.locator('#diagnostic').get_attribute('role'),'alert')
        self.assert_current_preview_warning(1)
        self.assertEqual(self.repository.snapshot()['revision'],1)
        field.fill('3');field.press('Tab')
        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2 && document.querySelector("#status").textContent.includes("Saved revision")')
        self.assert_current_preview_warning(2)
        self.assertNotIn('Enter a finite value',page.locator('#diagnostic').inner_text())
        self.assertEqual(page.locator('#diagnostic').get_attribute('role'),'status')
        self.assertTrue(any('Enter a finite value' in error for error in page.evaluate('window.hatchEditor.errors')))

    def test_current_warning_records_are_bounded_deduplicated_and_keyboard_scrollable(self):
        page=self.page;self.install_area_fixture(70)
        self.assert_current_preview_warning(1,64)
        self.assertEqual(page.locator('#diagnostic [data-code="EDITOR_VIEW_DIAGNOSTIC_LIMIT"]').count(),1)
        page.locator('#diagnostic').focus();page.keyboard.press('End')
        self.assertGreater(page.locator('#diagnostic').evaluate('element => element.scrollTop'),0)
        page.evaluate('window.previousWarningObject = window.hatchEditor.objects.get("area-0")')
        page.click('#refresh');page.wait_for_function('() => window.hatchEditor.objects.get("area-0") !== window.previousWarningObject')
        self.assert_current_preview_warning(1,64)
        self.assertEqual(page.locator('#diagnostic [data-code="EDITOR_VIEW_DIAGNOSTIC_LIMIT"]').count(),1)
