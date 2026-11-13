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

from editor3d_viewer import BinaryResponse, EditorServer, asset_identity, preview_snapshot
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


def versioned_repository(root):
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
    geometry = (root / 'triangle.obj').read_text().replace('v 1 0 0', 'v 2 0 0')
    (root / 'triangle2.obj').write_text(geometry)
    for field in ('content', 'source'):
        asset[field] = dict(path='triangle2.obj', sha256=hashlib.sha256(geometry.encode()).hexdigest())
    asset['meshes'] = [dict(asset['content'])]
    asset['resources']['bytes'] = len(geometry.encode())
    asset['bounds']['maximum'][0] = 2
    write('triangle2.json', asset)
    reference = dict(id='triangle', version='2.0.0', path='triangle2.json',
                     sha256=hashlib.sha256((root / 'triangle2.json').read_bytes()).hexdigest(), kind='scene3d-asset')
    project = read('project.json')
    project['assets'].append(reference)
    project['dependency_lock'].append(reference)
    write('project.json', project)
    scene = read('example-scene.json')
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

    def test_binary_artifacts_require_auth_and_reject_unsafe_response_headers(self):
        payload = b'\x89PNG\r\n\x1a\nverified-native-artifact'
        self.server.routes['render/artifact'] = lambda server, body: BinaryResponse(payload, 'image/png', 'frame-0.png')
        options = dict(data=b'{}', headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError):
            self.fetch('/api/render/artifact', token=False, **options)
        with self.fetch('/api/render/artifact', **options) as response:
            self.assertEqual(response.read(), payload)
            self.assertEqual(response.headers['Content-Type'], 'image/png')
            self.assertEqual(response.headers['Content-Disposition'], 'attachment; filename="frame-0.png"')
        for media, name in [('text/html', 'frame.html'), ('image/png', '../frame.png'), ('image/png', 'x\r\nInjected:true')]:
            self.server.routes['render/artifact'] = lambda server, body: BinaryResponse(payload, media, name)
            with self.subTest(media=media, name=name), self.assertRaises(HTTPError):
                self.fetch('/api/render/artifact', **options)

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
    def test_refresh_preserves_tab_auth_and_fresh_context_has_no_bearer(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with sync_playwright() as runtime:
            browser = launch_chromium(runtime)
            try:
                page = browser.new_page()
                page.goto(self.server.url)
                page.wait_for_function('() => window.editorReady === true')
                self.assertNotIn(self.server.token, page.url)
                page.reload()
                page.wait_for_function('() => window.editorReady === true')
                self.assertEqual(page.evaluate('location.hash + location.search'), '')
                context = browser.new_context()
                anonymous = context.new_page()
                anonymous.goto(self.base)
                anonymous.wait_for_function('() => document.querySelector("#diagnostic").hidden === false')
                self.assertIsNot(anonymous.evaluate('window.editorReady'), True)
                self.assertIsNone(anonymous.evaluate('sessionStorage.getItem("hatch-editor-bearer-v1")'))
                # A supplied invalid fragment must not inherit an existing tab token.
                page.goto(self.base + '/#invalid')
                page.reload()
                page.wait_for_function('() => document.querySelector("#diagnostic").hidden === false')
                self.assertIsNot(page.evaluate('window.editorReady'), True)
                context.close()
            finally:
                browser.close()

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
    def test_extension_transactions_lifecycle_and_bound_camera_capture(self):
        from tempfile import TemporaryDirectory
        from fractions import Fraction
        from editor3d_repository import EditorRepository
        from scene3d_graph import SceneGraph
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        import base64
        with TemporaryDirectory() as directory:
            from scene3d_example import build_example
            from scene3d_contracts import all_refs
            import math
            source = Path(directory) / 'source'
            build_example(source)
            document = json.loads((source / 'example-scene.json').read_text())
            native_camera = next(n for n in document['nodes'] if n['id'] == 'camera')
            # Aim the saved native camera at the triangle's actual front face.
            native_camera['transform'].update(translation=[2.5, -3, 0.5], rotation=[math.sqrt(0.5), 0, 0, math.sqrt(0.5)])
            (source / 'example-scene.json').write_text(json.dumps(document))
            project = json.loads((source / 'project.json').read_text())
            first_shot = json.loads((source / project['shots'][0]['path']).read_text())
            orthographic = json.loads(json.dumps(first_shot))
            orthographic.update(id='orthographic-shot', name='Orthographic override')
            orthographic['camera_overrides']['camera'] = dict(native_camera['camera'], projection='orthographic')
            (source / 'orthographic-shot.json').write_text(json.dumps(orthographic))
            reference = dict(project['shots'][0], id=orthographic['id'], path='orthographic-shot.json', sha256='0'*64)
            project['shots'].append(reference); project['dependency_lock'].append(reference)
            (source / 'project.json').write_text(json.dumps(project))
            for _ in range(6):
                for path in source.glob('*.json'):
                    document = json.loads(path.read_text())
                    for ref in all_refs(document):
                        if (source / ref['path']).is_file():
                            ref['sha256'] = hashlib.sha256((source / ref['path']).read_bytes()).hexdigest()
                    path.write_text(json.dumps(document))
            repository = EditorRepository.create(source, Path(directory) / 'saved')
            calls = []
            def edit(server, body):
                calls.append(body)
                return repository.apply(body['expected_revision'], dict(schema_version=1, commands=body['commands']),
                    body['transaction_id'], expected_sha256=body['expected_sha256'])
            def evaluate(server, body):
                current = repository.snapshot()
                self.assertEqual((body['revision'], body['sha256']), (current['revision'], current['sha256']))
                shot = current['documents'][body['shot']]
                rate = Fraction(**shot['fps'])
                graph = SceneGraph(current['scene'])
                sampled = graph.evaluate_base()
                for identifier, state in sampled.items():
                    authored = next(n for n in current['scene']['nodes'] if n['id'] == identifier)
                    state.update(visible=state['render_visible'], material_overrides=authored['material_overrides'])
                    for name in ('camera', 'light'):
                        if name in authored: state[name] = authored[name]
                    if identifier == shot['camera'] and 'camera' in shot['camera_overrides']:
                        state['camera'] = shot['camera_overrides']['camera']
                time = Fraction(body['frame']) / rate
                return dict(revision=current['revision'], snapshot_sha256=current['sha256'],
                    scene=shot['scene'], shot=next(r for r in current['project']['shots'] if r['path'] == body['shot']),
                    camera=shot['camera'], frame=dict(numerator=body['frame'], denominator=1),
                    time=dict(numerator=time.numerator, denominator=time.denominator), nodes=sampled, world=current['scene']['world'])
            server = EditorServer(repository, routes={'support/edit': edit, 'timeline/evaluate': evaluate})
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with sync_playwright() as runtime:
                    browser = launch_chromium(runtime)
                    try:
                        page = browser.new_page()
                        page.goto(server.url)
                        page.wait_for_function('() => window.editorReady === true')
                        page.evaluate('''() => {
                            window.supportCleanup = 0; window.supportInstall = 0;
                            window.unregister = window.hatchEditor.registerCommitInterceptor(envelope => ({route:'support/edit',
                                body:{expected_revision:envelope.expected_revision, expected_sha256:envelope.expected_sha256,
                                    transaction_id:envelope.transaction_id,commands:envelope.commands}}));
                            window.hatchEditor.registerExtension(({subscribeInstalled,getRenderer,getScene,getCamera}) => {
                                if (!getRenderer().isWebGLRenderer || !getScene().isScene || !getCamera().isCamera) throw Error('Missing viewer context');
                                const stop = subscribeInstalled(async ({isCurrent,snapshot}) => {
                                    if (!isCurrent() || snapshot.revision !== window.hatchEditor.snapshot.revision) throw Error('Stale install');
                                    window.supportInstall++; return () => {window.supportCleanup++;};
                                }); return {dispose:stop};
                            }, document.createElement('div'));
                        }''')
                        result = page.evaluate('''async () => {
                            const s = window.hatchEditor.snapshot, ref = s.project.shots[0], shot = s.documents[ref.path];
                            const metadata = {revision:s.revision,sha256:s.sha256,shot:{id:ref.id,version:ref.version},camera:shot.camera,
                                frame:0,source_time:{numerator:0,denominator:1},color_space:'srgb',view_transform:'standard',resolution:[320,240]};
                            const captured = await window.hatchEditor.capturePreview(metadata);
                            let rejected = false;
                            try { await window.hatchEditor.capturePreview({...metadata,sha256:'0'.repeat(64)}); } catch { rejected = true; }
                            const overrideRef = s.project.shots.find(r => r.id === 'orthographic-shot');
                            let projectionRejected = false;
                            try { await window.hatchEditor.capturePreview({...metadata,shot:{id:overrideRef.id,version:overrideRef.version}}); }
                            catch { projectionRejected = true; }
                            let exposureRejected = false;
                            try { window.hatchEditor.preview({nodes:[],world:{exposure:1024}}); }
                            catch { exposureRejected = true; }
                            return {captured,rejected,projectionRejected,exposureRejected,revision:window.hatchEditor.snapshot.revision};
                        }''')
                        self.assertTrue(result['rejected']); self.assertTrue(result['projectionRejected'])
                        self.assertTrue(result['exposureRejected']); self.assertEqual(result['revision'], 0)
                        captured = result['captured']
                        self.assertEqual(captured['resolution'], [320, 240])
                        from PIL import Image
                        from io import BytesIO
                        image = Image.open(BytesIO(base64.b64decode(captured['image'].split(',', 1)[1])))
                        self.assertEqual(image.size, (320, 240))
                        self.assertGreater(len(image.getcolors(320*240)), 1)
                        png = base64.b64decode(captured['image'].split(',', 1)[1])
                        server.routes['render/artifact'] = lambda server, body: BinaryResponse(png, 'image/png', 'captured-frame.png')
                        with page.expect_download() as downloaded:
                            receipt = page.evaluate("window.hatchEditor.downloadArtifact({job:'fixture',format:'image',frame:0})")
                        self.assertEqual(Path(downloaded.value.path()).read_bytes(), png)
                        self.assertEqual(receipt['bytes'], len(png))
                        page.locator('[data-id="mesh"]').click()
                        page.get_by_label('translation X', exact=True).fill('5')
                        page.get_by_label('translation X', exact=True).press('Tab')
                        page.wait_for_function('() => window.hatchEditor.snapshot.revision === 1')
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(repository.snapshot()['revision'], 1)
                        self.assertEqual(calls[0]['commands'][0]['operation'], 'transform')
                        page.wait_for_function('() => window.supportInstall === 2 && window.supportCleanup === 1')
                        self.assertEqual(page.evaluate('() => window.hatchEditor.errors'), [])
                        page.evaluate('window.unregister()')
                        lifecycle = page.evaluate('''async () => {
                            let first = true; window.delayedCleanups = 0;
                            const stop = window.hatchEditor.subscribeInstalled(async () => {
                                if (first) { first = false; await new Promise(resolve => {window.finishPending = resolve;}); }
                                return () => {window.delayedCleanups++;};
                            });
                            await window.hatchEditor.reload(); window.finishPending();
                            await new Promise(resolve => setTimeout(resolve, 10));
                            const stale = window.delayedCleanups; stop();
                            return [stale, window.delayedCleanups];
                        }''')
                        self.assertEqual(lifecycle, [1, 2])
                        document = json.loads((source / 'example-scene.json').read_text())
                        area = json.loads(json.dumps(document['nodes'][0]))
                        area.update(id='area', name='Unsupported native area light', type='light', parent='root',
                                    light=dict(type='area', color=[1, 1, 1], intensity=1))
                        document['nodes'].append(area); document['lights'].append('area')
                        (source / 'example-scene.json').write_text(json.dumps(document))
                        for _ in range(6):
                            for path in source.glob('*.json'):
                                document = json.loads(path.read_text())
                                for ref in all_refs(document):
                                    if (source / ref['path']).is_file():
                                        ref['sha256'] = hashlib.sha256((source / ref['path']).read_bytes()).hexdigest()
                                path.write_text(json.dumps(document))
                        repository = EditorRepository.create(source, Path(directory) / 'area-saved')
                        server.repository = repository
                        page.goto('about:blank'); page.goto(server.url)
                        page.wait_for_function('() => window.editorReady === true')
                        reason = page.evaluate('''async () => {
                            const s = window.hatchEditor.snapshot, ref = s.project.shots[0], shot = s.documents[ref.path];
                            try { await window.hatchEditor.capturePreview({revision:s.revision,sha256:s.sha256,
                                shot:{id:ref.id,version:ref.version},camera:shot.camera,frame:0,source_time:{numerator:0,denominator:1},
                                color_space:'srgb',view_transform:'standard',resolution:[320,240]}); return ''; }
                            catch (error) { return error.message; }
                        }''')
                        self.assertIn('area light', reason)
                    finally: browser.close()
            finally: server.shutdown(); server.server_close(); thread.join(timeout=5)

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
