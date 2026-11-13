"""Real canonical payload/API checks and opt-in rendered browser acceptance."""
from copy import deepcopy
import hashlib
import http.client
import json
import os
import socket
import time
from pathlib import Path
import threading
import unittest

from asset_library import file_record
from asset_library_contracts import seal
from asset_library_browser import AssetBrowserServer
from asset_library_search import SearchIndex
import test_asset_library as foundation_fixture


class BrowserTests(unittest.TestCase):
    def setUp(self):
        fixture=foundation_fixture.AssetLibraryTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture=fixture; self.root=fixture.root; self.library=fixture.library
        self.reference=fixture.register()
        from asset_library_versions import AssetVersions
        self.versions=AssetVersions(self.library)
        self.index=SearchIndex(self.root/'index', {'local':self.library}, create=True,versions={'local':self.versions})
        self.index.rebuild()
        self.server=AssetBrowserServer({'local':self.library},self.index,token='test-asset-browser-token-0123456789')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.stop)
        self.location=dict(library='local',reference=self.reference)

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def request(self,path,body=None,*,auth=True,origin=None,method='POST',raw=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        headers={'Content-Type':'application/json'}
        if auth:headers['Authorization']='Bearer '+self.server.token
        if origin:headers['Origin']=origin
        connection.request(method,path,raw if raw is not None else json.dumps(body or {}),headers)
        response=connection.getresponse();data=response.read();status=response.status;headers=dict(response.getheaders());connection.close()
        return status,json.loads(data) if headers['Content-Type']=='application/json' else data,headers

    def test_real_search_inspection_and_annotation_cas(self):
        status,data,_=self.request('/api/search',{'query':'triangle','limit':1});self.assertEqual(status,200,data)
        self.assertEqual(data['results'][0]['location'],self.location)
        status,data,_=self.request('/api/inspect',self.location);self.assertEqual(status,200,data)
        self.assertEqual(data['manifest'],self.fixture.manifest);self.assertEqual(data['managed_operations'],[])
        update=dict(location=self.location,expected_revision=0,tags=['repair'],favorite=True,note='Edited exact version')
        status,data,_=self.request('/api/annotations',update);self.assertEqual(status,200,data);self.assertTrue(data['index_current'])
        self.assertEqual(self.library.annotations(self.reference)['revision'],1)
        self.assertEqual(self.request('/api/annotations',update)[0],409)
        self.assertEqual(self.request('/api/search',{'filters':{'tags':['repair']}})[1]['total'],1)

    def test_auth_origins_routes_payload_and_path_boundaries(self):
        self.assertEqual(self.request('/api/search',auth=False)[0],401)
        self.assertEqual(self.request('/api/search',origin='https://evil.invalid')[0],403)
        self.assertEqual(self.request('/api/search?leak=1')[0],400)
        self.assertEqual(self.request('/api/search',raw='x'*65537)[0],400)
        self.assertEqual(self.request('/api/search',{'raw_sql':'SELECT *'})[0],400)
        self.assertEqual(self.request('/api/inspect',dict(library='../outside',reference=self.reference))[0],400)
        self.assertEqual(self.request('/api/model',dict(location=self.location,path='../private'))[0],400)
        self.assertEqual(self.request('/api/actions/export',{})[0],400)
        self.assertEqual(self.request('/../../scripts/asset_library.py',method='GET')[0],404)
        status,data,headers=self.request('/',method='GET',auth=False)
        self.assertEqual(status,200);self.assertIn("default-src 'none'",headers['Content-Security-Policy'])
        self.assertNotIn(self.server.token.encode(),data)

    def test_untrusted_names_are_only_text_and_no_unlisted_asset_files(self):
        manifest=deepcopy(self.fixture.manifest);manifest['version']='2.0.0';manifest['identity']['name']='<img src=x onerror=alert(1)>'
        reference=self.library.register(seal(manifest),self.fixture.sources);self.index.rebuild()
        self.assertEqual(self.request('/api/search')[0],200)
        self.assertEqual(self.request('/api/inspect',dict(library='local',reference=reference))[1]['manifest']['identity']['name'],manifest['identity']['name'])
        for route in ('/library.sqlite3','/vendor/../scripts/asset_library.py','/assets/private.glb'):
            self.assertEqual(self.request(route,method='GET')[0],404)

    def test_incomplete_headers_are_bounded_before_handler_threads(self):
        sockets=[]
        try:
            for _ in range(8):
                connection=socket.create_connection(('127.0.0.1',self.server.server_port),timeout=2)
                connection.sendall(b'GET / HTTP/1.1\r\n')
                sockets.append(connection)
            deadline=time.monotonic()+2
            while self.server.connection_budget._value and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(self.server.connection_budget._value,0)
            extra=socket.create_connection(('127.0.0.1',self.server.server_port),timeout=2)
            self.addCleanup(extra.close)
            self.assertIn(b'503 Service Unavailable',extra.recv(4096))
            self.assertEqual(self.server.connection_budget._value,0)
        finally:
            for connection in sockets:connection.close()

    def test_managed_jobs_use_only_injected_native_authority(self):
        class Operations:
            def submit(self,operation,request):return {'id':'native-job','operation':operation,'request':request}
            def list_jobs(self):return [{'id':'native-job'}]
            def job_status(self,identifier):return {'id':identifier,'status':'queued'}
            def cancel_job(self,identifier):return {'id':identifier,'status':'cancelled'}
        self.server.operations=Operations()
        self.assertEqual(self.request('/api/actions/export',{'reference':self.reference})[1]['operation'],'export')
        self.assertEqual(self.request('/api/jobs/list')[1],[{'id':'native-job'}])
        self.assertEqual(self.request('/api/jobs/status',{'id':'native-job'})[1]['status'],'queued')
        self.assertEqual(self.request('/api/jobs/cancel',{'id':'native-job'})[1]['status'],'cancelled')
        self.assertEqual(self.request('/api/jobs/status',{'id':'../private'})[0],400)

    def animated_asset(self):
        from rig_test_helpers import fixture
        from rig_gltf import export_gltf
        rig=fixture(self.root,name='browser-animated')
        source=self.root/'animated.glb';export_gltf(rig,source)
        manifest=deepcopy(self.fixture.manifest);manifest['identity']['id']='animated';manifest['identity']['slug']='animated';manifest['identity']['name']='Animated fixture'
        geometry=file_record(source,'runtime/asset.glb','geometry')
        manifest['files']=[geometry,self.fixture.files[1]];manifest['provenance']['sources']=[geometry]
        manifest['publication']['inventory']=['runtime/asset.glb',self.fixture.files[1]['path']]
        manifest['compatibility']['formats']=['glb']
        ref=self.library.register(seal(manifest),{'runtime/asset.glb':source,self.fixture.files[1]['path']:self.fixture.license})
        self.native_rig=rig
        self.index.rebuild();return ref

    @unittest.skipUnless(os.environ.get('HATCH_ASSET_BROWSER_TEST')=='1','Enable real Chromium/WebGL asset acceptance.')
    def test_real_browser_model_animation_metadata_keyboard_and_webgl_fallback(self):
        from playwright.sync_api import sync_playwright
        ref=self.animated_asset(); location=dict(library='local',reference=ref)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            self.addCleanup(browser.close)
            page=browser.new_page(viewport={'width':1200,'height':1100})
            external=[]; page.on('request',lambda r: external.append(r.url) if not r.url.startswith(f'http://127.0.0.1:{self.server.server_port}/') and not r.url.startswith(('blob:','data:')) else None)
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            page.locator('#token').fill(self.server.token);page.get_by_role('button',name='Connect',exact=True).click()
            page.wait_for_function('window.assetBrowserReady === true')
            page.get_by_role('button',name='Animated fixture · 1.0.0',exact=True).click()
            page.get_by_role('button',name='Load model',exact=True).click();page.wait_for_function('window.assetBrowserModelReady === true')
            before=page.evaluate('assetBrowser.sample(0);assetBrowser.snapshot()')
            after=page.evaluate('assetBrowser.sample(.5);assetBrowser.snapshot()')
            self.assertNotEqual(before,after)
            import numpy as np
            from rig_animation import evaluate_animation
            from rig_skinning import evaluate_deformation
            expected=evaluate_deformation(self.native_rig,evaluate_animation(self.native_rig,'bend',.5)['pose'])
            points=next(iter(expected.values()))['positions'][:4]
            self.assertLess(float(np.max(np.abs(np.asarray(after)-points))),1e-5)
            self.assertIn('b1',page.locator('#model-details').inner_text())
            page.locator('#skeleton').check();page.locator('#wireframe').check()
            page.locator('#clip').select_option('bend');page.locator('#time').fill('0.25');page.locator('#time').dispatch_event('input')
            camera=page.evaluate('assetBrowser.camera.position.toArray()');page.locator('#viewport').focus();page.keyboard.press('ArrowLeft')
            self.assertNotEqual(camera,page.evaluate('assetBrowser.camera.position.toArray()'))
            page.locator('#note').fill('Real browser annotation');page.get_by_role('button',name='Save annotations').click();page.wait_for_function("document.querySelector('#error').textContent==='Annotations saved.'")
            self.assertEqual(self.library.annotations(ref)['note'],'Real browser annotation')
            self.assertEqual(external,[])
            # Hold a completed A-save response while the user selects B. Its
            # annotation revision and note must never be attached to B's form.
            pending=[]
            def hold(route):
                response=route.fetch()
                pending.append((route,response))
            page.route('**/api/annotations',hold)
            page.locator('#note').fill('Save A while switching')
            page.get_by_role('button',name='Save annotations').click()
            page.wait_for_function("document.querySelector('#save').disabled")
            page.get_by_role('button',name='Triangle · 1.0.0',exact=True).click()
            page.wait_for_function("assetBrowserSelected.reference.id==='triangle'")
            self.assertTrue(pending)
            pending[0][0].fulfill(response=pending[0][1])
            page.wait_for_timeout(100)
            self.assertEqual(page.locator('#note').input_value(),'')
            page.unroute('**/api/annotations',hold)
            page.locator('#note').fill('Separate B repair')
            page.get_by_role('button',name='Save annotations').click()
            page.wait_for_function("document.querySelector('#error').textContent==='Annotations saved.'")
            self.assertEqual(self.library.annotations(self.reference)['note'],'Separate B repair')
            self.assertEqual(self.library.annotations(ref)['note'],'Save A while switching')
            fallback=browser.new_context();fallback.add_init_script("const old=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(k,...a){return k.includes('webgl')?null:old.call(this,k,...a)}")
            fpage=fallback.new_page();fpage.goto(f'http://127.0.0.1:{self.server.server_port}/');fpage.locator('#token').fill(self.server.token);fpage.get_by_role('button',name='Connect',exact=True).click();fpage.wait_for_function('window.assetBrowserReady === true');fpage.get_by_role('button',name='Animated fixture · 1.0.0',exact=True).click()
            self.assertIn('WebGL unavailable',fpage.locator('#model-status').inner_text());fpage.locator('#note').fill('Fallback repair');fpage.get_by_role('button',name='Save annotations').click();fpage.wait_for_function("document.querySelector('#error').textContent==='Annotations saved.'")
            self.assertEqual(self.library.annotations(ref)['note'],'Fallback repair');fallback.close()
