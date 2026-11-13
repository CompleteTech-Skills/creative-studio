"""Durable Render3D frame jobs, bounded claims and independently verified cache.

SQLite owns lifecycle state; immutable cache directories own verified bytes.
Expired heartbeats never authorize taking work from a live owner process.
"""
from __future__ import annotations
from contextlib import contextmanager
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
import tempfile
import time
import uuid

from atomic_publish import _rename_no_replace
from rig_errors import RigError, atomic_bytes, atomic_json, decode_json, object_hash, safe_path, sha256
from render3d_contracts import Render3DError, require, validate, make_frame_spec, frame_times, rational, open_regular

VERSION = 1
TERMINAL = ('complete', 'cancelled', 'failed')
RETRYABLE = ('timeout','worker-crash','device-initialization')


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _metadata(path):
    """Decode bounded metadata from the same admitted regular descriptor."""
    limit=32*1024**2
    try:
        with open_regular(path,limit) as stream:
            return decode_json(stream.read(limit+1),max_bytes=limit)
    except (OSError,RigError) as exc:
        raise Render3DError('invalid-input',str(exc),path=path) from exc


def _alive(pid):
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
        marker = _metadata(safe_path(self.root,'store.json',exists=True))
        require(marker == dict(version=VERSION, host=socket.gethostname(), capacity=self.capacity),
                'ownership', 'Store host, version or capacities differ; open with its original configuration.')
        safe_path(self.root,'jobs.sqlite3',exists=True)
        self.root=self.root.resolve()
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS storage_reservations('
                       'stage TEXT PRIMARY KEY, bytes INTEGER NOT NULL, pid INTEGER NOT NULL)')
            if 'job' not in {row['name'] for row in db.execute('PRAGMA table_info(storage_reservations)')}:
                db.execute('ALTER TABLE storage_reservations ADD COLUMN job TEXT')

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
            result['frames']=[dict(x) for x in db.execute('SELECT frame,state,attempts,pid,token,heartbeat,error,cache_key FROM frames WHERE job=? ORDER BY frame',(job,))]
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

    def _storage_reserved(self, db, live):
        reservations=[]
        for row in db.execute('SELECT * FROM storage_reservations').fetchall():
            if _alive(row['pid']):reservations.append(row)
            else:
                # The bytes remain evidence; only the dead owner's reservation
                # is released. Physical leftovers are then counted below.
                db.execute('DELETE FROM storage_reservations WHERE stage=?',(row['stage'],))
        reserved=sum(json.loads(row['request'])['settings']['resources']['storage_bytes'] for row in live)
        reserved+=sum(row['bytes'] for row in reservations)
        return reserved+self._stored_output_bytes(active_tokens=[row['token'] for row in live],
            reserved_stages=[row['stage'] for row in reservations])

    def cache_inventory(self):
        with self._transaction() as db:
            pinned={row[0] for row in db.execute('SELECT DISTINCT cache_key FROM frames')}
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
            pinned={row[0] for row in db.execute('SELECT DISTINCT cache_key FROM frames')}
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
        states=[row[0] for row in db.execute('SELECT state FROM frames WHERE job=?',(job,))]
        cancel=db.execute('SELECT cancel FROM jobs WHERE id=?',(job,)).fetchone()[0]
        state=('running' if 'running' in states else 'cancelled' if cancel else
               'complete' if all(s=='complete' for s in states) else
               'failed' if 'failed' in states and 'queued' not in states else 'queued')
        db.execute('UPDATE jobs SET state=? WHERE id=?',(state,job))

    def claim(self, job=None):
        """Claim one priority-ordered frame only when global reservations permit."""
        with self._transaction() as db:
            live=list(db.execute('SELECT j.request,j.backend,f.token FROM frames f JOIN jobs j ON j.id=f.job WHERE f.state="running"'))
            used={name:sum(json.loads(r['request'])['settings']['resources'][name] for r in live)
                  for name in ('workers','memory_bytes','storage_bytes')}
            used['storage_bytes']=self._storage_reserved(db,live)
            gpu_used=sum(json.loads(r['backend'])['device']!='cpu' for r in live)
            query='SELECT f.*,j.request,j.backend,j.snapshot FROM frames f JOIN jobs j ON j.id=f.job WHERE f.state="queued" AND j.cancel=0'
            args=[]
            if job is not None:query+=' AND j.id=?';args.append(job)
            query+=' ORDER BY j.priority DESC,j.created,f.frame'
            for row in db.execute(query,args).fetchall():
                request=json.loads(row['request']);backend=json.loads(row['backend'])
                budget=request['settings']['resources'];gpu=backend['device']!='cpu'
                if any(used[n]+budget[n]>self.capacity[n] for n in used):continue
                if gpu and gpu_used>=self.capacity['gpu_workers']:continue
                token=uuid.uuid4().hex
                db.execute('UPDATE frames SET state="running",attempts=attempts+1,token=?,pid=?,heartbeat=? WHERE job=? AND frame=?',
                           (token,os.getpid(),self.clock(),row['job'],row['frame']))
                self._event(db,row['job'],'claimed',{'frame':row['frame'],'token':token})
                self._refresh(db,row['job'])
                return dict(job=row['job'],frame=row['frame'],token=token,request=request,backend=backend,
                            snapshot=row['snapshot'],cache_key=row['cache_key'],spec=json.loads(row['spec']))
        return None

    def _owned(self, db, lease):
        row=db.execute('SELECT * FROM frames WHERE job=? AND frame=?',(lease['job'],lease['frame'])).fetchone()
        require(row is not None and row['state']=='running' and row['token']==lease['token']
                and row['pid']==os.getpid(),'ownership','Frame lease is stale or belongs to another process.')
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

    def fail(self, lease, error, *, retryable=False):
        with self._transaction() as db:
            row=self._owned(db,lease)
            job=db.execute('SELECT request,cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()
            state='cancelled' if job['cancel'] else 'queued' if retryable and row['attempts']<json.loads(job['request'])['max_attempts'] else 'failed'
            db.execute('UPDATE frames SET state=?,error=?,token=NULL,pid=NULL,heartbeat=NULL WHERE job=? AND frame=?',
                       (state,str(error)[:2048],lease['job'],lease['frame']))
            self._event(db,lease['job'],'attempt-failed',{'frame':lease['frame'],'state':state,'error':str(error)[:2048]})
            self._refresh(db,lease['job'])

    def recover(self, job=None):
        """Requeue dead-process claims; retain live claims despite old heartbeats."""
        recovered=[]
        with self._transaction() as db:
            query='SELECT f.*,j.request,j.cancel FROM frames f JOIN jobs j ON j.id=f.job WHERE f.state="running"'
            args=[]
            if job is not None:query+=' AND f.job=?';args.append(job)
            for row in db.execute(query,args).fetchall():
                if _alive(row['pid']):continue
                state='cancelled' if row['cancel'] else 'queued' if row['attempts']<json.loads(row['request'])['max_attempts'] else 'failed'
                db.execute('UPDATE frames SET state=?,token=NULL,pid=NULL,heartbeat=NULL,error="Owner process exited" WHERE job=? AND frame=?',
                           (state,row['job'],row['frame']))
                recovered.append((row['job'],row['frame']))
                self._event(db,row['job'],'owner-exited',{'frame':row['frame'],'state':state});self._refresh(db,row['job'])
        return recovered

    def resume(self, job, *, retry_failed=False, expected_revision=None, verifier=None):
        self.recover(job)
        snapshot=self.status(job)
        completed=[frame for frame in snapshot['frames'] if frame['state']=='complete']
        require(not completed or verifier is not None,'verification-failed',
                'Resuming completed frames requires an independent verifier.')
        invalid=[]
        for frame in completed:
            spec=make_frame_spec(snapshot['request'],frame['frame'],Path(snapshot['snapshot'])/'snapshot.json')
            lease=dict(request=snapshot['request'],frame=frame['frame'],spec=spec,
                       cache_key=frame['cache_key'],backend=snapshot['backend'])
            try:self._verify_cache(lease,verifier)
            except (Render3DError,ValueError,OSError,KeyError):invalid.append(frame['frame'])
        with self._transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            require(row is not None,'invalid-input','Unknown render job.')
            require(expected_revision is None or row['revision']==expected_revision,'ownership','Stale job revision.')
            require(row['revision']==snapshot['revision'],'ownership','Job changed during resume verification.')
            require(not db.execute('SELECT 1 FROM frames WHERE job=? AND state="running"',(job,)).fetchone(),
                    'ownership','Cannot resume while a live worker owns a frame.')
            db.execute('UPDATE jobs SET cancel=0 WHERE id=?',(job,))
            states=['cancelled']+(['failed'] if retry_failed else [])
            for state in states:
                db.execute('UPDATE frames SET state="queued",error=NULL WHERE job=? AND state=? AND attempts<?',
                           (job,state,json.loads(row['request'])['max_attempts']))
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

    def _verify_cache(self, lease, verifier):
        path=safe_path(self.root,'cache/'+lease['cache_key'])
        require(path.is_dir() and not path.is_symlink(),'missing-asset','Cache entry unavailable.')
        record=_metadata(safe_path(path,'cache.json',exists=True))
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
        verifier(record['request'],result,path)
        return record

    def reuse(self, lease, verifier):
        """Re-decode and hash every artifact; stale entries are never accepted."""
        # Revalidate source bytes, even if a cache directory already exists.
        current=make_frame_spec(lease['request'],lease['frame'],Path(lease['snapshot'])/'snapshot.json')
        require(current==lease['spec'],'stale-asset','Source changed since job submission.')
        if lease['request']['cache_policy']=='refresh':return False
        try:self._verify_cache(lease,verifier)
        except (Render3DError,ValueError,OSError,KeyError):return False
        with self._transaction() as db:
            self._owned(db,lease)
            require(not db.execute('SELECT cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()[0],
                    'cancelled','Cancellation requested before cache completion.')
            db.execute('UPDATE frames SET state="complete",token=NULL,pid=NULL,heartbeat=NULL,error=NULL WHERE job=? AND frame=?',
                       (lease['job'],lease['frame']))
            self._event(db,lease['job'],'cache-reused',{'frame':lease['frame']});self._refresh(db,lease['job'])
        return True

    def publish(self, lease, output_root, result, verifier):
        """Copy verified artifacts to private staging, reverify, publish exclusively."""
        output_root=Path(output_root)
        require(not output_root.is_symlink(),'ownership','Output root cannot be a symlink.')
        validate(result,'RenderResult')
        require(result['status']=='complete','incomplete-output','Only complete verified frames can enter cache.')
        require(result['backend']==lease['backend'],'stale-cache','Backend identity changed.')
        request=self.frame_request(lease)
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
            live=list(db.execute('SELECT j.request,f.token FROM frames f JOIN jobs j ON j.id=f.job WHERE f.state="running"'))
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
            require(fresh==report,'verification-failed','Copied artifact verification differs from admitted source evidence.')
            atomic_bytes(stage/'cache.json',payload)
            with self._transaction() as db:
                self._owned(db,lease)
                require(not db.execute('SELECT cancel FROM jobs WHERE id=?',(lease['job'],)).fetchone()[0],
                        'cancelled','Cancellation requested before publication.')
                destination=safe_path(self.root,'cache/'+lease['cache_key'])
                used=self._stored_output_bytes()
                require(destination.exists() or used<=self.capacity['storage_bytes'],
                        'storage-exhaustion','Verified cache publication exceeds store capacity.')
                try:_rename_no_replace(stage,destination)
                except FileExistsError:
                    try:
                        existing=self._verify_cache(lease,verifier)
                    except (Render3DError,ValueError,OSError,KeyError):
                        # Preserve corrupt bytes as evidence before installing a
                        # newly rendered and independently verified replacement.
                        require(used<=self.capacity['storage_bytes'],
                                'storage-exhaustion','Cache repair would exceed capacity while retaining corrupt evidence.')
                        quarantine=safe_path(self.root,'staging/corrupt-'+uuid.uuid4().hex)
                        _rename_no_replace(destination,quarantine)
                        try:_rename_no_replace(stage,destination)
                        except Exception:
                            _rename_no_replace(quarantine,destination)
                            raise
                        self._event(db,lease['job'],'cache-corruption-repaired',
                                    {'frame':lease['frame'],'preserved':str(quarantine.relative_to(self.root))})
                    else:
                        expected=[(i['frame'],i['pass_name'],i['file']['sha256']) for i in existing['result']['images']]
                        actual=[(i['frame'],i['pass_name'],i['file']['sha256']) for i in result['images']]
                        require(expected==actual,'stale-cache','Same render inputs produced different refreshed pixels.')
                db.execute('UPDATE frames SET state="complete",token=NULL,pid=NULL,heartbeat=NULL,error=NULL WHERE job=? AND frame=?',
                           (lease['job'],lease['frame']))
                self._event(db,lease['job'],'frame-published',{'frame':lease['frame'],'cache_key':lease['cache_key']});self._refresh(db,lease['job'])
        finally:
            try:
                if created and stage.exists():shutil.rmtree(stage)
            finally:
                with self._transaction() as db:
                    db.execute('DELETE FROM storage_reservations WHERE stage=? AND pid=?',
                               (stage.name,os.getpid()))
        return record

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
            try:self.fail(lease,exc,retryable=isinstance(exc,Render3DError) and exc.report['category'] in RETRYABLE)
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
        require(state['state']=='complete','incomplete-output','Job is not complete.')
        request=state['request'];backend=state['backend']
        destination=Path(destination).absolute()
        require(not destination.exists() and not destination.is_symlink(),
                'ownership','Delivery destination already exists.')
        require(not destination.parent.resolve().is_relative_to(self.root),
                'ownership','Delivery destination cannot be inside the job store.')
        destination.parent.mkdir(parents=True,exist_ok=True)
        stage=Path(tempfile.mkdtemp(prefix='.render3d-delivery-',dir=destination.parent))
        try:
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
                record=self._verify_cache(lease,verifier)
                source=safe_path(self.root,'cache/'+frame['cache_key'])
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
                        requested_frames=indices,successful=indices,failed=[],skipped=[],reused=[],
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
            require(actual<=budget,'storage-exhaustion','Complete delivery exceeds request storage budget.')
            atomic_bytes(stage/'result.json',payload)
            with self._transaction() as db:
                live=db.execute('SELECT revision,state FROM jobs WHERE id=?',(job,)).fetchone()
                require(live['revision']==state['revision'] and live['state']=='complete',
                        'ownership','Job changed while delivering frames.')
                _rename_no_replace(stage,destination)
                self._event(db,job,'delivery-published',dict(destination=str(destination),result_sha256=sha256(destination/'result.json')))
            return result
        finally:
            if stage.exists():shutil.rmtree(stage)
