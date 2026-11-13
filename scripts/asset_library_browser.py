"""Authenticated offline asset inspection over exact canonical library records.

The browser owns no import, version, export or job authority. Trusted callers may
attach their existing managed-operation dispatcher through the submit protocol.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Protocol
from urllib.parse import urlsplit

from asset_library_contracts import canonical_bytes, validate
from rig_errors import RigError, decode_json

MAX_BODY = 65536
MAX_MODEL = 32 * 1024**2
MAX_RESPONSE = 4 * 1024**2
ACTIONS = frozenset(('import', 'preview', 'thumbnail', 'turntable', 'export', 'index', 'bundle.create', 'bundle.restore', 'version', 'replace', 'repair'))
UI = Path(__file__).resolve().parents[1] / 'assets' / 'asset-browser'
VENDOR = Path(__file__).resolve().parents[1] / 'assets' / 'rig-viewer' / 'vendor'
IMPORTMAP = '{"imports":{"three":"/vendor/build/three.module.min.js"}}'
CSP = ("default-src 'none'; script-src 'self' 'sha256-" +
       base64.b64encode(hashlib.sha256(IMPORTMAP.encode()).digest()).decode() +
       "'; style-src 'self'; connect-src 'self' blob: data:; img-src 'self' blob: data:; "
       "worker-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def fail(code, message):
    raise RigError('LIBRARY_BROWSER_' + code, message, stage='asset-browser')


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        fail('SCHEMA', 'Unexpected or missing request fields.')
    return value


class ManagedOperations(Protocol):
    """Trusted native job authority; submit must validate its operation payload."""
    def submit(self, operation: str, request: dict) -> dict: ...
    def list_jobs(self) -> list: ...
    def job_status(self, identifier: str) -> dict: ...
    def cancel_job(self, identifier: str) -> dict: ...


class AssetBrowserServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, libraries, index, *, port=0, token=None, operations=None):
        if not isinstance(libraries, dict) or not 1 <= len(libraries) <= 32:
            fail('LIBRARIES', 'Configure one to 32 explicit library aliases.')
        if index.libraries != libraries:
            fail('LIBRARIES', 'Search and browser must share the same explicit libraries.')
        self.libraries = dict(libraries)
        self.index = index
        from asset_browser_details import BrowserDetails
        self.details=BrowserDetails(self.libraries,index)
        self.operations = operations
        self.token = token or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or not 24 <= len(self.token) <= 256 or not self.token.isascii() or not self.token.isprintable():
            fail('TOKEN', 'Use a printable ASCII bearer token of 24–256 characters.')
        self.budget = threading.BoundedSemaphore(2)
        self.connection_budget = threading.BoundedSemaphore(8)
        super().__init__(('127.0.0.1', port), AssetBrowserHandler)

    def process_request(self, request, client_address):
        # Admission happens before ThreadingMixIn allocates a handler thread,
        # including for sockets stalled before completing their HTTP headers.
        if not self.connection_budget.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_budget.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_budget.release()

    def location(self, value):
        fields(value, ('library', 'reference'), ('library', 'reference'))
        if not isinstance(value['library'], str) or value['library'] not in self.libraries:
            fail('LIBRARY', 'Unknown configured library alias.')
        validate(value['reference'], 'AssetRef')
        return self.libraries[value['library']], value['reference']

    def capabilities(self):
        actions=sorted(set(getattr(self.operations,'supported_operations',ACTIONS)) & ACTIONS) if self.operations else []
        context={}
        for name,attribute in (('intake_root','intake'),('output_root','output')):
            value=getattr(self.operations,attribute,None)
            if isinstance(value,Path):context[name]=str(value)
        return dict(schema_version=1,libraries=sorted(self.libraries),managed_operations=actions,
                    managed_context=context,offline_dependencies=True,loopback_only=True,
                    limits=dict(body_bytes=MAX_BODY,model_bytes=MAX_MODEL,vertices=100000,
                                triangles=200000,meshes=256,connections=8,workers=2))

    def inspect(self, location):
        library, reference = self.location(location)
        manifest = library.resolve(reference)
        versions = [r for r in library.list_references() if r['id'] == reference['id']]
        versions_owner=self.index.versions.get(location['library'])
        collections=versions_owner.collections(reference) if versions_owner else None
        actions=sorted(set(getattr(self.operations,'supported_operations',ACTIONS)) & ACTIONS) if self.operations else []
        return dict(schema_version=1, location=location, manifest=manifest, collections=collections,
                    annotations=library.annotations(reference), versions=versions,
                    completeness='payload-verified', visual_review='not-granted-by-browser',
                    managed_operations=actions)

    def model(self, location, logical_path):
        library, reference = self.location(location)
        manifest = library.resolve(reference)
        file = next((f for f in manifest['files'] if f['path'] == logical_path), None)
        if not file or file['format'] != 'glb' or file['bytes'] > MAX_MODEL:
            fail('MODEL', 'Select an inventoried GLB of at most 32 MiB; external glTF is not loaded by this viewer.')
        payload = library.read_file(reference, logical_path)
        # The existing importer independently checks actual geometry, accessor,
        # hierarchy, texture, skeleton and animation bounds before browser decode.
        from rig_contracts import DEFAULT_LIMITS
        from rig_gltf import read_gltf
        from rig_import import load_character_model
        limits = dict(DEFAULT_LIMITS, source_bytes=MAX_MODEL, dependency_bytes=MAX_MODEL,
                      vertices=100000, triangles=200000, memory_bytes=128*1024**2,
                      keyframes=25000, clips=32)
        with tempfile.TemporaryDirectory(prefix='hatch-browser-model-') as tmp:
            snapshot = Path(tmp) / 'model.glb'
            snapshot.write_bytes(payload)
            document = read_gltf(snapshot, limits)
            for group in ('buffers', 'images'):
                for item in document.json.get(group, []):
                    if 'uri' in item:
                        fail('MODEL_URI', 'Browser GLB must embed all resources in buffer views; convert through the native importer.')
            loaded=load_character_model(snapshot, limits=limits)
            if len(loaded.geometry.meshes)>256 or len(loaded.geometry.materials)>256:
                fail('MODEL_LIMIT','Browser inspection supports at most 256 meshes/materials; use the native workflow for larger assets.')
        return payload


class AssetBrowserHandler(BaseHTTPRequestHandler):
    server_version = 'HatchAssets/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def respond(self, status, payload, mime='application/json'):
        if mime == 'application/json':
            payload = canonical_bytes(payload)
            if len(payload)>MAX_RESPONSE:
                status=413
                payload=canonical_bytes(dict(error={'code':'LIBRARY_BROWSER_RESPONSE_LIMIT','message':'Native report exceeds the four MiB browser response budget; inspect a narrower scope with the native CLI.'}))
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            fail('CONTENT_TYPE', 'Use application/json.')
        raw = self.headers.get('Content-Length', '')
        if not raw.isdecimal() or not 0 < int(raw) <= MAX_BODY or self.headers.get('Transfer-Encoding'):
            fail('BODY', 'Use a bounded Content-Length of at most 64 KiB.')
        payload = self.rfile.read(int(raw))
        if len(payload) != int(raw):
            fail('BODY', 'Truncated request body.')
        value = decode_json(payload)
        if not isinstance(value, dict):
            fail('SCHEMA', 'Request must be an object.')
        return value

    def run(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                fail('ROUTE', 'Queries and fragments are not accepted.')
            allowed_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed_hosts:
                self.respond(403, dict(error={'code':'LIBRARY_BROWSER_HOST','message':'Unexpected Host.'})); return
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + host for host in allowed_hosts}:
                self.respond(403, dict(error={'code':'LIBRARY_BROWSER_ORIGIN','message':'Cross-origin request refused.'})); return
            if not parsed.path.startswith('/api/'):
                if self.command != 'GET':
                    fail('ROUTE', 'Static resources support GET only.')
                static = {'/':(UI/'index.html','text/html; charset=utf-8'),
                          '/app.js':(UI/'app.js','text/javascript'),
                          '/inspector-tools.js':(UI/'inspector-tools.js','text/javascript'),
                          '/details-view.js':(UI/'details-view.js','text/javascript'),
                          '/style.css':(UI/'style.css','text/css')}
                if parsed.path in static:
                    path, mime = static[parsed.path]
                elif parsed.path in ('/vendor/build/three.module.min.js', '/vendor/build/three.core.min.js',
                                     '/vendor/examples/jsm/loaders/GLTFLoader.js',
                                     '/vendor/examples/jsm/controls/OrbitControls.js',
                                     '/vendor/examples/jsm/utils/BufferGeometryUtils.js'):
                    path, mime = VENDOR / parsed.path.removeprefix('/vendor/'), 'text/javascript'
                else:
                    self.respond(404, dict(error={'code':'LIBRARY_BROWSER_NOT_FOUND','message':'Unknown resource.'})); return
                self.respond(200, path.read_bytes(), mime); return
            authorized = self.headers.get('Authorization', '')
            if not hmac.compare_digest(authorized, 'Bearer ' + self.server.token):
                self.respond(401, dict(error={'code':'LIBRARY_BROWSER_AUTH','message':'A valid local bearer token is required.'})); return
            if not self.server.budget.acquire(blocking=False):
                self.respond(429, dict(error={'code':'LIBRARY_BROWSER_BUSY','message':'Browser work budget occupied.'})); return
            try:
                self.dispatch(parsed.path)
            finally:
                self.server.budget.release()
        except RigError as exc:
            self.respond(409 if 'STALE' in exc.report['code'] or 'CONFLICT' in exc.report['code'] else 400, dict(error=exc.report))
        except ImportError:
            self.respond(503, dict(error={'code':'LIBRARY_BROWSER_DEPENDENCY','message':'Install optional requirements-rig.txt for model inspection.'}))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.respond(400, dict(error={'code':'LIBRARY_BROWSER_REQUEST','message':str(exc)[:512]}))

    def dispatch(self, path):
        if self.command != 'POST':
            fail('ROUTE', 'JSON endpoints support POST only.')
        body = self.body()
        if path == '/api/report':
            fields(body,('location','report'),('location','report'))
            self.server.location(body['location'])
            name=body['report']
            if name not in ('dependency_graph','usage','validation','exports','diagnose','cleanup_analysis'):
                fail('SCHEMA','Unknown read-only native report.')
            value=body['location']['library'] if name=='cleanup_analysis' else body['location']
            self.respond(200,getattr(self.server.details,name)(value));return
        if path == '/api/compare':
            fields(body,('location','other'),('location','other'))
            self.server.location(body['location'])
            self.respond(200,self.server.details.compare(body['location'],body['other']));return
        if path == '/api/thumbnail':
            self.server.location(body)
            result=self.server.details.thumbnail(body)
            if result is None:self.respond(204,b'','application/octet-stream')
            else:self.respond(200,result[0],result[1])
            return
        if path == '/api/capabilities':
            fields(body,())
            self.respond(200,self.server.capabilities());return
        if path == '/api/search':
            fields(body, ('query', 'filters', 'sort', 'limit', 'cursor'))
            self.respond(200, self.server.index.search(**body)); return
        if path == '/api/inspect':
            self.respond(200, self.server.inspect(body)); return
        if path == '/api/model':
            fields(body, ('location', 'path'), ('location','path'))
            self.respond(200, self.server.model(body['location'], body['path']), 'model/gltf-binary'); return
        if path == '/api/collections':
            fields(body, ('location','expected_revision','collections'), ('location','expected_revision','collections'))
            _, reference=self.server.location(body['location'])
            owner=self.server.index.versions.get(body['location']['library'])
            if owner is None:
                fail('UNAVAILABLE','Connect the native lifecycle authority for collections.')
            result=owner.set_collections(reference,expected_revision=body['expected_revision'],collections=body['collections'])
            try:
                self.server.index.rebuild()
                indexed=True
            except RigError:
                indexed=False
            self.respond(200,dict(collections=result,index_current=indexed));return
        if path == '/api/annotations':
            fields(body, ('location', 'expected_revision', 'tags', 'favorite', 'note'),
                   ('location', 'expected_revision', 'tags', 'favorite', 'note'))
            library, reference = self.server.location(body['location'])
            result = library.update_annotations(reference, **{k:v for k,v in body.items() if k != 'location'})
            # Search is derived; a rebuild failure never erases a committed edit.
            try:
                self.server.index.rebuild()
                indexed = True
            except RigError:
                indexed = False
            self.respond(200, dict(annotations=result, index_current=indexed)); return
        if path in ('/api/jobs/list', '/api/jobs/status', '/api/jobs/cancel'):
            if self.server.operations is None:
                fail('UNAVAILABLE', 'Connect the native managed-job authority first.')
            if path == '/api/jobs/list':
                fields(body, ())
                result = self.server.operations.list_jobs()
            else:
                fields(body, ('id',), ('id',))
                identifier = body['id']
                if not isinstance(identifier,str) or not 1 <= len(identifier) <= 128 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in identifier):
                    fail('SCHEMA', 'Use a portable job identifier.')
                result = (self.server.operations.cancel_job(identifier) if path.endswith('/cancel')
                          else self.server.operations.job_status(identifier))
            self.respond(200, result); return
        if path.startswith('/api/actions/'):
            operation = path.removeprefix('/api/actions/')
            if operation not in ACTIONS:
                fail('ROUTE', 'Unknown managed action.')
            if self.server.operations is None:
                fail('UNAVAILABLE', 'Connect the native managed-job authority before using this operation.')
            self.respond(200, self.server.operations.submit(operation, body)); return
        fail('ROUTE', 'Unknown JSON endpoint.')

    do_GET = run
    do_POST = run


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True)
    parser.add_argument('--index', required=True)
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--project', action='append',default=[],help='Explicit native project-binding ID for usage reports.')
    parser.add_argument('--jobs-root', help='Enable managed jobs in this explicit output directory.')
    parser.add_argument('--intake-root', help='Existing authorized import/package intake directory.')
    parser.add_argument('--output-root', help='Existing authorized managed output directory.')
    args=parser.parse_args(argv)
    from asset_library import AssetLibrary
    from asset_library_search import SearchIndex
    from asset_library_versions import AssetVersions, ScopedResolver
    libraries={'local': AssetLibrary(args.library)}
    index=SearchIndex(args.index,libraries,versions={"local":AssetVersions(libraries["local"])},
                      usage_resolver=ScopedResolver(list(libraries.items()),project_library="local") if args.project else None,
                      project_ids=args.project)
    roots=(args.jobs_root,args.intake_root,args.output_root)
    if any(roots) and not all(roots):
        parser.error('Managed jobs require --jobs-root, --intake-root and --output-root together.')
    operations=None
    if all(roots):
        from asset_library_operations import AssetOperations, ScopedAssetOperations
        operations=ScopedAssetOperations(AssetOperations(libraries['local'],jobs_root=args.jobs_root,project_ids=args.project),
                                        intake_root=args.intake_root,output_root=args.output_root)
    server=AssetBrowserServer(libraries,index,port=args.port,operations=operations)
    print(json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}/',token=server.token)),flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__':
    main()
