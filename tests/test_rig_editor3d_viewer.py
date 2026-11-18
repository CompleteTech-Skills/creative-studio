"""Real native-geometry and HTTP admission tests for the editor bridge.

The read-only repository below is a test transport for an unchanged, validated
native fixture. Persistence acceptance uses EditorRepository integration tests.
"""
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from editor3d_viewer import EditorServer, asset_identity, preview_snapshot
from rig_errors import RigError
from scene3d_contracts import load_project

ROOT = Path(__file__).resolve().parents[1]


class NativeReadRepository:
    def __init__(self):
        self.root = ROOT / 'examples/scene3d-contracts'
        self.value = load_project(self.root)
        self.value.update(revision=0, sha256=hashlib.sha256(
            json.dumps(self.value['project'], sort_keys=True).encode()).hexdigest(),
            scene=next(d for d in self.value['documents'].values() if d.get('kind') == 'scene3d-scene'))

    def snapshot(self):
        return self.value

    def read_file(self, path, snapshot_sha256):
        if snapshot_sha256 != self.value['sha256']:
            raise RigError('EDITOR3D_STALE', 'Unknown snapshot.')
        return (self.root / path).read_bytes()


def versioned_repository(root, *, extra_first_instance=False):
    """Load two validated immutable revisions of one real native mesh."""
    from copy import deepcopy
    from scene3d_example import build_example
    from scene3d_contracts import all_refs
    build_example(root)
    def read(name):
        return json.loads((root / name).read_text())
    def write(name, value):
        (root / name).write_text(json.dumps(value))
    asset = read('triangle.json')
    asset['version'] = '2.0.0'
    geometry = (root / 'triangle.obj').read_bytes().replace(b'v 1 0 0', b'v 2 0 0')
    (root / 'triangle2.obj').write_bytes(geometry)
    for field in ('content', 'source'):
        asset[field] = dict(path='triangle2.obj', sha256=hashlib.sha256(geometry).hexdigest())
    asset['meshes'] = [dict(asset['content'])]
    asset['resources']['bytes'] = len(geometry)
    asset['bounds']['maximum'][0] = 2
    write('triangle2.json', asset)
    reference = dict(id='triangle', version='2.0.0', path='triangle2.json',
                     sha256=hashlib.sha256((root / 'triangle2.json').read_bytes()).hexdigest(), kind='scene3d-asset')
    project = read('project.json')
    project['assets'].append(reference)
    project['dependency_lock'].append(reference)
    write('project.json', project)
    scene = read('example-scene.json')
    if extra_first_instance:
        first_instance = deepcopy(scene['nodes'][1])
        first_instance.update(id='mesh-extra', name='Extra first mesh')
        scene['nodes'].append(first_instance)
    node = deepcopy(scene['nodes'][1])
    node.update(id='mesh2', asset=reference)
    node['transform']['translation'] = [4, 0, 0]
    scene['nodes'].append(node)
    scene['dependencies'].append(reference)
    write('example-scene.json', scene)
    for _ in range(6):
        for path in root.glob('*.json'):
            document = read(path.name)
            for ref in all_refs(document):
                if (root / ref['path']).is_file():
                    ref['sha256'] = hashlib.sha256((root / ref['path']).read_bytes()).hexdigest()
            write(path.name, document)
    repository = NativeReadRepository()
    repository.root = root
    repository.value = load_project(root)
    repository.value.update(revision=0, sha256='0' * 64,
        scene=repository.value['documents']['example-scene.json'])
    return repository


