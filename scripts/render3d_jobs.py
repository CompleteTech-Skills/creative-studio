"""Durable Render3D frame jobs, bounded claims and independently verified cache.

SQLite owns lifecycle state; immutable cache directories own verified bytes.
Expired heartbeats never authorize taking work from a live owner process.
"""
from __future__ import annotations
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from fractions import Fraction
import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

from atomic_publish import _rename_no_replace, pinned_directory_fd, private_stage_fd, windows_private_stage
from rig_errors import (RigError, atomic_bytes, atomic_json, decode_json, directory_capability,
                        object_hash, open_payload, open_payload_at, safe_path, sha256)
from render3d_contracts import Render3DError, require, validate, make_frame_spec, frame_times, rational, open_regular, MAX_RESULT_JSON_BYTES

VERSION = 1
TERMINAL = ('complete', 'cancelled', 'failed')
RETRYABLE = ('timeout','worker-crash','device-initialization')


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _metadata(path, expected_digest=None):
    """Decode bounded metadata from the same admitted regular descriptor."""
    limit=32*1024**2
    try:
        with open_regular(path,limit) as stream:
            data=stream.read(limit+1)
            digest=hashlib.sha256(data).hexdigest()
            if expected_digest is not None:
                require(digest==expected_digest,
                        'stale-cache','Published metadata differs from the prepared frame.')
            return decode_json(data,max_bytes=limit),digest
    except (OSError,RigError) as exc:
        raise Render3DError('invalid-input',str(exc),path=path) from exc


def _alive(pid):
    if type(pid) is not int or not 1 <= pid <= 0x7fffffff:
        return True
    if os.name == 'nt':
        # A zero-time wait on a synchronization handle only observes process exit.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: absent PID
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: exited
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # Uncertain ownership must not authorize stealing an active process.
        return True


