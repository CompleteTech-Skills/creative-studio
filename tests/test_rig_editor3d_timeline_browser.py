"""Actual authenticated browser autokey and cross-scene preview regressions."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest

from editor3d_repository import EditorRepository
from editor3d_timeline import apply_timeline, routes
from editor3d_viewer import EditorServer
from scene3d_contracts import canonical_bytes
from scene3d_example import build_example
from scene3d_snapshot import snapshot_project


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST')=='1','Enable actual browser validation.')
class TimelineBrowserTests(unittest.TestCase):
    def test_autokey_second_shot_is_one_durable_transaction_and_foreign_scene_rejected(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';bundle=build_example(source)
            scene=deepcopy(bundle['documents']['example-scene.json']);scene.update(id='foreign-scene',shots=['foreign-shot'])
            scene_path='foreign-scene.json';scene_ref=dict(kind=scene['kind'],id=scene['id'],version=scene['version'],path=scene_path,sha256=hashlib.sha256(canonical_bytes(scene)).hexdigest())
            shot=deepcopy(bundle['documents']['example-shot.json']);shot.update(id='foreign-shot',name='Foreign',scene=scene_ref)
            snapshot_project(source,root/'with-foreign',derived_documents={scene_path:scene,'foreign-shot.json':shot})
            repo=EditorRepository.create(root/'with-foreign',root/'saved')
            before=repo.snapshot()
            apply_timeline(repo,dict(expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='second',commands=[dict(operation='shot-duplicate',source='example-shot.json',id='second',name='Second')]))
            baseline=repo.snapshot()['revision']
            server=EditorServer(repo,routes=routes());thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as runtime:
                    browser=launch_chromium(runtime)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1100});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(server.url);page.wait_for_function('() => window.editorReady===true')
                        page.get_by_label('Timeline shot',exact=True).select_option(label='Second')
                        page.locator('[data-id="mesh"]').click()
                        page.get_by_label('Exact frame',exact=True).fill('12')
                        page.get_by_label('Autokey transforms at selected frame',exact=True).check()
                        page.get_by_label('translation X',exact=True).fill('7');page.get_by_label('translation X',exact=True).press('Tab')
                        page.wait_for_function('(revision)=>window.hatchEditor.snapshot.revision===revision',arg=baseline+1)
                        saved=repo.snapshot();selected=next(ref for ref in saved['project']['shots'] if ref['id']=='second')
                        self.assertEqual(page.get_by_label('Timeline shot',exact=True).input_value(),selected['path'])
                        self.assertEqual(next(node for node in saved['scene']['nodes'] if node['id']=='mesh')['transform']['translation'],[2,0,0])
                        tracks=saved['documents'][selected['path']]['tracks'];self.assertEqual(len(tracks),3)
                        self.assertEqual(next(t for t in tracks if t['property']=='translation')['keys'],[dict(frame=12,value=[7,0,0])])
                        self.assertEqual(len(repo.history()),baseline+2)
                        page.get_by_role('button',name='Scrub',exact=True).click()
                        page.wait_for_function("()=>document.querySelector('[aria-label=\"Animation timeline\"] [role=status]').textContent.includes('Saved revision')")
                        self.assertEqual(page.evaluate("()=>window.hatchEditor.objects.get('mesh').position.x"),7)
                        page.get_by_label('Timeline shot',exact=True).select_option(label='Foreign')
                        page.get_by_role('button',name='Scrub',exact=True).click()
                        page.wait_for_function("()=>document.querySelector('[aria-label=\"Animation timeline\"] [role=status]').textContent.includes('Switch to the shot scene')")
                        self.assertEqual(page.evaluate("()=>window.hatchEditor.objects.get('mesh').position.x"),2)
                        self.assertEqual(repo.snapshot()['revision'],baseline+1)
                        self.assertEqual(errors,[])
                        # Reopening the persisted repository retains only the selected second-shot keys.
                        reopened=EditorRepository(repo.root).snapshot()
                        self.assertEqual(reopened['documents'][selected['path']]['tracks'],tracks)
                        page.get_by_label('Timeline shot',exact=True).select_option(label='Second')
                        page.get_by_label('Animated property',exact=True).select_option('world_exposure')
                        page.get_by_label('Key value as JSON',exact=True).fill('1024')
                        page.get_by_role('button',name='Insert or replace key',exact=True).click()
                        page.wait_for_function('(revision)=>window.hatchEditor.snapshot.revision===revision',arg=baseline+2)
                        page.get_by_role('button',name='Scrub',exact=True).click()
                        page.wait_for_function("()=>document.querySelector('[aria-label=\"Animation timeline\"] [role=status]').textContent.includes('-20 to +20 stops')")
                        self.assertEqual(page.evaluate('()=>window.hatchEditor.renderer.toneMappingExposure'),1)

                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(timeout=5)

if __name__=='__main__':unittest.main()
