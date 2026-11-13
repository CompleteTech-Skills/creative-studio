"""Unconfigured sessions expose an explicit unavailable rendering capability."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer
from scene3d_example import build_example


class RenderLaunchTests(unittest.TestCase):
    def test_standard_session_reports_unavailable_without_creating_a_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);build_example(root/'source')
            repository=EditorRepository.create(root/'source',root/'repository')
            server=EditorServer(repository)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                request=Request('http://127.0.0.1:'+str(server.server_port)+'/api/render/capabilities',
                    data=b'{}',headers={'Authorization':'Bearer '+server.token,'Content-Type':'application/json'})
                with urlopen(request,timeout=10) as response:result=json.load(response)
                self.assertFalse(result['available']);self.assertFalse(result['executed'])
                self.assertEqual(result['rendering'],'unavailable')
                self.assertIn('configured native renderer',result['diagnostic'])
                self.assertEqual(set(path.name for path in root.iterdir()),{'source','repository'})
            finally:server.shutdown();server.server_close();thread.join(timeout=5)
