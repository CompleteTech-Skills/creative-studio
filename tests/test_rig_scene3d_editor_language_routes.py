"""Actual HTTP language edits through the canonical native repository.

These integration tests require the Scene64 language owner modules in addition
 to the EditorServer. They do not replace the local HTTP or persistence layers.
"""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer
from scene3d_example import build_example
from scene3d_editor_language_routes import language_routes


class EditorLanguageRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        build_example(self.directory / 'source')
        self.repository = EditorRepository.create(self.directory / 'source', self.directory / 'saved',
                                                  scene_path='example-scene.json')
        self.server = EditorServer(self.repository, routes=language_routes())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def post(self, path, value, *, authenticated=True, token=None):
        headers = {'Content-Type': 'application/json', 'Origin': self.base}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + (self.server.token if token is None else token)
        request = Request(self.base + '/api/' + path, json.dumps(value).encode(), headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def request(self, text='hide mesh'):
        saved = self.repository.snapshot()
        return dict(text=text, environment=False, expected_revision=saved['revision'], expected_sha256=saved['sha256'])

    def test_real_plan_apply_reopen_and_idempotent_retry(self):
        code, plan = self.post('language/plan', self.request())
        self.assertEqual(code, 200, plan)
        self.assertEqual(self.repository.snapshot()['revision'], 0)
        request = dict(plan=plan, transaction_id='language-http')
        code, result = self.post('language/apply', request)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['revision'], 1)
        reopened = EditorRepository(self.directory / 'saved').snapshot()
        self.assertFalse(next(n for n in reopened['scene']['nodes'] if n['id'] == 'mesh')['visible'])
        code, repeated = self.post('language/apply', request)
        self.assertEqual(code, 200, repeated)
        self.assertEqual(repeated['revision'], 1)

    def test_stale_plan_and_unsupported_request_preserve_saved_revision(self):
        request = self.request()
        code, plan = self.post('language/plan', request)
        self.assertEqual(code, 200, plan)
        self.assertEqual(self.post('language/apply', dict(plan=plan, transaction_id='first'))[0], 200)
        code, result = self.post('language/plan', request)
        self.assertEqual(code, 409, result)
        self.assertEqual(result['error']['code'], 'SCENE3D_LANGUAGE_STALE')
        code, result = self.post('language/plan', self.request('execute arbitrary code'))
        self.assertEqual(code, 200, result)
        self.assertEqual(result['scene_plan']['status'], 'unresolved')
        self.assertEqual(self.post('language/apply', dict(plan=result, transaction_id='invalid'))[0], 400)
        self.assertEqual(self.repository.snapshot()['revision'], 1)

    def test_authentication_and_exact_body_allowlist_precede_publication(self):
        # The existing transport rejects an absent required header as malformed
        # (400), and a supplied invalid credential as unauthorized (401).
        self.assertEqual(self.post('language/plan', self.request(), authenticated=False)[0], 400)
        self.assertEqual(self.post('language/plan', self.request(), token='invalid')[0], 401)
        invalid = self.request()
        invalid['callable'] = 'os.system'
        code, result = self.post('language/plan', invalid)
        self.assertEqual(code, 400, result)
        self.assertEqual(result['error']['code'], 'SCENE3D_LANGUAGE_REQUEST')
        invalid = self.request()
        invalid['expected_revision'] = True
        self.assertEqual(self.post('language/plan', invalid)[0], 400)
        self.assertEqual(self.repository.snapshot()['revision'], 0)


if __name__ == '__main__':
    unittest.main()
