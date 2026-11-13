"""Optional authenticated loopback API; paths and work budgets stay local and bounded."""
from __future__ import annotations
from functools import partial
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
import uuid
from rig_errors import RigError,decode_json,require,safe_path,sha256,slug


MAX_TOKEN_CHARACTERS=4096


def _installed_template(identifier):
    """Resolve an exact installed public ID without passing user paths to the loader."""
    from rig_templates import ROOT,load_rig_template
    require(isinstance(identifier,str),
            'RIG_API_TEMPLATE','Use an exact installed template ID, not a template object or file path.')
    installed={}
    for path in sorted(ROOT.glob('*.json')):
        if not path.is_file() or path.is_symlink():continue
        canonical=load_rig_template(path)['id']
        require(canonical not in installed,'RIG_API_TEMPLATE','Installed template IDs must be unambiguous.')
        installed[canonical]=path.resolve()
    require(identifier in installed,'RIG_API_TEMPLATE','Use an exact installed template ID, not a template object or file path.')
    return str(installed[identifier])


class RigServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,root,intake,*,port=0,token=None,workers=2):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.intake=Path(intake).resolve();require(self.intake.is_dir(),'RIG_API_INTAKE','Intake must be an existing directory.')
        self.token=secrets.token_urlsafe(32) if token is None else token
        require(isinstance(self.token,str) and 24<=len(self.token)<=MAX_TOKEN_CHARACTERS
                and all(0x21<=ord(character)<=0x7e for character in self.token),
                'RIG_API_TOKEN','Use a printable ASCII token of 24–4096 characters without whitespace.')
        require(1<=workers<=4,'RIG_API_WORKERS','Use 1–4 bounded workers.');self.budget=threading.BoundedSemaphore(workers)
        super().__init__(('127.0.0.1',port),RigHandler)


