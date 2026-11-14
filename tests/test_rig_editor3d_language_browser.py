"""Real browser language authoring through shared guarded native transactions."""
import os
from pathlib import Path
import tempfile
import threading
import unittest

from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer
from scene3d_example import build_example


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1',
                     'Enable actual editor browser acceptance explicitly.')
class EditorLanguageBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        build_example(self.root / 'source')
        self.repository = EditorRepository.create(self.root / 'source', self.root / 'saved', scene_path='example-scene.json')
        self.server = EditorServer(self.repository)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_plan_flushes_pending_edit_and_applies_real_reopened_scene_geometry(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with sync_playwright() as runtime:
            browser = launch_chromium(runtime)
            try:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.goto(self.server.url)
                page.wait_for_function('() => window.editorReady === true')
                text = page.get_by_label('Supported scene instructions', exact=True)
                text.fill('move mesh 2 meters east')
                page.locator('[data-id="mesh"]').click()
                page.get_by_label('translation X', exact=True).fill('7')
                # Programmatic click leaves the inspector field focused; the one
                # shared action guard must flush that actual pending edit.
                page.get_by_role('button', name='Review language plan', exact=True).evaluate('(button) => button.click()')
                apply = page.get_by_role('button', name='Apply reviewed language plan', exact=True)
                page.wait_for_function("() => ![...document.querySelectorAll('button')].find(b=>b.textContent==='Apply reviewed language plan').disabled")
                self.assertEqual(self.repository.snapshot()['revision'], 1)
                mesh = next(n for n in self.repository.snapshot()['scene']['nodes'] if n['id'] == 'mesh')
                self.assertEqual(mesh['transform']['translation'][0], 7)
                apply.click()
                page.wait_for_function("() => hatchEditor.snapshot.revision === 2")
                reopened = EditorRepository(self.root / 'saved').snapshot()
                self.assertEqual(reopened['revision'], 2)
                self.assertEqual(next(n for n in reopened['scene']['nodes'] if n['id'] == 'mesh')['transform']['translation'][0], 9)
                self.assertAlmostEqual(page.evaluate("() => hatchEditor.objects.get('mesh').matrixWorld.elements[12]"), 9)
                self.assertTrue(apply.is_disabled())
                # Credentials are intentionally memory-only: reopen the saved
                # scene in a fresh authenticated page, not an unauthenticated URL.
                page.close()
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.goto(self.server.url)
                page.wait_for_function("() => window.editorReady === true && hatchEditor.snapshot.revision === 2")
                self.assertAlmostEqual(page.evaluate("() => hatchEditor.objects.get('mesh').matrixWorld.elements[12]"), 9)
                self.assertEqual(self.repository.snapshot()['sha256'], reopened['sha256'])
            finally:
                browser.close()

    def test_unsupported_description_and_external_stale_edit_cannot_publish(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with sync_playwright() as runtime:
            browser = launch_chromium(runtime)
            try:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.goto(self.server.url)
                page.wait_for_function('() => window.editorReady === true')
                text = page.get_by_label('Supported scene instructions', exact=True)
                text.fill('summon a dragon')
                page.get_by_role('button', name='Review language plan', exact=True).click()
                page.wait_for_function("() => document.querySelector('#scene-language [role=status]').textContent.includes('Resolve')")
                apply = page.get_by_role('button', name='Apply reviewed language plan', exact=True)
                self.assertTrue(apply.is_disabled())
                self.assertEqual(self.repository.snapshot()['revision'], 0)
                text.fill('hide mesh')
                page.get_by_role('button', name='Review language plan', exact=True).click()
                page.wait_for_function("() => ![...document.querySelectorAll('button')].find(b=>b.textContent==='Apply reviewed language plan').disabled")
                saved = self.repository.snapshot()
                self.repository.apply(saved['revision'], dict(schema_version=1, commands=[dict(operation='rename',
                    parameters=dict(identifier='mesh', name='Externally edited'))]), 'outside-language', expected_sha256=saved['sha256'])
                apply.click()
                page.wait_for_function("() => document.querySelector('#scene-language [role=status]').textContent.includes('current revision')")
                self.assertEqual(self.repository.snapshot()['revision'], 1)
                self.assertTrue(next(n for n in self.repository.snapshot()['scene']['nodes'] if n['id'] == 'mesh')['visible'])
                page.evaluate('() => hatchEditor.reload()')
                self.assertTrue(apply.is_disabled())
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