def _copy_artifact(source, target, ref):
    """Bound growing inputs and hash the exact bytes copied, not a later read."""
    target.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(source,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_BINARY',0))
    digest=hashlib.sha256();count=0
    with os.fdopen(fd,'rb') as inp,target.open('xb') as out:
        require(stat.S_ISREG(os.fstat(inp.fileno()).st_mode),'invalid-input','Artifact is not a regular file.')
        while True:
            chunk=inp.read(min(1024*1024,ref['bytes']-count+1))
            if not chunk:break
            count+=len(chunk)
            require(count<=ref['bytes'],'corrupt-output','Artifact grew during publication.')
            digest.update(chunk);out.write(chunk)
        out.flush();os.fsync(out.fileno())
    require(count==ref['bytes'] and digest.hexdigest()==ref['sha256'],
            'corrupt-output','Artifact changed during publication.')


def _identity(info):
    return _dump([info.st_dev,info.st_ino])


def _delivery_files(result):
    return [image['file'] for image in result['images']]+result['logs']+[result['manifest'],result['verification']['evidence']]


def _check_artifact(stream,info,ref,category):
    require(info.st_size==ref['bytes'],category,'Published artifact size differs.')
    digest=hashlib.sha256()
    remaining=ref['bytes']
    while remaining:
        chunk=stream.read(min(1024**2,remaining))
        if not chunk:break
        remaining-=len(chunk)
        digest.update(chunk)
    require(not remaining and not stream.read(1) and digest.hexdigest()==ref['sha256'],
            category,'Published artifact differs from its receipt.')


def _check_delivery_inventory(directory,refs):
    # Stop at the first unlisted entry, including an empty directory. Open only
    # expected descendants from the same directory authority used for readback.
    expected={}
    for path in ['result.json',*(ref['path'] for ref in refs)]:
        parts=path.split('/')
        for index,name in enumerate(parts):
            expected.setdefault(tuple(parts[:index]),{})[name]=index+1<len(parts)
    def inspect(parent,parts):
        children=expected[parts]
        with os.scandir(parent.path if os.name=='nt' else parent) as entries:
            for entry in entries:
                require(entry.name in children and
                        (entry.is_dir(follow_symlinks=False) if children[entry.name] else entry.is_file(follow_symlinks=False)),
                        'ownership','Published delivery inventory differs.')
        for name,is_dir in children.items():
            if is_dir:
                with ExitStack() as stack:
                    if os.name=='nt':
                        child=stack.enter_context(parent.pin_child(name))
                    else:
                        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                        stack.callback(os.close,child)
                    inspect(child,(*parts,name))
    inspect(directory,())


@contextmanager
def _delivery_parent(path):
    ancestor=path
    while not ancestor.exists() and ancestor!=ancestor.parent:
        ancestor=ancestor.parent
    with ExitStack() as stack:
        if os.name=='nt':
            from atomic_windows import pinned_directory
            parent=stack.enter_context(pinned_directory(ancestor))
            for name in path.relative_to(ancestor).parts:
                try:(parent.path/name).mkdir()
                except FileExistsError:pass
                parent=stack.enter_context(parent.pin_child(name))
            yield parent,_identity(parent.path.stat())
        else:
            parent=stack.enter_context(pinned_directory_fd(ancestor))
            for name in path.relative_to(ancestor).parts:
                try:os.mkdir(name,dir_fd=parent)
                except FileExistsError:pass
                parent=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                stack.callback(os.close,parent)
            yield parent,_identity(os.fstat(parent))


@contextmanager
def _delivery_stage(parent, destination):
    """Lend a private writer path and publish the same payload through its pinned parent."""
    with ExitStack() as stack:
        if os.name=='nt':
            stage=stack.enter_context(windows_private_stage(parent.path))
            yield stage.path,_identity(stage.path.stat()),lambda result,receipt:stage.publish_to(destination)
            return
        wrapper=stack.enter_context(private_stage_fd(parent))
        os.mkdir('delivery', mode=0o700, dir_fd=wrapper)
        payload=os.open('delivery',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=wrapper)
        stack.callback(os.close,payload)
        identity=_identity(os.fstat(payload))
        if sys.platform.startswith('linux'):
            stage=stack.enter_context(directory_capability(payload)).path
        else:
            # Path-based verifiers need an ordinary private workspace on macOS.
            # Only exact checked files are copied to the descriptor-relative payload.
            stage=Path(stack.enter_context(tempfile.TemporaryDirectory()))
        def publish(result,receipt):
            if not sys.platform.startswith('linux'):
                from atomic_publish import _copy_verified_file
                from scene3d_snapshot import _stage_directory
                for ref in [*_delivery_files(result),receipt]:
                    with open_payload(stage,ref['path']) as (source,_):
                        *parts,name=ref['path'].split('/')
                        with _stage_directory(payload,parts,create=True) as directory:
                            fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                            with os.fdopen(fd,'r+b') as target:
                                _copy_verified_file(source.fileno(),target.fileno(),ref['sha256'],ref['bytes'])
            require(_identity(os.stat('delivery',dir_fd=wrapper,follow_symlinks=False))==identity,
                    'ownership','Staged delivery changed before publication.')
            _rename_no_replace('delivery',destination.name,source_dir_fd=wrapper,destination_dir_fd=parent)
            os.fsync(parent)
        yield stage,identity,publish


# Events that cannot change the frames an earlier delivery intent recorded.
RECONCILABLE_DELIVERY_EVENTS = frozenset({'delivery-published'})


class JobStore:
    """One local store may coordinate independent processes on the same host.

    Resources are declared reservations, not measured process usage. Backend
    execution independently enforces worker time/memory/output bounds. Shared
    network filesystem leases and cross-host takeover are deliberately rejected.
    """
    def __init__(self, root, *, workers=1, memory_bytes=4*1024**3,
                 storage_bytes=16*1024**3, gpu_workers=0, clock=time.time):
        for key, value in dict(workers=workers, memory_bytes=memory_bytes,
                               storage_bytes=storage_bytes, gpu_workers=gpu_workers).items():
            require(type(value) is int and value >= (0 if key == 'gpu_workers' else 1),
                    'invalid-input', 'Invalid scheduler capacity: ' + key)
        self.root = Path(root).absolute()
        require(not self.root.is_symlink(), 'ownership', 'Job store cannot be a symlink.')
        self.capacity = dict(workers=workers, memory_bytes=memory_bytes,
                             storage_bytes=storage_bytes, gpu_workers=gpu_workers)
        self.clock = clock
        if not self.root.exists():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.render3d-store-', dir=self.root.parent))
            try:
                atomic_json(stage/'store.json', dict(version=VERSION, host=socket.gethostname(), capacity=self.capacity))
                (stage/'cache').mkdir(); (stage/'staging').mkdir()
                db = sqlite3.connect(stage/'jobs.sqlite3')
                db.executescript('''
                  CREATE TABLE jobs(id TEXT PRIMARY KEY, request TEXT NOT NULL,
                    snapshot TEXT NOT NULL, backend TEXT NOT NULL, state TEXT NOT NULL,
                    priority INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, cancel INTEGER NOT NULL DEFAULT 0);
                  CREATE TABLE frames(job TEXT NOT NULL, frame INTEGER NOT NULL,
                    spec TEXT NOT NULL, cache_key TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, token TEXT, pid INTEGER,
                    heartbeat REAL, error TEXT, PRIMARY KEY(job,frame));
                  CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job TEXT NOT NULL, at REAL NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
                '''); db.close()
                _rename_no_replace(stage, self.root)
            except FileExistsError:
                pass
            finally:
                if stage.exists(): shutil.rmtree(stage)
        marker,_ = _metadata(safe_path(self.root,'store.json',exists=True))
        require(marker == dict(version=VERSION, host=socket.gethostname(), capacity=self.capacity),
                'ownership', 'Store host, version or capacities differ; open with its original configuration.')
        safe_path(self.root,'jobs.sqlite3',exists=True)
        self.root=self.root.resolve()
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS storage_reservations('
                       'stage TEXT PRIMARY KEY, bytes INTEGER NOT NULL, pid INTEGER NOT NULL)')
            if 'job' not in {row['name'] for row in db.execute('PRAGMA table_info(storage_reservations)')}:
                db.execute('ALTER TABLE storage_reservations ADD COLUMN job TEXT')

            db.execute('CREATE INDEX IF NOT EXISTS jobs_priority ON jobs(priority DESC,created,id) '
                       "WHERE cancel=0 AND state IN ('queued','running')")
            db.execute('CREATE INDEX IF NOT EXISTS frames_by_state ON frames(state,job,frame)')
            if 'publication_token' not in {row['name'] for row in db.execute('PRAGMA table_info(frames)')}:
                db.execute('ALTER TABLE frames ADD COLUMN publication_token TEXT')
            if 'settlement_pending' not in {row['name'] for row in db.execute('PRAGMA table_info(frames)')}:
                db.execute('ALTER TABLE frames ADD COLUMN settlement_pending INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS frame_publications('
                       'token TEXT PRIMARY KEY, job TEXT NOT NULL, frame INTEGER NOT NULL, '
                       'payload TEXT NOT NULL, digest TEXT NOT NULL, receipt_key TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS delivery_intents('
                       'destination TEXT PRIMARY KEY, job TEXT NOT NULL, revision INTEGER NOT NULL, '
                       'parent TEXT NOT NULL, payload TEXT NOT NULL, result_sha256 TEXT NOT NULL, '
                       'result_bytes INTEGER NOT NULL)')

    @contextmanager
    def _transaction(self):
        safe_path(self.root,'jobs.sqlite3',exists=True)
        db=sqlite3.connect(self.root/'jobs.sqlite3', timeout=10)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def _event(self, db, job, event, detail=None):
        db.execute('INSERT INTO events(job,at,event,detail) VALUES (?,?,?,?)',
                   (job,self.clock(),event,_dump(detail or {})))
        db.execute('UPDATE jobs SET revision=revision+1 WHERE id=?',(job,))

    def submit(self, request, snapshot_root, backend):
        validate(request,'RenderRequest');validate(backend,'BackendIdentity')
        snapshot=Path(snapshot_root).absolute()
        require(not snapshot.is_symlink(),'ownership','Snapshot root cannot be a symlink.')
        resources=request['settings']['resources']
        for name in ('workers','memory_bytes','storage_bytes'):
            require(resources[name] <= self.capacity[name],'system-memory' if name=='memory_bytes' else 'storage-exhaustion',
                    'Request exceeds scheduler '+name)
        gpu=backend['device']!='cpu'
        require(not gpu or self.capacity['gpu_workers']>0,'unsupported-device','No GPU worker capacity.')
        require(request['settings']['device_policy']!='gpu-required' or gpu,
                'unsupported-device','GPU-required request cannot use a CPU backend.')
        require(request['settings']['device_policy']!='cpu' or not gpu,
                'unsupported-device','CPU request cannot use a GPU backend.')
        specs=[]
        for index in range(request['frame_range']['start'],request['frame_range']['end']):
            spec=make_frame_spec(request,index,snapshot/'snapshot.json')
            specs.append((index,spec,object_hash(dict(frame=spec['cache_key'],backend=backend))))
        job=object_hash(dict(request=request,backend=backend))
        with self._transaction() as db:
            previous=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            if previous:
                require(previous['request']==_dump(request) and previous['backend']==_dump(backend),
                        'ownership','Job identity conflict.')
                return job
            # Owner destination uniqueness is permanent, including cancelled jobs.
            for row in db.execute('SELECT request FROM jobs'):
                old=json.loads(row['request'])['destination']
                require(old['path']!=request['destination']['path'], 'ownership',
                        'Destination is already owned by another job.')
            db.execute('INSERT INTO jobs(id,request,snapshot,backend,state,priority,created) VALUES(?,?,?,?,?,?,?)',
                (job,_dump(request),str(snapshot.resolve()),_dump(backend),'queued',request['priority'],self.clock()))
            db.executemany('INSERT INTO frames(job,frame,spec,cache_key,state) VALUES(?,?,?,?,?)',
                          [(job,i,_dump(s),key,'queued') for i,s,key in specs])
            self._event(db,job,'submitted',{'frames':len(specs)})
        return job

    def _checked_request(self, row):
        """The stored ID binds both the exact native request and backend."""
        try:
            request=decode_json(row['request']);backend=decode_json(row['backend'])
            validate(request,'RenderRequest');validate(backend,'BackendIdentity')
        except (RigError,TypeError,ValueError) as exc:
            raise Render3DError('ownership','Persisted job input is invalid: '+str(exc)) from exc
        require(object_hash(dict(request=request,backend=backend))==row['id'],
                'ownership','Persisted request/backend no longer matches its immutable job ID.')
        require(row['priority']==request['priority'],'ownership','Persisted scheduling priority differs from its request.')
        return request,backend

    def resolve_request(self, job, request_sha256):
        """Resolve RenderJob.id/request_sha256 through this existing store.

        The returned native request contains project/scene/shot versions,
        scheduling/resource/retry policies and source pins. Resolution does not
        approve current source bytes or rendered output.
        """
        require(isinstance(job,str) and len(job)==64 and all(char in '0123456789abcdef' for char in job),
                'invalid-input','Request binding requires a native content-addressed job ID.')
        require(isinstance(request_sha256,str) and len(request_sha256)==64
                and all(char in '0123456789abcdef' for char in request_sha256),
                'invalid-input','Request binding requires a canonical SHA-256 digest.')
        with self._transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            require(row is not None,'missing-asset','Request binding has no job in this store.')
            request,_=self._checked_request(row)
            require(object_hash(request)==request_sha256,'stale-asset','Request binding digest differs from the stored request.')
            return request

    def status(self, job, *, event_limit=1000):
        require(isinstance(job,str) and len(job)==64 and all(char in '0123456789abcdef' for char in job),
                'invalid-input','Status requires a native content-addressed job ID.')
        require(type(event_limit) is int and 0 <= event_limit <= 10000,'invalid-input','Invalid event limit.')
        with self._transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            require(row is not None,'invalid-input','Unknown render job.')
            request,backend=self._checked_request(row)
            result=dict(row);result['request']=request;result['backend']=backend
            result['request_sha256']=object_hash(request)
            result['request_binding']=dict(owner='render3d_jobs.JobStore',job_id=job,
                request_sha256=result['request_sha256'],resolver='JobStore.resolve_request')
            result['project']=deepcopy(request['project']);result['scene']=deepcopy(request['scene'])
            result['shot']=deepcopy(request['shot'])
            result['frames']=[dict(x) for x in db.execute(
                'SELECT f.frame,f.state,f.attempts,f.pid,f.token,f.heartbeat,f.error,f.cache_key,'
                'f.publication_token,p.receipt_key,p.digest AS receipt_sha256 FROM frames f LEFT JOIN frame_publications p '
                'ON p.token=f.publication_token AND p.job=f.job AND p.frame=f.frame WHERE f.job=? ORDER BY f.frame',(job,))]
            for frame in result['frames']:
                require(frame.pop('publication_token') is None or frame['receipt_key'] is not None,
                        'ownership','Published frame has lost its bound receipt.')
            unbound={f['frame']:f for f in result['frames'] if f['state']=='complete' and f['receipt_key'] is None}
            if unbound:
                self._historical_receipts(db,job,unbound)
            counts={name:sum(frame['state']==state for frame in result['frames']) for name,state in
                    (('queued','queued'),('active','running'),('completed','complete'),('failed','failed'),('cancelled','cancelled'))}
            counts['requested']=len(result['frames'])
            expected=request['frame_range']['end']-request['frame_range']['start']
            require([frame['frame'] for frame in result['frames']]==list(range(request['frame_range']['start'],request['frame_range']['end']))
                    and counts['requested']==expected and sum(counts[key] for key in counts if key!='requested')==expected,
                    'ownership','Persisted frames no longer partition the immutable request.')
            result['counts']=counts
            result['progress']=dict(completion_ratio=rational(Fraction(counts['completed'],expected)),
                completion_percent=100*counts['completed']/expected,
                settled_percent=100*(counts['completed']+counts['failed']+counts['cancelled'])/expected)
            result['retry_policy']=dict(max_attempts=request['max_attempts'],retryable_categories=list(RETRYABLE))
            resources=request['settings']['resources'];active=counts['active']
            stages=list(db.execute('SELECT stage,bytes,pid,job FROM storage_reservations'))
            result['reservation']=dict(basis='declared-reservations-not-measured-usage',
                active_frames=active,workers=active*resources['workers'],
                memory_bytes=active*resources['memory_bytes'],
                worker_storage_bytes=active*resources['storage_bytes'],
                gpu_workers=active if backend['device']!='cpu' else 0,
                cache_staging_bytes=sum(stage['bytes'] for stage in stages if stage['job']==job and _alive(stage['pid'])),
                unattributed_store_staging_bytes=sum(stage['bytes'] for stage in stages if stage['job'] is None and _alive(stage['pid'])))
            result['events']=[dict(x) for x in db.execute('SELECT seq,at,event,detail FROM (SELECT seq,at,event,detail FROM events WHERE job=? ORDER BY seq DESC LIMIT ?) ORDER BY seq',(job,event_limit))]
            return result

    def _historical_receipts(self,db,job,frames):
        """Only an intent from the same claim may supply a missing historical binding."""
        claims={};completions={}
        for row in db.execute('SELECT event,detail FROM events WHERE job=? AND event IN '
                              '("claimed","cache-reused","frame-published") ORDER BY seq',(job,)):
            detail=decode_json(row['detail']);index=detail['frame']
            if index not in frames:continue
            if row['event']=='claimed':
                claims[index]=detail['token']
                continue
            completions[index]=(row['event'],claims.get(index))
        for index,(event,token) in completions.items():
            frame=frames[index]
            receipt=db.execute('SELECT receipt_key,digest FROM frame_publications WHERE token=? AND job=? AND frame=?',
                               (token,job,index)).fetchone()
            frame['receipt_key'],frame['receipt_sha256']=receipt if receipt else (None,None)
            frame['unbound_reuse']=receipt is None and event=='cache-reused'

    def list_jobs(self, *, limit=100, offset=0):
        require(type(limit) is int and 1<=limit<=1000 and type(offset) is int and offset>=0,
                'invalid-input','Invalid pagination.')
        with self._transaction() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,state,priority,revision,created,cancel FROM jobs ORDER BY created,id LIMIT ? OFFSET ?',
                (limit,offset))]

    def _cache_entries(self):
        entries=[]
        for child in safe_path(self.root,'cache').iterdir():
            require(not child.is_symlink() and child.is_dir() and len(child.name)==64
                    and all(c in '0123456789abcdef' for c in child.name),
                    'ownership','Unexpected cache entry; refusing cleanup or quota calculation.')
            size=0
            for path in child.rglob('*'):
                require(not path.is_symlink(),'ownership','Cache contains a symlink.')
                if path.is_file():size+=path.stat().st_size
            entries.append(dict(key=child.name,bytes=size))
        return entries

    def _retained_cache_bytes(self, *, active_tokens=(), reserved_stages=()):
        """All staging bytes consume capacity, including failed worker evidence."""
        total=0
        active={'run-'+token for token in active_tokens}|set(reserved_stages)
        staging=safe_path(self.root,'staging')
        for path in staging.rglob('*'):
            if path.relative_to(staging).parts[0] in active:continue
            require(not path.is_symlink(),'ownership','Staging contains a symlink.')
            try:
                info=path.stat()
            except FileNotFoundError:
                # A live worker may finish an intermediate file during scanning.
                continue
            require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                    'ownership','Staging contains a nonregular artifact.')
            if stat.S_ISREG(info.st_mode):total+=info.st_size
        return total

    def _stored_output_bytes(self, *, active_tokens=(), reserved_stages=()):
        return sum(entry['bytes'] for entry in self._cache_entries())+self._retained_cache_bytes(
            active_tokens=active_tokens,reserved_stages=reserved_stages)

    def _active_reservations(self,db):
        active=[]
        for row in db.execute('SELECT j.*,f.token FROM frames f JOIN jobs j ON j.id=f.job '
                              'WHERE f.state="running" LIMIT ?', (self.capacity['workers']+1,)):
            require(len(active)<self.capacity['workers'],'ownership','Running claims exceed store capacity.')
            request,backend=self._checked_request(row)
            active.append(dict(resources=request['settings']['resources'],gpu=backend['device']!='cpu',
                               token=row['token']))
        return active

    def _storage_reserved(self, db, live):
        reservations=[]
        for row in db.execute('SELECT * FROM storage_reservations').fetchall():
            if _alive(row['pid']):reservations.append(row)
            else:
                # The bytes remain evidence; only the dead owner's reservation
                # is released. Physical leftovers are then counted below.
                db.execute('DELETE FROM storage_reservations WHERE stage=?',(row['stage'],))
        reserved=sum(row['resources']['storage_bytes'] for row in live)
        reserved+=sum(row['bytes'] for row in reservations)
        return reserved+self._stored_output_bytes(active_tokens=[row['token'] for row in live],
            reserved_stages=[row['stage'] for row in reservations])

    def _pinned_keys(self,db):
        return {row[0] for row in db.execute('SELECT cache_key FROM frames UNION SELECT receipt_key '
                                             'FROM frame_publications WHERE receipt_key IS NOT NULL')}

    def cache_inventory(self):
        with self._transaction() as db:
            pinned=self._pinned_keys(db)
            return [dict(entry,pinned=entry['key'] in pinned) for entry in self._cache_entries()]

    def clean_cache(self, keys, *, apply=False):
        """Explicit two-phase cleanup only for cache keys absent from all jobs.

        Job history is never deleted by this API, so every submitted job pins
        its frames regardless of state. No age or filename heuristic can evict
        a published frame required by a completed or interrupted job.
        """
        require(isinstance(keys,list) and len(keys)<=1000 and all(isinstance(k,str) for k in keys),
                'invalid-input','Supply at most 1000 cache keys.')
        with self._transaction() as db:
            pinned=self._pinned_keys(db)
            entries={entry['key']:entry for entry in self._cache_entries()}
            require(all(key in entries and key not in pinned for key in keys),
                    'ownership','Cleanup includes an unknown or pinned cache key.')
            selected=[entries[key] for key in dict.fromkeys(keys)]
            if apply:
                for entry in selected:
                    source=safe_path(self.root,'cache/'+entry['key'])
                    quarantine=safe_path(self.root,'staging/cleanup-'+uuid.uuid4().hex)
                    _rename_no_replace(source,quarantine)
                    shutil.rmtree(quarantine)
            return dict(applied=apply,entries=selected)

    def _refresh(self, db, job):
        states={row[0] for row in db.execute('SELECT DISTINCT state FROM frames WHERE job=?',(job,))}
        cancel=db.execute('SELECT cancel FROM jobs WHERE id=?',(job,)).fetchone()[0]
        state=('running' if 'running' in states else 'cancelled' if cancel else
               'complete' if states=={'complete'} else
               'failed' if 'failed' in states and 'queued' not in states else 'queued')
        db.execute('UPDATE jobs SET state=? WHERE id=?',(state,job))

    def claim(self, job=None):
        """Claim one priority-ordered frame only when global reservations permit."""
        while True:
            outcome=self._claim_one(job)
            if not isinstance(outcome,Render3DError):
                return outcome
            if job is not None:
                raise outcome

    def _claim_one(self,job):
        with self._transaction() as db:
            live=self._active_reservations(db)
            used={name:sum(row['resources'][name] for row in live) for name in ('workers','memory_bytes')}
            gpu_used=sum(row['gpu'] for row in live)
            used['storage_bytes']=self._storage_reserved(db,live)
            if used['workers']>=self.capacity['workers']:
                return None
            # Each job has a single frame budget. Let SQLite select one fitting
            # job, probing the indexed queue once per job regardless of frame count.
            query="""SELECT j.*, (SELECT frame FROM frames WHERE state='queued' AND job=j.id
                                 ORDER BY frame LIMIT 1) AS candidate
                     FROM jobs j WHERE j.cancel=0 AND j.state IN ('queued','running')
                     AND candidate IS NOT NULL"""
            args=[]
            if job is not None:
                query+=' AND j.id=?'
                args.append(job)
            else:
                # Malformed JSON must reach the canonical validator. These
                # predicates only select a candidate, never authorize a lease.
                query+=""" AND CASE WHEN json_valid(request) AND json_valid(backend) THEN
                         coalesce(json_extract(request,'$.settings.resources.workers') <= ? AND
                                  json_extract(request,'$.settings.resources.memory_bytes') <= ? AND
                                  json_extract(request,'$.settings.resources.storage_bytes') <= ? AND
                                  (json_extract(backend,'$.device')='cpu' OR ?),1) ELSE 1 END"""
                args.extend(max(-1,min(self.capacity[name]-used[name],2**63-1))
                            for name in ('workers','memory_bytes','storage_bytes'))
                args.append(gpu_used<self.capacity['gpu_workers'])
            query+=' ORDER BY j.priority DESC,j.created,candidate,j.id LIMIT 1'
            candidate=db.execute(query,args).fetchone()
            if candidate is None:
                return None
            request,backend=self._checked_request(candidate)
            budget=request['settings']['resources']
            if (any(used[n]+budget[n]>self.capacity[n] for n in used) or
                    backend['device']!='cpu' and gpu_used>=self.capacity['gpu_workers']):
                return None
            row=db.execute('SELECT * FROM frames WHERE job=? AND frame=?',
                           (candidate['id'],candidate['candidate'])).fetchone()
            span=request['frame_range']
            length=span['end']-span['start']
            partition=db.execute("""SELECT count(*),min(frame),max(frame),min(typeof(frame)='integer'
                                       AND state IN ('queued','running','complete','cancelled','failed'))
                                    FROM (SELECT frame,state FROM frames WHERE job=? LIMIT ?)""",
                                 (candidate['id'],length+1)).fetchone()
            require(tuple(partition)==(length,span['start'],span['end']-1,1)
                    and type(row['attempts']) is int
                    and 0<=row['attempts']<request['max_attempts'],
                    'ownership','Persisted frame partition or attempt differs from the request.')
            try:
                spec=make_frame_spec(request,row['frame'],Path(candidate['snapshot'])/'snapshot.json')
            except Render3DError as exc:
                if exc.report['category'] not in ('missing-asset','stale-asset'):
                    raise
                message=str(exc)[:2048]
                db.execute('UPDATE frames SET state="failed",error=? WHERE job=? AND state="queued"',
                           (message,candidate['id']))
                self._event(db,candidate['id'],'source-unavailable',dict(category=exc.report['category'],error=message))
                self._refresh(db,candidate['id'])
                return exc
            require(row['spec']==_dump(spec) and row['cache_key']==object_hash(dict(frame=spec['cache_key'],backend=backend)),
                    'ownership','Persisted frame plan or cache key differs from the request.')
            token=uuid.uuid4().hex
            db.execute('UPDATE frames SET state="running",attempts=attempts+1,token=?,pid=?,heartbeat=? WHERE job=? AND frame=?',
                       (token,os.getpid(),self.clock(),row['job'],row['frame']))
            self._event(db,row['job'],'claimed',{'frame':row['frame'],'token':token})
            self._refresh(db,row['job'])
            return dict(job=row['job'],frame=row['frame'],token=token,request=request,backend=backend,
                        snapshot=candidate['snapshot'],cache_key=row['cache_key'],spec=spec)

    def _owned(self, db, lease):
        row=db.execute('SELECT * FROM frames WHERE job=? AND frame=?',(lease['job'],lease['frame'])).fetchone()
        require(row is not None and row['state']=='running' and type(lease['frame']) is int
                and row['token']==lease['token'] and not row['settlement_pending']
                and row['pid']==os.getpid(),'ownership','Frame lease is stale or belongs to another process.')
        job=db.execute('SELECT * FROM jobs WHERE id=?',(row['job'],)).fetchone()
        request,backend=self._checked_request(job)
        require(_dump(lease.get('request'))==_dump(request) and _dump(lease.get('backend'))==_dump(backend)
                and lease.get('snapshot')==job['snapshot'] and _dump(lease.get('spec'))==row['spec']
                and lease.get('cache_key')==row['cache_key'],'ownership','Work differs from its claimed frame.')
        return row

    def heartbeat(self, lease):
        with self._transaction() as db:
            self._owned(db,lease)
            db.execute('UPDATE frames SET heartbeat=? WHERE job=? AND frame=?',(self.clock(),lease['job'],lease['frame']))
            return bool(db.execute('SELECT cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()[0])

    def cancel(self, job, *, expected_revision=None):
        with self._transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            require(row is not None,'invalid-input','Unknown render job.')
            require(expected_revision is None or expected_revision==row['revision'],'ownership','Stale job revision.')
            if row['state']=='complete':return
            db.execute('UPDATE jobs SET cancel=1 WHERE id=?',(job,))
            db.execute('UPDATE frames SET state="cancelled" WHERE job=? AND state="queued"',(job,))
            self._event(db,job,'cancel-requested');self._refresh(db,job)

    def fail(self, lease, error, *, retryable=False, verifier=None):
        with self._transaction() as db:
            row=self._owned(db,lease)
            job=db.execute('SELECT request,cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()
            try:published=self._publication_path(db,row) is not None
            except OSError:published=True  # An uncertain rename must retain its intent.
            if not published:
                state='cancelled' if job['cancel'] else 'queued' if retryable and row['attempts']<json.loads(job['request'])['max_attempts'] else 'failed'
                db.execute('UPDATE frames SET state=?,error=?,token=NULL,pid=NULL,heartbeat=NULL WHERE job=? AND frame=?',
                           (state,str(error)[:2048],lease['job'],lease['frame']))
                db.execute('DELETE FROM frame_publications WHERE token=?',(lease['token'],))
                self._event(db,lease['job'],'attempt-failed',{'frame':lease['frame'],'state':state,'error':str(error)[:2048]})
                self._refresh(db,lease['job'])
                return
            # The caller has stopped writing. Fence its lease before invoking an
            # independent verifier; any process may now reconcile this exact intent.
            db.execute('UPDATE frames SET settlement_pending=1,pid=NULL,heartbeat=NULL,error=? WHERE job=? AND frame=?',
                       (str(error)[:2048],row['job'],row['frame']))
            self._event(db,row['job'],'publication-interrupted',dict(frame=row['frame'],error=str(error)[:2048]))
        if verifier is not None:
            with self._transaction() as db:
                self._recover(db,lease['job'],verifier,token=lease['token'])

    def recover(self, job=None, *, verifier=None):
        """Settle dead claims, rechecking any interrupted publication before completion."""
        with self._transaction() as db:
            return self._recover(db,job,verifier)

    def _publication_path(self, db, row):
        intent=db.execute('SELECT * FROM frame_publications WHERE token=? AND job=? AND frame=?',
                          (row['token'],row['job'],row['frame'])).fetchone()
        if intent is None:
            return None
        for key in (row['cache_key'],intent['receipt_key']):
            path=safe_path(self.root,'cache/'+key)
            try:info=path.stat(follow_symlinks=False)
            except FileNotFoundError:continue
            if stat.S_ISDIR(info.st_mode) and _identity(info)==intent['payload']:
                return intent,key,path
        return None

    def _published_receipt(self, db, row, verifier):
        publication=self._publication_path(db,row)
        if publication is None:return None
        intent,key,path=publication
        require(verifier is not None,'verification-failed',
                'Recovering a published frame requires an independent verifier.')
        job_row=db.execute('SELECT * FROM jobs WHERE id=?',(row['job'],)).fetchone()
        request,backend=self._checked_request(job_row)
        try:
            spec=make_frame_spec(request,row['frame'],Path(job_row['snapshot'])/'snapshot.json')
            require(row['spec']==_dump(spec) and row['cache_key']==object_hash(dict(
                frame=spec['cache_key'],backend=backend)),'ownership','Interrupted frame plan differs.')
            lease=dict(job=row['job'],frame=row['frame'],token=row['token'],request=request,
                       backend=backend,spec=spec,cache_key=row['cache_key'])
            record,_=self._verify_cache(lease,verifier,key=key,digest=intent['digest'])
            require(record['request']==self.frame_request(lease) and record['spec']==spec
                    and _identity(path.stat(follow_symlinks=False))==intent['payload'],
                    'ownership','Interrupted frame publication changed.')
        except (ValueError,KeyError,FileNotFoundError) as exc:
            if isinstance(exc,Render3DError) and exc.report['category'] in RETRYABLE:
                raise
            if isinstance(exc.__cause__,OSError) and not isinstance(exc.__cause__,FileNotFoundError):
                raise
            return None
        return key

    def _recover(self, db, job, verifier, *, token=None):
        recovered=[]
        query='SELECT f.*,j.request,j.cancel FROM frames f JOIN jobs j ON j.id=f.job WHERE f.state="running"'
        args=[]
        if job is not None:query+=' AND f.job=?';args.append(job)
        if token is not None:query+=' AND f.token=?';args.append(token)
        for row in db.execute(query,args).fetchall():
            relinquished=row['settlement_pending']==1 and row['pid'] is None
            if not relinquished and _alive(row['pid']):continue
            key=self._published_receipt(db,row,verifier)
            if key is not None:
                self._finish_frame(db,row,key,recovered=True)
                recovered.append((row['job'],row['frame']))
                continue
            state='cancelled' if row['cancel'] else 'queued' if row['attempts']<json.loads(row['request'])['max_attempts'] else 'failed'
            error=row['error'] if relinquished else 'Owner process exited'
            db.execute('UPDATE frames SET state=?,settlement_pending=0,token=NULL,pid=NULL,heartbeat=NULL,error=? WHERE job=? AND frame=?',
                       (state,error,row['job'],row['frame']))
            recovered.append((row['job'],row['frame']))
            event='publication-unavailable' if row['settlement_pending'] else 'owner-exited'
            self._event(db,row['job'],event,{'frame':row['frame'],'state':state});self._refresh(db,row['job'])
            db.execute('DELETE FROM frame_publications WHERE token=?',(row['token'],))
        return recovered

    def resume(self, job, *, retry_failed=False, expected_revision=None, verifier=None):
        snapshot=self.status(job)
        require(expected_revision is None or expected_revision==snapshot['revision'],'ownership','Stale job revision.')
        completed=[frame for frame in snapshot['frames'] if frame['state']=='complete']
        require(not completed or verifier is not None,'verification-failed',
                'Resuming completed frames requires an independent verifier.')
        invalid=[];historical=[]
        for frame in completed:
            spec=make_frame_spec(snapshot['request'],frame['frame'],Path(snapshot['snapshot'])/'snapshot.json')
            lease=dict(request=snapshot['request'],frame=frame['frame'],spec=spec,
                       cache_key=frame['cache_key'],backend=snapshot['backend'])
            try:
                _,digest=self._verify_cache(lease,verifier,key=frame['receipt_key'],digest=frame['receipt_sha256'])
                if frame.get('unbound_reuse'):
                    historical.append((frame,digest))
            except (Render3DError,ValueError,OSError,KeyError):invalid.append(frame['frame'])
        with self._transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            require(row is not None,'invalid-input','Unknown render job.')
            require(expected_revision is None or row['revision']==expected_revision,'ownership','Stale job revision.')
            require(row['revision']==snapshot['revision'],'ownership','Job changed during resume verification.')
            self._recover(db,job,verifier)
            require(not db.execute('SELECT 1 FROM frames WHERE job=? AND state="running"',(job,)).fetchone(),
                    'ownership','Cannot resume while a live worker owns a frame.')
            for frame,digest in historical:
                self._bind_reuse(db,job,frame['frame'],uuid.uuid4().hex,frame['cache_key'],digest)
                self._event(db,job,'cache-reuse-reverified',dict(frame=frame['frame'],receipt_sha256=digest))
            db.execute('UPDATE jobs SET cancel=0 WHERE id=?',(job,))
            states=['cancelled']+(['failed'] if retry_failed else [])
            for state in states:
                db.execute('UPDATE frames SET state="queued",error=NULL WHERE job=? AND state=? AND attempts<?',
                           (job,state,json.loads(row['request'])['max_attempts']))
            # Cancelled attempts that consumed the retry budget cannot run again.
            db.execute('UPDATE frames SET state="failed" WHERE job=? AND state="cancelled" AND attempts>=?',
                       (job,snapshot['request']['max_attempts']))
            for frame in invalid:
                db.execute('UPDATE frames SET state=CASE WHEN attempts<? THEN "queued" ELSE "failed" END,error="Cached frame failed verification" WHERE job=? AND frame=?',
                           (json.loads(row['request'])['max_attempts'],job,frame))
            self._event(db,job,'resumed',{'retry_failed':retry_failed});self._refresh(db,job)

    def frame_request(self, lease):
        value=deepcopy(lease['request'])
        _,source=frame_times(value,lease['frame'])
        value['source_start']=rational(source)
        value['frame_range']=dict(start=lease['frame'],end=lease['frame']+1)
        return value

    def _verify_cache(self, lease, verifier, *, key=None, digest=None):
        path=safe_path(self.root,'cache/'+(key or lease['cache_key']))
        require(path.is_dir() and not path.is_symlink(),'missing-asset','Cache entry unavailable.')
        record,observed=_metadata(safe_path(path,'cache.json',exists=True),digest)
        require(isinstance(record,dict) and set(record)=={
                    'version','cache_key','spec','backend','request','result','report'},
                'stale-cache','Cache metadata has an invalid record shape.')
        require(type(record['version']) is int and record['version']==VERSION
                and isinstance(record['report'],dict),
                'stale-cache','Cache metadata version or verification report is invalid.')
        for field,kind in (('spec','FrameSpec'),('backend','BackendIdentity'),
                           ('request','RenderRequest'),('result','RenderResult')):
            validate(record[field],kind)
        require(record['cache_key']==lease['cache_key'] and record['spec']['cache_key']==lease['spec']['cache_key'] and record['backend']==lease['backend'],
                'stale-cache','Cache identity differs from requested frame.')
        require({k:v for k,v in record['spec'].items() if k!='destination'} ==
                {k:v for k,v in lease['spec'].items() if k!='destination'},
                'stale-cache','Cache frame plan differs from the current frame.')
        old=record['request'];current=self.frame_request(lease)
        require(all(old[k]==current[k] for k in ('project','scene','shot','snapshot','fps','source_start','playback_rate','camera','settings','frame_range')),
                'stale-cache','Cache worker request differs from the current frame input.')
        result=record['result'];validate(result,'RenderResult')
        require(result['status']=='complete' and result['backend']==lease['backend']
                and result['request_sha256']==object_hash(old),
                'stale-cache','Cache result differs from its publishing worker request.')
        refs={}
        for ref in _delivery_files(result):
            require(refs.setdefault(ref['path'],ref)==ref,'stale-cache','Conflicting cached references.')
        require(sum(ref['bytes'] for ref in refs.values())<=old['settings']['resources']['storage_bytes'],
                'stale-cache','Cached references exceed the original worker budget.')
        for ref in refs.values():
            with open_payload(path,ref['path']) as (stream,info):
                _check_artifact(stream,info,ref,'stale-cache')
        report=verifier(record['request'],result,path)
        require(isinstance(report,dict) and _dump(report)==_dump(record['report']),
                'verification-failed','Stored frame report differs from independent verification.')
        return record,observed

    def _bind_reuse(self,db,job,frame,token,key,digest):
        path=safe_path(self.root,'cache/'+key)
        _metadata(safe_path(path,'cache.json',exists=True),digest)
        db.execute('INSERT INTO frame_publications VALUES(?,?,?,?,?,?)',
                   (token,job,frame,_identity(path.stat()),digest,key))
        db.execute('UPDATE frames SET state="complete",publication_token=?,token=NULL,pid=NULL,heartbeat=NULL,error=NULL WHERE job=? AND frame=?',
                   (token,job,frame))

    def reuse(self, lease, verifier):
        """Re-decode and hash every artifact; stale entries are never accepted."""
        lease=deepcopy(lease)
        with self._transaction() as db:
            self._owned(db,lease)
        # Revalidate source bytes, even if a cache directory already exists.
        current=make_frame_spec(lease['request'],lease['frame'],Path(lease['snapshot'])/'snapshot.json')
        require(current==lease['spec'],'stale-asset','Source changed since job submission.')
        if lease['request']['cache_policy']=='refresh':return False
        try:_,digest=self._verify_cache(lease,verifier)
        except (Render3DError,ValueError,OSError,KeyError):return False
        with self._transaction() as db:
            self._owned(db,lease)
            require(not db.execute('SELECT cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()[0],
                    'cancelled','Cancellation requested before cache completion.')
            self._bind_reuse(db,lease['job'],lease['frame'],lease['token'],lease['cache_key'],digest)
            self._event(db,lease['job'],'cache-reused',{'frame':lease['frame']});self._refresh(db,lease['job'])
        return True

    def publish(self, lease, output_root, result, verifier):
        """Copy verified artifacts to private staging, reverify, publish exclusively."""
        lease=deepcopy(lease)
        with self._transaction() as db:
            self._owned(db,lease)
        output_root=Path(output_root)
        require(not output_root.is_symlink(),'ownership','Output root cannot be a symlink.')
        validate(result,'RenderResult')
        require(result['status']=='complete','incomplete-output','Only complete verified frames can enter cache.')
        require(result['backend']==lease['backend'],'stale-cache','Backend identity changed.')
        request=self.frame_request(lease)
        require(result['request_sha256']==object_hash(request) and result['requested_frames']==[lease['frame']]
                and result['snapshot_sha256']==lease['spec']['snapshot_sha256'],
                'stale-cache','Worker result differs from its claimed request or frame.')
        report=verifier(request,result,output_root)
        require(isinstance(report,dict),'verification-failed','Verifier did not return a metadata report.')
        current=make_frame_spec(lease['request'],lease['frame'],Path(lease['snapshot'])/'snapshot.json')
        require(current==lease['spec'],'stale-asset','Source changed before frame publication.')
        refs=[i['file'] for i in result['images']]+result['logs']
        refs += [r for r in (result['manifest'],result['verification']['evidence']) if r is not None]
        unique={}
        for ref in refs:
            require(ref['path'] not in unique or unique[ref['path']]==ref,'invalid-input','Conflicting artifact references.')
            require(ref['path'].split('/')[0]!='cache.json','invalid-input','cache.json is reserved.')
            unique[ref['path']]=ref
        artifact_bytes=sum(ref['bytes'] for ref in unique.values())
        require(artifact_bytes<=request['settings']['resources']['storage_bytes'],
                'storage-exhaustion','Artifact inventory exceeds its storage budget.')
        record=dict(version=1,cache_key=lease['cache_key'],spec=lease['spec'],backend=lease['backend'],request=request,result=result,report=report)
        payload=(json.dumps(record,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
        require(len(payload)<=32*1024**2,'storage-exhaustion','Cache metadata exceeds its read bound.')
        reserve=artifact_bytes+len(payload)
        stage=safe_path(self.root,'staging/cache-'+uuid.uuid4().hex)
        with self._transaction() as db:
            self._owned(db,lease)
            require(not db.execute('SELECT cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()[0],
                    'cancelled','Cancellation requested before cache admission.')
            live=self._active_reservations(db)
            require(self._storage_reserved(db,live)+reserve<=self.capacity['storage_bytes'],
                    'storage-exhaustion','Complete cache stage exceeds unreserved store capacity.')
            db.execute('INSERT INTO storage_reservations(stage,bytes,pid,job) VALUES(?,?,?,?)',
                       (stage.name,reserve,os.getpid(),lease['job']))
        created=False
        try:
            stage.mkdir();created=True
            copied=0
            for ref in unique.values():
                source=safe_path(output_root,ref['path'],exists=True)
                target=safe_path(stage,ref['path']);target.parent.mkdir(parents=True,exist_ok=True)
                require(ref['bytes']<=reserve-len(payload)-copied,'storage-exhaustion','Cache copy exceeds its reservation.')
                _copy_artifact(source,target,ref)
                copied+=ref['bytes']
            fresh=verifier(request,result,stage)
            require(_dump(fresh)==_dump(report),'verification-failed','Copied artifact verification differs from admitted source evidence.')
            atomic_bytes(stage/'cache.json',payload)
            alternate=object_hash(dict(cache_key=lease['cache_key'],attempt=lease['token']))
            intent=(lease['token'],lease['job'],lease['frame'],_identity(stage.stat()),
                    hashlib.sha256(payload).hexdigest(),alternate)
            with self._transaction() as db:
                self._owned(db,lease)
                db.execute('INSERT OR REPLACE INTO frame_publications VALUES(?,?,?,?,?,?)',intent)
            with self._transaction() as db:
                self._owned(db,lease)
                require(not db.execute('SELECT cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()[0],
                        'cancelled','Cancellation requested before publication.')
                key=lease['cache_key']
                destination=safe_path(self.root,'cache/'+key)
                used=self._stored_output_bytes()
                require(destination.exists() or used<=self.capacity['storage_bytes'],
                        'storage-exhaustion','Verified cache publication exceeds store capacity.')
                try:_rename_no_replace(stage,destination)
                except FileExistsError:
                    try:
                        existing,_=self._verify_cache(lease,verifier)
                    except (Render3DError,ValueError,OSError,KeyError):
                        # Preserve corrupt bytes as evidence before installing a
                        # newly rendered and independently verified replacement.
                        require(used<=self.capacity['storage_bytes'],
                                'storage-exhaustion','Cache repair would exceed capacity while retaining corrupt evidence.')
                        quarantine=safe_path(self.root,'staging/corrupt-'+uuid.uuid4().hex)
                        _rename_no_replace(destination,quarantine)
                        try:_rename_no_replace(stage,destination)
                        except Exception:
                            if not destination.exists():_rename_no_replace(quarantine,destination)
                            raise
                        self._event(db,lease['job'],'cache-corruption-repaired',
                                    {'frame':lease['frame'],'preserved':str(quarantine.relative_to(self.root))})
                    else:
                        expected=[(i['frame'],i['pass_name'],i['file']['sha256']) for i in existing['result']['images']]
                        actual=[(i['frame'],i['pass_name'],i['file']['sha256']) for i in result['images']]
                        require(expected==actual,'stale-cache','Same render inputs produced different refreshed pixels.')
                        key=alternate
                        require(used<=self.capacity['storage_bytes'],'storage-exhaustion',
                                'Retaining the fresh receipt would exceed store capacity.')
                        _rename_no_replace(stage,safe_path(self.root,'cache/'+key))
                self._finish_frame(db,lease,key)
        finally:
            try:
                if created and stage.exists():shutil.rmtree(stage)
            finally:
                with self._transaction() as db:
                    db.execute('DELETE FROM storage_reservations WHERE stage=? AND pid=?',
                               (stage.name,os.getpid()))
        return record

    def _finish_frame(self,db,lease,key,*,recovered=False):
        db.execute('UPDATE frame_publications SET receipt_key=? WHERE token=?',(key,lease['token']))
        db.execute('UPDATE frames SET state="complete",settlement_pending=0,publication_token=?,token=NULL,pid=NULL,heartbeat=NULL,'
                   'error=NULL WHERE job=? AND frame=?',(lease['token'],lease['job'],lease['frame']))
        self._event(db,lease['job'],'frame-published',dict(frame=lease['frame'],cache_key=lease['cache_key'],
                                                          receipt_key=key,recovered=recovered))
        self._refresh(db,lease['job'])

    def run_one(self, backend, verifier, *, job=None):
        lease=self.claim(job)
        if lease is None:return None
        output=None;completed=False
        try:
            if self.reuse(lease,verifier):return dict(lease=lease,reused=True)
            output=safe_path(self.root,'staging/run-'+lease['token'])
            result=backend.render_frames(self.frame_request(lease),lease['snapshot'],output,
                                         cancelled=lambda:self.heartbeat(lease))
            self.publish(lease,output,result,verifier)
            completed=True
            return dict(lease=lease,reused=False)
        except Exception as exc:
            try:self.fail(lease,exc,retryable=isinstance(exc,Render3DError) and exc.report['category'] in RETRYABLE,
                          verifier=verifier)
            except Render3DError:pass
            raise
        finally:
            if completed and output is not None and output.exists():shutil.rmtree(output)

    def materialize(self, job, destination, verifier):
        """Publish a complete job's decoded frames to a new portable directory.

        Cached worker receipts retain their original per-frame requests; the
        new result binds the complete request and preserves every cache receipt
        as provenance. No existing destination, even an empty one, is replaced.
        """
        state=self.status(job)
        require(state['state']=='complete' and state['cancel']==0 and state['counts']['completed']==state['counts']['requested'],
                'incomplete-output','All requested frames must be complete and cancellation clear.')
        require(not any(frame.get('unbound_reuse') for frame in state['frames']),'verification-failed',
                'Historical cache reuse requires explicit receipt verification through resume.')
        destination=Path(destination).absolute()
        require(destination.name not in ('','.','..') and '..' not in destination.parts,
                'ownership','Delivery destination contains parent traversal.')
        require(not destination.parent.resolve().is_relative_to(self.root),
                'ownership','Delivery destination cannot be inside the job store.')
        if os.name=='nt':
            from atomic_windows import validate_destination
            destination=validate_destination(destination)
        with _delivery_parent(destination.parent) as (parent,parent_id):
            if destination.exists() or destination.is_symlink():
                return self._reconcile_delivery(state,destination,parent,parent_id)
            with _delivery_stage(parent,destination) as (stage,payload_id,publish):
                result,receipt=self._build_delivery(state,stage,verifier)
                digest=receipt['sha256']
                intent=(str(destination),job,state['revision'],parent_id,payload_id,digest,receipt['bytes'])
                with self._transaction() as db:
                    self._check_delivery_state(db,state)
                    require(not destination.exists() and not destination.is_symlink(),
                            'ownership','Delivery destination already exists.')
                    # Pre-rename interruption leaves no published destination. Replace
                    # that abandoned intent; exclusive rename also fences concurrent writers.
                    prepared=db.execute('SELECT job,parent FROM delivery_intents WHERE destination=?',
                                        (str(destination),)).fetchone()
                    require(prepared is None or (prepared['job'],prepared['parent'])==(job,parent_id),
                            'ownership','Another job or parent owns the pending delivery.')
                    db.execute('INSERT OR REPLACE INTO delivery_intents VALUES(?,?,?,?,?,?,?)',intent)
                with self._transaction() as db:
                    pending=db.execute('SELECT * FROM delivery_intents WHERE destination=?',(str(destination),)).fetchone()
                    self._check_delivery_state(db,state)
                    require(pending is not None and tuple(pending)==intent,'ownership','Delivery changed before publication.')
                    require(_identity(destination.parent.stat())==parent_id,'ownership','Delivery parent changed.')
                    publish(result,receipt)
                    require(_identity(destination.parent.stat())==parent_id and
                            _identity(destination.stat(follow_symlinks=False))==payload_id,
                            'ownership','Delivery location changed during publication.')
                    self._finish_delivery(db,job,destination,digest)
                return result

    def _check_delivery_state(self,db,state):
        job=state['id']
        current=db.execute('SELECT revision,state,cancel FROM jobs WHERE id=?',(job,)).fetchone()
        span=state['request']['frame_range']
        partition=db.execute('SELECT count(*),min(frame),max(frame),min(typeof(frame)="integer" AND state="complete") '
                             'FROM frames WHERE job=?',(job,)).fetchone()
        require(current is not None and tuple(current)==(state['revision'],'complete',0)
                and tuple(partition)==(span['end']-span['start'],span['start'],span['end']-1,1),
                'ownership','Job changed while delivering frames.')

    def _finish_delivery(self,db,job,destination,digest):
        self._event(db,job,'delivery-published',dict(destination=str(destination),result_sha256=digest))
        db.execute('DELETE FROM delivery_intents WHERE destination=?',(str(destination),))

    def _reconcile_delivery(self,state,destination,parent,parent_id):
        """Only complete an interrupted publication from the recorded inode and exact bytes."""
        job=state['id']
        with self._transaction() as db, ExitStack() as stack:
            pending=db.execute('SELECT * FROM delivery_intents WHERE destination=?',(str(destination),)).fetchone()
            require(pending is not None and pending['job']==job and pending['parent']==parent_id
                    and not destination.is_symlink() and destination.is_dir(),
                    'ownership','Delivery destination already exists.')
            self._check_delivery_state(db,state)
            # Later deliveries do not change the frames this intent delivered.
            # Any other event since the intent (resume, re-render, cache
            # rebinding, recovery) still refuses.
            later=state['revision']-pending['revision']
            require(later>=0,'ownership','Job changed while reconciling delivery.')
            events=[row['event'] for row in db.execute(
                'SELECT event FROM events WHERE job=? ORDER BY seq DESC LIMIT ?',(job,later))]
            require(len(events)==later and set(events)<=RECONCILABLE_DELIVERY_EVENTS,
                    'ownership','Job changed while reconciling delivery.')
            if os.name=='nt':
                payload=stack.enter_context(parent.pin_child(destination.name))
                identity=_identity(payload.path.stat())
                reader=lambda name:open_payload(payload.path,name)
            else:
                payload=os.open(destination.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                stack.callback(os.close,payload)
                identity=_identity(os.fstat(payload))
                reader=lambda name:open_payload_at(payload,name)
            require(identity==pending['payload'],'ownership','Published directory differs from the prepared delivery.')
            with reader('result.json') as (stream,info):
                require(info.st_size==pending['result_bytes'] and info.st_size<=MAX_RESULT_JSON_BYTES,
                        'ownership','Published result size differs from its intent.')
                data=stream.read(info.st_size+1)
            require(hashlib.sha256(data).hexdigest()==pending['result_sha256'],'ownership','Published result digest differs.')
            result=validate(decode_json(data,max_bytes=MAX_RESULT_JSON_BYTES),'RenderResult')
            require(result['job']==job and result['request_sha256']==state['request_sha256']
                    and result['backend']==state['backend'] and result['status']=='complete',
                    'ownership','Published result identity differs.')
            refs=_delivery_files(result)
            _check_delivery_inventory(payload,refs)
            for ref in refs:
                with reader(ref['path']) as (stream,info):
                    _check_artifact(stream,info,ref,'ownership')
            require(_identity(destination.parent.stat())==parent_id and
                    _identity(destination.stat(follow_symlinks=False))==identity,
                    'ownership','Delivery location changed while reconciling.')
            if os.name!='nt':os.fsync(parent)
            self._finish_delivery(db,job,destination,pending['result_sha256'])
            return result

    def _build_delivery(self,state,stage,verifier):
        request=state['request'];backend=state['backend'];job=state['id']
        reused=set()
        with self._transaction() as db:
            for row in db.execute('SELECT event,detail FROM events WHERE job=? AND event IN '
                                  '("frame-published","cache-reused") ORDER BY seq',(job,)):
                frame=decode_json(row['detail'])['frame']
                if row['event']=='cache-reused':reused.add(frame)
                else:reused.discard(frame)
        images=[];logs=[];total=0
        budget=request['settings']['resources']['storage_bytes']
        def write_metadata(name,value):
            nonlocal total
            payload=(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
            require(len(payload)<=budget-total,'storage-exhaustion',
                    'Delivery metadata exceeds request storage budget.')
            atomic_bytes(safe_path(stage,name),payload)
            total+=len(payload)
        for frame in state['frames']:
            spec=make_frame_spec(request,frame['frame'],Path(state['snapshot'])/'snapshot.json')
            lease=dict(request=request,frame=frame['frame'],cache_key=frame['cache_key'],
                       spec=spec,backend=backend)
            key=frame['receipt_key'] or frame['cache_key']
            record,_=self._verify_cache(lease,verifier,key=key,digest=frame['receipt_sha256'])
            # Older stores can cite a previous worker even for an executed refresh.
            if record['request']!=self.frame_request(lease):
                reused.add(frame['frame'])
            source=safe_path(self.root,'cache/'+key)
            for image in record['result']['images']:
                image=deepcopy(image);old=image['file']
                suffix=request['settings']['output']['format']
                newpath=(f'frames/{image["frame"]:06d}.{suffix}' if image['pass_name']=='combined'
                         else f'passes/{image["pass_name"]}/{image["frame"]:06d}.{suffix}')
                src=safe_path(source,old['path'],exists=True);dst=safe_path(stage,newpath)
                dst.parent.mkdir(parents=True,exist_ok=True)
                total+=old['bytes']
                require(total<=request['settings']['resources']['storage_bytes'],
                        'storage-exhaustion','Delivery exceeds request storage budget.')
                _copy_artifact(src,dst,old)
                image['file']=dict(old,path=newpath);images.append(image)
            provenance='provenance/'+format(frame['frame'],'08d')+'.json'
            target=safe_path(stage,provenance);write_metadata(provenance,record)
            logs.append(dict(path=provenance,sha256=sha256(target),bytes=target.stat().st_size))
        manifest=dict(version=1,job=job,request_sha256=object_hash(request),backend=backend,
                      images=images,provenance=logs)
        write_metadata('manifest.json',manifest)
        manifest_ref=dict(path='manifest.json',sha256=sha256(stage/'manifest.json'),bytes=(stage/'manifest.json').stat().st_size)
        indices=[frame['frame'] for frame in state['frames']]
        result=dict(schema_version=1,kind='render3d-result',job=job,request_sha256=object_hash(request),
                    snapshot_sha256=spec['snapshot_sha256'],backend=backend,status='partial',
                    requested_frames=indices,successful=[i for i in indices if i not in reused],failed=[],skipped=[],
                    reused=[i for i in indices if i in reused],
                    images=images,diagnostics=[],usage=dict(elapsed_seconds=None,peak_memory_bytes=None,
                    output_bytes=total,measurement='unavailable'),logs=logs,manifest=manifest_ref,
                    verification=dict(status='not-run',verifier=None,evidence=None))
        report=verifier(request,result,stage)
        require(isinstance(report,dict) and isinstance(report.get('verifier'),dict),
                'verification-failed','Verifier did not return its identity.')
        write_metadata('verification.json',report)
        result['verification']=dict(status='passed',verifier=report['verifier'],evidence=dict(
            path='verification.json',sha256=sha256(stage/'verification.json'),bytes=(stage/'verification.json').stat().st_size))
        result['status']='complete';validate(result,'RenderResult')
        # Include the result's own bytes in its truthful output usage.
        for _ in range(8):
            payload=(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
            actual=total+len(payload)
            if result['usage']['output_bytes']==actual:break
            result['usage']['output_bytes']=actual
        else:raise Render3DError('invalid-input','Unable to encode stable delivery usage.')
        require(len(payload)<=MAX_RESULT_JSON_BYTES,'storage-exhaustion','Delivery result exceeds its read bound.')
        require(actual<=budget,'storage-exhaustion','Complete delivery exceeds request storage budget.')
        atomic_bytes(stage/'result.json',payload)
        return result,dict(path='result.json',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())