class RigHandler(BaseHTTPRequestHandler):
    server_version='HatchRig/1'
    def setup(self):
        super().setup();self.connection.settimeout(10)
    def log_message(self,*args):pass
    def _respond(self,code,payload):
        encoded=json.dumps(payload,allow_nan=False).encode();self.send_response(code)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(encoded)
    def _body(self):
        require(self.headers.get('Content-Type','').split(';')[0]=='application/json','RIG_API_CONTENT_TYPE','Use application/json.')
        raw=self.headers.get('Content-Length','');require(raw.isdigit(),'RIG_API_LENGTH','A bounded Content-Length is required.')
        length=int(raw);require(0<length<=4*1024*1024,'RIG_API_BODY','JSON body exceeds the four MiB limit.')
        value=decode_json(self.rfile.read(length));require(isinstance(value,dict),'RIG_API_BODY','Request must be a JSON object.');return value
    def _root(self,identifier):
        require(identifier==slug(identifier) and len(identifier)<=128,'RIG_API_ID','Invalid job identifier.')
        root=safe_path(self.server.root,identifier);require(root.is_dir(),'RIG_API_NOT_FOUND','Job does not exist.');return root
    def _run(self):
        authorized=self.headers.get('Authorization','')
        valid_authorization=(isinstance(authorized,str) and len(authorized)<=MAX_TOKEN_CHARACTERS+7
                             and authorized.isascii() and hmac.compare_digest(authorized,'Bearer '+self.server.token))
        if not valid_authorization:
            self._respond(401,dict(ok=False,error=dict(code='RIG_API_AUTH',message='A valid local bearer token is required.')))
            return
        origin=self.headers.get('Origin')
        allowed={f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}
        if origin and origin not in allowed:self._respond(403,dict(ok=False,error=dict(code='RIG_API_ORIGIN',message='Cross-origin API calls are refused.')));return
        if not self.server.budget.acquire(blocking=False):self._respond(429,dict(ok=False,error=dict(code='RIG_API_BUSY',message='Worker budget is occupied; retry later.')));return
        try:
            result=self._dispatch();self._respond(200,result)
        except RigError as exc:
            code=404 if exc.report['code']=='RIG_API_NOT_FOUND' else 409 if 'STALE' in exc.report['code'] or 'LOCK' in exc.report['code'] else 400
            self._respond(code,dict(ok=False,error=exc.report))
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self._respond(400,dict(ok=False,error=RigError('RIG_API_INPUT',str(exc)).report))
        finally:self.server.budget.release()
    def _dispatch(self):
        from rig_jobs import create_run,status
        from rig_models import load_rig
        from rig_operations import mutate,pose_operation,solve_operation,export_operation
        route=urlsplit(self.path);require(not route.query,'RIG_API_ROUTE','Queries are not accepted on these JSON endpoints.')
        parts=route.path.strip('/').split('/');require(len(parts)>=2 and parts[0]=='v1','RIG_API_NOT_FOUND','Unknown endpoint.')
        if self.command=='POST' and parts==['v1','rig-jobs']:
            body=self._body();require(set(body)<={'model','id','scale','fixture','origin','attribution'} and 'model'in body,'RIG_API_SCHEMA','Import fields are model, id, scale, fixture, origin and attribution.')
            model=safe_path(self.server.intake,body['model'],exists=True);identifier=body.get('id','rig-'+uuid.uuid4().hex[:12])
            require(identifier==slug(identifier),'RIG_API_ID','Use a portable job ID.')
            root=safe_path(self.server.root,identifier)
            create_run(model,root,identifier=identifier,scale=body.get('scale',1.),fixture=body.get('fixture',False),
                       origin=body.get('origin','import'),attribution=body.get('attribution','Local API import.'))
            return status(root)
        require(len(parts) in (3,4) and parts[1] in ('rig-jobs','rigs'),'RIG_API_NOT_FOUND','Unknown endpoint.')
        root=self._root(parts[2]);rig=load_rig(root)
        if self.command=='GET' and len(parts)==3:return status(root) if parts[1]=='rig-jobs' else rig.manifest
        if self.command=='GET' and len(parts)==4 and parts[3]=='artifacts':
            return dict(artifacts=[dict(path=p.relative_to(root).as_posix(),bytes=p.stat().st_size,sha256=sha256(p)) for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()])
        body=self._body()
        if self.command=='PATCH' and parts[1]=='rigs' and len(parts)==3:return mutate(root,'correct',dict(correction=body))
        require(self.command=='POST' and len(parts)==4,'RIG_API_NOT_FOUND','Unsupported route or method.')
        operation=parts[3]
        aliases={'generate':'auto','skin':'weights','test-motion':'test-motion','validate':'validate','analyze':'analyze','state-map':'state-map','animate':'animate','configure':'configure'}
        if operation in aliases:
            # Template paths are deliberately restricted to installed IDs in HTTP.
            if operation=='generate':body['template']=_installed_template(body.get('template','humanoid'))
            return mutate(root,aliases[operation],body)
        if operation=='pose':return pose_operation(rig,body)
        if operation=='solve-ik':return solve_operation(rig,body)
        if operation=='review':return mutate(root,'review',dict(receipt=body))
        if operation=='retarget':
            require('source'in body,'RIG_API_SCHEMA','Retarget source job ID is required.');body['source']=str(self._root(body['source']));return mutate(root,'retarget',body)
        if operation=='preview':
            from rig_viewer import create_review_ui
            require(set(body)<={'destination'},'RIG_API_SCHEMA','Preview accepts only a relative destination.')
            destination=safe_path(root,body.get('destination','review-ui-r'+str(rig.manifest['revision'])))
            return create_review_ui(rig,destination)
        if operation=='export':
            require(set(body)<={'destination','format','clip','fps'},'RIG_API_SCHEMA','Unexpected export field.')
            require('destination'in body,'RIG_API_SCHEMA','Export destination is required.')
            body['destination']=str(safe_path(root,body['destination']));return export_operation(rig,body)
        raise RigError('RIG_API_NOT_FOUND','Unknown endpoint.')
    do_GET=_run
    do_POST=_run
    do_PATCH=_run


def serve(root,intake,*,port=8766,token=None):
    server=RigServer(root,intake,port=port,token=token)
    print(json.dumps(dict(status='listening',url=f'http://127.0.0.1:{server.server_port}',token=server.token,workers=2)),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return dict(status='stopped')
