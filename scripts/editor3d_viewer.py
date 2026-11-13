"""Authenticated local WebGL editor transport over an authoritative repository.

The repository owns every mutation and native scene validation. This bridge only
serves verified snapshot resources and turns supported native geometry into a
bounded preview. WebGL output is not represented as an offline render.
"""
from __future__ import annotations
import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import stat
import uuid
import tempfile
import threading
from urllib.parse import urlsplit, unquote

from rig_errors import RigError, decode_json, require, safe_path

MAX_BODY = 4 * 1024 * 1024
MAX_PREVIEW_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_VERTICES = 500_000
ROOT = Path(__file__).resolve().parents[1]


def asset_identity(reference):
    """Exact immutable native reference used by both producer and browser."""
    return json.dumps([reference['id'], reference['version'], reference['path'],
                       reference['sha256']], ensure_ascii=False, separators=(',', ':'))


def preview_snapshot(repository, snapshot):
    """Bind a real viewport payload to an immutable native repository snapshot."""
    from rig_import import load_character_model
    from rig_contracts import DEFAULT_LIMITS
    result = dict(snapshot)
    # No absolute filesystem roots or private repository internals reach a browser.
    result.pop('root', None)
    assets = {}
    scene = snapshot['scene']
    require(-20 <= scene['world']['exposure'] <= 20, 'EDITOR3D_VIEW_EXPOSURE',
            'WebGL preview exposure is supported from -20 to +20 stops.')
    require(len(scene['nodes']) <= 1000, 'EDITOR3D_VIEW_LIMIT',
            'The interactive preview supports at most 1,000 scene nodes.')
    projected_vertices = 0
    inventory = snapshot['inventory']
    total = sum(item['bytes'] for item in inventory.values())
    require(total <= MAX_PREVIEW_BYTES, 'EDITOR3D_VIEW_LIMIT',
            'Snapshot exceeds the 64 MiB WebGL preview budget.')
    with tempfile.TemporaryDirectory(prefix='hatch-editor-preview-') as temporary:
        root = Path(temporary)
        for name, entry in inventory.items():
            payload = repository.read_file(name, snapshot['sha256'])
            require(len(payload) == entry['bytes'] and hashlib.sha256(payload).hexdigest() == entry['sha256'],
                    'EDITOR3D_VIEW_HASH', 'Native snapshot bytes changed.')
            path = safe_path(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        for document_path, document in snapshot['documents'].items():
            if document.get('kind') != 'scene3d-asset':
                continue
            identifier = asset_identity(dict(id=document['id'], version=document['version'],
                path=document_path, sha256=inventory[document_path]['sha256']))
            path = safe_path(root, document['content']['path'], exists=True)
            try:
                require(path.suffix.lower() in ('.obj', '.glb'), 'EDITOR3D_VIEW_FORMAT',
                        'Preview currently supports verified GLB and OBJ geometry.')
                limits = dict(DEFAULT_LIMITS)
                limits['vertices'] = min(limits['vertices'], MAX_PREVIEW_VERTICES)
                imported = load_character_model(path, limits=limits)
                instances = sum(asset_identity(node['asset']) == identifier
                                for node in scene['nodes'] if node.get('asset'))
                projected_vertices += imported.geometry.vertex_count * instances
                require(projected_vertices <= MAX_PREVIEW_VERTICES, 'EDITOR3D_VIEW_LIMIT',
                        'Instanced geometry exceeds the 500,000-vertex viewport budget.')
                for dependency in imported.source_files:
                    relative = Path(dependency).resolve().relative_to(root).as_posix()
                    require(relative in inventory, 'EDITOR3D_VIEW_DEPENDENCY',
                            'Geometry contains a dependency outside the verified snapshot.')
                if path.suffix.lower() == '.glb':
                    # GLB loading remains embedded-only in the browser; external
                    # dependencies require conversion by the authoritative importer.
                    require(len(imported.source_files) == 1, 'EDITOR3D_VIEW_DEPENDENCY',
                            'Embed external GLB resources before browser preview.')
                    assets[identifier] = dict(format='glb', data=base64.b64encode(path.read_bytes()).decode('ascii'))
                else:
                    geometry = imported.geometry
                    images = []
                    for texture in geometry.textures:
                        image = geometry.images[texture['source']]
                        images.append('data:' + image.mime_type + ';base64,' + base64.b64encode(image.data).decode('ascii'))
                    meshes = []
                    for mesh in geometry.meshes:
                        meshes.append(dict(id=mesh.id, name=mesh.name, positions=mesh.positions.tolist(),
                            triangles=mesh.triangles.tolist(), material=mesh.material,
                            normals=mesh.attributes.get('NORMAL').tolist() if 'NORMAL' in mesh.attributes else None,
                            uv=mesh.attributes.get('TEXCOORD_0').tolist() if 'TEXCOORD_0' in mesh.attributes else None))
                    assets[identifier] = dict(format='geometry', meshes=meshes,
                                             materials=geometry.materials, images=images)
            except (RigError, ValueError, OSError) as error:
                assets[identifier] = dict(format='unavailable', message=str(error))
    result['viewport_assets'] = assets
    result['preview'] = dict(backend='three-0.180.0', offline_render=False,
                             source_sha256=snapshot['sha256'])
    encoded = json.dumps(result, allow_nan=False, separators=(',', ':')).encode()
    require(len(encoded) <= MAX_PREVIEW_BYTES * 3, 'EDITOR3D_VIEW_LIMIT', 'Encoded preview exceeds its budget.')
    return result


class EditorServer(HTTPServer):
    """Bounded loopback HTTP service; the supplied repository owns transactions."""
    def __init__(self, repository, *, port=0, token=None, workers=4, routes=None):
        require(isinstance(workers, int) and 1 <= workers <= 8, 'EDITOR3D_VIEW_WORKERS', 'Use 1–8 workers.')
        self.repository = repository
        self.routes = {}
        # Built-in optional inspector extension selects fixed trusted handlers.
        if (ROOT / "scripts/editor3d_inspectors.py").is_file():
            from editor3d_inspectors import inspector_routes
            self.routes.update(inspector_routes())
        self.routes.update(routes or {})
        self.upload_directory = tempfile.TemporaryDirectory(prefix="hatch-editor-uploads-")
        self.uploads = {}
        self.upload_lock = threading.Lock()
        self.token = token or secrets.token_urlsafe(32)
        require(isinstance(self.token, str) and len(self.token) >= 24,
                'EDITOR3D_VIEW_TOKEN', 'Use a bearer token of at least 24 characters.')
        self.slots = threading.BoundedSemaphore(workers)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='editor-http')
        self.cache = OrderedDict()
        self.cache_lock = threading.Lock()
        super().__init__(('127.0.0.1', port), EditorHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            self.executor.submit(self._handle, request, client_address)
        except BaseException:
            self.slots.release()
            request.close()
            raise

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self.slots.release()

    def snapshot(self):
        snapshot = self.repository.snapshot()
        key = (snapshot['revision'], snapshot['sha256'])
        with self.cache_lock:
            if key not in self.cache:
                self.cache[key] = preview_snapshot(self.repository, snapshot)
                while len(self.cache) > 2:
                    self.cache.popitem(last=False)
            return self.cache[key]

    def upload_path(self, identifier):
        """Resolve a live service upload by opaque identity, never a caller path."""
        with self.upload_lock:
            require(identifier in self.uploads, 'EDITOR3D_VIEW_UPLOAD', 'Unknown upload token.')
            record = self.uploads[identifier]
        path = record['path']
        require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode),
                'EDITOR3D_VIEW_UPLOAD', 'Uploaded source is no longer a regular file.')
        payload = path.read_bytes()
        require(len(payload) == record['bytes'] and hashlib.sha256(payload).hexdigest() == record['sha256'],
                'EDITOR3D_VIEW_UPLOAD', 'Uploaded source bytes changed.')
        return path

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_port}/#{self.token}'

    def server_close(self):
        super().server_close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.upload_directory.cleanup()