class EditorViewportTests(unittest.TestCase):
    def setUp(self):
        self.repository = NativeReadRepository()

    def test_real_geometry_is_decoded_and_snapshot_bound(self):
        value = preview_snapshot(self.repository, self.repository.snapshot())
        geometry = value['viewport_assets'][asset_identity(value['scene']['nodes'][1]['asset'])]
        self.assertEqual(geometry['format'], 'geometry')
        self.assertEqual(len(geometry['meshes'][0]['positions']), 3)
        self.assertEqual(len(geometry['meshes'][0]['triangles']), 1)
        self.assertEqual(value['preview']['source_sha256'], value['sha256'])
        self.assertFalse(value['preview']['offline_render'])

    def test_distinct_asset_versions_keep_geometry_and_instance_budgets(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        with TemporaryDirectory() as directory:
            repository = versioned_repository(Path(directory) / 'native')
            value = preview_snapshot(repository, repository.snapshot())
            first, second = [node['asset'] for node in value['scene']['nodes'] if node.get('asset')]
            self.assertNotEqual(asset_identity(first), asset_identity(second))
            self.assertEqual(len(value['viewport_assets']), 2)
            self.assertEqual(value['viewport_assets'][asset_identity(first)]['meshes'][0]['positions'][1][0], 1)
            self.assertEqual(value['viewport_assets'][asset_identity(second)]['meshes'][0]['positions'][1][0], 2)
            # Each three-vertex mesh is instantiated once; ID-only counting would count both twice.
            with patch('editor3d_viewer.MAX_PREVIEW_VERTICES', 6):
                limited = preview_snapshot(repository, repository.snapshot())
            self.assertTrue(all(asset['format'] == 'geometry' for asset in limited['viewport_assets'].values()))

    def test_asset_exceeding_instance_budget_leaves_room_for_the_next_asset(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        with TemporaryDirectory() as directory:
            repository = versioned_repository(Path(directory) / 'native', extra_first_instance=True)
            with patch('editor3d_viewer.MAX_PREVIEW_VERTICES', 4):
                value = preview_snapshot(repository, repository.snapshot())
            by_node = {node['id']: node for node in value['scene']['nodes']}
            first = value['viewport_assets'][asset_identity(by_node['mesh']['asset'])]
            second = value['viewport_assets'][asset_identity(by_node['mesh2']['asset'])]
            self.assertEqual(first['format'], 'unavailable')
            self.assertIn('Instanced geometry', first['message'])
            self.assertEqual(second['format'], 'geometry')
            self.assertEqual(len(second['meshes'][0]['positions']), 3)

    def test_restoring_same_content_retains_new_revision_and_redo_state(self):
        from tempfile import TemporaryDirectory
        from editor3d_repository import EditorRepository
        with TemporaryDirectory() as directory:
            repository = EditorRepository.create(ROOT / 'examples/scene3d-contracts', Path(directory) / 'saved')
            server = EditorServer(repository)
            try:
                initial = server.snapshot()
                commands = dict(schema_version=1, commands=[dict(operation='transform', parameters=dict(
                    identifier='mesh', transform=dict(translation=[3, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1])))])
                repository.apply(0, commands, 'move-1', expected_sha256=initial['sha256'])
                edited = server.snapshot()
                repository.undo(1, 'undo-1', expected_sha256=edited['sha256'])
                restored = server.snapshot()
                self.assertEqual(restored['sha256'], initial['sha256'])
                self.assertEqual(restored['revision'], 2)
                self.assertTrue(restored['redo_available'])
                self.assertFalse(restored['undo_available'])
            finally:
                server.server_close()

    def test_reload_retries_unavailable_projection_of_the_same_revision(self):
        from unittest.mock import patch
        server = EditorServer(self.repository)
        try:
            with patch('editor3d_viewer.preview_snapshot', side_effect=OSError('Preview staging unavailable')):
                unavailable = server.snapshot()
            self.assertFalse(unavailable['viewport_state']['available'])
            self.assertEqual(unavailable['viewport_state']['error']['code'], 'EDITOR3D_VIEW_PROJECTION')
            reloaded = server.snapshot()
            self.assertTrue(reloaded['viewport_state']['available'])
            self.assertEqual((reloaded['revision'], reloaded['sha256']),
                             (unavailable['revision'], unavailable['sha256']))
        finally:
            server.server_close()

    def test_native_read_interruption_preserves_the_verification_error(self):
        from unittest.mock import patch
        server = EditorServer(self.repository)
        try:
            with patch.object(self.repository, 'read_file', side_effect=OSError('Native snapshot read interrupted')):
                with self.assertRaises(RigError) as caught:
                    server.snapshot()
            self.assertEqual(caught.exception.report['code'], 'EDITOR3D_VIEW_HASH')
            self.assertTrue(server.snapshot()['viewport_state']['available'])
        finally:
            server.server_close()

    def test_corrupt_native_bytes_fail_before_preview(self):
        original = self.repository.read_file
        self.repository.read_file = lambda path, sha: original(path, sha) + b' '
        with self.assertRaises(RigError) as caught:
            preview_snapshot(self.repository, self.repository.snapshot())
        self.assertEqual(caught.exception.report['code'], 'EDITOR3D_VIEW_HASH')

    def test_preview_size_is_bounded_before_any_read(self):
        value = self.repository.snapshot()
        next(iter(value['inventory'].values()))['bytes'] = 65 * 1024 * 1024
        self.repository.read_file = lambda *args: self.fail('Oversize snapshot read bytes')
        with self.assertRaises(RigError):
            preview_snapshot(self.repository, value)

    def test_unrepresentable_exposure_is_rejected_before_read(self):
        value = self.repository.snapshot()
        value['scene']['world']['exposure'] = 1000
        self.repository.read_file = lambda *args: self.fail('Unsupported exposure read bytes')
        with self.assertRaises(RigError) as error:
            preview_snapshot(self.repository, value)
        self.assertEqual(error.exception.report['code'], 'EDITOR3D_VIEW_EXPOSURE')

    def test_upstream_transform_controls_is_pinned(self):
        manifest = json.loads((ROOT / 'assets/editor3d/vendor.json').read_text())
        for item in manifest['files']:
            path = ROOT / 'assets/editor3d' / item['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item['sha256'])
        self.assertEqual(manifest['version'], '0.180.0')


class EditorHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = EditorServer(NativeReadRepository())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def fetch(self, route, *, token=True, headers=None, data=None):
        all_headers = dict(headers or {})
        if token:
            all_headers['Authorization'] = 'Bearer ' + self.server.token
        request = Request(self.base + route, headers=all_headers, data=data)
        return urlopen(request, timeout=10)

    def test_actual_http_snapshot_and_offline_assets(self):
        with self.fetch('/api/snapshot') as response:
            snapshot = json.load(response)
        self.assertEqual(snapshot['viewport_assets'][asset_identity(snapshot['scene']['nodes'][1]['asset'])]['format'], 'geometry')
        for path in ('/', '/editor/editor.js', '/editor/TransformControls.js', '/vendor/build/three.module.min.js'):
            with self.fetch(path, token=False) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("object-src 'none'", response.headers['Content-Security-Policy'])
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_saved_edit_acknowledged_when_exposure_exceeds_preview_range(self):
        from tempfile import TemporaryDirectory
        from editor3d_repository import EditorRepository

        def snapshot():
            with self.fetch('/api/snapshot') as response:
                return json.load(response)

        def mutate(route, base, transaction_id, **fields):
            request = dict(expected_revision=base['revision'], expected_sha256=base['sha256'],
                           transaction_id=transaction_id, **fields)
            with self.fetch('/api/' + route, headers={'Content-Type': 'application/json'},
                            data=json.dumps(request).encode()) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)

        source = ROOT / 'examples/scene3d-contracts'
        with TemporaryDirectory() as directory:
            repository = EditorRepository.create(source, Path(directory) / 'saved')
            self.server.repository = repository
            before = snapshot()
            commands = dict(schema_version=1, commands=[dict(operation='configure-world',
                parameters=dict(properties=dict(exposure=21)))])
            edited = mutate('commands', before, 'high-exposure', commands=commands)
            native = EditorRepository(repository.root).snapshot()
            self.assertEqual(native['scene']['world']['exposure'], 21)
            self.assertEqual((edited['revision'], edited['sha256']), (native['revision'], native['sha256']))
            self.assertTrue(edited['undo_available'])
            state = edited['viewport_state']
            self.assertFalse(state['available'])
            self.assertEqual((state['revision'], state['sha256']), (edited['revision'], edited['sha256']))
            self.assertEqual(state['error']['code'], 'EDITOR3D_VIEW_EXPOSURE')
            self.assertEqual(snapshot(), edited)

            restored = mutate('undo', edited, 'undo-exposure')
            self.assertTrue(restored['viewport_state']['available'])
            self.assertTrue(restored['redo_available'])
            self.assertEqual(restored['sha256'], before['sha256'])
            replayed = mutate('commands', before, 'high-exposure', commands=commands)
            self.assertEqual(replayed, restored)
            self.assertEqual(len(repository.history()), 3)
            redone = mutate('redo', restored, 'redo-exposure')
            self.assertEqual(redone['sha256'], edited['sha256'])
            self.assertEqual(redone['revision'], 3)
            self.assertFalse(redone['viewport_state']['available'])

    def test_auth_origin_and_static_path_boundaries(self):
        for route, options in (
            ('/api/snapshot', dict(token=False)),
            ('/api/snapshot', dict(headers={'Origin': 'https://evil.invalid'})),
            ('/api/snapshot', dict(headers={'Host': 'evil.invalid'})),
            ('/editor/../../scripts/editor3d_viewer.py', {}),
            ('/vendor/../index.html', {}),
            ('/api/snapshot?token=secret', {}),
        ):
            with self.subTest(route=route, options=options), self.assertRaises(HTTPError):
                self.fetch(route, **options)

    def test_uploads_are_opaque_bound_and_preserve_original_bytes(self):
        payload = b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
        with self.fetch('/api/uploads', data=payload, headers={
            'Content-Type':'application/octet-stream', 'X-Filename':'triangle.obj'}) as response:
            receipt=json.load(response)
        self.assertEqual(receipt['sha256'],hashlib.sha256(payload).hexdigest())
        path=self.server.upload_path(receipt['upload_id'])
        self.assertEqual(path.read_bytes(),payload)
        path.write_bytes(payload+b'changed')
        with self.assertRaises(RigError):self.server.upload_path(receipt['upload_id'])
        for name in ('../source.obj','%2e%2e%2fsource.obj','C:%5csource.obj'):
            with self.subTest(name=name),self.assertRaises(HTTPError):
                self.fetch('/api/uploads',data=payload,headers={'Content-Type':'application/octet-stream','X-Filename':name})

    def test_body_encoding_and_unexpected_fields_fail_before_mutation(self):
        for data, headers in ((b'{}', {'Content-Type': 'text/plain'}),
                              ('{}'.encode('utf-16'), {'Content-Type': 'application/json'}),
                              (b'{"unexpected":true}', {'Content-Type': 'application/json'})):
            with self.subTest(data=data), self.assertRaises(HTTPError):
                self.fetch('/api/commands', data=data, headers=headers)


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual editor browser acceptance explicitly.')
class EditorBrowserTests(EditorHttpTests):
    def test_two_native_asset_versions_render_distinct_meshes(self):
        from tempfile import TemporaryDirectory
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with TemporaryDirectory() as directory:
            self.server.repository = versioned_repository(Path(directory) / 'native')
            with sync_playwright() as runtime:
                browser = launch_chromium(runtime)
                try:
                    page = browser.new_page(viewport={'width': 1400, 'height': 900})
                    page.goto(self.server.url)
                    page.wait_for_function('() => window.editorReady === true')
                    widths = page.evaluate("""() => ['mesh', 'mesh2'].map(id => {
                        const object = window.hatchEditor.objects.get(id);
                        let maximum = 0;
                        object.traverse(child => {
                            if (child.geometry) {
                                child.geometry.computeBoundingBox();
                                maximum = Math.max(maximum, child.geometry.boundingBox.max.x);
                            }
                        });
                        return maximum;
                    })""")
                    self.assertEqual(widths, [1, 2])
                    self.assertEqual(page.evaluate('() => window.hatchEditor.errors'), [])
                finally:
                    browser.close()

    def test_real_webgl_geometry_selection_and_navigation(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with sync_playwright() as runtime:
            browser = launch_chromium(runtime)
            try:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(self.server.url)
                page.wait_for_function('() => window.editorReady === true')
                # The scene is installed before requestAnimationFrame draws it.
                page.wait_for_function('() => window.hatchEditor.renderer.info.render.calls > 0')
                self.assertGreater(page.evaluate('() => window.hatchEditor.renderer.info.render.calls'), 0)
                page.locator('[data-id="mesh"]').click()
                self.assertEqual(page.evaluate('() => window.hatchEditor.selection'), ['mesh'])
                self.assertEqual(page.get_by_label('translation X', exact=True).input_value(), '2')
                for view in ('top', 'left', 'front', 'perspective'):
                    page.select_option('#view', view)
                    page.wait_for_timeout(50)
                    self.assertEqual(page.locator('#view-label').inner_text(), view)
                page.select_option('#shading', 'wireframe')
                page.get_by_label('axes overlay').uncheck()
                page.get_by_label('Filter scene objects').fill('mesh')
                self.assertEqual(page.locator('#outliner button').count(), 1)
                self.assertEqual(errors, [])
                self.assertEqual(page.evaluate('() => window.hatchEditor.errors'), [])
            finally:
                browser.close()


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual editor browser acceptance explicitly.')
class EditorPersistentBrowserTests(unittest.TestCase):
    def test_pending_and_consecutive_numeric_edits_preserve_control_state(self):
        from tempfile import TemporaryDirectory
        from editor3d_repository import EditorRepository
        from playwright.sync_api import sync_playwright, expect
        from browser_runtime import launch_chromium
        with TemporaryDirectory() as directory:
            repository = EditorRepository.create(ROOT / 'examples/scene3d-contracts', Path(directory) / 'saved')
            server = EditorServer(repository)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as runtime:
                    browser = launch_chromium(runtime)
                    try:
                        page = browser.new_page(viewport={'width': 1400, 'height': 900})
                        page.goto(server.url)
                        page.wait_for_function('() => window.editorReady === true')
                        page.locator('[data-id="mesh"]').click()
                        pending = []
                        page.route('**/api/commands', lambda route: pending.append(route))
                        x = page.get_by_label('translation X', exact=True)
                        y = page.get_by_label('translation Y', exact=True)
                        x.fill('3'); x.press('Tab')
                        page.wait_for_timeout(100)
                        self.assertEqual(len(pending), 1)
                        self.assertTrue(x.is_disabled())
                        self.assertTrue(y.is_disabled())
                        self.assertTrue(page.locator('#undo').is_disabled())
                        self.assertTrue(page.locator('#group').is_disabled())
                        self.assertFalse(page.locator('[data-id="mesh"]').evaluate('(node) => node.draggable'))
                        page.locator('body').focus()
                        page.keyboard.press('ArrowRight')
                        self.assertIn('retry this edit', page.locator('#status').inner_text())
                        pending.pop().continue_()
                        page.unroute('**/api/commands')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                        expect(y).to_be_enabled()
                        expect(page.locator('#group')).to_be_enabled()
                        y.fill('1'); y.press('Tab')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2')
                        expect(x).to_be_enabled()
                        saved = EditorRepository(repository.root).snapshot()
                        self.assertEqual(next(n for n in saved['scene']['nodes'] if n['id'] == 'mesh')['transform']['translation'], [3, 1, 0])
                        page.click('#undo')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 3')
                        expect(y).to_be_enabled()
                        self.assertEqual(y.input_value(), '0')
                        page.click('#redo')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 4')
                        expect(y).to_be_enabled()
                        self.assertEqual(y.input_value(), '1')
                    finally:
                        browser.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_numeric_group_undo_redo_and_reopen_are_real_native_edits(self):
        from tempfile import TemporaryDirectory
        from editor3d_repository import EditorRepository
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with TemporaryDirectory() as directory:
            repository = EditorRepository.create(ROOT / 'examples/scene3d-contracts', Path(directory) / 'saved')
            server = EditorServer(repository)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as runtime:
                    browser = launch_chromium(runtime)
                    try:
                        page = browser.new_page(viewport={'width': 1400, 'height': 900})
                        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(server.url)
                        page.wait_for_function('() => window.editorReady === true')
                        page.locator('[data-id="mesh"]').click()
                        page.get_by_label('translation X',exact=True).fill('3')
                        page.get_by_label('translation X',exact=True).press('Tab')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                        saved=EditorRepository(repository.root).snapshot()
                        self.assertEqual(next(n for n in saved['scene']['nodes'] if n['id']=='mesh')['transform']['translation'],[3,0,0])
                        page.click('#undo')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 2')
                        self.assertEqual(page.get_by_label('translation X',exact=True).input_value(),'2')
                        page.click('#redo')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 3')
                        self.assertEqual(page.get_by_label('translation X',exact=True).input_value(),'3')
                        page.click('#group')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 4')
                        saved=EditorRepository(repository.root).snapshot()
                        node=next(n for n in saved['scene']['nodes'] if n['id']=='mesh')
                        self.assertTrue(node['parent'].startswith('group-'))
                        # The app strips the bearer fragment; navigating to the same
                        # path with a fragment is only an in-document navigation.
                        page.goto('about:blank')
                        page.goto(server.url)
                        page.wait_for_function('() => window.editorReady === true && window.hatchEditor.snapshot.revision === 4')
                        page.locator('[data-id="mesh"]').click()
                        self.assertEqual(page.get_by_label('translation X',exact=True).input_value(),'3')
                        self.assertIn('Loaded saved revision 4',page.locator('#status').inner_text())
                        self.assertEqual(errors,[])
                        self.assertEqual(page.evaluate('() => window.hatchEditor.errors'),[])
                    finally:
                        browser.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
