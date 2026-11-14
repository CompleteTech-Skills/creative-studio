"""Actual authenticated browser autokey and cross-scene preview regressions."""
from copy import deepcopy
import hashlib
import json
import subprocess
import os
from pathlib import Path
import tempfile
import threading
import time
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
        project_root=Path(__file__).resolve().parents[1]
        metadata=dict(head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=project_root,text=True).strip(),
            tree=subprocess.check_output(['git','rev-parse','HEAD^{tree}'],cwd=project_root,text=True).strip(),
            files={name:hashlib.sha256((project_root/name).read_bytes()).hexdigest() for name in
                ('assets/editor3d/editor.js','assets/editor3d/timeline.js','scripts/editor3d_timeline.py','scripts/editor3d_viewer.py','tests/test_rig_editor3d_timeline_browser.py')})
        print('TIMELINE_BROWSER_SOURCE '+json.dumps(metadata,sort_keys=True),flush=True)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';bundle=build_example(source)
            scene=deepcopy(bundle['documents']['example-scene.json']);scene.update(id='foreign-scene',name='Foreign scene',shots=['foreign-shot'])
            scene_path='foreign-scene.json';scene_ref=dict(kind=scene['kind'],id=scene['id'],version=scene['version'],path=scene_path,sha256=hashlib.sha256(canonical_bytes(scene)).hexdigest())
            shot=deepcopy(bundle['documents']['example-shot.json']);shot.update(id='foreign-shot',name='Foreign',scene=scene_ref)
            snapshot_project(source,root/'with-foreign',derived_documents={scene_path:scene,'foreign-shot.json':shot})
            repo=EditorRepository.create(root/'with-foreign',root/'saved')
            before=repo.snapshot()
            apply_timeline(repo,dict(expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='second',commands=[dict(operation='shot-duplicate',source='example-shot.json',id='second',name='Second')]))
            baseline=repo.snapshot()['revision']
            handlers=routes();curve_handler=handlers['timeline/curve']
            def delayed_curve(server,body):
                time.sleep(.3)
                return curve_handler(server,body)
            handlers['timeline/curve']=delayed_curve
            server=EditorServer(repo,routes=handlers);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
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
                        # Edit the evaluated pose through a real enabled numeric control.
                        self.assertTrue(page.get_by_label('translation X',exact=True).is_enabled())
                        page.get_by_label('Exact frame',exact=True).fill('13')
                        self.assertFalse(page.get_by_label('translation X',exact=True).is_enabled())
                        page.get_by_role('button',name='Scrub',exact=True).click()
                        page.wait_for_function("()=>!document.querySelector('[aria-label=\"translation X\"]').disabled")
                        page.get_by_label('translation X',exact=True).fill('9');page.get_by_label('translation X',exact=True).press('Tab')
                        page.wait_for_function('(revision)=>window.hatchEditor.snapshot.revision===revision',arg=baseline+2)
                        page.wait_for_function("()=>window.hatchEditor.objects.get('mesh').position.x===9")
                        saved=repo.snapshot();selected=next(ref for ref in saved['project']['shots'] if ref['id']=='second')
                        tracks=saved['documents'][selected['path']]['tracks']
                        self.assertEqual(next(t for t in tracks if t['property']=='translation')['keys'],[dict(frame=12,value=[7,0,0]),dict(frame=13,value=[9,0,0])])
                        self.assertEqual(next(node for node in saved['scene']['nodes'] if node['id']=='mesh')['transform']['translation'],[2,0,0])
                        context=page.evaluate('()=>window.hatchEditor.getTimelineContext()')
                        self.assertEqual(context['revision'],baseline+2);self.assertEqual(context['sha256'],saved['sha256'])
                        self.assertEqual(context['shot_path'],selected['path']);self.assertEqual(context['frame'],dict(numerator=13,denominator=1))
                        # An independent async extension cannot move a pending edit to another frame.
                        page.evaluate('''()=>{window.__removeTimelineRace=window.hatchEditor.registerCommitInterceptor(async()=>{
                          await new Promise(resolve=>setTimeout(resolve,100));
                          const input=document.querySelector('[aria-label="Exact frame"]');input.value='14';
                          input.dispatchEvent(new Event('input',{bubbles:true}));return null;
                        });}''')
                        page.get_by_label('translation X',exact=True).fill('11');page.get_by_label('translation X',exact=True).press('Tab')
                        page.wait_for_function("()=>window.hatchEditor.errors.some(text=>text.includes('selected timeline or evaluated preview changed'))&&window.hatchEditor.objects.get('mesh').position.x===9")
                        page.evaluate('()=>{window.__removeTimelineRace();delete window.__removeTimelineRace;}')
                        page.wait_for_function("()=>!document.querySelector('[aria-label=\"translation X\"]').disabled")
                        self.assertEqual(repo.snapshot()['revision'],baseline+2)
                        self.assertEqual(repo.snapshot()['documents'][selected['path']]['tracks'],tracks)
                        with page.expect_response('**/api/timeline/curve'):
                            page.get_by_role('button',name='Evaluate channel curve',exact=True).click()
                            page.get_by_label('Animated property',exact=True).select_option('rotation')
                        page.wait_for_timeout(100)
                        self.assertEqual(page.locator('[aria-label=\"Evaluated channel curve\"] polyline').count(),0)
                        page.get_by_label('Animated property',exact=True).select_option('translation')
                        page.get_by_role('button',name='Evaluate channel curve',exact=True).click()
                        page.wait_for_function("()=>document.querySelectorAll('[aria-label=\"Evaluated channel curve\"] polyline').length===3")

                        page.get_by_label('Timeline shot',exact=True).select_option(label='Foreign')
                        self.assertIsNone(page.evaluate('()=>window.hatchEditor.getTimelineContext()'))
                        page.get_by_role('button',name='Scrub',exact=True).click()
                        page.wait_for_function("()=>document.querySelector('[aria-label=\"Animation timeline\"] [role=status]').textContent.includes('Switch to the shot scene')")
                        self.assertEqual(page.evaluate("()=>window.hatchEditor.objects.get('mesh').position.x"),2)
                        self.assertEqual(repo.snapshot()['revision'],baseline+2)
                        self.assertEqual(errors,[])
                        # Reopening the persisted repository retains only the selected second-shot keys.
                        reopened=EditorRepository(repo.root).snapshot()
                        self.assertEqual(reopened['documents'][selected['path']]['tracks'],tracks)
                        page.get_by_label('Timeline shot',exact=True).select_option(label='Second')
                        page.get_by_label('Animated property',exact=True).select_option('world_exposure')
                        page.get_by_label('Key value as JSON',exact=True).fill('1024')
                        page.get_by_role('button',name='Insert or replace key',exact=True).click()
                        page.wait_for_function('(revision)=>window.hatchEditor.snapshot.revision===revision',arg=baseline+3)
                        page.get_by_role('button',name='Scrub',exact=True).click()
                        page.wait_for_function("()=>document.querySelector('[aria-label=\"Animation timeline\"] [role=status]').textContent.includes('-20 to +20 stops')")
                        self.assertEqual(page.evaluate('()=>window.hatchEditor.renderer.toneMappingExposure'),1)
                        before_open=repo.snapshot()
                        page.get_by_label('Project scene',exact=True).select_option(label='Foreign scene')
                        page.get_by_role('button',name='Open scene',exact=True).click()
                        page.wait_for_function('(revision)=>window.hatchEditor.snapshot.revision===revision',arg=baseline+4)
                        self.assertEqual(page.evaluate('()=>window.hatchEditor.snapshot.scene.id'),'foreign-scene')
                        self.assertEqual(repo.snapshot()['sha256'],before_open['sha256'])
                        self.assertEqual(EditorRepository(repo.root).snapshot()['scene']['id'],'foreign-scene')
                        page.click('#undo');page.wait_for_function('(revision)=>window.hatchEditor.snapshot.revision===revision',arg=baseline+5)
                        self.assertEqual(page.evaluate('()=>window.hatchEditor.snapshot.scene.id'),'example-scene')
                        page.wait_for_function("()=>!document.querySelector('[aria-label=\"translation X\"]').disabled&&window.hatchEditor.objects.get('mesh').position.x===2")
                        print('TIMELINE_BROWSER_ACCEPTANCE '+json.dumps(dict(head=metadata['head'],status='passed',revision=repo.snapshot()['revision'],snapshot_sha256=repo.snapshot()['sha256']),sort_keys=True),flush=True)


                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_playback_survives_same_revision_refresh_and_pause_discards_pending_preview(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);build_example(root/'source')
            repo=EditorRepository.create(root/'source',root/'saved');before=repo.snapshot()
            apply_timeline(repo,dict(expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='playback',commands=[
                dict(operation='key-upsert',document='example-shot.json',target='mesh',property='translation',frame=0,value=[2,0,0]),
                dict(operation='key-upsert',document='example-shot.json',target='mesh',property='translation',frame=24,value=[26,0,0])]))
            server=EditorServer(repo,routes=routes());thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as runtime:
                    browser=launch_chromium(runtime)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1100});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(server.url);page.wait_for_function('() => window.editorReady===true')
                        page.locator('[data-id="mesh"]').click()
                        timeline=page.locator('[aria-label="Animation timeline"]')
                        timeline.get_by_role('button',name='Play',exact=True).click()
                        page.wait_for_function("() => Number(document.querySelector('[aria-label=\"Exact frame\"]').value)>=2 && hatchEditor.objects.get('mesh').position.x>2")
                        self.assertEqual(timeline.get_by_role('button',name='Pause',exact=True).count(),1)
                        pending=[]
                        page.route('**/api/timeline/evaluate',lambda route:pending.append(route))
                        for _ in range(100):
                            if pending:break
                            page.wait_for_timeout(20)
                        self.assertEqual(len(pending),1)
                        timeline.get_by_role('button',name='Pause',exact=True).click()
                        paused=page.evaluate("() => hatchEditor.objects.get('mesh').position.x")
                        pending.pop().continue_();page.unroute('**/api/timeline/evaluate')
                        page.wait_for_timeout(150)
                        self.assertEqual(page.evaluate("() => hatchEditor.objects.get('mesh').position.x"),paused)
                        self.assertEqual(timeline.get_by_role('button',name='Play',exact=True).count(),1)
                        self.assertEqual(repo.snapshot()['revision'],before['revision']+1)
                        self.assertEqual(errors,[])
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(timeout=5)

if __name__=='__main__':unittest.main()
