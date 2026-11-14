"""Real HTTP admission, fixed worker count and cancelled socket ownership."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
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
        repository=EditorRepository.create(ROOT/'examples/scene3d-contracts',Path(self.temp.name)/'repository')
        self.entered=threading.Event();self.release=threading.Event()
        def hold(server,body):
            self.entered.set()
            if not self.release.wait(10):raise RuntimeError('test release deadline exceeded')
            return {'released':True}
        self.server=EditorServer(repository,workers=1,routes={'test/hold':hold})
        self.permits=TrackedPermits(self.server.slots);self.server.slots=self.permits
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.clients=ThreadPoolExecutor(max_workers=4)
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
        with urlopen(request,timeout=10) as response:return response.status,response.read()
    def occupy(self):
        active=self.clients.submit(self.fetch,'/api/test/hold',{})
        self.assertTrue(self.entered.wait(5));self.assertEqual(self.permits.available,2)
        return active
    def test_module_requests_queue_behind_fixed_worker_and_excess_is_closed(self):
        active=self.occupy()
        first=self.clients.submit(self.fetch,'/editor/editor.js');second=self.clients.submit(self.fetch,'/editor/inspectors.js')
        self.wait(lambda:self.permits.available==0)
        self.assertFalse(first.done());self.assertFalse(second.done())
        # The one running and two queued requests exhaust this one-worker server.
        with socket.create_connection(('127.0.0.1',self.server.server_port),timeout=5) as overflow:
            overflow.sendall(('GET /editor/editor.js HTTP/1.0\r\nHost: 127.0.0.1:'+str(self.server.server_port)+'\r\n\r\n').encode())
            try:self.assertEqual(overflow.recv(1),b'')
            except ConnectionResetError:pass
        self.release.set();self.assertEqual(active.result(5)[0],200)
        for future,name in [(first,'editor.js'),(second,'inspectors.js')]:
            status,payload=future.result(5);self.assertEqual(status,200)
            self.assertEqual(hashlib.sha256(payload).digest(),hashlib.sha256((ROOT/'assets/editor3d'/name).read_bytes()).digest())
        self.wait(lambda:self.permits.available==3);self.assertEqual(self.permits.releases,3)
    def test_shutdown_cancels_queued_sockets_and_releases_each_permit_once(self):
        active=self.occupy()
        queued=[self.clients.submit(self.fetch,'/editor/editor.js') for _ in range(2)]
        self.wait(lambda:self.permits.available==0)
        self.server.shutdown()
        closer=threading.Thread(target=self.server.server_close,daemon=True);closer.start()
        try:
            for future in queued:
                with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
                    future.result(5)
            self.assertTrue(closer.is_alive())  # The active worker still owns its permit.
            self.assertEqual(self.permits.available,2);self.assertEqual(self.permits.releases,2)
        finally:
            self.release.set();closer.join(5)
        self.assertFalse(closer.is_alive());self.assertEqual(active.result(5)[0],200)
        self.assertEqual(self.permits.available,3);self.assertEqual(self.permits.releases,3)
        self.server.server_close()
        self.assertEqual(self.permits.releases,3)


if __name__=='__main__':unittest.main()
