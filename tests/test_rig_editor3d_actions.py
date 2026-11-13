"""Actual Chromium acceptance for the shared extension action lifecycle."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual editor browser acceptance explicitly.')
class EditorActionBrowserTests(unittest.TestCase):
    def test_focused_edits_pending_saves_failure_and_exclusive_action(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with TemporaryDirectory() as directory:
            repository = EditorRepository.create(ROOT / 'examples/scene3d-contracts', Path(directory) / 'saved')
            server = EditorServer(repository)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with sync_playwright() as runtime:
                    browser = launch_chromium(runtime)
                    try:
                        page = browser.new_page(viewport={'width': 1400, 'height': 900})
                        page.goto(server.url); page.wait_for_function('() => window.editorReady === true')
                        page.evaluate('''() => hatchEditor.registerExtension(context => {
                          window.actionContext=context; return {};
                        }, document.createElement('div'))''')
                        page.locator('[data-id="mesh"]').click()
                        x = page.get_by_label('translation X', exact=True)
                        x.fill('3')
                        value = page.evaluate('''async () => actionContext.runGuardedAction(async context => {
                          const snapshot = context.snapshot;
                          await context.reload();
                          return snapshot.scene.nodes.find(n=>n.id==='mesh').transform.translation[0];
                        })''')
                        self.assertEqual(value, 3)
                        self.assertEqual(repository.snapshot()['scene']['nodes'][1]['transform']['translation'][0], 3)
                        # An already pending ordinary field transaction is awaited before invocation.
                        pending = []
                        page.route('**/api/commands', lambda route: pending.append(route))
                        x.fill('4'); x.press('Tab'); page.wait_for_timeout(60)
                        self.assertEqual(len(pending), 1)
                        page.evaluate('''() => { window.guardResult=null; window.guardCalled=false;
                          actionContext.runGuardedAction(async c=>{window.guardCalled=true;return c.snapshot.revision;})
                          .then(v=>window.guardResult={ok:true,value:v},e=>window.guardResult={ok:false,error:e.message}); }''')
                        self.assertFalse(page.evaluate('() => window.guardCalled'))
                        pending.pop().continue_(); page.unroute('**/api/commands')
                        page.wait_for_function('() => window.guardResult !== null')
                        self.assertTrue(page.evaluate('() => window.guardResult.ok'))
                        # Failed saves never run the action against the previous revision.
                        page.route('**/api/commands', lambda route: route.fulfill(status=409, content_type='application/json', body='{"error":{"message":"conflicting save"}}'))
                        x.fill('5'); x.press('Tab')
                        page.evaluate('''() => { window.guardResult=null; window.guardCalled=false;
                          actionContext.runGuardedAction(()=>{window.guardCalled=true;}).then(
                            ()=>window.guardResult={ok:true},e=>window.guardResult={ok:false,error:e.message}); }''')
                        page.wait_for_function('() => window.guardResult !== null')
                        self.assertFalse(page.evaluate('() => window.guardResult.ok'))
                        self.assertFalse(page.evaluate('() => window.guardCalled'))
                        page.unroute('**/api/commands')
                        x.fill('6'); x.press('Tab')
                        page.wait_for_function("() => window.hatchEditor.snapshot.scene.nodes.find(n=>n.id==='mesh').transform.translation[0] === 6")
                        # The busy lock spans the callback and reload, including error cleanup.
                        page.evaluate('''() => { window.releaseGuard=null; window.guardResult=null;
                          actionContext.runGuardedAction(async c=>{
                            await new Promise(resolve=>window.releaseGuard=resolve); await c.reload(); throw Error('consumer failed');
                          }).then(()=>window.guardResult={ok:true},e=>window.guardResult={ok:false,error:e.message}); }''')
                        self.assertTrue(x.is_disabled())
                        for operation in ('reload', 'clearPreview'):
                            rejected = page.evaluate("""async operation => {try{await actionContext[operation]();return '';}catch(e){return e.message;}}""", operation)
                            self.assertIn('in progress', rejected)
                        api_rejection = page.evaluate("""async () => {try{await actionContext.api('snapshot');return '';}catch(e){return e.message;}}""")
                        self.assertIn('in progress', api_rejection)
                        rejection = page.evaluate("async()=>{try{await actionContext.runGuardedAction(()=>{});return '';}catch(e){return e.message;}}")
                        self.assertIn('in progress', rejection)
                        page.evaluate('() => window.releaseGuard()')
                        page.wait_for_function('() => window.guardResult !== null')
                        self.assertIn('consumer failed', page.evaluate('() => window.guardResult.error'))
                        self.assertFalse(x.is_disabled())
                        # Invalid focused values and active native interactions do not publish.
                        unchanged = repository.snapshot()['revision']
                        x.fill('2000000')
                        rejected = page.evaluate("async()=>{try{await actionContext.runGuardedAction(()=>{});return '';}catch(e){return e.message;}}")
                        self.assertIn('rejected', rejected)
                        self.assertEqual(repository.snapshot()['revision'], unchanged)
                        page.evaluate("() => hatchEditor.gizmo.dispatchEvent({type:'dragging-changed',value:true})")
                        rejected = page.evaluate("async()=>{try{await actionContext.runGuardedAction(()=>{});return '';}catch(e){return e.message;}}")
                        self.assertIn('active viewport interaction', rejected)
                        page.keyboard.press('Escape')
                        page.evaluate("() => actionContext.preview({nodes:hatchEditor.snapshot.viewport_state.nodes})")
                        rejected = page.evaluate("async()=>{try{await actionContext.runGuardedAction(()=>{});return '';}catch(e){return e.message;}}")
                        self.assertIn('active viewport interaction', rejected)
                        page.evaluate('() => actionContext.clearPreview()')
                        # Loss followed by restoration cannot resurrect an in-flight action.
                        page.evaluate('''() => { window.releaseGuard=null; window.guardResult=null;
                          actionContext.runGuardedAction(async c=>{
                            await new Promise(resolve=>window.releaseGuard=resolve); await c.reload();
                          }).then(()=>window.guardResult={ok:true},e=>window.guardResult={ok:false,error:e.message}); }''')
                        page.evaluate('''() => { const canvas=hatchEditor.renderer.domElement;
                          canvas.dispatchEvent(new Event('webglcontextlost',{cancelable:true}));
                          canvas.dispatchEvent(new Event('webglcontextrestored')); }''')
                        page.wait_for_function('() => window.editorReady === true')
                        page.evaluate('() => window.releaseGuard()')
                        page.wait_for_function('() => window.guardResult !== null')
                        self.assertFalse(page.evaluate('() => window.guardResult.ok'))
                        # Disposal rejects a pending consumer request and releases the lock.
                        page.evaluate('''() => { window.releaseGuard=null; window.guardResult=null;
                          actionContext.runGuardedAction(async c=>{
                            await new Promise(resolve=>window.releaseGuard=resolve); await c.api('snapshot');
                          }).then(()=>window.guardResult={ok:true},e=>window.guardResult={ok:false,error:e.message}); }''')
                        page.evaluate("() => {window.dispatchEvent(new Event('pagehide'));window.releaseGuard();}")
                        page.wait_for_function('() => window.guardResult !== null')
                        self.assertFalse(page.evaluate('() => window.guardResult.ok'))
                    finally:
                        browser.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)
