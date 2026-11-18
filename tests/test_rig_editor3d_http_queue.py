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
from unittest.mock import patch
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
            self.wait(lambda:self.permits.available==4 and self.permits.releases==4)
            self.assertTrue(closer.is_alive())
            self.assertEqual(self.permits.available,4);self.assertEqual(self.permits.releases,4)
        finally:
            self.release.set();closer.join(5)
        self.assertFalse(closer.is_alive())
        for future in active:self.assertEqual(future.result(5)[0],200)
        self.assertEqual(self.permits.available,8);self.assertEqual(self.permits.releases,8)
        self.server.server_close();self.assertEqual(self.permits.releases,8)

    def test_due_checkpoint_does_not_block_http_or_accept_loop_shutdown(self):
        import editor3d_repository

        entered = threading.Event()
        release = threading.Event()
        calls = []
        write = editor3d_repository._write_metadata
        repository = EditorRepository(self.repository.root)

        def gate_checkpoint(path, value):
            if path.name == 'autosave.json':
                calls.append(value['revision'])
                entered.set()
                if not release.wait(10):
                    self.fail('checkpoint release deadline exceeded')
            return write(path, value)

        with patch('editor3d_repository._write_metadata', side_effect=gate_checkpoint), EditorServer(
                repository, workers=1) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            repository.request_autosave(now=time.monotonic() - repository.limits['autosave_delay'])
            thread.start()
            shutdown = None
            try:
                self.assertTrue(entered.wait(5))
                endpoint = f'http://127.0.0.1:{server.server_port}/editor/editor.js'
                with urlopen(endpoint, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(calls, [0])
                shutdown = self.clients.submit(server.shutdown)
                shutdown.result(3)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                if shutdown is None:
                    server.shutdown()
                else:
                    shutdown.result(5)
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [0])
        checkpoint = json.loads((repository.root / 'autosave.json').read_text())
        self.assertEqual(checkpoint['sha256'], repository.snapshot()['sha256'])

    def test_edit_during_checkpoint_verification_supersedes_checkpoint(self):
        import editor3d_repository

        entered = threading.Event()
        release = threading.Event()
        bundle = self.repository._bundle
        write = editor3d_repository._write_metadata
        written = []

        def gate_verification(record):
            if threading.current_thread().name.startswith('editor-checkpoint') and record['revision'] == 1:
                entered.set()
                if not release.wait(10):
                    self.fail('checkpoint release deadline exceeded')
            return bundle(record)

        def record_write(path, value):
            write(path, value)
            if path.name == 'autosave.json':
                written.append((value['revision'], value['sha256']))

        def edit(base, x):
            commands = dict(schema_version=1, commands=[dict(operation='transform', parameters=dict(
                identifier='mesh', transform=dict(translation=[x, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1])))])
            status, body = self.fetch('/api/commands', dict(expected_revision=base['revision'],
                expected_sha256=base['sha256'], transaction_id=f'move-{x}', commands=commands))
            self.assertEqual(status, 200)
            return json.loads(body)

        with patch.object(self.repository, '_bundle', side_effect=gate_verification), patch(
                'editor3d_repository._write_metadata', side_effect=record_write):
            try:
                first = edit(self.repository.snapshot(), 3)
                self.assertTrue(entered.wait(5))
                latest = edit(first, 4)
                self.assertEqual(latest['revision'], 2)
                self.assertEqual(written, [])
            finally:
                release.set()
            self.wait(lambda: bool(written))
            self.assertEqual(written, [(2, latest['sha256'])])

    def test_edit_waits_for_checkpoint_publication_and_is_acknowledged(self):
        import editor3d_repository
        from editor3d_viewer import EditorHandler

        publishing = threading.Event()
        parsed = threading.Event()
        release = threading.Event()
        write = editor3d_repository._write_metadata
        read_body = EditorHandler.body
        baseline = self.repository.snapshot()
        request = dict(expected_revision=baseline['revision'], expected_sha256=baseline['sha256'],
                       transaction_id='move-during-checkpoint', commands=dict(schema_version=1, commands=[
                           dict(operation='transform', parameters=dict(identifier='mesh', transform=dict(
                               translation=[4, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1])))]))

        def gate_checkpoint(path, value):
            if path.name == 'autosave.json' and value['revision'] == baseline['revision']:
                publishing.set()
                if not release.wait(10):
                    self.fail('checkpoint release deadline exceeded')
            return write(path, value)

        def observe_body(handler):
            body = read_body(handler)
            if body.get('transaction_id') == request['transaction_id']:
                parsed.set()
            return body

        with patch('editor3d_repository._write_metadata', side_effect=gate_checkpoint), patch.object(
                EditorHandler, 'body', observe_body):
            self.repository.request_autosave(now=time.monotonic() - self.repository.limits['autosave_delay'])
            try:
                self.assertTrue(publishing.wait(5))
                pending = self.clients.submit(self.fetch, '/api/commands', request)
                self.assertTrue(parsed.wait(5))
                with self.assertRaises(TimeoutError):
                    pending.result(.25)
            finally:
                release.set()
            status, body = pending.result(5)
        self.assertEqual(status, 200)
        acknowledged = json.loads(body)
        self.assertEqual(acknowledged['revision'], baseline['revision'] + 1)
        self.assertEqual(self.repository.snapshot()['sha256'], acknowledged['sha256'])

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
