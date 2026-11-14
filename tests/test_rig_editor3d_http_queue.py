"""Real HTTP queue/worker bounds, cancelled socket ownership and browser recovery."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer

ROOT=Path(__file__).resolve().parents[1]


class TrackedPermits:
    def __init__(self,real):
        self.real=real;self.releases=0;self.lock=threading.Lock()
    def acquire(self,*args,**kwargs):return self.real.acquire(*args,**kwargs)
    def release(self):
        with self.lock:self.releases+=1
        return self.real.release()
    @property
    def available(self):return self.real._value


class HttpQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repository=EditorRepository.create(ROOT/'examples/scene3d-contracts',Path(self.temp.name)/'repository')
        self.entered=threading.Event();self.release=threading.Event();self.active=0;self.maximum_active=0;self.active_lock=threading.Lock()
        def hold(server,body):
            with self.active_lock:
                self.active+=1;self.maximum_active=max(self.maximum_active,self.active)
                if self.active==4:self.entered.set()
            try:
                if not self.release.wait(20):raise RuntimeError('test release deadline exceeded')
                return {'released':True}
            finally:
                with self.active_lock:self.active-=1
        self.server=EditorServer(self.repository,workers=4,routes={'test/hold':hold})
        self.permits=TrackedPermits(self.server.slots);self.server.slots=self.permits
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.clients=ThreadPoolExecutor(max_workers=8)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.addCleanup(self.cleanup)
    def cleanup(self):
        self.release.set();self.server.shutdown();self.server.server_close()
        self.server_thread.join(5);self.clients.shutdown(wait=True,cancel_futures=True)
    def wait(self,condition):
        deadline=time.monotonic()+5
        while not condition():
            if time.monotonic()>deadline:self.fail('HTTP resource transition did not occur')
            time.sleep(.01)
    def fetch(self,path,body=None):
        headers={}
        if body is not None:headers={'Content-Type':'application/json','Authorization':'Bearer '+self.server.token}
        request=Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        with urlopen(request,timeout=25) as response:return response.status,response.read()
    def occupy(self):
        active=[self.clients.submit(self.fetch,'/api/test/hold',{}) for _ in range(4)]
        self.assertTrue(self.entered.wait(5));self.assertEqual(self.permits.available,4)
        return active
    def test_four_workers_four_pending_and_explicit_bounded_overload(self):
        active=self.occupy()
        queued=[self.clients.submit(self.fetch,'/editor/editor.js') for _ in range(4)]
        self.wait(lambda:self.permits.available==0)
        self.assertTrue(all(not future.done() for future in queued))
        with self.assertRaises(HTTPError) as caught:self.fetch('/editor/editor.js')
        self.assertEqual(caught.exception.code,503)
        self.assertEqual(caught.exception.headers['Retry-After'],'1');self.assertEqual(caught.exception.read(),b'')
        self.release.set()
        for future in active:self.assertEqual(future.result(5)[0],200)
        expected=hashlib.sha256((ROOT/'assets/editor3d/editor.js').read_bytes()).digest()
        for future in queued:
            status,payload=future.result(5);self.assertEqual(status,200);self.assertEqual(hashlib.sha256(payload).digest(),expected)
        self.wait(lambda:self.permits.available==8)
        self.assertEqual(self.maximum_active,4);self.assertEqual(self.permits.releases,8)
    def test_shutdown_closes_queued_sockets_and_releases_each_permit_once(self):
        active=self.occupy()
        queued=[self.clients.submit(self.fetch,'/editor/editor.js') for _ in range(4)]
        self.wait(lambda:self.permits.available==0)
        self.server.shutdown()
        closer=threading.Thread(target=self.server.server_close,daemon=True);closer.start()
        try:
            for future in queued:
                with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):future.result(5)
            self.assertTrue(closer.is_alive())
            self.assertEqual(self.permits.available,4);self.assertEqual(self.permits.releases,4)
        finally:
            self.release.set();closer.join(5)
        self.assertFalse(closer.is_alive())
        for future in active:self.assertEqual(future.result(5)[0],200)
        self.assertEqual(self.permits.available,8);self.assertEqual(self.permits.releases,8)
        self.server.server_close();self.assertEqual(self.permits.releases,8)
    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST')=='1','Enable actual Chromium queue recovery explicitly.')
    def test_fresh_authenticated_browser_loads_while_old_requests_drain(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with sync_playwright() as runtime:
            browser=launch_chromium(runtime)
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.server.url);page.wait_for_function('()=>window.editorReady===true')
                page.evaluate('window.__oldQueueRuntime=true')
                active=self.occupy()
                transition=[]
                def drain():
                    try:
                        self.wait(lambda:self.permits.available<4)
                        transition.append('fresh-document-queued-behind-four-running-requests')
                    finally:self.release.set()
                drainer=threading.Thread(target=drain,daemon=True);drainer.start()
                page.goto('about:blank');page.goto(self.server.url)
                page.wait_for_function('()=>window.editorReady===true');drainer.join(5)
                self.assertEqual(transition,['fresh-document-queued-behind-four-running-requests'])
                self.assertFalse(page.evaluate('()=>window.__oldQueueRuntime===true'))
                self.assertEqual(page.evaluate('()=>window.hatchEditor.snapshot.sha256'),self.repository.snapshot()['sha256'])
                count=page.evaluate('()=>{let count=0;window.hatchEditor.objects.get("mesh").traverse(object=>{if(object.isMesh)count+=object.geometry.attributes.position.count});return count;}')
                self.assertGreaterEqual(count,3);self.assertEqual(errors,[])
                for future in active:self.assertEqual(future.result(5)[0],200)
            finally:browser.close()


if __name__=='__main__':unittest.main()