class EditorHandler(BaseHTTPRequestHandler):
    server_version = 'HatchEditor/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass

    def respond(self, status, payload, media='application/json'):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', media)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def checked_header(self, name, required=True):
        values = self.headers.get_all(name, [])
        require(len(values) == 1 if required else len(values) <= 1,
                'EDITOR3D_VIEW_HEADER', 'Missing or duplicate ' + name + ' header.')
        return values[0] if values else None

    def body(self):
        require(self.checked_header('Content-Type').split(';')[0].strip().lower() == 'application/json',
                'EDITOR3D_VIEW_CONTENT_TYPE', 'Use application/json.')
        require(self.headers.get('Transfer-Encoding') is None, 'EDITOR3D_VIEW_BODY', 'Chunked bodies are unsupported.')
        length = self.checked_header('Content-Length')
        require(length.isascii() and length.isdigit() and 0 < int(length) <= MAX_BODY,
                'EDITOR3D_VIEW_BODY', 'Content-Length must be 1 byte to 4 MiB.')
        payload = self.rfile.read(int(length))
        require(len(payload) == int(length), 'EDITOR3D_VIEW_BODY', 'Request body was incomplete.')
        try:
            value = decode_json(payload.decode('utf-8', errors='strict'))
        except UnicodeError as exc:
            raise RigError('EDITOR3D_VIEW_BODY', 'Use UTF-8 JSON.') from exc
        require(isinstance(value, dict), 'EDITOR3D_VIEW_BODY', 'Expected a JSON object.')
        return value

    def run(self):
        try:
            origin = f'http://127.0.0.1:{self.server.server_port}'
            require(self.checked_header('Host') == origin.removeprefix('http://'),
                    'EDITOR3D_VIEW_HOST', 'Use the exact loopback service origin.')
            request_origin = self.checked_header('Origin', required=False)
            require(request_origin in (None, origin), 'EDITOR3D_VIEW_ORIGIN', 'Cross-origin requests are refused.')
            route = urlsplit(self.path)
            require(not route.query and not route.fragment, 'EDITOR3D_VIEW_PATH', 'Queries are unsupported.')
            if route.path.startswith('/api/'):
                auth = self.checked_header('Authorization')
                require(hmac.compare_digest(auth.encode('utf-8'), ('Bearer ' + self.server.token).encode('utf-8')),
                        'EDITOR3D_VIEW_AUTH', 'A valid local bearer token is required.')
                return self.api(route.path[5:])
            require(self.command == 'GET', 'EDITOR3D_VIEW_METHOD', 'Static assets are read-only.')
            if route.path in ('/', '/index.html'):
                path = ROOT / 'assets/editor3d/index.html'
            elif route.path.startswith('/editor/'):
                relative = route.path.removeprefix('/editor/')
                require(relative in ('editor.js', 'editor.css', 'TransformControls.js', 'asset-browser.js', 'inspectors.js', 'timeline.js', 'render-panel.js'), 'EDITOR3D_VIEW_PATH', 'Unknown editor asset.')
                path = ROOT / 'assets/editor3d' / relative
            elif route.path.startswith('/vendor/'):
                path = safe_path(ROOT / 'assets/rig-viewer/vendor', route.path.removeprefix('/vendor/'), exists=True)
                require(path.suffix == '.js', 'EDITOR3D_VIEW_PATH', 'Unknown vendor asset.')
            else:
                raise RigError('EDITOR3D_VIEW_PATH', 'Unknown local route.')
            payload = path.read_bytes()
            if path.name == 'index.html':
                extensions = []
                for filename, symbol, container in (('asset-browser.js','mountAssetBrowser','asset-browser'),
                        ('inspectors.js','mountInspectors','native-inspectors'),
                        ('timeline.js','mountTimeline','timeline'),('render-panel.js','mountRenderPanel','render-panel')):
                    if (ROOT / 'assets/editor3d' / filename).is_file():
                        extensions.append(f"const {{{symbol}}} = await import('/editor/{filename}'); window.hatchEditor.registerExtension({symbol}, document.getElementById('{container}'));")
                if extensions:
                    boot = "<script type='module'>await import('/editor/editor.js');" + ''.join(extensions) + "</script>"
                    payload = payload.replace(b'</body>', boot.encode() + b'</body>')
            self.respond(200, payload, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        except (RigError, ValueError, OSError, TypeError) as exc:
            report = exc.report if isinstance(exc, RigError) else dict(code='EDITOR3D_VIEW_INPUT', message=str(exc))
            status = 401 if report['code'] == 'EDITOR3D_VIEW_AUTH' else 409 if any(word in report['code'] for word in ('STALE', 'CONFLICT')) else 400
            self.respond(status, dict(error=report))

    def upload(self):
        require(self.checked_header('Content-Type') == 'application/octet-stream',
                'EDITOR3D_VIEW_CONTENT_TYPE', 'Upload raw bytes as application/octet-stream.')
        require(self.headers.get('Transfer-Encoding') is None, 'EDITOR3D_VIEW_BODY', 'Chunked bodies are unsupported.')
        name = unquote(self.checked_header('X-Filename'), errors='strict')
        require(name and '/' not in name and '\\' not in name,
                'EDITOR3D_VIEW_UPLOAD', 'Upload name must be a portable basename.')
        safe_path(Path(self.server.upload_directory.name), name)
        length = self.checked_header('Content-Length')
        require(length.isascii() and length.isdigit() and 0 < int(length) <= MAX_PREVIEW_BYTES,
                'EDITOR3D_VIEW_UPLOAD', 'Upload must contain 1 byte to 64 MiB.')
        identifier = uuid.uuid4().hex
        folder = Path(self.server.upload_directory.name) / identifier
        folder.mkdir(mode=0o700)
        path = folder / name
        digest = hashlib.sha256()
        remaining = int(length)
        try:
            with path.open('xb') as stream:
                while remaining:
                    payload = self.rfile.read(min(remaining, 1024 * 1024))
                    require(payload, 'EDITOR3D_VIEW_UPLOAD', 'Upload ended before Content-Length.')
                    stream.write(payload); digest.update(payload); remaining -= len(payload)
            record = dict(path=path, name=name, bytes=int(length), sha256=digest.hexdigest())
            with self.server.upload_lock:
                require(len(self.server.uploads) < 64, 'EDITOR3D_VIEW_UPLOAD', 'Upload count limit reached; restart the service.')
                require(sum(item['bytes'] for item in self.server.uploads.values()) + record['bytes'] <= 256*1024*1024,
                        'EDITOR3D_VIEW_UPLOAD', 'Upload storage budget reached; restart the service.')
                self.server.uploads[identifier] = record
        except BaseException:
            path.unlink(missing_ok=True)
            folder.rmdir()
            raise
        return self.respond(200, dict(upload_id=identifier, **{key:record[key] for key in ('name', 'bytes', 'sha256')}))

    def api(self, route):
        if self.command == 'POST' and route == 'uploads':
            return self.upload()
        if self.command == 'POST' and route in self.server.routes:
            return self.respond(200, self.server.routes[route](self.server, self.body()))
        if self.command == 'GET' and route == 'snapshot':
            return self.respond(200, self.server.snapshot())
        require(self.command == 'POST' and route in ('commands', 'undo', 'redo'),
                'EDITOR3D_VIEW_METHOD', 'Unsupported editor operation.')
        body = self.body()
        expected = {'expected_revision', 'expected_sha256', 'transaction_id'} | ({'commands'} if route == 'commands' else set())
        require(set(body) == expected, 'EDITOR3D_VIEW_FIELDS', 'Unexpected or missing command envelope fields.')
        if route == 'commands':
            self.server.repository.apply(body['expected_revision'], body['commands'], body['transaction_id'],
                                         expected_sha256=body['expected_sha256'])
        else:
            getattr(self.server.repository, route)(body['expected_revision'], body['transaction_id'],
                                                    expected_sha256=body['expected_sha256'])
        return self.respond(200, self.server.snapshot())

    do_GET = run
    do_POST = run


def main(argv=None):
    import argparse
    from editor3d_repository import EditorRepository
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', type=Path)
    parser.add_argument('--port', type=int, default=8767)
    args = parser.parse_args(argv)
    server = EditorServer(EditorRepository(args.repository), port=args.port)
    print(json.dumps(dict(status='listening', url=server.url, host='127.0.0.1')), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
